// pwm_ledc.cpp - Wokwi PWM backend (-DBUCK_PWM_LEDC). Wokwi does not implement MCPWM on the
// classic ESP32 but does implement LEDC.
//
// Only the duty and the dead time carry over from the product: one LEDC timer at the 80 MHz
// APB clock, 9 bit (512 counts of 12.5 ns), integer divider 1 -> fsw = 156.25 kHz, not the
// product's 200 kHz. The controller still runs at 50 kHz and still outputs 400-tick duties.
//
// Mapping: d = round(duty * 512 / 400). 512/400 > 1, so every controller code maps to a
// distinct LEDC code (no extra quantisation).
//   channel H (GPIO18): high from hpoint 0 for d counts                -> [0, d)
//   channel L (GPIO19): high from hpoint d + dt to 512 - dt            -> [d + dt, 512 - dt)
// dt = 4 counts = 50 ns on both edges, and L never wraps past the period end.
// Duty 0: H has duty 0 = never high; L stays on as synchronous rectifier (same as MCPWM).
//
// Update order, safe without knowing how LEDC latches. Which register latches when (duty at the
// next overflow after ledc_update_duty(), hpoint at once or also at overflow) is not established
// here: no TRM page is cited and nothing was measured on silicon (Wokwi's model only shows that
// Wokwi latches both together). So the order is chosen to be overlap-free in every case:
//   duty rising  : write L first, H second, same control tick. L_new rises at d_new + dt, later
//                  than H_old falls (d_old) and H_new falls later still; true whichever of the two
//                  registers applies first.
//   duty falling : write H now, L only on the next control tick (20 us, more than one 6.4 us
//                  period). Meanwhile L keeps its old hpoint d_old + dt, which is later than
//                  both H_old's and H_new's falling edge, so there is no overlap before or after
//                  H latches. The dead time only grows for that one tick.
// Off = both duties to 0, then ledc_stop() on both channels with idle level 0 (see pwm_off()).
//
// Simulator build only: the 5 V ADC transfer and the time dilation make this build wrong for a
// real board. pwm_enable() therefore refuses unless the chip reports Wokwi's default MAC
// (24:0A:C4:00:01:10), and the BOOT line carries a warning either way.
#ifdef BUCK_PWM_LEDC
#include "pwm_backend.h"
#include "fw_config.h"
#include "esp_err.h"
#include "esp_mac.h"
#include "esp_rom_sys.h"
#include "driver/ledc.h"
extern "C" {
#include "buck_config.h"
}

#define LEDC_MODE   LEDC_HIGH_SPEED_MODE
#define LEDC_TMR    LEDC_TIMER_0
#define LEDC_CH_H   LEDC_CHANNEL_0
#define LEDC_CH_L   LEDC_CHANNEL_1
#define LEDC_TOP    512u
#define LEDC_FSW    156250u
#define LEDC_DT     4u

static uint32_t s_d;            // commanded LEDC high-side counts
static uint32_t s_h, s_l;       // counts last written to channel H / L (valid once enabled)
static bool s_enabled, s_latched_off;

static bool on_wokwi(void)
{
    static const uint8_t k_mac[6] = {0x24, 0x0A, 0xC4, 0x00, 0x01, 0x10};
    uint8_t mac[6] = {};
    if (esp_efuse_mac_get_default(mac) != ESP_OK) return false;
    for (int k = 0; k < 6; k++) if (mac[k] != k_mac[k]) return false;
    return true;
}

static uint32_t map_duty(uint16_t ticks)
{
    uint32_t d = ((uint32_t)ticks * LEDC_TOP + BUCK_PWM_TICKS / 2) / BUCK_PWM_TICKS;
    if (d > LEDC_TOP - 2 * LEDC_DT) d = LEDC_TOP - 2 * LEDC_DT;   // keeps L inside the period
    return d;
}

static esp_err_t write_h(uint32_t d)
{
    esp_err_t e = ledc_set_duty_with_hpoint(LEDC_MODE, LEDC_CH_H, d, 0);
    if (e == ESP_OK) e = ledc_update_duty(LEDC_MODE, LEDC_CH_H);
    if (e == ESP_OK) s_h = d;
    return e;
}

static esp_err_t write_l(uint32_t d)
{
    esp_err_t e = ledc_set_duty_with_hpoint(LEDC_MODE, LEDC_CH_L, LEDC_TOP - 2 * LEDC_DT - d, d + LEDC_DT);
    if (e == ESP_OK) e = ledc_update_duty(LEDC_MODE, LEDC_CH_L);
    if (e == ESP_OK) s_l = d;
    return e;
}
void pwm_init(void)
{
    ledc_timer_config_t t = {};
    t.speed_mode = LEDC_MODE;
    t.duty_resolution = LEDC_TIMER_9_BIT;
    t.timer_num = LEDC_TMR;
    t.freq_hz = LEDC_FSW;
    t.clk_cfg = LEDC_USE_APB_CLK;
    ESP_ERROR_CHECK(ledc_timer_config(&t));

    ledc_channel_config_t c = {};
    c.speed_mode = LEDC_MODE;
    c.intr_type = LEDC_INTR_DISABLE;
    c.timer_sel = LEDC_TMR;
    c.duty = 0;                    // both channels start with zero duty = low
    c.hpoint = 0;
    c.gpio_num = PIN_PWM_H;
    c.channel = LEDC_CH_H;
    ESP_ERROR_CHECK(ledc_channel_config(&c));
    c.gpio_num = PIN_PWM_L;
    c.channel = LEDC_CH_L;
    ESP_ERROR_CHECK(ledc_channel_config(&c));
    ESP_ERROR_CHECK(ledc_stop(LEDC_MODE, LEDC_CH_H, 0));   // held low until pwm_enable()
    ESP_ERROR_CHECK(ledc_stop(LEDC_MODE, LEDC_CH_L, 0));
}

bool pwm_set(uint16_t duty_ticks)
{
    if (s_latched_off) return true;
    const uint32_t d = map_duty(duty_ticks);
    s_d = d;                       // while held only stored; pwm_enable() writes it
    if (!s_enabled) return true;
    esp_err_t e = ESP_OK;
    if (s_l != s_h) e = write_l(s_h);            // L catches up with the previous falling step
    if (e == ESP_OK && d > s_h) { e = write_l(d); if (e == ESP_OK) e = write_h(d); }
    else if (e == ESP_OK && d < s_h) e = write_h(d);   // L follows on the next call
    return e == ESP_OK;
}

bool pwm_enable(void)
{
    if (s_latched_off || s_enabled) return true;
    if (!on_wokwi()) return false;              // sim build on a real board: never switch
    // Both channels were stopped low, so either order is safe; L first, H second.
    // ledc_update_duty() re-enables the outputs that ledc_stop() disabled.
    if (write_l(s_d) != ESP_OK || write_h(s_d) != ESP_OK) return false;
    s_enabled = true;
    return true;
}

bool pwm_off(void)
{
    // Zero both duties first, wait two PWM periods for the zero to latch, then ledc_stop() with
    // idle level 0. Reason: with ledc_stop() alone, one Wokwi run left GPIO18 high for at least
    // 23 ms of simulated time after the latch (the plant saw H on, the expect-pin check failed).
    // The cause is suspected, not established: the simulated channel may keep its last output
    // level when stopped while high. A latched-zero duty drives the pin low either way.
    bool ok = ledc_set_duty_with_hpoint(LEDC_MODE, LEDC_CH_H, 0, 0) == ESP_OK;
    ok = (ledc_set_duty_with_hpoint(LEDC_MODE, LEDC_CH_L, 0, 0) == ESP_OK) && ok;
    ok = (ledc_update_duty(LEDC_MODE, LEDC_CH_H) == ESP_OK) && ok;    // high side first
    ok = (ledc_update_duty(LEDC_MODE, LEDC_CH_L) == ESP_OK) && ok;
    esp_rom_delay_us(2 * 1000000u / LEDC_FSW + 1);                    // 14 us
    ok = (ledc_stop(LEDC_MODE, LEDC_CH_H, 0) == ESP_OK) && ok;
    ok = (ledc_stop(LEDC_MODE, LEDC_CH_L, 0) == ESP_OK) && ok;
    s_latched_off = true;
    return ok;
}

const char *pwm_note(void)
{
    return on_wokwi() ? "SIM_BUILD_NOT_FOR_HARDWARE mac=wokwi"
                      : "SIM_BUILD_NOT_FOR_HARDWARE mac=NOT_WOKWI_PWM_WILL_NOT_ENABLE";
}
const char *pwm_name(void) { return "LEDC"; }
uint32_t pwm_fsw_hz(void) { return LEDC_FSW; }
#endif // BUCK_PWM_LEDC

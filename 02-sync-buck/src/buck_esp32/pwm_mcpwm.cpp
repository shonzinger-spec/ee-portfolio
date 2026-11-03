// pwm_mcpwm.cpp - product PWM backend: ESP32 MCPWM group 0, one timer / operator.
//
// Timer   : 80 MHz resolution (12.5 ns/tick), count up, 400-tick period = 200 kHz.
// Compare : one comparator, value shadowed and loaded on timer-empty (TEZ), so a new duty
//           only takes effect at a period boundary.
// Gen A   : GPIO18 PWM_H. High on TEZ, low on compare -> raw high-side pulse of d ticks.
// Dead time (Espressif "active high complementary" mode): H = A with its rising edge delayed
//           4 ticks; L (GPIO19) = NOT A with the falling edge of A delayed 4 ticks. So 50 ns on
//           both edges, and the effective high-side pulse is d - 4 ticks; the loop's integrator
//           absorbs that offset like any other loss. IDF clocks the dead-time module from the
//           connected timer, so 4 ticks = 4 x 12.5 ns (check on a scope at bring-up).
//
// Off / hold: the generator force (mcpwm_generator_set_force_level) acts BEFORE the dead-time
//           module (IDF header: the force level "can be inverted by ... dead-time module").
//           Forcing A low therefore drives L = NOT A HIGH, and forcing generator B does
//           nothing because output B is built from A. So "both low" uses the fault handler,
//           which sits after the dead-time module: a software fault triggers a one-shot (OST)
//           brake whose action is LOW on both outputs. A is also force-held low, so H stays low
//           at the generator even if the brake were ever cleared.
// Duty 0:   with TEZ -> high and compare -> low at the same count the result would depend on
//           the event priority, so duty 0 is done by force-holding A low instead (H low for
//           whole periods, L on as synchronous rectifier, as in RUN at duty 0).
// Status: this backend is compile-verified only. It has never run on silicon or in any simulator
// (Wokwi has no MCPWM on the classic ESP32), so the force / brake / dead-time ordering above is
// argued from the IDF headers. pwm_enable() and pwm_off() return the driver status so a refused
// recovery shows up as a firmware fault (FAULT PWM) instead of silent gates.
// No MCPWM fault GPIO: the board has no comparator to drive one; over-current is the
// controller's 3-sample latch (60 us). A hard short needs that comparator (see buck_config.h).
#ifndef BUCK_PWM_LEDC
#include "pwm_backend.h"
#include "fw_config.h"
#include "esp_err.h"
#include "driver/mcpwm_prelude.h"
extern "C" {
#include "buck_config.h"
}

static mcpwm_timer_handle_t s_timer;
static mcpwm_oper_handle_t s_oper;
static mcpwm_cmpr_handle_t s_cmpr;
static mcpwm_gen_handle_t s_gen_h, s_gen_l;
static mcpwm_fault_handle_t s_fault;
static uint16_t s_duty;
static bool s_enabled, s_latched_off, s_zero_forced;

static esp_err_t hold_a_low(bool hold)
{
    // -1 removes the force; takes effect immediately (mcpwm_ll: upmethod 0)
    const esp_err_t e = mcpwm_generator_set_force_level(s_gen_h, hold ? 0 : -1, true);
    if (e == ESP_OK) s_zero_forced = hold;
    return e;
}

void pwm_init(void)
{
    mcpwm_timer_config_t tcfg = {};
    tcfg.group_id = 0;
    tcfg.clk_src = MCPWM_TIMER_CLK_SRC_DEFAULT;
    tcfg.resolution_hz = 80000000;
    tcfg.count_mode = MCPWM_TIMER_COUNT_MODE_UP;
    tcfg.period_ticks = BUCK_PWM_TICKS;
    ESP_ERROR_CHECK(mcpwm_new_timer(&tcfg, &s_timer));

    mcpwm_operator_config_t ocfg = {};
    ocfg.group_id = 0;
    ocfg.flags.update_gen_action_on_tez = 1;
    ocfg.flags.update_dead_time_on_tez = 1;
    ESP_ERROR_CHECK(mcpwm_new_operator(&ocfg, &s_oper));
    ESP_ERROR_CHECK(mcpwm_operator_connect_timer(s_oper, s_timer));

    mcpwm_comparator_config_t ccfg = {};
    ccfg.flags.update_cmp_on_tez = 1;
    ESP_ERROR_CHECK(mcpwm_new_comparator(s_oper, &ccfg, &s_cmpr));
    ESP_ERROR_CHECK(mcpwm_comparator_set_compare_value(s_cmpr, 0));

    // Generators carry no actions and no dead time yet, so both pins come up low
    // (R10/R11 have held them low until now).
    mcpwm_generator_config_t gcfg = {};
    gcfg.gen_gpio_num = PIN_PWM_H;
    ESP_ERROR_CHECK(mcpwm_new_generator(s_oper, &gcfg, &s_gen_h));
    gcfg.gen_gpio_num = PIN_PWM_L;
    ESP_ERROR_CHECK(mcpwm_new_generator(s_oper, &gcfg, &s_gen_l));

    // Software fault -> one-shot brake -> both outputs LOW (after the dead-time module)
    mcpwm_soft_fault_config_t fcfg = {};
    ESP_ERROR_CHECK(mcpwm_new_soft_fault(&fcfg, &s_fault));
    mcpwm_brake_config_t bcfg = {};
    bcfg.fault = s_fault;
    bcfg.brake_mode = MCPWM_OPER_BRAKE_MODE_OST;
    ESP_ERROR_CHECK(mcpwm_operator_set_brake_on_fault(s_oper, &bcfg));
    mcpwm_gen_handle_t gens[2] = {s_gen_h, s_gen_l};
    for (int g = 0; g < 2; g++) {
        ESP_ERROR_CHECK(mcpwm_generator_set_action_on_brake_event(gens[g],
            MCPWM_GEN_BRAKE_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP, MCPWM_OPER_BRAKE_MODE_OST,
                                         MCPWM_GEN_ACTION_LOW)));
        ESP_ERROR_CHECK(mcpwm_generator_set_action_on_brake_event(gens[g],
            MCPWM_GEN_BRAKE_EVENT_ACTION(MCPWM_TIMER_DIRECTION_DOWN, MCPWM_OPER_BRAKE_MODE_OST,
                                         MCPWM_GEN_ACTION_LOW)));
    }
    hold_a_low(true);

    ESP_ERROR_CHECK(mcpwm_timer_enable(s_timer));
    ESP_ERROR_CHECK(mcpwm_timer_start_stop(s_timer, MCPWM_TIMER_START_NO_STOP));
    ESP_ERROR_CHECK(mcpwm_soft_fault_activate(s_fault));   // brake latched: both low

    // Waveform and dead time only now, with the brake already holding both outputs low
    ESP_ERROR_CHECK(mcpwm_generator_set_action_on_timer_event(s_gen_h,
        MCPWM_GEN_TIMER_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP, MCPWM_TIMER_EVENT_EMPTY,
                                     MCPWM_GEN_ACTION_HIGH)));
    ESP_ERROR_CHECK(mcpwm_generator_set_action_on_compare_event(s_gen_h,
        MCPWM_GEN_COMPARE_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP, s_cmpr, MCPWM_GEN_ACTION_LOW)));
    mcpwm_dead_time_config_t dt = {};
    dt.posedge_delay_ticks = BUCK_DEADTIME_TICKS;
    ESP_ERROR_CHECK(mcpwm_generator_set_dead_time(s_gen_h, s_gen_h, &dt));
    dt.posedge_delay_ticks = 0;
    dt.negedge_delay_ticks = BUCK_DEADTIME_TICKS;
    dt.flags.invert_output = 1;
    ESP_ERROR_CHECK(mcpwm_generator_set_dead_time(s_gen_h, s_gen_l, &dt));
}

bool pwm_set(uint16_t duty_ticks)
{
    if (s_latched_off) return true;
    if (duty_ticks > BUCK_PWM_TICKS - 1) duty_ticks = BUCK_PWM_TICKS - 1;  // counter peak is 399
    esp_err_t e = ESP_OK;
    if (duty_ticks != s_duty) {
        e = mcpwm_comparator_set_compare_value(s_cmpr, duty_ticks);         // loads at next TEZ
        if (e == ESP_OK) s_duty = duty_ticks;
    }
    if (e != ESP_OK || !s_enabled) return e == ESP_OK;   // still held: pwm_enable() decides the force
    if (duty_ticks == 0 && !s_zero_forced) e = hold_a_low(true);
    else if (duty_ticks != 0 && s_zero_forced) e = hold_a_low(false);
    return e == ESP_OK;
}

bool pwm_enable(void)
{
    if (s_latched_off || s_enabled) return true;
    if (s_duty != 0 && hold_a_low(false) != ESP_OK) return false;
    // The release lands mid-period: A stays low until the next TEZ, so the rest of that one
    // period (< 5 us) has L on and H off, then switching starts with the loaded compare.
    // IDF refuses the recovery (ESP_ERR_INVALID_STATE) while it still considers the fault active;
    // then the brake stays on and the gates never switch, so the caller must latch a fault.
    if (mcpwm_operator_recover_from_fault(s_oper, s_fault) != ESP_OK) {
        (void)hold_a_low(true);
        return false;
    }
    s_enabled = true;
    return true;
}

bool pwm_off(void)
{
    bool ok = mcpwm_soft_fault_activate(s_fault) == ESP_OK;   // both outputs low now (fault handler)
    ok = (hold_a_low(true) == ESP_OK) && ok;
    ok = (mcpwm_comparator_set_compare_value(s_cmpr, 0) == ESP_OK) && ok;
    s_duty = 0;
    s_latched_off = true;
    return ok;
}

const char *pwm_note(void) { return nullptr; }
const char *pwm_name(void) { return "MCPWM"; }
uint32_t pwm_fsw_hz(void) { return 80000000u / BUCK_PWM_TICKS; }
#endif // !BUCK_PWM_LEDC

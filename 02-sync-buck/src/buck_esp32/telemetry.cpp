// telemetry.cpp - SPSC ring and the low-priority Serial printer (line formats: buck_esp32.ino).
#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "telemetry.h"
#include "fw_config.h"

static_assert((FW_RING_LEN & (FW_RING_LEN - 1)) == 0, "FW_RING_LEN must be a power of two");

static rec_t s_ring[FW_RING_LEN];
static uint32_t s_head;        // written by the producer only
static uint32_t s_tail;        // written by the consumer only
static uint32_t s_drop_samples, s_drop_events;   // written by the producer only
static boot_info_t s_boot;

bool telem_push(const rec_t *r, bool is_event)
{
    const uint32_t head = s_head;
    const uint32_t used = head - __atomic_load_n(&s_tail, __ATOMIC_ACQUIRE);
    const uint32_t limit = is_event ? FW_RING_LEN : FW_RING_LEN - FW_RING_RESERVE;
    if (used >= limit) {
        if (is_event) __atomic_store_n(&s_drop_events, s_drop_events + 1, __ATOMIC_RELAXED);
        else          __atomic_store_n(&s_drop_samples, s_drop_samples + 1, __ATOMIC_RELAXED);
        return false;
    }
    s_ring[head & (FW_RING_LEN - 1)] = *r;
    __atomic_store_n(&s_head, head + 1, __ATOMIC_RELEASE);   // publish after the copy
    return true;
}

static bool telem_pop(rec_t *r)
{
    const uint32_t tail = s_tail;
    if (tail == __atomic_load_n(&s_head, __ATOMIC_ACQUIRE)) return false;
    *r = s_ring[tail & (FW_RING_LEN - 1)];
    __atomic_store_n(&s_tail, tail + 1, __ATOMIC_RELEASE);   // free the slot after the copy
    return true;
}

static const char *state_name(uint8_t s)
{
    static const char *const names[] = {"OFF", "SOFTSTART", "RUN", "FAULT", "FW_FAULT"};
    return s <= FW_STATE_FW_FAULT ? names[s] : "?";
}

static void print_rec(const rec_t *r)
{
    const long long t = (long long)r->t_us;
    const float mhz = (float)getCpuFrequencyMhz();
    switch (r->kind) {
    case REC_SAMPLE:
        Serial.printf("%lld,%.3f,%.3f,%.2f,%u,%u,%lu\n", t, r->f[0], r->f[1], r->f[2],
                      (unsigned)r->duty, (unsigned)r->state, (unsigned long)r->overruns);
        break;
    case REC_CAP_BEGIN:
        Serial.printf("CAP begin t_us=%lld reason=%s\n", t, r->reason == CAP_DI ? "DI" : "STATE");
        break;
    case REC_CAP_END:
        Serial.printf("CAP end t_us=%lld\n", t);
        break;
    case REC_START:
        Serial.printf("START t_us=%lld vout_V=%.3f\n", t, r->f[0]);
        break;
    case REC_READY:
        Serial.printf("READY t_us=%lld\n", t);
        break;
    case REC_SS:
        Serial.printf("SS t_us=%lld t_1pct_us=%ld t_run_us=%ld\n", t, (long)r->n[0], (long)r->n[1]);
        break;
    case REC_STEP:
        Serial.printf("STEP t_us=%lld i_from_A=%.3f i_to_A=%.3f vmin_V=%.3f vmax_V=%.3f settle_us=%ld %s\n",
                      t, r->f[0], r->f[1], r->f[2], r->f[3], (long)r->n[0],
                      r->reason ? "STEP_OK" : "STEP_FAIL");
        break;
    case REC_FAULT:
        Serial.printf("FAULT %s t_us=%lld trip_us=%ld vout_V=%.3f il_A=%.3f vin_V=%.2f "
                      "raw_v=%u raw_i=%u raw_vin=%u duty_ticks=%u overruns=%lu\n",
                      r->reason == FLT_OCP ? "OCP" : r->reason == FLT_OVERRUN ? "OVERRUN" : "PWM", t, (long)r->n[0],
                      r->f[0], r->f[1], r->f[2], (unsigned)r->raw[0], (unsigned)r->raw[1],
                      (unsigned)r->raw[2], (unsigned)r->duty, (unsigned long)r->overruns);
        break;
    case REC_STAT:
        Serial.printf("STAT t_us=%lld state=%s exec_max_us=%.2f exec_mean_us=%.2f updates=%ld "
                      "overruns=%lu drop_samples=%lu drop_events=%lu\n",
                      t, state_name(r->state), (float)r->n[0] / mhz, (float)r->n[1] / mhz,
                      (long)r->n[2], (unsigned long)r->overruns,
                      (unsigned long)__atomic_load_n(&s_drop_samples, __ATOMIC_RELAXED),
                      (unsigned long)__atomic_load_n(&s_drop_events, __ATOMIC_RELAXED));
        break;
    default:
        break;
    }
}

static void telem_task(void *)
{
    Serial.printf("BOOT fw=buck_esp32 pwm=%s fsw_hz=%lu ctrl_hz=%lu adc_cal=%s baud=%lu "
                  "cpu_mhz=%lu overrun_limit=%d\n",
                  s_boot.pwm, (unsigned long)s_boot.fsw_hz,
                  (unsigned long)(FW_TICK_RES_HZ / FW_TICK_COUNTS), s_boot.adc_cal,
                  (unsigned long)FW_BAUD, (unsigned long)getCpuFrequencyMhz(), FW_OVERRUN_LIMIT);
    if (s_boot.note) Serial.printf("WARNING %s: not for hardware (5 V ADC transfer, time dilation)\n", s_boot.note);
#ifdef FW_SIM_WOKWI
    Serial.printf("SIM wokwi time_scale=%d tick_sim_us=%d (all t_us below are control time, 20 us per tick)\n",
                  FW_SIM_TIME_SCALE, FW_TICK_COUNTS * FW_SIM_TIME_SCALE);
#endif
    Serial.printf("t_us,vout_V,il_A,vin_V,duty_ticks,state,overruns\n");
    uint32_t seen_ds = 0, seen_de = 0;
    rec_t r;
    for (;;) {
        bool any = false;
        while (telem_pop(&r)) { print_rec(&r); any = true; }
        const uint32_t ds = __atomic_load_n(&s_drop_samples, __ATOMIC_RELAXED);
        const uint32_t de = __atomic_load_n(&s_drop_events, __ATOMIC_RELAXED);
        if (ds != seen_ds || de != seen_de) {
            Serial.printf("DROP t_us=%lld samples=%lu events=%lu\n", (long long)esp_timer_get_time(),
                          (unsigned long)ds, (unsigned long)de);
            seen_ds = ds;
            seen_de = de;
        }
        if (!any) vTaskDelay(1);
    }
}

void telem_start(const boot_info_t *boot)
{
    s_boot = *boot;
    xTaskCreatePinnedToCore(telem_task, "telem", FW_TELEM_STACK, nullptr, FW_TELEM_PRIO,
                            nullptr, FW_TELEM_CORE);
}

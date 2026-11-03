// control.cpp - runs src/buck_ctrl on target.
//
// Timing: the Xtensa ESP32 may not use the FPU inside an ISR and buck_ctrl uses float, so the
// gptimer alarm ISR (every 20 us) does integer work only and wakes the control task with a
// direct-to-task notification. The task (core 1, high priority) reads the three ADC channels,
// calls buck_update and loads the PWM. The gptimer is created from inside the task, so its
// interrupt is allocated on core 1 and the ISR and task share one core (plain volatiles are
// enough between them).
//
// The gptimer (APB) and the MCPWM timer (PLL) both come from the 40 MHz crystal, so the
// 50 kHz tick keeps a fixed but arbitrary phase to the 200 kHz switching period; the ADC
// samples are not synchronised to the switching edges.
//
// Overruns: the ISR counts a tick that arrives before the previous tick's update has finished
// (s_ticks != s_done). That also covers a notification count > 1, which can only happen after
// such a tick. FW_OVERRUN_LIMIT consecutive missed ticks latch a firmware fault: the count adds
// every missed tick of an update, so one stall spanning several ticks latches as well.
//
// Wokwi build (FW_SIM_WOKWI, set by the LEDC variant): no gptimer interrupt; the task waits on
// the CPU cycle counter instead, with the same overrun rule (see wait_tick() and fw_config.h).
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_cpu.h"
#include "esp_rom_sys.h"
#include "driver/gptimer.h"
#include "control.h"
#include "fw_config.h"
#include "pwm_backend.h"
#include "adc_sense.h"
#include "telemetry.h"
extern "C" {
#include "buck_ctrl.h"
#include "buck_config.h"
}

static_assert(FW_TICK_RES_HZ == 1000000, "trace times below assume 1 us gptimer counts");
#define FW_FAST_US   (FW_FAST_DIV * FW_TICK_COUNTS)   // 100 us per trace sample

static TaskHandle_t s_task;
#ifndef FW_SIM_WOKWI
static gptimer_handle_t s_tick_timer;
#endif
static volatile uint32_t s_ticks;      // ISR: ticks issued
static volatile uint32_t s_done;       // task: last tick whose update finished
static volatile uint32_t s_overruns;   // ISR: ticks that found the previous update unfinished

#ifndef FW_SIM_WOKWI
static bool IRAM_ATTR on_tick(gptimer_handle_t, const gptimer_alarm_event_data_t *, void *)
{
    if (s_ticks != s_done) s_overruns = s_overruns + 1;
    s_ticks = s_ticks + 1;
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(s_task, &woken);
    return woken == pdTRUE;
}

static void start_tick_timer(void)
{
    gptimer_config_t tc = {};
    tc.clk_src = GPTIMER_CLK_SRC_DEFAULT;
    tc.direction = GPTIMER_COUNT_UP;
    tc.resolution_hz = FW_TICK_RES_HZ;
    ESP_ERROR_CHECK(gptimer_new_timer(&tc, &s_tick_timer));
    gptimer_event_callbacks_t cbs = {};
    cbs.on_alarm = on_tick;
    ESP_ERROR_CHECK(gptimer_register_event_callbacks(s_tick_timer, &cbs, nullptr));
    gptimer_alarm_config_t ac = {};
    ac.alarm_count = FW_TICK_COUNTS;
    ac.reload_count = 0;
    ac.flags.auto_reload_on_alarm = 1;
    ESP_ERROR_CHECK(gptimer_set_alarm_action(s_tick_timer, &ac));
    ESP_ERROR_CHECK(gptimer_enable(s_tick_timer));
    ESP_ERROR_CHECK(gptimer_start(s_tick_timer));
}

static void wait_tick(void) { (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY); }
static int64_t ctrl_time_us(void) { return esp_timer_get_time(); }
#else  // FW_SIM_WOKWI
// Simulator-only tick (see fw_config.h): no alarm interrupt; the control task waits on the CPU
// cycle counter for the next deadline, 20 us x FW_SIM_TIME_SCALE of simulated time apart.
// Same overrun rule as on_tick(): every deadline that passes while an update is still running
// is one overrun, and the late update then runs at once (like a pending notification).
// Time is control time: 20 us per tick, counted from the first tick.
static uint32_t s_tick_cyc, s_next;

static int64_t ctrl_time_us(void) { return (int64_t)s_ticks * FW_TICK_COUNTS; }

static void start_tick_timer(void)
{
    s_tick_cyc = esp_rom_get_cpu_ticks_per_us() * (1000000u / FW_TICK_RES_HZ) * FW_TICK_COUNTS *
                 FW_SIM_TIME_SCALE;
    s_next = esp_cpu_get_cycle_count() + s_tick_cyc;
}

static void wait_tick(void)
{
    const uint32_t now = esp_cpu_get_cycle_count();
    if ((int32_t)(now - s_next) >= 0) {                  // deadline(s) passed during the update
        const uint32_t missed = (now - s_next) / s_tick_cyc + 1;
        s_overruns = s_overruns + missed;
        s_ticks = s_ticks + missed;
        s_next += missed * s_tick_cyc;
    } else {
        while ((int32_t)(esp_cpu_get_cycle_count() - s_next) < 0) {}
        s_ticks = s_ticks + 1;
        s_next += s_tick_cyc;
    }
}
#endif  // FW_SIM_WOKWI

static void push_event(uint8_t kind, int64_t t, uint8_t reason, uint8_t state, uint32_t ovr)
{
    rec_t r = {};
    r.kind = kind;
    r.t_us = t;
    r.reason = reason;
    r.state = state;
    r.overruns = ovr;
    (void)telem_push(&r, true);
}

// Both gates low; if the backend reports a failure the stage may not really be off, so say so.
static void shut_down(int64_t t, uint32_t ovr)
{
    if (pwm_off()) return;
    rec_t r = {};
    r.kind = REC_FAULT;
    r.reason = FLT_PWM;
    r.t_us = t;
    r.state = FW_STATE_FW_FAULT;
    r.overruns = ovr;
    (void)telem_push(&r, true);
}

typedef struct {
    bool active, had_out;
    int64_t t0, last_out;
    float i_from, vmin, vmax;
} step_ev_t;

static void finish_step(step_ev_t *s, float i_to, int32_t settle_us, uint8_t state, uint32_t ovr)
{
    rec_t r = {};
    r.kind = REC_STEP;
    r.t_us = s->t0;
    r.f[0] = s->i_from;
    r.f[1] = i_to;
    r.f[2] = s->vmin;
    r.f[3] = s->vmax;
    r.n[0] = settle_us;
    r.reason = (settle_us >= 0 && settle_us <= FW_STEP_SETTLE_MAX_US && s->vmin >= FW_STEP_VMIN_V &&
                s->vmax <= FW_STEP_VMAX_V) ? 1 : 0;
    r.state = state;
    r.overruns = ovr;
    (void)telem_push(&r, true);
    s->active = false;
}

static void ctrl_task(void *)
{
    s_task = xTaskGetCurrentTaskHandle();
    start_tick_timer();

    const float band_v = FW_BAND_FRAC * BUCK_VOUT_REF_V;
    buck_ctrl_t c;
    buck_init(&c);
    int phase = 0;                   // 0 first tick: buck_start; 1 first update (held); 2 running
    bool enabled = false, latched = false;
    uint8_t fw_state = BUCK_OFF, st_prev = BUCK_OFF;
    uint16_t last_duty = 0;
    bool oc_prev = false;
    int64_t t_first_oc = 0, t_first_ov = 0;
    uint32_t ov_prev = 0, ov_consec = 0;
    uint32_t ex_max = 0, ex_n = 0;
    uint64_t ex_sum = 0;
    // trace
    uint32_t acc_v = 0, acc_i = 0, acc_vin = 0, acc_n = 0, fast_idx = 0;
    float i_prev = 0.0f, i_prev2 = 0.0f;
    bool have_prev = false, state_trig = false, capturing = false;
    int32_t post_left = 0;
    int64_t last_pushed = -1;
    rec_t hist[FW_CAP_PRE];
    uint32_t hist_w = 0, hist_n = 0;
    // events
    int64_t t_start = 0, t_band = -1, t_run = -1;
    bool ss_reported = false;
    step_ev_t step = {};

    for (;;) {
        wait_tick();
        const uint32_t c0 = esp_cpu_get_cycle_count();
        const uint32_t snap = s_ticks;
        const int64_t t = ctrl_time_us();
        adc_sample_t a;
        adc_sense_read(&a);

        if (!latched) {
            if (phase == 0) {
                buck_start(&c, a.v);                 // ramp starts at the pre-bias level
                t_start = t;
                rec_t r = {};
                r.kind = REC_START;
                r.t_us = t;
                r.f[0] = a.v * BUCK_V_PER_COUNT;
                (void)telem_push(&r, true);
                phase = 1;
            } else {
                bool pwm_ok = true;
                if (phase == 2 && !enabled) { pwm_ok = pwm_enable(); enabled = true; }
                const uint16_t duty = buck_update(&c, a.v, a.i, a.vin);
                const bool oc = a.i * BUCK_A_PER_COUNT > BUCK_OCP_TRIP_A;   // same test as buck_ctrl
                if (oc && !oc_prev) t_first_oc = t;
                oc_prev = oc;
                if (c.state == BUCK_FAULT) {
                    shut_down(t, s_overruns);
                    latched = true;
                    rec_t r = {};
                    r.kind = REC_FAULT;
                    r.reason = FLT_OCP;
                    r.t_us = t;
                    r.n[0] = (int32_t)(t - t_first_oc);
                    r.f[0] = a.v * BUCK_V_PER_COUNT;
                    r.f[1] = a.i * BUCK_A_PER_COUNT;
                    r.f[2] = a.vin * BUCK_VIN_PER_COUNT;
                    r.raw[0] = a.raw_v;
                    r.raw[1] = a.raw_i;
                    r.raw[2] = a.raw_vin;
                    r.duty = last_duty;
                    r.state = BUCK_FAULT;
                    r.overruns = s_overruns;
                    (void)telem_push(&r, true);
                } else {
                    if (pwm_ok) pwm_ok = pwm_set(duty);
                    last_duty = duty;
                    if (!pwm_ok) {                       // enable refused or a driver call failed
                        shut_down(t, s_overruns);
                        latched = true;
                        fw_state = FW_STATE_FW_FAULT;
                        rec_t r = {};
                        r.kind = REC_FAULT;
                        r.reason = FLT_PWM;
                        r.t_us = t;
                        r.f[0] = a.v * BUCK_V_PER_COUNT;
                        r.f[1] = a.i * BUCK_A_PER_COUNT;
                        r.f[2] = a.vin * BUCK_VIN_PER_COUNT;
                        r.raw[0] = a.raw_v;
                        r.raw[1] = a.raw_i;
                        r.raw[2] = a.raw_vin;
                        r.duty = last_duty;
                        r.state = FW_STATE_FW_FAULT;
                        r.overruns = s_overruns;
                        (void)telem_push(&r, true);
                    }
                }
                if (phase == 1) phase = 2;
            }
        }
        if (!(latched && fw_state == FW_STATE_FW_FAULT)) fw_state = (uint8_t)c.state;

        if (fw_state != st_prev) {
            state_trig = true;
            if (st_prev == BUCK_SOFTSTART && fw_state == BUCK_RUN) {
                t_run = t;
                push_event(REC_READY, t, 0, fw_state, s_overruns);
            }
            st_prev = fw_state;
        }

        // ---- 100 us trace point -------------------------------------------------------
        acc_v += a.v;
        acc_i += a.i;
        acc_vin += a.vin;
        if (++acc_n == FW_FAST_DIV) {
            rec_t s = {};
            s.kind = REC_SAMPLE;
            s.t_us = t;
            s.f[0] = (float)acc_v / FW_FAST_DIV * BUCK_V_PER_COUNT;
            s.f[1] = (float)acc_i / FW_FAST_DIV * BUCK_A_PER_COUNT;
            s.f[2] = (float)acc_vin / FW_FAST_DIV * BUCK_VIN_PER_COUNT;
            s.duty = latched ? 0 : last_duty;
            s.state = fw_state;
            s.overruns = s_overruns;
            acc_v = acc_i = acc_vin = acc_n = 0;
            const float vm = s.f[0], im = s.f[1];
            const bool di = have_prev && fabsf(im - i_prev) > FW_DI_TRIG_A;
            // The step is already partly in the row before the trigger row (the trigger needs a
            // change of more than FW_DI_TRIG_A between two rows), so the "from" level is the row
            // two back, and the step began in the previous row if that row already moved by more
            // than FW_DI_START_A, else in this one.
            const bool began_prev = fabsf(i_prev - i_prev2) > FW_DI_START_A;
            const float i_ref = began_prev ? i_prev2 : i_prev;
            const int64_t t_begin = began_prev ? t - 2 * FW_FAST_US : t - FW_FAST_US;
            i_prev2 = i_prev;
            i_prev = im;
            have_prev = true;

            // capture window: 1 ms history + 4 ms after the (last) trigger
            const bool trig = di || state_trig;
            if (trig && !capturing) {
                push_event(REC_CAP_BEGIN, t, state_trig ? CAP_STATE : CAP_DI, fw_state, s_overruns);
                for (uint32_t j = 0; j < hist_n; j++) {
                    const rec_t *h = &hist[(hist_w + FW_CAP_PRE - hist_n + j) % FW_CAP_PRE];
                    if (h->t_us > last_pushed) { (void)telem_push(h, false); last_pushed = h->t_us; }
                }
                capturing = true;
            }
            state_trig = false;
            if (trig) post_left = FW_CAP_POST;
            if (capturing) {
                (void)telem_push(&s, false);
                last_pushed = t;
                if (--post_left <= 0) {
                    capturing = false;
                    push_event(REC_CAP_END, t, 0, fw_state, s_overruns);
                }
            } else if (fast_idx % (FW_BG_DIV / FW_FAST_DIV) == 0) {
                (void)telem_push(&s, false);
                last_pushed = t;
            }
            hist[hist_w] = s;
            hist_w = (hist_w + 1) % FW_CAP_PRE;
            if (hist_n < FW_CAP_PRE) hist_n++;
            fast_idx++;

            // soft start: START -> first 100 us mean within 1 % of 5 V, and START -> RUN
            const bool out = fabsf(vm - BUCK_VOUT_REF_V) > band_v;
            if (phase > 0 && t_band < 0 && !out && fw_state <= BUCK_RUN) t_band = t;
            if (!ss_reported && t_band >= 0 && t_run >= 0) {
                rec_t r = {};
                r.kind = REC_SS;
                r.t_us = t;
                r.n[0] = (int32_t)(t_band - t_start);
                r.n[1] = (int32_t)(t_run - t_start);
                r.state = fw_state;
                (void)telem_push(&r, true);
                ss_reported = true;
            }

            // load step: vmin/vmax and time back inside +/-1 % (held FW_SETTLE_HOLD_US)
            if (step.active) {
                if (di || fw_state != BUCK_RUN) {
                    finish_step(&step, im, -2, fw_state, s_overruns);      // interrupted
                } else {
                    if (vm < step.vmin) step.vmin = vm;
                    if (vm > step.vmax) step.vmax = vm;
                    if (out) { step.last_out = t; step.had_out = true; }
                    const int64_t ref = step.had_out ? step.last_out : step.t0;
                    if (t - ref >= FW_SETTLE_HOLD_US)
                        finish_step(&step, im,
                                    step.had_out ? (int32_t)(step.last_out + FW_FAST_US - step.t0) : 0,
                                    fw_state, s_overruns);
                    else if (t - step.t0 >= FW_STEP_TIMEOUT_US)
                        finish_step(&step, im, -1, fw_state, s_overruns);
                }
            }
            if (di && fw_state == BUCK_RUN && !step.active) {
                step.active = true;
                step.t0 = t_begin;
                step.i_from = i_ref;
                step.vmin = step.vmax = vm;
                step.had_out = out;
                step.last_out = out ? t : 0;
            }
        }

#ifdef FW_TEST_STALL_TICKS
        {   // negative test: one update that takes FW_TEST_STALL_TICKS ticks, once
            static bool stalled = false;
            if (!stalled && !latched && enabled && t >= FW_TEST_STALL_AT_US) {
                stalled = true;
#ifdef FW_SIM_WOKWI
                const uint32_t tick_cyc = s_tick_cyc;
#else
                const uint32_t tick_cyc = esp_rom_get_cpu_ticks_per_us() * FW_TICK_COUNTS;
#endif
                const uint32_t w0 = esp_cpu_get_cycle_count();
                while (esp_cpu_get_cycle_count() - w0 < FW_TEST_STALL_TICKS * tick_cyc) {}
            }
        }
#endif
        const uint32_t cyc = esp_cpu_get_cycle_count() - c0;
        s_done = snap;

        // ---- overrun accounting -----------------------------------------------------------
        const uint32_t ov = s_overruns;
        if (ov != ov_prev) {
            if (ov_consec == 0) t_first_ov = t;
            ov_consec += ov - ov_prev;                 // missed ticks, not late updates
        } else {
            ov_consec = 0;
        }
        ov_prev = ov;
        if (!latched && ov_consec >= FW_OVERRUN_LIMIT) {
            shut_down(t, ov);
            latched = true;
            fw_state = FW_STATE_FW_FAULT;
            const int64_t now = ctrl_time_us();
            rec_t r = {};
            r.kind = REC_FAULT;
            r.reason = FLT_OVERRUN;
            r.t_us = now;
            r.n[0] = (int32_t)(now - t_first_ov);
            r.f[0] = a.v * BUCK_V_PER_COUNT;
            r.f[1] = a.i * BUCK_A_PER_COUNT;
            r.f[2] = a.vin * BUCK_VIN_PER_COUNT;
            r.raw[0] = a.raw_v;
            r.raw[1] = a.raw_i;
            r.raw[2] = a.raw_vin;
            r.duty = last_duty;
            r.state = FW_STATE_FW_FAULT;
            r.overruns = ov;
            (void)telem_push(&r, true);
        }

        // ---- execution time (cycle counter), reported once per second ----------------------
        if (cyc > ex_max) ex_max = cyc;
        ex_sum += cyc;
        if (++ex_n >= FW_STAT_DIV) {
            rec_t r = {};
            r.kind = REC_STAT;
            r.t_us = t;
            r.n[0] = (int32_t)ex_max;
            r.n[1] = (int32_t)(ex_sum / ex_n);
            r.n[2] = (int32_t)ex_n;
            r.state = fw_state;
            r.overruns = ov;
            (void)telem_push(&r, true);
            ex_max = 0;
            ex_sum = 0;
            ex_n = 0;
        }
    }
}

void control_start(void)
{
    xTaskCreatePinnedToCore(ctrl_task, "buck_ctrl", FW_CTRL_STACK, nullptr, FW_CTRL_PRIO,
                            nullptr, FW_CTRL_CORE);
}

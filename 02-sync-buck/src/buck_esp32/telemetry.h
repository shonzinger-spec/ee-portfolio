// telemetry.h - records passed from the control task (core 1) to the telemetry task (core 0)
// through a single-producer / single-consumer ring. Only the telemetry task touches Serial.
#pragma once
#include <stdint.h>

enum rec_kind_t : uint8_t {
    REC_SAMPLE = 0,   // trace row (100 us mean)
    REC_CAP_BEGIN,    // reason: CAP_DI / CAP_STATE
    REC_CAP_END,
    REC_START,        // f[0] = Vout at buck_start (pre-bias)
    REC_READY,        // soft start finished (state RUN)
    REC_SS,           // n[0] = t to within 1 %, n[1] = t to RUN, both from START
    REC_STEP,         // f[0] i_from, f[1] i_to, f[2] vmin, f[3] vmax, n[0] settle_us, reason 1 = within limits
    REC_FAULT,        // reason: FLT_OCP / FLT_OVERRUN; n[0] trip_us; f[0..2] v/i/vin; raw[0..2]
    REC_STAT,         // n[0] exec max cycles, n[1] exec mean cycles, n[2] updates in window
};
enum { CAP_DI = 0, CAP_STATE = 1 };
enum { FLT_OCP = 0, FLT_OVERRUN = 1, FLT_PWM = 2 };   // FLT_PWM: a PWM backend call failed or refused

// Trace state column: buck_state_t (0 OFF, 1 SOFTSTART, 2 RUN, 3 FAULT = OCP latch) or
// 4 = firmware fault (overrun latch).
#define FW_STATE_FW_FAULT 4

typedef struct {
    int64_t t_us;        // esp_timer_get_time()
    float f[4];
    int32_t n[3];
    uint32_t overruns;   // overrun count so far
    uint16_t raw[3];
    uint16_t duty;       // controller ticks
    uint8_t kind;
    uint8_t state;
    uint8_t reason;
} rec_t;

typedef struct {
    const char *pwm;
    uint32_t fsw_hz;
    const char *adc_cal;
    const char *note;    // nullptr or a warning printed with the BOOT line (sim build marker)
} boot_info_t;

// Producer side (control task only). Samples may not use the last FW_RING_RESERVE slots,
// so events (START/READY/SS/STEP/FAULT/STAT, capture markers) still fit when samples drop.
// A refused record is counted, never lost silently.
bool telem_push(const rec_t *r, bool is_event);
// Start the telemetry task (core 0, low priority); it prints BOOT first.
void telem_start(const boot_info_t *boot);

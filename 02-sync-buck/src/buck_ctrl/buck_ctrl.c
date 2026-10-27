/* buck_ctrl.c - digital PI voltage-mode controller for the 02 synchronous buck. */
#include "buck_ctrl.h"
#include "buck_config.h"

static float clampf(float x, float lo, float hi) { return x < lo ? lo : (x > hi ? hi : x); }

void buck_init(buck_ctrl_t *c)
{
    c->state = BUCK_OFF;
    c->fault = BUCK_FAULT_NONE;
    c->vref = 0.0f;
    c->integ = 0.0f;
    c->duty = 0;
    c->oc_count = 0;
}

void buck_start(buck_ctrl_t *c, uint16_t adc_v)
{
    if (c->state == BUCK_FAULT) return;          /* latched: needs buck_init() */
    c->vref = adc_v * BUCK_V_PER_COUNT;          /* start the ramp at the pre-bias level */
    c->integ = 0.0f;
    c->duty = 0;
    c->oc_count = 0;
    c->state = BUCK_SOFTSTART;
}

uint16_t buck_update(buck_ctrl_t *c, uint16_t adc_v, uint16_t adc_i, uint16_t adc_vin)
{
    if (c->state == BUCK_OFF || c->state == BUCK_FAULT) { c->duty = 0; return 0; }

    const float v = adc_v * BUCK_V_PER_COUNT;
    const float i = adc_i * BUCK_A_PER_COUNT;
    float vin = adc_vin * BUCK_VIN_PER_COUNT;
    if (vin < BUCK_VIN_FF_MIN_V) vin = BUCK_VIN_FF_MIN_V;

    /* Over-current latch */
    if (i > BUCK_OCP_TRIP_A) {
        if (++c->oc_count >= BUCK_OCP_SAMPLES) {
            c->state = BUCK_FAULT;
            c->fault = BUCK_FAULT_OCP;
            c->duty = 0;
            return 0;
        }
    } else {
        c->oc_count = 0;
    }

    /* Soft-start reference: 1 V/ms ramp that turns into a first-order approach for the last
     * part, so the LC filter sees no slope step at the end (no ramp-stop overshoot). */
    if (c->state == BUCK_SOFTSTART) {
        const float rem = BUCK_VOUT_REF_V - c->vref;
        float step = BUCK_SOFTSTART_V_PER_S * BUCK_TS_S;
        const float approach = rem * (BUCK_TS_S / BUCK_SOFTSTART_TAU_S);
        if (approach < step) step = approach;
        c->vref += step;
        if (rem - step < BUCK_SOFTSTART_DONE_V) { c->vref = BUCK_VOUT_REF_V; c->state = BUCK_RUN; }
    }

    /* u = wanted average switch-node voltage = Vref (reference feed-forward) + PI correction;
     * the integrator only has to supply the IR drops and load-dependent losses. */
    const float e = c->vref - v;
    const float e_int = (e > -BUCK_DEADBAND_V && e < BUCK_DEADBAND_V) ? 0.0f : e;
    const float u_lo = BUCK_DUTY_MIN * vin, u_hi = BUCK_DUTY_MAX * vin;
    const float u_try = c->vref + BUCK_KP * e + c->integ + BUCK_KI * BUCK_TS_S * e_int;

    /* Anti-windup: conditional integration, then clamp the integrator to the duty range */
    const int pushing_high = (u_try > u_hi) && (e_int > 0.0f);
    const int pushing_low = (u_try < u_lo) && (e_int < 0.0f);
    if (!pushing_high && !pushing_low)                      /* AW-COND */
        c->integ += BUCK_KI * BUCK_TS_S * e_int;
    c->integ = clampf(c->integ, u_lo - c->vref, u_hi - c->vref); /* AW-CLAMP */

    /* Feed-forward: duty = wanted switch-node voltage / measured Vin */
    const float duty = clampf((c->vref + BUCK_KP * e + c->integ) / vin, BUCK_DUTY_MIN, BUCK_DUTY_MAX);
    const float target = duty * BUCK_PWM_TICKS;
    const float diff = target - (float)c->duty;
    if (diff > BUCK_DPWM_HYST_LSB || diff < -BUCK_DPWM_HYST_LSB)    /* DPWM-HYST */
        c->duty = (uint16_t)(target + 0.5f);
    return c->duty;
}

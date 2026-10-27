/* test_buck_ctrl.c - model-in-the-loop host test of src/buck_ctrl against the averaged
 * plant (plant.c). The controller runs as it would on the MCU: one update per 20 us, the ADC
 * sampled at the start of the period, the new duty applied one period later.
 * Prints one PASS/FAIL line per check and exits 1 on any failure. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include "buck_ctrl.h"
#include "buck_config.h"
#include "plant.h"

#define SUBSTEPS 40                      /* 0.5 us plant steps per 20 us control period */
#define NMAX 6000                        /* 120 ms of control periods */

static int fails, checks;
static void check(int ok, const char *name, const char *fmt, double a, double b)
{
    char buf[160];
    snprintf(buf, sizeof buf, fmt, a, b);
    printf("%s %-32s %s\n", ok ? "PASS" : "FAIL", name, buf);
    checks++;
    if (!ok) fails++;
}

/* Trace, one sample per control period */
static double T[NMAX], V[NMAX], ILPK[NMAX];
static int D[NMAX], ST[NMAX];
static int n;

typedef struct { double t, vin_to, ramp_s, rload; } event_t;

static void simulate(const event_t *ev, int nev, double t_end)
{
    plant_t p; buck_ctrl_t c;
    plant_init(&p, BUCK_VIN_NOM_V, 5.0);
    buck_init(&c);
    buck_start(&c, plant_adc_v(&p));
    const double ts = BUCK_TS_S, dt = ts / SUBSTEPS;
    uint16_t duty_applied = 0;
    double vin_from = p.vin, vin_to = p.vin, ramp_t0 = 0, ramp_len = 0;
    int e = 0;
    for (n = 0; n < NMAX && n * ts < t_end; n++) {
        const double t = n * ts;
        while (e < nev && ev[e].t <= t + 1e-12) {
            if (ev[e].rload > 0) p.rload = ev[e].rload;
            if (ev[e].vin_to > 0) { vin_from = p.vin; vin_to = ev[e].vin_to; ramp_t0 = t; ramp_len = ev[e].ramp_s; }
            e++;
        }
        const uint16_t next = buck_update(&c, plant_adc_v(&p), plant_adc_i(&p), plant_adc_vin(&p));
        double pk = 0;
        for (int k = 0; k < SUBSTEPS; k++) {
            const double tt = t + k * dt;
            p.vin = (ramp_len > 0 && tt < ramp_t0 + ramp_len)
                        ? vin_from + (vin_to - vin_from) * (tt - ramp_t0) / ramp_len : vin_to;
            const double d = (c.state == BUCK_FAULT) ? 0.0 : (double)duty_applied / BUCK_PWM_TICKS;
            plant_step(&p, d, dt);
            const double ipk = plant_il_peak(&p, d);
            if (ipk > pk) pk = ipk;
        }
        duty_applied = next;
        T[n] = t + ts; V[n] = plant_vload(&p); D[n] = duty_applied; ST[n] = c.state; ILPK[n] = pk;
    }
}

static int idx(double t) { int i = (int)(t / BUCK_TS_S + 0.5); return i < 0 ? 0 : (i >= n ? n - 1 : i); }
static double vmax(double a, double b) { double m = -1e9; for (int i = idx(a); i <= idx(b); i++) if (V[i] > m) m = V[i]; return m; }
static double vmin(double a, double b) { double m = 1e9; for (int i = idx(a); i <= idx(b); i++) if (V[i] < m) m = V[i]; return m; }
static double vmean(double a, double b) { double s = 0; int k = 0; for (int i = idx(a); i <= idx(b); i++, k++) s += V[i]; return s / k; }
/* time after t0 at which V last left the +/-tol band before t1 (settling time) */
static double settle(double t0, double t1, double tol)
{
    double last = t0;
    for (int i = idx(t0); i <= idx(t1); i++) if (fabs(V[i] - BUCK_VOUT_REF_V) > tol) last = T[i];
    return last - t0;
}
static int duty_changes(double a, double b) { int k = 0; for (int i = idx(a) + 1; i <= idx(b); i++) if (D[i] != D[i - 1]) k++; return k; }

static void scenario_closed_loop(void)
{
    const event_t ev[] = {
        {20e-3, 0, 0, 10.0},          /* 1 A -> 0.5 A */
        {30e-3, 0, 0, 1.6667},        /* 0.5 A -> 3 A load step */
        {45e-3, 0, 0, 10.0},          /* 3 A -> 0.5 A load release */
        {55e-3, 0, 0, 5.0},           /* 0.5 A -> 1 A */
        {65e-3, 10.8, 1e-3, 0},       /* Vin 12 -> 10.8 V in 1 ms */
        {80e-3, 13.2, 1e-3, 0},       /* Vin 10.8 -> 13.2 V in 1 ms */
        {100e-3, 0, 0, 0.8},          /* overload 6.25 A -> OCP latch */
    };
    simulate(ev, sizeof ev / sizeof ev[0], 105e-3);
    const double tol = 0.01 * BUCK_VOUT_REF_V;

    printf("# closed loop: soft start at 1 A, Vin 12 V\n");
    check(vmax(0, 20e-3) <= 5.10, "startup overshoot <= 2 %", "max %.4f V (limit %.2f V)", vmax(0, 20e-3), 5.10);
    check(settle(0, 20e-3, tol) <= 8e-3, "startup settle +/-1 % <= 8 ms", "%.2f ms (limit %.0f ms)", settle(0, 20e-3, tol) * 1e3, 8);
    printf("# steady state 1 A, 12-20 ms\n");
    check(fabs(vmean(12e-3, 20e-3) - 5.0) <= 0.025, "dc error <= 0.5 %", "mean %.4f V (target %.1f V)", vmean(12e-3, 20e-3), 5.0);
    check(duty_changes(12e-3, 20e-3) == 0, "no limit cycle (duty steady)", "%.0f duty-code changes (limit %.0f)", duty_changes(12e-3, 20e-3), 0);
    check(vmax(12e-3, 20e-3) - vmin(12e-3, 20e-3) <= 0.005, "low-frequency p-p <= 5 mV", "%.2f mV (limit %.0f mV)", (vmax(12e-3, 20e-3) - vmin(12e-3, 20e-3)) * 1e3, 5);
    printf("# load step 0.5 -> 3 A at 30 ms, release 3 -> 0.5 A at 45 ms\n");
    check(vmin(30e-3, 45e-3) >= 4.50, "step dip >= 4.50 V (-10 %)", "min %.4f V (limit %.2f V)", vmin(30e-3, 45e-3), 4.50);
    check(settle(30e-3, 45e-3, tol) <= 5e-3, "step recovery +/-1 % <= 5 ms", "%.2f ms (limit %.0f ms)", settle(30e-3, 45e-3, tol) * 1e3, 5);
    check(vmax(45e-3, 55e-3) <= 5.50, "release peak <= 5.50 V (+10 %)", "max %.4f V (limit %.2f V)", vmax(45e-3, 55e-3), 5.50);
    check(settle(45e-3, 55e-3, tol) <= 5e-3, "release recovery +/-1 % <= 5 ms", "%.2f ms (limit %.0f ms)", settle(45e-3, 55e-3, tol) * 1e3, 5);
    printf("# line ramps at 1 A: 12 -> 10.8 V at 65 ms, 10.8 -> 13.2 V at 80 ms (1 ms each)\n");
    const double dev = fmax(vmax(65e-3, 100e-3) - 5.0, 5.0 - vmin(65e-3, 100e-3));
    check(dev <= 0.10, "line deviation <= 2 %", "%.1f mV (limit %.0f mV)", dev * 1e3, 100);
    check(settle(80e-3, 100e-3, tol) <= 5e-3, "line recovery +/-1 % <= 5 ms", "%.2f ms (limit %.0f ms)", settle(80e-3, 100e-3, tol) * 1e3, 5);
    printf("# overload 0.8 ohm (6.25 A) at 100 ms\n");
    int trip = -1; double ilpk = 0;
    for (int i = idx(100e-3); i < n; i++) { if (ILPK[i] > ilpk) ilpk = ILPK[i]; if (ST[i] == BUCK_FAULT) { trip = i - idx(100e-3); break; } }
    check(trip >= 0 && trip <= BUCK_OCP_SAMPLES, "OCP latches within 3 samples", "%.0f samples (limit %.0f)", trip, BUCK_OCP_SAMPLES);
    check(ilpk < 6.0, "inductor peak < Isat 6 A", "%.2f A (limit %.1f A)", ilpk, 6.0);
    check(D[n - 1] == 0 && ST[n - 1] == BUCK_FAULT, "stays off after the trip", "duty %.0f, state %.0f", D[n - 1], ST[n - 1]);
}

static void unit_checks(void)
{
    buck_ctrl_t c;
    const uint16_t a12 = (uint16_t)(12.0f / BUCK_VIN_PER_COUNT + 0.5f);
    const uint16_t adc5v = (uint16_t)(5.0f / BUCK_V_PER_COUNT + 0.5f);
    const uint16_t adc55 = (uint16_t)(5.5f / BUCK_V_PER_COUNT + 0.5f);
    printf("# unit: integrator windup (output held at 0 V for 20 ms, then 5.5 V)\n");
    buck_init(&c); buck_start(&c, 0);
    for (int i = 0; i < 1000; i++) buck_update(&c, 0, 0, a12);
    check(c.vref + c.integ <= BUCK_DUTY_MAX * 12.0f + 1e-3, "Vref + integrator <= Dmax x Vin", "%.3f V (limit %.2f V)", c.vref + c.integ, BUCK_DUTY_MAX * 12.0);
    /* Bound for a clamped integrator: from Dmax x Vin - Vref down to the value that gives 50 %
     * duty, at Ki x |e| with e = -0.5 V. */
    const double e55 = 5.0 - 5.5;
    const double bound = ((BUCK_DUTY_MAX * 12.0 - 5.0) - (0.5 * 12.0 - 5.0 - BUCK_KP * e55))
                         / (BUCK_KI * -e55);
    int k = 0;
    while (k < 2000 && buck_update(&c, adc55, 0, a12) > BUCK_PWM_TICKS / 2) k++;
    check(k * BUCK_TS_S <= 1.1 * bound, "unwinds to 50 % within 1.1 x bound", "%.2f ms (limit %.2f ms)",
          k * BUCK_TS_S * 1e3, 1.1 * bound * 1e3);
    printf("# unit: OCP needs consecutive samples, then latches\n");
    const uint16_t adc_oc = (uint16_t)((BUCK_OCP_TRIP_A + 0.5f) / BUCK_A_PER_COUNT);
    buck_init(&c); buck_start(&c, adc5v);
    buck_update(&c, adc5v, adc_oc, a12); buck_update(&c, adc5v, adc_oc, a12); buck_update(&c, adc5v, 0, a12);
    buck_update(&c, adc5v, adc_oc, a12); buck_update(&c, adc5v, adc_oc, a12);
    check(c.state != BUCK_FAULT, "2 + 2 split samples: no trip", "state %.0f (fault = %.0f)", c.state, BUCK_FAULT);
    buck_update(&c, adc5v, adc_oc, a12);
    buck_start(&c, adc5v);
    check(c.state == BUCK_FAULT && buck_update(&c, adc5v, 0, a12) == 0, "3rd sample trips, start ignored", "state %.0f, fault code %.0f", c.state, c.fault);
    printf("# unit: pre-biased start\n");
    buck_init(&c); buck_start(&c, (uint16_t)(3.0f / BUCK_V_PER_COUNT + 0.5f));
    check(fabs(c.vref - 3.0f) < 0.005f, "soft start begins at 3 V pre-bias", "vref %.3f V (expected %.1f V)", c.vref, 3.0);
}

int main(void)
{
    printf("02 sync buck controller - host test (model in the loop)\n");
    printf("Kp %.4g V/V, Ki %.4g V/(V s), Ts %.0f us, DPWM %d ticks, dead band %.0f mV, DPWM hysteresis %.2f LSB, OCP %.1f A x %d\n",
           BUCK_KP, BUCK_KI, BUCK_TS_S * 1e6, BUCK_PWM_TICKS, BUCK_DEADBAND_V * 1e3, BUCK_DPWM_HYST_LSB, BUCK_OCP_TRIP_A, BUCK_OCP_SAMPLES);
    scenario_closed_loop();
    unit_checks();
    printf("RESULT: %d/%d PASS\n", checks - fails, checks);
    return fails ? 1 : 0;
}

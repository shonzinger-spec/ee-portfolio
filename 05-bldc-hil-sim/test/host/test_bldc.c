/* Host test of the BLDC firmware with the switch-level plant in the loop.
 * Every timer tick (31.25 ns) the plant advances; at each update event (20 kHz) the firmware
 * ISR runs on the latched inputs and writes the preload registers, exactly as on the MCU.
 * Prints PASS/FAIL per check, METRIC lines for main.py, and RESULT; exit code 1 on any FAIL.
 * `test_bldc --trace DIR` also writes DIR/hil_trace.csv and DIR/pwm_trace.csv. */
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "bldc.h"
#include "plant.h"

#define TICK_NS (1e9 / BLDC_TIM_CLK_HZ)
#define RPM_TO_W (6.283185307179586 / 60.0)

static int n_pass, n_fail;

#include <stdarg.h>

__attribute__((format(printf, 3, 4)))
static void check(int ok, const char *name, const char *fmt, ...)
{
    char detail[96];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(detail, sizeof detail, fmt, ap);
    va_end(ap);
    printf("%s %-44s %s\n", ok ? "PASS" : "FAIL", name, detail);
    if (ok)
        n_pass++;
    else
        n_fail++;
}

static void metric(const char *name, double v)
{
    printf("METRIC %s %.6g\n", name, v);
}

typedef struct {
    plant_t   p;
    bldc_io_t io;
    bldc_t    m;
    long      isr_count;
    double    t_fault;      /* time of the ISR that latched a fault, -1 if none */
    /* statistics since stats_reset() */
    double    i_ph_peak, i_meas_peak, rpm_min, rpm_max, rpm_sum;
    long      n;
    double    i_abs_sum;    /* per tick: largest |phase current| (plant truth) */
    long      i_abs_n;
    double    t_band_out;   /* last time outside the +/-1 % band around band_rpm */
    double    band_rpm;
    FILE     *trace;        /* decimated waveform rows while non-NULL */
    int       trace_div;
} rig_t;

static void rig_init(rig_t *r)
{
    memset(r, 0, sizeof *r);
    plant_init(&r->p, &r->io);
    bldc_init(&r->m, &r->io);
    r->t_fault = -1.0;
}

static void stats_reset(rig_t *r, double band_rpm)
{
    r->i_ph_peak = r->i_meas_peak = r->rpm_sum = r->i_abs_sum = 0.0;
    r->i_abs_n = 0;
    r->rpm_min = 1e9;
    r->rpm_max = -1e9;
    r->n = 0;
    r->band_rpm = band_rpm;
    r->t_band_out = r->p.t;
}

static void trace_row(rig_t *r)
{
    const plant_t *p = &r->p;
    fprintf(r->trace, "%.7f,%d,%d%d%d%d%d%d,%d%d%d%d%d%d,%.4f,%.4f,%.4f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f\n",
            p->t, p->cnt, p->gate_h[0], p->gate_l[0], p->gate_h[1], p->gate_l[1], p->gate_h[2], p->gate_l[2],
            p->sw_h[0], p->sw_l[0], p->sw_h[1], p->sw_l[1], p->sw_h[2], p->sw_l[2],
            p->i[0], p->i[1], p->i[2], p->vt[0], p->vt[1], p->vt[2], p->e[0], p->e[1], p->e[2], p->vn);
}

/* Run until plant time t_end. */
static void run(rig_t *r, double t_end)
{
    while (r->p.t < t_end) {
        if (plant_tick(&r->p, &r->io)) {
            bldc_pwm_isr(&r->m, &r->io);
            r->isr_count++;
            if (r->t_fault < 0.0 && r->m.state == BLDC_FAULT)
                r->t_fault = r->p.t;
            if (r->m.i_meas > r->i_meas_peak)
                r->i_meas_peak = r->m.i_meas;
            double rpm = plant_rpm(&r->p);
            r->rpm_sum += rpm;
            r->n++;
            if (rpm < r->rpm_min)
                r->rpm_min = rpm;
            if (rpm > r->rpm_max)
                r->rpm_max = rpm;
            if (fabs(rpm - r->band_rpm) > 0.01 * r->band_rpm)
                r->t_band_out = r->p.t;
        }
        double imax = 0.0;
        for (int x = 0; x < 3; x++)
            if (fabs(r->p.i[x]) > imax)
                imax = fabs(r->p.i[x]);
        if (imax > r->i_ph_peak)
            r->i_ph_peak = imax;
        r->i_abs_sum += imax;
        r->i_abs_n++;
        if (r->trace && r->p.tick % (uint64_t)r->trace_div == 0)
            trace_row(r);
    }
}

static FILE *open_trace(const char *dir, const char *name)
{
    char path[512];
    FILE *f;
    snprintf(path, sizeof path, "%s/%s", dir, name);
    f = fopen(path, "w");
    if (f)
        fprintf(f, "t_s,cnt,gates_AhAlBhBlChCl,sw_AhAlBhBlChCl,ia,ib,ic,va,vb,vc,ea,eb,ec,vn\n");
    else
        printf("FAIL cannot write %s\n", path), n_fail++;
    return f;
}

/* ------------------------------------------------------------------ unit checks */
static void unit_checks(void)
{
    int s, ok = 1;
    /* each sector drives + the phase whose back-EMF is flat at +1, - the one flat at -1 */
    for (s = 0; s < 6; s++) {
        double mid = 60.0 + 60.0 * s;                 /* sector s spans 30 + 60 s .. 90 + 60 s */
        int k = (s + BLDC_COMM_ADVANCE) % 6;
        if (plant_trap(mid - 120.0 * bldc_sector_high[k]) != 1.0 ||
            plant_trap(mid - 120.0 * bldc_sector_low[k]) != -1.0)
            ok = 0;
    }
    check(ok, "unit: commutation table on back-EMF flat tops", "6 sectors %d/%d", ok ? 6 : 0, 6);

    check(BLDC_PWM_HZ == 20000, "unit: PWM frequency 20 kHz", "%ld Hz", (long)BLDC_PWM_HZ);
}

/* ------------------------------------------------------------------ scenarios */
static void s0_fixed_point(void)
{
    /* Open loop, no firmware: 2000 rpm imposed, duty 0.75 with the same compare scheme and
     * dead time, for comparison with sim/ngspice/inverter_6step.cir (main.py C13-C15). */
    static rig_t r;
    double duty = 0.75, t0 = 0.020, t1 = 0.035, isum = 0.0, i2 = 0.0;
    long n = 0;
    rig_init(&r);
    r.p.hold = 1;
    r.p.w = 2000.0 * RPM_TO_W;
    printf("# S0 fixed operating point: 2000 rpm imposed, duty 0.75, no controller\n");
    while (r.p.t < t1) {
        if (plant_tick(&r.p, &r.io)) {
            int s = bldc_hall_sector(r.p.hall);
            int c = BLDC_TIM_ARR - (int)(duty * BLDC_TIM_ARR + 0.5);
            for (int x = 0; x < 3; x++) {
                if (x == bldc_sector_high[s]) {
                    r.io.ccr_h[x] = (uint16_t)(c + BLDC_DT_TICKS / 2);
                    r.io.ccr_l[x] = (uint16_t)(c - (BLDC_DT_TICKS - BLDC_DT_TICKS / 2));
                } else if (x == bldc_sector_low[s]) {
                    r.io.ccr_h[x] = BLDC_CCR_NEVER;
                    r.io.ccr_l[x] = BLDC_CCR_NEVER;
                } else {
                    r.io.ccr_h[x] = BLDC_CCR_NEVER;
                    r.io.ccr_l[x] = 0;
                }
            }
            r.io.moe = 1;
        }
        if (r.p.t >= t0) {
            isum += r.p.i_src;
            i2 += r.p.i[0] * r.p.i[0];
            n++;
            if (fabs(r.p.i[0]) > r.i_ph_peak)
                r.i_ph_peak = fabs(r.p.i[0]);
        }
    }
    check(r.p.overlap_ticks == 0, "S0 no shoot-through", "%.0f ticks", (double)r.p.overlap_ticks);
    printf("  info: Isrc %.4f A, Ia rms %.4f A, Ia peak %.4f A\n", isum / n, sqrt(i2 / n), r.i_ph_peak);
    metric("s0_isrc_a", isum / n);
    metric("s0_ia_rms", sqrt(i2 / n));
    metric("s0_ia_pk", r.i_ph_peak);
}

static void s1_s2_s4(const char *trace_dir)
{
    static rig_t r;
    double cmd = 2000.0;
    rig_init(&r);
    r.p.t_load = 0.05;
    r.p.us_offset = 0xFFFFFFFFu - 300000u;    /* the 1 MHz timer wraps at 0.3 s, mid-ramp */

    /* S1 start from standstill to 2000 rpm, 0.05 N m load */
    printf("# S1 start to 2000 rpm (ramp 4000 rpm/s), 0.05 N m load, 1.0 s; us timer wraps at 0.3 s\n");
    bldc_set_speed(&r.m, (float)cmd);
    stats_reset(&r, cmd);
    run(&r, 0.80);
    double t_settle = r.t_band_out, rpm_pk = r.rpm_max, i_pk = r.i_ph_peak;
    stats_reset(&r, cmd);
    double e_in0 = r.p.e_in, e_m0 = r.p.e_mech, e_l0 = r.p.e_load;
    run(&r, 0.90);
    if (trace_dir) {                          /* one electrical revolution at steady state */
        r.trace = open_trace(trace_dir, "hil_trace.csv");
        r.trace_div = 128;
        run(&r, 0.9075);
        if (r.trace)
            fclose(r.trace);
        r.trace = open_trace(trace_dir, "pwm_trace.csv");   /* two PWM periods, every tick */
        r.trace_div = 1;
        run(&r, 0.9076);
        if (r.trace)
            fclose(r.trace);
        r.trace = NULL;
    }
    run(&r, 1.00);
    {   /* the compare pair the firmware wrote for the PWM leg */
        int hi = bldc_sector_high[(r.m.sector + BLDC_COMM_ADVANCE) % 6];
        int gap = (int)r.io.ccr_h[hi] - (int)r.io.ccr_l[hi];
        check(r.io.ccr_h[hi] <= BLDC_TIM_ARR && gap * TICK_NS >= 500.0, "S1 dead time in the compare pair >= 500 ns",
              "%.1f ns (%d ticks)", gap * TICK_NS, gap);
    }
    double ss_rpm = r.rpm_sum / r.n, ss_isrc = (r.p.e_in - e_in0) / (PL_VBUS * 0.20);
    double eff = (r.p.e_mech - e_m0) / (r.p.e_in - e_in0);
    double eff_load = (r.p.e_load - e_l0) / (r.p.e_in - e_in0);
    double gap_ns = r.p.min_gap_ticks * TICK_NS;
    check(r.m.faults == 0, "S1 no fault", "faults %u", r.m.faults);
    check(r.p.overlap_ticks == 0, "S1 no shoot-through", "%.0f ticks overlap", (double)r.p.overlap_ticks);
    if (r.p.min_gap_ticks == UINT64_MAX)
        check(0, "S1 both-off gap at the switches >= 200 ns", "no gap: the switches always overlap");
    else
        check(gap_ns >= 200.0, "S1 both-off gap at the switches >= 200 ns", "min %.1f ns (limit %d ns)", gap_ns, 200);
    check(t_settle < 0.70, "S1 within +/-1 % of 2000 rpm by 0.7 s", "last outside band %.3f s", t_settle);
    check(rpm_pk < 1.05 * cmd, "S1 overshoot < 5 %", "peak %.1f rpm (%.2f %%)", rpm_pk, 100.0 * (rpm_pk / cmd - 1.0));
    check(fabs(ss_rpm - cmd) < 0.005 * cmd, "S1 steady-state speed error < 0.5 %", "%.2f rpm", ss_rpm);
    check(r.rpm_max - r.rpm_min < 0.02 * cmd, "S1 speed ripple < 2 % peak-peak", "%.2f rpm p-p", r.rpm_max - r.rpm_min);
    check(i_pk <= BLDC_I_LIMIT_A * 1.1, "S1 phase current <= 1.1 x limit", "%.2f A (limit %.1f A)", i_pk, BLDC_I_LIMIT_A * 1.1);
    check(eff > 0.90, "S1 inverter + winding efficiency > 90 %", "%.2f %% (air-gap power / supply)", 100.0 * eff);
    printf("  info: load power / supply power %.2f %% (friction takes the rest at 0.05 N m)\n", 100.0 * eff_load);
    metric("s1_t_settle_s", t_settle);
    metric("s1_rpm_pk", rpm_pk);
    metric("s1_rpm_ss", ss_rpm);
    metric("s1_rpm_pp", r.rpm_max - r.rpm_min);
    metric("s1_isrc_a", ss_isrc);
    metric("s1_eff", eff);
    metric("s1_eff_load", eff_load);
    metric("s1_gap_ns", r.p.min_gap_ticks == UINT64_MAX ? -1.0 : gap_ns);
    metric("s1_iph_pk", i_pk);

    /* S2 load step 0.05 -> 0.20 N m at 1.0 s */
    printf("# S2 load step 0.05 -> 0.20 N m at 1.0 s, 0.5 s\n");
    stats_reset(&r, cmd);
    r.p.t_load = 0.20;
    run(&r, 1.30);
    double e_in1 = r.p.e_in, dip = r.rpm_min, t_rec = r.t_band_out - 1.0;
    stats_reset(&r, cmd);
    run(&r, 1.50);
    double ss2_rpm = r.rpm_sum / r.n, ss2_isrc = (r.p.e_in - e_in1) / (PL_VBUS * 0.20);
    check(r.m.faults == 0, "S2 no fault", "faults %u", r.m.faults);
    check(dip > 0.75 * cmd, "S2 speed dip < 25 %", "min %.1f rpm (%.2f %%)", dip, 100.0 * (1.0 - dip / cmd));
    check(t_rec < 0.25, "S2 back within +/-1 % in < 250 ms", "%.3f s", t_rec);
    check(fabs(ss2_rpm - cmd) < 0.005 * cmd, "S2 steady-state speed error < 0.5 %", "%.2f rpm", ss2_rpm);
    check(r.p.overlap_ticks == 0, "S2 no shoot-through", "%.0f ticks overlap", (double)r.p.overlap_ticks);
    metric("s2_rpm_min", dip);
    metric("s2_t_rec_s", t_rec);
    metric("s2_rpm_ss", ss2_rpm);
    metric("s2_isrc_a", ss2_isrc);
    metric("s2_i_ref", r.m.i_ref);

    /* S4 Hall wire break (HA stuck low) at 1.5 s */
    printf("# S4 Hall A wire breaks (reads 0) at 1.5 s\n");
    r.p.hall_and = 6;
    run(&r, 1.56);
    double t_f = r.t_fault;
    double ipk_end = fabs(r.p.i[0]) + fabs(r.p.i[1]) + fabs(r.p.i[2]);
    check(r.m.faults == BLDC_F_HALL, "S4 Hall fault latched", "faults %u", r.m.faults);
    check(t_f > 0.0 && t_f - 1.5 < 0.010, "S4 fault within 10 ms of the break",
          t_f > 0.0 ? "after %.2f ms" : "no trip (%.0f)", t_f > 0.0 ? (t_f - 1.5) * 1e3 : 0.0);
    check(r.io.moe == 0 && ipk_end < 1e-6, "S4 gates off, phase currents zero", "moe %d, sum |i| %.2g A", r.io.moe, ipk_end);
    metric("s4_t_fault_ms", (t_f - 1.5) * 1e3);
}

static void s3_locked_rotor(void)
{
    static rig_t r;
    rig_init(&r);
    r.p.hold = 1;
    r.p.w = 0.0;
    printf("# S3 locked rotor, 2000 rpm commanded, 0.8 s\n");
    bldc_set_speed(&r.m, 2000.0f);
    run(&r, 0.10);
    stats_reset(&r, 0.0);
    run(&r, 0.25);
    double pk = r.i_ph_peak;
    stats_reset(&r, 0.0);
    run(&r, 0.45);
    double i_mean = r.i_abs_sum / r.i_abs_n;   /* plant phase current, not the firmware's reading */
    if (r.i_ph_peak > pk)
        pk = r.i_ph_peak;
    stats_reset(&r, 0.0);
    run(&r, 0.80);
    if (r.i_ph_peak > pk)
        pk = r.i_ph_peak;
    r.i_ph_peak = pk;
    double t_f = r.t_fault;
    check(fabs(i_mean - BLDC_I_LIMIT_A) < 0.05 * BLDC_I_LIMIT_A, "S3 current held at the limit",
          "%.3f A in the phases (limit %.1f A)", i_mean, BLDC_I_LIMIT_A);
    check(r.i_ph_peak <= 1.1 * BLDC_I_LIMIT_A, "S3 phase current <= 1.1 x limit", "%.2f A", r.i_ph_peak);
    check(r.m.faults == BLDC_F_STALL, "S3 stall fault (not over-current)", "faults %u", r.m.faults);
    check(t_f > 0.45 && t_f < 0.56, "S3 stall trip after 0.5 s of torque",
          t_f > 0.0 ? "at %.3f s" : "no trip (%.0f)", t_f > 0.0 ? t_f : 0.0);
    check(r.io.moe == 0 && fabs(r.p.i[0]) + fabs(r.p.i[1]) + fabs(r.p.i[2]) < 1e-6,
          "S3 gates off, phase currents zero", "moe %d", r.io.moe);
    metric("s3_i_mean_a", i_mean);
    metric("s3_t_stall_s", t_f);
}

static void s5_stuck_low_side(void)
{
    static rig_t r;
    rig_init(&r);
    r.p.t_load = 0.05;
    r.p.ls_stuck_on[1] = 1;
    printf("# S5 low-side MOSFET of phase B failed short, start to 2000 rpm, 0.3 s\n");
    bldc_set_speed(&r.m, 2000.0f);
    run(&r, 0.30);
    double t_f = r.t_fault, t_st = r.p.overlap_ticks ? r.p.t_overlap0 : -1.0;
    double ov_us = r.p.overlap_ticks * TICK_NS * 1e-3;
    check(r.m.faults == BLDC_F_OC, "S5 over-current fault latched", "faults %u", r.m.faults);
    check(t_st > 0.0 && t_f > 0.0 && t_f - t_st < 2.0 / BLDC_PWM_HZ, "S5 trip within 2 PWM periods",
          t_f > 0.0 ? "%.1f us after the first shoot-through" : "no trip (%.0f)",
          t_f > 0.0 ? (t_f - t_st) * 1e6 : 0.0);
    check(ov_us < 50.0, "S5 total shoot-through time < 50 us", "%.2f us, peak %.0f A", ov_us, r.p.i_st_peak);
    check(r.io.moe == 0, "S5 gates off", "moe %d", r.io.moe);
    metric("s5_t_trip_us", (t_f - t_st) * 1e6);
    metric("s5_overlap_us", ov_us);
}

static void s6_stop(void)
{
    static rig_t r;
    rig_init(&r);
    printf("# S6 no load: start to 2000 rpm, stop command (0 rpm) at 0.8 s, 3.0 s\n");
    bldc_set_speed(&r.m, 2000.0f);
    run(&r, 0.80);
    double rpm0 = plant_rpm(&r.p);
    bldc_set_speed(&r.m, 0.0f);
    double t_idle = -1.0, t_stop = -1.0;
    while (r.p.t < 3.0) {
        run(&r, r.p.t + 1.0 / BLDC_PWM_HZ);
        if (t_idle < 0.0 && r.m.state == BLDC_IDLE)
            t_idle = r.p.t;
        if (t_stop < 0.0 && r.p.w == 0.0)
            t_stop = r.p.t;
    }
    check(rpm0 > 1980.0 && r.m.state == BLDC_IDLE && r.m.faults == 0, "S6 stop: idle, no fault",
          "from %.0f rpm, state %d, faults %u", rpm0, r.m.state, r.m.faults);
    check(t_idle > 0.0 && t_idle - 0.8 < 0.55, "S6 outputs released after the 0.5 s ramp",
          t_idle > 0.0 ? "after %.3f s" : "never (%.0f)", t_idle > 0.0 ? t_idle - 0.8 : 0.0);
    check(t_stop > 0.0 && r.p.w == 0.0 && r.io.moe == 0, "S6 rotor stopped, gates off",
          t_stop > 0.0 ? "at %.3f s, moe %d" : "still turning (%.0f) moe %d", t_stop > 0.0 ? t_stop : 0.0, r.io.moe);
    metric("s6_t_idle_s", t_idle - 0.8);
    metric("s6_t_stop_s", t_stop);
}

int main(int argc, char **argv)
{
    const char *trace_dir = (argc == 3 && strcmp(argv[1], "--trace") == 0) ? argv[2] : NULL;
    printf("# BLDC host test: timer tick %.2f ns, PWM %ld Hz, dead time %d ticks (%.0f ns)\n",
           TICK_NS, (long)BLDC_PWM_HZ, BLDC_DT_TICKS, BLDC_DT_TICKS * TICK_NS);
    unit_checks();
    s0_fixed_point();
    s1_s2_s4(trace_dir);
    s3_locked_rotor();
    s5_stuck_low_side();
    s6_stop();
    printf("RESULT: %d/%d PASS%s\n", n_pass, n_pass + n_fail, n_fail ? "" : " (ALL PASS)");
    return n_fail ? 1 : 0;
}

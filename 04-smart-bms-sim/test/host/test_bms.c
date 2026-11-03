/* test_bms.c - model-in-the-loop host test of src/bms against the pack model (plant.c).
 * The BMS runs as it would on the MCU: every 100 ms one AFE scan of the plant, one call to
 * bms_step(), and the FET and bleed outputs applied to the plant for the next 100 ms.
 * Prints one PASS/FAIL line per check, METRIC lines that main.py compares against its own
 * Python run, and exits 1 on any failure. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include "bms.h"
#include "bms_config.h"
#include "plant.h"

static int fails, checks;
static void check(int ok, const char *name, const char *fmt, double a, double b)
{
    char buf[160];
    snprintf(buf, sizeof buf, fmt, a, b);
    printf("%s %-40s %s\n", ok ? "PASS" : "FAIL", name, buf);
    checks++;
    if (!ok) fails++;
}

typedef struct {
    plant_t p;
    bms_t b;
    bms_meas_t m;
    bms_out_t o;
    long k;                      /* 100 ms steps since start */
} sim_t;

static void sim_init(sim_t *s, double s0, double s1, double s2, double s3, double t_amb)
{
    const double soc0[BMS_NCELLS] = { s0, s1, s2, s3 };
    plant_init(&s->p, soc0, t_amb);
    plant_measure(&s->p, &s->m);
    bms_init(&s->b, &s->m);
    s->o = (bms_out_t){0};
    s->k = 0;
}

static void sim_step(sim_t *s)
{
    plant_measure(&s->p, &s->m);
    bms_step(&s->b, &s->m, &s->o);
    plant_step(&s->p, s->o.chg_on, s->o.dsg_on, s->o.bal_mask);
    s->k++;
}

static int vmax(const sim_t *s)
{
    int v = s->m.cell_mv[0];
    for (int i = 1; i < BMS_NCELLS; i++) if (s->m.cell_mv[i] > v) v = s->m.cell_mv[i];
    return v;
}
static int vmin(const sim_t *s)
{
    int v = s->m.cell_mv[0];
    for (int i = 1; i < BMS_NCELLS; i++) if (s->m.cell_mv[i] < v) v = s->m.cell_mv[i];
    return v;
}
static double tmax(const sim_t *s)
{
    double t = s->p.c[0].t_c;
    for (int i = 1; i < BMS_NCELLS; i++) if (s->p.c[i].t_c > t) t = s->p.c[i].t_c;
    return t;
}
static double ocv_spread_mv(const sim_t *s)
{
    double lo = 1e9, hi = -1e9;
    for (int i = 0; i < BMS_NCELLS; i++) {
        double v = plant_ocv(s->p.c[i].soc) * 1000.0;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
    }
    return hi - lo;
}

#define STEPS(sec) ((long)((sec) * 10.0 + 0.5))

/* S1: CC/CV charge of an unbalanced pack from about 30 %, then 2 h on the charger (done) */
static void s1_charge_balance(void)
{
    printf("# S1 charge 3 A / 16.8 V from 29-33 %% SoC, then 2 h rest; 25 degC\n");
    sim_t s;
    sim_init(&s, 0.30, 0.33, 0.29, 0.31, 25.0);
    double spread0 = ocv_spread_mv(&s);
    s.p.src = SRC_CHARGER;
    int faults = 0, vpk = 0, soc_at_done = -1, bal_steps = 0;
    double socpk = 0.0;
    long k_done = -1;
    while (s.k < STEPS(5 * 3600)) {
        sim_step(&s);
        faults |= s.o.faults;
        if (vmax(&s) > vpk) vpk = vmax(&s);
        if (s.o.bal_mask) bal_steps++;
        for (int i = 0; i < BMS_NCELLS; i++) if (s.p.c[i].soc > socpk) socpk = s.p.c[i].soc;
        if (k_done < 0 && s.p.chg_done) { k_done = s.k; soc_at_done = s.o.soc_pm; }
        if (k_done >= 0 && s.k >= k_done + STEPS(7200)) break;
    }
    double t_chg = k_done * 0.1;
    double spread1 = ocv_spread_mv(&s);
    check(faults == 0, "S1 no protection trip", "faults %.0f", faults, 0);
    check(vpk < BMS_OV_TRIP_MV - 10, "S1 max cell < OV trip - 10 mV", "%.0f mV (limit %.0f mV)",
          vpk, BMS_OV_TRIP_MV - 10);
    check(k_done > 0 && t_chg < 3 * 3600, "S1 charger terminates < 3 h", "%.0f s", t_chg, 0);
    check(socpk <= 1.0, "S1 no cell above 100 % SoC", "peak %.2f %%", socpk * 100.0, 0);
    check(soc_at_done == 1000, "S1 SoC set to 100 % at end of charge", "%.1f %% at termination",
          soc_at_done / 10.0, 0);
    check(spread1 <= 10.0, "S1 balanced: OCV spread <= 10 mV", "%.1f mV -> %.1f mV", spread0, spread1);
    check(bal_steps > 0, "S1 bleed resistors used", "%.0f s of balancing", bal_steps * 0.1, 0);
    printf("METRIC s1_t_chg_s %.1f\nMETRIC s1_vpk_mv %d\nMETRIC s1_spread_mv %.2f\nMETRIC s1_bal_s %.1f\nMETRIC s1_socpk %.5f\n",
           t_chg, vpk, spread1, bal_steps * 0.1, socpk);
}

/* S2: 3 A discharge to the under-voltage trip */
static void s2_discharge_uv(void)
{
    printf("# S2 discharge 3 A from 15 %% SoC to the UV trip, then 60 s rest\n");
    sim_t s;
    sim_init(&s, 0.15, 0.15, 0.15, 0.15, 25.0);
    s.p.src = SRC_LOAD_I;
    s.p.load_a = 3.0;
    int trip_cell = -1, vlow = 9999, toggles = 0, last = 0;
    long k_trip = -1;
    while (s.k < STEPS(3600)) {
        sim_step(&s);
        if (s.k > BMS_STARTUP_SAMPLES && vmin(&s) < vlow) vlow = vmin(&s);
        if (s.k > BMS_STARTUP_SAMPLES + 1 && s.o.dsg_on != last) toggles++;
        last = s.o.dsg_on;
        if (k_trip < 0 && (s.o.faults & BMS_F_UV)) {
            k_trip = s.k;
            for (int i = 0; i < BMS_NCELLS; i++) if (s.m.cell_mv[i] == vmin(&s)) trip_cell = i;
        }
        if (k_trip >= 0 && s.k >= k_trip + STEPS(60)) break;
    }
    check(k_trip > 0 && !s.o.dsg_on, "S2 UV trip opens the discharge FET", "after %.0f s, dsg %.0f",
          k_trip * 0.1, s.o.dsg_on);
    check(trip_cell == 1, "S2 smallest cell (2.94 Ah) trips first", "cell %.0f", trip_cell, 0);
    check(vlow >= BMS_UV_TRIP_MV - 50, "S2 lowest cell >= UV trip - 50 mV", "%.0f mV (limit %.0f mV)",
          vlow, BMS_UV_TRIP_MV - 50);
    check(s.o.soc_pm == 0, "S2 SoC reset to 0 % at UV", "%.1f %%", s.o.soc_pm / 10.0, 0);
    check(toggles == 1, "S2 no FET chatter after the trip", "%.0f FET transitions (limit 1)", toggles, 0);
    printf("METRIC s2_t_trip_s %.1f\n", k_trip * 0.1);
}

/* S3: charger loses CV regulation and pushes 3 A into a 90 % pack for 2 h. Every time
 * the OV flag clears (highest cell < 4.15 V) the charge FET closes again, so the pack
 * hiccups until the rested voltage itself holds the flag. */
static void s3_overcharge(void)
{
    printf("# S3 faulty charger: 3 A constant current, no CV, from 90 %% SoC, 2 h\n");
    sim_t s;
    sim_init(&s, 0.90, 0.90, 0.90, 0.90, 25.0);
    s.p.src = SRC_CHARGER;
    s.p.chg_faulty = 1;
    int vpk = 0, trips = 0, was = 0;
    long k_first = -1, k_off = -1;
    double tpk = 0.0, socpk = 0.0;
    while (s.k < STEPS(2 * 3600)) {
        sim_step(&s);
        if (vmax(&s) > vpk) vpk = vmax(&s);
        if (tmax(&s) > tpk) tpk = tmax(&s);
        for (int i = 0; i < BMS_NCELLS; i++) if (s.p.c[i].soc > socpk) socpk = s.p.c[i].soc;
        if (k_first < 0 && vmax(&s) >= BMS_OV_TRIP_MV) k_first = s.k;
        if (k_first >= 0 && k_off < 0 && !s.o.chg_on) k_off = s.k;
        int ov = (s.o.faults & BMS_F_OV) != 0;
        if (ov && !was) trips++;
        was = ov;
    }
    check(k_off > 0 && trips > 0, "S3 OV trip opens the charge FET", "first after %.1f s, %.0f trips",
          k_off * 0.1, trips);
    check(k_off > 0 && (k_off - k_first) * 0.1 <= 0.3 + 1e-9, "S3 trip within 300 ms of 4.25 V",
          "%.1f s (limit 0.3 s)", (k_off - k_first) * 0.1, 0);
    check(vpk <= 4300, "S3 peak cell <= 4.30 V", "%.0f mV", vpk, 0);
    check(socpk <= 1.0, "S3 no cell above 100 % SoC", "peak %.2f %%", socpk * 100.0, 0);
    check(tpk < 45.0, "S3 peak temperature < 45 degC", "%.2f degC", tpk, 0);
    check((s.o.faults & BMS_F_OV) && fabs(s.p.i_pack) < 1e-9 && s.o.dsg_on,
          "S3 after 2 h: OV held, no current, dsg on", "%.3f A, dsg %.0f", s.p.i_pack, s.o.dsg_on);
    /* Discharging releases OV once the highest cell is below 4.15 V */
    s.p.src = SRC_LOAD_I;
    s.p.load_a = 1.0;
    long k0 = s.k;
    while (s.k < k0 + STEPS(1800) && (s.o.faults & BMS_F_OV)) sim_step(&s);
    check(!(s.o.faults & BMS_F_OV), "S3 OV clears under 1 A discharge", "after %.1f s", (s.k - k0) * 0.1, 0);
    printf("METRIC s3_t_off_s %.1f\nMETRIC s3_trips %d\nMETRIC s3_vpk_mv %d\nMETRIC s3_socpk %.5f\n",
           k_off * 0.1, trips, vpk, socpk);
}

/* S4: 50 mOhm short across the pack at 80 % SoC; then the short is removed and a charger
 * is connected */
static void s4_short(void)
{
    printf("# S4 50 mOhm short at t = 10 s, removed at 15 s, charger connected at 30 s\n");
    sim_t s;
    sim_init(&s, 0.80, 0.80, 0.80, 0.80, 25.0);
    while (s.k < STEPS(10)) sim_step(&s);
    s.p.src = SRC_LOAD_R;
    s.p.load_ohm = 0.05;
    long k0 = s.k, k_off = -1;
    double ipk = 0.0, t0 = tmax(&s), tpk = 0.0;
    while (s.k < STEPS(15)) {
        sim_step(&s);
        if (-s.p.i_pack > ipk) ipk = -s.p.i_pack;
        if (tmax(&s) > tpk) tpk = tmax(&s);
        if (k_off < 0 && !s.o.dsg_on) k_off = s.k;
    }
    check(k_off > 0 && (s.o.faults & BMS_F_SCD), "S4 SCD trip opens the discharge FET",
          "%.0f A peak", ipk, 0);
    check(k_off > 0 && (k_off - k0) <= 2, "S4 FET off within 200 ms", "%.1f s (limit 0.2 s)",
          (k_off - k0) * 0.1, 0);
    check(tpk - t0 < 2.0, "S4 cell temperature rise < 2 K", "%.2f K", tpk - t0, 0);
    s.p.src = SRC_NONE;
    while (s.k < STEPS(30)) sim_step(&s);
    check(!s.o.dsg_on && (s.o.faults & BMS_F_SCD), "S4 SCD stays latched without a charger",
          "dsg %.0f at 30 s", s.o.dsg_on, 0);
    s.p.src = SRC_CHARGER;
    long k1 = s.k;
    while (s.k < STEPS(40) && !s.o.dsg_on) sim_step(&s);
    check(s.o.dsg_on && !s.o.faults, "S4 charger clears the latch", "after %.1f s",
          (s.k - k1) * 0.1, 0);
    printf("METRIC s4_ipk_a %.2f\n", ipk);
}

/* S5: 12 A overload at 80 % SoC */
static void s5_overload(void)
{
    printf("# S5 12 A overload at t = 10 s\n");
    sim_t s;
    sim_init(&s, 0.80, 0.80, 0.80, 0.80, 25.0);
    while (s.k < STEPS(10)) sim_step(&s);
    s.p.src = SRC_LOAD_I;
    s.p.load_a = 12.0;
    long k0 = s.k, k_off = -1;
    while (s.k < STEPS(20) && k_off < 0) {
        sim_step(&s);
        if (!s.o.dsg_on) k_off = s.k;
    }
    double t = (k_off - k0) * 0.1;
    check(k_off > 0 && (s.o.faults & BMS_F_OCD) && !(s.o.faults & BMS_F_SCD), "S5 OCD (not SCD) trip",
          "faults %.0f", s.o.faults, 0);
    check(t >= 0.5 - 1e-9 && t <= 0.6 + 1e-9, "S5 OCD after 0.5-0.6 s", "%.1f s", t, 0);
}

/* S6: cooling failure (60 K/W) at 40 degC ambient, 6 A discharge from 100 % */
static void s6_overtemp(void)
{
    printf("# S6 cooling failure: Rth 60 K/W, 40 degC ambient, 6 A discharge from 100 %%\n");
    sim_t s;
    sim_init(&s, 1.0, 1.0, 1.0, 1.0, 40.0);
    s.p.rth = PLANT_RTH_FAIL_K_W;
    s.p.src = SRC_LOAD_I;
    s.p.load_a = 6.0;
    long k_off = -1;
    double tpk = 0.0;
    int reenabled = 0;
    while (s.k < STEPS(3600)) {
        sim_step(&s);
        if (tmax(&s) > tpk) tpk = tmax(&s);
        if (k_off < 0 && (s.o.faults & BMS_F_OTD)) k_off = s.k;
        if (k_off > 0 && s.o.dsg_on && !(s.o.faults & BMS_F_UV)) reenabled = 1;
        if (s.o.faults & BMS_F_UV) break;
    }
    check(k_off > 0, "S6 OTD trip opens the discharge FET", "after %.0f s", k_off * 0.1, 0);
    check(tpk <= 62.0, "S6 peak temperature <= 62 degC", "%.2f degC", tpk, 0);
    check(reenabled, "S6 re-enabled after cooling below 55 degC", "%.0f", reenabled, 0);
    printf("METRIC s6_t_otd_s %.1f\nMETRIC s6_tpk_c %.3f\n", k_off * 0.1, tpk);
}

/* S7: charger connected at -5 degC; ambient rises to 10 degC at t = 60 s */
static void s7_cold_charge(void)
{
    printf("# S7 charger at -5 degC ambient, ambient to 10 degC at 60 s\n");
    sim_t s;
    sim_init(&s, 0.50, 0.50, 0.50, 0.50, -5.0);
    s.p.src = SRC_CHARGER;
    double imax_cold = 0.0;
    while (s.k < STEPS(60)) {
        sim_step(&s);
        if (s.p.i_pack > imax_cold) imax_cold = s.p.i_pack;
    }
    check(imax_cold == 0.0 && (s.o.faults & BMS_F_UTC), "S7 no charge current below 0 degC",
          "%.3f A peak", imax_cold, 0);
    s.p.t_amb_c = 10.0;
    long k0 = s.k;
    while (s.k < STEPS(3600) && s.p.i_pack <= 0.0) sim_step(&s);
    double tmin = s.p.c[0].t_c;
    for (int i = 1; i < BMS_NCELLS; i++) if (s.p.c[i].t_c < tmin) tmin = s.p.c[i].t_c;
    check(s.p.i_pack > 0.0 && tmin >= 2.95, "S7 charging starts once cells >= 3 degC",
          "after %.0f s at %.2f degC", (s.k - k0) * 0.1, tmin);
}

/* S8: thermistor wire breaks (reads -60 degC) at t = 60 s, reconnects at 120 s */
static void s8_tsense(void)
{
    printf("# S8 open thermistor 60-120 s, 1 A discharge\n");
    sim_t s;
    sim_init(&s, 0.60, 0.60, 0.60, 0.60, 25.0);
    s.p.src = SRC_LOAD_I;
    s.p.load_a = 1.0;
    while (s.k < STEPS(60)) sim_step(&s);
    s.p.tsense_override_c = -60.0;
    while (s.k < STEPS(62)) sim_step(&s);
    check(!s.o.chg_on && !s.o.dsg_on && (s.o.faults & BMS_F_TSENSE), "S8 sensor fault opens both FETs",
          "faults %.0f", s.o.faults, 0);
    s.p.tsense_override_c = -1000.0;
    while (s.k < STEPS(180)) sim_step(&s);
    check(!s.o.dsg_on && (s.o.faults & BMS_F_TSENSE), "S8 sensor fault stays latched",
          "dsg %.0f", s.o.dsg_on, 0);
}

/* Unit: OCV to SoC inverse of the table */
static void unit_soc_from_ocv(void)
{
    int ok = bms_soc_from_ocv_pm(2900) == 0 && bms_soc_from_ocv_pm(3740) == 500 &&
             bms_soc_from_ocv_pm(3710) == 450 && bms_soc_from_ocv_pm(4190) == 1000 &&
             bms_soc_from_ocv_pm(4300) == 1000;
    check(ok, "unit: OCV -> SoC table lookup", "3710 mV -> %.1f %%", bms_soc_from_ocv_pm(3710) / 10.0, 0);
}

int main(void)
{
    unit_soc_from_ocv();
    s1_charge_balance();
    s2_discharge_uv();
    s3_overcharge();
    s4_short();
    s5_overload();
    s6_overtemp();
    s7_cold_charge();
    s8_tsense();
    printf("RESULT: %d/%d PASS%s\n", checks - fails, checks, fails ? "" : " (ALL PASS)");
    return fails ? 1 : 0;
}

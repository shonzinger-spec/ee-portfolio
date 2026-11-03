// ============================================================================
//  buck_plant.chip.c - Wokwi custom chip: the 02 synchronous buck power stage
//
//  The plant is the host test's averaged RK4 model (test/host/plant.c, included
//  below), so the on-target run and `make -C test/host test` share one model.
//  This file only adds what the host test does not need:
//
//    gate timing   PWM_H / PWM_L edges are time-stamped with get_sim_nanos().
//                  Every window of stepNs (default 6400 ns = one LEDC period at
//                  156.25 kHz) the plant is stepped once (by dt / timeScale) with the average
//                  switch-node voltage of that window:
//                    H on (or H and L on)          -> Vin
//                    L on                          -> 0 V (synchronous rectifier)
//                    both off, gap <= GAP_IDEAL_NS -> 0 V (a normal dead time is
//                                                     treated as ideal, as in plant.c)
//                    both off, longer gap          -> body diodes: -VD while IL > 0,
//                                                     Vin + VD while IL < 0, VOUT
//                                                     (IL clamped at 0) once IL is 0
//                  A window of exactly one switching period averages out the
//                  ripple whatever its phase, which is what an averaged model
//                  assumes. 6.4 us is far below the plant's fastest dynamics (LC
//                  resonance period about 535 us); the 4.05 us anti-alias pole is
//                  integrated exactly by plant.c (exp of the step).
//    dead time     every H-fall -> L-rise and L-fall -> H-rise gap is measured
//                  (min / max / count); every interval with H and L high together
//                  is counted as shoot-through (count and total ns).
//                  Also kept: a dead-time histogram (10 ns bins), the count below
//                  DT_OK_NS, and the min/max period of each gate (rising edge to
//                  rising edge), which shows the edge timing resolution of the simulator.
//    FLT output    goes HIGH and stays HIGH on the first shoot-through (H and L high
//                  together), so a scenario can expect-pin it. A short dead time does
//                  not set FLT: Wokwi's LEDC edges jitter by tens of ns (the gate period
//                  itself jitters, see PGATE per_*), so dead times are judged from the
//                  printed statistics instead (README_WOKWI.md).
//    sensing       VSENSE   = plant_t.vaa (10k/6.8k divider + 4.05 us anti-alias)
//                  ISENSE   = 0.5 V/A x load current (INA181A3 on 5 mOhm)
//                  VINSENSE = VIN x 10/57
//                  all clamped to 0..3.3 V and written with pin_dac_write().
//    controls      loadMa: load current at 5 V (R = 5 V / I); vinMv: input voltage.
//    time scale    attribute timeScale (k, default 1): the plant integrates dt / k for every
//                  dt of simulated time. The Wokwi firmware build runs its 20 us loop k times
//                  slower (src/buck_esp32/fw_config.h, FW_SIM_TIME_SCALE), because one update
//                  costs about 91 us of simulated time; with the same k on both sides the plant
//                  still sees one update per 20 us of its own time. Gate timing (dead time,
//                  overlap) is measured in simulated ns and is not scaled. PLANT/PSEG times
//                  are plant time (us); PGATE t_sim_us is simulated time.
//
//  printf lines (PLANT / PSEG / PGATE) go to the simulator console; README_WOKWI.md
//  says where they show up.
//
//  Not modelled: switching ripple (averaged), MOSFET/driver delays, bootstrap
//  supply, ADC sampling capacitor, the 8 us VINSENSE filter (as in plant.c).
// ============================================================================
#include "wokwi-api.h"
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>

#include "plant.c"          // test/host/plant.c (+ plant.h, src/buck_ctrl/buck_config.h)

#define VD_V            0.7     // MOSFET body diode forward drop
#define GAP_IDEAL_NS    100u    // both-off gap up to this long = normal dead time (2 x 50 ns)
#define DT_OK_NS        40u     // dead times below this are counted (dt_lt40)
#define DT_MEAS_MAX_NS  1000u   // longer both-off gaps are idle time, not dead times
#define ADC_FS_V        3.3
#define PRINT_FAST_NS   100000ull     // 100 us PLANT rows ...
#define PRINT_FAST_FOR  8000000ull    // ... for 8 ms after a control change,
#define PRINT_SLOW_NS   2000000ull    // otherwise every 2 ms
#define BAND_V          (0.01 * BUCK_VOUT_REF_V)

typedef struct {
  pin_t h, l, vs, is, vins, flt;
  uint32_t a_load, a_vin, a_step, a_scale;
  double k;                   // time scale: plant time = simulated time / k
  double tp;                  // plant time, ns
  plant_t p;
  // gate state
  bool hs, ls;
  uint64_t t_acc;             // time accounted up to here
  int gap_from;               // pin whose fall opened the present both-off gap: 0 none, 1 H, 2 L
  uint64_t gap_start, gap_pending;
  bool gap_long;
  // window accumulators, ns
  uint64_t acc_h, acc_ovl, acc_d, t_win;
  // gate statistics
  uint64_t dt_min, dt_max; uint32_t dt_n;
  uint32_t ovl_n; uint64_t ovl_ns;
  uint32_t dt_hist[11], dt_lt_ok;     // 10 ns bins, last = 100 ns and up
  uint64_t dt_imin, dt_imax;          // since the last PGATE row
  uint64_t rise_h, rise_l;            // last rising edges
  uint64_t per_min[2], per_max[2];    // gate periods H, L (overall)
  uint64_t diode_ns;
  bool flt_on;
  // load segment: from one control change to the next
  uint32_t load_ma, vin_mv;
  uint64_t seg_t0, seg_last_out;
  bool seg_out;
  double seg_vmin, seg_vmax;
  // printing
  uint64_t next_print, fast_until;
  double row_vmin, row_vmax, row_dsum; uint32_t row_n;
} chip_t;

static double clamp_fs(double v) { return v < 0 ? 0 : (v > ADC_FS_V ? ADC_FS_V : v); }
static double rload_of(uint32_t ma) { return ma == 0 ? 1e6 : 5.0 / (ma * 1e-3); }

static void set_flt(chip_t *c)
{
  if (!c->flt_on) { c->flt_on = true; pin_write(c->flt, HIGH); }
}

static void account(chip_t *c, uint64_t now)
{
  const uint64_t dt = now - c->t_acc;
  c->t_acc = now;
  if (c->hs && c->ls) { c->acc_ovl += dt; c->ovl_ns += dt; }
  else if (c->hs) c->acc_h += dt;
  else if (!c->ls) {
    if (c->gap_long) c->acc_d += dt;
    else c->gap_pending += dt;
  }
}

static void gate_change(void *user, pin_t pin, uint32_t value)
{
  chip_t *c = (chip_t *)user;
  const uint64_t now = get_sim_nanos();
  account(c, now);
  const bool was_off = !c->hs && !c->ls, was_ovl = c->hs && c->ls;
  const int which = (pin == c->h) ? 1 : 2;
  if (which == 1) c->hs = value != 0; else c->ls = value != 0;
  const bool off = !c->hs && !c->ls, ovl = c->hs && c->ls;

  if (ovl && !was_ovl) { c->ovl_n++; set_flt(c); }
  if (was_off && !off) {                      // gap closed by a rising edge of `which`
    const uint64_t g = now - c->gap_start;
    if (c->gap_from != 0 && c->gap_from != which && g <= DT_MEAS_MAX_NS) {
      if (c->dt_n == 0 || g < c->dt_min) c->dt_min = g;
      if (g > c->dt_max) c->dt_max = g;
      c->dt_n++;
      if (g < DT_OK_NS) c->dt_lt_ok++;
      c->dt_hist[g >= 100 ? 10 : g / 10]++;
      if (c->dt_imin == 0 || g < c->dt_imin) c->dt_imin = g;
      if (g > c->dt_imax) c->dt_imax = g;
    }
    if (!c->gap_long && g > GAP_IDEAL_NS) c->acc_d += c->gap_pending;   // long gap: diodes
    c->gap_pending = 0;
    c->gap_long = false;
  }
  if (value != 0) {                           // gate period, rising edge to rising edge
    uint64_t *last = which == 1 ? &c->rise_h : &c->rise_l;
    const int k = which - 1;
    if (*last != 0) {
      const uint64_t per = now - *last;
      if (per < 100000) {                     // ignore restarts after idle time
        if (c->per_min[k] == 0 || per < c->per_min[k]) c->per_min[k] = per;
        if (per > c->per_max[k]) c->per_max[k] = per;
      }
    }
    *last = now;
  }
  if (off && !was_off) {                      // gap opened by a falling edge of `which`
    c->gap_start = now;
    c->gap_from = which;
    c->gap_pending = 0;
    c->gap_long = false;
  }
}

static void print_gate(chip_t *c, uint64_t now, const char *why)
{
  printf("PGATE t_sim_us=%llu tp_us=%.0f why=%s deadtimes=%lu dt_min_ns=%llu dt_max_ns=%llu overlaps=%lu "
         "overlap_ns=%llu diode_us=%.1f flt=%d dt_lt40=%lu int_dt_min_ns=%llu int_dt_max_ns=%llu "
         "per_h_ns=%llu..%llu per_l_ns=%llu..%llu "
         "dt_hist10ns=%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu\n",
         (unsigned long long)(now / 1000), c->tp * 1e-3, why, (unsigned long)c->dt_n,
         (unsigned long long)c->dt_min, (unsigned long long)c->dt_max, (unsigned long)c->ovl_n,
         (unsigned long long)c->ovl_ns, c->diode_ns * 1e-3, c->flt_on ? 1 : 0,
         (unsigned long)c->dt_lt_ok, (unsigned long long)c->dt_imin, (unsigned long long)c->dt_imax,
         (unsigned long long)c->per_min[0], (unsigned long long)c->per_max[0],
         (unsigned long long)c->per_min[1], (unsigned long long)c->per_max[1],
         (unsigned long)c->dt_hist[0], (unsigned long)c->dt_hist[1], (unsigned long)c->dt_hist[2],
         (unsigned long)c->dt_hist[3], (unsigned long)c->dt_hist[4], (unsigned long)c->dt_hist[5],
         (unsigned long)c->dt_hist[6], (unsigned long)c->dt_hist[7], (unsigned long)c->dt_hist[8],
         (unsigned long)c->dt_hist[9], (unsigned long)c->dt_hist[10]);
  c->dt_imin = c->dt_imax = 0;
}

static void seg_start(chip_t *c, uint64_t tp)
{
  const double v = plant_vload(&c->p);
  c->seg_t0 = tp;
  c->seg_vmin = c->seg_vmax = v;
  c->seg_out = false;
  c->seg_last_out = tp;
}

// One line per control segment: the plant's own view of a load or line step.
// settle_us = time from the change until VLOAD was last outside +/-1 % of 5 V
// (0 = never left the band; meaningful only if the segment lasted long enough).
static void seg_finish(chip_t *c, uint64_t tp, uint32_t new_ma, uint32_t new_mv)
{
  printf("PSEG t0_us=%llu t1_us=%llu load_mA=%lu vin_mV=%lu next_load_mA=%lu next_vin_mV=%lu "
         "vmin_V=%.4f vmax_V=%.4f settle_us=%lld v_end_V=%.4f il_end_A=%.4f\n",
         (unsigned long long)(c->seg_t0 / 1000), (unsigned long long)(tp / 1000),
         (unsigned long)c->load_ma, (unsigned long)c->vin_mv, (unsigned long)new_ma,
         (unsigned long)new_mv, c->seg_vmin, c->seg_vmax,
         c->seg_out ? (long long)((c->seg_last_out - c->seg_t0) / 1000) : 0ll,
         plant_vload(&c->p), c->p.il);
}

static void window(void *user)
{
  chip_t *c = (chip_t *)user;
  const uint64_t now = get_sim_nanos();
  account(c, now);
  if (!c->gap_long && !c->hs && !c->ls && now - c->gap_start > GAP_IDEAL_NS) {
    c->gap_long = true;                      // still open and already too long: diodes
    c->acc_d += c->gap_pending;
    c->gap_pending = 0;
  }
  const uint64_t dtw = now - c->t_win;
  c->t_win = now;
  if (dtw == 0) return;
  const double dtp = (double)dtw / c->k;     // plant-time step, ns
  const uint64_t tpn = (uint64_t)c->tp;     // plant time at the start of this window

  // controls (a change closes the present segment)
  const uint32_t ma = attr_read(c->a_load), mv = attr_read(c->a_vin);
  if (ma != c->load_ma || mv != c->vin_mv) {
    seg_finish(c, tpn, ma, mv);
    print_gate(c, now, "control");
    c->load_ma = ma;
    c->vin_mv = mv;
    c->p.rload = rload_of(ma);
    c->p.vin = mv * 1e-3;
    seg_start(c, tpn);
    c->fast_until = tpn + PRINT_FAST_FOR;
  }

  // average switch-node voltage over the window
  const double vin = c->p.vin, il0 = c->p.il;
  double vd;
  if (il0 > 0) vd = -VD_V;
  else if (il0 < 0) vd = vin + VD_V;
  else vd = plant_vout(&c->p);
  const double vsw = ((double)(c->acc_h + c->acc_ovl) * vin + (double)c->acc_d * vd) / (double)dtw;
  const double duty = vin > 1e-3 ? vsw / vin : 0.0;
  const bool diode_window = 2 * c->acc_d > dtw;
  c->p.vin = vin > 1e-3 ? vin : 1e-3;
  plant_step(&c->p, vin > 1e-3 ? duty : vsw / 1e-3, dtp * 1e-9);
  c->tp += dtp;
  const uint64_t tpe = (uint64_t)c->tp;
  c->p.vin = vin;
  if (diode_window && ((il0 >= 0 && c->p.il < 0) || (il0 <= 0 && c->p.il > 0)))
    c->p.il = 0;                             // a diode cannot reverse the current
  c->diode_ns += c->acc_d;
  c->acc_h = c->acc_ovl = c->acc_d = 0;

  // sense outputs
  pin_dac_write(c->vs, (float)clamp_fs(c->p.vaa));
  const double iload = plant_iload(&c->p);
  pin_dac_write(c->is, (float)clamp_fs(iload > 0 ? iload * BUCK_ISENSE_V_PER_A : 0.0));
  pin_dac_write(c->vins, (float)clamp_fs(vin * BUCK_VINDIV_RATIO));

  // statistics and rows
  const double v = plant_vload(&c->p);
  if (v < c->seg_vmin) c->seg_vmin = v;
  if (v > c->seg_vmax) c->seg_vmax = v;
  if (fabs(v - BUCK_VOUT_REF_V) > BAND_V) { c->seg_out = true; c->seg_last_out = tpe; }
  if (v < c->row_vmin) c->row_vmin = v;
  if (v > c->row_vmax) c->row_vmax = v;
  c->row_dsum += duty;
  c->row_n++;
  if (tpe >= c->next_print) {
    printf("PLANT tp_us=%llu vload_V=%.4f vmin_V=%.4f vmax_V=%.4f il_A=%.4f iload_A=%.4f "
           "duty=%.4f load_mA=%lu vin_V=%.2f\n",
           (unsigned long long)(tpe / 1000), v, c->row_vmin, c->row_vmax, c->p.il, iload,
           c->row_n ? c->row_dsum / c->row_n : 0.0, (unsigned long)c->load_ma, vin);
    c->row_vmin = 1e9; c->row_vmax = -1e9; c->row_dsum = 0; c->row_n = 0;
    c->next_print = tpe + (tpe < c->fast_until ? PRINT_FAST_NS : PRINT_SLOW_NS);
    if (tpe >= c->fast_until) print_gate(c, now, "periodic");
  }
}

void chip_init(void)
{
  chip_t *c = calloc(1, sizeof(chip_t));
  c->a_load = attr_init("loadMa", 1000);
  c->a_vin = attr_init("vinMv", 12000);
  c->a_step = attr_init("stepNs", 6400);
  c->a_scale = attr_init("timeScale", 1);
  c->k = attr_read(c->a_scale) >= 1 ? (double)attr_read(c->a_scale) : 1.0;
  c->load_ma = attr_read(c->a_load);
  c->vin_mv = attr_read(c->a_vin);
  plant_init(&c->p, c->vin_mv * 1e-3, rload_of(c->load_ma));

  // R10/R11 (10k pull-downs on HI/LI) are modelled by the input pull-downs
  c->h = pin_init("PWM_H", INPUT_PULLDOWN);
  c->l = pin_init("PWM_L", INPUT_PULLDOWN);
  c->vs = pin_init("VSENSE", ANALOG);
  c->is = pin_init("ISENSE", ANALOG);
  c->vins = pin_init("VINSENSE", ANALOG);
  c->flt = pin_init("FLT", OUTPUT_LOW);
  c->hs = pin_read(c->h) != 0;
  c->ls = pin_read(c->l) != 0;
  c->row_vmin = 1e9; c->row_vmax = -1e9;
  const uint64_t now = get_sim_nanos();
  c->t_acc = c->t_win = c->gap_start = now;
  c->tp = now / c->k;
  seg_start(c, (uint64_t)c->tp);
  pin_dac_write(c->vins, (float)clamp_fs(c->p.vin * BUCK_VINDIV_RATIO));

  const pin_watch_config_t w = { .user_data = c, .edge = BOTH, .pin_change = gate_change };
  pin_watch(c->h, &w);
  pin_watch(c->l, &w);
  const timer_config_t t = { .user_data = c, .callback = window };
  const timer_t tm = timer_init(&t);
  uint32_t step = attr_read(c->a_step);
  if (step < 100) step = 100;
  timer_start_ns(tm, step, true);
  printf("PLANT init step_ns=%lu time_scale=%.0f load_mA=%lu vin_mV=%lu gap_ideal_ns=%u dt_ok_ns=%u\n",
         (unsigned long)step, c->k, (unsigned long)c->load_ma, (unsigned long)c->vin_mv,
         GAP_IDEAL_NS, DT_OK_NS);
}

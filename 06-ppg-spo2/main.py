"""Validation script for the PPG SpO2 engine (simulation only; no hardware, no clinical data).

Runs every scenario, prints one PASS/FAIL line per check plus METRIC lines, and saves the figures
to docs/img/. Exit code 0 means every check passed.

    python3 main.py            # full run with figures
    python3 main.py --no-plot  # checks only

Accuracy is measured the way ISO 80601-2-61 (Annex EE) describes a controlled desaturation study:
SpO2 readings paired with the reference arterial saturation on steady plateaus between 70 % and
100 %, calibration fitted on one cohort and A_rms reported on a different cohort. Here the
"subjects" are synthetic, so the numbers check the signal processing against its own model; they
are not a clinical accuracy claim.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from ppg_spo2 import (
    TEXTBOOK_CALIBRATION,
    AmbientLight,
    MotionEpisode,
    PpgSpo2Engine,
    Status,
    SubjectOptics,
    fit_linear_calibration,
    generate_recording,
    plateau_profile,
)
from ppg_spo2.engine import EngineConfig
from ppg_spo2.filters import (
    AC_BAND_HZ, AC_ORDER, DC_CUTOFF_HZ, DC_ORDER, design_ac_bandpass, design_dc_lowpass, magnitude_db,
)
from ppg_spo2.synth import ADC_FULL_SCALE_COUNTS
from ppg_spo2.metrics import accuracy_stats, arms_by_decade, reference_pulse_rate_bpm
from ppg_spo2.optics import (
    IR_WAVELENGTH_NM,
    NOMINAL_DPF_RATIO_RED_IR,
    RED_WAVELENGTH_NM,
    ratio_of_ratios_from_saturation,
    saturation_from_ratio_of_ratios,
)

FS_HZ = 100.0
ISO_ARMS_LIMIT_PCT = 4.0  # ISO 80601-2-61:2017, 201.12.1.101: A_rms <= 4.0 % over 70-100 % SaO2
PULSE_RATE_LIMIT_BPM = 3.0  # design target of this project (ISO asks the maker to declare it)
SHOWN_ERROR_LIMIT_PCT = 3.0  # design target: no single value shown in motion off by more than 3 %
RECOVERY_LIMIT_S = 20.0  # design target: readings back within 20 s of the disturbance ending
WRONG_RUN_LIMIT_PCT = 5.0  # design target: share of motion runs allowed to show any value > 3 % off
# Motion seeds 100-111 and 300-305 were used while choosing the beat-quality thresholds; the
# reported motion results use seeds that played no part in any choice.
MOTION_SEEDS = range(500, 506)
DPF_RATIO_SPREAD = 0.05  # 1-sigma relative spread of DPF_red / DPF_ir between synthetic subjects
COVERAGE_LIMIT_PCT = 95.0  # design target: share of paired outputs that must show a value
STUDY_LEVELS_PCT = (98, 94, 90, 86, 82, 78, 74, 70)
STUDY_HOLD_S, STUDY_TRANSITION_S, STUDY_PAIR_LAST_S = 40.0, 15.0, 25.0
IMG_DIR = Path(__file__).resolve().parent / "docs" / "img"


class Report:
    def __init__(self):
        self.results = []

    def section(self, title):
        print(f"\n[{len(self.section_titles) + 1}] {title}")
        self.section_titles.append(title)

    section_titles: list = []

    def check(self, check_id, description, passed, detail):
        self.results.append((check_id, bool(passed)))
        print(f"  {check_id} {description:<52s} {detail:<44s} {'PASS' if passed else 'FAIL'}")

    def info(self, text):
        print(f"      info: {text}")

    @staticmethod
    def metric(name, value):
        print(f"METRIC {name} {value:.6g}")


def random_subject(rng):
    """One synthetic subject: tissue optics, perfusion and probe gains drawn from plausible ranges."""
    return SubjectOptics(
        # Scattering differs between people, so DPF_red / DPF_ir (and with it R at a given SaO2)
        # varies: a 5 % (1 sigma) spread. This spread is a model assumption; it sets most of A_rms.
        dpf_ratio_red_ir=float(rng.normal(NOMINAL_DPF_RATIO_RED_IR, DPF_RATIO_SPREAD * NOMINAL_DPF_RATIO_RED_IR)),
        perfusion_index_ir=float(np.exp(rng.uniform(np.log(0.005), np.log(0.05)))),
        static_absorbance_red=float(rng.uniform(1.9, 2.5)),
        static_absorbance_ir=float(rng.uniform(1.4, 1.8)),
        led_gain_red_counts=float(rng.uniform(0.8, 1.2) * 8.0e6),
        led_gain_ir_counts=float(rng.uniform(0.8, 1.2) * 4.5e6),
        respiratory_rate_hz=float(rng.uniform(0.2, 0.33)),
        venous_desaturation_frac=float(rng.uniform(0.2, 0.3)),
    )


def run_desaturation_study(subject, rate_bpm, seed, calibration):
    sao2_at, plateaus = plateau_profile(STUDY_LEVELS_PCT, STUDY_HOLD_S, STUDY_TRANSITION_S)
    duration_s = len(STUDY_LEVELS_PCT) * (STUDY_HOLD_S + STUDY_TRANSITION_S)
    recording = generate_recording(duration_s, FS_HZ, sao2_at, rate_bpm, subject=subject, seed=seed)
    result = PpgSpo2Engine(FS_HZ, calibration).process(recording)
    sao2_ref = np.interp(result.output_time_s, recording.time_s, recording.sao2_pct)
    paired = np.zeros(result.output_time_s.shape, dtype=bool)
    for start_s, stop_s, _ in plateaus:
        paired |= (result.output_time_s >= stop_s - STUDY_PAIR_LAST_S) & (result.output_time_s <= stop_s)
    rate_ref = reference_pulse_rate_bpm(recording.beat_onset_s, result.output_time_s)
    return recording, result, sao2_ref, paired, rate_ref


def splice(first, second, from_s):
    """Recording that is `first` before from_s and `second` from then on (probe moved, light lost)."""
    late = first.time_s >= from_s
    for name in ("red_counts", "red_ambient_counts", "ir_counts", "ir_ambient_counts", "sao2_pct", "pulse_rate_bpm"):
        getattr(first, name)[late] = getattr(second, name)[late]
    return first


def first_flag_delay(result, status_bit, event_s):
    """Seconds from event_s to the first output carrying status_bit (inf if it never appears)."""
    after = result.output_time_s > event_s
    flagged = (result.status[after] & status_bit) > 0
    return float(result.output_time_s[after][np.argmax(flagged)] - event_s) if flagged.any() else np.inf


def window_errors(result, recording, start_s, stop_s):
    t = result.output_time_s
    in_window = (t >= start_s) & (t <= stop_s)
    ref = np.interp(t, recording.time_s, recording.sao2_pct)
    err = result.spo2_pct[in_window] - ref[in_window]
    return err, in_window


def naive_spo2(result, calibration, hold_s=10.0, n_beats=8):
    """What the display would show if every detected beat were trusted (for comparison only)."""
    beats = [b for b in result.beats if np.isfinite(b.ratio_r) and b.reason != "first_beat"]
    out = np.full(result.output_time_s.shape, np.nan)
    for i, t in enumerate(result.output_time_s):
        window = [b.ratio_r for b in beats if t - hold_s <= b.peak_time_s <= t][-n_beats:]
        if window:
            out[i] = calibration.raw_spo2_pct(np.median(window))
    return np.clip(out, 0.0, 100.0)


def run_checks(make_plots=True):
    rep = Report()
    Report.section_titles = []
    figures = {}
    cfg = EngineConfig()
    print("Configuration")
    print(f"  optics: red {RED_WAVELENGTH_NM:.0f} nm, IR {IR_WAVELENGTH_NM:.0f} nm, Prahl haemoglobin extinction coefficients, "
          f"nominal DPF_red / DPF_ir {NOMINAL_DPF_RATIO_RED_IR}")
    print(f"  sample rate {FS_HZ:.0f} Hz per channel (red, ambient, IR, ambient per frame); ADC full scale {ADC_FULL_SCALE_COUNTS} counts (22-bit)")
    print(f"  DC low-pass {DC_CUTOFF_HZ} Hz order {DC_ORDER}; AC band-pass {AC_BAND_HZ[0]}-{AC_BAND_HZ[1]} Hz order {AC_ORDER} (Butterworth, SOS)")
    print(f"  beat checks: rate {cfg.min_rate_bpm:.0f}-{cfg.max_rate_bpm:.0f} bpm, perfusion >= {100 * cfg.min_perfusion_index:.2f} %, "
          f"red/IR correlation >= {cfg.min_red_ir_correlation}, amplitude {cfg.amplitude_tolerance[0]}-{cfg.amplitude_tolerance[1]}x, "
          f"interval +/-{100 * cfg.interval_tolerance_frac:.0f} %, R +/-({cfg.ratio_jump_abs} + {100 * cfg.ratio_jump_rel:.0f} %)")
    print(f"  seeding: {cfg.seed_beats} agreeing beats (R within {100 * cfg.seed_ratio_tolerance_frac:.0f} %), {cfg.seed_quiet_s:.0f} s quiet; "
          f"relearn after {cfg.reseed_after_rejections} rejections or {cfg.unseed_after_s:.0f} s without a beat")
    print(f"  output every {cfg.output_period_s:.0f} s: median of last {cfg.average_beats} accepted beats within {cfg.hold_s:.0f} s, "
          f"at least {cfg.min_beats_for_output} beats, R scatter <= {100 * cfg.max_ratio_scatter:.0f} %; "
          f"min DC {100 * cfg.min_dc_fraction_of_full_scale:.1f} % of full scale")
    print(f"  study: plateaus {', '.join(str(v) for v in STUDY_LEVELS_PCT)} % SaO2, hold {STUDY_HOLD_S:.0f} s, transition "
          f"{STUDY_TRANSITION_S:.0f} s, pairs from the last {STUDY_PAIR_LAST_S:.0f} s of each hold; DPF ratio spread {100 * DPF_RATIO_SPREAD:.0f} % (1 sigma)")
    print(f"  motion seeds {MOTION_SEEDS.start}-{MOTION_SEEDS.stop - 1}; the beat-check thresholds were chosen on seeds 100-111 and 300-305")
    print(f"  limits: A_rms <= {ISO_ARMS_LIMIT_PCT} % (ISO 80601-2-61); design targets: coverage >= {COVERAGE_LIMIT_PCT:.0f} %, "
          f"pulse rate A_rms <= {PULSE_RATE_LIMIT_BPM} bpm, any shown value within {SHOWN_ERROR_LIMIT_PCT} %, recovery <= {RECOVERY_LIMIT_S:.0f} s")

    # ---------------------------------------------------------------------------------------
    rep.section("Optics model and generator")
    sat_grid = np.linspace(0.7, 1.0, 301)
    roundtrip = np.max(np.abs(saturation_from_ratio_of_ratios(ratio_of_ratios_from_saturation(sat_grid)) - sat_grid))
    rep.check("C01", "eq. (3) R(S) and its inverse round trip", roundtrip < 1e-9, f"max error {roundtrip:.1e} (limit 1e-9)")
    clean = generate_recording(60, FS_HZ, 97.0, 72.0, seed=11)
    clean_result = PpgSpo2Engine(FS_HZ).process(clean)
    measured_r = np.nanmedian(clean_result.ratio_r[20:])
    true_r = float(ratio_of_ratios_from_saturation(0.97))
    r_err_pct = 100 * abs(measured_r / true_r - 1)
    rep.check("C02", "engine R on a clean 97 % recording vs eq. (3)", r_err_pct < 1.0, f"{measured_r:.4f} vs {true_r:.4f} ({r_err_pct:.2f} %)")
    rep.metric("clean_r_error_pct", r_err_pct)

    # ---------------------------------------------------------------------------------------
    rep.section("Filters")
    dc_sos, ac_sos = design_dc_lowpass(FS_HZ), design_ac_bandpass(FS_HZ)
    dc_1hz = magnitude_db(dc_sos, [1.0], FS_HZ)[0]
    rep.check("C03", "DC low-pass rejects the pulse (1 Hz)", dc_1hz <= -20, f"{dc_1hz:.1f} dB (limit -20 dB)")
    passband = magnitude_db(ac_sos, np.linspace(0.8, 3.5, 50), FS_HZ)
    ripple = float(np.max(np.abs(passband)))
    rep.check("C04", "AC band-pass flat over 0.8-3.5 Hz (48-210 bpm)", ripple <= 0.5, f"max deviation {ripple:.2f} dB (limit 0.5 dB)")
    resp_db, alias_db = magnitude_db(ac_sos, [0.25, 20.0], FS_HZ)
    rep.check("C05", "AC band-pass rejects breathing and flicker alias", resp_db <= -20 and alias_db <= -40,
              f"0.25 Hz {resp_db:.1f} dB, 20 Hz {alias_db:.1f} dB (-20/-40)")

    # ---------------------------------------------------------------------------------------
    rep.section("Calibration and accuracy (ISO 80601-2-61 style desaturation study)")
    rng = np.random.default_rng(2026)
    cal_r, cal_ref = [], []
    for subject_no in range(8):
        subject = random_subject(rng)
        rate = float(rng.uniform(55, 100))
        _, result, ref, paired, _ = run_desaturation_study(subject, rate, 100 + subject_no, TEXTBOOK_CALIBRATION)
        cal_r.extend(result.ratio_r[paired])
        cal_ref.extend(ref[paired])
    cal_r, cal_ref = np.asarray(cal_r), np.asarray(cal_ref)
    calibration = fit_linear_calibration(cal_r, cal_ref)
    fit_resid = cal_ref - calibration.raw_spo2_pct(cal_r)
    keep = np.isfinite(fit_resid)
    r_squared = 1 - np.sum(fit_resid[keep] ** 2) / np.sum((cal_ref[keep] - cal_ref[keep].mean()) ** 2)
    fit_arms = float(np.sqrt(np.mean(fit_resid[keep] ** 2)))
    rep.check("C06", "linear calibration describes its own cohort", fit_arms <= ISO_ARMS_LIMIT_PCT,
              f"SpO2 = {calibration.intercept_pct:.2f} - {calibration.slope_pct_per_r:.2f} R, residual {fit_arms:.2f} %, R^2 {r_squared:.3f}")
    rep.info(f"{int(keep.sum())} calibration pairs, {np.isnan(cal_r).sum()} blanked outputs excluded")
    rep.metric("cal_intercept_pct", calibration.intercept_pct)
    rep.metric("cal_slope_pct_per_r", calibration.slope_pct_per_r)
    rep.metric("cal_r_squared", r_squared)
    rep.metric("cal_fit_residual_arms_pct", fit_arms)

    val_spo2, val_ref, val_textbook, val_rate, val_rate_ref = [], [], [], [], []
    val_blanked, val_total = 0, 0
    example = None
    for subject_no in range(10):
        subject = random_subject(rng)
        rate = float(rng.uniform(55, 100))
        recording, result, ref, paired, rate_ref = run_desaturation_study(subject, rate, 200 + subject_no, calibration)
        val_spo2.extend(result.spo2_pct[paired])
        val_ref.extend(ref[paired])
        val_textbook.extend(TEXTBOOK_CALIBRATION.raw_spo2_pct(result.ratio_r[paired]))
        val_rate.extend(result.pulse_rate_bpm[paired])
        val_rate_ref.extend(rate_ref[paired])
        val_blanked += int(np.isnan(result.spo2_pct[paired]).sum())
        val_total += int(paired.sum())
        if example is None:
            example = (recording, result)
    stats = accuracy_stats(val_spo2, val_ref)
    coverage_pct = 100.0 * (1 - val_blanked / val_total)
    rep.check("C07", "validation cohort (10 other subjects) A_rms 70-100 %",
              stats["arms"] <= ISO_ARMS_LIMIT_PCT and coverage_pct >= COVERAGE_LIMIT_PCT,
              f"{stats['arms']:.2f} % (limit {ISO_ARMS_LIMIT_PCT:.1f}), shown {coverage_pct:.1f} % of {val_total}")
    rep.info(f"bias {stats['bias']:+.2f} %, precision {stats['precision']:.2f} %, blanked {val_blanked} of {val_total} paired outputs")
    rep.metric("val_arms_pct", stats["arms"])
    rep.metric("val_bias_pct", stats["bias"])
    rep.metric("val_precision_pct", stats["precision"])
    rep.metric("val_pairs", stats["n"])
    rep.metric("val_blanked", val_blanked)
    rep.metric("val_coverage_pct", coverage_pct)
    bands = arms_by_decade(val_spo2, val_ref)
    worst_band = max(b[2]["arms"] for b in bands)
    rep.check("C08", "A_rms in each band 70-80, 80-90, 90-100 %", worst_band <= ISO_ARMS_LIMIT_PCT,
              " / ".join(f"{b[2]['arms']:.2f}" for b in bands) + " %")
    for lo, hi, band in bands:
        rep.metric(f"val_arms_{int(lo)}_{int(hi)}_pct", band["arms"])
    textbook = accuracy_stats(val_textbook, val_ref)
    rep.info(f"same readings through the textbook line 110 - 25 R: A_rms {textbook['arms']:.2f} % "
             f"(the fitted line: {stats['arms']:.2f} %)")
    rep.metric("textbook_arms_pct", textbook["arms"])
    rate_stats = accuracy_stats(val_rate, val_rate_ref)
    rep.check("C09", "pulse rate A_rms vs 8-beat reference", rate_stats["arms"] <= PULSE_RATE_LIMIT_BPM,
              f"{rate_stats['arms']:.2f} bpm (target {PULSE_RATE_LIMIT_BPM:.1f} bpm), n {rate_stats['n']}")
    rep.metric("val_pulse_rate_arms_bpm", rate_stats["arms"])
    figures["study"] = (example, np.asarray(val_spo2), np.asarray(val_ref), cal_r, cal_ref, calibration)

    # ---------------------------------------------------------------------------------------
    rep.section("Ambient light and motion")
    # Red LED driven lower than IR (red DC ~5.5e5, IR ~9.0e5 counts): an ambient offset left in
    # the signal would then change the two AC/DC ratios differently and move R.
    unequal_leds = SubjectOptics(led_gain_red_counts=5.0e6)
    ambient_kwargs = dict(duration_s=90, fs_hz=FS_HZ, sao2_pct=95.0, pulse_rate_bpm=75.0, seed=31, subject=unequal_leds)
    dark = generate_recording(**ambient_kwargs)
    room = AmbientLight(dc_counts=2.0e5, flicker_counts=3.0e5, step_time_s=40.0, step_counts=4.0e5)
    lit = generate_recording(ambient=room, **ambient_kwargs)
    dark_result = PpgSpo2Engine(FS_HZ, calibration).process(dark)
    lit_result = PpgSpo2Engine(FS_HZ, calibration).process(lit)
    settled = dark_result.output_time_s >= 15
    diff = lit_result.spo2_pct[settled] - dark_result.spo2_pct[settled]
    diff_rms = float(np.sqrt(np.nanmean(diff**2)))
    lit_blank = int(np.isnan(lit_result.spo2_pct[settled]).sum())
    rep.check("C10", "120 Hz flicker + room-light step: SpO2 change", diff_rms <= 0.5 and lit_blank == 0,
              f"rms {diff_rms:.2f} % (limit 0.5 %), {lit_blank} blanked")
    rep.info("ambient 2.0e5 counts + 3.0e5 counts 120 Hz flicker, +4.0e5 step at 40 s; LED DC red ~5.5e5, IR ~9.0e5 counts")
    rep.metric("ambient_spo2_rms_change_pct", diff_rms)
    # Known limit, reported rather than checked: with 50 Hz mains the flicker is at 100 Hz, which a
    # 100 Hz frame rate samples at the same phase every frame. The LED and ambient samples then see
    # different but constant flicker levels, so the residue is a DC offset no filter can remove.
    mains50 = generate_recording(ambient=AmbientLight(dc_counts=2.0e5, flicker_counts=3.0e5, mains_frequency_hz=50.0),
                                 **ambient_kwargs)
    mains50_res = PpgSpo2Engine(FS_HZ, calibration).process(mains50)
    mains50_shift = float(np.nanmean(mains50_res.spo2_pct[settled] - dark_result.spo2_pct[settled]))
    rep.info(f"not handled: the same light on 50 Hz mains (100 Hz flicker, phase-locked to the 100 Hz frame) "
             f"shifts SpO2 by {mains50_shift:+.1f} %")
    rep.metric("ambient_50hz_mains_spo2_shift_pct", mains50_shift)

    # Motion: venous-blood and optical-coupling disturbance (rms absorbance, rms coupling change) from
    # barely visible to ten times the pulse, including coupling-only motion (same change at both
    # wavelengths, R = 1), each on 6 recordings with motion from 30 to 60 s.
    motion_cases = [("0.002/0.001", 0.002, 0.001), ("0.005/0.003", 0.005, 0.003), ("0.01/0.005", 0.01, 0.005),
                    ("0.02/0.01", 0.02, 0.01), ("0.05/0.03", 0.05, 0.03), ("0.2/0.1", 0.20, 0.10),
                    ("0/0.005", 0.0, 0.005), ("0/0.02", 0.0, 0.02)]
    motion_errors, wrong_runs, naive_wrong_runs, runs, all_flagged, worst_recovery = [], 0, 0, 0, True, 0.0
    shown_count, window_count, worst_motion_err = 0, 0, 0.0
    motion_figure = None
    for name, venous_rms, coupling_rms in motion_cases:
        case_err, case_wrong, case_shown, case_n, case_recovery = 0.0, 0, 0, 0, 0.0
        for seed in MOTION_SEEDS:
            episode = MotionEpisode(30.0, 60.0, venous_rms, coupling_rms)
            rec = generate_recording(100, FS_HZ, 95.0, 75.0, motion=[episode], seed=seed)
            res = PpgSpo2Engine(FS_HZ, calibration).process(rec)
            err, window = window_errors(res, rec, 30.0, 80.0)
            shown = np.isfinite(err)
            run_max = float(np.max(np.abs(err[shown]))) if shown.any() else 0.0
            motion_errors.extend(err[shown])
            case_err = max(case_err, run_max)
            case_wrong += run_max > SHOWN_ERROR_LIMIT_PCT
            case_shown += int(shown.sum())
            case_n += shown.size
            all_flagged &= bool(np.any(res.status[window] & Status.MOTION))
            after_motion = (res.output_time_s >= 60.0) & np.isfinite(res.spo2_pct)
            case_recovery = max(case_recovery, float(res.output_time_s[after_motion][0] - 60.0) if after_motion.any() else np.inf)
            naive = naive_spo2(res, calibration)
            ref = np.interp(res.output_time_s[window], rec.time_s, rec.sao2_pct)
            naive_wrong_runs += float(np.nanmax(np.abs(naive[window] - ref))) > SHOWN_ERROR_LIMIT_PCT
            runs += 1
            if name == "0.05/0.03" and motion_figure is None:
                motion_figure = (rec, res, naive)
        wrong_runs += case_wrong
        shown_count += case_shown
        window_count += case_n
        worst_recovery = max(worst_recovery, case_recovery)
        worst_motion_err = max(worst_motion_err, case_err)
        rep.info(f"motion {name:<12s} worst shown error {case_err:4.2f} %, runs with a value > {SHOWN_ERROR_LIMIT_PCT:.0f} % off "
                 f"{case_wrong}/{len(MOTION_SEEDS)}, shown {100 * case_shown / case_n:3.0f} % of 30-80 s, back {case_recovery:.0f} s after")
        key = name.replace("/", "_").replace(".", "p")
        rep.metric(f"motion_{key}_worst_err_pct", case_err)
        rep.metric(f"motion_{key}_wrong_runs", case_wrong)
        rep.metric(f"motion_{key}_shown_pct", 100 * case_shown / case_n)
        rep.metric(f"motion_{key}_recovery_s", case_recovery)
    motion_arms = float(np.sqrt(np.mean(np.square(motion_errors)))) if motion_errors else np.inf
    wrong_pct = 100.0 * wrong_runs / runs
    rep.check("C11", f"motion ({runs} runs): shown values, MOTION flag",
              motion_arms <= ISO_ARMS_LIMIT_PCT and wrong_pct <= WRONG_RUN_LIMIT_PCT and all_flagged,
              f"A_rms {motion_arms:.2f} %, {wrong_runs} runs with a value > {SHOWN_ERROR_LIMIT_PCT:.0f} % off (limit {WRONG_RUN_LIMIT_PCT:.0f} %)")
    rep.info(f"worst single value shown {worst_motion_err:.2f} %; values shown in {100 * shown_count / window_count:.0f} % of the 30-80 s windows; "
             f"MOTION flagged in {'every' if all_flagged else 'not every'} run")
    rep.metric("motion_runs", runs)
    rep.metric("motion_arms_shown_pct", motion_arms)
    rep.metric("motion_wrong_runs", wrong_runs)
    rep.metric("motion_worst_err_pct", worst_motion_err)
    rep.metric("motion_shown_pct", 100 * shown_count / window_count)
    rep.check("C12", "motion: SpO2 shown again after motion stops", worst_recovery <= RECOVERY_LIMIT_S,
              f"worst {worst_recovery:.0f} s (limit {RECOVERY_LIMIT_S:.0f} s)")
    rep.metric("motion_worst_recovery_s", worst_recovery)
    rep.check("C13", "motion scenarios are a real test (accept-all)", naive_wrong_runs >= runs / 2,
              f"accepting every beat shows a value > {SHOWN_ERROR_LIMIT_PCT:.0f} % off in {naive_wrong_runs}/{runs} runs")
    rep.metric("motion_naive_wrong_runs", naive_wrong_runs)

    # Fast desaturation, 97 -> 70 % and back: the artifact checks must not reject a real, fast change.
    for rate_pct_s in (1.0, 2.0):
        ramp_s = 27.0 / rate_pct_s

        def fast_profile(t, d=ramp_s):
            t = np.asarray(t, dtype=float)
            down = np.clip((t - 20.0) / d, 0.0, 1.0)
            up = np.clip((t - 50.0 - d) / d, 0.0, 1.0)
            return 97.0 - 27.0 * down + 27.0 * up

        errs, shown_n, total_n = [], 0, 0
        for seed in range(400, 403):
            rec = generate_recording(80 + 2 * ramp_s, FS_HZ, fast_profile, 75.0, seed=seed)
            res = PpgSpo2Engine(FS_HZ, calibration).process(rec)
            err, _ = window_errors(res, rec, 20.0, 60.0 + 2 * ramp_s)
            errs.extend(err[np.isfinite(err)])
            shown_n += int(np.isfinite(err).sum())
            total_n += err.size
        shown_pct = 100.0 * shown_n / total_n
        lag_err = float(np.max(np.abs(errs))) if errs else np.inf
        rep.metric(f"fast_desat_{rate_pct_s:g}pct_s_shown_pct", shown_pct)
        rep.metric(f"fast_desat_{rate_pct_s:g}pct_s_max_err_pct", lag_err)
        if rate_pct_s == 1.0:
            rep.check("C14", "fast desaturation 97 -> 70 % at 1 %/s: still shown", shown_pct >= COVERAGE_LIMIT_PCT,
                      f"shown {shown_pct:.0f} % of the time (limit {COVERAGE_LIMIT_PCT:.0f} %); lag error up to {lag_err:.1f} % (not limited)")
        else:
            rep.info(f"trade-off: at 2 %/s the artifact checks also reject real beats; shown {shown_pct:.0f} % of the time, "
                     f"max lag error {lag_err:.1f} %")
    figures["ambient"] = lit, lit_result
    figures["motion"] = motion_figure

    # ---------------------------------------------------------------------------------------
    rep.section("Pulse rate and perfusion range")
    worst_rate = 0.0
    for rate in (40.0, 120.0, 200.0):
        rec = generate_recording(60, FS_HZ, 97.0, rate, seed=41)
        res = PpgSpo2Engine(FS_HZ, calibration).process(rec)
        ref = reference_pulse_rate_bpm(rec.beat_onset_s, res.output_time_s)
        e = accuracy_stats(res.pulse_rate_bpm[res.output_time_s >= 15], ref[res.output_time_s >= 15])["arms"]
        worst_rate = max(worst_rate, e)
        rep.metric(f"pulse_rate_{int(rate)}_arms_bpm", e)
    rep.check("C15", "pulse rate A_rms at 40, 120 and 200 bpm", worst_rate <= PULSE_RATE_LIMIT_BPM, f"worst {worst_rate:.2f} bpm (target {PULSE_RATE_LIMIT_BPM:.1f})")
    step_rec = generate_recording(90, FS_HZ, 97.0, lambda t: np.where(t < 45, 60.0, 130.0), seed=42)
    step_res = PpgSpo2Engine(FS_HZ, calibration).process(step_rec)
    after = (step_res.output_time_s >= 45) & np.isfinite(step_res.pulse_rate_bpm)
    new_rate = float(np.mean(step_rec.pulse_rate_bpm[step_rec.time_s >= 50]))  # 130 bpm with beat jitter
    settle_time = np.inf
    for t in step_res.output_time_s[after]:
        later = step_res.pulse_rate_bpm[step_res.output_time_s >= t]
        later = later[np.isfinite(later)]  # blank seconds while relearning are not readings
        if later.size >= 10 and np.all(np.abs(later - new_rate) <= 3.0):
            settle_time = t - 45
            break
    rep.check("C16", "pulse rate step 60 -> 130 bpm settles within 3 bpm", settle_time <= 15, f"{settle_time:.0f} s after the step (limit 15 s)")
    rep.metric("pulse_rate_step_settle_s", settle_time)

    worst_pi, perfusion_rows, worst_pi_coverage = 0.0, [], 100.0
    for pi in (0.002, 0.005, 0.01, 0.02, 0.05, 0.10):
        rec = generate_recording(60, FS_HZ, 90.0, 75.0, subject=SubjectOptics(perfusion_index_ir=pi), seed=51)
        res = PpgSpo2Engine(FS_HZ, calibration).process(rec)
        err, _ = window_errors(res, rec, 20.0, 60.0)
        arms = float(np.sqrt(np.nanmean(err**2))) if np.isfinite(err).any() else np.inf
        shown_pct = 100.0 * float(np.mean(np.isfinite(err)))
        perfusion_rows.append((pi, arms, shown_pct))
        worst_pi = max(worst_pi, arms)
        worst_pi_coverage = min(worst_pi_coverage, shown_pct)
        rep.metric(f"perfusion_{pi * 100:g}pct_arms_pct", arms)
        rep.metric(f"perfusion_{pi * 100:g}pct_shown_pct", shown_pct)
    rep.check("C17", "SpO2 A_rms for perfusion index 0.2-10 % (at 90 %)",
              worst_pi <= ISO_ARMS_LIMIT_PCT and worst_pi_coverage >= COVERAGE_LIMIT_PCT,
              " ".join(f"{a:.2f}" for _, a, _ in perfusion_rows) + f" %, shown >= {worst_pi_coverage:.0f} %")
    weak = generate_recording(60, FS_HZ, 97.0, 75.0, subject=SubjectOptics(perfusion_index_ir=0.0002), seed=52)
    weak_res = PpgSpo2Engine(FS_HZ, calibration).process(weak)
    shown_weak = int(np.isfinite(weak_res.spo2_pct).sum())
    weak_flag = bool(np.any(weak_res.status & Status.LOW_PERFUSION))
    rep.check("C18", "perfusion index 0.02 %: no value shown, flagged", shown_weak == 0 and weak_flag,
              f"{shown_weak} values shown, LOW_PERFUSION {'set' if weak_flag else 'not set'}")
    cold = SubjectOptics(perfusion_index_ir=lambda t: np.where(t < 30.0, 0.03, 0.003))
    cold_rec = generate_recording(90, FS_HZ, 92.0, 75.0, subject=cold, seed=53)
    cold_res = PpgSpo2Engine(FS_HZ, calibration).process(cold_rec)
    cold_err, _ = window_errors(cold_res, cold_rec, 30.0 + RECOVERY_LIMIT_S, 90.0)
    cold_ok = np.isfinite(cold_err).all() and np.max(np.abs(cold_err)) <= SHOWN_ERROR_LIMIT_PCT
    rep.check("C19", "perfusion drop 3 % -> 0.3 % (cold finger): resumes", cold_ok,
              f"shown and within {SHOWN_ERROR_LIMIT_PCT:.0f} % for all of 50-90 s" if cold_ok else "not shown or wrong after 50 s")
    rep.metric("perfusion_drop_max_err_pct", float(np.nanmax(np.abs(cold_err))) if np.isfinite(cold_err).any() else np.inf)

    # ---------------------------------------------------------------------------------------
    rep.section("Edge cases")
    off = generate_recording(60, FS_HZ, 97.0, 75.0, sensor_off_after_s=30.0, seed=61)
    off_res = PpgSpo2Engine(FS_HZ, calibration).process(off)
    first_flag = first_flag_delay(off_res, Status.NO_SIGNAL, 30.0)
    shown_after = int(np.isfinite(off_res.spo2_pct[off_res.output_time_s > 30.0]).sum())
    # Probe slipping off in a lit room: from 30 s only 0.5 % of the LED light reaches the detector,
    # so DC stays above zero but is far too weak to trust (AC/DC would be mostly noise).
    room_light = AmbientLight(dc_counts=1.5e5, flicker_counts=5.0e4)
    on_finger = generate_recording(60, FS_HZ, 97.0, 75.0, ambient=room_light, seed=68)
    slipped = generate_recording(60, FS_HZ, 97.0, 75.0, ambient=room_light, seed=69, subject=SubjectOptics(
        led_gain_red_counts=0.005 * 8.0e6, led_gain_ir_counts=0.005 * 4.5e6))
    slip_res = PpgSpo2Engine(FS_HZ, calibration).process(splice(on_finger, slipped, 30.0))
    slip_flag = first_flag_delay(slip_res, Status.NO_SIGNAL, 30.0)
    slip_shown = int(np.isfinite(slip_res.spo2_pct[slip_res.output_time_s > 30.0]).sum())
    rep.check("C20", "probe off: zero light, and 0.5 % light in a lit room", max(first_flag, slip_flag) <= 1.0 and shown_after + slip_shown == 0,
              f"NO_SIGNAL at the next output ({first_flag:.0f} / {slip_flag:.0f} s), {shown_after + slip_shown} shown after 30 s")

    # Probe taken off one patient at 30 s and put on another (80 %) at 32 s: nothing measured on the
    # first patient may be shown for the second.
    patient_a = generate_recording(80, FS_HZ, 97.0, 75.0, seed=70)
    no_probe = generate_recording(80, FS_HZ, 97.0, 75.0, sensor_off_after_s=0.0, seed=71)
    patient_b = generate_recording(80, FS_HZ, 80.0, 90.0, seed=72)
    moved = splice(splice(patient_a, no_probe, 30.0), patient_b, 32.0)
    moved_res = PpgSpo2Engine(FS_HZ, calibration).process(moved)
    moved_err, _ = window_errors(moved_res, moved, 30.5, 80.0)
    moved_shown = np.isfinite(moved_err)
    moved_max = float(np.max(np.abs(moved_err[moved_shown]))) if moved_shown.any() else np.inf
    first_b = float(moved_res.output_time_s[(moved_res.output_time_s > 32.0) & np.isfinite(moved_res.spo2_pct)][0] - 32.0) \
        if np.any((moved_res.output_time_s > 32.0) & np.isfinite(moved_res.spo2_pct)) else np.inf
    rep.check("C21", "probe moved to another patient (97 -> 80 %)", moved_max <= SHOWN_ERROR_LIMIT_PCT and first_b <= RECOVERY_LIMIT_S,
              f"first reading {first_b:.0f} s after re-attach, max |error| {moved_max:.2f} %")
    rep.metric("reattach_first_reading_s", first_b)
    rep.metric("reattach_max_err_pct", moved_max)

    sat = generate_recording(60, FS_HZ, 97.0, 75.0, ambient=AmbientLight(step_time_s=30.0, step_counts=2.0e6), seed=62)
    sat_res = PpgSpo2Engine(FS_HZ, calibration).process(sat)
    sat_flag = first_flag_delay(sat_res, Status.SATURATED, 30.0)
    sat_beats = [b for b in sat_res.beats if b.accepted and b.peak_time_s > 30.0]
    shown_sat = int(np.isfinite(sat_res.spo2_pct[sat_res.output_time_s >= 31.0]).sum())
    rep.check("C22", "ADC saturation (sunlight): SATURATED, no beats used", sat_flag <= 1.0 and not sat_beats and shown_sat == 0,
              f"flag after {sat_flag:.0f} s, {len(sat_beats)} beats accepted, {shown_sat} shown")

    # From 20 s to 40 s the red LED gain is set so the brightest part of each beat (diastole, least
    # blood) exceeds full scale: only the pulse tops clip, the counts are not flat, and a clipped
    # beat would give a wrong R. Before and after, the same probe reads normally.
    clip_gain = 0.997 * 2**21 / np.exp(-SubjectOptics().static_absorbance_red)
    clip = generate_recording(80, FS_HZ, 97.0, 75.0, seed=67)
    clipping = generate_recording(80, FS_HZ, 97.0, 75.0, subject=SubjectOptics(led_gain_red_counts=clip_gain), seed=67)
    after_clip = generate_recording(80, FS_HZ, 97.0, 75.0, seed=66)
    clip = splice(splice(clip, clipping, 20.0), after_clip, 40.0)
    clip_res = PpgSpo2Engine(FS_HZ, calibration).process(clip)
    clipped = clip.red_counts >= clip.adc_full_scale_counts
    # Every accepted beat must come from unclipped samples (previous peak to this peak).
    peaks = [b.peak_index for b in clip_res.beats]
    dirty_beats = sum(
        1 for prev, b in zip([None] + peaks[:-1], clip_res.beats)
        if b.accepted and clipped[(prev if prev is not None else b.valley_index):b.peak_index + 1].any()
    )
    per_second = clipped[: len(clip_res.output_time_s) * int(FS_HZ)].reshape(-1, int(FS_HZ)).any(axis=1)
    flagged_blank = np.all((clip_res.status[per_second] & Status.SATURATED) > 0) and np.all(np.isnan(clip_res.spo2_pct[per_second]))
    clip_err, _ = window_errors(clip_res, clip, 0.0, 80.0)
    clip_max = float(np.nanmax(np.abs(clip_err))) if np.isfinite(clip_err).any() else np.inf
    shown_before = int(np.isfinite(clip_res.spo2_pct[clip_res.output_time_s <= 20.0]).sum())
    back = (clip_res.output_time_s > 40.0) & np.isfinite(clip_res.spo2_pct)
    clip_recovery = float(clip_res.output_time_s[back][0] - 40.0) if back.any() else np.inf
    clip_ok = (dirty_beats == 0 and flagged_blank and clip_max <= SHOWN_ERROR_LIMIT_PCT
               and shown_before >= 5 and clip_recovery <= RECOVERY_LIMIT_S)
    rep.check("C23", "pulse tops clipped 20-40 s: clipped beats never used", clip_ok,
              f"{dirty_beats} clipped beats used, max |error| {clip_max:.2f} %, back {clip_recovery:.0f} s after")
    rep.info(f"{100 * float(np.mean(clipped[(clip.time_s >= 20) & (clip.time_s < 40)])):.0f} % of red samples clipped in 20-40 s; "
             f"{shown_before} readings before, every second with a clipped sample blank and SATURATED")
    rep.metric("clipping_max_err_pct", clip_max)
    rep.metric("clipping_recovery_s", clip_recovery)

    stuck = generate_recording(60, FS_HZ, 97.0, 75.0, seed=63)
    stuck_from = stuck.time_s >= 30.0
    stuck.red_counts[stuck_from] = stuck.red_counts[np.argmax(stuck_from)]
    stuck.ir_counts[stuck_from] = stuck.ir_counts[np.argmax(stuck_from)]
    stuck_res = PpgSpo2Engine(FS_HZ, calibration).process(stuck)
    # A flat line is recognised once one full second (the detection window) is flat: from 31 s on.
    shown_stuck = int(np.isfinite(stuck_res.spo2_pct[stuck_res.output_time_s > 31.0]).sum())
    stuck_flag = bool(np.all(stuck_res.status[stuck_res.output_time_s > 31.0] & Status.NO_SIGNAL))
    rep.check("C24", "flat line (ADC stuck mid-scale): NO_SIGNAL, blank", stuck_flag and shown_stuck == 0,
              f"{shown_stuck} values shown after 31 s (1 s detection window)")

    zero_ok = True
    for offset_counts in (0.0, 30.0):  # all-zero samples, and a 30-count detector offset with no light
        dark_input = generate_recording(20, FS_HZ, 97.0, 75.0, sensor_off_after_s=0.0, seed=64)
        for name in ("red_counts", "ir_counts"):
            getattr(dark_input, name)[:] = offset_counts
        for name in ("red_ambient_counts", "ir_ambient_counts"):
            getattr(dark_input, name)[:] = 0.0
        dark_res = PpgSpo2Engine(FS_HZ, calibration).process(dark_input)
        zero_ok &= bool(np.all(np.isnan(dark_res.normalised_ir)) and np.all(np.isnan(dark_res.normalised_red))
                        and not dark_res.beats and not np.isfinite(dark_res.spo2_pct).any()
                        and np.all(dark_res.status & Status.NO_SIGNAL))
    rep.check("C25", "zero and near-zero DC (0 / 30 counts): never divided", zero_ok,
              "AC/DC never computed, no beats, blank, NO_SIGNAL")

    wrong_led = generate_recording(60, FS_HZ, 97.0, 75.0, subject=SubjectOptics(dpf_ratio_red_ir=8.0), seed=65)
    wrong_res = PpgSpo2Engine(FS_HZ, calibration).process(wrong_led)
    late = wrong_res.output_time_s >= 15
    shown_wrong = int(np.isfinite(wrong_res.spo2_pct[late]).sum())
    wrong_flag = bool(np.all(wrong_res.status[late] & (Status.R_OUT_OF_RANGE | Status.STALE)))
    rep.check("C26", "R outside any SpO2 (wrong LED): blank, flagged", shown_wrong == 0 and wrong_flag,
              f"R {np.nanmedian([b.ratio_r for b in wrong_res.beats]):.2f}, {shown_wrong} values shown")

    high = generate_recording(60, FS_HZ, 100.0, 75.0, subject=SubjectOptics(dpf_ratio_red_ir=1.25), seed=66)
    high_res = PpgSpo2Engine(FS_HZ, calibration).process(high)
    max_shown = float(np.nanmax(high_res.spo2_pct))
    clipped = bool(np.any(high_res.status & Status.CLIPPED_HIGH))
    rep.check("C27", "SpO2 never above 100 %, clipping flagged", max_shown <= 100.0 and clipped,
              f"max shown {max_shown:.1f} %, CLIPPED_HIGH {'set' if clipped else 'not set'}")

    mismatch_raised = False
    try:
        PpgSpo2Engine(250.0, calibration).process(clean)
    except ValueError:
        mismatch_raised = True
    low_rate_raised = False
    try:
        PpgSpo2Engine(10.0, calibration)
    except ValueError:
        low_rate_raised = True
    rep.check("C28", "sample-rate mismatch and too-low rate rejected", mismatch_raised and low_rate_raised,
              "ValueError for 100 Hz data into a 250 Hz engine and for fs 10 Hz")

    # ---------------------------------------------------------------------------------------
    passed = sum(ok for _, ok in rep.results)
    print(f"\n{passed}/{len(rep.results)} checks passed")
    if make_plots:
        save_figures(figures)
    return rep.results


def save_figures(figures):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    IMG_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Signal chain on the ambient-light recording.
    lit, lit_res = figures["ambient"]
    sl = slice(int(36 * FS_HZ), int(46 * FS_HZ))
    t = lit.time_s[sl]
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    axes[0].plot(t, lit.red_counts[sl], label="red LED sample", lw=0.8)
    axes[0].plot(t, lit.ir_counts[sl], label="IR LED sample", lw=0.8)
    axes[0].plot(t, lit.red_ambient_counts[sl], label="ambient sample", lw=0.8)
    axes[0].axvline(40, color="k", ls=":", lw=0.8)
    axes[0].set_ylabel("ADC counts")
    axes[0].set_title("Raw samples: 120 Hz room-light flicker, light switched on at 40 s")
    axes[0].legend(loc="upper left", fontsize=8)
    axes[1].plot(t, lit_res.dc_red_counts[sl], label="DC red")
    axes[1].plot(t, lit_res.dc_ir_counts[sl], label="DC IR")
    axes[1].set_ylabel("DC (counts)")
    axes[1].set_title("After ambient subtraction and the 0.3 Hz low-pass")
    axes[1].legend(loc="upper left", fontsize=8)
    axes[2].plot(t, 100 * lit_res.normalised_red[sl], label="red AC/DC")
    axes[2].plot(t, 100 * lit_res.normalised_ir[sl], label="IR AC/DC")
    for b in lit_res.beats:
        if t[0] <= b.peak_time_s <= t[-1]:
            axes[2].plot(b.peak_index / FS_HZ, 100 * lit_res.normalised_ir[b.peak_index], "o" if b.accepted else "x",
                         color="g" if b.accepted else "r", ms=5)
            if b.valley_index / FS_HZ >= t[0]:
                axes[2].plot(b.valley_index / FS_HZ, 100 * lit_res.normalised_ir[b.valley_index], "v", color="g", ms=4)
    axes[2].set_ylabel("-AC/DC (%)")
    axes[2].set_xlabel("time (s)")
    axes[2].set_title("0.5-5 Hz band-pass, normalised; beats: o peak, v foot (green accepted)")
    axes[2].legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(IMG_DIR / "signal_chain.png", dpi=110)
    plt.close(fig)

    # 2. Calibration curve.
    example, val_spo2, val_ref, cal_r, cal_ref, calibration = figures["study"]
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(cal_r, cal_ref, ".", ms=3, alpha=0.4, label="calibration pairs (8 subjects)")
    r_line = np.linspace(0.35, 1.9, 100)
    ax.plot(r_line, calibration.raw_spo2_pct(r_line), "k", label=f"fit {calibration.intercept_pct:.1f} - {calibration.slope_pct_per_r:.1f} R")
    ax.plot(r_line, TEXTBOOK_CALIBRATION.raw_spo2_pct(r_line), "k--", label="textbook 110 - 25 R")
    s_line = np.linspace(0.6, 1.0, 100)
    ax.plot(ratio_of_ratios_from_saturation(s_line), 100 * s_line, "r:", label="Beer-Lambert model, eq. (3)")
    ax.set_xlabel("ratio of ratios R")
    ax.set_ylabel("SaO2 / SpO2 (%)")
    ax.set_ylim(60, 102)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    ax.set_title("Ratio-of-ratios calibration")
    fig.tight_layout()
    fig.savefig(IMG_DIR / "calibration.png", dpi=110)
    plt.close(fig)

    # 3. Desaturation study: one subject over time and Bland-Altman of the validation cohort.
    rec, res = example
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].plot(rec.time_s, rec.sao2_pct, "k", label="reference SaO2")
    axes[0].plot(res.output_time_s, res.spo2_pct, ".", ms=3, label="engine SpO2 (1 Hz)")
    axes[0].set_xlabel("time (s)")
    axes[0].set_ylabel("%")
    axes[0].set_title("Desaturation plateaus, validation subject 1")
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.3)
    keep = np.isfinite(val_spo2) & np.isfinite(val_ref)
    diff = val_spo2[keep] - val_ref[keep]
    mean = 0.5 * (val_spo2[keep] + val_ref[keep])
    axes[1].plot(mean, diff, ".", ms=3, alpha=0.4)
    bias, sd = diff.mean(), diff.std(ddof=1)
    for y, style in ((bias, "-"), (bias + 1.96 * sd, "--"), (bias - 1.96 * sd, "--")):
        axes[1].axhline(y, color="k", ls=style, lw=0.8)
    axes[1].axhspan(-ISO_ARMS_LIMIT_PCT, ISO_ARMS_LIMIT_PCT, color="g", alpha=0.06)
    axes[1].set_xlabel("mean of SpO2 and SaO2 (%)")
    axes[1].set_ylabel("SpO2 - SaO2 (%)")
    axes[1].set_title("Bland-Altman, 10 validation subjects (bias, +/-1.96 SD)")
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(IMG_DIR / "desaturation_study.png", dpi=110)
    plt.close(fig)

    # 4. Motion.
    rec, res, naive = figures["motion"]
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True)
    axes[0].plot(rec.time_s, 100 * res.normalised_ir, lw=0.6, label="IR AC/DC")
    axes[0].axvspan(30, 60, color="orange", alpha=0.15, label="motion")
    for b in res.beats:
        axes[0].plot(b.peak_time_s, 100 * res.normalised_ir[b.peak_index], "o" if b.accepted else "x",
                     color="g" if b.accepted else "r", ms=3)
    axes[0].set_ylabel("-AC/DC (%)")
    axes[0].set_title("Moderate motion: accepted (o) and rejected (x) beats")
    axes[0].legend(fontsize=8, loc="upper right")
    axes[1].plot(rec.time_s, rec.sao2_pct, "k", label="reference SaO2")
    axes[1].plot(res.output_time_s, naive, "r.", ms=4, label="accept every beat")
    axes[1].plot(res.output_time_s, res.spo2_pct, "g.", ms=4, label="engine (blank = not shown)")
    axes[1].axvspan(30, 60, color="orange", alpha=0.15)
    axes[1].set_ylabel("SpO2 (%)")
    axes[1].set_xlabel("time (s)")
    axes[1].set_ylim(60, 101)
    axes[1].legend(fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(IMG_DIR / "motion.png", dpi=110)
    plt.close(fig)

    # 5. Filter responses.
    f = np.logspace(-2, np.log10(FS_HZ / 2 * 0.999), 600)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.semilogx(f, magnitude_db(design_dc_lowpass(FS_HZ), f, FS_HZ), label="DC low-pass (0.3 Hz, N=2)")
    ax.semilogx(f, magnitude_db(design_ac_bandpass(FS_HZ), f, FS_HZ), label=f"AC band-pass ({AC_BAND_HZ[0]}-{AC_BAND_HZ[1]} Hz, N=4)")
    for x, txt in ((0.25, "breathing"), (20.0, "120 Hz flicker alias")):
        ax.axvline(x, color="gray", ls=":", lw=0.8)
        ax.text(x, -95, txt, rotation=90, fontsize=8, va="bottom")
    ax.set_ylim(-100, 5)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("|H| (dB)")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8)
    ax.set_title(f"Filter responses at fs = {FS_HZ:.0f} Hz")
    fig.tight_layout()
    fig.savefig(IMG_DIR / "filters.png", dpi=110)
    plt.close(fig)
    print(f"figures written to {IMG_DIR.relative_to(Path(__file__).resolve().parent)}/")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-plot", action="store_true", help="skip the figures")
    args = parser.parse_args(argv)
    print("PPG SpO2 06 - main.py (simulated signals; not a medical device, no clinical data)")
    results = run_checks(make_plots=not args.no_plot)
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())

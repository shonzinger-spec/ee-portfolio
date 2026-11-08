"""Validation script for the fault tree risk simulator (simulation only; hypothetical device).

Builds the fault tree of a hypothetical insulin pump, computes its minimal cut sets and exact
top-event probabilities, runs a Monte Carlo of component state transitions over the service life,
cross-checks every Monte Carlo result against an independent analytic or Markov solution, and
evaluates the risk of each hazardous situation the way ISO 14971 describes (severity and probability
of harm against acceptability criteria), before and after one risk control measure. Prints one
PASS/FAIL line per check plus METRIC lines and saves figures to docs/img/. Exit code 0 means every
check passed.

    python3 main.py            # full run with figures
    python3 main.py --no-plot  # checks only

The device and all its numbers are assumptions made for this project, so the results describe the
model, not any real pump; nothing here is a regulatory or clinical claim.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy import stats

from fta_risk import (
    BDD,
    DriftFirstPassage,
    Exponential,
    FaultTree,
    IndependentComponentChain,
    PeriodicallyTested,
    RevealedWeibull,
    TwoStateExponential,
    Weibull,
    and_gate,
    brute_force_probability,
    life_average_unavailability,
    min_cut_upper_bound,
    or_gate,
    rare_event_approximation,
    simulate,
    vote_gate,
)
from fta_risk import pump, risk
from fta_risk.analysis import (
    brownian_drift_paths, component_curves, expected_onsets, hour_midpoints, top_intensity, z_limit,
)
from fta_risk.components import HOURS_PER_YEAR

N_DEVICES = 1_000_000
SEED_INITIAL, SEED_CONTROLLED, SEED_MARKOV, SEED_DRIFT_PATHS, SEED_EDGE = 7001, 7002, 7003, 7004, 7005
SAMPLE_TIMES_H = (4000.5, 17000.5, 30000.5)  # not on any test instant
MARKOV_DEVICES, MARKOV_HORIZON_H = 4000, 300_000.0
MARKOV_BURN_IN_H = 10_000.0  # 50 x the slowest relaxation time (1 / (lambda + mu) ~ 196 h for VREF_DRIFT)
DRIFT_PATHS, DRIFT_STEP_H = 200_000, 48.0
DRIFT_CHECK_TIMES_H = (8784.0, 17520.0, 26304.0, 35040.0)  # whole numbers of 48 h steps
KS_CRITICAL = 1.63  # Kolmogorov-Smirnov critical value x sqrt(n) at 1 %
CONFIDENCE_Z = 1.96  # one-sided 97.5 % upper bound used for the Monte Carlo risk bound
IMG_DIR = Path(__file__).resolve().parent / "docs" / "img"
LIFE_H = pump.SERVICE_LIFE_H
LIFE_Y = pump.SERVICE_LIFE_YEARS
YEAR_WINDOWS_H = tuple((k * HOURS_PER_YEAR, (k + 1) * HOURS_PER_YEAR) for k in range(int(LIFE_Y)))
SHORT = {"H1_OVER_DELIVERY": "H1", "H2_UNDER_DELIVERY": "H2", "ANY_HAZARD": "any"}


class Report:
    def __init__(self):
        self.results: list[tuple[str, bool]] = []
        self.sections = 0

    def section(self, title):
        self.sections += 1
        print(f"\n[{self.sections}] {title}")

    def check(self, check_id, description, passed, detail):
        self.results.append((check_id, bool(passed)))
        print(f"  {check_id} {description:<52s} {detail:<44s} {'PASS' if passed else 'FAIL'}")

    @staticmethod
    def info(text):
        print(f"      info: {text}")

    @staticmethod
    def metric(name, value):
        print(f"METRIC {name} {value:.6g}")


def print_configuration():
    print("Configuration")
    print(f"  service life {LIFE_Y:g} years = {LIFE_H:g} h (1 year = {HOURS_PER_YEAR:g} h); "
          f"self-test every {pump.SELF_TEST_INTERVAL_H:g} h; yearly service check every {pump.SERVICE_CHECK_INTERVAL_H:g} h")
    for model in pump.components(pump.INITIAL_DESIGN):
        print(f"  {model.name:<11s} {model.describe()}{'' if model.hardware else ' (use-related, not hardware)'}")
    print(f"  Hall sensors: total rate {pump.HALL_RATE_PER_H:g}/h each, beta factor {pump.HALL_BETA:g} (common cause)")
    print(f"  VREF_DRIFT: e(t) = v t + sigma W(t), v {pump.VREF_DRIFT_PCT_PER_KH:g} %/kh, sigma "
          f"{pump.VREF_DIFFUSION_PCT_PER_SQRT_KH:g} %/sqrt(kh), tolerance {pump.VREF_TOLERANCE_PCT:g} %; restored only when a check finds it out of tolerance")
    print(f"  designs: initial = {pump.INITIAL_DESIGN.description} ({pump.INITIAL_DESIGN.vref_check_interval_h:g} h); "
          f"with risk control = {pump.CONTROLLED_DESIGN.description} ({pump.CONTROLLED_DESIGN.vref_check_interval_h:g} h)")
    for top, hazard in pump.HAZARDS.items():
        print(f"  {hazard.key}: {hazard.hazardous_situation} -> {hazard.harm}; severity {hazard.severity} "
              f"({risk.SEVERITY_LEVELS[hazard.severity]}), P2 {hazard.p2_harm_given_situation:g}")
    levels = ", ".join(f"{name} >= {lower:g}" for name, lower in risk.PROBABILITY_LEVELS[:-1]) + ", Improbable below"
    print(f"  probability of harm per device-year (illustrative plan): {levels}")
    policy = ", ".join(f"{risk.SEVERITY_LEVELS[s]} up to {lvl}" for s, lvl in risk.MAX_ACCEPTABLE_LEVEL.items())
    print(f"  acceptability (illustrative plan): {policy}")
    print("  P1 for the risk evaluation = expected hazardous situations in the worst year of the life (analytic), "
          "with the Monte Carlo 97.5 % upper bound for that year as a second test")
    print(f"  Monte Carlo: {N_DEVICES} devices per design, seeds {SEED_INITIAL} (initial) and {SEED_CONTROLLED} (control); "
          f"sample times {', '.join(f'{t:g}' for t in SAMPLE_TIMES_H)} h")
    twin = ", ".join(f"{n} {lam:g}/h {rep:g} h" for n, lam, rep in pump.MARKOV_TWIN)
    print(f"  Markov twin of H2 (two-state exponential, rate / mean repair): {twin}; {MARKOV_DEVICES} devices over "
          f"{MARKOV_HORIZON_H:g} h (MTBF counted after a {MARKOV_BURN_IN_H:g} h burn-in), seed {SEED_MARKOV}")
    print(f"  drift paths: {DRIFT_PATHS} Brownian paths, {DRIFT_STEP_H:g} h steps, seed {SEED_DRIFT_PATHS}")
    print("  statistics: Monte Carlo vs analytic agrees when |z| < Bonferroni limit for the family at 1 % "
          f"family-wise error (e.g. {z_limit(1):.2f} for 1, {z_limit(12):.2f} for 12 comparisons)")


def analyse_design(design, seed, report_prefix):
    """Analytic curves, expected onsets and one Monte Carlo run for one design."""
    models = pump.components(design)
    midpoints = hour_midpoints(LIFE_H)
    q, w = component_curves(models, midpoints)
    trees = {top: pump.fault_tree(top) for top in pump.HAZARD_TOPS}
    bdds = {top: BDD(tree) for top, tree in trees.items()}
    cut_sets = {top: tree.minimal_cut_sets() for top, tree in trees.items()}
    analytic = {top: expected_onsets(bdds[top], q, w) for top in pump.HAZARD_TOPS}
    per_year = {top: np.array([float(np.sum(top_intensity(bdds[top], q, w)[(midpoints >= a) & (midpoints < b)]))
                               for a, b in YEAR_WINDOWS_H]) for top in pump.HAZARD_TOPS}
    result = simulate(models, cut_sets, LIFE_H, N_DEVICES, seed, sample_times_h=SAMPLE_TIMES_H,
                      onset_bins_h=np.linspace(0.0, LIFE_H, 49), windows_h=YEAR_WINDOWS_H)
    q_bar = {m.name: float(np.mean(q[m.name])) for m in models}
    time_average_top = {top: float(np.mean(bdds[top].probability(q))) for top in pump.HAZARD_TOPS}
    return {"models": models, "q": q, "w": w, "bdds": bdds, "trees": trees, "cut_sets": cut_sets,
            "analytic": analytic, "per_year": per_year, "mc": result, "q_bar": q_bar, "midpoints": midpoints,
            "time_average_top": time_average_top}


def run_checks(make_plots=True):
    report = Report()
    print("Fault tree risk simulator 07 - main.py (hypothetical insulin pump; simulation only, not a regulatory or clinical claim)")
    print_configuration()

    initial = analyse_design(pump.INITIAL_DESIGN, SEED_INITIAL, "initial")
    controlled = analyse_design(pump.CONTROLLED_DESIGN, SEED_CONTROLLED, "control")
    trees, bdds, q_bar = initial["trees"], initial["bdds"], initial["q_bar"]

    # ================================================================== 1 fault tree logic
    report.section("Fault tree logic (IEC 61025 gates, minimal cut sets, exact probability)")
    mocus = {top: trees[top].minimal_cut_sets() for top in pump.HAZARD_TOPS}
    brute = {top: trees[top].brute_force_minimal_cut_sets() for top in pump.HAZARD_TOPS}
    any_cuts = mocus["ANY_HAZARD"]
    orders = np.bincount([len(c) for c in any_cuts])
    order_text = ", ".join(f"{count} of order {order}" for order, count in enumerate(orders) if count)
    report.check("C01", "MOCUS minimal cut sets = brute-force enumeration", all(mocus[t] == brute[t] for t in mocus),
                 f"{len(any_cuts)} cut sets ({order_text}), 2^{len(pump.BASIC_EVENTS)} states")
    report.metric("cut_sets_total", len(any_cuts))
    report.metric("cut_sets_h1", len(mocus["H1_OVER_DELIVERY"]))
    report.metric("cut_sets_h2", len(mocus["H2_UNDER_DELIVERY"]))
    for cut_set in any_cuts:
        report.info("cut set {" + ", ".join(sorted(cut_set)) + "}")

    exact = {top: float(bdds[top].probability(q_bar)) for top in pump.HAZARD_TOPS}
    brute_p = {top: brute_force_probability(trees[top], q_bar) for top in pump.HAZARD_TOPS}
    rel = max(abs(exact[t] - brute_p[t]) / brute_p[t] for t in exact)
    report.check("C02", "BDD P(top) = sum over all 4096 states (at q-bar)", rel < 1e-12,
                 f"max relative difference {rel:.1e} (limit 1e-12)")
    report.metric("bdd_vs_bruteforce_rel", rel)
    report.info(f"BDD nodes: H1 {bdds['H1_OVER_DELIVERY'].size}, H2 {bdds['H2_UNDER_DELIVERY'].size}, any {bdds['ANY_HAZARD'].size}; "
                f"P(top) at life-average q (q-bar): H1 {exact['H1_OVER_DELIVERY']:.3e}, H2 {exact['H2_UNDER_DELIVERY']:.3e}, any {exact['ANY_HAZARD']:.3e}")
    tavg = initial["time_average_top"]
    report.info(f"time-average of P(top) over the life (BDD at every hour): H1 {tavg['H1_OVER_DELIVERY']:.3e}, "
                f"H2 {tavg['H2_UNDER_DELIVERY']:.3e}, any {tavg['ANY_HAZARD']:.3e} (latent faults on one test grid add up "
                f"in phase, so the time average exceeds P at q-bar)")
    naive = naive_probability(trees["ANY_HAZARD"], q_bar)
    report.info(f"multiplying gate probabilities instead (ignores repeated ALARM_FAIL, ENC_FAIL): any {naive:.6e} "
                f"vs exact {exact['ANY_HAZARD']:.6e} ({(naive / exact['ANY_HAZARD'] - 1) * 100:+.3f} %)")

    ordered = True
    bound_text = []
    for top in pump.HAZARD_TOPS:
        mcub = min_cut_upper_bound(mocus[top], q_bar)
        rare = rare_event_approximation(mocus[top], q_bar)
        ordered &= exact[top] <= mcub * (1 + 1e-12) and mcub <= rare * (1 + 1e-12)
        bound_text.append(f"{(rare / exact[top] - 1) * 100:.3f}")
    report.check("C03", "exact <= min-cut upper bound <= rare-event sum", ordered,
                 f"rare-event above exact by {' / '.join(bound_text)} % (H1/H2/any)")

    q_hall, q_ccf = q_bar["HALL_A"], q_bar["HALL_CCF"]
    position = BDD(trees["ANY_HAZARD"].subtree("POSITION_WRONG"))
    closed = 1 - (1 - q_ccf) * (1 - (3 * q_hall**2 - 2 * q_hall**3))
    vote_err = abs(float(position.probability(q_bar)) - closed) / closed
    ccf_share = q_ccf / closed
    report.check("C04", "2-of-3 vote + common cause = 1-(1-qc)(1-3q^2+2q^3)", vote_err < 1e-9,
                 f"relative difference {vote_err:.1e}; common cause {ccf_share * 100:.1f} % of it")
    report.metric("hall_ccf_share_pct", ccf_share * 100)

    worst_birnbaum = 0.0
    for top in pump.HAZARD_TOPS:
        for event in bdds[top].order:
            h = 0.5 * min(q_bar[event], 1.0 - q_bar[event])  # Q is linear in q_i, so any step is exact
            up, down = dict(q_bar), dict(q_bar)
            up[event] += h
            down[event] -= h
            numeric = (float(bdds[top].probability(up)) - float(bdds[top].probability(down))) / (2 * h)
            analytic = float(bdds[top].birnbaum(q_bar, event))
            worst_birnbaum = max(worst_birnbaum, abs(numeric - analytic) / max(abs(analytic), 1e-300))
    report.check("C05", "Birnbaum importance = dQ/dq_i (finite difference)", worst_birnbaum < 1e-9,
                 f"max relative difference {worst_birnbaum:.1e} (limit 1e-9)")

    # ================================================================== 2 component models
    report.section(f"Component models: Monte Carlo ({N_DEVICES} devices, {LIFE_Y:g} years) vs analytic")
    vref = pump.components(pump.INITIAL_DESIGN)[pump.BASIC_EVENTS.index("VREF_DRIFT")].ttf
    rng = np.random.default_rng(SEED_DRIFT_PATHS)
    crossings = brownian_drift_paths(rng, DRIFT_PATHS, max(DRIFT_CHECK_TIMES_H), DRIFT_STEP_H, vref.drift_per_h,
                                     vref.diffusion_per_sqrt_h, vref.tolerance, bridge=True)
    crossings_plain = brownian_drift_paths(rng, DRIFT_PATHS, max(DRIFT_CHECK_TIMES_H), DRIFT_STEP_H, vref.drift_per_h,
                                           vref.diffusion_per_sqrt_h, vref.tolerance, bridge=False)
    drift_z, plain_bias = [], []
    for t in DRIFT_CHECK_TIMES_H:
        f_exact = float(vref.cdf(t))
        f_mc = float(np.mean(crossings <= t))
        drift_z.append((f_mc - f_exact) / np.sqrt(f_exact * (1 - f_exact) / DRIFT_PATHS))
        plain_bias.append(float(np.mean(crossings_plain <= t)) / f_exact - 1)
    f_life = float(vref.cdf(DRIFT_CHECK_TIMES_H[-1]))
    worst = max(np.abs(drift_z))
    report.check("C06", "drift first passage eq. (1) vs Brownian paths", worst < z_limit(len(DRIFT_CHECK_TIMES_H)),
                 f"worst |z| {worst:.2f} of 4 times (limit {z_limit(4):.2f}); F(4 y) {f_life:.4f}")
    report.metric("drift_paths_worst_z", worst)
    report.metric("vref_crossing_prob_life", f_life)
    report.info(f"without the Brownian-bridge correction, checking the level only every {DRIFT_STEP_H:g} h misses "
                f"crossings: F(4 y) {plain_bias[-1] * 100:+.1f} %")
    report.metric("drift_no_bridge_bias_pct", plain_bias[-1] * 100)

    mc = initial["mc"]
    z_failures = {}
    for model in initial["models"]:
        mean, se = mc.expected_failures(model.name)
        z_failures[model.name] = zscore(mean, model.expected_failures(LIFE_H), se)
    worst_name = max(z_failures, key=lambda n: abs(z_failures[n]))
    report.check("C07", "failures per life, 12 components (eqs. 3-5)", abs(z_failures[worst_name]) < z_limit(12),
                 f"worst |z| {abs(z_failures[worst_name]):.2f} ({worst_name}, limit {z_limit(12):.2f})")
    report.metric("component_failures_worst_z", abs(z_failures[worst_name]))
    for model in initial["models"]:
        mean, se = mc.expected_failures(model.name)
        report.info(f"{model.name:<11s} analytic {model.expected_failures(LIFE_H):.5f}  Monte Carlo {mean:.5f} +/- {se:.5f} per life")

    p_values = []
    for model in initial["models"]:
        for i, t in enumerate(SAMPLE_TIMES_H):
            q_t = float(model.unavailability(np.array([t]))[0])
            observed = int(mc.component_down_at_samples[model.name][i])
            p_values.append(stats.binomtest(observed, N_DEVICES, min(max(q_t, 1e-300), 1.0)).pvalue)
    n_tests = len(p_values)
    report.check("C08", "unavailability q(t) at 3 times, 12 components", min(p_values) > 0.01 / n_tests,
                 f"min exact binomial p {min(p_values):.2g} of {n_tests} (limit {0.01 / n_tests:.1e})")
    report.metric("unavailability_min_p", min(p_values))

    weibull_one = RevealedWeibull("check", Weibull(1.0, 1000.0), 24.0)
    exponential = TwoStateExponential("check", 1e-3, 24.0)
    grid = np.arange(0.0, LIFE_H, 7.3)
    q_err = np.max(np.abs(weibull_one.unavailability(grid) - exponential.unavailability(grid))) / exponential.steady_state_unavailability()
    w_err = np.max(np.abs(weibull_one.failure_intensity(grid) - exponential.failure_intensity(grid)) / exponential.failure_intensity(grid))
    report.check("C09", "renewal solver (eq. 5), shape 1 vs closed form (3)", max(q_err, w_err) < 1e-3,
                 f"max error q {q_err:.1e}, w {w_err:.1e} (limit 1e-3), 1 h grid")
    report.metric("renewal_solver_max_err", max(q_err, w_err))

    general = PeriodicallyTested("check", Weibull(1.0, 1.0 / 2e-5), 72.0)
    memoryless = PeriodicallyTested("check", Exponential(2e-5), 72.0)
    fine = np.arange(0.0, LIFE_H, 0.37)
    latent_err = max(np.max(np.abs(general.unavailability(fine) - memoryless.unavailability(fine))),
                     np.max(np.abs(general.failure_intensity(fine) - memoryless.failure_intensity(fine))) / 2e-5)
    report.check("C10", "test-grid renewal (eq. 4) = 1-exp(-lambda u) for exp.", latent_err < 1e-12,
                 f"max difference {latent_err:.1e} (limit 1e-12)")

    # ================================================================== 3 Markov
    report.section("Markov state-transition model: exponential twin of H2 (6 components, 64 states)")
    twin = pump.markov_twin_components()
    twin_names = [m.name for m in twin]
    h2_tree = trees["H2_UNDER_DELIVERY"]
    h2_cuts = mocus["H2_UNDER_DELIVERY"]
    masks = [sum(1 << twin_names.index(name) for name in cut) for cut in h2_cuts]
    first, chain = None, None
    try:
        chain = IndependentComponentChain([m.failure_rate_per_h for m in twin], [m.repair_rate_per_h for m in twin], masks)
        q_twin = {m.name: m.steady_state_unavailability() for m in twin}
        twin_bdd = BDD(h2_tree)
        vesely = sum(float(twin_bdd.birnbaum(q_twin, m.name)) * m.failure_rate_per_h * (1 - q_twin[m.name]) for m in twin)
        ctmc_frequency = chain.top_frequency()
        freq_err = abs(vesely - ctmc_frequency) / ctmc_frequency
        report.check("C11", "CTMC hazard frequency (eq. 2) = Vesely sum I_B w", freq_err < 1e-10,
                     f"{ctmc_frequency:.6e} /h, relative difference {freq_err:.1e}")
        mttf_exact, mtbf_exact = chain.mttf_from_all_up(), chain.mtbf()
        report.metric("markov_mttf_h", mttf_exact)
        report.metric("markov_mtbf_h", mtbf_exact)

        twin_mc = simulate(twin, {"H2": h2_cuts}, MARKOV_HORIZON_H, MARKOV_DEVICES, SEED_MARKOV, chunk_size=1000,
                           windows_h=((MARKOV_BURN_IN_H, MARKOV_HORIZON_H),))
        first = twin_mc.hazards["H2"].first_onset_h
        censored = int(np.sum(~np.isfinite(first)))
        mttf_mc = float(np.mean(first[np.isfinite(first)]))
        mttf_se = float(np.std(first[np.isfinite(first)], ddof=1) / np.sqrt(np.sum(np.isfinite(first))))
        z_mttf = zscore(mttf_mc, mttf_exact, mttf_se)
        report.check("C12", "Monte Carlo MTTF from all-up vs CTMC (eq. 3)", abs(z_mttf) < z_limit(3) and censored == 0,
                     f"{mttf_mc:.0f} vs {mttf_exact:.0f} h, z {z_mttf:+.2f}, {censored} censored")
        report.metric("mc_mttf_h", mttf_mc)
        # all devices start with every component up; onsets before the burn-in are not counted, so the
        # estimate is the steady-state rate (no start-up bias)
        onsets = twin_mc.hazards["H2"].window_onsets[0]
        steady_h = MARKOV_HORIZON_H - MARKOV_BURN_IN_H
        rate_mc = onsets.mean() / steady_h
        rate_se = onsets.std(ddof=1) / np.sqrt(onsets.size) / steady_h
        mtbf_mc = 1 / rate_mc if rate_mc > 0 else np.inf
        mtbf_se = rate_se / rate_mc**2 if rate_mc > 0 else np.inf
        z_mtbf = zscore(mtbf_mc, mtbf_exact, mtbf_se)
        report.check("C13", "Monte Carlo MTBF (steady state) vs CTMC 1 / w", abs(z_mtbf) < z_limit(3),
                     f"{mtbf_mc:.0f} vs {mtbf_exact:.0f} h, z {z_mtbf:+.2f}, {onsets.sum()} onsets")
        report.metric("mc_mtbf_h", mtbf_mc)
        times = np.sort(first)
        cdf_grid = np.linspace(0.0, times[-1], 600)
        cdf_ctmc = np.interp(times, cdf_grid, chain.first_passage_cdf(cdf_grid))
        n_ks = times.size
        ks = max(np.max(np.arange(1, n_ks + 1) / n_ks - cdf_ctmc), np.max(cdf_ctmc - np.arange(n_ks) / n_ks))
        report.check("C14", "time to first hazard: MC vs CTMC exp(Q t) (eq. 4)", ks < KS_CRITICAL / np.sqrt(n_ks),
                     f"KS distance {ks:.4f} (limit {KS_CRITICAL / np.sqrt(n_ks):.4f}, 1 %)")
        report.metric("markov_ks_distance", ks)

    except (np.linalg.LinAlgError, ValueError, FloatingPointError) as exc:  # a broken chain fails its checks
        for check_id in ("C11", "C12", "C13", "C14"):
            if check_id not in {c for c, _ in report.results}:
                report.check(check_id, "Markov model", False, f"error: {type(exc).__name__}")

    # ================================================================== 4 insulin pump over its life
    report.section(f"Hypothetical insulin pump over a {LIFE_Y:g}-year life: Monte Carlo vs analytic (initial design)")
    z_family = z_limit(2 * len(pump.HAZARD_TOPS))
    z_initial = log_hazards(report, initial, "initial")
    report.check("C15", "hazardous situations per life, H1/H2/any (Vesely)", max(np.abs(z_initial)) < z_family,
                 f"z {' / '.join(f'{z:+.2f}' for z in z_initial)} (limit {z_family:.2f})")
    hw_expected = sum(m.expected_failures(LIFE_H) for m in initial["models"] if m.hardware)
    hw_counts = mc.hardware_failures()
    hw_mean, hw_se = hw_counts.mean(), hw_counts.std(ddof=1) / np.sqrt(hw_counts.size)
    mtbf_device_exact, mtbf_device_mc = LIFE_H / hw_expected, LIFE_H / hw_mean
    z_device = zscore(hw_mean, hw_expected, hw_se)
    report.check("C16", "device MTBF, 4-year average (hardware failures)", abs(z_device) < z_limit(1),
                 f"{mtbf_device_mc:.0f} h MC vs {mtbf_device_exact:.0f} h, z {z_device:+.2f}")
    report.metric("device_mtbf_h", mtbf_device_exact)
    report.metric("device_mtbf_mc_h", mtbf_device_mc)
    report.metric("device_failures_per_life", hw_expected)
    report.info(f"{hw_expected:.3f} hardware failures per pump life (repair or replacement); 4-year average MTBF "
                f"{mtbf_device_exact:.0f} h = {mtbf_device_exact / HOURS_PER_YEAR:.2f} years (analytic; wear-out and drift "
                f"make the failure rate rise with age)")

    # ================================================================== 5 ISO 14971
    report.section("ISO 14971 style risk estimation, evaluation and one risk control (illustrative acceptability plan)")
    estimates = {}
    for label, data in (("initial", initial), ("control", controlled)):
        estimates[label] = {}
        for top, hazard in pump.HAZARDS.items():
            yearly = data["per_year"][top]
            worst = int(np.argmax(yearly))
            estimate = risk.evaluate(hazard, float(yearly[worst]))
            estimates[label][top] = estimate
            verdict = "acceptable" if estimate.acceptable else "NOT acceptable"
            report.info(f"{label:<7s} {hazard.key} situations per device-year, years 1-4: "
                        f"{', '.join(f'{v:.2e}' for v in yearly)} (life average {data['analytic'][top] / LIFE_Y:.2e})")
            report.info(f"{label:<7s} {hazard.key} worst year {worst + 1}: P1 {yearly[worst]:.3e} x P2 "
                        f"{hazard.p2_harm_given_situation:g} = harm {estimate.harm_per_device_year:.2e}/device-year: "
                        f"{estimate.level}, severity {risk.SEVERITY_LEVELS[hazard.severity]} -> {verdict} under the illustrative plan")
            key = f"{label}_{hazard.key.lower()}"
            report.metric(f"{key}_situations_per_year_worst", yearly[worst])
            report.metric(f"{key}_worst_year", worst + 1)
            report.metric(f"{key}_situations_per_year_life_avg", data["analytic"][top] / LIFE_Y)
            report.metric(f"{key}_harm_per_year", estimate.harm_per_device_year)
    flagged = [estimates["initial"][top].hazard.key for top in pump.HAZARDS if not estimates["initial"][top].acceptable]
    report.check("C17", "design target: plan flags initial H2, not H1", flagged == ["H2"],
                 f"not acceptable (illustrative plan): {', '.join(flagged) if flagged else 'none'}")
    importance = bdds["H2_UNDER_DELIVERY"].importance(q_bar)
    ranking = sorted(importance, key=lambda e: -importance[e]["fussell_vesely"])
    report.info("H2 Fussell-Vesely importance (initial, at q-bar): " + ", ".join(
        f"{e} {importance[e]['fussell_vesely']:.3f}" for e in ranking[:4]) + " -> risk control on VREF_DRIFT")
    report.metric("initial_h2_fv_vref", importance["VREF_DRIFT"]["fussell_vesely"])

    z_controlled = log_hazards(report, controlled, "control")
    report.check("C18", "with risk control: MC vs analytic, H1/H2/any", max(np.abs(z_controlled)) < z_family,
                 f"z {' / '.join(f'{z:+.2f}' for z in z_controlled)} (limit {z_family:.2f})")
    all_ok, bound_levels = True, []
    for top, hazard in pump.HAZARDS.items():
        worst = int(np.argmax(controlled["per_year"][top]))
        mean, se = controlled["mc"].hazards[top].window_expected(worst)
        upper = risk.evaluate(hazard, mean + CONFIDENCE_Z * se)
        all_ok &= estimates["control"][top].acceptable and upper.acceptable
        bound_levels.append(f"{hazard.key} {upper.level}")
        report.info(f"control {hazard.key} year {worst + 1} Monte Carlo {mean:.3e} +/- {se:.1e} situations per device-year "
                    f"(analytic {controlled['per_year'][top][worst]:.3e}); 97.5 % bound -> harm {upper.harm_per_device_year:.2e}/device-year, {upper.level}")
        report.metric(f"control_{hazard.key.lower()}_harm_upper_per_year", upper.harm_per_device_year)
    reduction = initial["analytic"]["H2_UNDER_DELIVERY"] / controlled["analytic"]["H2_UNDER_DELIVERY"]
    worst_reduction = initial["per_year"]["H2_UNDER_DELIVERY"].max() / controlled["per_year"]["H2_UNDER_DELIVERY"].max()
    report.check("C19", "design target: control meets plan (analytic + MC bound)", all_ok,
                 f"{', '.join(bound_levels)}; H2 down {reduction:.1f}x over the life")
    report.metric("h2_risk_reduction_factor", reduction)
    report.metric("h2_worst_year_reduction_factor", worst_reduction)

    # ================================================================== 6 edge cases
    report.section("Edge cases and input handling")
    rejected, attempts = invalid_inputs()
    report.check("C20", "invalid trees, rates, probabilities rejected", rejected == attempts,
                 f"{rejected}/{attempts} raised ValueError")

    repeated = FaultTree(("A", "B"), [or_gate("G", "A", "B"), and_gate("TOP", "A", "G")], "TOP")
    single = FaultTree(("A",), [or_gate("TOP", "A")], "TOP")
    q_a, q_b = 0.3, 0.6
    rep = float(BDD(repeated).probability({"A": q_a, "B": q_b}))
    absorbed = repeated.minimal_cut_sets() == [frozenset({"A"})]
    naive_rep = naive_probability(repeated, {"A": q_a, "B": q_b})
    extremes_ok = all(float(bdds["ANY_HAZARD"].probability({e: float(v) for e in pump.BASIC_EVENTS})) == v for v in (0.0, 1.0))
    degenerate_ok = rep == q_a and absorbed and float(BDD(single).probability({"A": q_a})) == q_a and extremes_ok
    report.check("C21", "repeated event (absorption), single event, q = 0/1", degenerate_ok,
                 f"A AND (A OR B): P {rep:.2f} (naive {naive_rep:.2f}), cut set {{A}}; q 0/1 exact")

    rng = np.random.default_rng(SEED_EDGE)
    away = DriftFirstPassage(-vref.drift_per_h, vref.diffusion_per_sqrt_h, vref.tolerance)
    sampled = away.sample(rng, 400_000)
    frac = float(np.mean(np.isfinite(sampled)))
    p_cross = away.crossing_probability
    z_away = (frac - p_cross) / np.sqrt(p_cross * (1 - p_cross) / sampled.size)
    levy = DriftFirstPassage(0.0, vref.diffusion_per_sqrt_h, vref.tolerance)
    levy_err = abs(float(levy.cdf(LIFE_H)) - 2 * stats.norm.sf(levy.tolerance / (levy.diffusion_per_sqrt_h * np.sqrt(LIFE_H))))
    far_tail = float(away.cdf(1e12))
    levy_sampled = float(np.mean(levy.sample(rng, 400_000) <= LIFE_H))
    levy_cdf = float(levy.cdf(LIFE_H))
    z_levy = (levy_sampled - levy_cdf) / np.sqrt(levy_cdf * (1 - levy_cdf) / 400_000)
    report.check("C22", "drift away from limit (v < 0) and no drift (v = 0)",
                 max(abs(z_away), abs(z_levy)) < z_limit(2) and levy_err < 1e-14 and abs(far_tail - p_cross) < 1e-6,
                 f"P(ever) {p_cross:.4f} vs {frac:.4f}; v=0 F(4 y) {levy_cdf:.4f} vs {levy_sampled:.4f}")
    report.metric("drift_away_z", z_away)
    report.metric("levy_z", z_levy)

    twin_cuts = {"H2": h2_cuts}
    run_a = simulate(twin, twin_cuts, 20_000.0, 2000, 99, chunk_size=500).hazards["H2"].onsets
    run_b = simulate(twin, twin_cuts, 20_000.0, 2000, 99, chunk_size=500).hazards["H2"].onsets
    run_c = simulate(twin, twin_cuts, 20_000.0, 2000, 100, chunk_size=500).hazards["H2"].onsets
    report.check("C23", "same seed reproduces, a new seed differs", np.array_equal(run_a, run_b) and not np.array_equal(run_a, run_c),
                 f"onsets {run_a.sum()} / {run_b.sum()} (same seed), {run_c.sum()} (seed + 1)")

    passed = sum(ok for _, ok in report.results)
    print(f"\n{passed}/{len(report.results)} checks passed")
    if make_plots and chain is not None:
        from fta_risk import figures
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        figures.make_all(IMG_DIR, initial=initial, controlled=controlled, estimates=estimates, chain=chain,
                         twin_first_onsets=first, vref=vref, crossings=crossings, drift_rng_seed=SEED_DRIFT_PATHS)
        print("figures written to docs/img/")
    return report.results


def zscore(estimate, expected, standard_error):
    """(estimate - expected) / SE; a zero SE with a nonzero difference is an infinite deviation."""
    difference = float(estimate) - float(expected)
    if standard_error > 0:
        return difference / float(standard_error)
    return 0.0 if difference == 0 else float(np.inf)


def log_hazards(report, data, label):
    zs = []
    for top in pump.HAZARD_TOPS:
        mean, se = data["mc"].hazards[top].expected_onsets()
        analytic = data["analytic"][top]
        z = zscore(mean, analytic, se)
        zs.append(z)
        short = SHORT[top]
        p_any = data["mc"].hazards[top].probability_any()
        report.info(f"{label:<7s} {short:<3s} expected per pump life: analytic {analytic:.4e}, Monte Carlo {mean:.4e} "
                    f"+/- {se:.1e} ({data['mc'].hazards[top].onsets.sum()} onsets); P(>= 1 in the life) {p_any:.4e}; "
                    f"fleet average 1 per {LIFE_Y / analytic:.3g} device-years")
        report.metric(f"{label}_{short.lower()}_per_life", analytic)
        report.metric(f"{label}_{short.lower()}_per_life_mc", mean)
        report.metric(f"{label}_{short.lower()}_onsets_mc", data["mc"].hazards[top].onsets.sum())
        report.metric(f"{label}_{short.lower()}_device_years_per_situation", LIFE_Y / analytic)
    return zs


def naive_probability(tree, q):
    """Gate-by-gate multiplication, as if no event were repeated (for comparison only)."""
    cache = {}

    def value(name):
        if name in cache:
            return cache[name]
        if not tree.is_gate(name):
            result = float(q[name])
        else:
            gate = tree.gates[name]
            inputs = [value(child) for child in gate.inputs]
            if gate.kind == "AND":
                result = float(np.prod(inputs))
            elif gate.kind == "OR":
                result = 1.0 - float(np.prod([1.0 - p for p in inputs]))
            else:
                from itertools import combinations
                n = len(inputs)
                result = 0.0
                for m in range(gate.k, n + 1):
                    for combo in combinations(range(n), m):
                        result += float(np.prod([inputs[i] if i in combo else 1 - inputs[i] for i in range(n)]))
        cache[name] = result
        return result

    return value(tree.top)


def invalid_inputs():
    cases = [
        lambda: FaultTree(("A",), [and_gate("G", "A", "G2"), or_gate("G2", "G")], "G"),  # cycle
        lambda: FaultTree(("A",), [and_gate("G", "A", "B")], "G"),  # undefined input
        lambda: FaultTree(("A", "B"), [vote_gate("G", 3, "A", "B")], "G"),  # k > n
        lambda: FaultTree(("A", "A"), [or_gate("G", "A")], "G"),  # duplicate name
        lambda: FaultTree(("A",), [or_gate("G", "A")], "MISSING"),  # undefined top
        lambda: BDD(FaultTree(("A",), [or_gate("G", "A")], "G")).probability({"A": 1.5}),
        lambda: Exponential(-1e-6),
        lambda: PeriodicallyTested("x", Exponential(1e-6), 0.0),
        lambda: RevealedWeibull("x", Weibull(0.7, 1e4), 24.0),  # infinite density at t = 0
        lambda: DriftFirstPassage(1e-6, 0.0, 1.0),
        lambda: risk.Hazard("H", "s", "h", severity=3, p2_harm_given_situation=1.2),
        lambda: risk.probability_level(float("nan")),
        lambda: simulate([TwoStateExponential("A", 1e-3, 1.0)], {"T": [frozenset({"B"})]}, 100.0, 10, 1),
        lambda: IndependentComponentChain([1e-3], [-1.0], [1]),
    ]
    rejected = 0
    for case in cases:
        try:
            case()
        except ValueError:
            rejected += 1
    return rejected, len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-plot", action="store_true", help="skip the figures")
    args = parser.parse_args()
    results = run_checks(make_plots=not args.no_plot)
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())

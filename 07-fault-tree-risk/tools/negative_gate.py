"""Negative gate: main.py's checks must fail when any one piece of the method is broken.

Each mutant patches the package to break one part of the analysis (an algorithm, a model, a
safeguard or the risk control), runs the full check list, and is caught only if at least one check
FAILs. A crash is reported separately and does not count as caught: it shows the mutant broke the
code, not that a check noticed the error.

    python3 tools/negative_gate.py
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import sys
from functools import reduce
from itertools import combinations
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from fta_risk import bdd as bdd_module  # noqa: E402
from fta_risk import components as comp  # noqa: E402
from fta_risk import markov, pump, risk  # noqa: E402
from fta_risk import tree as tree_module  # noqa: E402
from scipy.special import ndtr  # noqa: E402


@contextlib.contextmanager
def patched(target, name, value):
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield original
    finally:
        setattr(target, name, original)


def naive_gate_probability():
    """BDD replaced by gate-by-gate multiplication (wrong when an event is repeated)."""
    def probability(self, q):
        def value(name):
            if not self.tree.is_gate(name):
                return np.asarray(q[name], dtype=float)
            gate = self.tree.gates[name]
            p = [value(c) for c in gate.inputs]
            if gate.kind == "AND":
                return reduce(np.multiply, p)
            if gate.kind == "OR":
                return 1.0 - reduce(np.multiply, [1.0 - x for x in p])
            total = 0.0
            for m in range(gate.k, len(p) + 1):
                for combo in combinations(range(len(p)), m):
                    total = total + reduce(np.multiply, [p[i] if i in combo else 1.0 - p[i] for i in range(len(p))])
            return total
        return value(self.tree.top)
    return patched(bdd_module.BDD, "probability", probability)


def no_absorption():
    return patched(tree_module, "absorb", lambda cut_sets: list(set(cut_sets)))


def vote_as_or():
    def threshold(self, k, children, memo):
        return reduce(lambda u, v: self._apply("OR", u, v), children)
    return patched(bdd_module.BDD, "_threshold", threshold)


def drift_cdf_without_reflection():
    def cdf(self, t):
        t = np.asarray(t, dtype=float)
        safe = np.where(t > 0, t, 1.0)
        value = ndtr((self.drift_per_h * safe - self.tolerance) / (self.diffusion_per_sqrt_h * np.sqrt(safe)))
        return np.where(t > 0, value, 0.0)
    return patched(comp.DriftFirstPassage, "cdf", cdf)


def restore_one_test_late():
    def sample(self, rng, n, horizon):
        tau = self.test_interval_h
        return comp.renewal_history(rng, n, horizon, lambda size: self.ttf.sample(rng, size),
                                    lambda failure: (np.floor(failure / tau) + 2.0) * tau)
    return patched(comp.PeriodicallyTested, "sample", sample)


def birnbaum_without_difference():
    return patched(bdd_module.BDD, "birnbaum", lambda self, q, event: self.conditional(q, event, 1.0)
                   if event in self.level else np.zeros(np.shape(q[event])))


def generator_without_diagonal():
    original = markov.IndependentComponentChain._generator

    def generator(self):
        q = original(self)
        np.fill_diagonal(q, 0.0)
        return q
    return patched(markov.IndependentComponentChain, "_generator", generator)


def weibull_renewal_ignored():
    original = comp.RevealedWeibull.solve

    def solve(self, horizon):
        t, _, q = original(self, horizon)
        return t, self.ttf.pdf(t), q
    return patched(comp.RevealedWeibull, "solve", solve)


def common_cause_not_simulated():
    original = comp.PeriodicallyTested.sample

    def sample(self, rng, n, horizon):
        if self.name == "HALL_CCF":
            return comp.Intervals(np.zeros(0, np.int64), np.zeros(0), np.zeros(0))
        return original(self, rng, n, horizon)
    return patched(comp.PeriodicallyTested, "sample", sample)


def never_restored():
    original = comp.renewal_history

    def history(rng, n, horizon, draw_up, restore_after):
        return original(rng, n, horizon, draw_up, lambda failure: np.full(failure.size, horizon))
    return patched(comp, "renewal_history", history)


def no_tree_validation():
    return patched(tree_module.FaultTree, "_validate", lambda self, gates: None)


def everything_acceptable():
    return patched(risk, "acceptable", lambda severity, level: True)


def risk_control_not_applied():
    return patched(pump, "CONTROLLED_DESIGN", dataclasses.replace(pump.CONTROLLED_DESIGN,
                                                                  vref_check_interval_h=pump.SERVICE_CHECK_INTERVAL_H))


def drift_away_always_crosses():
    original = comp.DriftFirstPassage.sample

    def sample(self, rng, size):
        if self.drift_per_h >= 0.0:  # only the v < 0 branch is broken
            return original(self, rng, size)
        return rng.wald(self.tolerance / abs(self.drift_per_h), self.tolerance**2 / self.diffusion_per_sqrt_h**2, size)
    return patched(comp.DriftFirstPassage, "sample", sample)


def latent_average_not_sawtooth():
    original = comp.PeriodicallyTested._renewal_sum

    def renewal_sum(self, t, kind):
        if not self.memoryless:
            return original(self, t, kind)
        lam, tau = self.ttf.rate_per_h, self.test_interval_h
        q = np.full(np.shape(t), lam * tau / 2)
        return q if kind == "q" else lam * (1 - q)
    return patched(comp.PeriodicallyTested, "_renewal_sum", renewal_sum)


MUTANTS = [
    ("M1", "BDD replaced by gate-by-gate multiplication", naive_gate_probability),
    ("M2", "MOCUS without absorption", no_absorption),
    ("M3", "2-of-3 vote gate built as OR", vote_as_or),
    ("M4", "drift CDF without the reflection term", drift_cdf_without_reflection),
    ("M5", "latent fault restored one test late (MC)", restore_one_test_late),
    ("M6", "Birnbaum = Q(q_i = 1), not the difference", birnbaum_without_difference),
    ("M7", "CTMC generator without its diagonal", generator_without_diagonal),
    ("M8", "Weibull renewals ignored (w = f)", weibull_renewal_ignored),
    ("M9", "common-cause failure not simulated", common_cause_not_simulated),
    ("M10", "failed components never restored (MC)", never_restored),
    ("M11", "fault tree input validation removed", no_tree_validation),
    ("M12", "every risk judged acceptable", everything_acceptable),
    ("M13", "risk control not applied", risk_control_not_applied),
    ("M14", "drift away from limit always crosses", drift_away_always_crosses),
    ("M15", "latent q as constant lambda tau / 2", latent_average_not_sawtooth),
]


def run_mutant(make_patch):
    buffer = io.StringIO()
    try:
        with make_patch(), contextlib.redirect_stdout(buffer):
            results = main.run_checks(make_plots=False)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return [check_id for check_id, ok in results if not ok], None


def main_gate():
    print("Fault tree risk simulator 07 - negative gate (each mutant breaks one part of the method)")
    baseline, crash = run_mutant(contextlib.nullcontext)
    if crash or baseline:
        print(f"baseline: {'crashed: ' + crash if crash else 'FAILING ' + ', '.join(baseline)}")
        return 1
    print("baseline: all checks pass")
    caught = 0
    for mutant_id, description, make_patch in MUTANTS:
        failed, crash = run_mutant(make_patch)
        if crash:
            print(f"  {mutant_id:<4s} {description:<44s} {'CRASHED':<11s} {crash}")
            continue
        verdict = "caught" if failed else "NOT CAUGHT"
        caught += bool(failed)
        print(f"  {mutant_id:<4s} {description:<44s} {verdict:<11s} failing: {', '.join(failed) if failed else '-'}")
    print(f"\n{caught}/{len(MUTANTS)} mutants caught")
    return 0 if caught == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main_gate())

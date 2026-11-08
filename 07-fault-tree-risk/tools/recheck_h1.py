"""Review follow-up: is the H1 Monte Carlo biased? Six more independent runs of 1,000,000 pumps.

The review noticed H1 above the analytic value in both runs of main.py (232 and 252 onsets against
213 expected). This repeats the full initial-design simulation with six new seeds and pools them.

    python3 tools/recheck_h1.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
from fta_risk import pump, BDD, simulate  # noqa: E402
from fta_risk.analysis import component_curves, expected_onsets, hour_midpoints  # noqa: E402
T = pump.SERVICE_LIFE_H
models = pump.components(pump.INITIAL_DESIGN)
q, w = component_curves(models, hour_midpoints(T))
tree = pump.fault_tree("H1_OVER_DELIVERY")
analytic = expected_onsets(BDD(tree), q, w)
cuts = {"H1": tree.minimal_cut_sets()}
total = 0
seeds = range(9101, 9107)
for seed in seeds:
    r = simulate(models, cuts, T, 1_000_000, seed)
    n = int(r.hazards["H1"].onsets.sum()); total += n
    print(f"seed {seed}: {n} H1 onsets in 1,000,000 pumps (expected {analytic * 1e6:.1f})", flush=True)
exp_total = analytic * 1e6 * len(seeds)
print(f"pooled new runs: {total} vs {exp_total:.1f} expected, z {(total - exp_total) / np.sqrt(exp_total):+.2f} (Poisson)")
# the two main.py runs (seed 7001 initial design, 7002 with the risk control; H1 components are the same)
main_runs = 0
for seed, design in ((7001, pump.INITIAL_DESIGN), (7002, pump.CONTROLLED_DESIGN)):
    r = simulate(pump.components(design), cuts, T, 1_000_000, seed)
    n = int(r.hazards["H1"].onsets.sum()); main_runs += n
    print(f"main.py seed {seed} ({design.name}): {n} H1 onsets", flush=True)
all_total, all_expected = total + main_runs, analytic * 1e6 * (len(seeds) + 2)
print(f"pooled all 8 runs: {all_total} vs {all_expected:.1f} expected, z {(all_total - all_expected) / np.sqrt(all_expected):+.2f} (Poisson)")

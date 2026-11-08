"""Time-dependent analytic results used to cross-check the Monte Carlo.

Expected number of top-event onsets (Vesely): for independent components the top event can only
start when one component fails while the others already hold the rest of a cut set, so

    w_TOP(t)    = sum_i I_B(i, t) w_i(t),    I_B(i, t) = Q(q(t) | q_i = 1) - Q(q(t) | q_i = 0)     (1)
    E[N(0, T)]  = int_0^T w_TOP(t) dt                                                            (2)

(1) is exact for a coherent tree with independent components and no simultaneous failures.
Integrals use the midpoint rule on 1-hour cells aligned to whole hours; every test instant is a
whole number of hours, so no cell straddles a jump of q or w.

Statistics: a Monte Carlo mean m with standard error s agrees with an analytic value a when
|m - a| / s < z_limit(k), the two-sided limit for a family of k such comparisons at a family-wise
error rate of 1 % (Bonferroni: z = Phi^-1(1 - 0.005 / k)).
"""

from __future__ import annotations

import numpy as np
from scipy.special import ndtri

from .bdd import BDD

FAMILY_ALPHA = 0.01


def z_limit(n_comparisons: int) -> float:
    return float(ndtri(1.0 - FAMILY_ALPHA / 2.0 / max(int(n_comparisons), 1)))


def hour_midpoints(horizon_h: float) -> np.ndarray:
    return np.arange(int(round(horizon_h))) + 0.5


def component_curves(components, times_h) -> tuple[dict, dict]:
    q = {c.name: np.asarray(c.unavailability(times_h), dtype=float) for c in components}
    w = {c.name: np.asarray(c.failure_intensity(times_h), dtype=float) for c in components}
    return q, w


def top_intensity(bdd: BDD, q: dict, w: dict) -> np.ndarray:
    """Eq. (1) at every time point of the q and w arrays."""
    total = np.zeros(np.shape(next(iter(q.values()))))
    for event in bdd.order:
        total = total + bdd.birnbaum(q, event) * w[event]
    return total


def expected_onsets(bdd: BDD, q: dict, w: dict, step_h: float = 1.0) -> float:
    """Eq. (2) from curves sampled at hour midpoints."""
    return float(np.sum(top_intensity(bdd, q, w)) * step_h)


def brownian_drift_paths(rng, n_paths: int, horizon_h: float, step_h: float, drift_per_h: float,
                         diffusion_per_sqrt_h: float, tolerance: float, bridge: bool = True):
    """First-passage times of e(t) = v t + sigma W(t) to the tolerance, by direct path simulation.

    Each step adds v dt + sigma sqrt(dt) Z. Checking the level only at the grid points misses
    crossings between them; with the Brownian-bridge correction a step from x0 to x1 (both below
    the level a) still counts as a crossing with probability

        P(cross | x0, x1) = exp(-2 (a - x0)(a - x1) / (sigma^2 dt))                               (3)

    which makes the result exact for this process up to sampling noise. Returns crossing times
    (inf when no crossing by the horizon); the time of a bridge crossing is set to the step end.
    """
    n_steps = int(np.ceil(horizon_h / step_h))
    position = np.zeros(n_paths)
    crossing = np.full(n_paths, np.inf)
    alive = np.ones(n_paths, dtype=bool)
    variance = diffusion_per_sqrt_h**2 * step_h
    for step in range(1, n_steps + 1):
        idx = np.flatnonzero(alive)
        if not idx.size:
            break
        x0 = position[idx]
        x1 = x0 + drift_per_h * step_h + np.sqrt(variance) * rng.standard_normal(idx.size)
        crossed = x1 >= tolerance
        if bridge:
            gap = np.maximum(tolerance - x0, 0.0) * np.maximum(tolerance - x1, 0.0)
            crossed |= rng.random(idx.size) < np.exp(-2.0 * gap / variance)
        crossing[idx[crossed]] = step * step_h
        alive[idx[crossed]] = False
        position[idx] = x1
    return crossing

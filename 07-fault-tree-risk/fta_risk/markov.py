"""Continuous-time Markov chain (CTMC) of independent two-state components, solved exactly.

State s is an n-bit number with bit i = 1 when component i is down. Component i fails at rate
lambda_i and is repaired at rate mu_i, so the generator matrix Q has

    Q[s, s | 2^i]  = lambda_i   (bit i clear),      Q[s, s & ~2^i] = mu_i   (bit i set),
    Q[s, s]        = -sum of the other entries of row s                                         (1)

The top event holds in the states that contain a minimal cut set.

    Steady state:        pi Q = 0,  sum(pi) = 1
    Hazard frequency:    w = sum over s not TOP, s' TOP of pi_s Q[s, s']                         (2)
    MTBF (top event):    1 / w   (mean time between successive hazard onsets in steady state)
    MTTF from all-up:    make the TOP states absorbing; with Q_TT the generator restricted to the
                         other (transient) states, the mean times to absorption m solve
                         -Q_TT m = 1, and MTTF = m[all up]                                        (3)
    First-passage CDF:   F(t) = 1 - e_0^T exp(Q_TT t) 1                                           (4)

For independent components eq. (2) must equal the Vesely frequency formula
    w = sum_i I_B(i) lambda_i (1 - q_i),  q_i = lambda_i / (lambda_i + mu_i),
which main.py checks.
"""

from __future__ import annotations

import numpy as np
from scipy import linalg

MAX_MARKOV_COMPONENTS = 12  # 4096 states, dense matrices


class IndependentComponentChain:
    def __init__(self, failure_rates, repair_rates, cut_set_masks):
        self.failure_rates = np.asarray(failure_rates, dtype=float)
        self.repair_rates = np.asarray(repair_rates, dtype=float)
        n = self.failure_rates.size
        if n == 0 or n > MAX_MARKOV_COMPONENTS or self.repair_rates.size != n:
            raise ValueError(f"need 1..{MAX_MARKOV_COMPONENTS} components with one repair rate each")
        if np.any(self.failure_rates < 0) or np.any(self.repair_rates < 0) or not np.all(np.isfinite(
                np.concatenate([self.failure_rates, self.repair_rates]))):
            raise ValueError("rates must be finite and >= 0")
        if not cut_set_masks:
            raise ValueError("need at least one cut set")
        self.n = n
        self.n_states = 2**n
        states = np.arange(self.n_states)
        self.top = np.zeros(self.n_states, dtype=bool)
        for mask in cut_set_masks:
            self.top |= (states & mask) == mask
        self.generator = self._generator()

    def _generator(self) -> np.ndarray:
        q = np.zeros((self.n_states, self.n_states))
        states = np.arange(self.n_states)
        for i in range(self.n):
            bit = 1 << i
            up = (states & bit) == 0
            q[states[up], states[up] | bit] += self.failure_rates[i]
            q[states[~up], states[~up] & ~bit] += self.repair_rates[i]
        q[states, states] = -q.sum(axis=1)
        return q

    def steady_state(self) -> np.ndarray:
        a = self.generator.T.copy()
        a[-1, :] = 1.0  # replace one balance equation by the normalisation sum(pi) = 1
        b = np.zeros(self.n_states)
        b[-1] = 1.0
        return linalg.solve(a, b)

    def top_frequency(self) -> float:
        pi = self.steady_state()
        into_top = self.generator[np.ix_(~self.top, self.top)]
        return float(pi[~self.top] @ into_top.sum(axis=1))

    def top_unavailability(self) -> float:
        return float(self.steady_state()[self.top].sum())

    def mtbf(self) -> float:
        return 1.0 / self.top_frequency()

    def _transient_generator(self):
        transient = np.flatnonzero(~self.top)
        if transient.size == 0 or transient[0] != 0:
            raise ValueError("the all-up state already has the top event")
        return transient, self.generator[np.ix_(transient, transient)]

    def mttf_from_all_up(self) -> float:
        _, q_tt = self._transient_generator()
        times = linalg.solve(-q_tt, np.ones(q_tt.shape[0]))
        return float(times[0])

    def first_passage_cdf(self, times_h) -> np.ndarray:
        _, q_tt = self._transient_generator()
        start = np.zeros(q_tt.shape[0])
        start[0] = 1.0
        return np.array([1.0 - start @ linalg.expm(q_tt * t) @ np.ones(q_tt.shape[0]) for t in np.atleast_1d(times_h)])

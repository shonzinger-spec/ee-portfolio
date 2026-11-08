"""Basic-event models: how each component fails, how the failure is found, and how it is restored.

Each model gives, as functions of time t since the device was new (hours),

    q(t)  unavailability: probability the basic event is true (component failed) at time t
    w(t)  failure intensity: rate of up -> down transitions, so E[failures in (0, T)] = int_0^T w dt

and draws Monte Carlo histories as down intervals [failure, restoration) per simulated device.

Time-to-failure distributions (X is the time from new or from the last restoration to failure):

    Exponential (random hardware failure)   F(t) = 1 - exp(-lambda t)
    Weibull (wear-out)                      F(t) = 1 - exp(-(t / eta)^beta)
    Drift first passage                     reference error e(t) = v t + sigma W(t)  (W: Wiener process)
                                            fails when e first reaches the tolerance a:
        F(t) = Phi((v t - a) / (sigma sqrt t)) + exp(2 v a / sigma^2) Phi(-(v t + a) / (sigma sqrt t))  (1)
        f(t) = a / (sigma sqrt(2 pi t^3)) exp(-(a - v t)^2 / (2 sigma^2 t))                              (2)
    (1) is the inverse Gaussian distribution with mean a / v and shape a^2 / sigma^2. For v < 0 the
    error drifts away from the limit and F(inf) = exp(2 v a / sigma^2) < 1: some references never
    cross. sigma in units per sqrt(hour) is how long-term drift of voltage references is specified
    (ppm per sqrt(1000 h)).

How failures are found and restored:

    TwoStateExponential   revealed at once, restored after an exponential repair time (mean 1 / mu):
        q(t) = lambda / (lambda + mu) (1 - exp(-(lambda + mu) t)),   w(t) = lambda (1 - q(t))      (3)
    RevealedWeibull       revealed at once, replaced by a new part after an exponential repair time.
                          w is the renewal density, solved numerically (renewal equation, eq. 5)
    PeriodicallyTested    latent: stays failed until the next test at t = k tau, which restores it.
                          Restorations happen on the test grid, so the process renews there. With
                          p_m = F(m tau) - F((m - 1) tau) the probability that a cycle ends at its
                          m-th test, the probability of a restoration at test k is
        r_0 = 1,   r_k = sum_{j < k} r_j p_{k - j}                                                 (4a)
                          and for t = k tau + u, 0 <= u < tau:
        q(t) = sum_{m=0..k} r_{k-m} (F(m tau + u) - F(m tau)),   w(t) = sum_{m=0..k} r_{k-m} f(m tau + u)  (4b)
        E[N(0, T)] = sum_{j tau < T} r_j F(T - j tau)                                               (4c)
                          For exponential X this reduces to q = 1 - exp(-lambda u), w = lambda exp(-lambda u).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal
from scipy.special import log_ndtr, ndtr

HOURS_PER_YEAR = 8766.0  # 365.25 days


def _check_positive(name: str, value: float, allow_zero: bool = False) -> float:
    value = float(value)
    if not np.isfinite(value) or value < 0.0 or (value == 0.0 and not allow_zero):
        raise ValueError(f"{name} must be {'>= 0' if allow_zero else '> 0'} and finite, got {value}")
    return value


# ====================================================================== time-to-failure distributions
@dataclass(frozen=True)
class Exponential:
    rate_per_h: float

    def __post_init__(self):
        _check_positive("failure rate", self.rate_per_h, allow_zero=True)

    def cdf(self, t):
        t = np.asarray(t, dtype=float)
        return np.where(t > 0.0, -np.expm1(-self.rate_per_h * np.maximum(t, 0.0)), 0.0)

    def pdf(self, t):
        t = np.asarray(t, dtype=float)
        return np.where(t >= 0.0, self.rate_per_h * np.exp(-self.rate_per_h * np.maximum(t, 0.0)), 0.0)

    def sample(self, rng, size):
        if self.rate_per_h == 0.0:
            return np.full(size, np.inf)
        return rng.exponential(1.0 / self.rate_per_h, size)

    def describe(self):
        return f"exponential, lambda {self.rate_per_h:.3g} /h"


@dataclass(frozen=True)
class Weibull:
    shape: float
    scale_h: float

    def __post_init__(self):
        _check_positive("Weibull shape", self.shape)
        _check_positive("Weibull scale", self.scale_h)

    def cdf(self, t):
        t = np.maximum(np.asarray(t, dtype=float), 0.0)
        return -np.expm1(-(t / self.scale_h) ** self.shape)

    def pdf(self, t):
        t = np.maximum(np.asarray(t, dtype=float), 0.0)
        z = t / self.scale_h
        with np.errstate(divide="ignore", invalid="ignore"):
            density = self.shape / self.scale_h * z ** (self.shape - 1.0) * np.exp(-z**self.shape)
        return np.nan_to_num(density, nan=0.0, posinf=np.inf)

    def sample(self, rng, size):
        return self.scale_h * rng.weibull(self.shape, size)

    def describe(self):
        return f"Weibull, beta {self.shape:g}, eta {self.scale_h:.4g} h"


@dataclass(frozen=True)
class DriftFirstPassage:
    """First time the error v t + sigma W(t), starting at 0, reaches the tolerance a (eqs. 1-2)."""

    drift_per_h: float
    diffusion_per_sqrt_h: float
    tolerance: float

    def __post_init__(self):
        if not np.isfinite(self.drift_per_h):
            raise ValueError("drift rate must be finite")
        _check_positive("drift diffusion", self.diffusion_per_sqrt_h)
        _check_positive("drift tolerance", self.tolerance)

    @property
    def crossing_probability(self) -> float:
        """F(inf): 1 for drift towards the limit, exp(2 v a / sigma^2) for drift away from it."""
        v, sigma, a = self.drift_per_h, self.diffusion_per_sqrt_h, self.tolerance
        return 1.0 if v >= 0.0 else float(np.exp(2.0 * v * a / sigma**2))

    def cdf(self, t):
        t = np.asarray(t, dtype=float)
        v, sigma, a = self.drift_per_h, self.diffusion_per_sqrt_h, self.tolerance
        safe_t = np.where(t > 0.0, t, 1.0)
        scale = sigma * np.sqrt(safe_t)
        first = ndtr((v * safe_t - a) / scale)
        # second term in log space: exp(2va/sigma^2) can overflow while Phi(...) underflows
        second = np.exp(2.0 * v * a / sigma**2 + log_ndtr(-(v * safe_t + a) / scale))
        return np.where(t > 0.0, np.clip(first + second, 0.0, 1.0), 0.0)

    def pdf(self, t):
        t = np.asarray(t, dtype=float)
        v, sigma, a = self.drift_per_h, self.diffusion_per_sqrt_h, self.tolerance
        safe_t = np.where(t > 0.0, t, 1.0)
        density = a / (sigma * np.sqrt(2.0 * np.pi * safe_t**3)) * np.exp(-((a - v * safe_t) ** 2) / (2.0 * sigma**2 * safe_t))
        return np.where(t > 0.0, density, 0.0)

    def sample(self, rng, size):
        v, sigma, a = self.drift_per_h, self.diffusion_per_sqrt_h, self.tolerance
        shape = a**2 / sigma**2
        if v == 0.0:  # pure diffusion: Levy distribution, a^2 / (sigma^2 Z^2)
            return shape / rng.standard_normal(size) ** 2
        times = rng.wald(a / abs(v), shape, size)
        if v < 0.0:  # conditional on crossing, the time is inverse Gaussian with drift |v|
            times = np.where(rng.random(size) < self.crossing_probability, times, np.inf)
        return times

    def describe(self):
        return (f"drift first passage, v {self.drift_per_h * 1e3:.3g}/kh, sigma {self.diffusion_per_sqrt_h * np.sqrt(1e3):.3g}/sqrt(kh), "
                f"tolerance {self.tolerance:g}")


# ====================================================================== Monte Carlo histories
@dataclass
class Intervals:
    """Down intervals of one component over many simulated devices (zero-length ones included)."""

    traj: np.ndarray
    start: np.ndarray
    end: np.ndarray

    def failure_counts(self, n_traj: int) -> np.ndarray:
        return np.bincount(self.traj, minlength=n_traj)

    def positive(self) -> "Intervals":
        keep = self.end > self.start
        return Intervals(self.traj[keep], self.start[keep], self.end[keep])

    def down_at(self, t: float, n_traj: int) -> np.ndarray:
        down = np.zeros(n_traj, dtype=bool)
        down[self.traj[(self.start <= t) & (self.end > t)]] = True
        return down


def renewal_history(rng, n_traj: int, horizon_h: float, draw_up, restore_after) -> Intervals:
    """Alternate up and down periods for every device until the horizon.

    draw_up(size) gives up durations; restore_after(failure_times) gives restoration times. Down
    intervals are clipped at the horizon.
    """
    trajs, starts, ends = [], [], []
    active = np.arange(n_traj)
    clock = np.zeros(n_traj)
    while active.size:
        failure = clock + draw_up(active.size)
        alive = failure < horizon_h
        active, failure = active[alive], failure[alive]
        if not active.size:
            break
        restore = restore_after(failure)
        trajs.append(active)
        starts.append(failure)
        ends.append(np.minimum(restore, horizon_h))
        keep = restore < horizon_h
        active, clock = active[keep], restore[keep]
    if not trajs:
        empty = np.zeros(0)
        return Intervals(np.zeros(0, dtype=np.int64), empty, empty.copy())
    return Intervals(np.concatenate(trajs).astype(np.int64), np.concatenate(starts), np.concatenate(ends))


# ====================================================================== component models
@dataclass(frozen=True)
class TwoStateExponential:
    """Revealed failure with exponential repair (eq. 3); also used for use-related events (occlusion)."""

    name: str
    failure_rate_per_h: float
    mean_repair_h: float
    hardware: bool = True
    description: str = ""
    revealed: bool = field(default=True, init=False)

    def __post_init__(self):
        _check_positive(f"{self.name} failure rate", self.failure_rate_per_h, allow_zero=True)
        _check_positive(f"{self.name} mean repair time", self.mean_repair_h, allow_zero=True)

    @property
    def repair_rate_per_h(self) -> float:
        return np.inf if self.mean_repair_h == 0.0 else 1.0 / self.mean_repair_h

    def unavailability(self, t):
        t = np.maximum(np.asarray(t, dtype=float), 0.0)
        lam, mu = self.failure_rate_per_h, self.repair_rate_per_h
        if lam == 0.0 or np.isinf(mu):
            return np.zeros(t.shape)
        return lam / (lam + mu) * -np.expm1(-(lam + mu) * t)

    def failure_intensity(self, t):
        return self.failure_rate_per_h * (1.0 - self.unavailability(t))

    def expected_failures(self, horizon_h: float) -> float:
        lam, mu = self.failure_rate_per_h, self.repair_rate_per_h
        if lam == 0.0 or np.isinf(mu):
            return lam * horizon_h
        s = lam + mu
        integral_q = lam / s * (horizon_h + np.expm1(-s * horizon_h) / s)
        return float(lam * (horizon_h - integral_q))

    def steady_state_unavailability(self) -> float:
        lam, mu = self.failure_rate_per_h, self.repair_rate_per_h
        return 0.0 if np.isinf(mu) else lam / (lam + mu)

    def sample(self, rng, n_traj: int, horizon_h: float) -> Intervals:
        ttf = Exponential(self.failure_rate_per_h)
        if self.mean_repair_h == 0.0:
            restore = lambda failure: failure
        else:
            restore = lambda failure: failure + rng.exponential(self.mean_repair_h, failure.size)
        return renewal_history(rng, n_traj, horizon_h, lambda size: ttf.sample(rng, size), restore)

    def describe(self):
        return f"exponential lambda {self.failure_rate_per_h:.3g}/h, revealed, mean repair {self.mean_repair_h:g} h"


@dataclass(frozen=True)
class RevealedWeibull:
    """Revealed wear-out failure; the part is replaced (age reset) after an exponential repair time.

    Renewal equation for the failure density h(t) (= w(t)), with g the density of repair + new life:

        h(t) = f(t) + int_0^t h(s) g(t - s) ds,     q(t) = int_0^t h(s) exp(-mu (t - s)) ds        (5)

    solved on a uniform grid with the trapezoid rule (g(0) = 0, so each step is explicit).
    """

    name: str
    ttf: Weibull
    mean_repair_h: float
    hardware: bool = True
    description: str = ""
    grid_step_h: float = 1.0
    revealed: bool = field(default=True, init=False)
    _cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.ttf.shape < 1.0:
            raise ValueError(f"{self.name}: Weibull shape below 1 has an infinite density at t = 0; use shape >= 1")
        _check_positive(f"{self.name} mean repair time", self.mean_repair_h, allow_zero=True)
        _check_positive(f"{self.name} grid step", self.grid_step_h)

    def solve(self, horizon_h: float):
        """(t grid, h, q) of eq. (5) up to at least horizon_h."""
        n_steps = int(np.ceil(horizon_h / self.grid_step_h)) + 1
        cached = self._cache.get("solution")
        if cached is not None and cached[0].size >= n_steps:
            return cached
        dt = self.grid_step_h
        t = np.arange(n_steps) * dt
        f = self.ttf.pdf(t)
        if self.mean_repair_h == 0.0:
            g = f.copy()
        else:
            # Both g = f * (mu e^{-mu t}) and q = h * e^{-mu t} are integrals against an exponential
            # kernel. With the integrand linear inside each step they follow exactly from
            #   y_k = a y_{k-1} + c0 x_{k-1} + c1 x_k,   a = e^{-mu dt},
            #   I0 = (1 - a) / mu,  c1 = (dt - I0) / (mu dt),  c0 = I0 - c1
            mu = 1.0 / self.mean_repair_h
            a = np.exp(-mu * dt)
            i0 = -np.expm1(-mu * dt) / mu
            c1 = (dt - i0) / (mu * dt)
            c0 = i0 - c1
            decay = a ** np.arange(n_steps)
            # lfilter starts with y_0 = c1 x_0; the integral over [0, 0] is 0, so remove that term
            g = mu * (signal.lfilter([c1, c0], [1.0, -a], f) - c1 * f[0] * decay)
        h = np.zeros(n_steps)
        h[0] = f[0]
        # trapezoid: h_k = f_k + dt (h_0 g_k / 2 + sum_{j=1}^{k-1} h_j g_{k-j} + h_k g_0 / 2), solved for h_k.
        # g_0 = 0 when there is a repair time; without one g = f and g_0 = f(0) can be nonzero (shape 1).
        implicit = 1.0 - 0.5 * dt * g[0]
        if implicit <= 0.0:
            raise ValueError(f"{self.name}: grid step too coarse for the density at t = 0")
        for k in range(1, n_steps):
            h[k] = (f[k] + dt * (0.5 * h[0] * g[k] + np.dot(h[1:k], g[k - 1:0:-1]))) / implicit
        if self.mean_repair_h == 0.0:
            q = np.zeros(n_steps)
        else:
            q = signal.lfilter([c1, c0], [1.0, -a], h) - c1 * h[0] * decay
        self._cache["solution"] = (t, h, q)
        return t, h, q

    def unavailability(self, t):
        t = np.asarray(t, dtype=float)
        grid, _, q = self.solve(float(np.max(t, initial=0.0)))
        return np.interp(t, grid, q)

    def failure_intensity(self, t):
        t = np.asarray(t, dtype=float)
        grid, h, _ = self.solve(float(np.max(t, initial=0.0)))
        return np.interp(t, grid, h)

    def expected_failures(self, horizon_h: float) -> float:
        grid, h, _ = self.solve(horizon_h)
        inside = grid <= horizon_h
        return float(np.trapezoid(h[inside], grid[inside]))

    def sample(self, rng, n_traj: int, horizon_h: float) -> Intervals:
        if self.mean_repair_h == 0.0:
            restore = lambda failure: failure
        else:
            restore = lambda failure: failure + rng.exponential(self.mean_repair_h, failure.size)
        return renewal_history(rng, n_traj, horizon_h, lambda size: self.ttf.sample(rng, size), restore)

    def describe(self):
        return f"{self.ttf.describe()}, revealed, mean repair {self.mean_repair_h:g} h"


@dataclass(frozen=True)
class PeriodicallyTested:
    """Latent failure found and restored at the next periodic test (eqs. 4a-4c)."""

    name: str
    ttf: Exponential | Weibull | DriftFirstPassage
    test_interval_h: float
    hardware: bool = True
    description: str = ""
    revealed: bool = field(default=False, init=False)
    _cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self):
        _check_positive(f"{self.name} test interval", self.test_interval_h)

    @property
    def memoryless(self) -> bool:
        return isinstance(self.ttf, Exponential)

    def restoration_probabilities(self, n_tests: int) -> np.ndarray:
        """r_0..r_n of eq. (4a)."""
        cached = self._cache.get("r")
        if cached is not None and cached.size > n_tests:
            return cached[: n_tests + 1]
        tau = self.test_interval_h
        cdf = self.ttf.cdf(np.arange(n_tests + 1) * tau)
        p = np.diff(cdf, prepend=0.0)  # p_m = F(m tau) - F((m-1) tau), p_0 = 0
        r = np.zeros(n_tests + 1)
        r[0] = 1.0
        for k in range(1, n_tests + 1):
            r[k] = np.dot(r[:k], p[k:0:-1])
        self._cache["r"] = r
        return r

    def _renewal_sum(self, t, kind: str):
        t = np.maximum(np.asarray(t, dtype=float), 0.0)
        tau = self.test_interval_h
        k = np.floor(t / tau).astype(np.int64)
        u = t - k * tau
        if self.memoryless:
            lam = self.ttf.rate_per_h
            return -np.expm1(-lam * u) if kind == "q" else lam * np.exp(-lam * u)
        flat_t, flat_k, flat_u = t.ravel(), k.ravel(), u.ravel()
        r = self.restoration_probabilities(int(flat_k.max(initial=0)))
        out = np.zeros(flat_t.size)
        for begin in range(0, flat_t.size, 2048):  # bounded memory: (block x tests) matrix
            kb, ub = flat_k[begin:begin + 2048], flat_u[begin:begin + 2048]
            m = np.arange(int(kb.max(initial=0)) + 1)
            valid = m[None, :] <= kb[:, None]
            weights = np.where(valid, r[np.clip(kb[:, None] - m[None, :], 0, None)], 0.0)
            ages = m[None, :] * tau + ub[:, None]
            if kind == "q":
                terms = self.ttf.cdf(ages) - self.ttf.cdf(m * tau)[None, :]
            else:
                terms = self.ttf.pdf(ages)
            out[begin:begin + 2048] = np.sum(weights * terms, axis=1)
        return out.reshape(t.shape)

    def unavailability(self, t):
        return self._renewal_sum(t, "q")

    def failure_intensity(self, t):
        return self._renewal_sum(t, "w")

    def expected_failures(self, horizon_h: float) -> float:
        tau = self.test_interval_h
        n_tests = int(np.ceil(horizon_h / tau))
        resets = np.arange(n_tests) * tau
        resets = resets[resets < horizon_h]
        r = self.restoration_probabilities(resets.size)
        return float(np.sum(r[: resets.size] * self.ttf.cdf(horizon_h - resets)))

    def sample(self, rng, n_traj: int, horizon_h: float) -> Intervals:
        tau = self.test_interval_h
        restore = lambda failure: (np.floor(failure / tau) + 1.0) * tau  # next test strictly after
        return renewal_history(rng, n_traj, horizon_h, lambda size: self.ttf.sample(rng, size), restore)

    def describe(self):
        return f"{self.ttf.describe()}, latent, tested every {self.test_interval_h:g} h"


def life_average_unavailability(component, horizon_h: float, step_h: float = 1.0) -> float:
    """(1/T) int_0^T q dt by the midpoint rule on cells aligned to whole hours (no test jump inside)."""
    midpoints = (np.arange(int(round(horizon_h / step_h))) + 0.5) * step_h
    return float(np.mean(component.unavailability(midpoints)))

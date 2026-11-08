"""Monte Carlo simulation of component state transitions and the hazard (top event) they cause.

Every simulated device draws, independently for each component, its history of down intervals
[failure, restoration) over the horizon (components.py). A minimal cut set C is "fully down" while
all its components are down, and a hazard (top event) is present while at least one of its cut sets
is fully down:

    TOP(t) = OR over C of AND over i in C of x_i(t)

Both operations are done for all devices at once with an event sweep: each interval contributes +1 at
its start and -1 at its end; sorting the events by (device, time, sign) and taking a running sum
gives, for a cut set, the number of its components that are down (the intersection is where the sum
equals |C|), and for a hazard, the number of fully-down cut sets (the union is where it is >= 1).
At equal times ends are sorted before starts, so a restoration and a failure at the same instant do
not create a zero-length overlap. The sums restart at zero for every device because each device's
events add up to zero.

Outputs per device: number of hazard onsets (0 -> 1 transitions of TOP), first onset time, number of
failures of each component; and whether each component and hazard is down at chosen sample times.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .components import Intervals


def _sweep(trajs: list[np.ndarray], starts: list[np.ndarray], ends: list[np.ndarray], level: int) -> Intervals:
    """Intervals where at least `level` of the given interval families overlap (per device)."""
    traj = np.concatenate([np.concatenate([t, t]) for t in trajs]) if trajs else np.zeros(0, np.int64)
    time = np.concatenate([np.concatenate([s, e]) for s, e in zip(starts, ends)]) if trajs else np.zeros(0)
    sign = np.concatenate([np.concatenate([np.ones(s.size, np.int64), -np.ones(s.size, np.int64)])
                           for s in starts]) if trajs else np.zeros(0, np.int64)
    if not traj.size:
        return Intervals(np.zeros(0, np.int64), np.zeros(0), np.zeros(0))
    order = np.lexsort((sign, time, traj))  # last key is primary: device, then time, then -1 before +1
    traj, time, sign = traj[order], time[order], sign[order]
    count = np.cumsum(sign)
    rising = (sign > 0) & (count == level)
    falling = (sign < 0) & (count == level - 1)
    return Intervals(traj[rising], time[rising], time[falling])


def intersect(families: list[Intervals]) -> Intervals:
    """Intervals where every family is down. Each family must be disjoint within a device."""
    families = [family.positive() for family in families]
    if any(not family.traj.size for family in families):
        return Intervals(np.zeros(0, np.int64), np.zeros(0), np.zeros(0))
    if len(families) == 1:
        return families[0]
    # keep only devices in which every family has at least one interval (cheap pre-filter)
    n = int(max(family.traj.max() for family in families)) + 1
    common = np.ones(n, dtype=bool)
    for family in families:
        present = np.zeros(n, dtype=bool)
        present[family.traj] = True
        common &= present
    kept = [Intervals(f.traj[common[f.traj]], f.start[common[f.traj]], f.end[common[f.traj]]) for f in families]
    return _sweep([f.traj for f in kept], [f.start for f in kept], [f.end for f in kept], len(kept))


def union(families: list[Intervals]) -> Intervals:
    """Intervals where at least one family is down (families may overlap each other)."""
    families = [family.positive() for family in families if family.traj.size]
    if not families:
        return Intervals(np.zeros(0, np.int64), np.zeros(0), np.zeros(0))
    return _sweep([f.traj for f in families], [f.start for f in families], [f.end for f in families], 1)


@dataclass
class HazardResult:
    onsets: np.ndarray            # per device: number of hazard onsets in the horizon
    first_onset_h: np.ndarray     # per device: first onset time (inf if none)
    down_at_samples: np.ndarray   # (n_samples,) number of devices with the hazard present
    onset_histogram: np.ndarray   # onsets per time bin (all devices), for cumulative curves
    window_onsets: np.ndarray     # (n_windows, n_devices) onsets inside each requested time window

    def expected_onsets(self) -> tuple[float, float]:
        """Mean onsets per device and its standard error."""
        n = self.onsets.size
        return float(self.onsets.mean()), float(self.onsets.std(ddof=1) / np.sqrt(n)) if n > 1 else np.inf

    def probability_any(self) -> float:
        return float(np.mean(self.onsets > 0))

    def window_expected(self, window: int) -> tuple[float, float]:
        """Mean onsets per device inside one requested window, and its standard error."""
        counts = self.window_onsets[window]
        return float(counts.mean()), float(counts.std(ddof=1) / np.sqrt(counts.size))


@dataclass
class SimulationResult:
    n_traj: int
    horizon_h: float
    sample_times_h: np.ndarray
    onset_bins_h: np.ndarray
    hazards: dict[str, HazardResult]
    failure_counts: dict[str, np.ndarray]      # component name -> failures per device
    component_down_at_samples: dict[str, np.ndarray]
    hardware: dict[str, bool] = field(default_factory=dict)

    def expected_failures(self, name: str) -> tuple[float, float]:
        counts = self.failure_counts[name]
        return float(counts.mean()), float(counts.std(ddof=1) / np.sqrt(counts.size))

    def hardware_failures(self) -> np.ndarray:
        return sum(self.failure_counts[name] for name, is_hw in self.hardware.items() if is_hw)


def simulate(components, hazard_cut_sets: dict[str, list[frozenset[str]]], horizon_h: float, n_traj: int,
             seed: int, sample_times_h=(), chunk_size: int = 50_000, onset_bins_h=None,
             windows_h=()) -> SimulationResult:
    """Simulate n_traj devices over [0, horizon_h).

    components: component models with .name, .hardware and .sample(rng, n, horizon).
    hazard_cut_sets: hazard name -> minimal cut sets (sets of component names).
    windows_h: (start, end) pairs; onsets starting in [start, end) are also counted per device
    (for per-year rates, or to skip a start-up transient).
    Each (chunk, component) pair gets its own random stream from numpy's SeedSequence, so the run is
    reproducible for a given seed and chunk size.
    """
    if n_traj < 2:
        raise ValueError("need at least 2 simulated devices")
    if not horizon_h > 0.0 or not np.isfinite(horizon_h):
        raise ValueError("horizon must be positive and finite")
    names = [component.name for component in components]
    for hazard, cut_sets in hazard_cut_sets.items():
        unknown = sorted({name for cut_set in cut_sets for name in cut_set} - set(names))
        if unknown:
            raise ValueError(f"hazard {hazard}: cut sets use unknown components {unknown}")
    sample_times = np.asarray(sample_times_h, dtype=float)
    bins = np.linspace(0.0, horizon_h, 101) if onset_bins_h is None else np.asarray(onset_bins_h, dtype=float)
    histogram = {h: np.zeros(bins.size - 1, np.int64) for h in hazard_cut_sets}
    windows = [(float(a), float(b)) for a, b in windows_h]
    if any(not 0.0 <= a < b <= horizon_h for a, b in windows):
        raise ValueError("windows must satisfy 0 <= start < end <= horizon")
    in_window = {h: [] for h in hazard_cut_sets}
    n_chunks = int(np.ceil(n_traj / chunk_size))
    streams = np.random.SeedSequence(seed).spawn(n_chunks)

    onsets = {h: [] for h in hazard_cut_sets}
    first = {h: [] for h in hazard_cut_sets}
    hazard_down = {h: np.zeros(sample_times.size, np.int64) for h in hazard_cut_sets}
    counts = {name: [] for name in names}
    comp_down = {name: np.zeros(sample_times.size, np.int64) for name in names}

    for chunk, stream in enumerate(streams):
        n = min(chunk_size, n_traj - chunk * chunk_size)
        component_streams = stream.spawn(len(components))
        histories = {}
        for component, component_stream in zip(components, component_streams):
            history = component.sample(np.random.default_rng(component_stream), n, horizon_h)
            histories[component.name] = history
            counts[component.name].append(history.failure_counts(n))
            for i, t in enumerate(sample_times):
                comp_down[component.name][i] += int(history.down_at(t, n).sum())
        for hazard, cut_sets in hazard_cut_sets.items():
            present = union([intersect([histories[name] for name in sorted(cut_set)]) for cut_set in cut_sets])
            onsets[hazard].append(np.bincount(present.traj, minlength=n))
            first_onset = np.full(n, np.inf)
            np.minimum.at(first_onset, present.traj, present.start)
            first[hazard].append(first_onset)
            histogram[hazard] += np.histogram(present.start, bins)[0]
            in_window[hazard].append(np.array(
                [np.bincount(present.traj[(present.start >= a) & (present.start < b)], minlength=n) for a, b in windows],
                dtype=np.int32).reshape(len(windows), n))
            for i, t in enumerate(sample_times):
                hazard_down[hazard][i] += int(present.down_at(t, n).sum())

    hazards = {h: HazardResult(np.concatenate(onsets[h]), np.concatenate(first[h]), hazard_down[h], histogram[h],
                               np.concatenate(in_window[h], axis=1))
               for h in hazard_cut_sets}
    return SimulationResult(n_traj, horizon_h, sample_times, bins, hazards,
                            {name: np.concatenate(counts[name]) for name in names}, comp_down,
                            {component.name: bool(component.hardware) for component in components})

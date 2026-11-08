"""Real-time SpO2 and pulse-rate engine for two-wavelength PPG.

Signal chain, per frame of four ADC samples (red, red ambient, IR, IR ambient):

  1. Ambient cancellation      x_lam = counts_lam - ambient_lam
  2. Saturation / signal check  an LED or ambient sample at ADC full scale -> saturated frame;
                                x_red or x_ir below min_dc (probe off, LED light lost, LED sample at
                                0 counts) or raw counts flat over the last second -> no signal.
                                Both are decided per sample from the unfiltered values, so a lost
                                probe is seen at once rather than after the DC filter has decayed.
  3. DC and AC separation       DC_lam = H_dc * x_lam,  AC_lam = H_ac * x_lam   (filters.py)
                                Filters run only on usable samples; after any unusable sample they
                                restart from the next usable one (no transient from the gap).
  4. Normalisation              n_lam = -AC_lam / DC_lam
                                Transmitted light falls when the arterial blood volume rises, so the
                                sign flip makes the pulse positive; n_lam ~ dA_lam (absorbance change).
  5. Beat tracking (IR)         causal peak/valley detector with hysteresis h = k_h * A_ref. A_ref is
                                the median of recent accepted IR pulse amplitudes, or before any beat
                                is accepted the peak-to-peak range of n_ir over the last 2 s.
                                A peak is confirmed when n_ir falls h below the running maximum, a
                                valley when it rises h above the running minimum. The peak time is
                                refined between samples with a parabola through the three samples
                                around the maximum:  delta = (y[-1] - y[+1]) / (2 (y[-1] - 2 y[0] + y[+1])).
  6. Per-beat features          IR pulse amplitude from the tracked peak-to-peak of the systolic
                                upstroke:  AC/DC_ir = n_ir(peak) - n_ir(valley).
                                Ratio of ratios from the whole cycle (previous peak -> this peak):
                                both traces are detrended by a straight line, then red is fitted to
                                IR by least squares,
                                    R_beat = sum(r_k i_k) / sum(i_k^2),   AC/DC_red = R_beat AC/DC_ir.
                                Taking red at the two IR extremes would bias R: the extremes are
                                picked on the noisy IR trace, so IR noise is positive at the chosen
                                peak and negative at the chosen valley while red noise is not; the
                                fit uses every sample of the beat and has no such selection.
                                interval = t_peak(k) - t_peak(k - 1)
  7. Beat quality               rate within 30-220 bpm, IR perfusion index above the low-perfusion
                                limit, R physically possible, no saturated sample, and red
                                proportional to IR over the cycle: an arterial pulse is one absorber
                                seen at two wavelengths, so n_red(t) = R n_ir(t) and their correlation
                                (after removing a straight-line trend) is ~1; motion adds a second
                                source with its own ratio and the correlation drops. Once seeded, the
                                amplitude within 0.5-2x and the interval within +/-30 % of the recent
                                medians, R within +/-(0.10 + 10 %) of the recent median R (about
                                what a 1 %/s desaturation moves R in the averaging lag), and the
                                cycle shape correlated with the running template.
                                Seeding needs 3 consecutive beats that agree in amplitude, interval
                                and R, and no rejected beat (nor 3 disagreeing beats) in the 5 s
                                before, so the tracker does not lock onto a run of motion.
  8. Output once per second     R = median of the last 8 accepted beats inside the last 10 s,
                                SpO2 = calibration(R) (calibration.py), pulse rate = 60 / median
                                interval. Nothing is shown with fewer than 4 accepted beats in the
                                last 10 s, with any saturated / no-signal sample in the last second,
                                or when the window's beat R values scatter by more than 4 % (rms,
                                relative) around a straight-line trend: a real desaturation moves R
                                smoothly, motion that slipped into accepted beats does not.
                                Whenever the beat statistics are relearned (after a saturated or
                                no-signal sample, after 6 rejected beats in a row, or after 4 s
                                without an accepted beat) a new epoch starts: beats from before it
                                are never shown again (the probe may now be on another patient).

Every output carries a Status bit field; a number is never shown without the conditions that apply.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from enum import IntFlag

import numpy as np

from .calibration import CalFlag, LinearRatioCalibration
from .filters import AC_BAND_HZ, DC_CUTOFF_HZ, StreamingSos, design_ac_bandpass, design_dc_lowpass
from .synth import ADC_FULL_SCALE_COUNTS


class Status(IntFlag):
    OK = 0
    SATURATED = 1  # an ADC sample at full scale in the last second
    NO_SIGNAL = 2  # LED light below the minimum (probe off, LED sample at 0) or counts flat (ADC stuck)
    LOW_PERFUSION = 4  # pulse amplitude below the low-perfusion limit
    MOTION = 8  # beats rejected by the amplitude / interval / shape checks
    STALE = 16  # no accepted beat in the hold window: outputs blanked
    LEARNING = 32  # beat statistics not seeded yet
    CLIPPED_HIGH = 64  # calibrated value above 100 % shown as 100 %
    BELOW_CAL_RANGE = 128  # value below the validated range (extrapolated)
    R_OUT_OF_RANGE = 256  # R maps outside any plausible SpO2: value blanked
    LOW_QUALITY = 512  # beat-to-beat R too erratic around its trend: value withheld


_CAL_TO_STATUS = {
    CalFlag.CLIPPED_HIGH: Status.CLIPPED_HIGH,
    CalFlag.BELOW_CAL_RANGE: Status.BELOW_CAL_RANGE,
    CalFlag.R_OUT_OF_RANGE: Status.R_OUT_OF_RANGE,
}


@dataclass(frozen=True)
class EngineConfig:
    dc_cutoff_hz: float = DC_CUTOFF_HZ
    ac_band_hz: tuple = AC_BAND_HZ
    min_rate_bpm: float = 30.0
    max_rate_bpm: float = 220.0
    min_perfusion_index: float = 0.0005  # 0.05 % AC/DC on IR
    min_dc_fraction_of_full_scale: float = 0.005
    flat_counts_std: float = 2.0  # std of raw counts over one second below this = flat line
    ratio_r_limits: tuple = (0.2, 4.0)  # R outside this cannot come from blood (eq. 3, any S)
    hysteresis_fraction: float = 0.3
    initial_amplitude_guess: float = 0.002  # AC/DC used for hysteresis before any beat is seen
    amplitude_tolerance: tuple = (0.5, 2.0)
    interval_tolerance_frac: float = 0.3
    template_points: int = 32
    template_min_correlation: float = 0.8
    # Accepted |R - median R| <= abs + rel * median R. R moves about 0.045 per 1 % SpO2, and the median
    # of the last 8 beats lags about 3 s, so a 1 %/s desaturation moves R by ~0.1 against it: the band
    # follows real changes up to about that speed and treats faster jumps as artifact (main.py
    # measures what this costs at 2 %/s).
    ratio_jump_abs: float = 0.10
    ratio_jump_rel: float = 0.10
    min_red_ir_correlation: float = 0.95
    seed_ratio_tolerance_frac: float = 0.15
    seed_window_s: float = 2.0
    seed_quiet_s: float = 5.0  # no seeding until this long after the last rejected beat
    unseed_after_s: float = 4.0  # no accepted beat for this long: statistics are stale, learn again
    template_update_weight: float = 0.2
    seed_beats: int = 3
    reseed_after_rejections: int = 6
    average_beats: int = 8
    min_beats_for_output: int = 4  # fewer accepted beats in the hold window: show nothing
    # Signal quality: rms scatter of the window's beat R values around a straight-line trend in time,
    # relative to their median. A real desaturation moves R smoothly (scatter stays low); motion that
    # slips into accepted beats makes R erratic. Above this, the value is withheld.
    max_ratio_scatter: float = 0.04
    history_beats: int = 8
    hold_s: float = 10.0
    output_period_s: float = 1.0
    motion_flag_window_s: float = 5.0


@dataclass
class BeatRecord:
    valley_index: int
    peak_index: int
    peak_time_s: float
    interval_s: float
    ac_dc_red: float
    ac_dc_ir: float
    ratio_r: float
    accepted: bool
    reason: str  # "ok" or the check that rejected the beat


@dataclass
class EngineResult:
    output_time_s: np.ndarray
    spo2_pct: np.ndarray
    pulse_rate_bpm: np.ndarray
    ratio_r: np.ndarray
    perfusion_index_pct: np.ndarray
    status: np.ndarray  # Status values as int
    beats: list
    normalised_red: np.ndarray
    normalised_ir: np.ndarray
    dc_red_counts: np.ndarray
    dc_ir_counts: np.ndarray

    def accepted_beats(self):
        return [b for b in self.beats if b.accepted]


# Rejections that come from the signal changing shape (motion), counted for re-seeding.
RESEED_REASONS = ("amplitude", "interval", "ratio_jump", "shape", "proportionality")
# Rejections reported to the user as motion.
MOTION_REASONS = RESEED_REASONS + ("rate",)


class PpgSpo2Engine:
    def __init__(self, fs_hz, calibration=None, adc_full_scale_counts=ADC_FULL_SCALE_COUNTS, config=None):
        self.config = config or EngineConfig()
        self.fs_hz = float(fs_hz)
        self.calibration = calibration or LinearRatioCalibration()
        self.adc_full_scale_counts = float(adc_full_scale_counts)
        self._dc_red = StreamingSos(design_dc_lowpass(self.fs_hz, self.config.dc_cutoff_hz))
        self._dc_ir = StreamingSos(design_dc_lowpass(self.fs_hz, self.config.dc_cutoff_hz))
        self._ac_red = StreamingSos(design_ac_bandpass(self.fs_hz, self.config.ac_band_hz))
        self._ac_ir = StreamingSos(design_ac_bandpass(self.fs_hz, self.config.ac_band_hz))
        self._samples_per_output = int(round(self.config.output_period_s * self.fs_hz))
        self._filters_need_reset = False
        self.reset_tracking()
        self._n_red, self._n_ir, self._dc_r, self._dc_i = [], [], [], []
        self._saturated, self._no_signal = [], []
        self._raw_red_tail = np.zeros(0)
        self._raw_ir_tail = np.zeros(0)
        self._sample_count = 0
        self._outputs = []
        self.beats = []

    # ----- beat tracker state -------------------------------------------------------------
    def reset_tracking(self):
        self._seeking_peak = False
        self._extreme_value = np.inf
        self._extreme_index = -1
        self._last_valley_index = -1
        self._last_peak_index = -1
        self._accepted_amplitudes = []
        self._accepted_intervals = []
        self._candidates = []  # (amplitude, interval, R) while not seeded
        self._last_peak_time_s = float("nan")
        self._seeded = False
        self._consecutive_rejections = 0
        self._template = None
        self._accepted_peak_times = []
        self._last_rejection_time_s = -np.inf
        self._accepted_ratios = []
        self._valid_since_s = -np.inf
        self._median_cache = (-1, float("nan"), None)  # (list length, median, list identity)

    def _forget_statistics(self, now_s):
        """Drop the learned beat statistics and start a new epoch at now_s: only beats accepted
        after it may be shown."""
        self._valid_since_s = now_s
        self._seeded = False
        self._accepted_amplitudes, self._accepted_intervals = [], []
        self._accepted_peak_times, self._accepted_ratios = [], []
        self._candidates, self._template = [], None
        self._consecutive_rejections = 0

    # ----- signal chain steps (separate methods so each can be tested on its own) ------------
    def cancel_ambient(self, led_counts, ambient_counts):
        """Step 1: x = LED sample - following ambient sample."""
        return led_counts - ambient_counts

    def normalise(self, ac_counts, dc_counts, valid):
        """Step 4: n = -AC / DC where DC is valid, NaN elsewhere (never divides by ~0)."""
        out = np.full(ac_counts.shape, np.nan)
        out[valid] = -ac_counts[valid] / dc_counts[valid]
        return out

    def saturated_frames(self, red, red_amb, ir, ir_amb):
        """Step 2: a frame is saturated when any of its four samples sits at ADC full scale.

        The low rail is not saturation: an ambient sample of 0 is a dark room, and an LED sample of
        0 means no light reached the detector, which the low-light test reports as NO_SIGNAL."""
        rail_high = self.adc_full_scale_counts
        frames = np.zeros(red.shape, dtype=bool)
        for counts in (red, red_amb, ir, ir_amb):
            frames |= counts >= rail_high
        return frames

    def flat_samples(self, red, ir):
        """Step 2: True where the raw LED counts of either channel have a standard deviation below
        flat_counts_std over the second ending at that sample (ADC or front end stuck)."""
        window = int(round(self.fs_hz))
        red_hist = np.concatenate([self._raw_red_tail, red])
        ir_hist = np.concatenate([self._raw_ir_tail, ir])
        flat = np.zeros(red.shape, dtype=bool)
        offset = red_hist.size - red.size  # index of the block's first sample in the history
        if red_hist.size >= window:
            red_std = np.lib.stride_tricks.sliding_window_view(red_hist, window).std(axis=1)
            ir_std = np.lib.stride_tricks.sliding_window_view(ir_hist, window).std(axis=1)
            # Window j ends at history index j + window - 1; the block sample i is index offset + i.
            first = max(0, window - 1 - offset)
            ends = np.arange(first, red.size) + offset - (window - 1)
            flat[first:] = np.minimum(red_std[ends], ir_std[ends]) < self.config.flat_counts_std
        self._raw_red_tail = red_hist[-(window - 1):] if window > 1 else red_hist[:0]
        self._raw_ir_tail = ir_hist[-(window - 1):] if window > 1 else ir_hist[:0]
        return flat

    # ----- streaming entry points ----------------------------------------------------------
    def process(self, recording, block_s=1.0):
        """Run a whole PpgRecording through the engine in blocks of block_s."""
        if not np.isclose(recording.fs_hz, self.fs_hz):
            raise ValueError(
                f"sample-rate mismatch: recording {recording.fs_hz} Hz, engine configured for {self.fs_hz} Hz"
            )
        if recording.adc_full_scale_counts != self.adc_full_scale_counts:
            raise ValueError("ADC full scale of the recording differs from the engine's")
        block = max(1, int(round(block_s * self.fs_hz)))
        for start in range(0, recording.red_counts.size, block):
            stop = start + block
            self.process_block(
                recording.red_counts[start:stop],
                recording.red_ambient_counts[start:stop],
                recording.ir_counts[start:stop],
                recording.ir_ambient_counts[start:stop],
            )
        return self.result()

    def process_block(self, red_counts, red_ambient_counts, ir_counts, ir_ambient_counts):
        arrays = [np.asarray(a, dtype=float) for a in (red_counts, red_ambient_counts, ir_counts, ir_ambient_counts)]
        if len({a.shape for a in arrays}) != 1 or arrays[0].ndim != 1:
            raise ValueError("the four sample streams must be 1-D arrays of equal length")
        if not all(np.all(np.isfinite(a)) for a in arrays):
            raise ValueError("samples must be finite numbers")
        red, red_amb, ir, ir_amb = arrays
        n = red.size
        if n == 0:
            return

        saturated = self.saturated_frames(red, red_amb, ir, ir_amb)
        x_red = self.cancel_ambient(red, red_amb)
        x_ir = self.cancel_ambient(ir, ir_amb)
        min_dc = self.config.min_dc_fraction_of_full_scale * self.adc_full_scale_counts
        low_light = (x_red <= min_dc) | (x_ir <= min_dc)
        flat = self.flat_samples(red, ir)
        unusable = saturated | low_light | flat

        dc_red, dc_ir, ac_red, ac_ir = (np.full(n, np.nan) for _ in range(4))
        filters = (self._dc_red, self._dc_ir, self._ac_red, self._ac_ir)
        usable_index = np.flatnonzero(~unusable)
        if usable_index.size:
            # Contiguous runs of usable samples; the filters restart at the start of a run that
            # follows an unusable sample.
            breaks = np.flatnonzero(np.diff(usable_index) > 1)
            starts = np.concatenate([[usable_index[0]], usable_index[breaks + 1]])
            stops = np.concatenate([usable_index[breaks] + 1, [usable_index[-1] + 1]])
            for start, stop in zip(starts, stops):
                if self._filters_need_reset or start > 0:
                    for f in filters:
                        f.reset()
                self._filters_need_reset = False
                dc_red[start:stop] = self._dc_red.process(x_red[start:stop])
                dc_ir[start:stop] = self._dc_ir.process(x_ir[start:stop])
                ac_red[start:stop] = self._ac_red.process(x_red[start:stop])
                ac_ir[start:stop] = self._ac_ir.process(x_ir[start:stop])
        if unusable[-1]:
            self._filters_need_reset = True

        with np.errstate(invalid="ignore"):
            dc_ok = (dc_red > min_dc) & (dc_ir > min_dc)
        valid = ~unusable & dc_ok
        no_signal = low_light | flat | (~unusable & ~dc_ok)

        n_red = self.normalise(ac_red, dc_red, valid)
        n_ir = self.normalise(ac_ir, dc_ir, valid)
        base = self._sample_count
        self._n_red.extend(n_red)
        self._n_ir.extend(n_ir)
        self._dc_r.extend(dc_red)
        self._dc_i.extend(dc_ir)
        self._saturated.extend(saturated)
        self._no_signal.extend(no_signal)

        self._block_base = base
        self._block_range = self._rolling_range(base, n)
        for i in range(n):
            k = base + i
            self._track_sample(k)
            self._sample_count = k + 1
            if self._sample_count % self._samples_per_output == 0:
                self._emit_output(k)

    # ----- beat detection --------------------------------------------------------------------
    def _amplitude_reference(self, k):
        if self._seeded and self._accepted_amplitudes:
            count = len(self._accepted_amplitudes)
            if self._median_cache[0] != count or self._median_cache[2] is not self._accepted_amplitudes:
                median = float(np.median(self._accepted_amplitudes[-self.config.history_beats:]))
                self._median_cache = (count, median, self._accepted_amplitudes)
            return self._median_cache[1]
        recent_range = self._block_range[k - self._block_base]
        if not np.isfinite(recent_range):
            return self.config.initial_amplitude_guess
        return max(float(recent_range), 1e-6)

    def _rolling_range(self, base, n):
        """Peak-to-peak of n_ir over the seed window ending at each sample of the block (NaN-aware)."""
        window = int(self.config.seed_window_s * self.fs_hz)
        start = max(0, base + 1 - window)
        history = np.asarray(self._n_ir[start:base + n], dtype=float)
        padded = np.concatenate([np.full(window - (base + 1 - start), np.nan), history])
        views = np.lib.stride_tricks.sliding_window_view(padded, window)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            ranges = np.nanmax(views, axis=1) - np.nanmin(views, axis=1)
            counts = np.sum(np.isfinite(views), axis=1)
        ranges[counts < 2] = np.nan
        return ranges

    def _track_sample(self, k):
        value = self._n_ir[k]
        if not np.isfinite(value) or self._no_signal[k] or self._saturated[k]:
            # No usable light: restart the detector so no beat spans the gap, forget the beat
            # statistics, and start a new epoch: beats from before the gap are never shown again.
            self._forget_statistics((k + 1) / self.fs_hz)
            self._seeking_peak = False
            self._extreme_value = np.inf
            self._extreme_index = -1
            self._last_valley_index = -1
            self._last_peak_index = -1
            self._last_peak_time_s = float("nan")
            return
        if self._seeded and self._accepted_peak_times and (
            k / self.fs_hz - self._accepted_peak_times[-1] > self.config.unseed_after_s
        ):
            # The learned amplitude may be far too large for the present pulse (e.g. it was learned
            # on motion, or perfusion fell), which would hide every real beat. Learn again.
            self._forget_statistics(k / self.fs_hz)
        hysteresis = self.config.hysteresis_fraction * self._amplitude_reference(k)
        if self._seeking_peak:
            if value > self._extreme_value:
                self._extreme_value, self._extreme_index = value, k
            elif value < self._extreme_value - hysteresis:
                peak_index = self._extreme_index
                if self._last_valley_index >= 0:
                    self._on_beat(self._last_valley_index, peak_index)
                self._seeking_peak = False
                self._extreme_value, self._extreme_index = value, k
        else:
            if value < self._extreme_value:
                self._extreme_value, self._extreme_index = value, k
            elif value > self._extreme_value + hysteresis:
                self._last_valley_index = self._extreme_index
                self._seeking_peak = True
                self._extreme_value, self._extreme_index = value, k

    def _cycle_shape(self, start_index, stop_index):
        """IR cycle from the previous peak to this one, resampled and z-scored (for the template)."""
        segment = np.asarray(self._n_ir[start_index:stop_index + 1], dtype=float)
        if segment.size < 4 or not np.all(np.isfinite(segment)):
            return None
        grid = np.linspace(0, segment.size - 1, self.config.template_points)
        shape = np.interp(grid, np.arange(segment.size), segment)
        spread = shape.std()
        if spread == 0:
            return None
        return (shape - shape.mean()) / spread

    def _refined_peak_time_s(self, peak_index):
        """Sub-sample peak time from a parabola through the samples around the maximum."""
        if peak_index < 1 or peak_index + 1 >= len(self._n_ir):
            return peak_index / self.fs_hz
        y_prev, y_peak, y_next = self._n_ir[peak_index - 1], self._n_ir[peak_index], self._n_ir[peak_index + 1]
        curvature = y_prev - 2 * y_peak + y_next
        if not np.isfinite(curvature) or curvature >= 0:
            return peak_index / self.fs_hz
        delta = 0.5 * (y_prev - y_next) / curvature
        return (peak_index + float(np.clip(delta, -0.5, 0.5))) / self.fs_hz

    def _red_ir_fit(self, start_index, stop_index):
        """Least-squares fit of n_red to n_ir over a cycle, both detrended by a straight line.

        Returns (correlation, slope): Pearson correlation sum(r i) / sqrt(sum(r^2) sum(i^2)) and
        the ratio of ratios sum(r i) / sum(i^2). NaN when the cycle is too short or not finite.
        """
        red = np.asarray(self._n_red[start_index:stop_index + 1], dtype=float)
        ir = np.asarray(self._n_ir[start_index:stop_index + 1], dtype=float)
        if red.size < 4 or not (np.all(np.isfinite(red)) and np.all(np.isfinite(ir))):
            return float("nan"), float("nan")
        t = np.arange(red.size)
        red = red - np.polyval(np.polyfit(t, red, 1), t)
        ir = ir - np.polyval(np.polyfit(t, ir, 1), t)
        ir_power, red_power, cross = float(np.dot(ir, ir)), float(np.dot(red, red)), float(np.dot(red, ir))
        if ir_power <= 0 or red_power <= 0:
            return float("nan"), float("nan")
        return cross / np.sqrt(ir_power * red_power), cross / ir_power

    def _on_beat(self, valley_index, peak_index):
        cfg = self.config
        previous_peak = self._last_peak_index
        previous_peak_time_s = self._last_peak_time_s
        peak_time_s = self._refined_peak_time_s(peak_index)
        self._last_peak_index = peak_index
        self._last_peak_time_s = peak_time_s
        ac_dc_ir = float(self._n_ir[peak_index] - self._n_ir[valley_index])
        if previous_peak >= 0:
            red_ir_corr, ratio_r = self._red_ir_fit(previous_peak, peak_index)
            interval_s = peak_time_s - previous_peak_time_s
            shape = self._cycle_shape(previous_peak, peak_index)
        else:
            red_ir_corr = ratio_r = interval_s = float("nan")
            shape = None
        ac_dc_red = ratio_r * ac_dc_ir
        window_start = previous_peak if previous_peak >= 0 else valley_index
        saturated = any(self._saturated[window_start:peak_index + 1])

        reason = self._quality(interval_s, ac_dc_ir, ratio_r, saturated, shape, red_ir_corr, peak_time_s)
        accepted = reason == "ok"
        if accepted:
            self._accepted_amplitudes.append(ac_dc_ir)
            self._accepted_intervals.append(interval_s)
            self._accepted_peak_times.append(peak_time_s)
            self._accepted_ratios.append(ratio_r)
            self._consecutive_rejections = 0
            if shape is not None:
                self._template = shape if self._template is None else (
                    (1 - cfg.template_update_weight) * self._template + cfg.template_update_weight * shape
                )
        else:
            if not self._seeded and reason != "learning":
                # Seeding needs consecutive good beats: any rejected beat starts the count again.
                self._candidates = []
            if reason in RESEED_REASONS:
                self._consecutive_rejections += 1
                if self._consecutive_rejections >= cfg.reseed_after_rejections:
                    # Statistics no longer describe the signal (rate or perfusion really changed):
                    # forget them and learn again from the next consistent beats.
                    self._forget_statistics(peak_time_s)
        self.beats.append(BeatRecord(
            valley_index, peak_index, peak_time_s, interval_s,
            ac_dc_red, ac_dc_ir, ratio_r, accepted, reason,
        ))

    def _quality(self, interval_s, ac_dc_ir, ratio_r, saturated, shape, red_ir_corr, peak_time_s):
        reason = self._check_beat(interval_s, ac_dc_ir, ratio_r, saturated, shape, red_ir_corr, peak_time_s)
        if reason not in ("ok", "learning", "first_beat"):
            self._last_rejection_time_s = peak_time_s
        return reason

    def _check_beat(self, interval_s, ac_dc_ir, ratio_r, saturated, shape, red_ir_corr, peak_time_s):
        cfg = self.config
        if saturated:
            return "saturated"
        if not np.isfinite(interval_s):
            return "first_beat"
        if not 60.0 / cfg.max_rate_bpm <= interval_s <= 60.0 / cfg.min_rate_bpm:
            return "rate"
        if ac_dc_ir < cfg.min_perfusion_index:
            return "low_perfusion"
        if not (np.isfinite(ratio_r) and cfg.ratio_r_limits[0] <= ratio_r <= cfg.ratio_r_limits[1]):
            return "ratio_range"
        if not (np.isfinite(red_ir_corr) and red_ir_corr >= cfg.min_red_ir_correlation):
            return "proportionality"
        if not self._seeded:
            self._candidates.append((ac_dc_ir, interval_s, ratio_r))
            recent = self._candidates[-cfg.seed_beats:]
            if len(recent) < cfg.seed_beats:
                return "learning"
            amplitudes = np.array([c[0] for c in recent])
            intervals = np.array([c[1] for c in recent])
            ratios = np.array([c[2] for c in recent])
            consistent = (
                np.all(np.abs(amplitudes / np.median(amplitudes) - 1) <= 0.5)
                and np.all(np.abs(intervals / np.median(intervals) - 1) <= 0.25)
                and np.all(np.abs(ratios / np.median(ratios) - 1) <= cfg.seed_ratio_tolerance_frac)
            )
            if not consistent:
                # Three beats in a row that disagree are a sign of motion too: restart the quiet time.
                self._last_rejection_time_s = peak_time_s
                return "learning"
            if peak_time_s - self._last_rejection_time_s < cfg.seed_quiet_s:
                return "learning"
            self._seeded = True
            self._accepted_amplitudes = list(amplitudes[:-1])
            self._accepted_intervals = list(intervals[:-1])
            self._accepted_ratios = list(ratios[:-1])
            self._candidates = []
            return "ok"
        amplitude_ratio = ac_dc_ir / np.median(self._accepted_amplitudes[-cfg.history_beats:])
        if not cfg.amplitude_tolerance[0] <= amplitude_ratio <= cfg.amplitude_tolerance[1]:
            return "amplitude"
        interval_ratio = interval_s / np.median(self._accepted_intervals[-cfg.history_beats:])
        if abs(interval_ratio - 1) > cfg.interval_tolerance_frac:
            return "interval"
        median_ratio = float(np.median(self._accepted_ratios[-cfg.history_beats:]))
        if abs(ratio_r - median_ratio) > cfg.ratio_jump_abs + cfg.ratio_jump_rel * median_ratio:
            return "ratio_jump"
        if self._template is not None and shape is not None:
            correlation = float(np.mean(shape * self._template) / max(self._template.std(), 1e-12))
            if correlation < cfg.template_min_correlation:
                return "shape"
        return "ok"

    # ----- 1 Hz output -----------------------------------------------------------------------
    def _emit_output(self, k):
        cfg = self.config
        now_s = (k + 1) / self.fs_hz
        last_second = slice(max(0, k + 1 - self._samples_per_output), k + 1)
        status = Status.OK
        saturated_now = any(self._saturated[last_second])
        no_signal_now = any(self._no_signal[last_second])
        if saturated_now:
            status |= Status.SATURATED
        if no_signal_now:
            status |= Status.NO_SIGNAL
        if not self._seeded:
            status |= Status.LEARNING
        recent = [b for b in self.beats if b.peak_time_s >= now_s - cfg.motion_flag_window_s]
        if any(b.reason in MOTION_REASONS for b in recent):
            status |= Status.MOTION
        pulse_window = np.asarray(self._n_ir[max(0, k + 1 - int(cfg.seed_window_s * self.fs_hz)):k + 1])
        pulse_window = pulse_window[np.isfinite(pulse_window)]
        if any(b.reason == "low_perfusion" for b in recent) or (
            pulse_window.size and np.ptp(pulse_window) < cfg.min_perfusion_index
        ):
            status |= Status.LOW_PERFUSION

        window = [
            b for b in self.beats
            if b.accepted and b.peak_time_s >= now_s - cfg.hold_s and b.peak_time_s > self._valid_since_s
        ]
        window = window[-cfg.average_beats:]
        spo2 = rate = ratio = pi_pct = float("nan")
        if saturated_now or no_signal_now:
            pass  # no usable light or ADC at full scale in the last second: show nothing
        elif len(window) < cfg.min_beats_for_output:
            status |= Status.STALE
        else:
            ratios = np.array([b.ratio_r for b in window])
            times = np.array([b.peak_time_s for b in window])
            # Least-squares straight line R(t) = mean R + slope (t - mean t), in closed form so a
            # window of one beat (or equal times) gives slope 0 instead of a singular fit.
            dt = times - times.mean()
            spread = float(np.dot(dt, dt))
            slope = float(np.dot(dt, ratios - ratios.mean()) / spread) if spread > 0 else 0.0
            trend = ratios.mean() + slope * dt
            scatter = float(np.sqrt(np.mean((ratios - trend) ** 2)) / np.median(ratios))
            if scatter > cfg.max_ratio_scatter:
                status |= Status.LOW_QUALITY
                self._outputs.append((now_s, spo2, rate, ratio, pi_pct, int(status)))
                return
            ratio = float(np.median(ratios))
            intervals = [b.interval_s for b in window if np.isfinite(b.interval_s)]
            rate = 60.0 / float(np.median(intervals)) if intervals else float("nan")
            pi_pct = 100.0 * float(np.median([b.ac_dc_ir for b in window]))
            spo2, cal_flag = self.calibration.spo2_pct(ratio)
            for flag, status_bit in _CAL_TO_STATUS.items():
                if cal_flag & flag:
                    status |= status_bit
        self._outputs.append((now_s, spo2, rate, ratio, pi_pct, int(status)))

    def result(self):
        outputs = np.array(self._outputs, dtype=float).reshape(-1, 6)
        return EngineResult(
            output_time_s=outputs[:, 0],
            spo2_pct=outputs[:, 1],
            pulse_rate_bpm=outputs[:, 2],
            ratio_r=outputs[:, 3],
            perfusion_index_pct=outputs[:, 4],
            status=outputs[:, 5].astype(int),
            beats=list(self.beats),
            normalised_red=np.asarray(self._n_red),
            normalised_ir=np.asarray(self._n_ir),
            dc_red_counts=np.asarray(self._dc_r),
            dc_ir_counts=np.asarray(self._dc_i),
        )

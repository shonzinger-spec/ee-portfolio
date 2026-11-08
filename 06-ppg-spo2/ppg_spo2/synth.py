"""Synthetic two-wavelength PPG recordings with a known ground truth.

The model follows a multiplexed analog front end of the AFE4490 kind: every frame (1 / fs) the ADC
takes four samples in turn, each in its own quarter of the frame,

    red LED on (offset 0)   ambient (T/4)   IR LED on (T/2)   ambient (3T/4)

and the firmware subtracts the ambient sample that follows each LED sample. Light that does not come
from the LED (room light, sunlight) adds to every sample; anything that changes between an LED sample
and its ambient sample leaks through the subtraction. Mains-powered lighting flickers at twice the
mains frequency (120 Hz in North America), so the leak is a 120 Hz tone and its harmonics (240,
360 Hz, ...) sampled at fs, which alias to |f - k fs|: 20 Hz and 40 Hz at fs = 100 Hz. With 50 Hz
mains the flicker is at 100 Hz and multiples, which fs = 100 Hz samples at the same phase in every
frame: the leak is then a constant offset, not a tone, and cannot be filtered out.

Detected counts for wavelength lam (photodiode -> transimpedance amplifier -> ADC):

    counts_lam(t) = G_lam * coupling(t) * exp(-A_lam(t)) + ambient(t + offset_lam) + noise    (4)

    A_lam(t) = A_static_lam * (1 + w_resp sin(2 pi f_resp t))            static tissue + venous
             + PI_ir * r_lam(S(t)) * p(phase(t))                          arterial pulse
             + motion_absorbance_ir(t) * r_lam(S_venous(t))               venous blood moved by motion

r_ir = 1, r_red = R(S) from equation (3) in optics.py, PI_ir is the IR perfusion index (pulse
amplitude AC/DC in the IR channel) and p(phase) is a unit-height pulse shape. coupling(t) is the
optical coupling of the probe, 1 at rest; motion changes it equally at both wavelengths.

Counts are clipped to the ADC range [0, full scale], which is how saturation appears.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from .optics import NOMINAL_DPF_RATIO_RED_IR, ratio_of_ratios_from_saturation

# 22-bit two's-complement ADC (AFE4490-like): codes -2^21 .. 2^21 - 1. Photocurrent is positive, so
# the usable range is 0 .. 2^21 - 1 and the positive full scale is 2^21 - 1 = 2097151 counts.
ADC_FULL_SCALE_COUNTS = 2**21 - 1


@dataclass
class SubjectOptics:
    """Optical parameters of one simulated subject and probe placement."""

    dpf_ratio_red_ir: float = NOMINAL_DPF_RATIO_RED_IR
    perfusion_index_ir: object = 0.02  # pulsatile AC/DC in the IR channel (2 %); float or f(t)
    static_absorbance_red: float = 2.2
    static_absorbance_ir: float = 1.6
    led_gain_red_counts: float = 8.0e6  # counts for an unattenuated beam; DC lands near mid-scale
    led_gain_ir_counts: float = 4.5e6
    respiratory_rate_hz: float = 0.25
    respiratory_absorbance_frac: float = 0.004  # breathing modulates the static absorbance by 0.4 %
    venous_desaturation_frac: float = 0.25  # venous SvO2 = SaO2 - 0.25


@dataclass
class AmbientLight:
    """Light reaching the photodiode that does not come from the LEDs."""

    dc_counts: float = 0.0
    flicker_counts: float = 0.0  # peak of the full-wave rectified mains component
    mains_frequency_hz: float = 60.0
    step_time_s: float | None = None  # room light switched on
    step_counts: float = 0.0


@dataclass
class MotionEpisode:
    """A burst of probe motion between start_s and stop_s."""

    start_s: float
    stop_s: float
    venous_absorbance_rms: float = 0.05  # rms of the moved venous absorbance (IR)
    coupling_rms: float = 0.03  # rms fractional change of optical coupling
    band_hz: tuple = (0.6, 3.5)  # motion overlaps the pulse band


@dataclass
class PpgRecording:
    """Raw ADC samples plus the ground truth they were generated from."""

    fs_hz: float
    time_s: np.ndarray
    red_counts: np.ndarray
    red_ambient_counts: np.ndarray
    ir_counts: np.ndarray
    ir_ambient_counts: np.ndarray
    sao2_pct: np.ndarray  # reference arterial saturation per frame
    pulse_rate_bpm: np.ndarray  # reference instantaneous pulse rate per frame
    beat_onset_s: np.ndarray  # times at which each beat starts (phase crosses an integer)
    motion_mask: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    adc_full_scale_counts: int = ADC_FULL_SCALE_COUNTS


def _two_gaussian_wave(x):
    """Systolic wave at 0.18 of the beat plus reflected (dicrotic) wave at 0.45, wrapped periodic.

        w(x) = exp(-(x - 0.18)^2 / (2 * 0.065^2)) + 0.38 exp(-(x - 0.45)^2 / (2 * 0.10^2))
    """
    total = np.zeros_like(x)
    for offset in (-1.0, 0.0, 1.0):
        total += np.exp(-((x + offset - 0.18) ** 2) / (2 * 0.065**2))
        total += 0.38 * np.exp(-((x + offset - 0.45) ** 2) / (2 * 0.10**2))
    return total


_SHAPE_GRID = _two_gaussian_wave(np.linspace(0.0, 1.0, 2001))
_SHAPE_MIN, _SHAPE_MAX = float(_SHAPE_GRID.min()), float(_SHAPE_GRID.max())


def pulse_shape(phase_frac):
    """Unit-height arterial pulse over one beat: p = (w - min w) / (max w - min w), phase mod 1."""
    x = np.asarray(phase_frac, dtype=float) % 1.0
    return (_two_gaussian_wave(x) - _SHAPE_MIN) / (_SHAPE_MAX - _SHAPE_MIN)


def _band_limited_noise(rng, n_samples, fs_hz, band_hz, rms):
    """Gaussian noise filtered to band_hz (4th-order Butterworth band-pass), scaled to rms."""
    settle = int(4 * fs_hz)
    white = rng.standard_normal(n_samples + settle)
    sos = signal.butter(2, band_hz, btype="bandpass", fs=fs_hz, output="sos")
    shaped = signal.sosfilt(sos, white)[settle:]
    return shaped * (rms / max(float(np.std(shaped)), 1e-12))


def _as_profile(value, time_s):
    out = value(time_s) if callable(value) else value
    return np.broadcast_to(np.asarray(out, dtype=float), time_s.shape).copy()


def generate_recording(
    duration_s,
    fs_hz=100.0,
    sao2_pct=97.0,
    pulse_rate_bpm=72.0,
    subject=None,
    ambient=None,
    motion=(),
    adc_noise_counts=40.0,
    beat_interval_jitter_frac=0.03,
    sensor_off_after_s=None,
    seed=0,
):
    """Generate one recording.

    sao2_pct and pulse_rate_bpm are constants or callables of time (s). The beat phase is
    phase(t) = integral of rate(t) / 60 dt, with each beat's interval scaled by its own factor
    (1 + jitter * N(0, 1)) drawn when the beat starts (heart-rate variability). sensor_off_after_s
    removes the LED light from that time on (probe off the finger), leaving ambient light and noise.
    """
    if duration_s <= 0 or fs_hz <= 0:
        raise ValueError("duration and sample rate must be positive")
    subject = subject or SubjectOptics()
    ambient = ambient or AmbientLight()
    rng = np.random.default_rng(seed)
    n_samples = int(round(duration_s * fs_hz))
    frame_period_s = 1.0 / fs_hz
    time_s = np.arange(n_samples) * frame_period_s

    sao2_ref_pct = _as_profile(sao2_pct, time_s)
    if np.any((sao2_ref_pct < 0) | (sao2_ref_pct > 100)):
        raise ValueError("SaO2 must be within 0-100 %")
    rate_nominal_bpm = _as_profile(pulse_rate_bpm, time_s)
    if np.any(rate_nominal_bpm <= 0):
        raise ValueError("pulse rate must be positive")

    phase = np.zeros(n_samples)
    rate_ref_bpm = np.zeros(n_samples)
    beat_scale = 1.0 + beat_interval_jitter_frac * rng.standard_normal()
    beat_onsets = [0.0]
    for k in range(n_samples):
        rate_ref_bpm[k] = rate_nominal_bpm[k] / beat_scale
        if k == 0:
            continue
        phase[k] = phase[k - 1] + rate_ref_bpm[k - 1] / 60.0 * frame_period_s
        if np.floor(phase[k]) > np.floor(phase[k - 1]):
            # Crossing time by linear interpolation inside the frame.
            frac = (np.floor(phase[k]) - phase[k - 1]) / (phase[k] - phase[k - 1])
            beat_onsets.append(time_s[k - 1] + frac * frame_period_s)
            beat_scale = 1.0 + beat_interval_jitter_frac * rng.standard_normal()
    pulse = pulse_shape(phase)

    saturation_frac = sao2_ref_pct / 100.0
    ratio_arterial = ratio_of_ratios_from_saturation(saturation_frac, subject.dpf_ratio_red_ir)
    venous_frac = np.clip(saturation_frac - subject.venous_desaturation_frac, 0.0, 1.0)
    ratio_venous = ratio_of_ratios_from_saturation(venous_frac, subject.dpf_ratio_red_ir)

    motion_mask = np.zeros(n_samples, dtype=bool)
    motion_absorbance_ir = np.zeros(n_samples)
    coupling = np.ones(n_samples)
    for episode in motion:
        in_episode = (time_s >= episode.start_s) & (time_s < episode.stop_s)
        if not np.any(in_episode):
            continue
        motion_mask |= in_episode
        n_episode = int(in_episode.sum())
        # Raised-cosine taper over 0.3 s at each end so the burst starts and stops without a step.
        taper = np.ones(n_episode)
        n_taper = min(int(0.3 * fs_hz), n_episode // 2)
        if n_taper > 0:
            ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n_taper))
            taper[:n_taper] = ramp
            taper[-n_taper:] = ramp[::-1]
        # Moved venous blood can only add absorbance, so the band-limited noise is rectified (|.|):
        # that gives it a positive mean and doubles its frequency content (0.6-3.5 Hz -> up to ~7 Hz).
        motion_absorbance_ir[in_episode] += taper * np.abs(
            _band_limited_noise(rng, n_episode, fs_hz, episode.band_hz, episode.venous_absorbance_rms)
        )
        coupling[in_episode] += taper * _band_limited_noise(
            rng, n_episode, fs_hz, episode.band_hz, episode.coupling_rms
        )

    respiration = 1.0 + subject.respiratory_absorbance_frac * np.sin(
        2 * np.pi * subject.respiratory_rate_hz * time_s
    )
    perfusion_index = _as_profile(subject.perfusion_index_ir, time_s)
    if np.any(perfusion_index < 0):
        raise ValueError("perfusion index must be non-negative")
    absorbance_ir = (
        subject.static_absorbance_ir * respiration
        + perfusion_index * pulse
        + motion_absorbance_ir
    )
    absorbance_red = (
        subject.static_absorbance_red * respiration
        + perfusion_index * ratio_arterial * pulse
        + motion_absorbance_ir * ratio_venous
    )

    led_on = np.ones(n_samples)
    if sensor_off_after_s is not None:
        led_on[time_s >= sensor_off_after_s] = 0.0

    def ambient_counts(sample_time_s):
        # Full-wave rectified mains |sin(2 pi f_mains t)| has its fundamental at 2 f_mains.
        level = ambient.dc_counts + ambient.flicker_counts * np.abs(
            np.sin(2 * np.pi * ambient.mains_frequency_hz * sample_time_s)
        )
        if ambient.step_time_s is not None:
            level = level + ambient.step_counts * (sample_time_s >= ambient.step_time_s)
        return level

    def adc(counts):
        noisy = counts + adc_noise_counts * rng.standard_normal(n_samples)
        return np.clip(np.round(noisy), 0, ADC_FULL_SCALE_COUNTS)

    red_counts = adc(
        led_on * subject.led_gain_red_counts * coupling * np.exp(-absorbance_red) + ambient_counts(time_s)
    )
    red_ambient = adc(ambient_counts(time_s + 0.25 * frame_period_s))
    ir_counts = adc(
        led_on * subject.led_gain_ir_counts * coupling * np.exp(-absorbance_ir)
        + ambient_counts(time_s + 0.5 * frame_period_s)
    )
    ir_ambient = adc(ambient_counts(time_s + 0.75 * frame_period_s))

    return PpgRecording(
        fs_hz=float(fs_hz),
        time_s=time_s,
        red_counts=red_counts,
        red_ambient_counts=red_ambient,
        ir_counts=ir_counts,
        ir_ambient_counts=ir_ambient,
        sao2_pct=sao2_ref_pct,
        pulse_rate_bpm=rate_ref_bpm,
        beat_onset_s=np.asarray(beat_onsets),
        motion_mask=motion_mask,
    )


def plateau_profile(levels_pct, hold_s, transition_s):
    """SaO2(t) for a desaturation study: hold each level hold_s, cosine transition between levels.

    Returns (sao2_at(t), plateaus) where plateaus lists (start_s, stop_s, level_pct) of each hold.
    """
    levels = np.asarray(levels_pct, dtype=float)
    segment_s = hold_s + transition_s
    plateaus = [(i * segment_s, i * segment_s + hold_s, float(lv)) for i, lv in enumerate(levels)]

    def sao2_at(time_s):
        time_s = np.asarray(time_s, dtype=float)
        out = np.full(time_s.shape, levels[-1])
        for i, level in enumerate(levels):
            start = i * segment_s
            in_hold = (time_s >= start) & (time_s < start + hold_s)
            out[in_hold] = level
            if i + 1 < len(levels):
                in_ramp = (time_s >= start + hold_s) & (time_s < start + segment_s)
                frac = (time_s[in_ramp] - start - hold_s) / transition_s
                out[in_ramp] = level + (levels[i + 1] - level) * (0.5 - 0.5 * np.cos(np.pi * frac))
        return out

    return sao2_at, plateaus

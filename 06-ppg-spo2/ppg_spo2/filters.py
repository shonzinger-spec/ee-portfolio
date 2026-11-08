"""Causal IIR filters that run block by block, as firmware would.

DC path (baseline, light that passes through bloodless tissue): 2nd-order Butterworth low-pass,
    |H_dc(f)|^2 = 1 / (1 + (f / f_c)^(2N)),   N = 2, f_c = 0.3 Hz.
AC path (arterial pulse): Butterworth band-pass of order N per edge,
    |H_ac(f)|^2 = 1 / (1 + ((f^2 - f_lo f_hi) / (f (f_hi - f_lo)))^(2N)),   N = 4, 0.5-5 Hz.
The band covers 30-300 bpm fundamentals plus the first harmonics; the upper edge rejects the 20 Hz
alias of 120 Hz room-light flicker and wide-band noise, the lower edge rejects breathing (~0.25 Hz)
and the DC level. Both are realised as cascaded second-order sections (direct form II transposed),
which keeps the poles well conditioned at low cut-off / sample-rate ratios.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

DC_CUTOFF_HZ = 0.3
DC_ORDER = 2
AC_BAND_HZ = (0.5, 5.0)
AC_ORDER = 4


class StreamingSos:
    """Second-order-section filter that keeps its state between calls.

    On the first sample the state is set to the steady state for a constant input equal to that
    sample (scipy.signal.sosfilt_zi scaled by x[0]), so no start-up step reaches the output.
    """

    def __init__(self, sos):
        self.sos = np.asarray(sos, dtype=float)
        self._zi_unit = signal.sosfilt_zi(self.sos)
        self._state = None

    def reset(self):
        self._state = None

    def process(self, samples):
        samples = np.asarray(samples, dtype=float)
        if samples.size == 0:
            return samples.copy()
        if self._state is None:
            self._state = self._zi_unit * samples[0]
        out, self._state = signal.sosfilt(self.sos, samples, zi=self._state)
        return out


def _check_rate(fs_hz, highest_hz):
    if not np.isfinite(fs_hz) or fs_hz <= 0:
        raise ValueError("sample rate must be a positive number")
    if highest_hz >= 0.45 * fs_hz:
        raise ValueError(
            f"sample rate {fs_hz} Hz too low: filter edge {highest_hz} Hz must be below 0.45 x fs"
        )


def design_dc_lowpass(fs_hz, cutoff_hz=DC_CUTOFF_HZ, order=DC_ORDER):
    _check_rate(fs_hz, cutoff_hz)
    return signal.butter(order, cutoff_hz, btype="lowpass", fs=fs_hz, output="sos")


def design_ac_bandpass(fs_hz, band_hz=AC_BAND_HZ, order=AC_ORDER):
    low_hz, high_hz = band_hz
    if not 0 < low_hz < high_hz:
        raise ValueError("band edges must satisfy 0 < low < high")
    _check_rate(fs_hz, high_hz)
    return signal.butter(order, [low_hz, high_hz], btype="bandpass", fs=fs_hz, output="sos")


def magnitude_db(sos, frequencies_hz, fs_hz):
    """|H(f)| in dB at the given frequencies (for checks and plots)."""
    _, response = signal.sosfreqz(sos, worN=np.asarray(frequencies_hz, dtype=float), fs=fs_hz)
    return 20 * np.log10(np.maximum(np.abs(response), 1e-15))

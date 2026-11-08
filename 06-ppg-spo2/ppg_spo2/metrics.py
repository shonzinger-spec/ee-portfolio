"""Accuracy statistics used by ISO 80601-2-61 (clause 201.12.1.101 and Annex EE).

    bias      = mean(SpO2_i - SaO2_i)
    precision = std(SpO2_i - SaO2_i)   (sample standard deviation)
    A_rms     = sqrt(mean((SpO2_i - SaO2_i)^2)),   A_rms^2 = bias^2 + ((n-1)/n) precision^2
"""

from __future__ import annotations

import numpy as np


def paired(predicted, reference):
    predicted = np.asarray(predicted, dtype=float)
    reference = np.asarray(reference, dtype=float)
    if predicted.shape != reference.shape:
        raise ValueError("predicted and reference must have the same shape")
    keep = np.isfinite(predicted) & np.isfinite(reference)
    return predicted[keep], reference[keep]


def accuracy_stats(predicted, reference):
    """Return dict with n, bias, precision, arms (NaN when there are no pairs)."""
    p, r = paired(predicted, reference)
    if p.size == 0:
        return {"n": 0, "bias": float("nan"), "precision": float("nan"), "arms": float("nan")}
    err = p - r
    return {
        "n": int(err.size),
        "bias": float(err.mean()),
        "precision": float(err.std(ddof=1)) if err.size > 1 else 0.0,
        "arms": float(np.sqrt(np.mean(err**2))),
    }


def arms_by_decade(predicted, reference, edges=(70.0, 80.0, 90.0, 100.0)):
    """A_rms per reference band [70, 80), [80, 90), [90, 100]."""
    p, r = paired(predicted, reference)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        in_band = (r >= lo) & ((r < hi) if hi < edges[-1] else (r <= hi))
        out.append((lo, hi, accuracy_stats(p[in_band], r[in_band])))
    return out


def reference_pulse_rate_bpm(beat_onset_s, at_time_s, n_beats=8):
    """Reference pulse rate a display averaging n_beats would show: 60 / mean of the last n_beats
    true beat intervals that ended at or before each time (NaN until n_beats intervals exist).

        PR_ref(t) = 60 / ((t_onset[j] - t_onset[j - n]) / n),   j = last onset <= t
    """
    onsets = np.asarray(beat_onset_s, dtype=float)
    out = np.full(np.shape(at_time_s), np.nan)
    for i, t in enumerate(np.atleast_1d(at_time_s)):
        j = np.searchsorted(onsets, t, side="right") - 1
        if j - n_beats >= 0:
            out.flat[i] = 60.0 * n_beats / (onsets[j] - onsets[j - n_beats])
    return out

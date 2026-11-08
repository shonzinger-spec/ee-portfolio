"""Ratio-of-ratios to SpO2 calibration.

Pulse oximeters map R = (AC_red / DC_red) / (AC_ir / DC_ir) to SpO2 with an empirical curve fitted
to paired (R, SaO2) data from a controlled desaturation study, SaO2 measured on arterial blood
samples by CO-oximetry (ISO 80601-2-61, Annex EE). The common first-order form is

    SpO2_pct = a - b * R                                                                     (5)

(textbook constants a = 110, b = 25). Here a and b are fitted by ordinary least squares,

    [a, -b] = argmin sum_i (SaO2_i - (a - b R_i))^2,

on a calibration cohort, and accuracy is then measured on a separate validation cohort.

Output rules (nothing is clipped silently; every case sets a flag):
    raw > 100 + max_overshoot_pct      -> no value, R_OUT_OF_RANGE (R below anything physiological)
    100 < raw <= 100 + max_overshoot   -> 100, CLIPPED_HIGH
    calibrated_min <= raw <= 100       -> raw
    floor <= raw < calibrated_min      -> raw, BELOW_CAL_RANGE (extrapolated, not validated)
    raw < floor                        -> no value, R_OUT_OF_RANGE
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag

import numpy as np


class CalFlag(IntFlag):
    NONE = 0
    CLIPPED_HIGH = 1
    BELOW_CAL_RANGE = 2
    R_OUT_OF_RANGE = 4


@dataclass(frozen=True)
class LinearRatioCalibration:
    intercept_pct: float = 110.0  # a
    slope_pct_per_r: float = 25.0  # b
    calibrated_min_pct: float = 70.0
    floor_pct: float = 50.0
    max_overshoot_pct: float = 5.0

    def raw_spo2_pct(self, ratio_r):
        return self.intercept_pct - self.slope_pct_per_r * np.asarray(ratio_r, dtype=float)

    def spo2_pct(self, ratio_r):
        """Return (spo2_pct, CalFlag) for one R value; spo2 is NaN when no value may be shown."""
        if not np.isfinite(ratio_r) or ratio_r <= 0:
            return float("nan"), CalFlag.R_OUT_OF_RANGE
        raw = float(self.raw_spo2_pct(ratio_r))
        if raw > 100.0 + self.max_overshoot_pct or raw < self.floor_pct:
            return float("nan"), CalFlag.R_OUT_OF_RANGE
        if raw > 100.0:
            return 100.0, CalFlag.CLIPPED_HIGH
        if raw < self.calibrated_min_pct:
            return raw, CalFlag.BELOW_CAL_RANGE
        return raw, CalFlag.NONE


TEXTBOOK_CALIBRATION = LinearRatioCalibration(110.0, 25.0)


def fit_linear_calibration(ratio_r, sao2_ref_pct, **limits):
    """Least-squares fit of equation (5). Needs at least 2 distinct R values."""
    ratio_r = np.asarray(ratio_r, dtype=float)
    sao2_ref_pct = np.asarray(sao2_ref_pct, dtype=float)
    keep = np.isfinite(ratio_r) & np.isfinite(sao2_ref_pct)
    ratio_r, sao2_ref_pct = ratio_r[keep], sao2_ref_pct[keep]
    if ratio_r.size < 2 or np.ptp(ratio_r) == 0:
        raise ValueError("calibration needs at least two distinct R values")
    design = np.column_stack([np.ones_like(ratio_r), ratio_r])
    (intercept, coef_r), *_ = np.linalg.lstsq(design, sao2_ref_pct, rcond=None)
    return LinearRatioCalibration(float(intercept), float(-coef_r), **limits)

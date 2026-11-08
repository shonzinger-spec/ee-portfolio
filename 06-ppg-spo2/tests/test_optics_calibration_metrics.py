import numpy as np
import pytest

from ppg_spo2.calibration import CalFlag, LinearRatioCalibration, fit_linear_calibration
from ppg_spo2.metrics import accuracy_stats, arms_by_decade, reference_pulse_rate_bpm
from ppg_spo2.optics import ratio_of_ratios_from_saturation, saturation_from_ratio_of_ratios


def test_ratio_of_ratios_inverse_round_trip():
    s = np.linspace(0.5, 1.0, 51)
    for kappa in (1.3, 1.5, 1.7):
        assert np.allclose(saturation_from_ratio_of_ratios(ratio_of_ratios_from_saturation(s, kappa), kappa), s)


def test_ratio_rises_as_saturation_falls():
    r = ratio_of_ratios_from_saturation(np.linspace(0.6, 1.0, 41))
    assert np.all(np.diff(r) < 0)


def test_ratio_scales_with_dpf_ratio():
    assert ratio_of_ratios_from_saturation(0.9, 3.0) == pytest.approx(2 * ratio_of_ratios_from_saturation(0.9, 1.5))


@pytest.mark.parametrize("bad", [-0.1, 1.2])
def test_saturation_outside_unit_interval_rejected(bad):
    with pytest.raises(ValueError):
        ratio_of_ratios_from_saturation(bad)


def test_fit_recovers_known_line():
    r = np.linspace(0.4, 1.8, 30)
    cal = fit_linear_calibration(r, 107.0 - 21.0 * r)
    assert cal.intercept_pct == pytest.approx(107.0)
    assert cal.slope_pct_per_r == pytest.approx(21.0)


def test_fit_ignores_nan_pairs_and_needs_two_r_values():
    cal = fit_linear_calibration([0.5, np.nan, 1.0], [97.5, 90.0, 85.0])
    assert cal.raw_spo2_pct(0.75) == pytest.approx(91.25)
    with pytest.raises(ValueError):
        fit_linear_calibration([0.5, 0.5], [97.0, 96.0])


def test_calibration_output_rules():
    cal = LinearRatioCalibration(110.0, 25.0)  # raw = 110 - 25 R
    assert cal.spo2_pct(0.6) == (95.0, CalFlag.NONE)
    assert cal.spo2_pct(0.36) == (100.0, CalFlag.CLIPPED_HIGH)  # raw 101
    value, flag = cal.spo2_pct(1.8)  # raw 65
    assert value == pytest.approx(65.0) and flag == CalFlag.BELOW_CAL_RANGE
    for r in (0.1, 2.5, np.nan, 0.0, -1.0):  # raw 107.5 / 47.5 / undefined
        value, flag = cal.spo2_pct(r)
        assert np.isnan(value) and flag == CalFlag.R_OUT_OF_RANGE


def test_arms_identity():
    rng = np.random.default_rng(0)
    ref = rng.uniform(70, 100, 500)
    pred = ref + 0.7 + rng.normal(0, 1.5, 500)
    st = accuracy_stats(pred, ref)
    n = st["n"]
    assert st["arms"] ** 2 == pytest.approx(st["bias"] ** 2 + (n - 1) / n * st["precision"] ** 2)


def test_accuracy_stats_drop_nan_and_handle_empty():
    st = accuracy_stats([90.0, np.nan, 80.0], [91.0, 85.0, np.nan])
    assert st["n"] == 1 and st["arms"] == pytest.approx(1.0)
    assert accuracy_stats([np.nan], [90.0])["n"] == 0


def test_arms_by_decade_includes_100_in_top_band():
    bands = arms_by_decade([100.0, 85.0, 75.0], [100.0, 84.0, 77.0])
    assert [b[2]["n"] for b in bands] == [1, 1, 1]


def test_reference_pulse_rate():
    onsets = np.arange(0, 30, 0.75)  # 80 bpm
    rate = reference_pulse_rate_bpm(onsets, [2.0, 10.0])
    assert np.isnan(rate[0]) and rate[1] == pytest.approx(80.0)

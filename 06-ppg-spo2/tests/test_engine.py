import numpy as np
import pytest

from ppg_spo2 import AmbientLight, PpgSpo2Engine, Status, SubjectOptics, generate_recording
from ppg_spo2.metrics import reference_pulse_rate_bpm
from ppg_spo2.optics import ratio_of_ratios_from_saturation


@pytest.fixture(scope="module")
def clean_run():
    rec = generate_recording(40, 100.0, 90.0, 72.0, seed=5)
    return rec, PpgSpo2Engine(100.0).process(rec)


def test_ratio_of_ratios_matches_the_model(clean_run):
    _, res = clean_run
    r = np.nanmedian(res.ratio_r[15:])
    assert r == pytest.approx(float(ratio_of_ratios_from_saturation(0.90)), rel=0.01)


def test_one_output_per_second_and_pulse_rate(clean_run):
    rec, res = clean_run
    assert np.allclose(res.output_time_s, np.arange(1, 41))
    ref = reference_pulse_rate_bpm(rec.beat_onset_s, res.output_time_s)
    assert np.nanmax(np.abs(res.pulse_rate_bpm[15:] - ref[15:])) < 2.0


def test_beats_are_systolic_upstrokes(clean_run):
    _, res = clean_run
    accepted = res.accepted_beats()
    assert len(accepted) > 30
    for b in accepted:
        assert b.valley_index < b.peak_index
        assert res.normalised_ir[b.peak_index] > res.normalised_ir[b.valley_index]


def test_block_size_does_not_change_the_result(clean_run):
    rec, res = clean_run
    other = PpgSpo2Engine(100.0).process(rec, block_s=0.37)
    assert np.array_equal(np.nan_to_num(other.spo2_pct, nan=-1), np.nan_to_num(res.spo2_pct, nan=-1))
    assert np.array_equal(other.status, res.status)


def test_sample_rate_mismatch_rejected(clean_run):
    rec, _ = clean_run
    with pytest.raises(ValueError, match="sample-rate mismatch"):
        PpgSpo2Engine(125.0).process(rec)


@pytest.mark.parametrize("streams", [
    ([1.0, 2.0], [0.0, 0.0], [1.0], [0.0]),
    ([1.0, np.nan], [0.0, 0.0], [1.0, 1.0], [0.0, 0.0]),
    ([[1.0, 2.0]], [[0.0, 0.0]], [[1.0, 2.0]], [[0.0, 0.0]]),
])
def test_block_input_validation(streams):
    with pytest.raises(ValueError):
        PpgSpo2Engine(100.0).process_block(*streams)


def test_ambient_cancellation_and_normalisation_steps():
    engine = PpgSpo2Engine(100.0)
    assert np.array_equal(engine.cancel_ambient(np.array([10.0, 12.0]), np.array([3.0, 2.0])), [7.0, 10.0])
    n = engine.normalise(np.array([1.0, 1.0]), np.array([100.0, 0.0]), np.array([True, False]))
    assert n[0] == pytest.approx(-0.01) and np.isnan(n[1])


def test_saturation_rules():
    engine = PpgSpo2Engine(100.0)
    fs = engine.adc_full_scale_counts
    led = np.array([5e5, fs, 0.0, 5e5])
    amb = np.array([0.0, 0.0, 0.0, fs])
    frames = engine.saturated_frames(led, amb, np.full(4, 5e5), np.zeros(4))
    # Only full scale is saturation. An ambient sample of 0 is a dark room; an LED sample of 0 is
    # "no light", reported as NO_SIGNAL by the low-light test (see test_dark_room_probe_off_...).
    assert frames.tolist() == [False, True, False, True]


def test_zero_input_gives_no_signal_and_no_numbers():
    rec = generate_recording(10, 100.0, seed=6)
    for name in ("red_counts", "red_ambient_counts", "ir_counts", "ir_ambient_counts"):
        getattr(rec, name)[:] = 0.0
    res = PpgSpo2Engine(100.0).process(rec)
    assert np.all(np.isnan(res.spo2_pct)) and np.all(res.status & Status.NO_SIGNAL)
    assert not res.beats


def test_dark_room_ambient_does_not_flag_saturation():
    rec = generate_recording(20, 100.0, ambient=AmbientLight(dc_counts=0.0), seed=7)
    res = PpgSpo2Engine(100.0).process(rec)
    assert not np.any(res.status & Status.SATURATED)


def test_high_rate_peak_timing_sub_sample():
    rec = generate_recording(40, 100.0, 97.0, 190.0, beat_interval_jitter_frac=0.0, seed=8)
    res = PpgSpo2Engine(100.0).process(rec)
    # 190 bpm = 31.6 samples per beat: whole-sample timing gives 31 or 32 (184-194 bpm per beat).
    assert np.nanmax(np.abs(res.pulse_rate_bpm[15:] - 190.0)) < 1.0


def test_low_perfusion_blank_and_flagged():
    rec = generate_recording(30, 100.0, subject=SubjectOptics(perfusion_index_ir=0.0002), seed=9)
    res = PpgSpo2Engine(100.0).process(rec)
    assert np.all(np.isnan(res.spo2_pct))
    assert np.any(res.status & Status.LOW_PERFUSION)


def _splice(first, second, from_s):
    """Recording that is `first` before from_s and `second` from then on."""
    late = first.time_s >= from_s
    for name in ("red_counts", "red_ambient_counts", "ir_counts", "ir_ambient_counts", "sao2_pct"):
        getattr(first, name)[late] = getattr(second, name)[late]
    return first


def test_reattach_to_another_patient_never_shows_the_old_reading():
    patient_a = generate_recording(70, 100.0, 97.0, 75.0, seed=21)
    probe_off = generate_recording(70, 100.0, 97.0, 75.0, sensor_off_after_s=0.0, seed=22)
    patient_b = generate_recording(70, 100.0, 80.0, 75.0, seed=23)
    rec = _splice(_splice(patient_a, probe_off, 30.0), patient_b, 32.0)
    res = PpgSpo2Engine(100.0).process(rec)
    after = res.output_time_s > 30.0
    shown = after & np.isfinite(res.spo2_pct)
    assert shown.any()  # patient B is read eventually
    assert np.all(np.abs(res.spo2_pct[shown] - 80.0) < 3.0)


def test_probe_slipping_off_is_flagged_at_the_next_output():
    room = AmbientLight(dc_counts=1.5e5, flicker_counts=5e4)
    on = generate_recording(50, 100.0, 97.0, 75.0, ambient=room, seed=24)
    slipped = generate_recording(50, 100.0, 97.0, 75.0, ambient=room, seed=25, subject=SubjectOptics(
        led_gain_red_counts=0.005 * 8.0e6, led_gain_ir_counts=0.005 * 4.5e6))
    res = PpgSpo2Engine(100.0).process(_splice(on, slipped, 30.0))
    first_after = np.flatnonzero(res.output_time_s > 30.0)[0]
    assert res.status[first_after] & Status.NO_SIGNAL
    assert np.all(np.isnan(res.spo2_pct[first_after:]))


def test_dark_room_probe_off_is_no_signal_not_saturation():
    rec = generate_recording(30, 100.0, 97.0, 75.0, sensor_off_after_s=15.0, seed=26)
    res = PpgSpo2Engine(100.0).process(rec)
    late = res.output_time_s > 16.0
    assert np.all(res.status[late] & Status.NO_SIGNAL)
    assert not np.any(res.status[late] & Status.SATURATED)


def test_flat_line_detection_does_not_depend_on_block_size():
    rec = generate_recording(40, 100.0, 97.0, 75.0, seed=27)
    stuck = (rec.time_s >= 12.3) & (rec.time_s < 14.0)
    rec.red_counts[stuck] = rec.red_counts[np.argmax(stuck)]
    rec.ir_counts[stuck] = rec.ir_counts[np.argmax(stuck)]
    one = PpgSpo2Engine(100.0).process(rec, block_s=1.0)
    ten = PpgSpo2Engine(100.0).process(rec, block_s=10.0)
    assert np.array_equal(one.status, ten.status)
    assert np.array_equal(np.nan_to_num(one.spo2_pct, nan=-1), np.nan_to_num(ten.spo2_pct, nan=-1))
    assert np.any(one.status & Status.NO_SIGNAL)


def test_near_zero_light_is_never_divided():
    rec = generate_recording(20, 100.0, sensor_off_after_s=0.0, seed=28)
    rec.red_counts += 30.0  # detector offset: DC ~30 counts, far below any usable light level
    rec.ir_counts += 30.0
    res = PpgSpo2Engine(100.0).process(rec)
    assert np.all(np.isnan(res.normalised_red)) and np.all(np.isnan(res.normalised_ir))
    assert not res.beats and np.all(np.isnan(res.spo2_pct))
    assert np.all(res.status & Status.NO_SIGNAL)


def test_erratic_ratio_sequence_is_withheld():
    # Weak in-band motion that leaves every beat plausible but makes the beat-to-beat R erratic
    # (seed chosen during design because it slipped past the per-beat checks).
    from ppg_spo2 import EngineConfig, MotionEpisode
    rec = generate_recording(100, 100.0, 95.0, 75.0, motion=[MotionEpisode(30, 60, 0.005, 0.003)], seed=303)
    ref = lambda res: np.interp(res.output_time_s, rec.time_s, rec.sao2_pct)
    with_limit = PpgSpo2Engine(100.0).process(rec)
    without_limit = PpgSpo2Engine(100.0, config=EngineConfig(max_ratio_scatter=np.inf)).process(rec)
    assert np.nanmax(np.abs(with_limit.spo2_pct - ref(with_limit))) <= 3.0
    assert np.nanmax(np.abs(without_limit.spo2_pct - ref(without_limit))) > 3.0
    assert np.any(with_limit.status & Status.LOW_QUALITY)


def test_single_beat_window_does_not_crash():
    from ppg_spo2 import EngineConfig
    rec = generate_recording(30, 100.0, 97.0, 75.0, seed=29)
    res = PpgSpo2Engine(100.0, config=EngineConfig(min_beats_for_output=1)).process(rec)
    assert np.isfinite(res.spo2_pct).any()

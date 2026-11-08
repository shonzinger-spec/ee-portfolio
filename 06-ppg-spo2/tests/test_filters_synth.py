import numpy as np
import pytest
from scipy import signal

from ppg_spo2.filters import StreamingSos, design_ac_bandpass, design_dc_lowpass, magnitude_db
from ppg_spo2.synth import (
    ADC_FULL_SCALE_COUNTS, AmbientLight, MotionEpisode, SubjectOptics, generate_recording, plateau_profile, pulse_shape,
)


def test_streaming_filter_matches_one_shot_for_any_block_split():
    rng = np.random.default_rng(1)
    x = 5000 + rng.normal(0, 100, 1000)
    sos = design_ac_bandpass(100.0)
    reference = signal.sosfilt(sos, x, zi=signal.sosfilt_zi(sos) * x[0])[0]
    streaming = StreamingSos(sos)
    pieces = [streaming.process(x[a:b]) for a, b in [(0, 7), (7, 300), (300, 301), (301, 1000)]]
    assert np.allclose(np.concatenate(pieces), reference)


def test_dc_filter_starts_without_a_step():
    out = StreamingSos(design_dc_lowpass(100.0)).process(np.full(200, 7.0e5))
    assert np.allclose(out, 7.0e5)


def test_filter_design_rejects_too_low_sample_rate():
    with pytest.raises(ValueError):
        design_ac_bandpass(10.0)
    with pytest.raises(ValueError):
        design_ac_bandpass(100.0, band_hz=(5.0, 1.0))


def test_band_pass_gain():
    db = magnitude_db(design_ac_bandpass(100.0), [1.5, 20.0], 100.0)
    assert abs(db[0]) < 0.1 and db[1] < -40


def test_pulse_shape_unit_height_and_periodic():
    x = np.linspace(0, 3, 3001)
    p = pulse_shape(x)
    assert p.min() == pytest.approx(0.0, abs=1e-3) and p.max() == pytest.approx(1.0, abs=1e-3)
    assert np.allclose(pulse_shape(x[:1000]), pulse_shape(x[:1000] + 1.0))


def test_generator_beat_onsets_follow_the_rate():
    rec = generate_recording(60, 100.0, 97.0, 90.0, beat_interval_jitter_frac=0.0, seed=1)
    assert np.allclose(np.diff(rec.beat_onset_s), 60.0 / 90.0, atol=0.002)
    assert np.allclose(rec.pulse_rate_bpm, 90.0)


def test_generator_counts_stay_inside_adc_range_and_sensor_off_removes_light():
    rec = generate_recording(20, 100.0, sensor_off_after_s=10.0, ambient=AmbientLight(dc_counts=1e5), seed=2)
    assert rec.red_counts.min() >= 0 and rec.red_counts.max() <= ADC_FULL_SCALE_COUNTS
    late = rec.time_s > 10.0
    # With the LED off, the LED sample is only ambient light (+ noise): subtraction leaves ~0.
    assert abs(np.mean(rec.red_counts[late] - rec.red_ambient_counts[late])) < 50


def test_flicker_leaks_through_ambient_subtraction():
    rec = generate_recording(10, 100.0, ambient=AmbientLight(flicker_counts=1e5), adc_noise_counts=0.0, seed=3)
    clean = generate_recording(10, 100.0, adc_noise_counts=0.0, seed=3)
    leak = (rec.ir_counts - rec.ir_ambient_counts) - (clean.ir_counts - clean.ir_ambient_counts)
    spectrum = np.abs(np.fft.rfft(leak))
    freqs = np.fft.rfftfreq(leak.size, 0.01)
    assert freqs[np.argmax(spectrum[1:]) + 1] == pytest.approx(20.0, abs=0.2)  # 120 Hz alias at fs 100 Hz


def test_motion_mask_and_time_varying_perfusion():
    rec = generate_recording(30, 100.0, motion=[MotionEpisode(10, 20)],
                             subject=SubjectOptics(perfusion_index_ir=lambda t: 0.01 + 0.0 * t), seed=4)
    assert rec.motion_mask[(rec.time_s >= 10) & (rec.time_s < 20)].all()
    assert not rec.motion_mask[rec.time_s < 10].any()


@pytest.mark.parametrize("kwargs", [dict(duration_s=0), dict(sao2_pct=101.0), dict(pulse_rate_bpm=0.0),
                                    dict(subject=SubjectOptics(perfusion_index_ir=-0.01))])
def test_generator_input_validation(kwargs):
    args = dict(duration_s=5)
    args.update(kwargs)
    with pytest.raises(ValueError):
        generate_recording(**args)


def test_plateau_profile_holds_and_transitions():
    sao2_at, plateaus = plateau_profile([98, 80], hold_s=10, transition_s=5)
    assert sao2_at(np.array([5.0, 12.5, 20.0])).tolist() == pytest.approx([98.0, 89.0, 80.0])
    assert plateaus == [(0.0, 10.0, 98.0), (15.0, 25.0, 80.0)]

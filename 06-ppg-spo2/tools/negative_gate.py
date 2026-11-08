"""Negative gate: main.py's checks must fail when any one safeguard is removed.

Each mutant patches the engine to drop one part of the signal chain or one protection, runs the
full check list, and is caught only if at least one check FAILs. A crash is reported separately
and does not count as caught: it shows the mutant broke the code, not that a check noticed the
missing safeguard. A check list that still passes with a safeguard removed would not be testing it.

    python3 tools/negative_gate.py
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import ppg_spo2.engine as engine_module  # noqa: E402
from ppg_spo2 import calibration as calibration_module  # noqa: E402
from ppg_spo2.filters import AC_BAND_HZ  # noqa: E402
from scipy import signal  # noqa: E402


@contextlib.contextmanager
def patched(target, name, value):
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


@contextlib.contextmanager
def config_override(**changes):
    original = engine_module.EngineConfig
    replacement = dataclasses.make_dataclass(
        "EngineConfig",
        [(f.name, f.type, dataclasses.field(default=changes.get(f.name, f.default)))
         for f in dataclasses.fields(original)],
        frozen=True,
    )
    engine_module.EngineConfig = replacement
    try:
        yield
    finally:
        engine_module.EngineConfig = original


Engine = engine_module.PpgSpo2Engine


def no_ambient_subtraction():
    return patched(Engine, "cancel_ambient", lambda self, led, ambient: led)


def no_saturation_check():
    return patched(Engine, "saturated_frames", lambda self, r, ra, i, ia: np.zeros(r.shape, dtype=bool))


def no_dc_normalisation():
    def normalise(self, ac, dc, valid):
        out = np.full(ac.shape, np.nan)
        out[valid] = -ac[valid] / 1.0e6  # fixed scale instead of the channel's own DC
        return out
    return patched(Engine, "normalise", normalise)




def no_low_pass_edge():
    def highpass_only(fs_hz, band_hz=AC_BAND_HZ, order=4):
        return signal.butter(order, band_hz[0], btype="highpass", fs=fs_hz, output="sos")
    return patched(engine_module, "design_ac_bandpass", highpass_only)


def no_calibration_limits():
    def raw_only(self, ratio_r):
        return float(self.raw_spo2_pct(ratio_r)), calibration_module.CalFlag.NONE
    return patched(calibration_module.LinearRatioCalibration, "spo2_pct", raw_only)


MUTANTS = [
    ("M1", "ambient sample not subtracted", no_ambient_subtraction),
    ("M2", "saturated frames not detected", no_saturation_check),
    ("M3", "AC not divided by its own DC", no_dc_normalisation),
    ("M4", "no minimum-DC (probe-off) threshold", lambda: config_override(min_dc_fraction_of_full_scale=0.0)),
    ("M5", "band-pass without its 5 Hz upper edge", no_low_pass_edge),
    ("M6", "calibration output limits removed", no_calibration_limits),
    ("M7", "beat amplitude/interval/shape checks off", lambda: config_override(
        amplitude_tolerance=(0.0, np.inf), interval_tolerance_frac=np.inf, template_min_correlation=-np.inf)),
    ("M8", "red/IR proportionality check off", lambda: config_override(min_red_ir_correlation=-np.inf)),
    ("M9", "seeding without quiet period or R agreement", lambda: config_override(
        seed_quiet_s=0.0, seed_ratio_tolerance_frac=np.inf)),
    ("M10", "no un-seed timeout", lambda: config_override(unseed_after_s=np.inf)),
    ("M11", "no flat-line detection", lambda: config_override(flat_counts_std=-1.0)),
    ("M12", "no R-continuity check", lambda: config_override(ratio_jump_abs=np.inf)),
    ("M13", "no minimum beat count per output", lambda: config_override(min_beats_for_output=1)),
    ("M14", "no R-scatter quality limit", lambda: config_override(max_ratio_scatter=np.inf)),
    ("M15", "whole motion layer off (M7 + M8 + M12 + M14)", lambda: config_override(
        amplitude_tolerance=(0.0, np.inf), interval_tolerance_frac=np.inf, template_min_correlation=-np.inf,
        min_red_ir_correlation=-np.inf, ratio_jump_abs=np.inf, max_ratio_scatter=np.inf)),
]


def run_mutant(make_patch):
    buffer = io.StringIO()
    try:
        with make_patch(), contextlib.redirect_stdout(buffer):
            results = main.run_checks(make_plots=False)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return [check_id for check_id, ok in results if not ok], None


def main_gate():
    print("PPG SpO2 06 - negative gate (each mutant removes one safeguard)")
    baseline, crash = run_mutant(contextlib.nullcontext)
    if crash or baseline:
        print(f"baseline: {'crashed: ' + crash if crash else 'FAILING ' + ', '.join(baseline)}")
        return 1
    print("baseline: all checks pass")
    caught = 0
    for mutant_id, description, make_patch in MUTANTS:
        failed, crash = run_mutant(make_patch)
        if crash:
            print(f"  {mutant_id:<4s} {description:<46s} {'CRASHED':<11s} {crash}")
            continue
        verdict = "caught" if failed else "NOT CAUGHT"
        caught += bool(failed)
        print(f"  {mutant_id:<4s} {description:<46s} {verdict:<11s} failing: {', '.join(failed) if failed else '-'}")
    print(f"\n{caught}/{len(MUTANTS)} mutants caught")
    return 0 if caught == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main_gate())

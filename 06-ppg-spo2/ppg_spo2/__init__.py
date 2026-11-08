"""Two-wavelength PPG pulse-oximetry DSP engine (simulation; not a medical device)."""

from .calibration import TEXTBOOK_CALIBRATION, LinearRatioCalibration, fit_linear_calibration
from .engine import EngineConfig, PpgSpo2Engine, Status
from .synth import AmbientLight, MotionEpisode, SubjectOptics, generate_recording, plateau_profile

__all__ = [
    "AmbientLight", "EngineConfig", "LinearRatioCalibration", "MotionEpisode", "PpgSpo2Engine",
    "Status", "SubjectOptics", "TEXTBOOK_CALIBRATION", "fit_linear_calibration",
    "generate_recording", "plateau_profile",
]

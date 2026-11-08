"""Hypothetical insulin pump used as the worked example: components, fault tree and hazards.

The device, its architecture and every number below are illustrative assumptions made for this
simulation project. They are not taken from any real pump, reliability database or regulatory
submission. Failure rates are set high enough that the Monte Carlo can count the events within a
four-year service life.

Architecture assumed:
  - A DC motor drives a lead screw that pushes the reservoir plunger. The H-bridge motor driver can
    latch on (a MOSFET fails short): the motor runs away. An independent second supply switch, driven
    by a watchdog, cuts motor power when the motor turns without a command; it is self-tested at
    every reservoir change (72 h).
  - Plunger position is read by three Hall sensors with 2-out-of-3 voting and cross-checked against
    the motor encoder count. A common-cause failure (beta factor) can upset all three Hall sensors.
  - An infusion-set occlusion raises line pressure, sensed by a force sensor on the drive; the force
    sensor's ADC reference drifts with age (a random walk plus a steady drift). An occlusion alarm
    needs the force sensor, an in-tolerance reference and a working alarm (buzzer and vibration).
  - Drive-train wear (Weibull) ends in a motor stall, detected through the encoder and annunciated
    by the same alarm. A revealed failure is restored after a mean 24 h (in practice the pump is
    swapped).

Simplifications (each makes the hazard estimates higher, not lower, except where noted):
  - Only the failed component is renewed after a revealed failure; latent faults elsewhere carry
    over, and other hazards may still start during the 24 h restoration.
  - The reference error is reset only when a check finds it out of tolerance, and it counts as out of
    tolerance from its first crossing until the next check even if it wanders back.
  - Only the drift direction that hides an occlusion is modelled; the other direction gives false
    occlusion alarms (a nuisance, not this hazard) and is left out.
  - All basic events are independent apart from the beta-factor common cause of the Hall sensors.
    Not modelled: new failure modes of the risk control itself (a fault of the second reference or
    of the comparison, a common-cause drift of both references), software, use errors.

Hazardous situations (ISO 14971 terms):
  H1 over-delivery:  motor runaway not cut off, or a wrong plunger position the encoder check misses
  H2 under-delivery: an occlusion or a stall that is not annunciated
"""

from __future__ import annotations

from dataclasses import dataclass

from .components import (
    HOURS_PER_YEAR,
    DriftFirstPassage,
    Exponential,
    PeriodicallyTested,
    RevealedWeibull,
    TwoStateExponential,
    Weibull,
)
from .risk import Hazard
from .tree import FaultTree, and_gate, or_gate, vote_gate

SERVICE_LIFE_YEARS = 4.0
SERVICE_LIFE_H = SERVICE_LIFE_YEARS * HOURS_PER_YEAR  # 35064 h
SELF_TEST_INTERVAL_H = 72.0          # power-up self-test at every reservoir / infusion-set change
SERVICE_CHECK_INTERVAL_H = HOURS_PER_YEAR  # yearly service check (initial design's reference check)
REPLACEMENT_MEAN_H = 24.0            # revealed failure: pump replaced, mean 24 h
OCCLUSION_RATE_PER_H = 2.5e-4        # about 2.2 occlusions per year
OCCLUSION_MEAN_H = 24.0              # until cleared by an alarm-free set change
HALL_RATE_PER_H = 5e-6
HALL_BETA = 0.05                     # beta-factor common-cause share of the Hall sensor failure rate
VREF_DRIFT_PCT_PER_KH = 0.02         # steady drift of the force-sensor reference, % per 1000 h
VREF_DIFFUSION_PCT_PER_SQRT_KH = 0.20  # random-walk drift, % per sqrt(1000 h)
VREF_TOLERANCE_PCT = 3.0             # error at which the occlusion threshold is no longer met
GEAR_WEIBULL_SHAPE = 3.0
GEAR_WEIBULL_SCALE_H = 60_000.0

BASIC_EVENTS = ("DRV_LATCH", "CUTOFF_FAIL", "HALL_A", "HALL_B", "HALL_C", "HALL_CCF", "ENC_FAIL",
                "OCCL", "FORCE_FAIL", "VREF_DRIFT", "GEAR_WEAR", "ALARM_FAIL")

EVENT_TEXT = {
    "DRV_LATCH": "motor driver latched on (MOSFET short)",
    "CUTOFF_FAIL": "watchdog motor cut-off switch failed closed",
    "HALL_A": "plunger Hall sensor A wrong",
    "HALL_B": "plunger Hall sensor B wrong",
    "HALL_C": "plunger Hall sensor C wrong",
    "HALL_CCF": "common-cause failure of all Hall sensors",
    "ENC_FAIL": "motor encoder / count check failed",
    "OCCL": "infusion-set micro-occlusion present",
    "FORCE_FAIL": "occlusion force sensor failed",
    "VREF_DRIFT": "force-sensor ADC reference out of tolerance",
    "GEAR_WEAR": "drive-train wear-out (motor stall)",
    "ALARM_FAIL": "alarm annunciator (buzzer + vibration) failed",
}

GATES = (
    and_gate("RUNAWAY_NOT_CUT_OFF", "DRV_LATCH", "CUTOFF_FAIL"),
    vote_gate("HALL_2OO3", 2, "HALL_A", "HALL_B", "HALL_C"),
    or_gate("POSITION_WRONG", "HALL_2OO3", "HALL_CCF"),
    and_gate("POSITION_ERROR_MISSED", "POSITION_WRONG", "ENC_FAIL"),
    or_gate("H1_OVER_DELIVERY", "RUNAWAY_NOT_CUT_OFF", "POSITION_ERROR_MISSED"),
    or_gate("OCCLUSION_ALARM_LOST", "FORCE_FAIL", "VREF_DRIFT", "ALARM_FAIL"),
    and_gate("OCCLUSION_UNANNOUNCED", "OCCL", "OCCLUSION_ALARM_LOST"),
    or_gate("STALL_ALARM_LOST", "ENC_FAIL", "ALARM_FAIL"),
    and_gate("STALL_UNANNOUNCED", "GEAR_WEAR", "STALL_ALARM_LOST"),
    or_gate("H2_UNDER_DELIVERY", "OCCLUSION_UNANNOUNCED", "STALL_UNANNOUNCED"),
    or_gate("ANY_HAZARD", "H1_OVER_DELIVERY", "H2_UNDER_DELIVERY"),
)

HAZARD_TOPS = ("H1_OVER_DELIVERY", "H2_UNDER_DELIVERY", "ANY_HAZARD")

HAZARDS = {
    "H1_OVER_DELIVERY": Hazard("H1", "insulin over-delivery (runaway or wrong plunger position not caught)",
                               "severe hypoglycaemia", severity=4, p2_harm_given_situation=0.1),
    "H2_UNDER_DELIVERY": Hazard("H2", "insulin under-delivery not annunciated (occlusion or stall)",
                                "hyperglycaemia / ketoacidosis", severity=3, p2_harm_given_situation=0.05),
}


def fault_tree(top: str = "ANY_HAZARD") -> FaultTree:
    return FaultTree(BASIC_EVENTS, GATES, top)


@dataclass(frozen=True)
class Design:
    name: str
    vref_check_interval_h: float
    description: str


INITIAL_DESIGN = Design("initial", SERVICE_CHECK_INTERVAL_H, "reference checked at the yearly service only")
CONTROLLED_DESIGN = Design("with risk control", SELF_TEST_INTERVAL_H,
                           "reference compared with a second reference at every power-up self-test")


def components(design: Design = INITIAL_DESIGN) -> list:
    """Component models in BASIC_EVENTS order."""
    tested = lambda name, rate: PeriodicallyTested(name, Exponential(rate), SELF_TEST_INTERVAL_H,
                                                   description=EVENT_TEXT[name])
    vref = DriftFirstPassage(VREF_DRIFT_PCT_PER_KH / 1e3, VREF_DIFFUSION_PCT_PER_SQRT_KH / 1e3**0.5, VREF_TOLERANCE_PCT)
    models = [
        TwoStateExponential("DRV_LATCH", 1e-5, REPLACEMENT_MEAN_H, description=EVENT_TEXT["DRV_LATCH"]),
        tested("CUTOFF_FAIL", 1e-5),
        tested("HALL_A", HALL_RATE_PER_H * (1 - HALL_BETA)),
        tested("HALL_B", HALL_RATE_PER_H * (1 - HALL_BETA)),
        tested("HALL_C", HALL_RATE_PER_H * (1 - HALL_BETA)),
        tested("HALL_CCF", HALL_RATE_PER_H * HALL_BETA),
        tested("ENC_FAIL", 5e-6),
        TwoStateExponential("OCCL", OCCLUSION_RATE_PER_H, OCCLUSION_MEAN_H, hardware=False, description=EVENT_TEXT["OCCL"]),
        tested("FORCE_FAIL", 5e-6),
        PeriodicallyTested("VREF_DRIFT", vref, design.vref_check_interval_h, description=EVENT_TEXT["VREF_DRIFT"]),
        RevealedWeibull("GEAR_WEAR", Weibull(GEAR_WEIBULL_SHAPE, GEAR_WEIBULL_SCALE_H), REPLACEMENT_MEAN_H,
                        description=EVENT_TEXT["GEAR_WEAR"]),
        tested("ALARM_FAIL", 3e-6),
    ]
    assert tuple(m.name for m in models) == BASIC_EVENTS
    return models


# Markov twin of H2: every H2 component as a two-state exponential, with rates raised so that the
# first hazard comes within about two years and a long run sees many hazard onsets.
MARKOV_TWIN = (
    ("OCCL", 2e-3, 24.0),
    ("FORCE_FAIL", 2e-4, 36.0),
    ("VREF_DRIFT", 1e-4, 200.0),
    ("ALARM_FAIL", 1e-4, 36.0),
    ("GEAR_WEAR", 2e-4, 24.0),
    ("ENC_FAIL", 1e-4, 36.0),
)


def markov_twin_components() -> list[TwoStateExponential]:
    return [TwoStateExponential(name, rate, repair, hardware=name != "OCCL") for name, rate, repair in MARKOV_TWIN]

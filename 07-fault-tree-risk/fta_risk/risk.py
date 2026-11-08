"""ISO 14971 style risk estimation and evaluation for each hazardous situation.

ISO 14971:2019 estimates risk from the severity of the harm and its probability of occurrence,
often split as

    P(harm) = P1 x P2                                                                            (1)

with P1 the probability (here: the expected number per device-year) of the hazardous situation and
P2 the probability that the hazardous situation leads to the harm. The standard leaves the
probability and severity scales and the acceptability criteria to the manufacturer's risk management
plan. The scales and the acceptability policy below are an illustrative example of such a plan,
written for this project; they are not taken from the standard or from any real device.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SEVERITY_LEVELS = {1: "Negligible", 2: "Minor", 3: "Serious", 4: "Critical", 5: "Catastrophic"}

# Probability levels of the harm, per device-year: (name, lower bound inclusive).
PROBABILITY_LEVELS = (
    ("Frequent", 1e-2),
    ("Probable", 1e-3),
    ("Occasional", 1e-4),
    ("Remote", 1e-5),
    ("Improbable", 0.0),
)
LEVEL_RANK = {name: rank for rank, (name, _) in enumerate(reversed(PROBABILITY_LEVELS), start=1)}  # Improbable 1

# Illustrative acceptability policy: highest probability level accepted for each severity.
MAX_ACCEPTABLE_LEVEL = {1: "Frequent", 2: "Probable", 3: "Remote", 4: "Improbable", 5: "Improbable"}


def probability_level(harm_per_device_year: float) -> str:
    if not np.isfinite(harm_per_device_year) or harm_per_device_year < 0.0:
        raise ValueError(f"probability of harm must be finite and >= 0, got {harm_per_device_year}")
    for name, lower in PROBABILITY_LEVELS:
        if harm_per_device_year >= lower:
            return name
    raise AssertionError("unreachable")


def acceptable(severity: int, level: str) -> bool:
    if severity not in SEVERITY_LEVELS:
        raise ValueError(f"severity must be one of {sorted(SEVERITY_LEVELS)}, got {severity}")
    return LEVEL_RANK[level] <= LEVEL_RANK[MAX_ACCEPTABLE_LEVEL[severity]]


@dataclass(frozen=True)
class Hazard:
    """One hazardous situation of the device, the harm it can lead to and the parameters of eq. (1)."""

    key: str
    hazardous_situation: str
    harm: str
    severity: int
    p2_harm_given_situation: float

    def __post_init__(self):
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(f"{self.key}: severity must be one of {sorted(SEVERITY_LEVELS)}")
        if not 0.0 <= self.p2_harm_given_situation <= 1.0:
            raise ValueError(f"{self.key}: P2 must be within [0, 1]")


@dataclass(frozen=True)
class RiskEstimate:
    hazard: Hazard
    situations_per_device_year: float
    harm_per_device_year: float
    level: str
    acceptable: bool


def evaluate(hazard: Hazard, situations_per_device_year: float) -> RiskEstimate:
    harm = situations_per_device_year * hazard.p2_harm_given_situation
    level = probability_level(harm)
    return RiskEstimate(hazard, situations_per_device_year, harm, level, acceptable(hazard.severity, level))

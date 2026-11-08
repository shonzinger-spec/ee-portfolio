"""Tissue optics for a two-wavelength transmission pulse oximeter.

Modified Beer-Lambert law. The light that reaches the photodiode at wavelength lam is

    I_lam(t) = I0_lam * exp(-A_lam(t))                                         (1)

    A_lam(t) = A_static_lam + DPF_lam * d_art(t) * [eps_HbO2_lam * C_HbO2 + eps_Hb_lam * C_Hb]

where d_art(t) is the pulsatile arterial path length (cm), C_* are molar concentrations (mol/L),
eps_* are molar extinction coefficients (1/(cm*M), natural-log base) and DPF_lam is the differential
path-length factor that accounts for scattering (the photons travel further than the geometric path).

The pulsatile change in absorbance over one beat is

    dA_lam = DPF_lam * dd * C_tot * [eps_Hb_lam + S * (eps_HbO2_lam - eps_Hb_lam)]          (2)

with S = C_HbO2 / C_tot the arterial oxygen saturation (fraction, 0..1). For small dA the normalised
pulse amplitude AC/DC equals dA (1 - exp(-dA) ~ dA), so the ratio of ratios is

    R = (AC_red / DC_red) / (AC_ir / DC_ir) = dA_red / dA_ir
      = kappa * [eps_Hb_r + S (eps_HbO2_r - eps_Hb_r)] / [eps_Hb_ir + S (eps_HbO2_ir - eps_Hb_ir)]  (3)

kappa = DPF_red / DPF_ir. Path length and concentration cancel, which is why the ratio of ratios
is used. kappa depends on tissue scattering and differs between people; a real oximeter does not
know it, which is why devices are calibrated empirically against arterial blood samples
(CO-oximetry) rather than by inverting (3).
"""

from __future__ import annotations

import numpy as np

# Molar extinction coefficients of haemoglobin, 1/(cm*M), converted from the decadic values tabulated
# by S. Prahl (Oregon Medical Laser Center, "Optical absorption of hemoglobin", compiled 1999)
# with eps_natural = ln(10) * eps_decadic. Only the ratios enter equation (3), so the conversion
# factor cancels; it is kept so that the absorbances in the generator are physically scaled.
LN10 = float(np.log(10.0))
RED_WAVELENGTH_NM = 660.0
IR_WAVELENGTH_NM = 940.0
EPS_HBO2_RED = 319.6 * LN10
EPS_HB_RED = 3226.56 * LN10
EPS_HBO2_IR = 1214.0 * LN10
EPS_HB_IR = 693.44 * LN10

# Nominal DPF_red / DPF_ir. With 1.5 the textbook linear calibration SpO2 = 110 - 25 R falls within
# about 2.3 % SpO2 of equation (3) over 70-100 %; it is a model assumption, not a measured value.
NOMINAL_DPF_RATIO_RED_IR = 1.5


def ratio_of_ratios_from_saturation(saturation_frac, dpf_ratio_red_ir=NOMINAL_DPF_RATIO_RED_IR):
    """Equation (3): ratio of ratios R for arterial saturation S (fraction 0..1)."""
    saturation_frac = np.asarray(saturation_frac, dtype=float)
    if np.any((saturation_frac < 0.0) | (saturation_frac > 1.0)):
        raise ValueError("saturation must be a fraction in [0, 1]")
    if dpf_ratio_red_ir <= 0.0:
        raise ValueError("DPF ratio must be positive")
    absorptivity_red = EPS_HB_RED + saturation_frac * (EPS_HBO2_RED - EPS_HB_RED)
    absorptivity_ir = EPS_HB_IR + saturation_frac * (EPS_HBO2_IR - EPS_HB_IR)
    return dpf_ratio_red_ir * absorptivity_red / absorptivity_ir


def saturation_from_ratio_of_ratios(ratio_r, dpf_ratio_red_ir=NOMINAL_DPF_RATIO_RED_IR):
    """Closed-form inverse of equation (3).

    R * [eps_Hb_ir + S (eps_HbO2_ir - eps_Hb_ir)] = kappa * [eps_Hb_r + S (eps_HbO2_r - eps_Hb_r)]
    => S = (kappa eps_Hb_r - R eps_Hb_ir) / (R (eps_HbO2_ir - eps_Hb_ir) - kappa (eps_HbO2_r - eps_Hb_r))

    Returns the saturation fraction; values outside [0, 1] are returned unclipped so the caller can
    see that R is outside the physical range.
    """
    ratio_r = np.asarray(ratio_r, dtype=float)
    kappa = dpf_ratio_red_ir
    numerator = kappa * EPS_HB_RED - ratio_r * EPS_HB_IR
    denominator = ratio_r * (EPS_HBO2_IR - EPS_HB_IR) - kappa * (EPS_HBO2_RED - EPS_HB_RED)
    return numerator / denominator

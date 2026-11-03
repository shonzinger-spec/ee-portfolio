// adc_sense.h - ADC1 oneshot reads converted to the controller's counts.
#pragma once
#include <stdint.h>

typedef struct {
    uint16_t v, i, vin;          // controller counts, 0..4095 against 3.3 V full scale
    uint16_t raw_v, raw_i, raw_vin;  // ADC raw codes as read
} adc_sample_t;

// ADC1 oneshot unit, 12 dB attenuation, 12 bit, channels 6/7/5; line-fitting calibration
// when the eFuse holds Vref or two-point data, otherwise raw x 3300 / 4095.
void adc_sense_init(void);
// "EFUSE_VREF", "EFUSE_TP" or "RAW" (the path taken at boot); "WOKWI_5V_LINEAR" in the LEDC/Wokwi build
const char *adc_sense_cal_name(void);
// Three conversions; call from the control task only (not ISR safe).
void adc_sense_read(adc_sample_t *s);

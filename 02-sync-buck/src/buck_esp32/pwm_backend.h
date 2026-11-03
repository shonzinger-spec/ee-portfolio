// pwm_backend.h - one small PWM interface, two implementations chosen at compile time:
//   default         : pwm_mcpwm.cpp (the product, 200 kHz, 80 MHz MCPWM timer)
//   -DBUCK_PWM_LEDC : pwm_ledc.cpp  (Wokwi build; Wokwi has no MCPWM on the classic ESP32)
// All calls are made from the control task only.
#pragma once
#include <stdint.h>

// Configure the outputs and hold BOTH gates low. Call first thing in setup().
void pwm_init(void);
// Load a new high-side on-time in controller ticks (0..BUCK_PWM_TICKS, 12.5 ns each).
// Returns false if a driver call failed. Takes effect at a PWM period boundary. While the outputs are still held (before
// pwm_enable) the value is only stored.
bool pwm_set(uint16_t duty_ticks);
// Release the hold. Call one control period after the first pwm_set, so the first switching
// period already uses the controller's duty.
// Returns false if the hold could not be released or the backend refuses to run; the caller
// must then treat it as a fault (the outputs may still be held low).
bool pwm_enable(void);
// Both gates low immediately; latched until reset (no call releases it).
// Returns false if a driver call failed (the caller reports it; there is nothing more to try).
bool pwm_off(void);
// nullptr, or a warning string for the boot line (the LEDC backend is a simulator build).
const char *pwm_note(void);
// "MCPWM" or "LEDC" for the boot line.
const char *pwm_name(void);
// Switching frequency of this backend in Hz, for the boot line.
uint32_t pwm_fsw_hz(void);

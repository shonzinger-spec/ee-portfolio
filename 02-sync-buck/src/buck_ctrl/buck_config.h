/* buck_config.h - design constants for the 02 synchronous buck controller.
 * One source of truth: main.py and test/host read the numbers below.
 * Every constant is guarded with #ifndef so a test build can override it with -D. */
#ifndef BUCK_CONFIG_H
#define BUCK_CONFIG_H

/* Power stage (12 V -> 5 V / 3 A) */
#define BUCK_VIN_NOM_V        12.0f
#define BUCK_VOUT_REF_V        5.0f
#define BUCK_FSW_HZ       200000.0f
/* ESP32 MCPWM timer at 80 MHz: 400 ticks per 200 kHz period (0.25 % duty LSB, 30 mV at 12 V) */
#define BUCK_PWM_TICKS          400
#define BUCK_DEADTIME_TICKS       4   /* 50 ns at 12.5 ns per tick, both edges */
#define BUCK_CTRL_DIV             4   /* control update every 4th PWM period */
#define BUCK_TS_S        (BUCK_CTRL_DIV / BUCK_FSW_HZ)   /* 20 us, 50 kHz loop */

/* Sensing: ESP32 ADC1, 12 bit, 3.3 V full scale (calibrated). 10k / 6.8k divider on VLOAD,
 * INA181A3 (gain 100) on a 5 mOhm shunt = 0.5 V/A, 47k / 10k divider on VIN for feed-forward */
#define BUCK_ADC_FS_V          3.3f
#define BUCK_ADC_COUNTS        4096
#define BUCK_VDIV_RATIO        (6.8f / (10.0f + 6.8f))
#define BUCK_ISENSE_V_PER_A    0.5f
#define BUCK_V_PER_COUNT       (BUCK_ADC_FS_V / BUCK_ADC_COUNTS / BUCK_VDIV_RATIO)
#define BUCK_A_PER_COUNT       (BUCK_ADC_FS_V / BUCK_ADC_COUNTS / BUCK_ISENSE_V_PER_A)
#define BUCK_VINDIV_RATIO      (10.0f / (47.0f + 10.0f))
#define BUCK_VIN_PER_COUNT     (BUCK_ADC_FS_V / BUCK_ADC_COUNTS / BUCK_VINDIV_RATIO)
#define BUCK_VIN_FF_MIN_V      6.0f     /* feed-forward floor (driver UVLO is about 7 V) */

/* PI loop on volts with two feed-forward terms: u = Vref + PI(e) is the wanted average
 * switch-node voltage (reference feed-forward) and duty = u / Vin (input feed-forward), so the
 * loop gain does not change with Vin and soft start tracks the ramp without a lag.
 * Crossover about 130 Hz, gain margin >= 7.5 dB at every corner (see main.py). */
#ifndef BUCK_KP
#define BUCK_KP                0.02f    /* volt per volt */
#endif
#ifndef BUCK_KI
#define BUCK_KI                800.0f   /* volt per volt-second */
#endif
#define BUCK_DUTY_MIN          0.0f
#define BUCK_DUTY_MAX          0.90f    /* leaves time to recharge the bootstrap capacitor */
/* Integrator dead band: half a DPWM step at Vout is 15 mV; the ADC step is 2 mV, so without
 * this band the integrator hunts between two duty codes (limit cycle). */
#ifndef BUCK_DEADBAND_V
#define BUCK_DEADBAND_V        0.020f
#endif
/* DPWM hysteresis: the duty code moves only when the PI output is more than 0.75 LSB away
 * from it. With plain rounding (0.5 LSB), one-LSB chatter (30 mV) excites the LC resonance
 * (Q about 4) and a 3 A -> 0.5 A release rings for about 10 ms instead of 2 ms. */
#ifndef BUCK_DPWM_HYST_LSB
#define BUCK_DPWM_HYST_LSB     0.75f
#endif

/* Soft start: reference ramps from the measured output at 1 V/ms, then approaches 5 V with a
 * 0.4 ms time constant (about 7 ms from 0 V to within 1 mV) */
#define BUCK_SOFTSTART_V_PER_S 1000.0f
#define BUCK_SOFTSTART_TAU_S   0.4e-3f
#define BUCK_SOFTSTART_DONE_V  0.001f

/* Over-current: latch off after 3 consecutive samples above 4.5 A (60 us).
 * A hard short needs a hardware comparator into the MCPWM fault input; not modelled. */
#ifndef BUCK_OCP_TRIP_A
#define BUCK_OCP_TRIP_A        4.5f
#endif
#define BUCK_OCP_SAMPLES          3

#endif /* BUCK_CONFIG_H */

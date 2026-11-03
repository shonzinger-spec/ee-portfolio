/* BLDC controller limits and tuning. main.py and the host test read these values.
 * Every value can be overridden with -D (the negative gate builds broken controllers that way). */
#ifndef BLDC_CONFIG_H
#define BLDC_CONFIG_H

/* Timer: centre-aligned (up/down) counter, two compare registers per half-bridge.
 * f_pwm = TIM_CLK / (2 * ARR) = 32 MHz / 1600 = 20 kHz; one tick = 31.25 ns. */
#ifndef BLDC_TIM_CLK_HZ
#define BLDC_TIM_CLK_HZ     32000000L
#endif
#ifndef BLDC_TIM_ARR
#define BLDC_TIM_ARR        800
#endif
#define BLDC_PWM_HZ         (BLDC_TIM_CLK_HZ / (2L * BLDC_TIM_ARR))

/* Dead time between one switch of a leg turning off and the other turning on. 500 ns covers
 * the MOSFET turn-off delay minus turn-on delay found in sim/ngspice/leg_deadtime.cir. */
#ifndef BLDC_DT_NS
#define BLDC_DT_NS          500
#endif
#define BLDC_DT_TICKS       ((int)((BLDC_DT_NS * (BLDC_TIM_CLK_HZ / 1000000L) + 999) / 1000))

/* Highest duty: the low-side switch must conduct for part of every period so that the
 * bootstrap capacitor of the high-side driver recharges. */
#ifndef BLDC_DUTY_MAX
#define BLDC_DUTY_MAX       0.95f
#endif

/* Lowest duty while running, and the shortest pulse whose current sample is trusted. At 0.03
 * the high-side gate is on for 33 ticks centred on the counter peak, so the ADC samples 16 ticks
 * (500 ns) after the gate edge; the switch conducts within 215 ns of it (ngspice) and the shunt
 * amplifier has about 285 ns to settle. Shorter pulses keep the previous reading. */
#ifndef BLDC_DUTY_MIN_SAMPLE
#define BLDC_DUTY_MIN_SAMPLE 0.03f
#endif

/* Motor (used for the speed calculation and the back-EMF feed-forward only) */
#ifndef BLDC_POLE_PAIRS
#define BLDC_POLE_PAIRS     4
#endif
#ifndef BLDC_KE_LL
#define BLDC_KE_LL          0.070f    /* line-to-line back-EMF on the flat top, V per rad/s */
#endif

/* Commutation: 0 = Hall table aligned to the back-EMF flat tops. The negative gate sets 1
 * (the table one sector early) to show that the test notices a wrong commutation table. */
#ifndef BLDC_COMM_ADVANCE
#define BLDC_COMM_ADVANCE   0
#endif

/* Current sense: 2.5 mOhm low-side DC-link shunt, INA181A1 (gain 20) -> 50 mV/A,
 * 1.65 V offset, 12-bit ADC on 3.3 V, triggered at the counter peak (centre of the ON pulse). */
#define BLDC_ADC_FULL       4096
#define BLDC_ADC_VREF_MV    3300
#define BLDC_ISENSE_MV_PER_A 50
#define BLDC_ISENSE_ZERO    2048
/* Bus voltage divider 100k / 10k */
#define BLDC_VBUS_DIV       11

/* Speed and current loops */
#ifndef BLDC_SPEED_DIV
#define BLDC_SPEED_DIV      20        /* speed loop every 20 PWM periods = 1 kHz */
#endif
#ifndef BLDC_RAMP_RPM_S
#define BLDC_RAMP_RPM_S     4000.0f   /* speed reference slew rate */
#endif
#ifndef BLDC_SPD_KP
#define BLDC_SPD_KP         0.0080f   /* A per rpm (twice this limit-cycles: make gains) */
#endif
#ifndef BLDC_SPD_KI
#define BLDC_SPD_KI         0.120f    /* A per rpm per second */
#endif
#ifndef BLDC_CUR_KP
#define BLDC_CUR_KP         0.20f     /* duty per A (about 1 kHz current-loop bandwidth) */
#endif
#ifndef BLDC_CUR_KI
#define BLDC_CUR_KI         200.0f    /* duty per A per second (zero at R/L) */
#endif

/* Protection */
#ifndef BLDC_I_LIMIT_A
#define BLDC_I_LIMIT_A      8.0f      /* current reference clamp (0.56 N m) */
#endif
#ifndef BLDC_OC_TRIP_A
#define BLDC_OC_TRIP_A      20.0f     /* sampled DC-link current: all gates off at once */
#endif
#ifndef BLDC_STALL_MS
#define BLDC_STALL_MS       500       /* torque demanded but no Hall edge for this long */
#endif
#ifndef BLDC_HALL_CHECK
#define BLDC_HALL_CHECK     1         /* Hall code 000 or 111 on two samples in a row = fault */
#endif
#define BLDC_SPEED_TIMEOUT_MS 100     /* no Hall edge for this long: measured speed is 0 */
#define BLDC_SPEED_WINDOW_US 10000    /* average the Hall intervals of the last 10 ms (at least
                                         one, at most six = one electrical revolution) */

#endif

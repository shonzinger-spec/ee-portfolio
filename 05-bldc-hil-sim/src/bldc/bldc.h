/* Sensored 6-step BLDC controller: PWM update interrupt, commutation, cascaded speed and
 * current loops, software dead-time insertion and protection. Portable C99; the only
 * hardware contact is the bldc_io_t block below, which a target port maps to its timer,
 * ADC and GPIO registers (and the host test maps to the plant model). */
#ifndef BLDC_H
#define BLDC_H

#include <stdint.h>
#include "bldc_config.h"

/* Compare value that the counter never reaches: the output stays in its "below" state. */
#define BLDC_CCR_NEVER  (BLDC_TIM_ARR + 1)

enum { BLDC_IDLE = 0, BLDC_RUN = 1, BLDC_FAULT = 2 };
enum { BLDC_F_OC = 1, BLDC_F_HALL = 2, BLDC_F_STALL = 4 };

/* Per leg x (A, B, C), with the counter cnt running 0 -> ARR -> 0:
 *   high-side gate on while cnt >= ccr_h[x]
 *   low-side gate on  while cnt <  ccr_l[x]
 * The registers are preloaded: values written in the ISR take effect at the next update
 * event (counter = 0), so a write in the middle of a period never cuts a pulse short.
 * moe = 0 (main output enable) forces all six gates off at once, without waiting. */
typedef struct {
    /* inputs, latched by the hardware before the ISR runs */
    uint8_t  hall;          /* bit0 = HA, bit1 = HB, bit2 = HC (GPIO, read at the update event) */
    uint16_t adc_ibus;      /* DC-link shunt current, sampled at the last counter peak */
    uint16_t adc_vbus;      /* bus voltage divider */
    uint32_t now_us;        /* 1 MHz free-running timer, read at the update event (wraps) */
    uint32_t hall_edge_us;  /* capture of the latest Hall edge on the same timer */
    /* outputs: preload registers and the output enable */
    uint16_t ccr_h[3];
    uint16_t ccr_l[3];
    uint8_t  moe;
} bldc_io_t;

typedef struct {
    int      state;
    unsigned faults;
    float    rpm_cmd;       /* set by the application */
    float    rpm_ref;       /* ramped reference */
    float    rpm;           /* measured from the Hall edges */
    float    i_ref, i_meas; /* A */
    float    duty;
    float    duty_out;      /* duty written to the registers by this ISR (0 if outputs off) */
    float    duty_prev;     /* ... by the previous ISR: in force during the next sample */
    float    vbus;
    float    spd_int, cur_int;
    uint8_t  hall_last;
    uint8_t  hall_bad;
    int      sector;        /* 0..5, -1 = none */
    uint32_t edge_last_us;
    uint32_t period_us[6];  /* the last six Hall edge intervals (one electrical revolution) */
    int      n_periods;
    uint32_t ticks;         /* ISR count */
    uint32_t speed_div;
    uint32_t stall_periods;  /* PWM periods with torque demanded and no Hall edge */
} bldc_t;

void bldc_init(bldc_t *m, bldc_io_t *io);
void bldc_set_speed(bldc_t *m, float rpm);
/* The PWM update (counter underflow) interrupt, BLDC_PWM_HZ. */
void bldc_pwm_isr(bldc_t *m, bldc_io_t *io);

/* Hall code -> sector 0..5 (-1 for 000 / 111), and the leg pattern of a sector:
 * high[s] is driven with PWM, low[s] has its low-side switch on, the third leg floats. */
int  bldc_hall_sector(uint8_t hall);
extern const int bldc_sector_high[6], bldc_sector_low[6];

#endif

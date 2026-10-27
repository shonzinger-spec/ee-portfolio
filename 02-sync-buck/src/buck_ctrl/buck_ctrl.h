/* buck_ctrl.h - digital PI voltage-mode controller for the 02 synchronous buck.
 * Portable C99, no hardware access: the caller samples the ADC and loads the PWM compare. */
#ifndef BUCK_CTRL_H
#define BUCK_CTRL_H
#include <stdint.h>

typedef enum { BUCK_OFF = 0, BUCK_SOFTSTART, BUCK_RUN, BUCK_FAULT } buck_state_t;
typedef enum { BUCK_FAULT_NONE = 0, BUCK_FAULT_OCP } buck_fault_t;

typedef struct {
    buck_state_t state;
    buck_fault_t fault;
    float vref;        /* present reference, V (ramps during soft start) */
    float integ;       /* integrator state, V (correction added to the Vref feed-forward) */
    uint16_t duty;     /* last commanded high-side on-time, PWM ticks */
    uint8_t oc_count;  /* consecutive over-current samples */
} buck_ctrl_t;

void buck_init(buck_ctrl_t *c);
/* Begin soft start from the present output voltage (pre-bias safe). */
void buck_start(buck_ctrl_t *c, uint16_t adc_v);
/* One control update (every BUCK_TS_S) from raw ADC counts of VLOAD, load current and VIN.
 * Returns the duty in ticks to load at the next period boundary; 0 with state BUCK_FAULT or
 * BUCK_OFF means both switches off. */
uint16_t buck_update(buck_ctrl_t *c, uint16_t adc_v, uint16_t adc_i, uint16_t adc_vin);

#endif /* BUCK_CTRL_H */

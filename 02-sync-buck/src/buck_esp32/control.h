// control.h - 50 kHz control loop: gptimer ISR -> task notification -> control task on core 1.
#pragma once

// Create the control task (core 1). Call after pwm_init(), adc_sense_init() and telem_start().
void control_start(void);

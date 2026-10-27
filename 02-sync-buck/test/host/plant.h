/* plant.h - averaged model of the 02 synchronous buck power stage for host tests.
 * Same component values as sim/buck_sync.cir; switching ripple and dead time are not
 * modelled (the SPICE decks cover those). */
#ifndef PLANT_H
#define PLANT_H
#include <stdint.h>

typedef struct {
    double vin;      /* input voltage, V */
    double rload;    /* load resistance, ohm */
    double il;       /* inductor current, A */
    double vc;       /* output capacitor voltage (behind its ESR), V */
    double vaa;      /* ADC pin voltage after the divider + 1 nF anti-alias filter, V */
} plant_t;

void plant_init(plant_t *p, double vin, double rload);
void plant_step(plant_t *p, double duty, double dt);   /* duty 0..1, held over dt */
double plant_vout(const plant_t *p);                   /* capacitor node */
double plant_vload(const plant_t *p);                  /* regulation point, after the shunt */
double plant_iload(const plant_t *p);
double plant_il_peak(const plant_t *p, double duty);   /* average + half ripple */
uint16_t plant_adc_v(const plant_t *p);
uint16_t plant_adc_i(const plant_t *p);
uint16_t plant_adc_vin(const plant_t *p);  /* 47k/10k divider; its 8 us filter is neglected */

#endif

/* plant.c - averaged buck model: L = 15 uH (DCR 20 mOhm + 6.8 mOhm switch), C = 480 uF
 * (470 uF polymer + 10 uF ceramic, 10 mOhm ESR), 5 mOhm shunt, resistive load. */
#include "plant.h"
#include <math.h>
#include "buck_config.h"

#define L_H     15e-6
#define R_SER   (0.020 + 0.0068)
#define C_F     480e-6
#define ESR     0.010
#define R_SH    0.005
#define TAU_AA  (10e3 * 6.8e3 / 16.8e3 * 1e-9)   /* divider Thevenin x 1 nF = 4.05 us */

void plant_init(plant_t *p, double vin, double rload)
{
    p->vin = vin; p->rload = rload; p->il = 0.0; p->vc = 0.0; p->vaa = 0.0;
}

static double vout_of(double il, double vc, double rload)
{
    const double rl = R_SH + rload;
    return (il + vc / ESR) / (1.0 / ESR + 1.0 / rl);
}

double plant_vout(const plant_t *p) { return vout_of(p->il, p->vc, p->rload); }
double plant_iload(const plant_t *p) { return plant_vout(p) / (R_SH + p->rload); }
double plant_vload(const plant_t *p) { return plant_iload(p) * p->rload; }

static void deriv(const plant_t *p, double il, double vc, double duty, double *dil, double *dvc)
{
    const double vo = vout_of(il, vc, p->rload);
    *dil = (duty * p->vin - il * R_SER - vo) / L_H;
    *dvc = (il - vo / (R_SH + p->rload)) / C_F;
}

void plant_step(plant_t *p, double duty, double dt)
{
    double k1i, k1v, k2i, k2v, k3i, k3v, k4i, k4v;
    deriv(p, p->il, p->vc, duty, &k1i, &k1v);
    deriv(p, p->il + 0.5 * dt * k1i, p->vc + 0.5 * dt * k1v, duty, &k2i, &k2v);
    deriv(p, p->il + 0.5 * dt * k2i, p->vc + 0.5 * dt * k2v, duty, &k3i, &k3v);
    deriv(p, p->il + dt * k3i, p->vc + dt * k3v, duty, &k4i, &k4v);
    p->il += dt / 6.0 * (k1i + 2 * k2i + 2 * k3i + k4i);
    p->vc += dt / 6.0 * (k1v + 2 * k2v + 2 * k3v + k4v);
    const double target = plant_vload(p) * BUCK_VDIV_RATIO;
    p->vaa += (target - p->vaa) * (1.0 - exp(-dt / TAU_AA));
}

double plant_il_peak(const plant_t *p, double duty)
{
    const double vo = plant_vout(p);
    const double ripple = (p->vin - vo) * duty / (L_H * BUCK_FSW_HZ);
    return p->il + 0.5 * ripple;
}

static uint16_t adc(double v)
{
    double c = floor(v / BUCK_ADC_FS_V * BUCK_ADC_COUNTS + 0.5);
    if (c < 0) c = 0;
    if (c > BUCK_ADC_COUNTS - 1) c = BUCK_ADC_COUNTS - 1;
    return (uint16_t)c;
}

uint16_t plant_adc_v(const plant_t *p) { return adc(p->vaa); }
uint16_t plant_adc_i(const plant_t *p)
{
    const double i = plant_iload(p);
    return adc(i > 0 ? i * BUCK_ISENSE_V_PER_A : 0.0);
}

uint16_t plant_adc_vin(const plant_t *p) { return adc(p->vin * BUCK_VINDIV_RATIO); }

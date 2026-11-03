/* Switch-level plant: timer, MOSFETs with delays and body diodes, BLDC motor. See plant.h. */
#include <math.h>
#include <string.h>
#include "plant.h"

#define DT (1.0 / (double)BLDC_TIM_CLK_HZ)
#define TWO_PI 6.283185307179586
#define I_EPS 1e-9

enum { OPEN = 0, HS, LS, SHOOT, D_LOW, D_HIGH };

double plant_trap(double deg)
{
    double a = fmod(deg, 360.0);
    if (a < 0.0)
        a += 360.0;
    if (a < 30.0)
        return a / 30.0;                     /* rising, 330 .. 30 deg */
    if (a <= 150.0)
        return 1.0;
    if (a < 210.0)
        return 1.0 - (a - 150.0) / 30.0;
    if (a <= 330.0)
        return -1.0;
    return -1.0 + (a - 330.0) / 30.0;
}

static uint8_t hall_code(const plant_t *p)
{
    static const double err[3] = {PL_HALL_ERR_A, PL_HALL_ERR_B, PL_HALL_ERR_C};
    double el = p->theta * PL_POLE_PAIRS * 360.0 / TWO_PI;
    uint8_t code = 0;
    int x;
    for (x = 0; x < 3; x++) {
        double s = fmod(el + 30.0 - 120.0 * x - err[x], 360.0);
        if (s < 0.0)
            s += 360.0;
        if (s < 180.0)
            code |= (uint8_t)(1u << x);
    }
    return (uint8_t)((code & p->hall_and) | p->hall_or);
}

static uint16_t adc12(double volts)
{
    long c = lround(volts / (BLDC_ADC_VREF_MV / 1000.0) * BLDC_ADC_FULL);
    return (uint16_t)(c < 0 ? 0 : (c > BLDC_ADC_FULL - 1 ? BLDC_ADC_FULL - 1 : c));
}

void plant_init(plant_t *p, bldc_io_t *io)
{
    memset(p, 0, sizeof *p);
    p->up = 1;
    p->hall_and = 7;
    p->min_gap_ticks = UINT64_MAX;
    for (int x = 0; x < 3; x++) {
        p->ccr_h[x] = BLDC_CCR_NEVER;
        p->ccr_l[x] = 0;
    }
    p->hall = hall_code(p);
    io->hall = p->hall;
    io->adc_ibus = adc12(BLDC_ISENSE_ZERO * BLDC_ADC_VREF_MV / 1000.0 / BLDC_ADC_FULL);
    io->adc_vbus = adc12(PL_VBUS / BLDC_VBUS_DIV);
}

double plant_rpm(const plant_t *p)
{
    return p->w * 60.0 / TWO_PI;
}

/* Gate -> conducting after PL_TD_ON / PL_TD_OFF ticks. Records the both-off gap that precedes
 * a switch turning on while its partner was the last to conduct. */
static void switch_delay(plant_t *p, const int *gate, int *sw, int *pend, uint64_t *off_self,
                         const uint64_t *off_other, const int *sw_other)
{
    if (*gate == *sw) {
        *pend = 0;
        return;
    }
    if (++*pend < (*gate ? PL_TD_ON : PL_TD_OFF))
        return;
    *pend = 0;
    *sw = *gate;
    if (!*sw) {
        *off_self = p->tick;
    } else if (!*sw_other && *off_other > *off_self) {
        uint64_t gap = p->tick - *off_other;
        if (gap < p->min_gap_ticks)
            p->min_gap_ticks = gap;
    }
}

static void electrical(plant_t *p)
{
    int mode[3], x, k = 0, again;
    double v[3], vn = 0.0, el = p->theta * PL_POLE_PAIRS * 360.0 / TWO_PI, i_st = 0.0;

    for (x = 0; x < 3; x++) {
        p->e[x] = PL_KE_PH * p->w * plant_trap(el - 120.0 * x);
        if (p->sw_h[x] && p->sw_l[x]) {
            mode[x] = SHOOT;
            v[x] = PL_VBUS / 2.0;
            i_st += PL_VBUS / (2.0 * PL_RDS_ON + PL_R_LOOP);
            if (p->overlap_ticks++ == 0)
                p->t_overlap0 = p->t;
        } else if (p->sw_h[x]) {
            mode[x] = HS;
            v[x] = PL_VBUS - p->i[x] * PL_RDS_ON;
        } else if (p->sw_l[x]) {
            mode[x] = LS;
            v[x] = -p->i[x] * PL_RDS_ON;
        } else if (p->i[x] > I_EPS) {
            mode[x] = D_LOW;
            v[x] = -PL_VF;
        } else if (p->i[x] < -I_EPS) {
            mode[x] = D_HIGH;
            v[x] = PL_VBUS + PL_VF;
        } else {
            mode[x] = OPEN;
            v[x] = 0.0;
            p->i[x] = 0.0;
        }
    }
    if (i_st > p->i_st_peak)
        p->i_st_peak = i_st;

    /* Neutral voltage from the connected phases (sum of di/dt over them is zero); an open
     * phase whose terminal would pass a rail picks up its body diode, then repeat once. */
    for (again = 0; again < 2; again++) {
        double sum = 0.0;
        k = 0;
        for (x = 0; x < 3; x++)
            if (mode[x] != OPEN) {
                sum += v[x] - p->e[x] - PL_R_PH * p->i[x];
                k++;
            }
        vn = k >= 2 ? sum / k : PL_VBUS / 2.0;
        if (k < 2)
            break;
        int changed = 0;
        for (x = 0; x < 3; x++) {
            if (mode[x] != OPEN)
                continue;
            if (p->e[x] + vn > PL_VBUS + PL_VF) {
                mode[x] = D_HIGH;
                v[x] = PL_VBUS + PL_VF;
                changed = 1;
            } else if (p->e[x] + vn < -PL_VF) {
                mode[x] = D_LOW;
                v[x] = -PL_VF;
                changed = 1;
            }
        }
        if (!changed)
            break;
    }

    /* Integrate the connected phases; a diode stops at zero current */
    if (k < 2) {
        for (x = 0; x < 3; x++)
            p->i[x] = 0.0;
    } else {
        double resid = 0.0;
        int live = 0, clamped[3] = {0, 0, 0};
        for (x = 0; x < 3; x++) {
            if (mode[x] == OPEN)
                continue;
            double inew = p->i[x] + (v[x] - p->e[x] - PL_R_PH * p->i[x] - vn) / PL_L_PH * DT;
            if ((mode[x] == D_LOW && inew <= 0.0) || (mode[x] == D_HIGH && inew >= 0.0)) {
                inew = 0.0;
                clamped[x] = 1;
            }
            p->i[x] = inew;
        }
        for (x = 0; x < 3; x++) {
            resid += p->i[x];
            if (mode[x] != OPEN && !clamped[x])
                live++;
        }
        if (live > 0)
            for (x = 0; x < 3; x++)
                if (mode[x] != OPEN && !clamped[x])
                    p->i[x] -= resid / live;
    }
    p->vn = vn;

    p->i_shunt = i_st;
    p->i_src = i_st;
    for (x = 0; x < 3; x++) {
        p->vt[x] = mode[x] == OPEN ? p->e[x] + vn : v[x];
        if (mode[x] == LS || mode[x] == D_LOW)
            p->i_shunt -= p->i[x];
        if (mode[x] == HS || mode[x] == D_HIGH)
            p->i_src += p->i[x];
    }
    p->torque = 0.0;
    for (x = 0; x < 3; x++)
        p->torque += PL_KE_PH * plant_trap(el - 120.0 * x) * p->i[x];
}

static void mechanical(plant_t *p)
{
    if (p->hold) {
        p->theta += p->w * DT;               /* speed imposed by the test (0 = locked rotor) */
        return;
    }
    /* The load is reactive (brake, conveyor, pump): like Coulomb friction it opposes motion and
     * cannot turn the rotor backwards. */
    double tc = PL_TC + p->t_load;
    double net = p->torque - PL_B * p->w;
    if (fabs(p->w) < 1e-9 && fabs(net) <= tc) {
        p->w = 0.0;
        return;
    }
    double dir = p->w > 1e-9 ? 1.0 : (p->w < -1e-9 ? -1.0 : (net > 0.0 ? 1.0 : -1.0));
    double wn = p->w + (net - tc * dir) / PL_J * DT;
    if (p->w != 0.0 && wn * p->w < 0.0)
        wn = 0.0;                            /* friction stops the rotor, it does not reverse it */
    p->w = wn;
    p->theta += p->w * DT;
}

int plant_tick(plant_t *p, bldc_io_t *io)
{
    int x, event = 0;

    p->tick++;
    p->t = (double)p->tick * DT;
    if (p->up) {
        if (++p->cnt >= BLDC_TIM_ARR)
            p->up = 0;
    } else if (--p->cnt <= 0) {
        p->up = 1;
    }
    if (p->cnt == 0) {
        for (x = 0; x < 3; x++) {
            p->ccr_h[x] = io->ccr_h[x];
            p->ccr_l[x] = io->ccr_l[x];
        }
        io->hall = p->hall;
        io->hall_edge_us = p->hall_edge_us;
        io->now_us = (uint32_t)(p->t * 1e6) + p->us_offset;
        io->adc_vbus = adc12(PL_VBUS / BLDC_VBUS_DIV);
        event = 1;
    }
    if (p->cnt == BLDC_TIM_ARR)
        io->adc_ibus = adc12((BLDC_ISENSE_ZERO * (double)BLDC_ADC_VREF_MV / BLDC_ADC_FULL
                              + p->i_shunt * BLDC_ISENSE_MV_PER_A) / 1000.0);

    for (x = 0; x < 3; x++) {
        p->gate_h[x] = io->moe && p->cnt >= p->ccr_h[x];
        p->gate_l[x] = io->moe && p->cnt < p->ccr_l[x];
        switch_delay(p, &p->gate_h[x], &p->sw_h[x], &p->pend_h[x], &p->off_h[x], &p->off_l[x], &p->sw_l[x]);
        switch_delay(p, &p->gate_l[x], &p->sw_l[x], &p->pend_l[x], &p->off_l[x], &p->off_h[x], &p->sw_h[x]);
        if (p->ls_stuck_on[x])
            p->sw_l[x] = 1;
    }

    electrical(p);
    p->e_in += PL_VBUS * p->i_src * DT;
    p->e_mech += p->torque * p->w * DT;
    p->e_load += p->t_load * fabs(p->w) * DT;
    mechanical(p);

    uint8_t h = hall_code(p);
    if (h != p->hall) {
        p->hall = h;
        p->hall_edge_us = (uint32_t)(p->t * 1e6) + p->us_offset;
    }
    return event;
}

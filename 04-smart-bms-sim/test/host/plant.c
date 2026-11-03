/* plant.c - see plant.h. Mirrored line for line by the Plant class in main.py. */
#include <math.h>
#include "plant.h"

static const double ocv_tab[11] = { BMS_OCV_TABLE_MV };
static const double q_ah[BMS_NCELLS] = { PLANT_CELL_Q_AH };
static const double r_scale[BMS_NCELLS] = { PLANT_CELL_R_SCALE };

#define RGAS 8.314
#define TK(c) ((c) + 273.15)

double plant_ocv(double soc)
{
    if (soc >= 1.0) return ocv_tab[10] / 1000.0 + PLANT_OC_SLOPE_V * (soc - 1.0);
    if (soc <= 0.0) return ocv_tab[0] / 1000.0 + PLANT_OD_SLOPE_V * soc;
    double x = soc * 10.0;
    int i = (int)x;
    if (i > 9) i = 9;
    return (ocv_tab[i] + (x - i) * (ocv_tab[i + 1] - ocv_tab[i])) / 1000.0;
}

static double r_temp(double t_c)
{
    return exp(PLANT_R_B_K * (1.0 / TK(t_c) - 1.0 / 298.15));
}

void plant_init(plant_t *p, const double soc0[BMS_NCELLS], double t_amb_c)
{
    *p = (plant_t){0};
    for (int i = 0; i < BMS_NCELLS; i++) {
        p->c[i].soc = soc0[i];
        p->c[i].t_c = t_amb_c;
        p->c[i].x_rr = 1.0;
        p->c[i].v_term = plant_ocv(soc0[i]);
    }
    p->t_amb_c = t_amb_c;
    p->rth = PLANT_RTH_K_W;
    p->chg_cc_a = 3.0;
    p->chg_cv_v = 16.8;
    p->chg_term_a = 0.15;
    p->tsense_override_c = -1000.0;
}

static void substep(plant_t *p, double dt, int chg_on, int dsg_on, uint8_t bal_mask)
{
    double e = 0.0, r = 0.0;
    for (int i = 0; i < BMS_NCELLS; i++) {
        const cell_t *c = &p->c[i];
        e += plant_ocv(c->soc) + c->v1 + c->v2;
        r += PLANT_R0_OHM * r_scale[i] * r_temp(c->t_c);
    }

    double ip = 0.0;
    if (p->src == SRC_CHARGER && !p->chg_done) {
        /* The charger regulates on its own; it terminates once it is in CV and sees less than
         * the termination current, also when the BMS has opened the charge FET. */
        if (chg_on) {
            ip = p->chg_cc_a;
            if (!p->chg_faulty) {
                double icv = (p->chg_cv_v - e) / r;
                if (icv < ip) { ip = icv > 0.0 ? icv : 0.0; p->chg_cv = 1; }
            }
        }
        if (!p->chg_faulty && p->chg_cv && ip < p->chg_term_a) {
            p->chg_term_s += dt;
            if (p->chg_term_s >= 1.0) p->chg_done = 1;
        } else {
            p->chg_term_s = 0.0;
        }
    } else if (p->src == SRC_LOAD_I && dsg_on) {
        ip = -p->load_a;
    } else if (p->src == SRC_LOAD_R && dsg_on) {
        ip = -e / (p->load_ohm + r);
    }
    p->i_pack = ip;

    double a_rr = PLANT_RR_Q100_W / (PLANT_RR_H_J * exp(-PLANT_RR_EA_J_MOL / (RGAS * TK(100.0))));
    for (int i = 0; i < BMS_NCELLS; i++) {
        cell_t *c = &p->c[i];
        double ks = r_scale[i] * r_temp(c->t_c);
        double r0 = PLANT_R0_OHM * ks, r1 = PLANT_R1_OHM * ks, r2 = PLANT_R2_OHM * ks;
        double ocv = plant_ocv(c->soc);
        double ic = ip;
        if (bal_mask & (1u << i)) ic -= (ocv + c->v1 + c->v2) / PLANT_BLEED_OHM;
        double vt = ocv + c->v1 + c->v2 + ic * r0;

        double heat = ic * ic * r0 + c->v1 * c->v1 / r1 + c->v2 * c->v2 / r2;
        double f = 0.0;
        if (ic > 0.0) {
            f = (c->soc - 1.0) / PLANT_OC_SIDE_WIDTH;
            f = f < 0.0 ? 0.0 : (f > 1.0 ? 1.0 : f);
            heat += f * ic * vt;
        }
        c->soc += (1.0 - f) * ic * dt / (q_ah[i] * 3600.0);

        double e1 = exp(-dt / PLANT_TAU1_S), e2 = exp(-dt / PLANT_TAU2_S);
        c->v1 = c->v1 * e1 + r1 * ic * (1.0 - e1);
        c->v2 = c->v2 * e2 + r2 * ic * (1.0 - e2);

        double k = a_rr * exp(-PLANT_RR_EA_J_MOL / (RGAS * TK(c->t_c)));
        double dx = c->x_rr * (1.0 - exp(-k * dt));
        c->x_rr -= dx;
        heat += PLANT_RR_H_J * dx / dt;

        c->t_c += dt * (heat - (c->t_c - p->t_amb_c) / p->rth) / PLANT_CTH_J_K;
        c->v_term = vt;
        c->i_a = ic;
    }
}

void plant_step(plant_t *p, int chg_on, int dsg_on, uint8_t bal_mask)
{
    int hot = 0;
    for (int i = 0; i < BMS_NCELLS; i++)
        if (p->c[i].t_c > PLANT_HOT_C) hot = 1;
    int n = hot ? 100 : 1;
    for (int k = 0; k < n; k++)
        substep(p, PLANT_DT_S / n, chg_on, dsg_on, bal_mask);
}

void plant_measure(const plant_t *p, bms_meas_t *m)
{
    for (int i = 0; i < BMS_NCELLS; i++) {
        m->cell_mv[i] = (int32_t)floor(p->c[i].v_term * 1000.0 + 0.5);
        double t = p->tsense_override_c > -273.0 ? p->tsense_override_c : p->c[i].t_c;
        m->temp_dc[i] = (int32_t)floor(t * 10.0 + 0.5);
    }
    m->pack_ma = (int32_t)floor(p->i_pack * 1000.0 + 0.5) + PLANT_I_OFFSET_MA;
}

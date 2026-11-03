/* plant.h - 4S1P pack model for the host test: per cell a second-order Thevenin equivalent
 * circuit (R0 + two RC pairs) with temperature-dependent resistance, a lumped thermal mass,
 * an overcharge side reaction and one lumped exothermic decomposition reaction (thermal
 * runaway). main.py reads the constants below and runs the same model in Python. */
#ifndef PLANT_H
#define PLANT_H

#include <stdint.h>
#include "bms.h"

#define PLANT_CELL_Q_AH        3.00, 2.94, 3.05, 2.97   /* capacity spread about +/-2 % */
#define PLANT_CELL_R_SCALE     1.00, 1.08, 0.96, 1.04   /* resistance spread */
#define PLANT_R0_OHM           0.025    /* at 25 degC */
#define PLANT_R1_OHM           0.015
#define PLANT_TAU1_S           10.0
#define PLANT_R2_OHM           0.020
#define PLANT_TAU2_S           200.0
#define PLANT_R_B_K            2250.0   /* R(T) = R25 exp(B (1/T - 1/298.15)): x2 at 0 degC */
#define PLANT_OC_SLOPE_V       4.0      /* OCV above 100 % SoC rises 4 V per unit SoC */
#define PLANT_OD_SLOPE_V       6.0      /* OCV below 0 % falls 6 V per unit SoC */
#define PLANT_OC_SIDE_WIDTH    0.05     /* side-reaction share of the current 0 -> 1 over 100-105 % */
#define PLANT_CTH_J_K          46.0     /* 46 g cell, 1.0 J/(g K) */
#define PLANT_RTH_K_W          15.0     /* cell to ambient, natural convection in the pack */
#define PLANT_RTH_FAIL_K_W     60.0     /* cooling failure */
#define PLANT_RR_EA_J_MOL      135080.0 /* SEI-decomposition activation energy (literature order) */
#define PLANT_RR_H_J           12000.0  /* heat of the lumped reaction */
#define PLANT_RR_Q100_W        0.30     /* calibration: self-heating 0.3 W at 100 degC */
#define PLANT_BLEED_OHM        33.0
#define PLANT_I_OFFSET_MA      15       /* current-sense offset seen by the BMS */
#define PLANT_DT_S             0.1
#define PLANT_HOT_C            150.0    /* above this the plant sub-steps at 1 ms (runaway) */

typedef enum { SRC_NONE, SRC_CHARGER, SRC_LOAD_I, SRC_LOAD_R } src_t;

typedef struct {
    double soc, v1, v2, t_c, x_rr, v_term, i_a;
} cell_t;

typedef struct {
    cell_t c[BMS_NCELLS];
    double t_amb_c, rth;
    src_t src;
    double chg_cc_a, chg_cv_v, chg_term_a;   /* charger: CC, CV, termination current */
    int chg_faulty;                           /* CV regulation lost: CC regardless of voltage */
    int chg_cv, chg_done;
    double chg_term_s;
    double load_a, load_ohm;
    double i_pack;
    double tsense_override_c;                 /* < -273: no override (open thermistor test) */
} plant_t;

double plant_ocv(double soc);
void plant_init(plant_t *p, const double soc0[BMS_NCELLS], double t_amb_c);
void plant_step(plant_t *p, int chg_on, int dsg_on, uint8_t bal_mask);   /* one PLANT_DT_S */
void plant_measure(const plant_t *p, bms_meas_t *m);

#endif

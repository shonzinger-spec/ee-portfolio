/* bms.h - protection, state machine, state of charge and passive balancing for a 4S Li-ion
 * pack. Portable C99, integer arithmetic only, no MCU dependencies: the caller feeds one AFE
 * scan per BMS_TS_MS and applies the FET and bleed outputs. */
#ifndef BMS_H
#define BMS_H

#include <stdint.h>
#include "bms_config.h"

typedef enum { BMS_STANDBY = 0, BMS_CHARGING = 1, BMS_DISCHARGING = 2, BMS_FAULT = 3 } bms_state_t;

/* Fault flags */
#define BMS_F_OV      0x0001u   /* cell over-voltage          -> charge FET off */
#define BMS_F_UV      0x0002u   /* cell under-voltage         -> discharge FET off */
#define BMS_F_OCC     0x0004u   /* charge over-current        -> charge FET off, latched */
#define BMS_F_OCD     0x0008u   /* discharge over-current     -> discharge FET off, latched */
#define BMS_F_SCD     0x0010u   /* short circuit in discharge -> discharge FET off, latched */
#define BMS_F_OTC     0x0020u   /* too hot to charge          -> charge FET off */
#define BMS_F_OTD     0x0040u   /* too hot to discharge       -> discharge FET off */
#define BMS_F_UTC     0x0080u   /* too cold to charge         -> charge FET off */
#define BMS_F_TSENSE  0x0100u   /* thermistor open or shorted -> both off, latched */

#define BMS_F_BLOCK_CHG (BMS_F_OV | BMS_F_OCC | BMS_F_OTC | BMS_F_OTD | BMS_F_UTC | BMS_F_TSENSE)
#define BMS_F_BLOCK_DSG (BMS_F_UV | BMS_F_OCD | BMS_F_SCD | BMS_F_OTD | BMS_F_TSENSE)

typedef struct {
    int32_t cell_mv[BMS_NCELLS];
    int32_t pack_ma;                 /* + = charge */
    int32_t temp_dc[BMS_NCELLS];     /* 0.1 degC, one thermistor per cell */
} bms_meas_t;

typedef struct {
    uint8_t chg_on, dsg_on;          /* back-to-back charge / discharge FETs */
    uint8_t bal_mask;                /* bit n = bleed resistor of cell n on */
    uint8_t state;                   /* bms_state_t */
    uint16_t faults;
    int16_t soc_pm;                  /* state of charge, 0.1 % */
} bms_out_t;

typedef struct {
    uint16_t faults;
    uint8_t cnt_ov, cnt_uv, cnt_occ, cnt_ocd, cnt_scd, cnt_otc, cnt_otd, cnt_utc, cnt_ts;
    uint8_t cnt_rec_dsg, cnt_rec_chg, cnt_full, cnt_hold, chg_hold;
    uint8_t bal_mask, startup;
    int64_t q_mams;                  /* charge in the pack, mA x ms */
} bms_t;

int32_t bms_soc_from_ocv_pm(int32_t mv);               /* rested cell voltage -> SoC, 0.1 % */
void bms_init(bms_t *b, const bms_meas_t *rested);     /* SoC from the lowest rested cell */
void bms_step(bms_t *b, const bms_meas_t *m, bms_out_t *out);

#endif

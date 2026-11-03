/* bms.c - see bms.h. Called once per BMS_TS_MS with a fresh AFE scan. */
#include "bms.h"

static const int32_t ocv_mv[11] = { BMS_OCV_TABLE_MV };

#define CAP_MAMS ((int64_t)BMS_CAP_MAH * 3600 * 1000)

int32_t bms_soc_from_ocv_pm(int32_t mv)
{
    if (mv <= ocv_mv[0]) return 0;
    if (mv >= ocv_mv[10]) return 1000;
    int i = 0;
    while (mv >= ocv_mv[i + 1]) i++;
    return 100 * i + 100 * (mv - ocv_mv[i]) / (ocv_mv[i + 1] - ocv_mv[i]);
}

void bms_init(bms_t *b, const bms_meas_t *rested)
{
    int32_t vmin = rested->cell_mv[0];
    for (int i = 1; i < BMS_NCELLS; i++)
        if (rested->cell_mv[i] < vmin) vmin = rested->cell_mv[i];
    *b = (bms_t){0};
    b->q_mams = CAP_MAMS * bms_soc_from_ocv_pm(vmin) / 1000;
}

/* Debounced trip: the flag sets after `delay` consecutive samples with `cond`.
 * A non-latched flag clears as soon as `clear` holds. */
static void trip(bms_t *b, uint16_t flag, uint8_t *cnt, int cond, int delay, int clear)
{
    if (cond) {
        if (*cnt < 255) (*cnt)++;
        if (*cnt >= delay) b->faults |= flag;
    } else {
        *cnt = 0;
        if (clear) b->faults &= (uint16_t)~flag;
    }
}

void bms_step(bms_t *b, const bms_meas_t *m, bms_out_t *out)
{
    int32_t vmax = m->cell_mv[0], vmin = m->cell_mv[0];
    int32_t tmax = m->temp_dc[0], tmin = m->temp_dc[0];
    int tbad = 0;
    for (int i = 0; i < BMS_NCELLS; i++) {
        if (m->cell_mv[i] > vmax) vmax = m->cell_mv[i];
        if (m->cell_mv[i] < vmin) vmin = m->cell_mv[i];
        if (m->temp_dc[i] > tmax) tmax = m->temp_dc[i];
        if (m->temp_dc[i] < tmin) tmin = m->temp_dc[i];
        if (m->temp_dc[i] < BMS_TSENSE_MIN_DC || m->temp_dc[i] > BMS_TSENSE_MAX_DC) tbad = 1;
    }
    int32_t i_ma = m->pack_ma;

    /* Protection. Latched faults pass clear = 0 and are released below. */
    trip(b, BMS_F_TSENSE, &b->cnt_ts, tbad, BMS_TEMP_DELAY, 0);
    trip(b, BMS_F_OV, &b->cnt_ov, vmax >= BMS_OV_TRIP_MV, BMS_OV_DELAY, vmax < BMS_OV_CLEAR_MV);
    trip(b, BMS_F_UV, &b->cnt_uv, vmin <= BMS_UV_TRIP_MV, BMS_UV_DELAY, vmin >= BMS_UV_CLEAR_MV);
    trip(b, BMS_F_OCC, &b->cnt_occ, i_ma >= BMS_OCC_TRIP_MA, BMS_OCC_DELAY, 0);
    trip(b, BMS_F_OCD, &b->cnt_ocd, i_ma <= -BMS_OCD_TRIP_MA, BMS_OCD_DELAY, 0);
    trip(b, BMS_F_SCD, &b->cnt_scd, i_ma <= -BMS_SCD_TRIP_MA, BMS_SCD_DELAY, 0);
    if (!tbad) {
        trip(b, BMS_F_OTC, &b->cnt_otc, tmax >= BMS_OTC_TRIP_DC, BMS_TEMP_DELAY, tmax < BMS_OTC_CLEAR_DC);
        trip(b, BMS_F_OTD, &b->cnt_otd, tmax >= BMS_OTD_TRIP_DC, BMS_TEMP_DELAY, tmax < BMS_OTD_CLEAR_DC);
        trip(b, BMS_F_UTC, &b->cnt_utc, tmin <= BMS_UTC_TRIP_DC, BMS_TEMP_DELAY, tmin >= BMS_UTC_CLEAR_DC);
    }

    /* A charger clears latched discharge faults; a load clears a latched charge fault */
    if ((b->faults & (BMS_F_OCD | BMS_F_SCD)) && i_ma >= BMS_RECOVER_MA) {
        if (++b->cnt_rec_dsg >= BMS_RECOVER_DELAY) {
            b->faults &= (uint16_t)~(BMS_F_OCD | BMS_F_SCD);
            b->cnt_rec_dsg = 0;
        }
    } else {
        b->cnt_rec_dsg = 0;
    }
    if ((b->faults & BMS_F_OCC) && i_ma <= -BMS_RECOVER_MA) {
        if (++b->cnt_rec_chg >= BMS_RECOVER_DELAY) {
            b->faults &= (uint16_t)~BMS_F_OCC;
            b->cnt_rec_chg = 0;
        }
    } else {
        b->cnt_rec_chg = 0;
    }

    /* State of charge: coulomb counting, reset to 100 % at the end of charge taper and
     * to 0 % at the under-voltage trip (both points where the cell voltage is unambiguous) */
    b->q_mams += (int64_t)i_ma * BMS_TS_MS;
    if (b->q_mams < 0) b->q_mams = 0;
    if (b->q_mams > CAP_MAMS) b->q_mams = CAP_MAMS;
    if (vmax >= BMS_FULL_MV && i_ma >= BMS_IDLE_MA && i_ma <= BMS_TAPER_MA) {
        if (++b->cnt_full >= BMS_FULL_DELAY) b->q_mams = CAP_MAMS;
    } else {
        b->cnt_full = 0;
    }
    if (vmax >= BMS_CELL_FULL_MV && i_ma >= BMS_IDLE_MA && i_ma <= BMS_CELL_FULL_MA) {
        if (++b->cnt_hold >= BMS_FULL_DELAY) { b->chg_hold = 1; b->q_mams = CAP_MAMS; }
    } else {
        b->cnt_hold = 0;
    }
    if (b->chg_hold && vmax < BMS_OV_CLEAR_MV) b->chg_hold = 0;
    if (b->faults & BMS_F_UV) b->q_mams = 0;

    bms_state_t st;
    if (b->faults) st = BMS_FAULT;
    else if (i_ma > BMS_IDLE_MA) st = BMS_CHARGING;
    else if (i_ma < -BMS_IDLE_MA) st = BMS_DISCHARGING;
    else st = BMS_STANDBY;

    /* Passive balancing with hysteresis, only while not discharging and near full */
    if (!b->faults && st != BMS_DISCHARGING && vmax >= BMS_BAL_MIN_MV && tmax < BMS_BAL_MAX_DC) {
        for (int i = 0; i < BMS_NCELLS; i++) {
            int32_t d = m->cell_mv[i] - vmin;
            uint8_t bit = (uint8_t)(1u << i);
            if (b->bal_mask & bit) {
                if (d <= BMS_BAL_STOP_MV) b->bal_mask &= (uint8_t)~bit;
            } else if (d >= BMS_BAL_START_MV) {
                b->bal_mask |= bit;
            }
        }
    } else {
        b->bal_mask = 0;
    }

    int ready = b->startup >= BMS_STARTUP_SAMPLES;
    if (!ready) b->startup++;
    out->chg_on = ready && !b->chg_hold && (b->faults & BMS_F_BLOCK_CHG) == 0;
    out->dsg_on = ready && (b->faults & BMS_F_BLOCK_DSG) == 0;
    out->bal_mask = b->bal_mask;
    out->state = (uint8_t)st;
    out->faults = b->faults;
    out->soc_pm = (int16_t)(b->q_mams * 1000 / CAP_MAMS);
}

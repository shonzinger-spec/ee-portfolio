/* bms_config.h - design constants for the 04 smart BMS (4S1P Li-ion, NMC 18650 class, 3.0 Ah).
 * One source of truth: main.py and test/host read the numbers below.
 * Every limit is guarded with #ifndef so a test build can override it with -D. */
#ifndef BMS_CONFIG_H
#define BMS_CONFIG_H

#define BMS_NCELLS              4
#define BMS_TS_MS             100      /* firmware step: 10 Hz, one AFE scan per step */
#define BMS_CAP_MAH          3000      /* nominal cell capacity used by the coulomb counter */
#define BMS_STARTUP_SAMPLES    10      /* both FETs held off for 1 s after reset while every
                                          protection gets a full debounce window of data */

/* Open-circuit voltage of the cell at 0, 10, ..., 100 % state of charge (mV), 25 degC.
 * Generic NMC curve; the plant uses the same table, as a calibrated BMS would. */
#define BMS_OCV_TABLE_MV  3000, 3450, 3560, 3630, 3680, 3740, 3820, 3910, 4000, 4090, 4190

/* Cell voltage limits (mV) and debounce (samples of BMS_TS_MS) */
#ifndef BMS_OV_TRIP_MV
#define BMS_OV_TRIP_MV       4250
#endif
#define BMS_OV_CLEAR_MV      4150
#ifndef BMS_OV_DELAY
#define BMS_OV_DELAY            3      /* 300 ms */
#endif
#define BMS_UV_TRIP_MV       2800
#define BMS_UV_CLEAR_MV      3100
#define BMS_UV_DELAY           10      /* 1 s: rides through load-step sag */

/* Pack current limits (mA, + = charge). SCD at 10 Hz is a firmware backstop only;
 * a real pack needs the AFE's hardware short-circuit comparator (us response). */
#define BMS_OCC_TRIP_MA      4500      /* 1.5 C charge */
#define BMS_OCC_DELAY          10
#ifndef BMS_OCD_TRIP_MA
#define BMS_OCD_TRIP_MA     10000      /* 3.3 C discharge */
#endif
#define BMS_OCD_DELAY           5      /* 500 ms */
#ifndef BMS_SCD_TRIP_MA
#define BMS_SCD_TRIP_MA     40000
#endif
#define BMS_SCD_DELAY           1
/* OCD and SCD latch; a charger (charge current >= this for BMS_RECOVER_DELAY) clears them */
#define BMS_RECOVER_MA        100
#define BMS_RECOVER_DELAY      10

/* Temperature limits (0.1 degC) */
#define BMS_OTC_TRIP_DC       450      /* no charging above 45 degC */
#define BMS_OTC_CLEAR_DC      400
#ifndef BMS_OTD_TRIP_DC
#define BMS_OTD_TRIP_DC       600      /* no discharging above 60 degC */
#endif
#define BMS_OTD_CLEAR_DC      550
#ifndef BMS_UTC_TRIP_DC
#define BMS_UTC_TRIP_DC         0      /* no charging below 0 degC (lithium plating) */
#endif
#define BMS_UTC_CLEAR_DC       30
#define BMS_TEMP_DELAY         10
#define BMS_TSENSE_MIN_DC    -400      /* outside this range the thermistor is open or shorted */
#define BMS_TSENSE_MAX_DC    1200

/* State detection and state of charge */
#define BMS_IDLE_MA            50      /* |I| below this is STANDBY */
#define BMS_FULL_MV          4150      /* full = max cell >= this and charge current <= BMS_TAPER_MA */
#define BMS_TAPER_MA          200      /* C/15: above the charger's C/20 termination */
#define BMS_FULL_DELAY         10
/* Cell-level end of charge: a pack charger regulates 4 x 4.20 V, so in an unbalanced pack the
 * weakest cell passes 4.20 V in the CV phase. Below C/3 (where IR no longer inflates the
 * reading) the BMS opens the charge FET when any cell reaches 4.20 V and treats the pack as
 * full; the hold releases once the highest cell is back below BMS_OV_CLEAR_MV. */
#ifndef BMS_CELL_FULL_MV
#define BMS_CELL_FULL_MV     4200
#endif
#define BMS_CELL_FULL_MA     1000

/* Passive balancing: 33 ohm bleed per cell (about 125 mA at 4.1 V) */
#ifndef BMS_BAL_START_MV
#define BMS_BAL_START_MV        8      /* start when a cell is this far above the lowest */
#endif
#define BMS_BAL_STOP_MV         3
#define BMS_BAL_MIN_MV       3900      /* only near the top of charge, where OCV is steep */
#define BMS_BAL_MAX_DC        500

#endif

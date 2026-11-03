// ============================================================================================
//  buck_esp32.ino - ESP32 firmware for the 02 synchronous buck (12 V -> 5 V / 3 A).
//  Runs the portable controller in src/buck_ctrl (compiled from there, one copy, unmodified:
//  arduino-cli compile ... --library src/buck_ctrl src/buck_esp32).
//
//  Build variants
//    default          MCPWM, 200 kHz, 400 ticks of 12.5 ns, 50 ns hardware dead time (product)
//    -DBUCK_PWM_LEDC  LEDC, 156.25 kHz, 512 counts, 50 ns dead time (Wokwi; see pwm_ledc.cpp)
//
//  Tasks
//    gptimer ISR      every 20 us, integer only: counts overruns, notifies the control task
//    buck_ctrl        core 1, priority configMAX_PRIORITIES-2: ADC -> buck_update -> PWM,
//                     faults, trace and event bookkeeping; never prints
//    telem            core 0, priority 1: the only code that writes to Serial
//
//  Start-up: both gates held low from pwm_init() (first line of setup) until the ADC is up and
//  the controller has produced its first duty; buck_start() uses the measured output (pre-bias
//  safe). Faults (controller OCP latch, or FW_OVERRUN_LIMIT consecutive overruns) force both
//  gates low in the control task and stay latched until reset.
//
//  SERIAL OUTPUT  (921600 baud 8N1, '\n' line ends)
//  Lines that start with a digit are CSV trace rows; every other line starts with a keyword
//  followed by space-separated key=value fields. Times are esp_timer microseconds since boot.
//
//    BOOT fw=buck_esp32 pwm=<MCPWM|LEDC> fsw_hz=<n> ctrl_hz=50000 adc_cal=<EFUSE_TP|EFUSE_VREF|RAW|WOKWI_5V_LINEAR>
//         baud=921600 cpu_mhz=<n> overrun_limit=<n>                    (one line, once)
//    SIM wokwi time_scale=<k> tick_sim_us=<n>   LEDC/Wokwi build only (fw_config.h FW_SIM_WOKWI):
//         the loop runs k times slower in simulated time; every t_us is control time.
//    t_us,vout_V,il_A,vin_V,duty_ticks,state,overruns                  (CSV header, once)
//    <t_us>,<vout_V>,<il_A>,<vin_V>,<duty_ticks>,<state>,<overruns>    (CSV row)
//        values are means of 5 control samples (100 us); duty_ticks = controller ticks of 400;
//        state 0 OFF, 1 SOFTSTART, 2 RUN, 3 FAULT (OCP latch), 4 FW_FAULT (overrun latch);
//        overruns = running total. Background rows every 10 ms; inside a capture window every
//        100 us.
//    CAP begin t_us=<t> reason=<DI|STATE>   capture window opens: the 1 ms of rows before the
//                                           trigger follow, then rows until 4 ms after the last
//                                           trigger. DI = |il| moved > 0.5 A between rows,
//                                           STATE = state changed.
//    CAP end t_us=<t>
//    START t_us=<t> vout_V=<pre-bias V>     buck_start() called
//    READY t_us=<t>                         soft start finished (SOFTSTART -> RUN)
//    SS t_us=<t> t_1pct_us=<n> t_run_us=<n> from START: first row within +/-1 % of 5 V, and RUN
//    STEP t_us=<t0> i_from_A=<A> i_to_A=<A> vmin_V=<V> vmax_V=<V> settle_us=<n>
//        load step seen at t0 (DI trigger in RUN); vmin/vmax over the event; settle_us = time
//        from t0 until Vout is back inside +/-1 % for good (held 2 ms), 0 if it never left,
//        -1 not settled within 50 ms, -2 interrupted by another step or a state change.
//        Resolution 100 us.
//    FAULT <OCP|OVERRUN> t_us=<t> trip_us=<n> vout_V=<V> il_A=<A> vin_V=<V> raw_v=<n>
//          raw_i=<n> raw_vin=<n> duty_ticks=<n> overruns=<n>
//        printed once. OCP: trip_us = first over-current sample -> gates off (20 us steps).
//        OVERRUN: trip_us = first overrun of the run -> gates off. vout/il/vin and raw_* are the
//        last ADC readings; duty_ticks is the last duty loaded before the fault.
//    STAT t_us=<t> state=<name> exec_max_us=<x> exec_mean_us=<x> updates=<n> overruns=<n>
//         drop_samples=<n> drop_events=<n>
//        once per second; exec = one control update (ADC + buck_update + PWM + bookkeeping),
//        measured with the CPU cycle counter.
//    DROP t_us=<t> samples=<n> events=<n>   ring was full; running totals of refused records
// ============================================================================================
#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "fw_config.h"
#include "pwm_backend.h"
#include "adc_sense.h"
#include "telemetry.h"
#include "control.h"

void setup()
{
    pwm_init();                      // gates held low from here on (R10/R11 held them before)
    Serial.setTxBufferSize(FW_TX_BUF);
    Serial.begin(FW_BAUD);
    adc_sense_init();

    boot_info_t boot = {};
    boot.pwm = pwm_name();
    boot.fsw_hz = pwm_fsw_hz();
    boot.adc_cal = adc_sense_cal_name();
    boot.note = pwm_note();
    telem_start(&boot);
    control_start();
}

void loop()
{
    vTaskDelete(nullptr);            // all work runs in the two tasks
}

// fw_config.h - firmware-only constants for buck_esp32 (the controller's own numbers live in
// src/buck_ctrl/buck_config.h and are not repeated here).
#pragma once

// Pin map (ESP32 header J3)
#define PIN_PWM_H            18   // -> UCC27211 HI (high-side gate)
#define PIN_PWM_L            19   // -> UCC27211 LI (low-side gate)
// ADC1: GPIO34 = ADC1_CH6 (VSENSE), GPIO35 = ADC1_CH7 (ISENSE), GPIO33 = ADC1_CH5 (VINSENSE)

// Simulator build (FW_SIM_WOKWI). The LEDC variant is the Wokwi build (pwm_backend.h), so it also
// selects the simulator-only paths below; hardware builds (MCPWM) never define it. Measured in
// Wokwi (sim/wokwi/README_WOKWI.md):
//   - three adc_oneshot_read calls take 128 us of simulated time (budget 20 us) and Wokwi's raw
//     code is V x 4095 / 5.0 V whatever the simulated eFuse says (adc_sense.cpp);
//   - one control update costs about 91 us of simulated time (buck_update alone 15 us), and the
//     50 kHz gptimer interrupt stretches it to about 500 us (about 16 us of every 20 us goes to
//     the interrupt), so a 20 us loop cannot run there at all (docs/wokwi_timing_diag.log).
// So the sim build runs the same discrete-time loop slowed down by FW_SIM_TIME_SCALE: one update
// every 20 us x scale of simulated time, paced on the CPU cycle counter (no interrupt), and the
// buck_plant chip integrates its model with dt / scale, so plant and controller see exactly
// 20 us per update. Firmware times (t_us, settle_us, trip_us) are control time: 20 us per tick.
#if defined(BUCK_PWM_LEDC) && !defined(FW_SIM_WOKWI)
#define FW_SIM_WOKWI         1
#endif
#if defined(FW_SIM_WOKWI) && !defined(FW_SIM_TIME_SCALE)
#define FW_SIM_TIME_SCALE    20       // must match the chip's timeScale attribute (diagram.json)
#endif

// Serial: 921600 baud 8N1. At 115200 the 100 us capture window (about 40 bytes per row,
// 10 rows per ms) needs about 35 ms of UART time per 1 ms captured; 921600 drains a 50-row
// window in about 22 ms, so back-to-back load steps 50 ms apart are printed without drops.
#define FW_BAUD              921600
#define FW_TX_BUF            4096

// Tasks: control on core 1 (the gptimer ISR is allocated from it, so it fires on core 1 too),
// telemetry on core 0 at low priority so UART work never competes with the loop.
#define FW_CTRL_CORE         1
#define FW_CTRL_PRIO         (configMAX_PRIORITIES - 2)
#define FW_CTRL_STACK        4096
#define FW_TELEM_CORE        0
#define FW_TELEM_PRIO        1
#define FW_TELEM_STACK       4096

// Control tick: gptimer at 1 MHz, alarm every 20 counts = BUCK_TS_S (20 us, 50 kHz)
#define FW_TICK_RES_HZ       1000000
#define FW_TICK_COUNTS       20

// Overrun fault when this many control ticks were missed in a row. The count is of missed
// ticks, not of late updates: one update that stalls for N ticks adds N, and a stall of
// N >= FW_OVERRUN_LIMIT ticks latches on its own (the notification count collapses the missed
// ticks into one late update, so counting updates would lose them). Updates with no overrun
// reset the count.
// N = 3 matches the OCP debounce (BUCK_OCP_SAMPLES = 3, 60 us): one isolated late tick only
// delays one duty change by 20 us and is harmless, but three in a row means the loop has run
// late for the whole OCP window, so the firmware can no longer promise the over-current
// response it was designed for and shuts the stage down.
#ifndef FW_OVERRUN_LIMIT
#define FW_OVERRUN_LIMIT     3
#endif

// Test build only: -DFW_TEST_STALL_TICKS=n busy-waits n control ticks inside one update (once,
// after FW_TEST_STALL_AT_US of control time) to prove the overrun latch catches a single stall.
#ifdef FW_TEST_STALL_TICKS
#ifndef FW_TEST_STALL_AT_US
#define FW_TEST_STALL_AT_US  20000
#endif
#endif

// Trace decimation (in control updates of 20 us)
#define FW_FAST_DIV          5        // 100 us: one trace sample = mean of 5 control samples
#define FW_BG_DIV            500      // 10 ms background trace
#define FW_CAP_PRE           10       // capture window: 1 ms before the trigger ...
#define FW_CAP_POST          40       // ... and 4 ms after it (extended on re-trigger)
#define FW_DI_TRIG_A         0.5f     // |change of il| between 100 us samples that triggers
#define FW_DI_START_A        0.1f     // a row that moved by more than this already holds the step start
#define FW_BAND_FRAC         0.01f    // +/-1 % of BUCK_VOUT_REF_V
// Load-step pass limits, the same as the host test (test_buck_ctrl.c): dip >= 4.50 V,
// release peak <= 5.50 V, recovery to +/-1 % within 5 ms. The STEP line ends in STEP_OK or STEP_FAIL.
#define FW_STEP_VMIN_V       4.50f
#define FW_STEP_VMAX_V       5.50f
#define FW_STEP_SETTLE_MAX_US 5000
#define FW_SETTLE_HOLD_US    2000     // in band this long = settled
#define FW_STEP_TIMEOUT_US   50000    // STEP event gives up after this (settle_us = -1)
#ifndef FW_STAT_DIV
#ifdef FW_SIM_WOKWI
#define FW_STAT_DIV          1000     // 20 ms of control time (0.4 s simulated at scale 20)
#else
#define FW_STAT_DIV          50000    // 1 s STAT line
#endif
#endif

// Telemetry ring (single producer = control task, single consumer = telemetry task)
#define FW_RING_LEN          512      // power of two
#define FW_RING_RESERVE      16       // slots only events may use, so a FAULT is never dropped

#pragma once
// ============================================================================
//  config.h  -  every tunable constant for the Smart Power-Monitoring Node
// ============================================================================
#include <Arduino.h>

// ---------------------------------------------------------------- Pin map ---
constexpr int      PIN_I2C_SDA  = 21;       // ESP32 default I2C0 SDA
constexpr int      PIN_I2C_SCL  = 22;       // ESP32 default I2C0 SCL
constexpr int      PIN_OLED_RST = -1;       // -1 = module has no / internal reset
constexpr uint32_t I2C_FREQ_HZ  = 400000;   // Fast-mode (needs ~2.2k-3.3k pull-ups)

// ---------------------------------------------------------- I2C addresses ---
constexpr uint8_t  INA219_ADDR  = 0x40;     // A0 = A1 = GND
constexpr uint8_t  OLED_ADDR    = 0x3C;     // SSD1306, SA0 = GND

// -------------------------------------------------------------- OLED -------
constexpr int      OLED_W       = 128;
constexpr int      OLED_H       = 64;

// ------------------------------------------------- INA219 analog front-end --
// Shunt: 0.05 ohm / PGA /4 (+-160 mV) -> 3.2 A full scale, 100 mV @ 2 A.
// Using a stock breakout with its 0.1 ohm shunt?  Set SHUNT_OHMS = 0.1 and
// INA219_PGA = 0b11 (+-320 mV).  Everything else recomputes automatically.
constexpr double   SHUNT_OHMS    = 0.05;
constexpr double   CURRENT_LSB_A = 0.0001;  // 100 uA/bit  (>= Imax / 2^15 = 61 uA)
constexpr uint8_t  INA219_PGA    = 0b10;    // 00:+-40mV 01:+-80mV 10:+-160mV 11:+-320mV
// Shunt full scale = 40 mV * 2^PGA = 4000 * 2^PGA raw counts (10 uV/LSB).
// With CAL = 8192 the OVF bit never fires, so a clipped SHUNT register is the only over-range signal.
constexpr int32_t INA219_SHUNT_FS_RAW  = 4000 * (1 << INA219_PGA);   // 16000 = 160.00 mV at PGA /4
constexpr int32_t INA219_SHUNT_SAT_RAW = INA219_SHUNT_FS_RAW - 10;    // 15990 = 159.90 mV -> treat as saturated
constexpr uint8_t  INA219_BRNG   = 0;       // 0 = 16 V bus range, 1 = 32 V
// ADC mode: 0b0011 = 12-bit single (532 us) ... 0b1110 = 64 samples (34 ms)
// 64 samples x 2 channels = ~68 ms  <  100 ms sample period
constexpr uint8_t  INA219_ADC_MODE = 0b1110;

// Datasheet eq.: Cal = trunc(0.04096 / (Current_LSB * R_shunt)); +0.5 guards FP round-off
constexpr uint16_t INA219_CAL = static_cast<uint16_t>(0.04096 / (CURRENT_LSB_A * SHUNT_OHMS) + 0.5);
static_assert(INA219_CAL > 0, "INA219 calibration underflow - check SHUNT_OHMS / CURRENT_LSB_A");

constexpr float    CURRENT_LSB_MA = static_cast<float>(CURRENT_LSB_A * 1000.0);
constexpr float    POWER_LSB_MW   = 20.0f * CURRENT_LSB_MA;   // fixed 20x by silicon

// ------------------------------------------------------------- RTOS ---------
constexpr uint32_t   SAMPLE_PERIOD_MS    = 100;   // Task 1
constexpr uint32_t   DISPLAY_PERIOD_MS   = 500;   // Task 2
constexpr uint32_t   I2C_MUTEX_TIMEOUT_MS = 50;   // > one OLED frame flush (~25-30 ms @400k)

constexpr UBaseType_t PRIO_SENSOR   = 3;          // higher: must not miss its 100 ms slot
constexpr UBaseType_t PRIO_DISPLAY  = 2;          // lower : cosmetic
constexpr uint32_t    STACK_SENSOR  = 3072;       // bytes
constexpr uint32_t    STACK_DISPLAY = 4096;       // bytes (snprintf + GFX)
constexpr BaseType_t  TASK_CORE     = 1;          // same core => priorities actually matter

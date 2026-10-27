#pragma once
// ============================================================================
//  Ina219.h - tiny register-level INA219 driver (no external library).
//  NOT thread-safe by itself: callers must hold the I2C mutex.
// ============================================================================
#include <Wire.h>
#include "config.h"

// One measurement snapshot, passed by value through the FreeRTOS queue.
struct PowerSample {
  float    bus_V        = 0.0f;   // voltage at VIN- (load side) w.r.t. GND
  float    shunt_mV     = 0.0f;   // drop across the shunt
  float    current_mA   = 0.0f;
  float    power_mW     = 0.0f;
  uint32_t timestamp_ms = 0;
  bool     valid        = false;
};

class Ina219 {
public:
  explicit Ina219(TwoWire &wire = Wire, uint8_t addr = INA219_ADDR)
      : _w(wire), _addr(addr) {}

  // Reset, program calibration + config, read config back to prove the chip is alive.
  bool begin() {
    if (!writeReg(REG_CONFIG, CFG_RESET)) return false;
    delay(2);
    if (!writeReg(REG_CALIB, INA219_CAL)) return false;   // reset wipes CAL, so write after
    if (!writeReg(REG_CONFIG, kConfig))   return false;
    uint16_t rb = 0;
    return readReg(REG_CONFIG, rb) && (rb == kConfig);
  }

  // True if the raw SHUNT register (10 uV/LSB) sits at the PGA full-scale clip.
  static constexpr bool shuntSaturated(int16_t raw) {
    return raw >= INA219_SHUNT_SAT_RAW || raw <= -INA219_SHUNT_SAT_RAW;
  }

  // Fills bus/shunt/current/power. Returns false on I2C error, math overflow or shunt saturation.
  bool read(PowerSample &out) {
    uint16_t rShunt, rBus, rPower, rCurrent;
    if (!readReg(REG_BUS,     rBus))     return false;
    if (!readReg(REG_SHUNT,   rShunt))   return false;
    if (!readReg(REG_POWER,   rPower))   return false;
    if (!readReg(REG_CURRENT, rCurrent)) return false;

    if (rBus & 0x0001) return false;                       // OVF: current/power invalid
    if (shuntSaturated(static_cast<int16_t>(rShunt))) return false;   // clipped at PGA full scale (> ~3.2 A)

    out.bus_V      = static_cast<float>(rBus >> 3) * 0.004f;            // 4 mV/LSB
    out.shunt_mV   = static_cast<int16_t>(rShunt)   * 0.01f;            // 10 uV/LSB
    out.current_mA = static_cast<int16_t>(rCurrent) * CURRENT_LSB_MA;   // signed
    out.power_mW   = static_cast<float>(rPower)     * POWER_LSB_MW;
    return true;
  }

private:
  enum : uint8_t { REG_CONFIG = 0x00, REG_SHUNT = 0x01, REG_BUS = 0x02,
                   REG_POWER  = 0x03, REG_CURRENT = 0x04, REG_CALIB = 0x05 };
  static constexpr uint16_t CFG_RESET = 0x8000;

  // CONFIG: BRNG[13] | PG[12:11] | BADC[10:7] | SADC[6:3] | MODE[2:0]=111 (shunt+bus, continuous)
  static constexpr uint16_t kConfig =
      (static_cast<uint16_t>(INA219_BRNG) << 13) |
      (static_cast<uint16_t>(INA219_PGA)  << 11) |
      (static_cast<uint16_t>(INA219_ADC_MODE) << 7) |
      (static_cast<uint16_t>(INA219_ADC_MODE) << 3) | 0x7;

  bool writeReg(uint8_t reg, uint16_t val) {
    _w.beginTransmission(_addr);
    _w.write(reg);
    _w.write(static_cast<uint8_t>(val >> 8));
    _w.write(static_cast<uint8_t>(val & 0xFF));
    return _w.endTransmission() == 0;
  }

  bool readReg(uint8_t reg, uint16_t &val) {
    _w.beginTransmission(_addr);
    _w.write(reg);
    if (_w.endTransmission() != 0) return false;
    if (_w.requestFrom(_addr, static_cast<uint8_t>(2)) != 2) return false;
    uint8_t hi = _w.read();          // separate statements: C++ evaluation order of
    uint8_t lo = _w.read();          // (read()<<8)|read() is unspecified
    val = (static_cast<uint16_t>(hi) << 8) | lo;
    return true;
  }

  TwoWire &_w;
  uint8_t  _addr;
};

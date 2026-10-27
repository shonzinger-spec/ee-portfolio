// test_ina219.cpp - host test: the real firmware driver (src/power_node/Ina219.h,
// config.h, unmodified) talks over a mocked Wire bus to the real Wokwi chip
// model (sim/wokwi/ina219.chip.c) running on the mocked Wokwi Chips API.
// Prints one PASS/FAIL line per case; exits 1 if any case fails.
#include <math.h>
#include <stdio.h>

#include <Arduino.h>
#include <Wire.h>
#include "Ina219.h"
#include "mock_wokwi.h"

static int g_failures = 0;

static void report(bool ok, const char *name, const char *detail) {
  printf("%s  %-28s %s\n", ok ? "PASS" : "FAIL", name, detail);
  if (!ok) g_failures++;
}

static bool near(double got, double want, double tol) { return fabs(got - want) <= tol; }

// Raw register access over the same bus, outside the driver under test.
static bool readReg(uint8_t reg, uint16_t &val) {
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission() != 0) return false;
  if (Wire.requestFrom(INA219_ADDR, static_cast<uint8_t>(2)) != 2) return false;
  const int hi = Wire.read();
  const int lo = Wire.read();
  val = static_cast<uint16_t>((hi << 8) | lo);
  return true;
}

static void writeReg(uint8_t reg, uint16_t val) {
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(reg);
  Wire.write(static_cast<uint8_t>(val >> 8));
  Wire.write(static_cast<uint8_t>(val & 0xFF));
  if (Wire.endTransmission() != 0) report(false, "raw register write", "NACK");
}

// Compares (register & mask) with want.
static void checkRegister(const char *name, uint8_t reg, uint16_t want, uint16_t mask = 0xFFFF) {
  uint16_t got = 0;
  const bool ok = readReg(reg, got) && (got & mask) == want;
  char detail[64];
  snprintf(detail, sizeof detail, "got 0x%04X want 0x%04X (mask 0x%04X)", got & mask, want, mask);
  report(ok, name, detail);
}

static void setAttr(const char *name, uint32_t value) {
  if (!mock_attr_set(name, value)) report(false, name, "chip never declared this attribute");
}

// Expected values for a 5 V supply and the 0.05 ohm high-side shunt. Above the
// 160 mV PGA /4 full scale (3200 mA) SHUNT and CURRENT saturate with OVF = 0
// (SBOS448 sets OVF only on a CURRENT/POWER math overflow), so the driver must
// reject |shunt| >= 159.9 mV itself: read() returns false there.
static void checkCurrent(Ina219 &ina, uint32_t current_ma) {
  char name[32];
  const bool saturated = current_ma >= 3198;   // 159.90 mV and up
  snprintf(name, sizeof name, "read() at %u mA%s", current_ma, saturated ? " (sat)" : "");
  if (!mock_attr_set("currentMa", current_ma)) {
    report(false, name, "chip has no currentMa attribute");
    return;
  }

  PowerSample s;
  const bool got = ina.read(s);
  if (saturated) {
    report(!got, name, got ? "read() true, want false (saturated)" : "false (saturated, invalid)");
    return;
  }
  if (!got) {
    report(false, name, "read() returned false");
    return;
  }

  const double I         = current_ma;
  const double shunt_mV  = I * 0.05;
  const double bus_V     = 5.000 - 0.05 * current_ma / 1000.0;
  // The chip multiplies CURRENT by the bus voltage it measured, so power is
  // checked against the bus_V that read() returned (bus LSB is 4 mV, which is
  // up to 6 mW at 3 A; the remaining error is the 2 mW power LSB).
  const double power_mW  = s.bus_V * I;

  const bool ok = near(s.shunt_mV, shunt_mV, 0.01) &&
                  near(s.current_mA, I, 0.1) &&
                  near(s.bus_V, bus_V, 0.004) &&
                  near(s.power_mW, power_mW, 4.0);
  char detail[160];
  snprintf(detail, sizeof detail,
                "shunt %.3f/%.3f mV  I %.2f/%.2f mA  bus %.3f/%.3f V  P %.1f/%.1f mW",
                s.shunt_mV, shunt_mV, s.current_mA, I, s.bus_V, bus_V, s.power_mW, power_mW);
  report(ok, name, detail);
}

int main() {
  mock_chip_init();

  // Raw registers at power-on (CONFIG 0x399F = PGA /8; default 1000 mA -> 50 mV).
  checkRegister("power-on CONFIG", 0x00, 0x399F);
  checkRegister("power-on CAL", 0x05, 0x0000);
  checkRegister("power-on SHUNT (50 mV)", 0x01, 5000);
  checkRegister("CURRENT = 0 before CAL", 0x04, 0x0000);
  checkRegister("POWER = 0 before CAL", 0x03, 0x0000);
  checkRegister("BUS CNVR=1 bit2=0 OVF=0", 0x02, 0x0002, 0x0007);
  writeReg(0x05, 0x2001);
  checkRegister("CAL bit0 reads 0", 0x05, 0x2000);
  checkRegister("CURRENT after CAL", 0x04, 10000);   // 5000 * 8192 / 4096
  writeReg(0x00, 0x8000);
  checkRegister("CONFIG after RST", 0x00, 0x399F);
  checkRegister("CAL cleared by RST", 0x05, 0x0000);

  Ina219 ina;
  report(ina.begin(), "begin()", "reset, CAL, CONFIG, read-back");
  checkRegister("CONFIG after begin()", 0x00, 0x1777);
  checkRegister("CAL after begin()", 0x05, 8192);

  // 3196 mA = 159.80 mV is valid; 3198 mA = 159.90 mV and above is saturated
  // (3300 mA = 165 mV clips at 160 mV with no OVF) and must be rejected.
  const uint32_t currents_ma[] = {0, 500, 1500, 2000, 3000, 3196, 3198, 3200, 3300};
  for (uint32_t ma : currents_ma) checkCurrent(ina, ma);

  // Helper directly (the chip model cannot produce negative current).
  const struct { int16_t raw; bool want; } sat[] = {
    {0, false}, {15980, false}, {15990, true}, {16000, true},
    {-15980, false}, {-15990, true}, {-16000, true}, {-32768, true}};
  for (const auto &c : sat) {
    char name[40], detail[48];
    snprintf(name, sizeof name, "shuntSaturated(%d)", c.raw);
    snprintf(detail, sizeof detail, "want %s", c.want ? "true" : "false");
    report(Ina219::shuntSaturated(c.raw) == c.want, name, detail);
  }

  // OVF rule, raw registers: PGA /8, CAL 4096, 3000 mA * 100 mohm = 300 mV,
  // bus 26.0 V -> CURRENT 30000, POWER 30000 * 6500 / 5000 = 39000. POWER is
  // unsigned, so 39000 is in range (no OVF); CAL 8192 doubles CURRENT to
  // 60000, past int16, which must set OVF.
  setAttr("shuntMohm", 100);
  setAttr("supplyMv", 26300);
  setAttr("currentMa", 3000);
  writeReg(0x00, 0x399F);
  writeReg(0x05, 4096);
  checkRegister("POWER 39000 (above 32767)", 0x03, 39000);
  checkRegister("no OVF for POWER 39000", 0x02, 0x0000, 0x0001);
  writeReg(0x05, 8192);
  checkRegister("CURRENT overflow sets OVF", 0x02, 0x0001, 0x0001);

  printf("%s: %d failure(s)\n", g_failures ? "FAILED" : "ALL PASS", g_failures);
  return g_failures ? 1 : 0;
}

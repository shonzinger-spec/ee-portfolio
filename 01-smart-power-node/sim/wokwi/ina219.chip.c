// ============================================================================
//  ina219.chip.c - Wokwi custom-chip model of the TI INA219 (SBOS448)
//
//  Wokwi has no INA219 part, so this chip stands in for it on the I2C bus.
//  The shunt is not simulated electrically: the load current comes from the
//  "currentMa" slider and the shunt/supply values from attributes, and every
//  register is recomputed from them on each read.
//
//    Vshunt = I * R_shunt             (high-side shunt, IN+ -> IN-)
//    Vbus   = supply - I * R_shunt    (measured at IN-, the load side)
//
//  Registers (16-bit, MSB first):
//    0x00 CONFIG   power-on 0x399F; writing bit15 resets every register
//    0x01 SHUNT    signed, 10 uV/LSB, saturates at the PGA range (CONFIG[12:11])
//    0x02 BUS      (Vbus / 4 mV) << 3 | CNVR(bit1) | OVF(bit0)
//    0x03 POWER    CURRENT * BUS[15:3] / 5000, unsigned
//    0x04 CURRENT  SHUNT * CAL / 4096  (0 while CAL == 0)
//    0x05 CAL      bit0 always reads 0
//  OVF is set only when the CURRENT or POWER calculation is out of range
//  (SBOS448 8.6.3.2). A shunt voltage beyond the PGA range is NOT flagged: the
//  SHUNT register just saturates, so 3.3 A through 50 mohm at PGA /4 reads
//  160.00 mV / 3.2 A with OVF = 0.
//  I2C: the first byte of a write sets the register pointer; two more bytes
//  (MSB, LSB) write that register. A read returns the pointed register.
//  Fixed address 0x40 (A0 = A1 = GND), so A0/A1 are not modelled as pins.
//
//  Model assumptions and simplifications (none reachable at the 5 V / 0-3.2 A
//  spec except the first; the firmware does not read CNVR):
//    - SBOS448 gives the SHUNT range but not its over-range code; the model
//      saturates at +/-FS, the end points of the datasheet's register table.
//    - No conversion timing: every read is a fresh, completed conversion, so
//      CNVR always reads 1. The real part clears it on a POWER read or a CONFIG
//      mode write until the next conversion completes.
//    - BUS is clamped to its 13-bit field; BRNG (16/32 V) does not change it.
//    - POWER uses |CURRENT|; the slider cannot make a negative current.
// ============================================================================
#include "wokwi-api.h"
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>

#define INA219_ADDR      0x40
#define CONFIG_POWER_ON  0x399F
#define CONFIG_RST       0x8000

enum { REG_CONFIG = 0, REG_SHUNT, REG_BUS, REG_POWER, REG_CURRENT, REG_CAL };

// Attributes are clamped before use so every product below fits in int64_t.
#define MAX_CURRENT_MA   1000000u   // 1 kA
#define MAX_SHUNT_MOHM   1000000u   // 1 kohm
#define MAX_SUPPLY_MV    1000000u   // 1 kV

typedef struct {
  uint32_t attr_current_ma;
  uint32_t attr_shunt_mohm;
  uint32_t attr_supply_mv;
  uint16_t config;
  uint16_t cal;
  uint8_t  pointer;
  uint8_t  write_count;   // bytes received since the current write started
  uint8_t  write_msb;
  uint8_t  read_count;    // bytes returned since the current read started
  uint16_t read_latch;    // register value latched when the read started
} chip_state_t;

typedef struct {
  int16_t  shunt;
  uint16_t bus;
  uint16_t power;
  int16_t  current;
} measurement_t;

static uint32_t clamp_u32(uint32_t v, uint32_t max) { return v > max ? max : v; }

static int64_t clamp_i64(int64_t v, int64_t lo, int64_t hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

// Round-half-away-from-zero division for a positive divisor.
static int64_t div_round(int64_t num, int64_t den) {
  return num >= 0 ? (num + den / 2) / den : -((-num + den / 2) / den);
}

static measurement_t measure(const chip_state_t *s) {
  measurement_t m;
  const int64_t i_ma      = clamp_u32(attr_read(s->attr_current_ma), MAX_CURRENT_MA);
  const int64_t r_mohm    = clamp_u32(attr_read(s->attr_shunt_mohm), MAX_SHUNT_MOHM);
  const int64_t supply_uv = (int64_t)clamp_u32(attr_read(s->attr_supply_mv), MAX_SUPPLY_MV) * 1000;
  bool ovf = false;

  // Shunt: mA * mohm = uV. PGA full scale = 40 mV << PG. Saturates, no OVF.
  const int64_t vshunt_uv = i_ma * r_mohm;
  const int64_t fs_uv     = (int64_t)40000 << ((s->config >> 11) & 0x3);
  const int64_t fs_lsb    = fs_uv / 10;
  m.shunt = (int16_t)clamp_i64(div_round(vshunt_uv, 10), -fs_lsb, fs_lsb);

  // Bus: load-side node, 4 mV/LSB in a 13-bit field.
  const int64_t vbus_uv = clamp_i64(supply_uv - vshunt_uv, 0, INT64_MAX);
  const int64_t bus_lsb = clamp_i64(div_round(vbus_uv, 4000), 0, 0x1FFF);

  // Current = SHUNT * CAL / 4096; 0 until CAL is programmed.
  int64_t cur = (s->cal == 0) ? 0 : ((int64_t)m.shunt * s->cal) / 4096;
  if (cur > INT16_MAX || cur < INT16_MIN) { ovf = true; cur = clamp_i64(cur, INT16_MIN, INT16_MAX); }
  m.current = (int16_t)cur;

  // Power = |CURRENT| * BUS / 5000 into the unsigned PD15..PD0 register.
  int64_t pwr = ((cur < 0 ? -cur : cur) * bus_lsb) / 5000;
  if (pwr > UINT16_MAX) { ovf = true; pwr = UINT16_MAX; }
  m.power = (uint16_t)pwr;

  m.bus = (uint16_t)((bus_lsb << 3) | 0x2 /* CNVR */ | (ovf ? 0x1 : 0x0));
  return m;
}

static uint16_t read_register(const chip_state_t *s, uint8_t reg) {
  switch (reg) {
    case REG_CONFIG:  return s->config;
    case REG_SHUNT:   return (uint16_t)measure(s).shunt;
    case REG_BUS:     return measure(s).bus;
    case REG_POWER:   return measure(s).power;
    case REG_CURRENT: return (uint16_t)measure(s).current;
    case REG_CAL:     return s->cal;
    default:          return 0;
  }
}

static void write_register(chip_state_t *s, uint8_t reg, uint16_t value) {
  switch (reg) {
    case REG_CONFIG:
      if (value & CONFIG_RST) {   // reset: every register back to power-on state
        s->config = CONFIG_POWER_ON;
        s->cal    = 0;
      } else {
        s->config = value;
      }
      break;
    case REG_CAL:
      s->cal = value & 0xFFFE;
      break;
    default:                      // SHUNT/BUS/POWER/CURRENT are read-only
      break;
  }
}

static bool on_i2c_connect(void *user_data, uint32_t address, bool read) {
  chip_state_t *s = user_data;
  (void)address;
  if (read) {
    s->read_latch = read_register(s, s->pointer);
    s->read_count = 0;
  } else {
    s->write_count = 0;
  }
  return true;
}

static uint8_t on_i2c_read(void *user_data) {
  chip_state_t *s = user_data;
  const uint8_t byte = (s->read_count & 1) ? (uint8_t)(s->read_latch & 0xFF)
                                           : (uint8_t)(s->read_latch >> 8);
  s->read_count++;
  return byte;
}

static bool on_i2c_write(void *user_data, uint8_t data) {
  chip_state_t *s = user_data;
  switch (s->write_count) {
    case 0:  s->pointer = data; break;
    case 1:  s->write_msb = data; break;
    case 2:  write_register(s, s->pointer, (uint16_t)((s->write_msb << 8) | data)); break;
    default: break;               // extra bytes are ACKed and ignored
  }
  if (s->write_count < 3) s->write_count++;
  return true;
}

static void on_i2c_disconnect(void *user_data) {
  (void)user_data;
}

void chip_init(void) {
  chip_state_t *s = calloc(1, sizeof(chip_state_t));
  if (!s) return;

  s->attr_current_ma = attr_init("currentMa", 1000);
  s->attr_shunt_mohm = attr_init("shuntMohm", 50);
  s->attr_supply_mv  = attr_init("supplyMv", 5000);
  s->config          = CONFIG_POWER_ON;
  s->cal             = 0;

  const i2c_config_t i2c = {
    .user_data  = s,
    .address    = INA219_ADDR,
    .scl        = pin_init("SCL", INPUT_PULLUP),
    .sda        = pin_init("SDA", INPUT_PULLUP),
    .connect    = on_i2c_connect,
    .read       = on_i2c_read,
    .write      = on_i2c_write,
    .disconnect = on_i2c_disconnect,
  };
  i2c_init(&i2c);
}

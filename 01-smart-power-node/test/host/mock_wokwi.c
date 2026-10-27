// mock_wokwi.c - host implementation of the Wokwi Chips API calls that
// ina219.chip.c uses (pin_init, attr_init, attr_read, i2c_init), plus the
// mock_* bridge declared in mock_wokwi.h.
#include <wokwi-api.h>
#include <stddef.h>
#include <string.h>
#include "mock_wokwi.h"

#define MAX_ATTRS 8

static struct {
  const char *name;
  uint32_t    value;
} g_attrs[MAX_ATTRS];
static uint32_t     g_attr_count;
static i2c_config_t g_i2c;
static bool         g_i2c_ready;
static pin_t        g_next_pin;

pin_t pin_init(const char *name, uint32_t mode) {
  (void)name;
  (void)mode;
  return g_next_pin++;
}

uint32_t attr_init(const char *name, uint32_t default_value) {
  for (uint32_t i = 0; i < g_attr_count; i++)
    if (strcmp(g_attrs[i].name, name) == 0) return i;   // re-init keeps the set value
  if (g_attr_count == MAX_ATTRS) return 0;
  g_attrs[g_attr_count].name  = name;
  g_attrs[g_attr_count].value = default_value;
  return g_attr_count++;
}

uint32_t attr_read(uint32_t attr_id) {
  return attr_id < g_attr_count ? g_attrs[attr_id].value : 0;
}

i2c_dev_t i2c_init(const i2c_config_t *config) {
  g_i2c       = *config;
  g_i2c_ready = true;
  return 1;
}

void mock_chip_init(void) { chip_init(); }

bool mock_attr_set(const char *name, uint32_t v) {
  for (uint32_t i = 0; i < g_attr_count; i++) {
    if (strcmp(g_attrs[i].name, name) == 0) {
      g_attrs[i].value = v;
      return true;
    }
  }
  return false;
}

bool mock_i2c_connect(uint8_t address, bool read) {
  if (!g_i2c_ready) return false;
  if (g_i2c.address != 0 && g_i2c.address != address) return false;
  return g_i2c.connect ? g_i2c.connect(g_i2c.user_data, address, read) : true;
}

bool mock_i2c_write(uint8_t data) {
  return g_i2c.write ? g_i2c.write(g_i2c.user_data, data) : true;
}

uint8_t mock_i2c_read(void) {
  return g_i2c.read ? g_i2c.read(g_i2c.user_data) : 0xFF;
}

void mock_i2c_disconnect(void) {
  if (g_i2c.disconnect) g_i2c.disconnect(g_i2c.user_data);
}

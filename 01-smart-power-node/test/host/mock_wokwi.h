// mock_wokwi.h - test-side handle on the host implementation of the Wokwi
// Chips API (mock_wokwi.c). Lets the C++ test drive the chip's I2C callbacks
// and set attribute values (the "currentMa" slider) without wokwi-api.h.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void    mock_chip_init(void);                         // runs the chip's chip_init()
bool    mock_attr_set(const char *name, uint32_t v);  // false: chip never declared it
bool    mock_i2c_connect(uint8_t address, bool read); // false: NACK (no device there)
bool    mock_i2c_write(uint8_t data);
uint8_t mock_i2c_read(void);
void    mock_i2c_disconnect(void);

#ifdef __cplusplus
}
#endif

// Host stand-in for <Wire.h>: TwoWire transfers go straight into the custom
// chip's I2C callbacks through mock_wokwi.h, one connect/disconnect per transfer.
#pragma once
#include <Arduino.h>
#include "../mock_wokwi.h"

class TwoWire {
public:
  void beginTransmission(uint8_t address) {
    _address = address;
    _txLen   = 0;
  }

  size_t write(uint8_t data) {
    if (_txLen == kBufferSize) return 0;   // same as Arduino: buffer full
    _tx[_txLen++] = data;
    return 1;
  }

  // Arduino return codes: 0 ok, 2 NACK on address, 3 NACK on data.
  uint8_t endTransmission(bool sendStop = true) {
    (void)sendStop;
    if (!mock_i2c_connect(_address, false)) return 2;
    for (size_t i = 0; i < _txLen; i++) {
      if (!mock_i2c_write(_tx[i])) {
        mock_i2c_disconnect();
        return 3;
      }
    }
    mock_i2c_disconnect();
    return 0;
  }

  uint8_t requestFrom(uint8_t address, uint8_t quantity) {
    _rxLen = 0;
    _rxPos = 0;
    if (quantity > kBufferSize) quantity = kBufferSize;
    if (!mock_i2c_connect(address, true)) return 0;
    while (_rxLen < quantity) _rx[_rxLen++] = mock_i2c_read();
    mock_i2c_disconnect();
    return quantity;
  }

  int available() const { return static_cast<int>(_rxLen - _rxPos); }

  int read() { return _rxPos < _rxLen ? _rx[_rxPos++] : -1; }

private:
  static constexpr size_t kBufferSize = 128;   // arduino-esp32 I2C_BUFFER_LENGTH
  uint8_t _address = 0;
  uint8_t _tx[kBufferSize] = {};
  uint8_t _rx[kBufferSize] = {};
  size_t  _txLen = 0;
  size_t  _rxLen = 0;
  size_t  _rxPos = 0;
};

inline TwoWire Wire;

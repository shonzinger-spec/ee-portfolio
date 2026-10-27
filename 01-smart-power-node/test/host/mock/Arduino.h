// Host stand-in for <Arduino.h>: just what config.h and Ina219.h need.
// C headers only, so the mocks build even where the C++ library headers are
// missing (this Mac's Command Line Tools ship an incomplete include/c++/v1).
#pragma once
#include <stddef.h>
#include <stdint.h>

// ESP32 FreeRTOS port types (portmacro.h: portBASE_TYPE is int).
typedef int          BaseType_t;
typedef unsigned int UBaseType_t;

inline void delay(uint32_t) {}

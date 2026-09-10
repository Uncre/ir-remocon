#pragma once
#include <Arduino.h>

constexpr uint8_t MAX_SIGNALS = 12;
constexpr uint16_t MAX_RAW_LEN = 1024;
constexpr size_t MAX_NAME_BYTES = 60;
struct Signal {
  char name[MAX_NAME_BYTES + 1] = {};
  uint16_t length = 0;
  uint16_t raw[MAX_RAW_LEN] = {};
};
// All calls are serialized by the firmware mutex. No automatic format on failure.
bool storeBegin();
bool storeRead(uint8_t id, Signal& signal);
bool storeExists(uint8_t id);
bool storeWrite(uint8_t id, const Signal& signal);
bool storeDelete(uint8_t id);

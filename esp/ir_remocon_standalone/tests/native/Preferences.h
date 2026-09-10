#pragma once
class Preferences {
 public:
  inline static bool initialized = false;
  bool begin(const char*, bool) { return true; }
  bool getBool(const char*, bool) { return initialized; }
  int putBool(const char*, bool value) { initialized = value; return 1; }
  void end() {}
};

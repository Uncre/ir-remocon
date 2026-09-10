#pragma once
#include <cstdint>
#include <cstring>
#include <string>
class String : public std::string {
 public:
  using std::string::string;
  String(const std::string& s) : std::string(s) {}
  String(uint8_t n) : std::string(std::to_string(n)) {}
};

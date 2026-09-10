#pragma once
#include "Arduino.h"
#include <map>
#include <vector>
#include <algorithm>
class File {
 public:
  std::vector<uint8_t>* bytes = nullptr;
  size_t offset = 0;
  inline static bool failWrites = false;
  explicit operator bool() const { return bytes; }
  size_t size() const { return bytes ? bytes->size() : 0; }
  size_t read(uint8_t* target, size_t count) {
    count = std::min(count, size() - offset);
    if (count) memcpy(target, bytes->data() + offset, count);
    offset += count; return count;
  }
  int read() { return offset < size() ? (*bytes)[offset++] : -1; }
  size_t write(const uint8_t* data, size_t count) {
    if (!bytes || failWrites) return 0;
    bytes->insert(bytes->end(), data, data + count); return count;
  }
  void flush() {}
  void close() {}
};
class FakeFS {
 public:
  std::map<std::string, std::vector<uint8_t>> files;
  bool mountable = true, failRename = false;
  int formats = 0;
  bool begin(bool format, const char*, int, const char*) {
    if (format) { ++formats; files.clear(); mountable = true; }
    return mountable;
  }
  bool exists(const String& path) { return files.count(path); }
  File open(const String& path, const char* mode) {
    if (*mode == 'w') files[path].clear();
    return {exists(path) ? &files[path] : nullptr};
  }
  bool rename(const String& from, const String& to) {
    if (failRename || !exists(from) || exists(to)) return false;
    files[to] = files[from]; files.erase(from); return true;
  }
  bool remove(const String& path) { return files.erase(path); }
};
inline FakeFS LittleFS;

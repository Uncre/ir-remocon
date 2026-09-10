#include "signal_store.h"
#include <LittleFS.h>
#include <Preferences.h>

static String pathFor(uint8_t id) { return "/signal-" + String(id) + ".bin"; }

bool storeBegin() {
  // A dedicated partition prevents formatting another sketch's filesystem.
  // NVS remembers successful initialization; a later mount failure never erases it.
  Preferences prefs;
  if (!prefs.begin("ir-simple", false)) return false;
  bool initialized = prefs.getBool("initialized", false);
  bool ok = LittleFS.begin(false, "/littlefs", 4, "irdata");
  if (!ok && !initialized) ok = LittleFS.begin(true, "/littlefs", 4, "irdata");
  if (ok && !initialized) ok = prefs.putBool("initialized", true) == 1;
  prefs.end();
  return ok;
}

bool storeExists(uint8_t id) { return id < MAX_SIGNALS && LittleFS.exists(pathFor(id)); }

bool storeRead(uint8_t id, Signal& s) {
  if (id >= MAX_SIGNALS) return false;
  File f = LittleFS.open(pathFor(id), "r");
  uint8_t header[6];
  if (!f || f.read(header, sizeof(header)) != sizeof(header)) return false;
  if (header[0] != 'I' || header[1] != 'R' || header[2] != 1) return false;
  size_t nameLen = header[3];
  s.length = header[4] | (uint16_t(header[5]) << 8);
  if (!nameLen || nameLen > MAX_NAME_BYTES || !s.length || s.length > MAX_RAW_LEN ||
      f.size() != 6 + nameLen + size_t(s.length) * 2) return false;
  if (f.read(reinterpret_cast<uint8_t*>(s.name), nameLen) != nameLen) return false;
  s.name[nameLen] = 0;
  if (strlen(s.name) != nameLen) return false;
  for (uint16_t i = 0; i < s.length; ++i) {
    int lo = f.read(), hi = f.read();
    if (lo < 0 || hi < 0) return false;
    s.raw[i] = uint16_t(lo) | (uint16_t(hi) << 8);
  }
  return true;
}

bool storeWrite(uint8_t id, const Signal& s) {
  size_t n = strnlen(s.name, MAX_NAME_BYTES + 1);
  if (id >= MAX_SIGNALS || storeExists(id) || !n || n > MAX_NAME_BYTES ||
      !s.length || s.length > MAX_RAW_LEN) return false;
  File f = LittleFS.open("/pending.tmp", "w");
  if (!f) return false;
  uint8_t header[] = {'I', 'R', 1, uint8_t(n), uint8_t(s.length), uint8_t(s.length >> 8)};
  bool ok = f.write(header, sizeof(header)) == sizeof(header) &&
            f.write(reinterpret_cast<const uint8_t*>(s.name), n) == n;
  for (uint16_t i = 0; ok && i < s.length; ++i) {
    uint8_t pair[] = {uint8_t(s.raw[i]), uint8_t(s.raw[i] >> 8)};
    ok = f.write(pair, 2) == 2;
  }
  f.flush();
  f.close();
  // Only publish a fully written new slot. An interrupted write leaves a temp file.
  return ok && LittleFS.rename("/pending.tmp", pathFor(id));
}

bool storeDelete(uint8_t id) { return storeExists(id) && LittleFS.remove(pathFor(id)); }

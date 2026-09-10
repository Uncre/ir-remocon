#include <cassert>
#include <iostream>
#include "LittleFS.h"
#include "Preferences.h"
#include "../../signal_store.h"
int main() {
  LittleFS.mountable = false;
  assert(storeBegin()); assert(LittleFS.formats == 1);
  Signal original, loaded;
  strcpy(original.name, "light"); original.length = MAX_RAW_LEN;
  for (unsigned i = 0; i < MAX_RAW_LEN; ++i) original.raw[i] = i == 0 ? 65535 : i;
  assert(storeWrite(0, original)); assert(storeRead(0, loaded));
  assert(!strcmp(original.name, loaded.name));
  assert(loaded.length == MAX_RAW_LEN);
  assert(!memcmp(original.raw, loaded.raw, sizeof(original.raw)));
  assert(!storeWrite(0, original)); // No overwrite.
  assert(!storeWrite(MAX_SIGNALS, original));
  original.length = MAX_RAW_LEN + 1; assert(!storeWrite(1, original));
  original.length = 3;
  File::failWrites = true; assert(!storeWrite(1, original)); assert(!storeExists(1));
  File::failWrites = false;
  LittleFS.failRename = true; assert(!storeWrite(1, original)); assert(!storeExists(1));
  LittleFS.failRename = false;
  assert(storeWrite(1, original)); assert(storeRead(1, loaded));
  LittleFS.files["/signal-1.bin"].pop_back(); assert(!storeRead(1, loaded));
  assert(storeRead(0, loaded)); // Damage does not spread to another signal.
  assert(storeDelete(1)); assert(!storeDelete(1));
  LittleFS.mountable = false; assert(!storeBegin()); assert(LittleFS.formats == 1);
  assert(storeExists(0)); // Mount failure never formats initialized data.
  std::cout << "Storage checks passed: max waveform roundtrip, bounds, no overwrite, short write, rename failure, corruption, mount failure.\n";
}

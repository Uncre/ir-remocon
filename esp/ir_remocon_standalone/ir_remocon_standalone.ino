/* ESP32 standalone IR remote v1.0.0
 * Derived from ir-remocon firmware v2.2.0 (GPIO13 receive / GPIO4 send).
 * HTTP callbacks, SQLite and the Python server are replaced by local storage.
 */
#include <ArduinoJson.h>
#include <IRremoteESP8266.h>
#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ESPAsyncWebServer.h>
#include <IRrecv.h>
#include <IRsend.h>
#include <IRutils.h>
#include "signal_store.h"
#include "web_ui.h"
#ifdef IR_COMPILE_CHECK
#define WIFI_SSID "compile-only"
#define WIFI_PASSWORD "not-a-real-password"
#else
#include "secrets.h"
#endif

constexpr uint16_t IR_RECV_PIN = 13;
constexpr uint16_t IR_SEND_PIN = 4;
constexpr uint32_t LEARN_MS = 15000;
enum Mode { MODE_IDLE, MODE_RECEIVE, MODE_SEND };
static Mode currentMode = MODE_IDLE;
static SemaphoreHandle_t mutex;
static bool storageOk = false, pendingSend = false, receiving = false;
static uint32_t started = 0, operation = 0, lastSendAt = 0;
static bool sentOnce = false;
static String bootId, message = "ボタンを登録して使い始めましょう。";
static const char* outcome = "idle";
static Signal activeSignal, scratch;
static uint8_t activeId = 0;
static bool overflowSeen = false;
AsyncWebServer server(80);
IRrecv irrecv(IR_RECV_PIN, MAX_RAW_LEN, 50, true);
IRsend irsend(IR_SEND_PIN);
decode_results results;

// Keep critical sections out of IR interrupts; HTTP and loop share this mutex.
class Guard {
 public:
  bool held;
  Guard() : held(xSemaphoreTake(mutex, 0) == pdTRUE) {}
  ~Guard() { if (held) xSemaphoreGive(mutex); }
};

static void error(AsyncWebServerRequest* r, int code, const char* text) {
  StaticJsonDocument<384> d;
  d["message"] = text;
  String body; serializeJson(d, body);
  r->send(code, "application/json; charset=utf-8", body);
}
static void json(AsyncWebServerRequest* r, JsonDocument& d, int code = 200) {
  String body; serializeJson(d, body);
  auto response = r->beginResponse(code, "application/json; charset=utf-8", body);
  response->addHeader("Cache-Control", "no-store");
  r->send(response);
}
static bool canChange(AsyncWebServerRequest* r) {
  if (!storageOk) { error(r, 503, "保存領域を開けません。シリアルモニターを確認してください。"); return false; }
  if (currentMode != MODE_IDLE || (sentOnce && millis() - lastSendAt < 300)) {
    error(r, 409, "操作中です。少し待ってください。"); return false;
  }
  return true;
}
static void status(AsyncWebServerRequest* r) {
  Guard g;
  if (!g.held) { error(r, 409, "処理中です。"); return; }
  StaticJsonDocument<1024> d;
  d["mode"] = currentMode == MODE_IDLE ? "idle" : currentMode == MODE_SEND ? "send" : "learn";
  d["outcome"] = outcome; d["message"] = message;
  d["operation"] = operation; d["boot"] = bootId;
  d["remaining_ms"] = currentMode == MODE_RECEIVE ? LEARN_MS - min(uint32_t(millis() - started), LEARN_MS) : 0;
  d["storage_ok"] = storageOk; d["version"] = "1.0.0";
  json(r, d);
}
static void listSignals(AsyncWebServerRequest* r) {
  Guard g;
  if (!g.held) { error(r, 409, "処理中です。"); return; }
  if (!storageOk) { error(r, 503, "保存領域を開けません。"); return; }
  DynamicJsonDocument d(4096);
  JsonArray arr = d.to<JsonArray>();
  for (uint8_t i = 0; i < MAX_SIGNALS; ++i) {
    if (!storeExists(i)) continue;
    bool valid = storeRead(i, scratch);
    JsonObject row = arr.createNestedObject();
    row["id"] = i;
    row["name"] = valid ? String(scratch.name) : String("読み出せない信号 ") + String(i + 1);
    row["valid"] = valid;
  }
  json(r, d);
}
static bool parseId(const String& value, uint8_t& id) {
  if (!value.length() || value.length() > 2) return false;
  for (char c : value) if (c < '0' || c > '9') return false;
  int n = value.toInt(); if (n >= MAX_SIGNALS) return false;
  id = n; return true;
}
static void deleteSignal(AsyncWebServerRequest* r) {
  if (!r->hasHeader("X-IR-Request") || r->getHeader("X-IR-Request")->value() != "1") {
    error(r, 403, "操作画面から実行してください。"); return;
  }
  uint8_t id;
  if (r->params() != 1 || !r->hasParam("id") || !parseId(r->getParam("id")->value(), id)) {
    error(r, 400, "信号番号が不正です。"); return;
  }
  Guard g;
  if (!g.held) { error(r, 409, "処理中です。"); return; }
  if (!canChange(r)) return;
  if (!storeExists(id)) { error(r, 404, "信号がありません。"); return; }
  if (!storeDelete(id)) { error(r, 500, "削除に失敗しました。"); return; }
  r->send(200, "application/json", "{}");
}
static void commandBody(AsyncWebServerRequest* r, uint8_t* data, size_t len, size_t index, size_t total) {
  if (index == 0) {
    if (!r->hasHeader("X-IR-Request") || r->getHeader("X-IR-Request")->value() != "1") {
      error(r, 403, "操作画面から実行してください。"); return;
    }
    if (r->contentType() != "application/json" || !total || total > 512) {
      error(r, 400, "リクエストが不正か、大きすぎます。"); return;
    }
    r->_tempObject = malloc(total + 1);
    if (!r->_tempObject) { error(r, 503, "メモリ不足です。"); return; }
  }
  if (!r->_tempObject) return;
  if (index > total || len > total - index) { error(r, 400, "データが不正です。"); return; }
  memcpy(static_cast<uint8_t*>(r->_tempObject) + index, data, len);
  if (index + len != total) return;
  StaticJsonDocument<768> d;
  auto err = deserializeJson(d, static_cast<char*>(r->_tempObject), total);
  if (err || !d.is<JsonObject>() || d.size() != 1) { error(r, 400, "入力を確認してください。"); return; }
  Guard g;
  if (!g.held) { error(r, 409, "処理中です。"); return; }
  if (!canChange(r)) return;
  if (r->url() == "/api/learn") {
    if (!d["name"].is<const char*>()) { error(r, 400, "名前を入力してください。"); return; }
    JsonString nameJson = d["name"].as<JsonString>();
    if (strlen(nameJson.c_str()) != nameJson.size()) { error(r, 400, "名前に使えない文字があります。"); return; }
    String name = nameJson.c_str(); name.trim();
    if (!name.length() || name.length() > MAX_NAME_BYTES) { error(r, 400, "名前は日本語20文字程度（60バイト以内）にしてください。"); return; }
    for (char c : name) if (uint8_t(c) < 32) { error(r, 400, "名前に制御文字は使えません。"); return; }
    int available = -1;
    for (uint8_t i = 0; i < MAX_SIGNALS; ++i) {
      if (!storeExists(i)) { if (available < 0) available = i; continue; }
      if (storeRead(i, scratch) && name == scratch.name) { error(r, 409, "同じ名前があります。別の名前を付けてください。"); return; }
    }
    if (available < 0) { error(r, 409, "12個まで登録できます。不要な信号を削除してください。"); return; }
    activeId = available; name.toCharArray(activeSignal.name, sizeof(activeSignal.name));
    currentMode = MODE_RECEIVE; started = millis(); overflowSeen = false;
    message = "受信部にリモコンを向け、ボタンを短く押してください。";
  } else {
    if (!d["id"].is<unsigned int>() || d["id"].as<unsigned int>() >= MAX_SIGNALS) {
      error(r, 400, "信号番号が不正です。"); return;
    }
    activeId = d["id"].as<unsigned int>();
    if (!storeExists(activeId)) { error(r, 404, "信号がありません。"); return; }
    if (!storeRead(activeId, activeSignal)) { error(r, 500, "保存信号を読み出せません。"); return; }
    currentMode = MODE_SEND;
    pendingSend = true;  // Publish only after the entire buffer has been loaded.
    message = "送信を受け付けました。";
  }
  outcome = "pending"; ++operation;
  StaticJsonDocument<192> reply;
  reply["operation"] = operation; reply["boot"] = bootId;
  json(r, reply, 202);
}
static void finish(const char* result, const char* text) {
  if (receiving) { irrecv.disableIRIn(); receiving = false; }
  outcome = result; message = text; currentMode = MODE_IDLE;
}
void setup() {
  Serial.begin(115200);
  mutex = xSemaphoreCreateMutex();
  if (!mutex) { Serial.println("Mutex allocation failed"); while (true) delay(1000); }
  bootId = String(esp_random(), HEX);
  storageOk = storeBegin();
  Serial.printf("Storage: %s\n", storageOk ? "OK" : "ERROR (not erased)");
  irsend.begin();
  WiFi.mode(WIFI_STA); WiFi.setHostname("ir-remocon");
  WiFi.persistent(false); WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  server.on("/", HTTP_GET, [](AsyncWebServerRequest* r) {
    r->send(200, "text/html; charset=utf-8", WEB_UI);
  });
  server.on("/api/status", HTTP_GET, status);
  server.on("/api/signals", HTTP_GET, listSignals);
  server.on("/api/signals", HTTP_DELETE, deleteSignal);
  auto emptyBody = [](AsyncWebServerRequest* r) {
    if (!r->contentLength()) error(r, 400, "入力がありません。");
  };
  server.on("/api/learn", HTTP_POST, emptyBody, nullptr, commandBody);
  server.on("/api/send", HTTP_POST, emptyBody, nullptr, commandBody);
  server.onNotFound([](AsyncWebServerRequest* r) { error(r, 404, "Not found"); });
  server.begin();
}
void loop() {
  static uint32_t wifiCheck = 0;
  static bool connected = false;
  if (millis() - wifiCheck >= 1000) {
    wifiCheck = millis();
    bool now = WiFi.status() == WL_CONNECTED;
    if (now && !connected) {
      Serial.printf("Open http://%s/\n", WiFi.localIP().toString().c_str());
      MDNS.end();
      if (MDNS.begin("ir-remocon")) MDNS.addService("http", "tcp", 80);
    }
    if (!now && millis() % 10000 < 1000) { Serial.println("Waiting for Wi-Fi (2.4 GHz)..."); WiFi.reconnect(); }
    connected = now;
  }
  bool sendNow = false;
  {
    Guard g;
    if (!g.held) { delay(1); return; }
    if (pendingSend) { pendingSend = false; sendNow = true; }
    if (currentMode == MODE_RECEIVE) {
      if (millis() - started >= LEARN_MS) {
        finish("timeout", overflowSeen ? "信号が長すぎて保存できませんでした。短く押して再試行してください。" : "時間内に受信できませんでした。配線と向きを確認してください。");
      } else {
        if (!receiving) { irrecv.enableIRIn(); receiving = true; }
        if (irrecv.decode(&results)) {
          uint16_t count = getCorrectedRawLength(&results);
          if (results.overflow || !count || count > MAX_RAW_LEN) {
            overflowSeen = true; irrecv.resume();
          } else {
            uint16_t* raw = resultToRawArray(&results);
            if (!raw) finish("error", "メモリ不足で学習できませんでした。");
            else {
              activeSignal.length = count;
              memcpy(activeSignal.raw, raw, count * sizeof(uint16_t)); delete[] raw;
              bool ok = storeWrite(activeId, activeSignal);
              finish(ok ? "success" : "error", ok ? "学習して保存しました。送信を試してください。" : "保存に失敗しました。再試行してください。");
            }
          }
        }
      }
    }
  }
  if (sendNow) {
    // MODE_SEND prevents buffer changes; HTTP status remains available during IR.
    irsend.sendRaw(activeSignal.raw, activeSignal.length, 38);
    xSemaphoreTake(mutex, portMAX_DELAY);
    lastSendAt = millis(); sentOnce = true;
    finish("success", "送信処理が完了しました。家電が反応したか確認してください。");
    xSemaphoreGive(mutex);
  }
  delay(1);
}

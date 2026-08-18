/*
 * ESP32 Smart IR Remote Firmware
 * Version: 1.2.2
 * * Changelog:
 * - v1.2.2:
 * - Fixed '400 Bad Request' error on /mode endpoint by removing redundant JSON deserialization,
 * mirroring the fix applied to the /ir/send endpoint.
 * - v1.2.1:
 * - Fixed '400 Bad Request' error on /ir/send endpoint by removing redundant JSON deserialization.
 * The handler now correctly casts the JsonVariant to a JsonObject.
 * - v1.2.0:
 * - Fixed "HW TIMER NEVER INIT ERROR" by managing IRrecv state explicitly.
 * - v1.1.0:
 * - Implemented state machine for idle, receive, send modes.
 * - v1.0.0:
 * - Initial version with basic Wi-Fi and Web server setup.
 */

// -----------------------------------------------------------------------------
// 1. ライブラリのインポート
// -----------------------------------------------------------------------------
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRrecv.h>
#include <IRutils.h>

// -----------------------------------------------------------------------------
// 2. 定数とグローバル変数の設定
// -----------------------------------------------------------------------------

// --- Wi-Fi設定 (ご自身の環境に合わせて変更してください) ---
const char* WIFI_SSID = "your_wifi_ssid";
const char* WIFI_PASSWORD = "your_wifi_password";

// --- GPIOピン設定 ---
const uint16_t IR_RECV_PIN = 15; // 赤外線受信モジュールのピン
const uint16_t IR_SEND_PIN = 4;  // 赤外線LEDのピン

// --- 赤外線設定 ---
const uint16_t RECV_BUFFER_SIZE = 1024; // 受信バッファサイズ
const uint8_t RECV_TIMEOUT = 50;        // 信号の終端を検出するまでのタイムアウト(ms)

// --- 動作モード定義 ---
enum Mode {
  MODE_IDLE,
  MODE_RECEIVE,
  MODE_SEND
};
volatile Mode currentMode = MODE_IDLE; // 現在の動作モード (揮発性)

// --- Webサーバー ---
AsyncWebServer server(80);

// --- 赤外線送受信オブジェクト ---
IRsend irsend(IR_SEND_PIN);
IRrecv irrecv(IR_RECV_PIN, RECV_BUFFER_SIZE, RECV_TIMEOUT, true);
decode_results results; // デコード結果を格納する構造体

// --- 受信モード用変数 ---
unsigned long receiveStartTime = 0;
unsigned long receiveTimeout = 10000; // デフォルトのタイムアウトは10秒
String callbackUrl = "";

// -----------------------------------------------------------------------------
// 3. Wi-Fi接続関数
// -----------------------------------------------------------------------------
void connectToWiFi() {
  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int retries = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (++retries > 20) {
      Serial.println("\nFailed to connect to WiFi. Restarting...");
      ESP.restart();
    }
  }

  Serial.println("\nWiFi connected!");
  Serial.print("IP Address: ");
  Serial.println(WiFi.localIP());
}

// -----------------------------------------------------------------------------
// 4. APIエンドポイントのハンドラ関数
// -----------------------------------------------------------------------------

// [GET] /status
void handleGetStatus(AsyncWebServerRequest *request) {
  StaticJsonDocument<256> doc;
  doc["status"] = "ok";
  doc["device_mode"] = (currentMode == MODE_IDLE) ? "idle" : 
                      (currentMode == MODE_RECEIVE) ? "receive" : "send";
  doc["wifi_ssid"] = WiFi.SSID();
  doc["ip_address"] = WiFi.localIP().toString();
  
  String response;
  serializeJson(doc, response);
  request->send(200, "application/json", response);
}

// [POST] /ir/send
void handleIrSend(AsyncWebServerRequest *request, JsonVariant &json) {
  if (currentMode != MODE_IDLE) {
    request->send(409, "application/json", "{\"status\":\"error\",\"message\":\"Device is busy.\"}");
    return;
  }
  
  JsonObject doc = json.as<JsonObject>();

  if (doc.isNull() || !doc.containsKey("format") || !doc.containsKey("data") || doc["format"] != "raw") {
    request->send(400, "application/json", "{\"status\":\"error\",\"message\":\"Invalid request body.\"}");
    return;
  }

  currentMode = MODE_SEND;
  
  uint16_t freq = doc["freq"] | 38;
  JsonArray data = doc["data"];
  uint16_t rawData[data.size()];
  for (int i = 0; i < data.size(); i++) {
    rawData[i] = data[i].as<uint16_t>();
  }

  Serial.printf("Sending IR signal (freq: %d kHz, data size: %d)...\n", freq, data.size());
  irsend.sendRaw(rawData, data.size(), freq);
  Serial.println("Signal sent.");

  request->send(200, "application/json", "{\"status\":\"ok\",\"message\":\"Signal sent successfully.\"}");
  currentMode = MODE_IDLE;
}

// [PUT] /mode
void handleSetMode(AsyncWebServerRequest *request, JsonVariant &json) {
  if (currentMode != MODE_IDLE) {
    request->send(409, "application/json", "{\"status\":\"error\",\"message\":\"Device is busy.\"}");
    return;
  }

  // MODIFIED: 渡されたJsonVariantをJsonObjectとして直接扱う
  JsonObject doc = json.as<JsonObject>();

  // MODIFIED: JsonObjectを直接チェックする
  if (doc.isNull() || !doc.containsKey("mode") || doc["mode"] != "receive" || !doc.containsKey("callback_url")) {
    request->send(400, "application/json", "{\"status\":\"error\",\"message\":\"Invalid request body.\"}");
    return;
  }
  
  currentMode = MODE_RECEIVE;
  receiveTimeout = doc["timeout"] | 10000;
  callbackUrl = doc["callback_url"].as<String>();
  receiveStartTime = millis();

  irrecv.enableIRIn(); 
  
  Serial.printf("Switching to receive mode. Timeout: %lu ms, Callback: %s\n", receiveTimeout, callbackUrl.c_str());
  
  request->send(202, "application/json", "{\"status\":\"ok\",\"message\":\"Switching to receive mode. Waiting for signal.\"}");
}


// -----------------------------------------------------------------------------
// 5. セットアップ関数
// -----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  while (!Serial);

  irsend.begin();

  connectToWiFi();

  DefaultHeaders::Instance().addHeader("Access-Control-Allow-Origin", "*");

  server.on("/status", HTTP_GET, handleGetStatus);
  
  server.on(
    "/ir/send", HTTP_POST, 
    [](AsyncWebServerRequest *request){}, NULL, 
    [](AsyncWebServerRequest *request, uint8_t *data, size_t len, size_t index, size_t total) {
      StaticJsonDocument<2048> jsonDoc;
      if (deserializeJson(jsonDoc, (const char*)data) == DeserializationError::Ok) {
        JsonVariant json = jsonDoc.as<JsonVariant>();
        handleIrSend(request, json);
      } else {
        request->send(400, "application/json", "{\"status\":\"error\",\"message\":\"Invalid JSON.\"}");
      }
    }
  );

  server.on(
    "/mode", HTTP_PUT, 
    [](AsyncWebServerRequest *request){}, NULL, 
    [](AsyncWebServerRequest *request, uint8_t *data, size_t len, size_t index, size_t total) {
      StaticJsonDocument<512> jsonDoc;
      if (deserializeJson(jsonDoc, (const char*)data) == DeserializationError::Ok) {
        JsonVariant json = jsonDoc.as<JsonVariant>();
        handleSetMode(request, json);
      } else {
        request->send(400, "application/json", "{\"status\":\"error\",\"message\":\"Invalid JSON.\"}");
      }
    }
  );

  server.begin();
  Serial.println("HTTP server started.");
}

// -----------------------------------------------------------------------------
// 6. メインループ関数
// -----------------------------------------------------------------------------
void loop() {
  if (currentMode == MODE_RECEIVE) {
    if (irrecv.decode(&results)) {
      irrecv.disableIRIn();

      Serial.println("IR signal received!");
      
      StaticJsonDocument<2048> doc;
      doc["format"] = "raw";
      doc["freq"] = 38;
      
      JsonArray data = doc.createNestedArray("data");
      uint16_t *rawData = resultToRawArray(&results);
      for (int i = 1; i < results.rawlen; i++) {
        data.add(rawData[i]);
      }
      delete[] rawData;
      
      String output;
      serializeJson(doc, output);
      
      HTTPClient http;
      http.begin(callbackUrl);
      http.addHeader("Content-Type", "application/json");
      int httpCode = http.POST(output);
      
      if (httpCode > 0) {
        Serial.printf("POST to callback server successful, code: %d\n", httpCode);
      } else {
        Serial.printf("POST to callback server failed, error: %s\n", http.errorToString(httpCode).c_str());
      }
      http.end();

      currentMode = MODE_IDLE;
      Serial.println("Returning to idle mode.");
      
    } else if (millis() - receiveStartTime > receiveTimeout) {
      irrecv.disableIRIn();
      currentMode = MODE_IDLE;
      Serial.println("Receive mode timed out. Returning to idle mode.");
    }
  }
}
/*
 * ESP32 Smart IR Remote Firmware
 * Version: 2.1.0
 *
 * サーバ側 (FastAPI / ir-remocon) との契約は AGENTS.md と
 * tools/fake_esp32.py が正。**このファームを直す前に必ず両方を読むこと。**
 *
 *   GET  /status   -> 200 {"status","device_mode","send_count","last_send_ok","queue_len",...}
 *   POST /ir/send  -> 202 (キュー投入) / 400 (不正) / 409 (ビジー)
 *                     body: {"format":"raw","freq":38,"data":[uint16,...]}
 *   PUT  /mode     -> 202 (受信モードへ) / 400 / 409
 *                     body: {"mode":"receive","timeout":15000,"callback_url":"http://.../<token>"}
 *   受信したら callback_url へ POST {"format":"raw","freq":38,"data":[...]}
 *
 * Changelog:
 * - v2.1.0 (Phase 6, 実機検証で判明した学習の不安定さに対処):
 *   - **ノイズで学習が終わってしまう問題を修正。** 最初の decode() でセッションを
 *     打ち切っていたため、照明のちらつき等が 1 発入るだけで学習ウィンドウを
 *     使い切っていた。採用できない捕捉は捨てて待受を続けるようにした。
 *   - setUnknownThreshold() を設定 (既定の 6 要素はノイズが素通りする)。
 *   - /status に reject_count / last_reject_len / min_accept_raw_len を追加。
 * - v2.0.0 (Phase 6):
 *   - 送信を loop() へ退避し /ir/send は 202 を即返す。AsyncTCP タスクを
 *     ブロックしなくなった (不具合 C の ESP 側の根治)。
 *   - ボディ分割受信に対応 (index/total を尊重)。deserializeJson に長さを明示。
 *   - 受信バッファ溢れを検知したらコールバックせずエラーにする
 *     (「学習は成功したのに送信しても効かない」の根絶)。
 *   - 可変長配列を撤廃し static バッファ + 境界チェックに。
 *   - /status に send_count / last_send_ok / queue_len / 診断情報を追加。
 *   - 静的 IP 対応 (DHCP フォールバック付き) と Wi-Fi 自動復帰。
 *   - while (!Serial) を削除。CORS ヘッダを削除。
 * - v1.2.2: /mode の冗長な JSON 再パースを除去して 400 を解消。
 * - v1.2.1: /ir/send について同上。
 * - v1.2.0: IRrecv の状態を明示管理して "HW TIMER NEVER INIT ERROR" を解消。
 * - v1.1.0: idle / receive / send の状態機械を導入。
 * - v1.0.0: Wi-Fi と Web サーバの初期実装。
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRrecv.h>
#include <IRutils.h>

// =============================================================================
// 1. 設定 — 書き込み前に編集するのはこのブロックだけ
// =============================================================================

// --- Wi-Fi 認証情報 ---
// WIFI_SSID / WIFI_PASSWORD は secrets.h で定義する (.gitignore 済み)。
// 初回は secrets.h.example を secrets.h にコピーして値を入れること。
#if __has_include("secrets.h")
  #include "secrets.h"
#else
  #error "secrets.h がありません。secrets.h.example を secrets.h にコピーして Wi-Fi の値を入れてください。"
#endif

// --- IP 設定 ---
// 0 にすると DHCP になる。静的 IP はルーターの DHCP 割当**範囲外**から選ぶこと
// (範囲内だと後から他機器と衝突する)。
#define USE_STATIC_IP 1
IPAddress STATIC_IP(192, 168, 1,  50);
IPAddress GATEWAY  (192, 168, 1,   1);
IPAddress SUBNET   (255, 255, 255, 0);
IPAddress DNS1     (192, 168, 1,   1);

const char* HOSTNAME = "ir-remocon";

// --- GPIO ピン ---
const uint16_t IR_RECV_PIN = 13;  // 赤外線受信モジュール
const uint16_t IR_SEND_PIN = 4;   // 赤外線 LED

// =============================================================================
// 2. 定数
// =============================================================================

static const char* FIRMWARE_VERSION = "2.1.0";

static const uint16_t RECV_BUFFER_SIZE = 1024;  // IRrecv の生バッファ (要素数)
static const uint8_t  RECV_TIMEOUT_MS  = 50;    // 信号終端とみなす無音時間

// 送受信で扱う raw 要素数の上限。RECV_BUFFER_SIZE と揃えてある。
// これを超えるものは「黙って切り詰める」のではなく必ずエラーにする。
static const uint16_t MAX_RAW_LEN = 1024;

// リクエストボディの上限。1024 要素なら実測 7KB 程度なので十分な余裕がある。
static const size_t MAX_BODY_LEN = 16384;

// 学習で「信号」とみなす最小の生要素数。
//
// ★ これを入れないと部屋に置いてあるだけで学習が成立してしまう。
//   IRrecv の既定 kUnknownThreshold は **6**（IRrecv.h:28）で、蛍光灯・LED 照明・
//   日光のちらつきが容易に 6 要素を超えるため、decode() がノイズで成功を返す。
//   実在するリモコン信号は最短の Sony 12bit でも rawlen が 26 程度あるので、
//   24 なら正規の信号を落とさずにノイズだけを弾ける。
//   環境ノイズが強くて足りない場合はシリアルの「ノイズとして破棄」行に出る
//   実測値を見て上げること。
static const uint16_t MIN_ACCEPT_RAW_LEN = 24;

static const uint32_t WIFI_CONNECT_TIMEOUT_MS = 10000;  // 1 回の接続試行の上限
static const uint32_t WIFI_CHECK_INTERVAL_MS  = 10000;  // loop() での切断チェック間隔

// =============================================================================
// 3. 状態
// =============================================================================

enum Mode { MODE_IDLE, MODE_RECEIVE, MODE_SEND };

// currentMode は「機器の排他ロック」そのもの。MODE_IDLE 以外の間は
// /ir/send も /mode も 409 を返す。
static volatile Mode currentMode = MODE_IDLE;
static portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;

// --- 送信キュー (深さ 1) ---
// AsyncTCP タスクが書き、loop() が読む。可変長配列は使わない。
static uint16_t sendBuf[MAX_RAW_LEN];
static volatile uint16_t sendLen  = 0;
static volatile uint16_t sendFreq = 38;
static volatile bool     pendingSend = false;

// --- /status 用のカウンタ ---
static volatile uint32_t sendCount        = 0;
static volatile bool     lastSendOk       = true;   // 直近の送信要求が loop() で完了したか
static volatile uint16_t lastRecvLen      = 0;      // 直近の受信要素数 (溢れ判定の材料)
static volatile bool     lastRecvOverflow = false;  // 直近の受信でバッファが溢れたか
// 破棄した捕捉の統計。環境ノイズの強さを画面から見るための材料。
static volatile uint32_t rejectCount      = 0;      // 起動以降に破棄した捕捉の数
static volatile uint16_t lastRejectLen    = 0;      // 直近に破棄した捕捉の生要素数
static bool usingStaticIp = false;

// --- 受信モード用 ---
static unsigned long receiveStartTime = 0;
static unsigned long receiveTimeout   = 10000;
static String callbackUrl = "";

// --- Wi-Fi 監視用 ---
static unsigned long lastWifiCheck = 0;

AsyncWebServer server(80);
IRsend irsend(IR_SEND_PIN);
IRrecv irrecv(IR_RECV_PIN, RECV_BUFFER_SIZE, RECV_TIMEOUT_MS, true);
decode_results results;

// =============================================================================
// 4. 小道具
// =============================================================================

static void sendError(AsyncWebServerRequest* request, int code, const char* message) {
  StaticJsonDocument<256> doc;
  doc["status"]  = "error";
  doc["message"] = message;
  String out;
  serializeJson(doc, out);
  request->send(code, "application/json", out);
}

/*
 * 機器を占有する (test-and-set)。取れたら true。
 *
 * ESPAsyncWebServer のハンドラは全て単一の AsyncTCP タスク上で走るので
 * ハンドラ同士は本来並行しないが、意図を明示するためにクリティカルセクションで
 * 囲んである。**バッファのコピーはここに含めない** — クリティカルセクションを
 * 長く持つと割り込みが止まり、赤外線の受信タイミングに影響する。
 */
static bool claimMode(Mode target) {
  bool claimed = false;
  portENTER_CRITICAL(&stateMux);
  if (currentMode == MODE_IDLE) {
    currentMode = target;
    claimed = true;
  }
  portEXIT_CRITICAL(&stateMux);
  return claimed;
}

/*
 * 分割して届くリクエストボディを request->_tempObject に組み立てる。
 * ボディが全て揃ったときだけ true を返す。
 *
 * 旧実装は index/total を無視して最初の 1 パケットだけをパースしていたため、
 * 長い信号 (エアコン等) がパケット分割されると必ず 400 になっていた。
 *
 * ★ 確保は必ず malloc で行うこと。AsyncWebServerRequest のデストラクタが
 *   _tempObject を free() するので、new で確保すると解放の仕方が食い違う。
 */
static bool collectBody(AsyncWebServerRequest* request, uint8_t* data, size_t len,
                        size_t index, size_t total) {
  if (index == 0) {
    if (total == 0) {
      sendError(request, 400, "Empty request body.");
      return false;
    }
    if (total > MAX_BODY_LEN) {
      sendError(request, 413, "Request body too large.");
      return false;
    }
    request->_tempObject = malloc(total + 1);
    if (request->_tempObject == NULL) {
      sendError(request, 500, "Out of memory.");
      return false;
    }
  }
  // 確保に失敗した / 既にエラーを返した後の後続チャンクは黙って捨てる。
  if (request->_tempObject == NULL) return false;

  memcpy((uint8_t*)request->_tempObject + index, data, len);
  if (index + len != total) return false;  // まだ揃っていない

  ((char*)request->_tempObject)[total] = '\0';
  return true;
}

// =============================================================================
// 5. Wi-Fi
// =============================================================================

// 静的 IP 設定が自己矛盾していないか (IP と GW が同一サブネットにあるか) を見る。
// サブネットマスクや GW の打ち間違いはこれで拾える。
// ただし「設定としては正しいが、この LAN が実は 192.168.0.x だった」ようなケースは
// 拾えない — AP へのアソシエート自体は成功してしまうため。最終確認はブラウザで
// http://<設定した IP>/status を開くこと。
static bool staticConfigLooksSane() {
  uint32_t ip   = (uint32_t)STATIC_IP;
  uint32_t gw   = (uint32_t)GATEWAY;
  uint32_t mask = (uint32_t)SUBNET;
  if (mask == 0) return false;
  if (ip == 0 || ip == 0xFFFFFFFF) return false;
  return (ip & mask) == (gw & mask);
}

static bool waitForWifi(uint32_t timeoutMs) {
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > timeoutMs) return false;
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  return true;
}

static void connectToWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);  // mode() の後・begin() の前でないと効かない
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);

#if USE_STATIC_IP
  if (staticConfigLooksSane()) {
    if (WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS1)) {
      usingStaticIp = true;
      Serial.printf("静的 IP を設定しました: %s (GW %s)\n",
                    STATIC_IP.toString().c_str(), GATEWAY.toString().c_str());
    } else {
      Serial.println("WiFi.config() に失敗しました。DHCP で続行します。");
    }
  } else {
    Serial.println("静的 IP 設定が矛盾しています (IP と GW が別サブネット)。DHCP で続行します。");
  }
#endif

  Serial.printf("Wi-Fi に接続します: %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  if (!waitForWifi(WIFI_CONNECT_TIMEOUT_MS)) {
    if (usingStaticIp) {
      // フェイルセーフ: 静的設定で繋がらないなら DHCP に戻してもう一度。
      // 設定ミスで機器に一切アクセスできなくなる事態を防ぐ。
      Serial.println("\n静的 IP では接続できませんでした。DHCP で再試行します。");
      WiFi.disconnect(true);
      WiFi.config(INADDR_NONE, INADDR_NONE, INADDR_NONE);  // DHCP に戻す
      usingStaticIp = false;
      Serial.printf("Wi-Fi に接続します (DHCP): %s", WIFI_SSID);
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      if (!waitForWifi(WIFI_CONNECT_TIMEOUT_MS)) {
        Serial.println("\nWi-Fi に接続できません。再起動します。");
        ESP.restart();
      }
    } else {
      Serial.println("\nWi-Fi に接続できません。再起動します。");
      ESP.restart();
    }
  }

  // 「静的 / DHCP のどちらで、どの IP を取ったか」は必ず出す。
  // 書き込み後にユーザーがシリアルモニタ (115200bps) で確認する唯一の手掛かり。
  Serial.println("Wi-Fi 接続完了");
  Serial.printf("  取得方法 : %s\n", usingStaticIp ? "静的 IP" : "DHCP");
  Serial.printf("  IP       : %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("  GW       : %s\n", WiFi.gatewayIP().toString().c_str());
  Serial.printf("  Subnet   : %s\n", WiFi.subnetMask().toString().c_str());
  Serial.printf("  ホスト名 : %s\n", WiFi.getHostname());
  Serial.printf("  MAC      : %s\n", WiFi.macAddress().c_str());
  Serial.printf("  RSSI     : %d dBm\n", WiFi.RSSI());
}

// 起動時だけでなく実行中の切断からも復帰する (旧実装は起動時のみだった)。
static void maintainWiFi() {
  if (millis() - lastWifiCheck < WIFI_CHECK_INTERVAL_MS) return;
  lastWifiCheck = millis();
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.println("Wi-Fi が切断されています。再接続します...");
  WiFi.reconnect();
}

// =============================================================================
// 6. HTTP ハンドラ
// =============================================================================

// [GET] /status — モードを問わず必ず即答すること。
// サーバの get_status() は 3 秒しか待たない (esp32.py の _status_timeout)。
static void handleGetStatus(AsyncWebServerRequest* request) {
  // キーを足したらここも増やすこと (溢れると JSON が途中で切れる)。
  StaticJsonDocument<1024> doc;
  Mode m = currentMode;

  doc["status"] = "ok";
  doc["device_mode"] = (m == MODE_IDLE)    ? "idle"
                     : (m == MODE_RECEIVE) ? "receive"
                                           : "send";
  doc["wifi_ssid"]  = WiFi.SSID();
  doc["ip_address"] = WiFi.localIP().toString();

  // サーバ / スタブが参照する契約上のキー
  doc["send_count"]   = sendCount;
  doc["last_send_ok"] = lastSendOk;
  doc["queue_len"]    = pendingSend ? 1 : 0;

  // 診断情報。設定タブが key=value で全部そのまま描画するのでフロント無変更で見える。
  // last_recv_overflow が true なら「学習は動いたが信号が長すぎて捨てた」の意。
  doc["firmware_version"]   = FIRMWARE_VERSION;
  doc["static_ip"]          = usingStaticIp;
  doc["uptime_ms"]          = millis();
  doc["free_heap"]          = ESP.getFreeHeap();
  doc["rssi"]               = WiFi.RSSI();
  doc["last_recv_len"]      = lastRecvLen;
  doc["last_recv_overflow"] = lastRecvOverflow;
  // 破棄した捕捉の統計。reject_count がじりじり増えるなら環境ノイズが乗っている。
  doc["reject_count"]       = rejectCount;
  doc["last_reject_len"]    = lastRejectLen;
  doc["min_accept_raw_len"] = MIN_ACCEPT_RAW_LEN;

  String out;
  serializeJson(doc, out);
  request->send(200, "application/json", out);
}

// [POST] /ir/send — 検証してキューに積み、202 を即返す。実際の送信は loop()。
static void handleIrSendBody(AsyncWebServerRequest* request, uint8_t* data, size_t len,
                             size_t index, size_t total) {
  if (!collectBody(request, data, len, index, total)) return;

  // MAX_RAW_LEN 要素ぶんちょうど入る大きさ。これを超えると NoMemory になるので
  // 「黙って切り詰める」ことがない。約 8.7KB になるので、AsyncTCP タスクの
  // スタック (CONFIG_ASYNC_TCP_STACK_SIZE = 16KB) を圧迫しないよう
  // 必ずヒープ (DynamicJsonDocument) に置くこと。StaticJsonDocument は不可。
  DynamicJsonDocument doc(JSON_ARRAY_SIZE(MAX_RAW_LEN) + 512);
  DeserializationError err = deserializeJson(doc, (const char*)request->_tempObject, total);
  if (err == DeserializationError::NoMemory) {
    sendError(request, 400, "Signal too long (max 1024 elements).");
    return;
  }
  if (err) {
    sendError(request, 400, "Invalid JSON.");
    return;
  }

  JsonVariantConst root = doc.as<JsonVariantConst>();
  if (!root.is<JsonObjectConst>() || root["format"] != "raw" ||
      !root["data"].is<JsonArrayConst>()) {
    sendError(request, 400, "Invalid request body.");
    return;
  }

  JsonArrayConst arr = root["data"].as<JsonArrayConst>();
  if (arr.size() == 0) {
    sendError(request, 400, "data must not be empty.");
    return;
  }
  if (arr.size() > MAX_RAW_LEN) {
    sendError(request, 400, "Signal too long (max 1024 elements).");
    return;
  }

  // 排他は検証を全て通してから取る。先に取ると 400 の経路ごとに解放が要り、
  // 1 箇所でも漏らすと機器が永久にビジーのままになる。
  if (!claimMode(MODE_SEND)) {
    sendError(request, 409, "Device is busy.");
    return;
  }

  uint16_t n = 0;
  for (JsonVariantConst v : arr) sendBuf[n++] = v.as<uint16_t>();
  sendLen  = n;
  sendFreq = root["freq"] | 38;
  lastSendOk = false;  // loop() が完了させるまでは「未完了」

  // ★ pendingSend の公開は必ずバッファのコピーが終わった後。
  //   ここを上に動かすと loop() が書きかけのバッファを送信する。
  pendingSend = true;

  Serial.printf("送信をキューに入れました (freq: %u kHz, %u 要素)\n", sendFreq, n);
  request->send(202, "application/json",
                "{\"status\":\"queued\",\"message\":\"Send queued.\"}");
}

// [PUT] /mode — 受信モードへ移行する。
static void handleModeBody(AsyncWebServerRequest* request, uint8_t* data, size_t len,
                           size_t index, size_t total) {
  if (!collectBody(request, data, len, index, total)) return;

  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, (const char*)request->_tempObject, total);
  if (err) {
    sendError(request, 400, "Invalid JSON.");
    return;
  }

  JsonVariantConst root = doc.as<JsonVariantConst>();
  if (!root.is<JsonObjectConst>() || root["mode"] != "receive") {
    sendError(request, 400, "Invalid request body.");
    return;
  }
  const char* cb = root["callback_url"];
  if (cb == NULL || strlen(cb) == 0) {
    sendError(request, 400, "callback_url is required.");
    return;
  }

  if (!claimMode(MODE_RECEIVE)) {
    sendError(request, 409, "Device is busy.");
    return;
  }

  // ★ callback_url はサーバから渡されたものをそのまま使う。
  //   URL を組み立て直したり信号名を足したりしてはいけない
  //   (パス末尾のトークンでセッションが特定されている)。
  callbackUrl      = cb;
  receiveTimeout   = root["timeout"] | 10000UL;
  receiveStartTime = millis();
  lastRecvLen      = 0;
  lastRecvOverflow = false;

  irrecv.enableIRIn();

  Serial.printf("受信モードへ移行します。timeout: %lu ms, callback: %s\n",
                receiveTimeout, callbackUrl.c_str());
  request->send(202, "application/json",
                "{\"status\":\"ok\",\"message\":\"Switching to receive mode.\"}");
}

// =============================================================================
// 7. 受信結果の処理 (loop() から呼ばれる)
// =============================================================================

static void postCallback(const String& body) {
  HTTPClient http;
  if (!http.begin(callbackUrl)) {
    Serial.println("コールバック URL を解釈できませんでした");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  if (code > 0) {
    Serial.printf("コールバック送信: HTTP %d\n", code);
  } else {
    Serial.printf("コールバック失敗: %s\n", http.errorToString(code).c_str());
  }
  http.end();
}

// 捕捉を 1 件処理する。**採用してコールバックまで済ませたときだけ true**。
// false を返したときは呼び出し側が待受を続ける (セッションを終了しない)。
static bool handleReceivedSignal() {
  // --- ノイズの門前払い ---
  // ここで弾けなかった分がそのまま「学習したのに変な信号」になる。
  if (results.rawlen < MIN_ACCEPT_RAW_LEN) {
    rejectCount++;
    lastRejectLen = results.rawlen;
    Serial.printf("ノイズとして破棄 (rawlen=%u < %u)。待受を続けます [累計 %lu 件]\n",
                  results.rawlen, MIN_ACCEPT_RAW_LEN, (unsigned long)rejectCount);
    return false;
  }

  Serial.printf("赤外線信号を受信しました (rawlen=%u)\n", results.rawlen);

  // results.overflow は IRrecv 側で生バッファ (RECV_BUFFER_SIZE) が
  // 埋まりきったことを示す。これが立っている時点で信号は既に不完全。
  bool overflow = results.overflow;

  uint16_t* rawData = resultToRawArray(&results);
  if (rawData == NULL) {
    Serial.println("resultToRawArray() のメモリ確保に失敗しました");
    lastRecvLen = 0;
    lastRecvOverflow = true;
    return false;
  }

  // ★ 添字に注意 — 旧実装 (v1.2.2) はここを 1 要素ずらして壊していた。
  //
  //   resultToRawArray() が返す配列は **0 起点**で、result[0] は rawbuf[1] を
  //   変換したもの = 信号の先頭マークである (IRutils.cpp:427-443 で確認)。
  //   配列長は rawlen-1 ではなく getCorrectedRawLength() — UINT16_MAX を超える
  //   間隔は {UINT16_MAX, 0} の 2 要素に分割されるため、長い信号ほど伸びる。
  //
  //   旧実装の `for (i = 1; i < rawlen; i++)` は
  //     (a) result[0] = 先頭マークを落とし、
  //     (b) result[rawlen-1] を配列外読みしていた。
  //   先頭マークを失った配列を sendRaw() に渡すと、マークとスペースが総入れ替えに
  //   なって家電が反応しない。「学習は成功したのに送信しても効かない」の有力な原因。
  //
  //   DB にある既存 2 件が 3040(マーク), 1442(スペース), ... と正しく並んでいるのは、
  //   これらが IRrecvDumpV2 相当 (resultToSourceCode() も同じ 0 起点) で採取された
  //   ものだから。この形が正であり、下のループはそれに揃えてある。
  uint16_t count = getCorrectedRawLength(&results);
  lastRecvLen = count;

  if (count == 0) overflow = true;
  if (count > MAX_RAW_LEN) overflow = true;

  if (overflow) {
    // ★ 切り詰めた信号をコールバックしない。
    //   旧実装はここで黙って途中まで送っていたため、「学習は成功したのに
    //   送信しても家電が反応しない」という形でしか気づけなかった。
    lastRecvOverflow = true;
    rejectCount++;
    lastRejectLen = results.rawlen;
    // 待受は続ける。溢れの多くは直前のノイズが混ざったせいなので、
    // 押し直してもらえば次の捕捉で成功する見込みがある。
    Serial.printf("受信バッファが溢れました (rawlen=%u, 要素数=%u)。"
                  "破棄して待受を続けます。もう一度押してください。\n",
                  results.rawlen, count);
    delete[] rawData;
    return false;
  }

  DynamicJsonDocument doc(JSON_ARRAY_SIZE(count) + 256);
  doc["format"] = "raw";
  // freq は実測できない (IRremoteESP8266 に搬送波周波数の測定手段が無い)。
  // サーバ側も freq を保存しておらず、送信は常に 38kHz で行われる。
  doc["freq"] = 38;
  JsonArray arr = doc.createNestedArray("data");
  for (uint16_t i = 0; i < count; i++) arr.add(rawData[i]);
  delete[] rawData;

  if (doc.overflowed()) {
    lastRecvOverflow = true;
    rejectCount++;
    lastRejectLen = results.rawlen;
    Serial.printf("JSON バッファが溢れました (要素数=%u)。破棄して待受を続けます。\n", count);
    return false;
  }

  String body;
  serializeJson(doc, body);
  Serial.printf("受信完了: %u 要素。コールバックします。\n", count);
  postCallback(body);
  return true;
}

// =============================================================================
// 8. setup / loop
// =============================================================================

void setup() {
  Serial.begin(115200);
  // while (!Serial) は入れないこと。シリアル未接続のヘッドレス起動で
  // ブートが永久に止まる。

  Serial.println();
  Serial.printf("ir-remocon ESP32 firmware v%s\n", FIRMWARE_VERSION);

  irsend.begin();

  // ライブラリ側でも短すぎる UNKNOWN を成功扱いしないようにする。
  // 既定は 6 要素しかなく、環境ノイズがそのまま「受信成功」になる。
  irrecv.setUnknownThreshold(MIN_ACCEPT_RAW_LEN);

  connectToWiFi();

  // CORS ヘッダは意図的に付けない。この機器に話しかけるのは FastAPI サーバだけで、
  // サーバ間通信に CORS は関係しない。Access-Control-Allow-Origin: * を付けると
  // 「ユーザーがたまたま開いた Web ページが LAN 内のこの機器に POST できる」
  // 状態を作るだけで、得るものが無い。
  // ブラウザで直接 http://<ip>/status を開く確認手順はトップレベル遷移なので
  // ヘッダ無しでも動く。

  server.on("/status", HTTP_GET, handleGetStatus);

  server.on("/ir/send", HTTP_POST,
            [](AsyncWebServerRequest* request) {},  // onRequest: ボディ側で応答する
            NULL,                                    // onUpload: 使わない
            handleIrSendBody);

  server.on("/mode", HTTP_PUT,
            [](AsyncWebServerRequest* request) {},
            NULL,
            handleModeBody);

  server.onNotFound([](AsyncWebServerRequest* request) {
    sendError(request, 404, "Not found.");
  });

  server.begin();
  Serial.println("HTTP サーバを開始しました");
}

void loop() {
  maintainWiFi();

  // --- 送信 ---
  // ハンドラではなくここで送る。irsend.sendRaw() は数百 ms ブロックするので、
  // AsyncTCP タスク上で実行すると送信中は全ての HTTP が停止する (不具合 C)。
  if (pendingSend) {
    Serial.printf("赤外線を送信します (freq: %u kHz, %u 要素)...\n", sendFreq, sendLen);
    irsend.sendRaw(sendBuf, sendLen, sendFreq);
    Serial.println("送信完了");

    sendCount++;
    lastSendOk  = true;
    pendingSend = false;

    // ★ MODE_IDLE への解放は必ず最後。先に解放すると、送信がまだ終わって
    //   いないうちに次のリクエストが sendBuf を上書きできてしまう。
    currentMode = MODE_IDLE;
  }

  // --- 受信 ---
  if (currentMode == MODE_RECEIVE) {
    if (irrecv.decode(&results)) {
      // ★ 最初の捕捉で打ち切らないこと。
      //   照明のちらつき等のノイズでも decode() は成功を返すので、ここで
      //   無条件に MODE_IDLE へ落とすと「ノイズが 1 発入っただけで
      //   15 秒の学習ウィンドウが終わる」= ユーザーにはタイムアウトに見える。
      //   採用できなかった捕捉は捨てて、タイムアウトまで聞き続ける。
      if (handleReceivedSignal()) {
        irrecv.disableIRIn();
        currentMode = MODE_IDLE;
        Serial.println("待機モードに戻ります");
      } else {
        irrecv.resume();   // 次の捕捉を待つ
      }
    } else if (millis() - receiveStartTime > receiveTimeout) {
      irrecv.disableIRIn();
      currentMode = MODE_IDLE;
      // タイムアウトはサーバへ通知しない。サーバ側が学習セッションの
      // expires_at で自前に判定している (routers/learn.py)。
      Serial.printf("受信モードがタイムアウトしました (破棄した捕捉 累計 %lu 件)。"
                    "待機モードに戻ります\n", (unsigned long)rejectCount);
    }
  }
}

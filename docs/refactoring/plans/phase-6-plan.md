# Phase 6 — ESP32 ファーム（`esp/ir_remocon/ir_remocon.ino`）

> Codex 移管メモ: これは Claude Code で作成した計画の履歴スナップショット。
> 現在の作業規約は `../../../AGENTS.md`、実装後の確定事項は `../HANDOFF.md` を優先する。

## Context

Phase 1〜5 でサーバ側は作り直しが終わった。残っているのは **ESP32 のファームだけ**で、
現物は `esp/temp.ino`（v1.2.2 / 261 行）が Phase 1 のコミット以降ノータッチのまま残っている。

このファームには、サーバ側では直せない欠陥が積み残っている:

| 箇所 | 症状 |
|---|---|
| `temp.ino:134` | `irsend.sendRaw()` を AsyncTCP タスク内で同期実行し、送信中は TCP ごと停止（**不具合 C の ESP 側**。サーバ側は Phase 2 で直列化して緩和済みだが根治していない） |
| `temp.ino:188,202` | `onBody` が `index`/`total` を無視。ボディが TCP パケットに分割されると**断片をパースして 400** |
| `temp.ino:190,204` | `deserializeJson(doc, (const char*)data)` が非 NUL 終端バッファを読む |
| `temp.ino:227-235` | 受信結果を `StaticJsonDocument<2048>` に詰めるが `doc.overflowed()` を見ない。**黙って切り詰めた信号をコールバックする** → 「学習は成功したのに送信しても効かない」（AGENTS.md:481-483 の未解決疑問の第一容疑者） |
| `temp.ino:128` | `uint16_t rawData[data.size()]` — AsyncTCP タスク（スタック 8KB）上の可変長配列 |
| `temp.ino:98-107` | `/status` に `send_count` / `last_send_ok` / `queue_len` が無い（`fake_esp32.py` と統合テストは既にこれらを前提にしている） |
| `temp.ino:72-90` | 静的 IP なし・Wi-Fi 再接続なし。IP が DHCP で動くことが**不具合 E の元凶**だった |
| `temp.ino:175` | `while (!Serial);` — シリアル未接続のヘッドレス起動でブートが止まる |

**意図する成果**: ESP32 を静的 IP `192.168.1.50` に固定し、送信で TCP を止めず、
受信バッファの溢れを黙殺しないファームを **コンパイルが通る状態で** 用意する。
実機書き込みは後日ユーザーが行う（AGENTS.md の絶対ルール 4）。

## 確定した設計判断（ユーザー確認済み）

1. **送信は 202 即応答**。ハンドラは検証 → 静的バッファへコピー → `202` を返し、`loop()` が
   `irsend.sendRaw()` を実行する。**サーバ側 Python は無変更**（`esp32.py:114` の
   `SUCCESS_STATUSES = {200, 202}` が既に 202 を成功扱いにしており、`tests/test_esp32.py:72-90`
   と `tests/test_send.py:50` が回帰ガードになっている）。
   代償として「赤外線が実際に出た」ことは保証しなくなるので、**フロントの文言を弱める**
   （唯一の該当箇所は `ir_remocon/static/js/tab-remote.js:64`）。
   `esp32.send_raw()` の docstring（`esp32.py:325-328`）は既にこの限界を明記済み。
2. **PlatformIO でビルド検証する**。`esp/ir_remocon/platformio.ini` を追加し `pio run` を通す。
   PlatformIO Core 6.1.18 は導入済み・**ESP32 プラットフォームは未導入**なので初回は数百MB〜1GB の
   ダウンロードが発生する。
3. **静的 IP は `192.168.1.50` / GW `192.168.1.1` / Subnet `255.255.255.0`**。
   ファイル冒頭の設定ブロックにまとめ、後から書き換えやすい形にする。

---

## 作業ステップ

### 1. ファイル配置を Arduino IDE の規約に合わせる

Arduino IDE は「スケッチ名 == 親フォルダ名」を要求する。現状の `esp/temp.ino` は
どちらの規約も満たしていない。

- `esp/temp.ino` → **`esp/ir_remocon/ir_remocon.ino`**（`git mv` してから中身を書き換える。
  diff が読める形で残す）
- 退路: `backup_before_refactor/temp.ino`（バイト同一。gitignore 済み）と git 履歴（`a966a3a`）
- `.gitignore` に `esp/**/.pio/` と `esp/**/.vscode/` を追加

### 2. `esp/ir_remocon/platformio.ini`

```ini
[platformio]
src_dir = .                 ; .ino を platformio.ini と同じ階層に置く（Arduino IDE 互換）

[env:esp32dev]
platform  = espressif32
board     = esp32dev
framework = arduino
monitor_speed = 115200
lib_deps =
  bblanchon/ArduinoJson @ ^6.21.5          ; v7 は StaticJsonDocument を非推奨化。現コードは v6 API
  crankyoldgit/IRremoteESP8266 @ ^2.8.6
  <ESPAsyncWebServer の維持フォーク>       ; 実装時に解決（下記）
```

> **ESPAsyncWebServer の注意**: 本家 `me-no-dev/ESPAsyncWebServer` は
> レジストリ上で複数のフォークに分かれている。実装時に `pio pkg search` で解決し、
> **実際にビルドが通ったものを記録する**。使う API は
> `AsyncWebServer` / `server.on(path, method, onRequest, onUpload, onBody)` /
> `request->send(code, type, body)` / `request->_tempObject` / `DefaultHeaders` だけで、
> どのフォークでも共通のため、フォーク差でビルド検証の価値は落ちない。
> ただし **ユーザーの Arduino IDE に入っているのは別フォークの可能性がある**。
> これは検証の限界として AGENTS.md に明記する。

### 3. 設定ブロック（ファイル冒頭）

```cpp
// ===== 書き込み前にここだけ編集する =====
#define USE_STATIC_IP 1                     // 0 にすると DHCP
const char* WIFI_SSID     = "your_wifi_ssid";      // ★ 未記入。コミットしないこと
const char* WIFI_PASSWORD = "your_wifi_password";  // ★ 未記入。コミットしないこと
IPAddress STATIC_IP(192, 168, 1,  50);
IPAddress GATEWAY  (192, 168, 1,   1);
IPAddress SUBNET   (255, 255, 255, 0);
IPAddress DNS1     (192, 168, 1,   1);
const char* HOSTNAME = "ir-remocon";
```

`STATIC_IP` はルーターの **DHCP 割当範囲外**である必要がある（ユーザー物理作業 1 で確認）。

### 4. 送信を `loop()` へ退避（不具合 C の根治）

状態機械そのものを排他に使う。**ESPAsyncWebServer のハンドラは全て単一の AsyncTCP タスク上で
走るのでハンドラ同士は並行しない**。競合するのは「ハンドラ ↔ `loop()`」だけなので、
書き込み順序で解決できる:

```cpp
// ハンドラ（AsyncTCP タスク）
portENTER_CRITICAL(&stateMux);              // test-and-set だけを保護（コピーは含めない）
bool claimed = (currentMode == MODE_IDLE);
if (claimed) currentMode = MODE_SEND;
portEXIT_CRITICAL(&stateMux);
if (!claimed) { request->send(409, ...); return; }

lastSendOk = false;                         // 「まだ完了していない」
memcpy(sendBuf, ...); sendLen = n; sendFreq = f;
pendingSend = true;                         // ★ 公開は必ず最後（コピー完了後）
request->send(202, "application/json", "{\"status\":\"queued\"}");

// loop()
if (pendingSend) {
  irsend.sendRaw(sendBuf, sendLen, sendFreq);
  sendCount++; lastSendOk = true;
  pendingSend = false;
  currentMode = MODE_IDLE;                  // ★ 解放は必ず最後
}
```

この「公開は最後 / 解放は最後」がこの実装の要なので、**理由をコメントに書き残す**
（後から順序を入れ替えられると静かに壊れる）。

`MAX_RAW_LEN = 1024`（`RECV_BUFFER_SIZE` と揃える）、`static uint16_t sendBuf[MAX_RAW_LEN]`。
可変長配列は撤廃。要素数が `MAX_RAW_LEN` を超えるリクエストは **400 + 理由**（黙って切らない）。

### 5. ボディ分割受信に対応

`onBody` で `request->_tempObject` に蓄積する（`AsyncWebServerRequest` のデストラクタが
`free()` するので **`malloc` を使う。`new` は不可**）:

```cpp
if (index == 0) {
  if (total > MAX_BODY_LEN) { request->send(413, ...); return; }   // MAX_BODY_LEN = 16384
  request->_tempObject = malloc(total + 1);
  if (!request->_tempObject) { request->send(500, ...); return; }
}
if (!request->_tempObject) return;               // 既にエラー応答済み
memcpy((uint8_t*)request->_tempObject + index, data, len);
if (index + len != total) return;                // まだ揃っていない
((char*)request->_tempObject)[total] = '\0';
// ここで初めてパース。長さを明示して非 NUL 終端読みを解消:
DeserializationError err = deserializeJson(doc, (const char*)request->_tempObject, total);
```

送信側の doc は `DynamicJsonDocument doc(JSON_ARRAY_SIZE(MAX_RAW_LEN) + 512)`（≈8.7KB, **ヒープ**）。
AsyncTCP タスクのスタックは 8KB しかないので、大きい doc をスタックに置いてはいけない。
`/mode` は小さいので `StaticJsonDocument<512>` のままでよい。

### 6. 受信バッファの溢れを検知して報告（学習失敗の切り分け）

`loop()` の受信側:

```cpp
uint16_t n = (results.rawlen > 1) ? (results.rawlen - 1) : 0;
lastRecvLen = n;
lastRecvOverflow = results.overflow || (n > MAX_RAW_LEN);   // decode_results.overflow を見る
DynamicJsonDocument doc(JSON_ARRAY_SIZE(n) + 256);          // 実測 n からサイズを決める
...
if (lastRecvOverflow || doc.overflowed()) {
  // ★ コールバックしない。切り詰めた信号を学習させない
  Serial.printf("受信バッファ溢れ (rawlen=%u)。コールバックを中止します\n", results.rawlen);
} else {
  http.POST(output);
}
currentMode = MODE_IDLE;
```

- `resultToRawArray()` は**そのまま使う**（tick→μs 変換と `kMarkExcess` 補正を行う。
  DB にある既存 2 件はこの変換で取得されている）。`i = 1` から始めて先頭ギャップを飛ばす
  現行挙動も維持する。戻り値の NULL チェックと `delete[]` は入れる。
- コールバックのボディは `{"format":"raw","freq":38,"data":[...]}` のまま。
  **`raw` でも `raw_length` でもない**（`models.py:286-291` の `IRSignalCallback` が
  `format`/`freq`/`data` を要求し、`tests/test_learn.py:207-222` が固定している）。
- `callback_url` はサーバから渡されたものを**そのまま使う**。組み立て直さない
  （`tests/test_learn.py:72-83` が `/api/callback/ir_signal/{token}` を固定している）。
- **既知の限界**: 溢れた場合サーバには何も届かないので、UI 上は「タイムアウト」と表示される。
  真因は `/status` の `last_recv_overflow` を設定タブで見て判別する。
  エラーコールバック経路の新設はサーバ契約の変更を伴うため **Phase 7 へ申し送る**。

### 7. `/status` の拡張

```json
{"status":"ok","device_mode":"idle|receive|send",
 "wifi_ssid":"...","ip_address":"...",
 "send_count":12,"last_send_ok":true,"queue_len":0,
 "firmware_version":"2.0.0","free_heap":123456,"rssi":-52,
 "last_recv_len":0,"last_recv_overflow":false}
```

- `queue_len` = `pendingSend ? 1 : 0`、`last_send_ok` = 「直近の送信要求が `loop()` で完了したか」
- `/status` はモードを見ない（ビジー中も必ず答える）。`esp32.get_status()` は read timeout
  `min(3.0, 10.0)` = 3 秒しか待たない。送信を `loop()` に逃がしたので TCP が止まらず、
  ここが初めて実際に守られるようになる。
- 追加キーは **設定タブが `key=value` で全部そのまま描画する**（`tab-settings.js:91-95`）ので、
  フロント無変更で診断情報が画面に出る。

### 8. Wi-Fi（静的 IP + 自動復帰）

```cpp
WiFi.mode(WIFI_STA);
WiFi.setHostname(HOSTNAME);     // mode() の後・begin() の前でないと効かない
WiFi.setAutoReconnect(true);
WiFi.persistent(false);
#if USE_STATIC_IP
  // 設定の自己矛盾を先に弾く: STATIC_IP と GATEWAY が SUBNET 上で同一ネットワークか
  if (!sameSubnet(STATIC_IP, GATEWAY, SUBNET)) → 警告して DHCP へ
  WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS1);
#endif
WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
// 10 秒待つ → 失敗かつ静的設定なら WiFi.config(INADDR_NONE,...) で DHCP に戻して再試行（10 秒）
// それでも失敗なら ESP.restart()
// 成功後は必ず「静的 / DHCP のどちらで、どの IP を取ったか」をシリアルに出す
```

- `loop()` で 10 秒ごとに `WiFi.status()` を見て、切断していれば `WiFi.reconnect()`（ログ付き）。
  ログにあった `Connection refused` の一部はこれが原因の可能性がある。
- **フェイルセーフの限界（正直に書く）**: 静的設定が「文法的に正しいが、この LAN には
  間違っている」場合（例: 実際は `192.168.0.x` だった）、ESP は AP に**アソシエートは成功する**ので
  `WL_CONNECTED` になり、DHCP フォールバックは発動しない。到達不能なまま起動する。
  自己矛盾チェックが拾えるのはサブネット/GW の打ち間違いまで。
  最終確認は物理作業 4（ブラウザで `http://192.168.1.50/status`）で行う。

### 9. 細部

- `while (!Serial);` を削除（ヘッドレス起動でのブート停止を回避）
- **CORS ヘッダを削除する**（元計画 9 の「`Allow-Methods`/`Headers` を追加」からの意図的な逸脱）。
  新アーキテクチャで ESP に話しかけるのは FastAPI サーバだけ（サーバ間通信に CORS は無関係）。
  `Access-Control-Allow-Origin: *` は「ユーザーが開いた任意の Web ページが ESP に POST できる」
  状態を作るだけで、得るものが無い。物理作業 4 のブラウザ直アクセスはトップレベル遷移なので
  ヘッダ無しで動く。**逸脱なので AGENTS.md に理由を残す。**
- ヘッダのチェンジログを v2.0.0 として更新（`/status.firmware_version` と揃える）
- `freq` は受信時 `38` 固定のまま（IRremoteESP8266 に搬送波周波数の実測手段が無い。
  サーバも `freq` を保存していない — `learn.py:304-309` は既定値と違えば WARNING を出すだけ）
- 受信タイムアウト時にサーバへ通知しない現行挙動は維持（サーバ側が `expires_at` で
  自前にタイムアウト判定している — `learn.py:110-119`）

### 10. `tools/fake_esp32.py` に新ファーム挙動を追加（任意だが推奨）

`--async-send` フラグを追加: `/ir/send` が即 `202` を返し、`send_duration` 後に
バックグラウンドスレッドで `send_count` を増やす。`queue_len` は保留中 1 / 完了後 0。
**既定の挙動は変えない**（`tests/test_integration_stub.py:128-154` が送信直後に
`send_count == 10` と `queue_len == 0` を assert しているため）。
統合テストを 1 本追加して、202 即応答でもサーバが成功と判定し `queue_len` が戻ることを確認する。

### 11. フロントの文言

`ir_remocon/static/js/tab-remote.js:64`:

```diff
- toastSuccess(`「${name}」を ${result.device_name}（${result.host}）へ送信しました。`);
+ toastSuccess(`「${name}」を ${result.device_name}（${result.host}）へ送信を指示しました。`);
```

送信は 1 箇所だけ（予約・目覚ましは発火が非同期なので元から断定していない）。

### 12. AGENTS.md の更新（フェーズ完了の必須作業）

進行状況表を Phase 6 完了に、`esp/temp.ino` → `esp/ir_remocon/ir_remocon.ino` を反映し、
「Phase 6 で完成したもの」節を追加する。特に書き残すもの:

- 202 即応答を選んだこと、実発射をポーリング確認する案は**採らなかった**こと
- 「公開は最後 / 解放は最後」の順序が排他の要であること
- PlatformIO のビルド検証は**ライブラリのフォークが Arduino IDE 側と異なりうる**という限界
- CORS 削除という元計画からの逸脱と、その理由
- 静的 IP フェイルセーフが「LAN 自体が違う」ケースを拾えないこと
- 受信溢れ時にサーバへ通知できない（UI 上はタイムアウトに見える）→ Phase 7 の課題

---

## 検証

すべて実機なしで実施できる。

```powershell
# 1. ファームのビルド（初回は数百MB〜1GB のダウンロード。十数分かかる）
cd esp\ir_remocon
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run
#    → エラー 0 で完了すること。Flash/RAM 使用量を記録する

# 2. 既存テストの回帰（tab-remote.js の 1 行変更が回帰ガードを壊していないこと）
New-Item -ItemType Directory -Force "$env:TEMP\pytest-ir" | Out-Null   # 先に作ること
$env:PYTEST_DEBUG_TEMPROOT = "$env:TEMP\pytest-ir"
uv run pytest -q                     # 単体（既存の全件が通ること）
uv run pytest -m integration -v      # 統合（--async-send の新規 1 本を含む）

# 3. スタブで 202 経路を手動確認（本番 DB を触らない。AGENTS.md の定型どおり）
#    ターミナル A
uv run python tools/fake_esp32.py --port 8080 --async-send
#    ターミナル B
$env:IR_DB_PATH = "$env:TEMP\ir_scratch.db"; $env:IR_JOBS_DB_PATH = "$env:TEMP\ir_scratch_jobs.db"
$env:IR_LOG_PATH = "$env:TEMP\ir_scratch.log"; $env:IR_ADVERTISE_HOST = "127.0.0.1"; uv run ir-remocon
#    ターミナル C
curl.exe -s -X PUT "http://127.0.0.1:8102/api/devices/1" -H "Content-Type: application/json" -d '{\"host\":\"http://127.0.0.1:8080/\"}'
#    ブラウザ http://127.0.0.1:8102 → リモコンタブで送信
#    → トーストが「送信を指示しました」になっていること
#    → 設定タブの機器ステータスに send_count / queue_len / last_recv_overflow が出ること
```

**目視レビューで確認する項目**（コンパイラが拾わないもの）:

- 可変長配列が 1 つも残っていない
- `deserializeJson` の呼び出しが全て長さ付き
- `request->_tempObject` が `malloc`（`new` でない）
- `pendingSend = true` がバッファコピーの**後**、`currentMode = MODE_IDLE` が送信完了の**後**
- コールバック URL を組み立て直していない
- `doc.overflowed()` と `results.overflow` の両方を見ている

**実機での確認は後日ユーザーが実施**（AGENTS.md 絶対ルール 4）。手順は元計画
`esp32-smooth-waffle.md:249-261` の「ユーザーにお願いする物理作業」1〜7 をそのまま使う
（静的 IP は `192.168.1.50` で確定したので、手順 1 は「DHCP 割当範囲に .50 が入っていないことの確認」になる）。

---

## 触らないもの

- `ir_remocon/app/esp32.py` — 202 は既に成功扱い。**変更不要**
- `ir_remocon/app/models.py` / `routers/learn.py` — コールバック契約は据え置き
- `tests/test_esp32.py` / `test_send.py` / `test_learn.py` — ESP 契約の回帰ガード。無変更で通るはず
- 本番 DB（`ir_database.db`）— このフェーズは DB に一切触らない

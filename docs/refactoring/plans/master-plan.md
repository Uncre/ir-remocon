# ESP32 IR リモコン リファクタリング計画

> Codex 移管メモ: これは Claude Code で作成した全体計画の履歴スナップショット。
> 現在の作業規約は `../../../AGENTS.md`、実装後の確定事項は `../HANDOFF.md` を優先する。

## Context

ESP32 + FastAPI で家電を操作する自宅用リモコン。現状は動くには動くが、「ボタンを押しても反映されないことがある」「フロントが見にくい」「バックエンドが読みにくい」「ESP の IP が変わると壊れる」という問題を抱えている。

コード・ログ (`ir_db_server.log`)・DB (`ir_database.db` / `jobs.db`) を調査した結果、体感的な「不安定さ」は**推測ではなく特定可能な複数のバグ**であることが確認できた。本計画はそれらを潰したうえで、バックエンドをパッケージ分割、フロントをタブ UI 化、ESP の IP をサーバ側の機器登録で管理する形に作り直す。

---

## 確定した不具合（証拠付き）

### A. 予約フォームが無言で送信されない ★体感バグの主犯
`templates/index.html:60,65,66` — `#signal-select` / `#on-signal-select` / `#off-signal-select` がすべて `required`。モード切替は `display:none` で隠すだけなので、**隠れた空の select が HTML5 検証で invalid のまま残り、ブラウザが submit を握り潰す**。`submit` イベント自体が発火しないので JS のエラー表示も出ない。ページリロード直後は全 select が空 = 必ず再発、一度両方のモードを触ると通るようになる → 「時々効かない」の正体。

**修正**: 各モードの入力を `<fieldset>` で包み、非表示側を `fieldset.disabled = true` にする。disabled な要素は制約検証の対象外かつ送信対象外になるのが正攻法。

### B. 送信が失敗しても UI に「成功」と出る
`ir_db_server.py:226` の `send_signal_to_esp32` が `execute_ir_send` の結果を見ずに常に `{"status":"ok"}` を返す。`execute_ir_send` (同 58-74) は例外を握り潰して `print` するだけ。ログに実例:

```
Error sending 'room_light_turn_off' to 192.168.1.4. Reason: timed out
INFO: ... "POST /api/send/room_light_turn_off HTTP/1.1" 200 OK
```

**修正**: 送信結果を戻り値/例外で返し、失敗時は 502 + 理由。フロントは実結果でトーストを出す。

### C. 同時送信が ESP32 を殺している
ログ中、6 リクエストが同時に走って全部 `timed out`。原因は 2 つ:
- サーバ側: 連打に対する直列化もデバウンスもない
- ESP 側: `esp/temp.ino:134` の `irsend.sendRaw()` を **ESPAsyncWebServer の非同期ハンドラ内で同期実行**している。ここをブロックすると TCP スタックごと止まり、後続接続がタイムアウトする

**修正**: サーバ側は機器ごとの `threading.Lock` + 最小送信間隔。ESP 側は送信を `loop()` のキューへ逃がし、ハンドラは即 202 を返す。

### D. スケジューラのスレッドが死んだまま API は 200 を返し続けている
ログ 1032 行目:
```
Exception in thread APScheduler:
sqlite3.OperationalError: database or disk is full
```
以降 APScheduler スレッドは復帰せず、`jobs.db` には **7 件のジョブが数ヶ月前の `next_run` のまま停止**している。アプリ側にこれを検知する仕組みがゼロ。

**修正**: `EVENT_JOB_ERROR` / `EVENT_JOB_MISSED` リスナ + `GET /api/health`（scheduler 生死・ジョブ数・DB 疎通・直近エラー）。フロントに赤バナー表示。

### E. 予約ジョブに IP が焼き付いている
`ir_db_server.py:241,258` — `scheduler.add_job(..., args=[req.name, req.esp32_ip])`。**ジョブ作成時の IP が pickle されて固定**される。ログでは現行 IP が `192.168.1.4` なのに古いジョブは `192.168.1.16` を叩き続け、全部 `Connection refused`。IP を変えると既存予約が全滅する。

**修正**: ジョブ引数を `device_id` にし、発火時に DB から host を解決する。

### F. 日付デフォルトが UTC
`index.html:313` — `now.toISOString().split('T')[0]`。JST では **00:00〜09:00 の間、前日の日付**が入る。「一回のみ」予約が過去日になり、misfire で即発火 or 破棄される。`toTimeString()` (312 行) は現地時刻なので、日付と時刻でタイムゾーンが食い違っている。

### G. 目覚ましがスケジューラのワーカーを占有する
`execute_wakeup_alarm` (76-85) が `duration_seconds` の間 `time.sleep` でスレッドを保持。ログに `Run time of job ... was missed by 0:00:01` が出ている。開始後の中断手段もない。

### H. その他（修正対象に含める）
| 箇所 | 内容 |
|---|---|
| `ir_db_server.py:266` | `next_run` が `None` のジョブがあると naive/aware 比較で `TypeError` → 予約一覧が 500 |
| `ir_db_server.py:186-200` | `update_signal` で既存名にリネームすると `IntegrityError` 未捕捉 → 500。conn 保持中に別関数を呼ぶ構造 |
| `ir_db_server.py:30-31` | DB/テンプレートが相対パス。CWD 依存で、別ディレクトリから起動すると空 DB が生まれる |
| `ir_db_server.py:33-41` | `get_server_ip()` が 8.8.8.8 への UDP で IP を推定。多重 NIC 環境では LAN 側と違う IP を返しうる（返すと学習コールバックが ESP から到達不能になる）。→ `IR_ADVERTISE_HOST` で明示可能にする。**なお本番機での実際の誤検出は未確認**（下記「未解決」参照） |
| `ir_db_server.py:113-116` | `@app.on_event` は deprecated（ログに警告あり）→ lifespan へ |
| 全体 | `print()` のみ。タイムスタンプなし・ローテーションなし → `logging` + `RotatingFileHandler` |
| `ir_db_server.py:146-153` | `read_root` が渡す `signals` をテンプレートが使っていない（死んだクエリ） |
| `ir_db_server.py:155-162` | `/api/signals` に `ORDER BY` がなく一覧の並びが安定しない |
| `index.html:160-173` | 信号名を未エスケープで `innerHTML` に挿入（self-XSS） |
| `index.html:126` | FastAPI の 422 は `detail` が配列 → `[object Object]` と表示される |
| `index.html:247` | 学習完了を `setTimeout(refreshAll, 16000)` で当てずっぽう判定。成否が分からない |
| `esp/temp.ino:188-196` | ボディ分割受信を考慮していない（`index`/`total` 無視）。MSS 超えのペイロード（エアコン等）で必ず失敗。`deserializeJson(doc, (const char*)data)` は非 NUL 終端バッファを読む |
| `esp/temp.ino:227` | 受信側 `StaticJsonDocument<2048>` は最大 1024 要素を収容できず**黙って切り詰める** |
| `esp/temp.ino:128` | 非同期タスクのスタック上に可変長配列 |
| `esp/temp.ino:175` | `while (!Serial);` はヘッドレス運用でブート停止のリスク |
| `esp/temp.ino:72-90` | 起動時しか Wi-Fi 再接続しない。切断後は復帰しない |
| なし | 依存定義（`pyproject.toml` / ロックファイル）・`README.md`・テストが存在しない。サーバ側 venv は手作りで再現性なし |

---

## 目標構成

```
ir-remocon/
├── README.md                  # uv セットアップ・起動方法・ESP 側の物理作業手順
├── pyproject.toml             # uv で依存管理（requirements.txt は作らない）
├── uv.lock                    # uv sync 用ロックファイル（コミット対象）
├── .python-version            # 3.10（デプロイ先 Linux に合わせる）
├── ir_remocon/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py            # FastAPI 生成・lifespan・ルータ登録・uvicorn エントリ
│   │   ├── config.py          # 設定（絶対パス・ポート・広告ホスト・上限値）。環境変数で上書き可
│   │   ├── logging_conf.py    # logging + RotatingFileHandler
│   │   ├── db.py              # get_conn() コンテキストマネージャ・WAL・スキーマ初期化/マイグレーション
│   │   ├── models.py          # Pydantic スキーマ（1 行 1 定義に展開）
│   │   ├── repository.py      # signals / devices の CRUD
│   │   ├── esp32.py           # ESP32 クライアント。機器ごとのロック・最小送信間隔・リトライ・型付き例外
│   │   ├── jobs.py            # スケジューラから呼ばれる関数（device_id を受け取る）
│   │   ├── scheduler.py       # APScheduler 設定・トリガ生成・ジョブ説明整形・health
│   │   └── routers/
│   │       ├── signals.py  send.py  schedules.py  devices.py  callback.py  health.py
│   ├── static/
│   │   ├── style.css
│   │   └── app.js
│   ├── templates/index.html   # 骨格のみ（タブ構造）
│   ├── ir_database.db         # 既存データ維持
│   └── jobs.db
├── esp/
│   └── ir_remocon.ino         # temp.ino から改名・書き直し
├── tools/
│   ├── fake_esp32.py          # ESP32 スタブ（実機なしで全フロー検証用）
│   └── migrate_jobs.py        # 旧 jobs.db の棚卸し・退避
└── tests/                     # pytest
```

---

## 作業フェーズ

### Phase 1: 土台（config / db / logging） — ✅ 完了 (2026-08-18)

> 実施結果: 既存信号 2 件を保持したまま `devices` テーブル追加と日時列追加が完了。`init_db()` の冪等性・WAL 有効化・入力検証（once/weekly の必須項目）を実測で確認済み。`models.py` も土台として同時に作成。詳細は `AGENTS.md` を参照。

- `config.py`: `BASE_DIR = Path(__file__).resolve().parent.parent` を基点に DB・テンプレート・static の**絶対パス**を決定。`IR_DB_PATH` / `IR_JOBS_DB_PATH` / `IR_PORT` / `IR_ADVERTISE_HOST` / `IR_LOG_LEVEL` を環境変数で上書き可能に。`IR_ADVERTISE_HOST` は学習コールバック URL 用（未指定時のみ自動検出にフォールバック。**Tailscale 環境での学習失敗の根治**）。
- `db.py`: `@contextmanager get_conn()` で `row_factory` 設定・commit/rollback・close を一元化。全エンドポイントの `try/finally` ボイラープレートが消える。起動時に `PRAGMA journal_mode=WAL` / `busy_timeout=5000`。
- スキーマ移行（冪等・既存 2 件の信号は維持）:
  ```sql
  CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
  );
  ALTER TABLE ir_signals ADD COLUMN created_at TEXT;   -- 存在チェックのうえ実行
  ALTER TABLE ir_signals ADD COLUMN updated_at TEXT;
  ```
  `devices` が空なら `('esp32', '192.168.1.4', is_default=1)` を初期投入。
- `logging_conf.py`: 全 `print()` を置換。`RotatingFileHandler`（5MB × 3）でログ肥大を防ぐ。

### Phase 2: ESP32 通信レイヤ（体感バグ B/C の根治）
`esp32.py` に集約。**同期実装**（FastAPI の `def` エンドポイントはスレッドプールで動くため問題なく、スケジューラのスレッドからも同じコードを共有できる）。

- `send_raw(host, raw_data, freq=38)` — 機器ホストごとの `threading.Lock` + `MIN_SEND_INTERVAL`（既定 0.3 秒）で**連打を直列化**。
- `httpx.Client` をモジュールレベルで再利用（毎回生成をやめる）。`Timeout(connect=2.0, read=10.0)`。接続エラーのみ 1 回リトライ。
- 例外を `Esp32Unreachable` / `Esp32Busy`(409) / `Esp32Error` に分類。ルータ層で 502 / 409 / 502 にマッピング。
- `start_receive(host, callback_url, timeout_ms)` / `get_status(host)` も同居。

`POST /api/send/{name}` は例外を**握り潰さず**伝播させ、成功時のみ 200。

### Phase 3: 機器登録と IP 管理（要望「IP 固定」のサーバ側）
- `GET/POST/PUT/DELETE /api/devices` — 機器 CRUD。`PUT /api/devices/{id}` で host を変更すれば**既存の予約すべてに即反映**（E の解消）。
- `GET /api/devices/{id}/status` — ESP の `/status` を叩いて疎通確認（設定タブの「接続テスト」ボタン用）。
- 全リクエストボディから `esp32_ip` を削除し、`device_id`（省略時は既定機器）に置換。
- ジョブ引数は `[signal_name, device_id]` / `[on, off, interval, duration, device_id]`。`jobs.py` の実行関数が**発火時に**DB から host を解決する。

### Phase 4: スケジューラ堅牢化（D/G/H の解消）
- `scheduler.py` に `build_trigger(repeat_type, execute_time, execute_date, repeat_days)` を切り出す（単発/目覚ましで重複していたトリガ生成 21 行 × 2 を 1 本化）。
- `ThreadPoolExecutor(max_workers=4)`、`job_defaults = {coalesce: True, max_instances: 1, misfire_grace_time: 300}`。
- `add_listener(EVENT_JOB_ERROR | EVENT_JOB_MISSED)` → ログ + 直近エラーを保持。
- `GET /api/health` → `{scheduler_running, jobs, db_ok, last_job_error}`。
- 目覚まし: `threading.Event` で中断可能にし、実行中アラームを `running_alarms` に登録。`DELETE /api/alarms/{id}` で停止。`duration_seconds` に上限を設ける。ロックは 1 送信ごとに取得/解放し、全体を占有しない。
- ジョブ説明整形: `trigger.fields[5]` のようなインデックス直参照をやめ、**ジョブ作成時にメタ情報（種別・信号名・繰り返し表現）を `job.kwargs` に保存**して読み出す。APScheduler のバージョン差に壊されない。
- 一覧のソート: `next_run` が `None` のジョブを比較対象から外す（`(next_run is None, next_run)` キー）。naive/aware 比較の `TypeError` を消す。

### Phase 5: フロントエンド（タブ UI + 体感バグ A/F の根治）
`templates/index.html` を骨格のみにし、`static/style.css` / `static/app.js` へ分離（`main.py` で `StaticFiles` をマウント）。ビルド不要の素の JS を維持。

タブ構成: **リモコン / 学習 / 予約 / 設定**（アクティブタブを `localStorage` に保持）。

必須の修正:
1. **`required` を外し、非表示モードの `<fieldset>` を `disabled` にする**（A の根治）。バリデーションは既にある JS 側チェックに寄せる。
2. **日付デフォルトを現地時刻で生成**（F）。`toISOString()` 廃止。
3. **多重送信ガード** — 送信中はボタンを `disabled` + スピナー表示。同一操作の重複クリックを無視。
4. **正直なトースト** — 実際のレスポンスに基づく成功/失敗表示。`detail` が配列の場合を整形（`[object Object]` 対策）。
5. **XSS 対策** — 信号名は `textContent` でノード生成、または `escapeHtml()` を通す。
6. **設定タブ** — IP 直書き入力を廃止し、機器のプルダウン + 機器編集 + 「接続テスト」ボタン。
7. **学習フロー** — 盲目的な `setTimeout` を廃止。サーバが学習セッション状態（`pending` / `success` / `timeout`）を保持し、`GET /api/learn/status` をポーリングして実結果を表示。
8. **健全性バナー** — `/api/health` で `scheduler_running=false` を検知したら赤帯を出す（D の再発を隠さない）。
9. 予約一覧を一定間隔で自動更新。
10. `read_root` の未使用 `signals` を削除（純粋な SPA 的配信にする）。

### Phase 6: ESP32 ファーム（`esp/ir_remocon.ino`）
実機書き込みは後日。コードとして用意する。

1. **送信を `loop()` へ退避（C の根治）** — ハンドラは検証 → 静的バッファへコピー → `pendingSend = true` → 即 `202` 応答。`loop()` が `irsend.sendRaw()` を実行。非同期タスクをブロックしない。
2. **`/status` を拡張** — `last_send_ok` / `send_count` / `queue_len` を返し、サーバから結果確認できるようにする。
3. **ボディ分割受信に対応** — `index`/`total` を使ってバッファに蓄積し、`index + len == total` で初めてパース。`deserializeJson(doc, data, len)` と長さを明示（非 NUL 終端読みの解消）。
4. **JSON バッファ適正化** — 受信側を `results.rawlen` 実測に応じた `DynamicJsonDocument` にし、`doc.overflowed()` を必ずチェック。溢れたらコールバックせずエラーログ（エアコン信号の黙示的破損を防ぐ）。
5. **可変長配列を撤廃** — `static uint16_t rawBuf[MAX_RAW_LEN]` + 境界チェック。
6. **静的 IP 対応（要望本体）** — ファイル冒頭に設定ブロックを置く:
   ```cpp
   #define USE_STATIC_IP 1
   IPAddress STATIC_IP (192,168,1,50);
   IPAddress GATEWAY   (192,168,1,1);
   IPAddress SUBNET    (255,255,255,0);
   IPAddress DNS1      (192,168,1,1);
   ```
   `WiFi.config()` を `WiFi.begin()` の前に呼ぶ。`WiFi.setHostname("ir-remocon")` も設定。
   **フェイルセーフ**: 静的設定で 10 秒以内に接続できなければ DHCP で再試行し、取得 IP をシリアルに出す。設定ミスで機器に一切アクセスできなくなる事態を防ぐ。
7. **Wi-Fi 自動復帰** — `WiFi.setAutoReconnect(true)` + `loop()` 内で切断を検知して再接続（現状は起動時のみ）。ログの `Connection refused` の一部はこれが原因の可能性。
8. **`while (!Serial);` を削除**（ヘッドレス起動のブート停止回避）。
9. CORS の `Access-Control-Allow-Methods` / `-Headers` を追加。

### Phase 7: 周辺整備（Python 環境は uv で管理）
- **`pyproject.toml`** — `requirements.txt` は作らない。アプリではなくローカル実行のみなので `[tool.uv] package = false` にしてビルドバックエンド設定を省く。
  ```toml
  [project]
  name = "ir-remocon"
  version = "0.1.0"
  requires-python = ">=3.10"        # デプロイ先 Linux が 3.10（ログのパスより）
  dependencies = [
      "fastapi", "uvicorn[standard]", "httpx",
      "jinja2", "pydantic", "apscheduler>=3.10,<4", "sqlalchemy",
  ]

  [dependency-groups]
  dev = ["pytest", "pytest-asyncio"]

  [build-system]                      # 実装時の変更点（下記参照）
  requires = ["hatchling"]
  build-backend = "hatchling.build"

  [tool.hatch.build.targets.wheel]
  packages = ["ir_remocon"]
  ```
  > **実装時の変更**: 当初 `[tool.uv] package = false`（virtual project）にしたが、それだとプロジェクトルートが `sys.path` に載らず `python -m ir_remocon.app.main` がプロジェクト直下からしか動かない ＝ 本計画の「CWD 非依存」要件を満たせなかった。hatchling の通常パッケージにして **editable インストール**する形に変更。データファイル（DB / templates / static）は editable なのでソースツリー上の実体を指し続ける。副産物として `uv run ir-remocon` が使える。
  `apscheduler` は 4.x で API が別物なので **`<4` で上限を切る**（`job.next_run_time` や `trigger.fields` 周りの既存混乱の再発防止）。
- **`uv.lock` を生成してコミット**（`uv lock`）。Windows の開発機と Linux のデプロイ先で同一バージョンを再現できるようにする。`.python-version` に `3.10` を置く。
- **`.gitignore`**: `.venv/`、`*.db-wal`、`*.db-shm`、`__pycache__/`、`*.log`。DB 本体（`ir_database.db`）は残す判断でよいか実装時に確認。
- **`README.md`**: uv セットアップ、起動、環境変数一覧、**ESP 側の物理作業手順**（後述）
- `tools/fake_esp32.py`: `/ir/send` `/mode` `/status` を実装した ESP32 スタブ。`/mode` を受けたら数秒後に本物同様コールバックを POST する。**実機なしで学習まで含めた全フローを検証できる**
- `tools/migrate_jobs.py`: 既存 `jobs.db` の 7 件を JSON にダンプして退避（後述）
- `tests/`: 信号 CRUD、トリガ生成、ジョブ説明整形、`next_run=None` 混在時の一覧ソート、送信失敗 → 502、機器 host 変更が既存ジョブに効くこと

---

## データ移行（破壊的変更あり）

- **`ir_database.db` は保持**。既存の `room_light_turn_on` / `room_light_turn_off` はそのまま。テーブル追加と列追加のみ。
- **`jobs.db` は退避のうえクリア**。理由:
  - 現存する 7 件はすべて数ヶ月前に停止済み（`next_run` が 2026-01 で止まっている）
  - 引数に古い IP `192.168.1.16` が pickle されており、そのままでは動かない
  - ジョブ関数のシグネチャが `device_id` 化で変わるため unpickle 後に実行不能
  - → `tools/migrate_jobs.py` で内容を JSON にダンプ + `jobs.db.bak` として保存し、新スキーマで空から開始。ダンプを見て必要な予約だけ UI から再登録。
- API 破壊的変更: リクエストボディの `esp32_ip` → `device_id`。自宅内利用のみなので互換レイヤは設けない。

---

## ユーザーにお願いする物理作業

コード側の準備が終わってから実施してください。順番が重要です。

1. **ルーターの DHCP 割当範囲を確認**（管理画面 → LAN/DHCP 設定）。静的 IP はこの**範囲外**から選ぶこと。範囲内を選ぶと後で他機器と衝突します。例: DHCP が `.100〜.200` なら `192.168.1.50` は安全。ゲートウェイ・サブネットマスクもここで控えてください。
2. **ESP32 を PC に USB 接続**し、Arduino IDE で `esp/ir_remocon.ino` を開く。冒頭の `WIFI_SSID` / `WIFI_PASSWORD` / `STATIC_IP` / `GATEWAY` / `SUBNET` を 1 の値で埋める。
3. **書き込み後、シリアルモニタ (115200bps) で IP を確認**。静的設定が失敗すると DHCP へフォールバックしてその旨と取得 IP が出ます。その場合は 1 の値を見直してください。
4. **疎通確認**: PC のブラウザで `http://<設定したIP>/status` を開く。JSON が返れば OK。
5. **サーバの設定タブ**で機器の host を 3 の IP に更新し、「接続テスト」を押す。
6. **学習テスト**: 学習タブで名前を入れて開始 → ESP の受信モジュールに向けて実機リモコンのボタンを押す → 一覧に出れば OK。
7. **送信テスト**: リモコンタブから送信 → 家電が反応するか確認。**連打して挙動が安定しているかも確認してください**（Phase 2 + Phase 6-1 の効果検証）。

> 補足: 本番機 `uncre-switch` の LAN IP は **`192.168.1.110`（固定）**、Tailscale は `100.95.100.1`。`IR_ADVERTISE_HOST=192.168.1.110` を明示して自動検出に頼らない運用にします（ESP32 は LAN 側からしかサーバに到達できないため、ここに Tailscale IP が入ると学習が必ず失敗する）。

### 未解決: 学習が時々失敗する原因

当初「`get_server_ip()` が Tailscale の IP を拾ってコールバックが届かない」と推測したが、**ログの `100.104.223.25` はスマホ (`xiaomi-13t-pro`) からフロントを開いたアクセス元であり、サーバの自 IP 検出結果ではないと判明したため、この説は根拠を失った**。`uncre-switch` は exit node を offer しているだけで使用はしておらず、自動検出は正しく `192.168.1.110` を返すと考えられる。

真因は未特定。以下で切り分ける:
- Phase 5: 設定タブに**実際に ESP へ渡すコールバック URL を表示**する
- Phase 7: `fake_esp32.py` でコールバック経路だけを独立に検証する
- Phase 6: ESP 側の受信バッファ溢れ (`StaticJsonDocument<2048>` に 1024 要素) が黙って信号を壊している可能性も併せて潰す

---

## 検証手順

ESP 実機なしで、Phase 6 以外は全部検証できます。環境は uv 管理なので、`activate` は不要です（`uv run` が `.venv` を自動作成・同期して実行します）。

```powershell
# 依存を解決してロック + .venv 構築（プロジェクト直下で一度だけ）
uv lock
uv sync
```

1. **ユニットテスト**
   ```powershell
   uv run pytest tests/ -v
   ```
2. **スタブ ESP32 を起動**（別ターミナル）
   ```powershell
   uv run python tools/fake_esp32.py --port 8080
   ```
3. **サーバ起動**
   ```powershell
   $env:IR_ADVERTISE_HOST = "127.0.0.1"
   uv run python -m ir_remocon.app.main
   ```
4. **ブラウザで `http://127.0.0.1:8102`** を開き、設定タブで機器 host を `127.0.0.1:8080` に設定。以下を手動確認:
   - **[A の回帰確認]** リロード直後に予約タブで「目覚まし」を選び、ON/OFF と時刻だけ入れて送信 → **登録されること**（現状はここが無反応）
   - **[B]** スタブを落とした状態で送信 → **エラートーストが出ること**（現状は成功と表示される）
   - **[C]** 送信ボタンを高速連打 → リクエストが直列化され、ボタンが押下中 disabled になること
   - **[F]** ブラウザのタイムゾーンを JST にして朝 9 時前に予約タブを開き、日付デフォルトが**当日**であること
   - **[D]** サーバログに scheduler 起動が出ること、`/api/health` が `scheduler_running: true` を返すこと
   - **[E]** 予約を 1 件登録 → 設定タブで機器 host を変更 → 予約一覧が新 host を向くこと（`GET /api/schedules` で確認）
   - **学習**: 学習タブから開始 → スタブが数秒後にコールバック → **ポーリングで成功が表示され**一覧に信号が増えること
   - **目覚まし**: 短い duration で登録 → 実行中に停止ボタンで中断できること
5. **CWD 非依存の確認**: 別ディレクトリから `uv run --project <path> python -m ir_remocon.app.main` を実行しても同じ DB を掴むこと（新規空 DB が作られないこと）
6. **Phase 6 は実機で**上記「物理作業」7 番まで実施して確認。

### デプロイ先（Linux）での再現

ログから、本番は `/home/uncre/python_works/ir_remocon/` の Linux 機・Python 3.10・手作り venv で動いています。移行後は同じ手順に揃えられます。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv 未導入なら
cd ~/python_works/ir_remocon
uv sync --frozen        # uv.lock どおりに再現。Python 3.10 が無ければ uv が自動取得
export IR_ADVERTISE_HOST=192.168.1.110            # ESP32 から見たこのサーバの LAN IP
uv run python -m ir_remocon.app.main
```

`uv sync --frozen` によりロック内容から逸脱しないので、開発機と本番のバージョン差に起因する事故（`trigger.fields` のインデックス問題など）を防げます。

> 補足: 現在の uv は 0.5.5（2024-11 リリース）です。`pyproject.toml` + `uv lock/sync/run` はこのバージョンで動作しますが、可能なら `uv self update` を推奨します。

---

## 実装順序

Phase 1 → 2 → 3 → 4 を先に通し（この時点で B/C/D/E/G と H の大半が解消）、次に Phase 5 でフロント（A/F が解消、ここで体感が大きく変わる）、Phase 7 の `fake_esp32.py` は Phase 2 完了直後に用意して以降の検証に使う。Phase 6 のファームは並行して書き、書き込みは後日まとめて。

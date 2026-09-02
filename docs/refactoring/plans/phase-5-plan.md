# Phase 5: フロントエンド刷新 + 学習 API

> Codex 移管メモ: これは Claude Code で作成した計画の履歴スナップショット。
> 現在の作業規約は `../../../AGENTS.md`、実装後の確定事項は `../HANDOFF.md` を優先する。

## Context

Phase 1〜4 でバックエンドは作り直しが終わり、不具合 B/C/D/E/G は解消済み。だが
**新サーバの `/` は JSON スタブを返すだけで、UI が存在しない**。旧 `templates/index.html`
は送信時に `esp32_ip` を送る設計で、新 API（`device_id` 参照 + `extra="forbid"`）では
9 本中 4 本のリクエストが 422/404 になるため配信していない。つまり今このアプリは
**ブラウザから一切操作できない状態**にある。

このフェーズで UI を作り直し、残る体感バグ 2 つを根治する。

| ID | 症状 | 原因 |
|---|---|---|
| **A** | 予約ボタンが無反応（体感バグの主犯） | `index.html:60,65,66,77` の非表示 select/input に `required` が残り、HTML5 検証が submit を握り潰す。`submit` イベント自体が発火しないのでエラーも出ない |
| **F** | 朝 9 時前だと予約日付が前日になる | `index.html:313` の `toISOString()`（UTC）。隣の `toTimeString()`（314 行）は現地時刻なので、日付と時刻でタイムゾーンが食い違っている |

加えて、Phase 4 で作った**安全機構がどれも画面に出ていない**（健全性バナー、実行中
アラームの停止ボタン、504「送信できたか不明」、409「機器がビジー」）。これらは
「黙って壊れない」ためにサーバ側へ入れたものなので、UI に出して初めて意味を持つ。

### 調査で判明した追加スコープ: 学習 API が未実装

**新サーバには学習（信号受信）のエンドポイントが 1 本も無い。** モデル
（`models.py:264-284` の `LearnStartRequest` / `LearnSessionOut` / `IRSignalCallback`）、
ESP 通信（`esp32.py:344 start_receive()`）、DB 保存（`repository.py:181 upsert_signal()`）は
すべて用意済みだが、繋ぐルータが無い（`repository.py:184` に「Phase 5 のコールバック
ルータから使う」と明記）。元プランの Phase 5 項目 7 に含まれている作業なので、
**ユーザ確認のうえ Phase 5 に含める**。

### 確定した方針（ユーザ回答）

1. 学習 API（`routers/learn.py`）は Phase 5 に含める
2. 旧 `ir_remocon/ir_db_server.py` と旧 `templates/index.html` は **Phase 5 の最後に削除**
3. UI は **デスクトップ / モバイル両対応**（スマホから Tailscale 経由で操作するため）

---

## Step 1: 学習 API（バックエンド）

### 新規 `ir_remocon/app/learn.py` — セッション登録簿

`jobs.py` が実行中アラームの登録簿を持つのと同じ層。ルータは薄いまま保つ。

```python
@dataclass
class LearnSession:
    token: str; name: str; device_id: int; device_name: str; host: str
    overwrite: bool; status: str; message: str | None
    started_at: datetime; expires_at: datetime
    raw_length: int | None = None
```

- `start(device, name, overwrite) -> LearnSession` — token 発行 + 登録
- `get(token)` / `list_pending()` / `complete(token, raw_data)` / `fail(token, msg)`
- **タイムアウトは遅延評価。** `status == "pending"` かつ `now > expires_at` なら
  `"timeout"` を返す。**バックグラウンドスレッドは作らない** — 現ファームは
  タイムアウト時に何も通知せず黙って idle に戻るので、サーバは自分の時計だけが頼り。
- 10 分より古いセッションはアクセスのたびに刈る（メモリを有界に保つ）
- プロセスメモリ。サーバ再起動で消える（実行中アラームと同じ扱い。許容）

例外（`main.py` に `LearnError` の handler を 1 つ追加。既存 3 つと同じ形）:

| 例外 | status | 使う場面 |
|---|---|---|
| `LearnSessionNotFound` | 404 | 未知 / 期限切れ token へのコールバック、`GET /api/learn/{token}` |
| `LearnAlreadyRunning` | 409 | 同一機器で学習が進行中（ESP は `currentMode` 1 本の状態機械） |

**同名の既存信号は `repository.DuplicateName`（409）を再利用する。** 新しい例外は作らない。

### 新規 `ir_remocon/app/routers/learn.py`

`APIRouter(prefix="/api", tags=["learn"])`。**`try/except` は書かない。**

| メソッド | パス | 成功 | 内容 |
|---|---|---|---|
| POST | `/api/learn` | **201** `LearnSessionOut` | `LearnStartRequest{device_id?, name, overwrite}` |
| GET | `/api/learn` | 200 `list[LearnSessionOut]` | pending のみ。リロード時の状態復元用 |
| GET | `/api/learn/{token}` | 200 `LearnSessionOut` | ポーリング先 |
| POST | `/api/callback/ir_signal/{token}` | 200 | ESP → サーバ。`IRSignalCallback{format, freq, data}` |

`POST /api/learn` の順序（**この順序が要点**）:

1. `repository.resolve_device(device_id)`
2. `overwrite=False` かつ同名が既にある → **ここで 409**
   （15 秒待たせた末に「既にあります」は最悪の UX）
3. `learn.start()` でセッション登録（token 発行）
4. `esp32.start_receive(host, f"{config.callback_base_url()}/api/callback/ir_signal/{token}", ...)`
5. ESP 通信が失敗したら**セッションを削除して例外を伝播**（502/409）。
   開始できていないものを pending として残さない

#### コールバック URL に **信号名ではなく token** を入れる

旧実装は `/api/callback/ir_signal/{name}`。token にする理由:

- ファームは `callback_url` をサーバから受け取ってそのまま `http.begin()` する
  （`esp/temp.ino:159,242` で確認済み）ので、**パス設計はサーバの完全な自由**
- 日本語・スペース・`/` を含む信号名の URL エンコード問題が消える
- 旧実装は**誰でも任意の信号を上書きできた**。token 制なら pending セッション以外は 404
- ポーリングで「どの学習の結果か」を一意に紐付けられる

#### 期限切れ後に届いたコールバックは 404 で捨て、WARNING に残す

保存すると UI が既に "timeout" と表示した後に信号が増える（次の学習と競合する）。
**ただしログには「コールバックは届いたが期限を N 秒超過していた」を必ず出す。**
これは未解決の疑問「学習が時々失敗する原因」を切り分ける唯一の証拠になる。

### モデルの追加（`models.py`）

- `LearnSessionOut` に `device_id: int | None` / `device_name: str | None` /
  `raw_length: int | None` を追加。`raw_length` は「1024 要素を受信」を画面に出すため
  — Phase 6 で疑っている ESP 側の受信バッファ切り詰め（`StaticJsonDocument<2048>` に
  1024 要素）の切り分けに直結する
- `HealthOut` に **`callback_base_url: str`** を追加。`config.callback_base_url()`
  （`config.py:96`）を返すだけ。設定タブに「ESP に教えているサーバのアドレス」を
  出せるようにする（未解決の疑問の切り分け用。元プラン 268 行の要求）。
  `scheduler.health()` の返り値にも同じキーを足す

---

## Step 2: フロント基盤

### ファイル構成

```
ir_remocon/
├── templates/index.html      # 骨格のみ（タブ構造 + 空コンテナ）。Jinja2 変数は 0
└── static/
    ├── style.css
    ├── favicon.ico           # templates/ から移動（現状どこからも配信されず 404）
    └── js/
        ├── api.js        fetch ラッパ・エラー整形・多重送信ガード
        ├── dom.js        el() / $ / $$。textContent ベースの描画ヘルパー
        ├── toast.js      success / error / warn / info の 4 種
        ├── state.js      送信先 device・学習中フラグ・localStorage
        ├── poll.js       visibilityState 連動のポーリング管理
        ├── health.js     健全性バナー
        ├── tab-remote.js / tab-learn.js / tab-schedules.js / tab-settings.js
        └── app.js        エントリ（<script type="module">）
```

> **元プランからの変更**: 当初は `static/app.js` 1 本の予定だったが、4 タブ + バナー +
> アラーム + 学習ポーリングで 1200 行規模になる。`type="module"` の ES modules で
> 分割する（ビルド不要は維持。バンドラもトランスパイラも入れない）。

`main.py` の変更は 2 箇所だけ:

- `/`（150-164 行）を `FileResponse(config.TEMPLATES_DIR / "index.html")` に差し替え。
  Jinja2 は使わない（旧実装が渡していた `signals` 変数はテンプレート側で一度も
  参照されていない死んだクエリだった）。`Cache-Control: no-cache` を付ける
- `include_router(learn.router)` を追加

**`static/` を作れば StaticFiles のマウントは自動で通る**（142-148 行のガードが
ディレクトリの存在を見ているだけ。コード変更不要）。`/favicon.ico` のルートを 1 本足す。

### `api.js` — エラー整形が本体

```
ApiError { status, detail, error, host, outcome_unknown }
```

| 応答 | 画面表示 |
|---|---|
| **422**（`detail` が配列） | `loc.slice(1).join('.') + ': ' + msg` に整形。**`[object Object]` の根治** |
| **409** `Esp32LocalBusy` / `Esp32DeviceBusy` | error ではなく **warn**「機器がビジーです。少し待ってください」 |
| **504** `outcome_unknown: true` | **warn**「送信できたか不明です」（「失敗しました」と言わない） |
| **409** `ConstraintViolation` / `DuplicateName` | `detail` をそのまま出す（サーバが日本語で理由と次の操作を書いている） |
| **400** `InvalidSchedule` | 同上（過去日時の予約） |
| その他 | `detail` をそのまま |

**多重送信ガード**: 同じキーの in-flight リクエストは黙って無視し、呼び出し元の
ボタンを `disabled` + スピナーにする。

### `dom.js` — XSS の構造的な排除

`el(tag, props, children)` で `textContent` / `setAttribute` 経由の DOM を組む。
クリアは `replaceChildren()`。**`innerHTML` を 1 箇所も使わない**（旧実装は
`index.html:161,165,181` の 3 箇所で信号名を未エスケープ挿入していた）。
これは Step 4 のテストで文字列検索ガードをかける。

`encodeURIComponent()` を必ず通す（旧実装は `api/send/${name}` を素で埋めていたため
名前に `/` や `#` が入ると壊れる）。

### ポーリング（モバイル配慮）

`document.visibilityState === 'visible'` のときだけ動かす。Tailscale 経由の
スマホ操作が常用なので、バックグラウンドでモバイル帯域とバッテリーを食わない。

| 対象 | 間隔 |
|---|---|
| `/api/health` | 30 秒 |
| `/api/schedules` | 60 秒（操作直後は即時） |
| `/api/alarms` | 30 秒。**鳴っている間は 5 秒**に加速 |
| `/api/learn/{token}` | 学習中のみ 1 秒（`expires_at` + 2 秒で打ち切り） |

---

## Step 3: 各タブ

タブ: **リモコン / 学習 / 予約 / 設定**。アクティブタブは `localStorage` に保持。

### 共通ヘッダ

- タイトル + **送信先セレクタ**（全タブ共有。先頭は「既定機器を使う」= `device_id` 省略）。
  選択は `localStorage` に保持。**起動時に `/api/devices` と突き合わせ、消えた機器 id が
  残っていたら既定へフォールバック**する（そのまま送ると 404 になる）
- **健全性バナー**（タブの外に常時）:
  - `ok:false` または `scheduler_running:false` → **赤バナー**「予約が動いていません」（不具合 D の再発）
  - `last_job_error` → **黄色の別枠**。`ok:true` のままここだけ埋まるのが正常な状態
    （「機構は動いているが直近のジョブが失敗した」）。赤と同一視しない
  - `device_count === 0` → 赤（送信も予約も全滅する状態）

### リモコンタブ
信号一覧（送信 / リネーム / 削除）。`POST /api/send/{name}` の応答に `device_name` と
`host` が入るので、トーストに「〜へ送信しました (esp32 / 192.168.1.4)」まで出す
（送信先の設定ミスにユーザが自力で気づける）。
**学習中は送信ボタンを全て `disabled`**（ESP は受信モード中の送信を必ず 409 で返す）。

### 学習タブ
名前入力 + 上書きチェック → 開始 → **カウントダウン付きの進行表示** →
`GET /api/learn/{token}` のポーリングで実結果。
`setTimeout(refreshAll, 16000)` の当てずっぽう判定（旧 247 行）を廃止。

| status | 表示 |
|---|---|
| pending | 「ESP32 に向けてリモコンのボタンを押してください（残り N 秒）」 |
| success | 「学習しました（N 要素）」+ 信号一覧を更新 |
| timeout | 「時間内に信号を受信できませんでした」+ 再試行ボタン |
| error | `message` をそのまま |

### 予約タブ

**不具合 A の根治**: 各モードの入力を `<fieldset>` で包み、非表示側は
`fieldset.disabled = true` と `fieldset.hidden = true` を**両方**立てる。
`disabled` な子孫は制約検証の対象外かつ送信対象外になるのが正攻法。
**`required` 属性は 1 つも使わない** — 検証は JS 側に一本化する。

**不具合 F の根治**: 日付/時刻の既定値は現地時刻で組み立てる。

```js
const pad = (n) => String(n).padStart(2, '0');
const localDate = (d) => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
```

**`toISOString()` は 1 箇所も使わない**（Step 4 で文字列検索ガード）。
「一回のみ」で過去日時が選ばれたら送信前にフロントでも警告する
（サーバも 400 `InvalidSchedule` で断るが、往復を待たせない）。

その他:
- 目覚ましの持続時間に「最大 30 分」と明記。超過は 422 で返るので `detail` をそのまま出す
- 予約一覧は `next_run` 昇順・未定は末尾（サーバ整形済み）。`schedule_description` と
  `device_name` はそのまま表示してよい。`device_name` が `(削除済み id=N)` のときは**赤字**
- **実行中アラームのセクション**（`GET /api/alarms`）。`ends_at` から残り時間を出し、
  **「今すぐ止める」ボタン**（`DELETE /api/alarms/{run_id}`）。旧実装には止める手段が
  無かった＝不具合 G。空のときは非表示

### 設定タブ
IP 直書き入力を廃止し、`/api/devices` の CRUD + 接続テスト。

- **接続テストは失敗しても 200 が返る。`reachable` の真偽で描き分ける**
  （HTTP ステータスで判定すると常に「成功」になる）。`host` も返るので
  「どのアドレスを叩いたか」を画面に出す
- 既定を外す PUT / 最後の 1 台の削除 → **409 `ConstraintViolation`**。`detail` をそのまま出す
- 既定機器の削除 → `new_default_device_id` が返る。**送信先が変わったことを明示**する
- `/api/health` の情報を出す: `advertise_host` / **`callback_base_url`** /
  `device_count` / `default_device_name` / `job_count` / `last_heartbeat`。
  コールバック URL の表示は未解決の疑問「学習が時々失敗する」の切り分け材料

---

## Step 4: テスト・旧実装の削除

### 新規 `tests/test_learn.py`（~20 件）

`conftest.py` の `client` / `mock_esp` fixture を使う（`mock_esp.requests` で
ESP に渡した `PUT /mode` のボディを検証できる）。

- 開始 → 201 / pending / **callback_url に `ADVERTISE_HOST` と token が入っている**
- 同名あり + `overwrite=False` → **409（ESP を叩く前に）**、`overwrite=True` → OK
- 同一機器で 2 重開始 → 409 `LearnAlreadyRunning`
- ESP 到達不能 → 502 で、**pending セッションが残っていない**
- ESP が 409 → `Esp32DeviceBusy` → 409
- コールバック → 信号が保存され `status:"success"`、`raw_length` が入る
- 未知 token → 404 / 期限切れ後のコールバック → 404 かつ信号は保存されない
- `expires_at` を過ぎた `GET /api/learn/{token}` → `timeout`
- `{"esp32_ip": ...}` → 422（`extra="forbid"`）、空 `data` → 422
- 日本語やスラッシュを含む信号名でも学習が通る（token 制の効果）

### 新規 `tests/test_frontend_assets.py` — 不具合 A/F の回帰ガード

**ファイルを文字列検索するだけの安いテスト。だが今回潰すバグに直接効く。**

| ガード | 内容 |
|---|---|
| `GET /` | 200 + `text/html` を返す（既存 `test_root_does_not_serve_legacy_ui` を反転） |
| `/static/js/app.js` / `/static/style.css` | 200 |
| **不具合 A** | `templates/index.html` に `required` 属性が **1 つも無い** |
| **不具合 F** | `static/js/**.js` のどこにも **`toISOString(` が現れない** |
| **XSS** | `static/js/**.js` のどこにも **`innerHTML` が現れない** |
| **旧契約の残骸** | `static/js/**.js` に **`esp32_ip` が現れない** |

### 既存テストの修正
- `tests/test_routers_signals.py:66` の `test_root_does_not_serve_legacy_ui` を
  `test_root_serves_ui` に反転（HTML を返すことを assert）
- `tests/test_schedules_api.py` の health テストに `callback_base_url` の assert を追加

### 旧実装の削除（Step 3 の検証が終わってから）
- `ir_remocon/ir_db_server.py` を削除
- `ir_remocon/ir_request_test.py` も旧 API 前提なら削除（内容を確認して判断）
- 旧 `templates/index.html` は新しい骨格に置き換わる。参照が要るときは
  `backup_before_refactor/index.html` と git 履歴に残る

---

## 検証手順

すべて `tools/fake_esp32.py` で実施。実機不要。**本番 DB / jobs.db / ログを触らない。**

```powershell
# ターミナル A: ESP32 スタブ
uv run python tools/fake_esp32.py --port 8080

# ターミナル B: サーバ（本番から完全に隔離する）
$env:IR_DB_PATH = "$env:TEMP\ir_scratch.db"; $env:IR_JOBS_DB_PATH = "$env:TEMP\ir_scratch_jobs.db"
$env:IR_LOG_PATH = "$env:TEMP\ir_scratch.log"; $env:IR_ADVERTISE_HOST = "127.0.0.1"; uv run ir-remocon

# ターミナル C: 機器 host をスタブへ
curl.exe -s -X PUT "http://127.0.0.1:8102/api/devices/1" -H "Content-Type: application/json" -d '{\"host\":\"http://127.0.0.1:8080/\"}'
```

```powershell
# テスト（開発機の temproot 問題の回避が必須）
New-Item -ItemType Directory -Force "$env:TEMP\pytest-ir" | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = "$env:TEMP\pytest-ir"; uv run pytest -q
```

ブラウザ（`http://127.0.0.1:8102`）で確認する項目:

| # | 確認 | 期待 |
|---|---|---|
| 1 | **[A 回帰]** リロード直後に予約タブ → 目覚ましを選び ON/OFF と時刻だけ入れて送信 | **201 で登録される**（旧 UI はここが完全に無反応） |
| 2 | **[A 回帰]** 同じくリロード直後に単発 + 毎日（日付欄が隠れる）で送信 | 登録される |
| 3 | **[F 回帰]** OS/ブラウザの時計を **JST の 00:30 頃**にして予約タブを開く | 日付の既定値が**当日**（前日にならない） |
| 4 | **[B]** スタブを落として送信 | **エラートースト**（502）。成功と表示しない |
| 5 | **409** `--no-reject-concurrent` 無しで連打 | 「機器がビジーです」の **warn** 表示（赤エラーにしない） |
| 6 | **504** `--send-duration 15` で送信 | 「**送信できたか不明です**」の warn 表示 |
| 7 | **学習成功** 学習タブから開始 | 3 秒後にスタブがコールバック → **ポーリングで success 表示**、一覧に信号が増え要素数が出る |
| 8 | **学習タイムアウト** `--callback-fail` で起動して学習 | 15 秒後に **timeout 表示**。「成功」と出ない |
| 9 | **学習中の送信ロック** 学習開始直後にリモコンタブ | 送信ボタンが全て disabled |
| 10 | **[D]** `$env:IR_JOBS_DB_PATH` を読み取り専用にする等で health を崩す | **赤バナー**が出る |
| 11 | **`last_job_error`** 死んだポート宛の目覚ましを発火させる | **黄色の別枠**。赤バナーにはならない |
| 12 | **[G]** 目覚ましを 120 秒で開始 → アラーム欄の「今すぐ止める」 | 即停止し、一覧から消える |
| 13 | **409 表示** 設定タブで最後の 1 台を削除 | サーバの日本語 `detail` がそのまま出る |
| 14 | **既定機器の削除** 2 台にして既定を削除 | 「機器 id=N を既定に昇格しました」が見える |
| 15 | **422 整形** DevTools から `{"esp32_ip":"x"}` を送る | `[object Object]` ではなく `body.esp32_ip: Extra inputs are not permitted` |
| 16 | **XSS** 信号名を `<img src=x onerror=alert(1)>` にリネーム | **スクリプトが実行されず**文字列として表示される |
| 17 | **モバイル** DevTools のデバイスエミュレーション（iPhone SE 幅 375px） | 横スクロールが出ず、ボタンが押せる大きさ |
| 18 | **コールバック URL 表示** 設定タブ | `http://127.0.0.1:8102` が見える（本番では `192.168.1.110`） |

最後に **本番 DB が無傷であること**を確認する（信号 2 件 / 機器 1 件、`/api/health` が `ok:true`）。

---

## 完了時に AGENTS.md へ書き戻すこと

- 進行状況表の Phase 5 を ✅ に。**不具合 A / F を「解消」に更新**
- **新規追加した API**（`POST /api/learn` / `GET /api/learn` / `GET /api/learn/{token}` /
  `POST /api/callback/ir_signal/{token}`）と `HealthOut.callback_base_url`
- **コールバック URL を token 制にした設計判断**と、旧実装が任意の信号を上書きできた点
- **期限切れコールバックを 404 で捨てつつ WARNING に残す**方針
  — 「学習が時々失敗する」の切り分けに使う証拠なので、次フェーズ担当が消さないように
- **`required` を使わず `fieldset.disabled`** にした理由（不具合 A の根治方法）
- **`toISOString` / `innerHTML` / `esp32_ip` の文字列検索ガード**を入れたこと
  （新しい JS を足すときに引っかかる可能性があるため）
- **旧 `ir_db_server.py` を削除した**こと（＝旧 UI への退路は git 履歴のみ）
- Phase 7 への申し送り: **`jinja2` 依存が未使用になった**（旧モノリス削除により）。
  `uv remove jinja2` は uv.lock の再生成を伴うので Phase 7 でまとめる
- 学習の実挙動から新たに分かったこと（特にコールバックの到達性について）

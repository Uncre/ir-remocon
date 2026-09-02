# Phase 2: ESP32 通信レイヤ — 実装計画

> Codex 移管メモ: これは Claude Code で作成した計画の履歴スナップショット。
> 現在の作業規約は `../../../AGENTS.md`、実装後の確定事項は `../HANDOFF.md` を優先する。

親計画: `master-plan.md`
引き継ぎメモ: `../HANDOFF.md`

---

## Context

このフェーズが潰すのは、**「UI は成功と言うのに家電が反応しない」** という体感バグの根そのもの。
調査で確定している 2 件が対象:

- **バグ B**: `ir_db_server.py:225` の `send_signal_to_esp32` が `execute_ir_send` の結果を見ずに常に
  `{"status":"ok"}` を返す。`execute_ir_send`(58-74) は例外を `print` して握り潰す。
  実ログに `Error sending ... Reason: timed out` の直後に `200 OK` が並んでいる。
- **バグ C**: サーバ側に直列化もデバウンスも無く、連打すると 6 リクエストが同時に ESP32 へ飛ぶ。
  ファーム側も非同期ハンドラ内で `irsend.sendRaw()` を同期実行して TCP ごとブロックするため、
  全部タイムアウトする（ファーム側は Phase 6）。

Phase 1 で土台（config / db / logging / models）は完成済み。ここに通信レイヤを載せる。

**スコープはユーザー確認済みで「動く最小スライス」**: `esp32.py` 単体では「失敗が 502 になる」ことを
実際に動かして確認できないため、`uv run ir-remocon` が起動して実機なしで検証できる所まで含める。
親計画も「`fake_esp32.py` は Phase 2 完了直後に用意する」と書いている。

**スコープ外（明示）**: スケジューラ(Phase 4)、機器 CRUD API(Phase 3)、学習フロー(Phase 5)、
フロントエンド(Phase 5)、ファーム(Phase 6)。旧 `ir_db_server.py` は**触らず残置**（削除は Phase 5）。

### 確定済みの設計判断（ユーザー確認済み）

1. **同期実装で統一**。FastAPI の `def` エンドポイントはスレッドプールで動くので問題なく、
   Phase 4 の APScheduler ワーカースレッドと同じコードを共有できる。
2. **ロック待ちは上限あり**。`lock.acquire(timeout=5.0)` で待ち、取れなければ 409。
   無制限に待つとスレッドが溜まる。即 409 だと正当な連続操作まで弾く。
3. **読み取りタイムアウトは独立した例外**。現ファームは `irsend.sendRaw()` 完了後に応答するため、
   タイムアウト時は**実際には発射済みの可能性がある**。`Esp32Timeout` → 504 +「結果不明」と正直に伝え、
   **リトライしない**（トグル型信号を二度打ちすると状態が反転する）。

---

## 実装

### 1. `ir_remocon/app/esp32.py` — 新規（本フェーズの中心）

#### 例外階層

`http_status` を例外クラスの属性に持たせる。マッピングを 1 箇所に閉じ込め、
例外を増やしても `main.py` を編集せずに済む。

| 例外 | HTTP | 意味 |
|---|---|---|
| `Esp32Error`(基底) | 502 | `host` / `message` / `esp_status` / `outcome_unknown` を持つ |
| `Esp32Unreachable` | 502 | 接続が成立しない |
| `Esp32BadStatus` | 502 | ESP が想定外のステータスを返した |
| `Esp32Busy`(基底) | 409 | ビジー |
| ├ `Esp32LocalBusy` | 409 | 自サーバのロックが取れなかった |
| └ `Esp32DeviceBusy` | 409 | ESP が 409 を返した |
| `Esp32Timeout` | 504 | 読み取りタイムアウト。`outcome_unknown = True` |

`Esp32Busy` を 2 種に分ける理由: HTTP は同じ 409 でも意味が違う。前者は「システム正常・飽和しただけ」、
後者は「機器が受信モードか固まっている」。Phase 4 の health とログ調査でこの区別が要る。

#### httpx クライアント（テスト可能性）

```python
_client: httpx.Client | None = None      # 遅延生成・ダブルチェックロック
def get_client() -> httpx.Client
def set_client(client: httpx.Client | None) -> None   # テストで MockTransport に差し替え
def close_client() -> None                            # lifespan の shutdown で呼ぶ
def reset_state() -> None                             # ロック登録簿と last_send をクリア(テスト用)
```

すべてのリクエストは `get_client()` 経由。これだけで
`esp32.set_client(httpx.Client(transport=httpx.MockTransport(handler)))` で差し替えられる。
DI コンテナも `Depends` も要らない。

**`limits=httpx.Limits(max_keepalive_connections=0)` を指定する**（重要）。
ESPAsyncWebServer は接続を積極的に閉じるため、プールに残った idle 接続を再利用した瞬間に
`RemoteProtocolError: Server disconnected` が出るのは典型的な事故。これを 502 にしてしまうと
「1 バイトも送っていないのに送信失敗と報告する」＝また新種の「たまに失敗する」を作り込む。
代償は LAN 内で TCP ハンドシェイク 1 往復（〜1ms）。どのみち per-host ロックで直列化しているので
プール再利用の利点はほぼ無い。

タイムアウトは `httpx.Timeout(connect=ESP32_CONNECT_TIMEOUT, read=ESP32_READ_TIMEOUT, ...)`。
`get_status()` だけは read=3.0 の別インスタンス（接続テストで 10 秒固まらせない）。

#### ホストごとのロックと最小送信間隔

```python
@dataclass
class _HostState:
    lock: threading.Lock
    last_send: float          # time.monotonic()

_states: dict[str, _HostState]
_registry_guard = threading.Lock()      # 登録簿自体を守る

@contextmanager
def _send_slot(host, lock_timeout):
    st = _state_for(host)
    if not st.lock.acquire(timeout=lock_timeout):
        raise Esp32LocalBusy(host, "この機器への送信が処理中です。少し待って再試行してください。")
    try:
        wait = config.MIN_SEND_INTERVAL - (time.monotonic() - st.last_send)
        if wait > 0:
            time.sleep(wait)        # ★ ロックの内側で待つ
        yield
    finally:
        st.last_send = time.monotonic()   # ★ 成否によらず更新
        st.lock.release()
```

非自明な点 3 つ:

1. **インターバル待機はロックの内側**。外でやると 2 スレッドが同時に「まだ 0.3 秒経ってない」を読んで
   両方待ち、ロックを取った瞬間に連続送信してしまう。内側で計測して初めて間隔が構造的に保証される。
2. **`last_send` はリクエスト完了後に打つ**。現ファームでは「赤外線が終わってから次を始めるまでの実測
   ギャップ」になる。Phase 6 で 202 即応答になっても「キュー投入完了から次まで」に劣化するだけ。
3. **失敗しても `last_send` を更新する**。失敗直後の ESP はむしろ詰まっており、バックオフの必要性は高い。

#### 例外分類

| 発生したもの | リトライ | 例外 | HTTP |
|---|---|---|---|
| `httpx.ConnectError`（接続拒否/経路なし/名前解決失敗） | **1 回**（0.2 秒後） | `Esp32Unreachable` | 502 |
| `httpx.ConnectTimeout` | しない | `Esp32Unreachable` | 502 |
| `httpx.ReadTimeout` / `WriteTimeout` / `PoolTimeout` | しない | `Esp32Timeout` | 504 |
| その他 `httpx.RequestError` | しない | `Esp32Unreachable` | 502 |
| レスポンス **200 / 202** | — | なし（成功） | 200 |
| レスポンス 409 | しない | `Esp32DeviceBusy` | 409 |
| その他 4xx/5xx/想定外の 2xx | しない | `Esp32BadStatus` | 502 |

- **接続確立前のエラーだけリトライ**。TCP が張れていないなら赤外線は絶対に出ていないので原理的に安全。
  `ConnectTimeout` は「ハンドシェイク完了を待つのを諦めた」であって「SYN が届かなかった」ではないので
  **リトライ対象から外す**（失うのは稀なリトライ 1 回、得るのは「二度打ちしない」の反例なき保証）。
- **成功は `{200, 202}` の厳格な allowlist**。202 は Phase 6 のファームが返すので今から受け入れる（前方互換）。
  「2xx なら成功」にしないのは、このリファクタの主題が「失敗を成功と言わない」ことだから。
- **ESP の 400 は 502 に落とす**。400 が返るのは我々のペイロードがファーム契約に合っていないときで、
  API 呼び出し側の落ち度ではない。素通しすると「あなたのリクエストが不正です」と嘘の非難になる。

#### 公開関数

```python
def send_raw(host, raw_data, freq=config.DEFAULT_FREQ_KHZ, *, lock_timeout=None) -> None
def start_receive(host, callback_url, timeout_ms=None, *, lock_timeout=None) -> None
def get_status(host) -> dict          # ★ ロックを取らない
```

- `send_raw` の戻り値は `None`。「例外が出なければ成功」という不変条件そのものがバグ B の修正内容。
  docstring には**「ESP がリクエストを受理した」ことしか保証しない**と明記する（赤外線が出たことは保証しない）。
- `start_receive` も**同じ `_send_slot` を使う**。ESP は `currentMode` 一本の状態機械なので競合を防ぐ。
  ただしロックは 202 で解放され、ESP はその後 15 秒受信モードに留まる。その間の送信は ESP から本物の
  409 が返る = 正しい挙動（15 秒ロックを保持して全体を止める方が悪い）。Phase 5 の UI で送信ボタンを無効化する。
- `get_status` がロックを取らないのは、読み取り専用であり、かつ「接続テスト」は**機器がビジーなときこそ**
  使いたいから。
- URL 組み立ては `models.normalize_host()` を再利用（`http://` や末尾スラッシュの混入に耐える）。

### 2. `ir_remocon/app/config.py` — 追記 1 行

```python
#: 同一機器のロック待ちの上限(秒)。超えたら 409 を返して呼び出し側に判断を返す。
ESP32_LOCK_TIMEOUT = float(os.environ.get("IR_ESP32_LOCK_TIMEOUT", "5.0"))
```

導出値（`MIN_SEND_INTERVAL + CONNECT + READ` = 12.3 秒）にしない理由: 1 件詰まっているだけで
クリックが 12 秒待たされ、その間 FastAPI のスレッドプールワーカーを 1 本占有する。
`send_raw(..., lock_timeout=...)` で呼び出しごとに上書き可能にする（Phase 4 のアラームは
interval 1 秒なのに 5 秒待つのは不合理で、短いタイムアウトで 1 拍スキップしたい）。

> `ESP32_READ_TIMEOUT = 10.0` は**今回は変更しない**。長いという指摘はあるが、`ESP32_LOCK_TIMEOUT=5.0`
> が入るのでロック待ちは 5 秒で頭打ちになり、停滞は非有界にならない。実機が戻って実測できる
> Phase 6 で見直す（env で上書き可）。

### 3. `ir_remocon/app/models.py` — 追記 1 行

`SendRequest` に `model_config = ConfigDict(extra="forbid")` を追加。

理由: Pydantic v2 の既定は `extra="ignore"`。旧フロントが送る `{"esp32_ip": "192.168.1.99"}` は
**422 にならず受理され、`device_id=None` として既定機器に送られる**。つまり
「ユーザーが 192.168.1.99 を指定 → 200 OK → 赤外線は 192.168.1.4 に飛ぶ」。
嘘の成功を潰すフェーズで新種の静かな誤動作を作り込むことになるので、フィールド名を名指しした 422 で落とす。
継承先の `ScheduleBase` / `LearnStartRequest` にも波及するが、それも望ましい。

### 4. `ir_remocon/app/repository.py` — 新規

#### ドメイン例外

`RepositoryError`(500) / `NotFound`(404) / `DuplicateName`(409)。
`HTTPException` はリポジトリに持ち込まない — Phase 4 の `jobs.py` は HTTP コンテキストの外から呼ぶ。
`esp32.py` と同じ「例外クラスに `http_status`、変換は `main.py` のハンドラ」パターンで統一する。

#### 信号（今フェーズで実装）

```python
list_signals() -> list[dict]          # id,name のみ。ORDER BY name COLLATE NOCASE
get_signal(name) -> dict              # raw_data デコード済み。NotFound
get_signal_raw(name) -> list[int]     # 送信ホットパス。SELECT raw_data のみ
create_signal(name, raw_data) -> dict # DuplicateName
update_signal(name, *, new_name=None, raw_data=None) -> dict   # NotFound / DuplicateName
delete_signal(name) -> None           # NotFound
```

ここで潰すバグ H:
- **ORDER BY 欠落** → `ORDER BY name COLLATE NOCASE`
- **リネーム衝突で 500** → 単一の `get_conn()` 内で完結させる（旧実装は接続保持中に `get_signal_by_name`
  を呼んで 2 本目の接続を開いていた）。事前チェック + `except sqlite3.IntegrityError` の**両方**を置く
  （事前チェックは同時実行に対して原理的に競合するため）。
- `created_at` / `updated_at` に実際に書き込む（Phase 1 で列は足したが誰も書いていない）。

`upsert_signal()`（学習コールバック用）は**入れない** — コールバックルータが Phase 5 なので。

#### 機器（今フェーズは読み取りのみ）

```python
list_devices() / get_device(id) / get_default_device() / resolve_device(id|None) -> dict
```

`resolve_device` の挙動:

| 状況 | 挙動 |
|---|---|
| id 指定・存在する | その行 |
| id 指定・存在しない | `NotFound` → 404 |
| `None` | `WHERE is_default=1 ORDER BY id LIMIT 1` |
| `None` で既定フラグが 1 件も無い | `ORDER BY id LIMIT 1` にフォールバック + WARNING ログ |
| `devices` が空 | `NotFound("機器が 1 台も登録されていません")` → 404 |

「機器ゼロ」に専用ステータス（503/424）を作らず 404 に寄せる: `resolve_device(None)` は
「既定機器」というリソースを引く操作なので 404 が素直。409 は `Esp32Busy` に予約したい（フロントの分岐が濁る）。
→ **Phase 3 で「最後の 1 台は削除禁止」のガードを入れること**を申し送る。

戻り値は `sqlite3.Row` でも Pydantic モデルでもなく**素の `dict`**。Phase 4 の `jobs.py` が欲しいのは
`host` 文字列であって `DeviceOut` ではない。Pydantic 構築はルータの責務。

**Phase 3 に残す**: `create/update/delete_device`、`is_default` の排他制御、`GET /api/devices/{id}/status`。

### 5. `ir_remocon/app/routers/signals.py` — 新規

`APIRouter(prefix="/api/signals", tags=["signals"])`。GET(一覧) / POST / GET(単体) / PUT / DELETE。
いずれも本体は `return repository.xxx(...)` の 1〜2 行。**`HTTPException` は書かない**（ハンドラが変換）。

> **落とし穴**: prefix がある場合、コレクションのパスは `"/"` ではなく **`""`**。`"/"` にすると
> `/api/signals` へのアクセスが 307 リダイレクトになる。

DELETE は旧実装と同じ 200 + `{"message": ...}` を維持（204 化はフロント刷新と同時が筋）。
信号名に `/` を含むと 404 になるのは旧実装から不変 — 今は直さない。

### 6. `ir_remocon/app/routers/send.py` — 新規

```python
@router.post("/{name}")
def send_signal(name: str, req: SendRequest | None = None) -> dict:
    raw_data = repository.get_signal_raw(name)                            # NotFound -> 404
    device   = repository.resolve_device(req.device_id if req else None)  # NotFound -> 404
    esp32.send_raw(device["host"], raw_data)                              # -> 409/502/504
    return {"status": "ok", "name": name,
            "device_id": device["id"], "host": device["host"]}
```

**このルータの本質は `try/except` が 1 つも無いこと。** `return` に到達する経路は成功しかない。
バグ B の修正がコードの形として現れている。

- レスポンスに `host` / `device_id` を含めるのは、トーストが「どこへ送ったか」を出せるようにするため。
- 順序も意図的: 信号の存在確認を機器解決より先に。存在しない信号名で ESP に無駄な通信をしない。
- ボディは `SendRequest | None = None` で省略可（curl / テストから叩きやすい）。
  既定値に `SendRequest()` を置くのは可変インスタンス共有になるので避ける。

### 7. `ir_remocon/app/main.py` — 新規

```python
def create_app() -> FastAPI     # テストが一時DBを指した状態で組み立て直せる
app = create_app()              # uvicorn "ir_remocon.app.main:app" 用
def run() -> None               # [project.scripts] のエントリ（既に pyproject に宣言済み）
```

#### lifespan（deprecated な `@app.on_event` は使わない）

```python
setup_logging()      # ★ init_db より先
logger.info("起動: DB=%s ADVERTISE_HOST=%s PORT=%s", ...)
init_db()
yield
esp32.close_client()
```

**`setup_logging()` を `init_db()` より先に**呼ぶのが順序の要点。逆にすると `init_db()` の
「列を追加しました」「既定機器を登録しました」が、ハンドラ未設定のルートロガーに落ちて消える。
移行が黙って起きるのは、まさにこのリファクタが潰したい類の事象。

`run()` は `uvicorn.run()` の**前にも** `setup_logging()` を呼ぶ。`logging_conf._configured` ガードで
二重呼び出しは無害（このガードが存在する理由がこれ）。

> **必須**: `uvicorn.run(app, host=..., port=..., log_config=None)` と **`log_config=None` を明示**。
> 既定の log_config は `uvicorn.*` ロガーに自前ハンドラを再設定し、`logging_conf.py` の
> `handlers.clear()` を上書きして無効化する。

#### static / templates

`config.STATIC_DIR` はディスク上に**存在しない**。`StaticFiles(directory=...)` は mount 時点で
`RuntimeError` を投げて**サーバが起動しない**。存在チェックでガードし、スキップをログに残す:

```python
if config.STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
else:
    logger.info("static が無いためマウントを省略: %s (Phase 5 で作成)", config.STATIC_DIR)
```

`check_dir=False` の 1 行版もあるが、それだと設定ミスが永遠に静かな 404 になる。
`Jinja2Templates` は今フェーズでは構築しない（下記の通り `/` を出さないため）。

#### `/` で旧 `index.html` を出すか → **出さない**

旧 `index.html` は `esp32_ip` を送る。上記 3 の `extra="forbid"` を入れれば 422 で落ちるが、
そもそも旧 UI は device_id を知らないので操作不能。**最小の JSON スタブを返す**:

```python
{"status": "ok", "phase": 2, "note": "UI は Phase 5 で実装します。API ドキュメント: /docs"}
```

旧 UI を触りたいときは旧モノリス `ir_db_server.py` をそのまま起動すればよい（残置済み）。

#### CORS

```python
allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"]
```

旧実装の `allow_origins=["*"]` + `allow_credentials=True` は**やめる**。Starlette はこの組み合わせで
Origin をそのまま反響させ、事実上「任意オリジンから資格情報付きリクエスト可」になる。
このアプリは Tailscale 経由で LAN 外からも開かれる。認証もクッキーも無いので `allow_credentials` は不要。

#### 例外ハンドラ（各ルータの try/except ではなくこちらを採用）

```python
@app.exception_handler(esp32.Esp32Error)      # 基底1つで全サブクラスに効く（Starlette は __mro__ を辿る）
def _esp32_handler(request, exc):
    logger.warning("ESP32 通信エラー [%s] %s", type(exc).__name__, exc)
    return JSONResponse(exc.http_status,
        {"detail": exc.message, "error": type(exc).__name__,
         "host": exc.host, "outcome_unknown": exc.outcome_unknown})

@app.exception_handler(repository.RepositoryError)
def _repo_handler(request, exc): ...
```

ハンドラを採る理由:
1. Phase 3(接続テスト) / Phase 5(学習開始) が**同一のマッピングを必要とする**。1 箇所なら食い違わない。
2. ルータから ESP の知識が消え、「例外を握り潰す try/except」を書く場所自体が無くなる
   → **バグ B の再発を構造的に防ぐ**。
3. Phase 4 のスケジューラは HTTP 抜きで `esp32` を直接呼ぶ。だからマッピングを `esp32.py` に
   置いてはいけない。ハンドラはちょうど HTTP 境界そのもの。

キーに `detail` を選ぶのは意図的 — FastAPI の `HTTPException` と同じキーなので Phase 5 のトーストが 1 本で済む。

> **ファイル冒頭の docstring に恒久的制約として明記**: ロック登録簿はプロセスローカルなので
> **`workers` は永久に 1**。増やした瞬間に直列化（バグ C の修正）が丸ごと無効化される。同じ理由で
> `reload=True` も使わない。アプリ**オブジェクト**を渡す（文字列ではなく）ことで構造的に強制する。

### 8. `tools/fake_esp32.py` — 新規（以降の全フェーズの検証基盤）

`build_app(opts) -> FastAPI` と `main()`（argparse）に分ける。関数に切り出すのは、テストから import して
`uvicorn.Server` をスレッド起動でき、subprocess なしで実ソケット統合テストが書けるから。
**`ir_remocon` は import しない** — ハードウェアの代役なので任意のサーバに向けられる独立物であるべき。

ハンドラは `def` + `threading.Lock` で `currentMode` 相当の状態機械を再現。
**`/ir/send` は `time.sleep(send_duration)` で同期ブロックする** — これが実ファームの挙動であり、
これが無いとバグ C が再現しない。

| エンドポイント | 挙動 |
|---|---|
| `GET /status` | `{"status":"ok","device_mode":"idle|receive|send",...}` + 前方互換で `send_count`/`last_send_ok` |
| `POST /ir/send` | 検証(不正→400) → mode≠idle なら 409 → sleep → `--send-status`(既定200) → idle |
| `PUT /mode` | mode≠idle なら 409 → 検証 → 202 即返し → `threading.Timer` で callback_url へ POST → idle |

| フラグ | 既定 | 何を試せるか |
|---|---|---|
| `--host` / `--port` | `127.0.0.1` / `8080` | バインド先 |
| `--send-duration` | `0.2` | **`15` にすると読み取りタイムアウト(504)を再現** |
| `--send-status` | `200` | **`202` で Phase 6 のファームを先取り検証** |
| `--fail-mode` | `none` | `busy`(409) / `error`(500) / `bad-request`(400) / `hang` / `drop` |
| `--fail-rate` | `0.0` | 「たまに失敗する」の再現 |
| `--no-reject-concurrent` | — | ESP の 409 を無効化し、直列化が無い世界と比較 |
| `--callback-delay` | `3.0` | 学習コールバックまでの秒数 |
| `--callback-fail` | off | コールバックを送らない（**未解決の「学習が時々失敗する」の切り分け用**） |

接続拒否（502）用のフラグは作らない — スタブを起動しないか別ポートを指せば再現できる。

`--send-duration 15` は特に価値がある: **504 を返した後に ESP 側では実際に赤外線が出ている**という
最も厄介な現実ケースを再現できる。「読み取りタイムアウトはリトライしない」決定が守っているのがこれ。

### 9. `tests/` — 新規

```
conftest.py                 # 一時DB / esp32 状態リセット / 本番DB保護
test_esp32.py               # MockTransport: 例外分類・直列化・インターバル・リトライ
test_repository.py          # 信号CRUD・リネーム衝突・並び順・機器解決
test_routers_signals.py     # CRUD の HTTP 層
test_send.py                # 例外 → HTTP マッピング（★ バグ B の証明）
test_integration_stub.py    # 実ソケット。@pytest.mark.integration で既定スキップ
```

`conftest.py`:
- `temp_db`: `monkeypatch.setattr(config, "DB_PATH", tmp_path/"test.db")` + `db.init_db()`。
  `get_conn()` は `config.DB_PATH` を呼び出しごとに評価するので import 順の罠は無い。
  `init_db()` が既定機器 id=1 を入れるので、そのまま使える。
- `autouse` で `esp32.reset_state()` / `set_client(None)`、`MIN_SEND_INTERVAL` を 0 に。
  インターバル検証テストだけが明示的に 0.3 へ戻す（スイート全体が秒単位で遅くなるのを防ぐ）。
- **本番 DB 保護のガードを入れる**。`.gitignore` に `*.db` があり未追跡＝失うと復旧手段が無い
  （AGENTS.md 絶対ルール 3 の自動化）。
- `TestClient` は**必ず `with TestClient(app) as client:`**。この形にしないと lifespan が走らず
  `init_db()` が呼ばれない（典型的な静かな失敗）。fixture 内で context manager として yield する。

`pyproject.toml` に `markers = ["integration"]` と `addopts = "-m 'not integration'"` を追加。

#### バグ B の証明（`test_send.py` 表駆動）

| MockTransport の挙動 | 期待 |
|---|---|
| `Response(200)` / `Response(202)` | **200** |
| `Response(409)` | 409 |
| `Response(400)` / `Response(500)` | 502 |
| `raise ConnectError` | 502 |
| `raise ReadTimeout` | 504 + body に `outcome_unknown: true` |
| 別スレッドがロック保持中 | 409 |

**回帰ガード**として、失敗系の全行に `assert r.status_code != 200` を足す。旧ログの
`Reason: timed out` → `200 OK` が二度と起きないことを機械的に固定する。
加えて `test_no_esp_call_when_signal_missing`（`assert len(recorded) == 0`）。

#### バグ C の証明（`test_esp32.py`）

- `test_send_raw_is_serialized_per_host`: 10 スレッドが同一ホストへ → `max_concurrent == 1`、
  かつ**全件成功**（旧実装は全部タイムアウトした）
- `test_different_hosts_run_in_parallel`: → `max_concurrent == 2`
- `test_min_send_interval_is_enforced`: 連続 3 発、各間隔 ≥ 0.3s

**壁時計ではなく同時実行カウンタで判定する**（Windows・高負荷で flaky になりにくい）。
インターバルのテストだけ壁時計が不可避なので、そこだけ 0.3s × 3 発（約 0.6 秒）で許容。

安全側の要:
- `test_read_timeout_is_not_retried`: `len(recorded) == 1` — リトライすればトグル型信号を
  二度打ちして状態が反転する、という理由を docstring に書く
- `test_connect_error_is_retried_once`: `len(recorded) == 2`
- `test_connect_error_recovers_on_retry`: 1 発目 raise / 2 発目 200 → 成功
- `test_lock_timeout_raises_local_busy`
- `test_payload_shape`: `{"format":"raw","freq":38,"data":[...]}` と URL

統合テスト（既定スキップ）は `fake_esp32.build_app()` を import して `uvicorn.Server` をスレッド起動し、
閉じたポート→502 / `send_duration>read_timeout`→504 / 同時 10 件→全成功かつスタブ側 409 ゼロ / 202→200 を確認。

---

## 実装順序

1. `config.py` に `ESP32_LOCK_TIMEOUT` 追記
2. `models.py` の `SendRequest` に `extra="forbid"` 追記
3. `repository.py` → `test_repository.py`
4. `esp32.py` → `test_esp32.py`　※ 3 と 4 は独立、並行可
5. `routers/signals.py` / `routers/send.py`
6. `main.py`（ハンドラ・lifespan・ルータ登録・`run()`）→ `test_routers_signals.py` / `test_send.py`
7. `tools/fake_esp32.py` → `test_integration_stub.py`
8. 手動検証（下記）
9. **`AGENTS.md` の進行状況表・環境変数表・該当セクションを更新**（絶対ルール）

---

## 検証

作業前に `ir_remocon/ir_database.db` を `backup_before_refactor/` へコピーしておく
（AGENTS.md 絶対ルール 3。スキーマ変更は無いが新サーバが初めて DB を開くため）。

```powershell
uv sync
uv run pytest -v                      # 単体（integration は既定スキップ）
uv run pytest -v -m integration       # 実ソケット
```

手動検証（別ターミナルでスタブ、実機不要）。
**機器編集 API は Phase 3 なので、本番 DB は触らず検証用の別 DB を使う**
（`IR_DB_PATH` を差し替えれば `init_db()` が既定機器 id=1 を作る。あとは host だけ書き換える）:

```powershell
# ターミナル1: スタブ
uv run python tools/fake_esp32.py --port 8080

# ターミナル2: 検証用DBを作って host をスタブに向ける
$env:IR_DB_PATH = "$env:TEMP\ir_verify.db"
uv run python -c "from ir_remocon.app import db; db.init_db()"
uv run python -c "from ir_remocon.app import db; c=db.get_conn().__enter__(); c.execute(\"UPDATE devices SET host='127.0.0.1:8080' WHERE id=1\"); c.commit()"
uv run ir-remocon
```

> 本番 `ir_database.db` を検証に使わないので、既存の学習済み信号 2 件は一切触らない。
> 検証用 DB には信号が無いので、`POST /api/signals` で適当な信号を 1 件作ってから送信テストする。

| 確認項目 | 期待 |
|---|---|
| **[B] スタブを落として送信** | **502** + エラー内容が返る（旧実装は 200 OK） |
| **[B] スタブ `--fail-mode error`** | 502 |
| **[C] 送信を 10 連打** | 全部 200、スタブ側ログに 409 が 1 件も出ない |
| **[C] `--no-reject-concurrent` でも** | サーバ側で直列化されている（スタブログの時刻が 0.3s 以上離れる） |
| **[504] `--send-duration 15`** | 504 + `outcome_unknown: true`。**リクエストは 1 回だけ**（スタブログで確認） |
| **[Phase6 前方互換] `--send-status 202`** | 200 |
| **[409] `--fail-mode busy`** | 409 |
| 存在しない信号名で送信 | 404、かつスタブに 1 件もリクエストが来ない |
| `extra="forbid"` の効き | `{"esp32_ip":"..."}` を送ると 422（黙って既定機器に送らない） |
| ログ | `ir_db_server.log` にタイムスタンプ付きで出る。uvicorn の行も同じ書式 |
| CWD 非依存 | 別ディレクトリから `uv run --project <path> ir-remocon` で同じ DB を掴む |

---

## 申し送り（`AGENTS.md` に書き戻す）

- **Phase 2〜3 の間、新サーバに予約機能は存在しない**（スケジューラは Phase 4）。新アプリは `jobs.db` を
  一切開かない。既存 7 件は既に死んでいる（バグ D）ので実害なし。
- 新サーバは旧サーバと**同じポート 8102**。同時起動不可。
- `uvicorn` の `workers` は永久に 1（ロック登録簿がプロセスローカル）。
- **Phase 3 で「最後の 1 台は削除禁止」ガードを入れる**（`resolve_device` が 404 で全送信を殺すため）。
- **Phase 5 の UI**: 学習中は送信ボタンを無効化する（ESP の状態機械が単一のため 409 になる）。
  409 は「エラー」ではなく「機器がビジーです」と描画する。504 は「送信できたか不明です」と描画する。
- **Phase 4 のアラーム**は短い `lock_timeout` を渡して 1 拍スキップさせる（積み上がらせない）。
- **Phase 6 で決めること**: 202 は「キューに入れた」しか証明しない。UI の言い回しを変えるか、
  `/status` の `send_count` 増分をポーリングして実発射を確認するか。
- `ESP32_READ_TIMEOUT=10.0` は実機が戻ったら実測して見直す。
- 新規環境変数 `IR_ESP32_LOCK_TIMEOUT`（既定 5.0）を環境変数表に追加。

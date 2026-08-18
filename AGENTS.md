# AGENTS.md — ir-remocon

ESP32 + FastAPI による自宅用スマート赤外線リモコン。
**現在リファクタリング作業中。フェーズごとにチャットを分けて進めている。**
新しいチャットを始めたら、まずこのファイルを最後まで読むこと。

---

## 絶対に守るルール

1. **Python 環境は uv で管理すること。** `pip install` / `python -m venv` / `requirements.txt` は使わない。
   依存の追加は `uv add <pkg>`、実行は `uv run ...`。`uv.lock` は必ずコミット対象。
2. **フェーズの区切りで必ず手を止めて報告する。** 勝手に次フェーズへ進まない。
3. **DB を触る作業の前にバックアップを取る。** 既存の学習済み信号は再取得に実機が要るため失うと痛い。
4. ESP32 の実機は現在**稼働しておらず、開発環境も無い**。ファームの検証は不可。
   実機作業（USB接続・書き込み・リモコン送受信）が必要なときは、手順を書いてユーザーに依頼する。

---

## リファクタリング進行状況

計画の全文: `C:\Users\UncrewedSloth\.claude\plans\esp32-smooth-waffle.md`
（背景・確定バグの証拠・検証手順・実機作業手順まで含む。**次フェーズ担当は必ず読むこと**）

| Phase | 内容 | 状態 |
|---|---|---|
| 1 | 土台: config / db / logging（+ models） | ✅ **完了・検証済み** (2026-08-18) |
| 2 | ESP32 通信レイヤ: 送信の直列化・失敗を握り潰さない | ✅ **完了・検証済み** (2026-08-18) |
| 3 | 機器登録と IP 管理（`esp32_ip` → `device_id`） | ✅ **完了・検証済み** (2026-08-18) |
| 4 | スケジューラ堅牢化（health / 中断可能アラーム / メタ情報） | ⬜ 未着手 ← **次はここ** |
| 5 | フロントエンド（タブUI・static分割・体感バグ修正） | ⬜ 未着手 |
| 6 | ESP32 ファーム（`esp/ir_remocon.ino`）※書き込みは後日 | ⬜ 未着手 |
| 7 | 周辺整備（README / migrate_jobs / 追加テスト） | ⬜ 未着手 |

> `tools/fake_esp32.py`（ESP32 スタブ）は Phase 2 で作成済み。**以降の検証はこれを使う。**

### Phase 1 で完成したもの

| ファイル | 役割 |
|---|---|
| `ir_remocon/app/config.py` | 全設定を集約。`__file__` 起点の絶対パス。環境変数で上書き可 |
| `ir_remocon/app/db.py` | `get_conn()` コンテキストマネージャ / WAL / 冪等なスキーマ移行 |
| `ir_remocon/app/logging_conf.py` | `logging` + RotatingFileHandler(5MB×3) |
| `ir_remocon/app/models.py` | Pydantic スキーマ。繰り返し種別ごとの必須項目検証を含む |
| `pyproject.toml` / `uv.lock` / `.python-version` | uv 管理。hatchling で editable インストール |

検証済みの事実:
- 既存信号 2 件（`room_light_turn_on` / `room_light_turn_off`）は保持されている
- `devices` テーブル作成 + `ir_signals` への `created_at`/`updated_at` 追加が完了
- `init_db()` は冪等（2 回実行して差分なし）
- `journal_mode = wal` 有効
- `once` で日付なし / `weekly` で曜日なし / 不正な曜日 → すべて 422 で弾く

### Phase 2 で完成したもの

計画の全文: `C:\Users\UncrewedSloth\.claude\plans\phase2-ethereal-cocke.md`

| ファイル | 役割 |
|---|---|
| `ir_remocon/app/esp32.py` | **本フェーズの中心。** 機器ごとの Lock + 最小送信間隔で直列化。失敗は型付き例外 |
| `ir_remocon/app/repository.py` | 信号 CRUD + 機器の読み取り解決。SQL をここに集約 |
| `ir_remocon/app/routers/signals.py` / `send.py` | 薄いルータ。**`try/except` を 1 つも書かない** |
| `ir_remocon/app/main.py` | lifespan / 例外ハンドラ / CORS / `run()`。`uv run ir-remocon` が動く |
| `tools/fake_esp32.py` | ESP32 スタブ。実機なしで全フローを検証できる |
| `tests/` | 84 件（うち統合 8 件）。`uv run pytest` / `uv run pytest -m integration` |

実測で確認済みの事実（スタブ使用、実機不要）:

| 確認 | 結果 |
|---|---|
| **[B] 送信失敗** | スタブ停止中に送信 → **502** `Esp32Unreachable`（旧実装は 200 OK を返していた） |
| **[C] 10 連打** | ESP 側の `max_concurrent = 1`、ESP 側の 409 は **0 件**。7 件成功 + 3 件が 409 `Esp32LocalBusy`（ロック待ち 5 秒超過。設計どおりの正直な応答） |
| **読み取りタイムアウト** | ファーム 15 秒ブロック → **504** `outcome_unknown: true`。**ESP 側の受信回数は 1 回**（＝リトライしていない＝トグル信号を二度打ちしない） |
| **Phase 6 前方互換** | ファームが 202 を返しても 200（成功）と判定する |
| **`extra="forbid"`** | 旧フロントの `{"esp32_ip": ...}` は **422**。黙って既定機器に送らない |
| **リネーム衝突** | 409（旧実装は 500） |
| **一覧の並び** | `ORDER BY name COLLATE NOCASE` で安定 |
| **CWD 非依存** | 別ディレクトリから `uv run --project <path>` で同じ絶対パスの DB を掴む |
| **ログ** | uvicorn の行も含めて同一書式でファイルに出る |
| **本番 DB** | 既存信号 2 件は無傷（検証は `IR_DB_PATH` で別 DB を使った） |

#### Phase 2 の設計上、以降のフェーズが必ず守ること

- **`uvicorn` の `workers` は永久に 1。** `esp32.py` のロック登録簿はプロセスローカルなので、
  増やした瞬間に送信の直列化（バグ C の修正）が丸ごと無効化される。`reload=True` も同様に不可。
- **ルータに `try/except` を書かない。** 例外 → HTTP の変換は `main.py` の例外ハンドラ 1 箇所だけ。
  Starlette は `__mro__` を辿るので、基底（`Esp32Error` / `RepositoryError`）の登録だけで
  サブクラスに効く。新しい例外を足しても `main.py` は無変更でよい。
- **`esp32.py` に HTTP ステータスのマッピングを持ち込まない。** Phase 4 のスケジューラは
  HTTP レイヤ抜きで `esp32` を直接呼ぶため。`http_status` は例外クラスの属性として持つ。
- **読み取りタイムアウトはリトライ禁止。** 現ファームは `irsend.sendRaw()` 完了後に応答するので、
  再送はトグル型信号の二度打ち＝状態反転を意味する。リトライは `ConnectError` のみ 1 回。

### Phase 3 で完成したもの

計画の全文: `C:\Users\UncrewedSloth\.claude\plans\phase3-logical-neumann.md`

| ファイル | 役割 |
|---|---|
| `ir_remocon/app/routers/devices.py` | **本フェーズの中心。** 機器 CRUD + 接続テスト |
| `ir_remocon/app/repository.py` | 機器の書き込み系を追加（`create_device` / `update_device` / `delete_device` / `_set_default`）。例外に `ConstraintViolation`(409) |
| `ir_remocon/app/models.py` | `DeviceStatusOut` を実際の形に、`DeviceDeletedOut` 追加、入力モデルに `extra="forbid"` |
| `tests/test_devices.py` | 26 件。単体は計 110 件 / 統合 8 件 |

**スキーマ変更なし**（`devices` テーブルは Phase 1 で作成済み）。本番 DB は無変更で、
検証は `IR_DB_PATH` を一時 DB に向けて行った。

この層が守る不変条件は 2 つ。破れると送信経路が丸ごと死ぬ:

1. **機器は常に 1 台以上存在する** — 0 台になると `resolve_device()` が 404 を投げ、送信も予約も全滅
2. **既定機器は常にちょうど 1 台** — 破れても即死しないが「既定は無いのに送信は動く」不可解な状態になる

実測で確認済みの事実（スタブ使用、実機不要）:

| 確認 | 結果 |
|---|---|
| **[E] host 変更の即時反映** | `PUT /api/devices/1 {"host": ...}` → **次の送信から新 host へ**（再起動も予約作り直しも不要） |
| **接続テスト（不可）** | **200 + `reachable:false`** + `detail` に理由。502 にはしない |
| **接続テスト（可）** | `reachable:true` + ESP の `/status` 中身（`send_count` 等）をそのまま返す |
| **host 正規化** | `"http://127.0.0.1:8080/"` → DB には `"127.0.0.1:8080"` |
| **既定の排他** | 2 台目を `is_default:true` で登録 → 1 台目のフラグが自動で降りる（常に 1 台） |
| **既定を外す PUT** | **409** `ConstraintViolation`（「先に別の機器を既定に」） |
| **最後の 1 台の削除** | **409**。機器は残り送信も生きたまま |
| **既定機器の削除** | 最若番を自動昇格し、`new_default_device_id` をレスポンスに明記（黙って変えない） |
| **削除済み機器を明示指定して送信** | **404**。黙って既定機器に送らない |
| **タイプミス `is_defualt`** | **422**（`extra="forbid"`）。既定にしたつもりが違う、を防ぐ |
| **本番 DB** | 既存信号 2 件・機器 1 件は無傷 |

#### Phase 3 の設計上の決定

- **`GET /api/devices/{id}/status` は、このプロジェクト唯一の `try/except` を持つルータ。**
  「ルータに try/except を書かない」ルールへの**意図的な例外**として `devices.py` に明記してある。
  理由: このエンドポイントの成果物は「到達できたか」そのものなので、到達不可は API の失敗では
  なく**テストの正常な結果**。502 にするとフロントが「疎通確認という操作が失敗した」のか
  「機器に到達できなかった」のかを区別できない。**捕まえるのは `Esp32Error` だけ**で、
  機器 id が無い場合の `NotFound` は素通りさせて 404 にする（握り潰しの範囲を最小に保つ）。
- **host は repository でも `normalize_host()` を通して保存する。** API 経由なら Pydantic が
  正規化するが、Phase 4 のスケジューラや `tools/` から直接呼ぶと素通りする。そして
  `esp32._states` の**ロック登録簿は host 文字列がキー**なので、`192.168.1.4` と
  `http://192.168.1.4/` が別エントリになると同一機器への直列化（バグ C の修正）が静かに壊れる。
- **機器の登録・更新時に疎通確認はしない。** ESP の電源が入っていなくても先に登録できるべき
  （Phase 6 で静的 IP を焼く前にサーバ側の設定を用意する運用になる）。
- host を変更すると `esp32._states` に旧 host のエントリが残る。ロック 1 個分なので実害なし。
  掃除は入れていない（掃除中に旧 host へ進行中の送信があるとロックを取り違えるため）。
- `test_root_does_not_serve_legacy_ui` はフェーズ番号ではなく **「HTML を返していないこと」** を
  見る形に変えた。番号を assert すると毎フェーズ書き換えるだけのテストになる。

### ⚠️ 現在は新旧が同居している

- **旧実装 `ir_remocon/ir_db_server.py` はまだ削除していない**（動く状態のまま残置）。
  最終的に Phase 5 完了後に削除する。それまでは参照用。
  新サーバと**同じポート 8102** なので同時起動はできない。
- **新サーバの `/` は JSON スタブを返す。** 旧 `templates/index.html` は配信していない。
  旧 UI は送信時に `esp32_ip` を送るが新 API は `device_id` 参照なので、配信すると
  「押しても効かない画面」になるため。旧 UI を触りたいときは旧モノリスを起動する。
- **新サーバに予約機能はまだ存在しない**（スケジューラは Phase 4）。
  新アプリは `jobs.db` を一切開かない。既存 7 件は既に死んでいる（不具合 D）ので実害なし。
- 検証で host を変えたいときは `IR_DB_PATH` で別 DB を作り、
  `PUT /api/devices/{id}` で変更する（Phase 3 で API 化済み。DB 直書きは不要）。

---

## コマンド

```powershell
uv sync                                   # 依存を .venv に同期（初回/依存変更時）
uv run ir-remocon                         # サーバ起動（どのディレクトリからでも可）
uv run python -m ir_remocon.app.main      # 同上（別の起動方法）
uv run pytest -v                          # 単体テスト（統合は既定でスキップ）
uv run pytest -m integration -v           # 統合テスト（スタブを実ソケットで起動）
uv run python tools/fake_esp32.py --port 8080   # ESP32 スタブ（検証用）
uv add <pkg>                              # 依存追加（pip は使わない）
```

`tools/fake_esp32.py` の主なフラグ（`--help` に全部ある）:

| フラグ | 何を試せるか |
|---|---|
| `--send-duration 15` | 読み取りタイムアウト（504・結果不明）を再現 |
| `--send-status 202` | Phase 6 のファーム（キュー投入後に即応答）を先取り検証 |
| `--fail-mode busy\|error\|bad-request\|hang\|drop` | 各種失敗。`--fail-rate 0.3` で確率的にも |
| `--no-reject-concurrent` | ESP 側の 409 を無効化し、サーバ側の直列化だけを見る |
| `--callback-fail` | 学習コールバックを送らない（「学習が時々失敗する」の切り分け用） |

> 接続拒否（502）を試すときは、スタブを起動しないか別ポートを指すだけでよい。

スタブに向けて手動確認するときの定型（本番 DB を触らない）:

```powershell
# ターミナル A
uv run python tools/fake_esp32.py --port 8080
# ターミナル B
$env:IR_DB_PATH = "$env:TEMP\ir_scratch.db"; $env:IR_ADVERTISE_HOST = "127.0.0.1"; uv run ir-remocon
# ターミナル C: 機器の host をスタブに向ける（http:// も末尾 / も正規化される）
curl.exe -s -X PUT "http://127.0.0.1:8102/api/devices/1" -H "Content-Type: application/json" -d '{\"host\":\"http://127.0.0.1:8080/\"}'
```

> PowerShell 5.1 の `Invoke-RestMethod` はエラー応答の本文を読めない。**4xx/5xx の
> `detail` を確認したいときは `curl.exe -s -w "\nHTTP %{http_code}\n"` を使うこと。**
> 日本語のログを読むときは `Get-Content -Encoding UTF8`（既定の ANSI だと文字化けする）。

デプロイ先（Linux）:
```bash
cd ~/python_works/ir_remocon && uv sync --frozen && uv run python -m ir_remocon.app.main
```

### 環境変数（`config.py` 参照）

| 変数 | 既定 | 用途 |
|---|---|---|
| `IR_ADVERTISE_HOST` | LAN IP を自動検出 | ESP32 に渡す学習コールバック URL のホスト。**本番機では `192.168.1.110` を明示すること** |
| `IR_PORT` | `8102` | サーバのポート |
| `IR_DB_PATH` / `IR_JOBS_DB_PATH` / `IR_LOG_PATH` | `ir_remocon/` 配下 | 各ファイルの絶対パス |
| `IR_MIN_SEND_INTERVAL` | `0.3` | 同一機器への送信間隔の下限（連打対策） |
| `IR_ESP32_LOCK_TIMEOUT` | `5.0` | 同一機器のロック待ちの上限。超えたら 409 を返す |
| `IR_ESP32_CONNECT_TIMEOUT` / `IR_ESP32_READ_TIMEOUT` | `2.0` / `10.0` | ESP への接続 / 応答待ち |
| `IR_LEARN_TIMEOUT` | `15` | 学習の待ち受け秒数（ESP 側と揃える） |
| `IR_MAX_ALARM_DURATION` | `1800` | 目覚ましの最大継続秒数 |

---

## 環境・ネットワーク実情報

| 対象 | 値 |
|---|---|
| 本番サーバ | `uncre-switch`（Linux, Python 3.10）。**LAN: `192.168.1.110`（固定）** / Tailscale: `100.95.100.1` |
| 本番の配置先 | `/home/uncre/python_works/ir_remocon/` |
| 開発機（このリポジトリ） | Windows 11。LAN: `192.168.1.100` |
| ESP32 | 直近の稼働 IP は `192.168.1.4`（DHCP）。**現在停止中**。Phase 6 で静的 IP 化する |
| スマホからの操作 | Tailscale 経由（`xiaomi-13t-pro` = `100.104.223.25`）。**外出先からフロントを開く使い方をしている** |

> **注意**: 外部アクセスが Tailscale 経由である以上、フロントは LAN 外からも開かれる。
> `IR_ADVERTISE_HOST` は「ESP32 から見たサーバのアドレス」なので、**必ず LAN 側の
> `192.168.1.110` にすること**（Tailscale IP にすると ESP32 から到達できない）。

---

## 確定済みの不具合（調査済み。再調査不要）

ログ・DB・コードを突き合わせて特定済み。詳細と証拠はプランファイル参照。

| ID | 症状 | 原因 | 対応フェーズ |
|---|---|---|---|
| A | 予約ボタンが**無反応**（体感バグの主犯） | 非表示 select に `required` が残り、HTML5 検証が submit を握り潰す。エラーも出ない | 5 |
| B | 送信失敗でも UI に「成功」と出る | `send_signal_to_esp32` が例外を握り潰して常に 200 | ✅ **2 で解消** |
| C | 連打すると全部タイムアウト | サーバ側に直列化なし + ESP 側が非同期ハンドラ内で `irsend.sendRaw()` を同期実行し TCP ごとブロック | ✅ **サーバ側は 2 で解消** / ESP 側は 6 |
| D | 予約が数ヶ月間 1 件も発火していない | `sqlite3.OperationalError: database or disk is full` で APScheduler スレッドが死亡。誰も検知できなかった | 4 |
| E | IP を変えると既存予約が全滅 | ジョブ引数に IP が pickle されている（古いジョブが今も `192.168.1.16` を叩いている） | ✅ **3 で解消**（host は DB の 1 行のみ。ジョブ引数の `device_id` 化は Phase 4 で実施） |
| F | 朝 9 時前だと予約日付が前日になる | `toISOString()`（UTC）で日付デフォルトを生成。時刻側は現地時刻で不整合 | 5 |
| G | 目覚まし中に他の予約が取りこぼされる | `time.sleep` でワーカースレッドを占有。中断手段も無い | 4 |
| H | その他（naive/aware 比較で予約一覧が 500、リネーム衝突で 500、CWD 依存、XSS、`[object Object]` 表示、ESP のボディ分割未対応、JSON バッファ溢れ 等） | — | 各所 |

### 未解決の疑問（次フェーズで切り分ける）

- **学習（信号の受信）が時々失敗する原因は未特定。**
  当初「`detect_lan_ip()` が Tailscale IP を誤検出してコールバックが届かない」と推測したが、
  `100.104.223.25` はスマホのアドレスだと判明したため**この説は根拠を失った**。
  `uncre-switch` は exit node を offer しているだけで使用はしていないので、自動検出は
  正しく `192.168.1.110` を返すと思われる。Phase 5 で設定画面に実際のコールバック URL を
  表示し、Phase 7 の `fake_esp32.py` と併せて切り分ける。
- 旧ログの `database or disk is full` の真因（本当にディスク満杯だったのか、
  DB 破損や一時ディレクトリの問題だったのか）は未確認。

### Phase 2 で新たに判明したこと

- **httpx の keep-alive を無効化した**（`limits=httpx.Limits(max_keepalive_connections=0)`）。
  ESPAsyncWebServer は接続を積極的に閉じるため、プールに残った idle 接続を再利用すると
  `RemoteProtocolError: Server disconnected` が出る。これを 502 にすると「1 バイトも送って
  いないのに送信失敗」と報告することになり、**「たまに失敗する」の新種を自分で作る**ことに
  なっていた。旧実装は毎回 `httpx.Client()` を作っていたので偶然この問題を踏んでいない。
  代償は LAN 内で TCP ハンドシェイク 1 往復（〜1ms）だけ。どのみち直列化しているので損失はない。
- **`extra="forbid"` が無いと静かな誤送信になる。** Pydantic v2 の既定 `extra="ignore"` では
  旧フロントの `{"esp32_ip": "192.168.1.99"}` が 422 にならず受理され、`device_id=None` と
  みなされて**既定機器に送られる**。「ユーザが指定した機器と実際の送信先が違うのに 200 が返る」
  という、まさに今回潰している類のバグを新規に作り込むところだった。
- **ロック待ちの 409 は正常動作。** 実測では 10 連打のうち 3 件が `Esp32LocalBusy` で 409 に
  なった（1 件あたり 0.5 秒 + 間隔 0.3 秒 × 10 件 > ロック待ち上限 5 秒）。これは失敗ではなく
  「飽和したので正直に断った」状態。**Phase 5 の UI はこれを「エラー」ではなく
  「機器がビジーです。少し待ってください」と描画すること。**
- **開発機の環境問題**: `C:\Users\UncrewedSloth\AppData\Local\Temp\pytest-of-UncrewedSloth`
  のアクセス権が壊れており、そのままだと `tmp_path` を使う全テストが `PermissionError`
  （WinError 5）で落ちる。**コード側の問題ではない。** 回避手順（Phase 3 で実証済み）:

  ```powershell
  New-Item -ItemType Directory -Force "$env:TEMP\pytest-ir" | Out-Null   # 先に作ること
  $env:PYTEST_DEBUG_TEMPROOT = "$env:TEMP\pytest-ir"
  uv run pytest -q
  ```

  ディレクトリを作らずに `PYTEST_DEBUG_TEMPROOT` だけ設定すると、今度は全テストが
  `FileNotFoundError`（WinError 3）になる。pytest は temproot を自動生成しない。

---

## 設計方針（Phase 2 以降で守るもの）

- **ESP32 通信は同期実装で統一する。** FastAPI の `def` エンドポイントはスレッドプールで
  動くのでブロックして問題なく、APScheduler のワーカースレッドとも同じコードを共有できる。
  async/sync の二重実装を避けるための意図的な選択。
- **失敗を握り潰さない。** ESP への送信失敗は型付き例外（`Esp32Unreachable` /
  `Esp32Busy` / `Esp32Error`）にしてルータ層で 502/409 にマップする。
  「UI に嘘の成功を出さない」ことが今回のリファクタの中心。
- **ジョブに IP を焼き付けない。** ジョブ引数は `device_id`。発火時に DB から host を解決する。
- **APScheduler は 3.x に固定**（`>=3.10,<4`）。4.x は API が別物。
  `trigger.fields[5]` のようなインデックス直参照はせず、
  **ジョブ作成時にメタ情報を `job.kwargs` に保存して読み出す**こと。
  （検証済み: 3.11.3 では `job.next_run_time` が正しく、`next_run` は存在しない）
- **フロントはビルド不要の素の JS を維持**。`templates/index.html` は骨格のみにし、
  `static/style.css` と `static/app.js` に分離する。

### 次フェーズへの具体的な申し送り

#### Phase 4（スケジューラ）— Phase 3 から

- **ジョブ引数は `[signal_name, device_id]` / `[on, off, interval, duration, device_id]`。**
  実行関数は**発火のたびに `repository.resolve_device(device_id)` を呼ぶ**こと。
  host を引数に入れた瞬間に不具合 E が復活する（旧実装がまさにそれで、古いジョブが
  今も `192.168.1.16` を叩き続けている）。`device_id=None` なら既定機器に解決される。
- **参照先の機器が削除済みだとジョブ実行時に `repository.NotFound` が飛ぶ。**
  Phase 3 で「最後の 1 台は削除禁止」を入れたので送信先が消えることはないが、
  ジョブが名指ししている機器が消える経路は残る。`EVENT_JOB_ERROR` リスナと
  `/api/health` の `last_job_error` で**必ず可視化する**こと。黙って失敗させると不具合 D の再来。
- `/api/health` に機器台数と既定機器名を含めるか検討（設定ミスの自己診断に効く）。
- 目覚ましアラームは `esp32.send_raw(..., lock_timeout=短い値)` を渡して、
  ロックが取れなければ 1 拍スキップさせること（待ちを積み上げない）。この引数は既に用意してある。
  また `repository.upsert_signal()` も実装済みなので Phase 5 のコールバックで使える。
#### Phase 5（フロントエンド）

- **設定タブは `/api/devices` を使う。** IP 直書き入力は廃止。機器のプルダウン + 編集 +
  「接続テスト」ボタン（`GET /api/devices/{id}/status`）。
  - **接続テストは失敗しても 200 が返る。** `reachable` の真偽で描き分けること
    （HTTP ステータスで判定すると常に「成功」になる）。`host` も返しているので
    「どのアドレスを叩いたか」を画面に出せる — 設定ミスにユーザが自力で気づける。
  - 既定を外す PUT / 最後の 1 台の削除は **409 `ConstraintViolation`** で返る。
    `detail` に日本語の理由と次の操作が入っているので、そのまま出せばよい。
  - 既定機器を削除すると `new_default_device_id` が返る。**送信先が変わったことを
    ユーザに見せること**（黙って変わったように見せない）。
- **UI 表示のルール**:
  - 409 → 「エラー」ではなく「機器がビジーです。少し待ってください」
  - 504（`outcome_unknown: true`）→ 「失敗しました」ではなく**「送信できたか不明です」**
  - エラーレスポンスは `{"detail", "error", "host", "outcome_unknown"}` の形。`detail` は
    FastAPI の `HTTPException` と同じキーなので、422 の配列形式だけ別処理すればよい。
  - 学習中は送信ボタンを無効化すること。ESP は `currentMode` 1 本の状態機械なので、
    受信モード中の送信は必ず 409 になる。
#### Phase 6（ESP32 ファーム）

- **決めること**: 202 が証明するのは「キューに入れた」ことだけで、赤外線が出たことでは
  ない（現在の 200 も「ハンドラが走った」までしか証明しない）。UI の言い回しを弱めるか、
  `/status` の `send_count` の増分をポーリングして実発射を確認するかを選ぶ。
  `esp32.send_raw()` の docstring にもこの限界を明記してある。
- `IR_ESP32_READ_TIMEOUT=10.0` は長い可能性がある（ロックを保持したまま消費される）。
  ただし `IR_ESP32_LOCK_TIMEOUT=5.0` があるので待ちは非有界にならない。**実機が戻ったら
  実測して見直すこと。** Phase 6 で 202 即応答になれば 2 秒程度で足りるはず。

---

## 引き継ぎメモの更新義務

**フェーズを 1 つ終えたら、このファイルの「進行状況」表と該当セクションを必ず更新すること。**
次のチャットはこのファイルとプランファイルしか手がかりが無い。
新しく判明した事実（特に「未解決の疑問」の解消や、推測が外れたこと）も必ず書き残す。

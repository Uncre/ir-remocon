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
| 4 | スケジューラ堅牢化（health / 中断可能アラーム / メタ情報） | ✅ **完了・検証済み** (2026-08-19) |
| 5 | フロントエンド（タブUI・static分割・体感バグ修正）**+ 学習 API** | ✅ **完了・検証済み** (2026-08-19) |
| 6 | ESP32 ファーム（`esp/ir_remocon.ino`）※書き込みは後日 | ⬜ 未着手 ← **次はここ** |
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

### Phase 4 で完成したもの

計画の全文: `C:\Users\UncrewedSloth\.claude\plans\phase4-refactored-widget.md`

| ファイル | 役割 |
|---|---|
| `ir_remocon/app/scheduler.py` | **本フェーズの中心。** 起動/停止・`build_trigger()`・ジョブ登録/削除/一覧・イベントリスナ・ハートビート・health・専用例外 |
| `ir_remocon/app/jobs.py` | 発火時に走る関数と実行中アラームの登録簿。**改名禁止**（下記） |
| `ir_remocon/app/routers/schedules.py` | 予約 CRUD + 実行中アラームの参照/停止 |
| `ir_remocon/app/routers/health.py` | `GET /api/health` |
| `tests/test_scheduler.py` / `test_jobs.py` / `test_schedules_api.py` | 62 件追加。単体は計 **172 件** / 統合 8 件 |

追加 API: `POST /api/schedule` / `POST /api/schedule/wakeup`（201）、`GET /api/schedules`、
`DELETE /api/schedules/{job_id}`、`GET /api/alarms`、`DELETE /api/alarms/{run_id}`、`GET /api/health`。
**スキーマ変更なし**（`jobs.db` は APScheduler が管理）。

実測で確認済みの事実（スタブ使用、実機不要）:

| 確認 | 結果 |
|---|---|
| **実際に発火するか** | `once` 予約が **秒単位で正確に発火**（23:00:05 予約 → 23:00:05 実行）。発火後は一覧から自動で消える |
| **[E] 完全解消** | 機器 host が `:8081` のときに作った予約 → 発火前に `:8080` へ変更 → **`:8080` に届いた**（`:8081` は 0 件のまま）。旧実装なら `:8081` に飛んでいた |
| **[G] アラーム中断** | 120 秒の目覚ましを開始 14 秒後に `DELETE /api/alarms/{run_id}` → **即停止**（3 秒間隔で 5 発だけ送信して終了） |
| **アラーム失敗時** | 死んだポート宛で `(1/5)`〜`(5/5)` を WARNING に出したのち `AlarmAborted` で中止 → **`/api/health` の `last_job_error` に出る** |
| **[D] 死の検知** | ハートビートを古くすると `scheduler_running:false` / `ok:false`。`scheduler.running` は **True のまま**（＝これだけでは検知できないことを実証） |
| **旧 7 件の破棄** | 起動前に 7 件の id を INFO で棚卸し → APScheduler が **ERROR 7 行**を残して削除 → `jobs.db` 0 件。黙って消えてはいない |
| **本番 DB** | 信号 2 件・機器 1 件は無傷。`/api/health` は `ok:true` |

#### Phase 4 の設計上の決定

- **`scheduler.running` を健全性の根拠にしてはいけない。** これは状態フラグを見ているだけで、
  メインループのスレッドが例外で死んでも `True` を返し続ける（実測で確認: `_thread` を
  潰しても `running` は `True`）。**まさに不具合 D の状況**なので、60 秒間隔の内部ジョブ
  （ハートビート）を走らせ、**180 秒以上古ければ死んでいるとみなす**。
  ハートビートは `MemoryJobStore` の `'internal'` ジョブストアに置いてあるので、
  `jobs.db` にも `/api/schedules` にも `job_count` にも出ない。
- **`/api/health` は不健全でも 200 を返す。** `ok` フィールドで表現する
  （`GET /api/devices/{id}/status` と同じ理由）。
- **`ok` と `last_job_error` は別物。** `ok` は「機構が動いているか」、`last_job_error` は
  「過去に失敗があったか」。ジョブが 1 回失敗しても `ok:true` のままになる。
  UI では**別の見せ方**にすること（赤バナー vs 警告）。
- **ジョブ引数は kwargs のみ、host は絶対に入れない。** `{"signal_name", "device_id", "meta"}`。
  `device_id=None` は「既定機器」の意味で **None のまま保存する**（既定を切り替えると追従する）。
- **表示用の説明文は作成時に焼いて `meta["description"]` に入れる。** 旧実装の
  `trigger.fields[5]` のようなインデックス直参照は廃止した（バージョン差で壊れるため）。
- **ジョブ一覧のソートキーは `(next_run is None, next_run)`。** None 同士はタプルの前半で
  等しくなり後半の比較に進まないので、`None` と datetime を比較する経路が存在しない（不具合 H）。
- **過去日時の `once` 予約は 400 で断る。** misfire 次第で即発火するか黙って捨てられるかが
  変わり、どちらもユーザには「予約したのに動かなかった」に見える（不具合 F の受け皿）。
- **目覚ましの送信は `lock_timeout=min(interval, 1.0)`。** ロックが取れなければ 1 拍
  スキップして待ちを積み上げない。
- **アラームの登録簿はプロセスメモリ。** サーバ再起動で消える（＝鳴り止む）。これは許容。
- 終了時は **先にアラームへ停止を通知してから** `scheduler.shutdown(wait=True)`。
  逆にすると Ctrl-C が最大 30 分効かないサーバになる。
- テストは `config.LOG_PATH` も一時ファイルへ逃がすようにした。`TestClient` は lifespan 経由で
  `setup_logging()` を呼ぶため、放置すると**テストの実行記録が `ir_db_server.log` に混ざる**。
  あのログは不具合 D のような事象を追うための証拠なので汚さない。

#### ⛔ 改名禁止（最重要の申し送り）

APScheduler は関数を **`ir_remocon.app.jobs:run_signal_job`** という文字列で pickle する。

- `ir_remocon/app/jobs.py` のモジュール名
- `run_signal_job` / `run_wakeup_alarm` の関数名

これらを改名・移動すると `jobs.db` の既存の予約は復元不能になり、**起動時に
APScheduler が削除する**（今回まさにこれで旧 `__main__:execute_wakeup_alarm` の
7 件が消えた）。変更するなら移行スクリプトを併せて用意すること。
kwargs のキー名（`signal_name` など）も同様に pickle 済みなので、消すと発火時に `TypeError`。

### Phase 5 で完成したもの

計画の全文: `C:\Users\UncrewedSloth\.claude\plans\phase5-logical-crescent.md`

**当初「フロントエンドだけ」の予定だったが、調査で新サーバに学習（受信）API が
1 本も無いことが判明したため、バックエンドの追加を含む**（`models.py` の
`LearnStartRequest` 等・`esp32.start_receive()`・`repository.upsert_signal()` は
Phase 1〜2 で用意済みだったが、繋ぐルータが未作成だった）。

| ファイル | 役割 |
|---|---|
| `ir_remocon/app/learn.py` | **学習セッションの登録簿。** トークン発行・遅延タイムアウト判定・ESP への受信モード切替まで担当 |
| `ir_remocon/app/routers/learn.py` | 学習 API 4 本。`try/except` なし |
| `ir_remocon/templates/index.html` | 骨格のみ（178 行）。**Jinja2 変数は 0、`required` も 0** |
| `ir_remocon/static/style.css` | 362 行。ライト/ダーク両対応、モバイルはタブを下端固定 |
| `ir_remocon/static/js/*.js` | **13 モジュール**（ES modules）。合計 約 1,200 行 |
| `ir_remocon/static/favicon.ico` | `templates/` から移動（旧位置ではどのルートからも配信されず常に 404 だった） |
| `tests/test_learn.py` / `test_frontend_assets.py` | 28 件 + 27 件。単体は計 **226 件** / 統合 8 件 |

追加 API: `POST /api/learn`（201）、`GET /api/learn`、`GET /api/learn/{token}`、
`POST /api/callback/ir_signal/{token}`。`HealthOut` に **`callback_base_url`** を追加。
`repository.signal_exists()` を追加。**スキーマ変更なし。**

JS のモジュール構成（`static/js/`）:

| 基盤 | 役割 |
|---|---|
| `api.js` | fetch ラッパ・`ApiError`・**エラー整形**・多重送信ガード（`guard()`）・`seg()` |
| `dom.js` | `el()` / `render()` / `fillSelect()` / **`toggleFieldset()`**。`innerHTML` を使わない |
| `format.js` | **現地時刻の日付/時刻生成**。`toISOString()` を使わない |
| `toast.js` | success / error / **warn** / info の 4 種 |
| `state.js` | 送信先 device・学習中フラグ・購読機構 |
| `data.js` | 信号/機器の共有読み込み（`app.js` に置くと循環参照になる） |
| `poll.js` | `visibilityState` 連動のポーリング |
| `health.js` | 健全性バナー |
| `tab-remote/learn/schedules/settings.js` | 各タブ。`app.js` がエントリ |

実測で確認済みの事実（スタブ使用、実機不要）:

| 確認 | 結果 |
|---|---|
| **学習フロー（実ソケット）** | `POST /api/learn` → スタブが 3 秒後にコールバック → **`status:"success"` / `raw_length:38`**。信号名が **`リビング照明 ON`（日本語＋スペース）でも通る**（旧実装は名前を URL に埋めていたので壊れていた） |
| **学習タイムアウト** | `--callback-fail` で 15 秒後に **`status:"timeout"`**。信号は作られない |
| **[G'] 学習中の送信** | ESP が受信モードなので **409 `Esp32DeviceBusy`**（＝UI で送信ボタンを無効化する根拠が実測で裏付いた） |
| **学習の二重開始** | **409 `LearnAlreadyRunning`**。`detail` に競合相手の信号名が入る |
| **遅延コールバック** | 期限切れ後に届いたコールバックは **404 で破棄**し、WARNING に `期限超過=1.4秒 受信=3 要素` を記録 |
| **UI の配信** | `/` が `text/html`、`/static/style.css`・`/static/js/*.js`・`/favicon.ico` すべて 200 |
| **JS の煙試験** | Node で最小 DOM スタブを与えて `app.js` を読み込み、**リンクと初期化が例外なく完了**。信号名 `<img src=x onerror=alert(1)>` が `textContent` に入ることを実地確認 |
| **エラー整形** | 実レスポンスを `ApiError` に通して確認（下表） |
| **本番 DB** | 信号 2 件・機器 1 件は無傷 |

エラーが画面にどう出るか（実レスポンスで確認済み）:

| 応答 | 表示 |
|---|---|
| 422（旧 `esp32_ip`） | ERROR「入力を確認してください — `esp32_ip: Extra inputs are not permitted`」**`[object Object]` は出ない** |
| 422（複数） | `name: Field required / execute_time: Field required` |
| 409 `Esp32LocalBusy` / `Esp32DeviceBusy` | **WARN**「機器がビジーです — 少し待ってからもう一度お試しください。」 |
| 504 `outcome_unknown` | **WARN**「送信できたか不明です — …赤外線が出たかどうかは分かりません。家電の状態を目で確認してください。」 |
| 502 `Esp32Unreachable` | ERROR「機器に接続できません — …（設定タブの「接続テスト」で確認できます）」 |
| 409 `ConstraintViolation` / 400 `InvalidSchedule` | ERROR。サーバの日本語 `detail` をそのまま表示 |

#### Phase 5 の設計上の決定

- **学習コールバックの URL は信号名ではなくトークンを入れる。**
  ファームは `callback_url` をサーバから受け取ってそのまま `http.begin()` する
  （`esp/temp.ino:159,242` で確認）ので、パス設計はサーバの完全な自由。
  旧 `/api/callback/ir_signal/{name}` には 2 つ問題があった —
  日本語やスラッシュを含む名前で URL が壊れること、そして
  **誰でも任意の信号を上書きできた**こと。トークン制なら pending セッション以外は 404。
- **学習のタイムアウトは遅延評価。監視スレッドを立てない。** 現ファームは受信
  タイムアウト時に何も通知せず黙って idle に戻るので、サーバは自分の時計しか
  根拠を持たない。参照時に `expires_at` と比べれば足りる。
- **⛔ 期限切れ後に届いたコールバックの WARNING ログを消さないこと。**
  「学習が時々失敗する」の原因が**「コールバックが届いていない」のか
  「遅れて届いている」のか**を区別できる唯一の証拠。`learn.py` の `complete()` にある。
- **学習は ESP を叩く前に同名チェックを行う。** 15 秒待たせた末に
  「その名前は既にあります」と言わないため。`overwrite` 未指定なら 409。
- **開始に失敗した学習セッションは必ず取り消す。** 残すとその機器で二度と
  学習を始められなくなる（`LearnAlreadyRunning` が出続ける）。テストで固定してある。
- **ESP が報告する `freq` は保存しない。** `ir_signals` に列が無く、送信は常に
  `config.DEFAULT_FREQ_KHZ`。ただし**既定と違う値が来たら WARNING に残す** —
  黙って捨てると「学習はできたのに送信しても効かない」という切り分けの難しい
  症状になる。列の追加は Phase 7 の判断。
- **不具合 A の対策は 3 段構え。** ①`required` を 1 つも書かない ②非表示の
  `<fieldset>` は `hidden` と `disabled` を**必ず一緒に**動かす（`dom.js` の
  `toggleFieldset()`）③`<form novalidate>` でブラウザの制約検証自体を止める。
  検証は JS に一本化した。
- **`app.js` 1 本ではなく 13 モジュールに分割した**（元プランからの変更）。
  4 タブ + バナー + アラーム + 学習ポーリングで 1,200 行規模になるため。
  `type="module"` をブラウザがそのまま読む。**バンドラもトランスパイラも入れない。**
- **`poll.js` は失敗しても淡々と再実行する。打ち切りは呼び出し側の責任。**
  学習の 1 秒ポーリングだけは自分で止めないと無限リトライになる（実装中に踏んだ）。
  起きる条件は「学習中にサーバが再起動してセッションが消えた」場合 — 登録簿は
  プロセスメモリなので 404 が返り続ける。`tab-learn.js` は 404 なら即中止、
  それ以外は 5 回まで見送ってから中止する。**新しいポーリングを足すときは同じ
  検討をすること**（間隔が長いものは poll.js 任せでよい）。
- **ポーリングは `visibilityState === 'visible'` のときだけ動かす。**
  スマホから Tailscale 経由で開く使い方なので、バックグラウンドでモバイル回線と
  バッテリーを黙って消費させない。health 30 秒 / 予約 60 秒 /
  アラーム 30 秒（鳴っている間は 5 秒）/ 学習 1 秒。
- **`ir_db_server.log` というログ名はそのまま**にした。旧モノリスは消えたが、
  このログには不具合 D の証拠（`database or disk is full`）が入っている。
  改名すると既存ログが孤立する。

#### ⛔ フロントを触るときの回帰ガード

`tests/test_frontend_assets.py`（27 件）が以下を**機械的に**見張っている。
新しい JS を足すときに引っかかったら、抑制せず書き方を直すこと。

| ガード | 理由 |
|---|---|
| `index.html` に `required` が無い | 不具合 A の再発（非表示の必須項目が submit を握り潰す） |
| すべての `<form>` が `novalidate` | 同上（二重の防御） |
| `hidden` な `<fieldset>` は `disabled` も持つ | 同上 |
| JS に `toISOString(` が無い | 不具合 F の再発（UTC で日付を作ると朝 9 時前に前日になる） |
| JS に `innerHTML` が無い | XSS（信号名はユーザが自由に命名できる） |
| JS に `esp32_ip` が無い | 旧 API 契約の残骸（送ると 422） |
| `/api/...` のテンプレート文字列は `seg()` を通る | 名前に `/` `#` が入ると URL が壊れる |
| 相対 import の解決先が実在する | バンドラが無いので**画面が真っ白になるだけ**で気づけない |
| 名前付き import が実際に export されている | 同上（ESM はリンク時に落ちる） |
| `$('#id')` の id が HTML に実在する | 綴り違いは `null` になり「押しても何も起きない」になる |
| `/static/...` の参照先が実在する | 同上 |

> 検査はコメントを除いたコードだけを対象にしている（禁止した理由をコメントに
> 書けるようにするため）。`_strip_js_comments()` を参照。

### ⚠️ 旧実装は削除済み

- **`ir_remocon/ir_db_server.py` は削除した**（Phase 5）。旧 UI への退路は
  **git 履歴と `backup_before_refactor/index.html` のみ**。
- **`ir_remocon/ir_request_test.py` は残してある。** ESP32 に直接 POST する
  実機用スクリプトで「旧 API 前提」ではないため削除対象から外した。ただし
  死んだ IP `192.168.1.16`（不具合 E の元凶そのもの）を直書きしており、
  `requests`（依存に無い）を import し、トップレベルで即実行される。
  **信号 2 件の raw データの平文コピーでもある。** 扱いは Phase 7 で判断すること。
- **予約機能は Phase 4 で新サーバに載った。** 旧 `jobs.db` の 7 件はバックアップのうえ
  破棄済み（`backup_before_refactor/jobs.phase4-20260818-213446.db`）。現在 `jobs.db` は空。
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
uv run pytest tests/test_frontend_assets.py -q   # フロントの回帰ガードだけ（1 秒）
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
# ターミナル B（jobs.db とログも本番から離すこと。予約を試すと本番 jobs.db が書き換わる）
$env:IR_DB_PATH = "$env:TEMP\ir_scratch.db"; $env:IR_JOBS_DB_PATH = "$env:TEMP\ir_scratch_jobs.db"
$env:IR_LOG_PATH = "$env:TEMP\ir_scratch.log"; $env:IR_ADVERTISE_HOST = "127.0.0.1"; uv run ir-remocon
# ターミナル C: 機器の host をスタブに向ける（http:// も末尾 / も正規化される）
curl.exe -s -X PUT "http://127.0.0.1:8102/api/devices/1" -H "Content-Type: application/json" -d '{\"host\":\"http://127.0.0.1:8080/\"}'
# ブラウザで http://127.0.0.1:8102 を開けば UI が出る（Phase 5 以降）
```

> **PowerShell の落とし穴 2 つ**（Phase 5 で踏んだ）:
> - `curl.exe -o $null` は `$null` が空文字に展開されて**次の引数を飲み込む**。
>   出力を捨てるなら `-o NUL` を使うこと。
> - `-d '{"name":"日本語"}'` は引数のエンコードで壊れる。日本語を含むボディは
>   BOM 無し UTF-8 でファイルに書き、`--data-binary "@file"` で渡す:
>   `[System.IO.File]::WriteAllText($p, $json, (New-Object System.Text.UTF8Encoding($false)))`

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
| `IR_MAX_ALARM_DURATION` | `1800` | 目覚ましの最大継続秒数。**超過は 422 で断る**（黙って切り詰めない） |
| `IR_TIMEZONE` | `Asia/Tokyo` | スケジューラのタイムゾーン |
| `IR_SCHEDULER_WORKERS` | `4` | ジョブの同時実行数。目覚まし 1 本が 1 スレッドを占有する点に注意 |
| `IR_MISFIRE_GRACE_TIME` | `300` | 発火予定を過ぎたジョブを何秒まで許容して実行するか |

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
| A | 予約ボタンが**無反応**（体感バグの主犯） | 非表示 select に `required` が残り、HTML5 検証が submit を握り潰す。エラーも出ない | ✅ **5 で解消**（`required` 廃止 + `fieldset.disabled` + `novalidate` の 3 段構え。回帰ガードあり） |
| B | 送信失敗でも UI に「成功」と出る | `send_signal_to_esp32` が例外を握り潰して常に 200 | ✅ **2 で解消** |
| C | 連打すると全部タイムアウト | サーバ側に直列化なし + ESP 側が非同期ハンドラ内で `irsend.sendRaw()` を同期実行し TCP ごとブロック | ✅ **サーバ側は 2 で解消** / ESP 側は 6 |
| D | 予約が数ヶ月間 1 件も発火していない | `sqlite3.OperationalError: database or disk is full` で APScheduler スレッドが死亡。誰も検知できなかった | ✅ **4 で解消**（ハートビート + `EVENT_JOB_ERROR/MISSED` リスナ + `/api/health`。バナー表示は 5） |
| E | IP を変えると既存予約が全滅 | ジョブ引数に IP が pickle されている（古いジョブが今も `192.168.1.16` を叩いている） | ✅ **完全解消**（3 で host を DB に集約 / 4 でジョブ引数を `device_id` 化。発火前の host 変更が効くことを実測） |
| F | 朝 9 時前だと予約日付が前日になる | `toISOString()`（UTC）で日付デフォルトを生成。時刻側は現地時刻で不整合 | ✅ **5 で解消**（`format.js` の `localDateValue()`。回帰ガードあり。サーバ側は 4 で過去日時を 400 で断る受け皿も用意済み） |
| G | 目覚まし中に他の予約が取りこぼされる | `time.sleep` でワーカースレッドを占有。中断手段も無い | ✅ **4 で解消**（`Event.wait` + `DELETE /api/alarms/{run_id}` + 継続時間の上限） |
| H | その他（naive/aware 比較で予約一覧が 500、リネーム衝突で 500、CWD 依存、XSS、`[object Object]` 表示、ESP のボディ分割未対応、JSON バッファ溢れ 等） | — | 各所 |

### 未解決の疑問（次フェーズで切り分ける）

- **学習（信号の受信）が時々失敗する原因は未特定。ただし Phase 5 で切り分ける道具は揃った。**
  当初「`detect_lan_ip()` が Tailscale IP を誤検出してコールバックが届かない」と推測したが、
  `100.104.223.25` はスマホのアドレスだと判明したため**この説は根拠を失った**。
  `uncre-switch` は exit node を offer しているだけで使用はしていないので、自動検出は
  正しく `192.168.1.110` を返すと思われる。

  **Phase 5 で用意した切り分け材料**（次に実機で学習が失敗したら、この順で見る）:

  1. **設定タブの「学習コールバック」表示** — ESP に渡している URL がそのまま出る。
     ここが `192.168.1.110` 以外（Tailscale IP や `127.0.0.1`）なら原因はこれ。
     `IR_ADVERTISE_HOST=192.168.1.110` を明示すれば直る。
  2. **ログの `期限切れの学習セッションにコールバックが届いたため破棄しました`**
     （WARNING）— これが出ていれば**コールバックは届いている**。原因は
     「届かない」ではなく「遅い」。`IR_LEARN_TIMEOUT` を延ばす話になる。
     出ていなければ本当に届いていない（経路の問題）。
  3. **成功時の `raw_length`**（学習タブと `GET /api/learn/{token}` に出る）—
     ちょうど 1024 前後で頭打ちなら、ESP 側の `StaticJsonDocument<2048>` が
     溢れて**黙って切り詰めている**疑い（Phase 6 で対処）。
  4. **学習中の送信は 409 `Esp32DeviceBusy` になる**ことを実測済み。UI は学習中
     送信ボタンを無効化するので、「学習を始めたまま送信して失敗」は起きなくなった。
- 旧ログの `database or disk is full` の真因（本当にディスク満杯だったのか、
  DB 破損や一時ディレクトリの問題だったのか）は未確認。
  ただし **Phase 4 で「同じことが起きても気づける」状態にはなった**（ハートビートが
  止まれば `/api/health` の `ok` が落ちる）。真因の特定は本番機のディスク状況を
  見ないと進まないので、次に本番へデプロイするときに `df -h` を確認すること。
  ログ自体は Phase 1 でローテーション（5MB×3）を入れたので、ログ肥大が原因だったなら再発しない。

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
  （Phase 4 で実装済み。`ir_remocon/app/jobs.py` を参照）
- **APScheduler は 3.x に固定**（`>=3.10,<4`）。4.x は API が別物。
  `trigger.fields[5]` のようなインデックス直参照はせず、
  **ジョブ作成時にメタ情報を `job.kwargs` に保存して読み出す**こと。
  （検証済み: 3.11.3 では `job.next_run_time` が正しく、`next_run` は存在しない）
- **`ir_remocon.app.jobs` のモジュール名・関数名・kwargs のキー名は改名禁止。**
  APScheduler が文字列参照で pickle しており、変えると既存の予約が起動時に消える。
- **`scheduler.running` を「動いている」の根拠にしない。** ハートビートの鮮度を見る
  (`scheduler.is_running()`)。状態フラグはスレッドが死んでも True のままになる。
- **フロントはビルド不要の素の JS を維持**（Phase 5 で実施済み）。
  `templates/index.html` は骨格のみ、`static/style.css` と `static/js/*.js`（13 モジュール）。
  ES modules をブラウザがそのまま読む。**バンドラ・トランスパイラ・npm は入れない。**

### 次フェーズへの具体的な申し送り

#### Phase 5 は完了した（下は Phase 6 / 7 向け）

フロントの実装・設計判断・回帰ガードは「Phase 5 で完成したもの」の節を参照。
API 側の申し送りは Phase 3 / 4 の節に残してある内容がそのまま有効。

#### Phase 6（ESP32 ファーム）

- **決めること**: 202 が証明するのは「キューに入れた」ことだけで、赤外線が出たことでは
  ない（現在の 200 も「ハンドラが走った」までしか証明しない）。UI の言い回しを弱めるか、
  `/status` の `send_count` の増分をポーリングして実発射を確認するかを選ぶ。
  `esp32.send_raw()` の docstring にもこの限界を明記してある。
- `IR_ESP32_READ_TIMEOUT=10.0` は長い可能性がある（ロックを保持したまま消費される）。
  ただし `IR_ESP32_LOCK_TIMEOUT=5.0` があるので待ちは非有界にならない。**実機が戻ったら
  実測して見直すこと。** Phase 6 で 202 即応答になれば 2 秒程度で足りるはず。
- **`callback_url` はサーバから渡されたものをそのまま使い続けること。**
  Phase 5 でパスにトークンを入れる形にした（`/api/callback/ir_signal/{token}`）。
  ファーム側で URL を組み立て直したり、信号名を付け足したりしてはいけない。
- **受信バッファの溢れは必ず検知して報告すること。** 現在の
  `StaticJsonDocument<2048>` は 1024 要素を収容できず**黙って切り詰める**。
  サーバは受信要素数を `raw_length` として画面に出すようになったので、
  ファーム側が `doc.overflowed()` を見てエラーにすれば、切り詰めが
  「学習はできたが送信しても効かない」に化ける前に気づける。
- **`freq` はサーバが保存していない**（`ir_signals` に列が無く、送信は常に 38kHz）。
  既定と違う値を返すと WARNING が出るだけで、その周波数では送信されない。

#### Phase 7（周辺整備）

- **`jinja2` 依存が未使用になった。** 唯一の利用者だった `ir_db_server.py` を
  Phase 5 で削除した。新実装は `FileResponse` で HTML を返すだけ。
  `uv remove jinja2` は `uv.lock` の再生成を伴うので、他の整備とまとめて行うこと。
- **`ir_remocon/ir_request_test.py` の扱いを決めること。** 死んだ IP
  `192.168.1.16` を直書きし、依存に無い `requests` を import し、トップレベルで
  即実行される。ただし**信号 2 件の raw データの平文コピー**でもある
  （DB とそのバックアップにも入っているので、削除しても失われはしない）。
- `ir_db_server.log` というログ名は**意図的に据え置いた**。不具合 D の証拠が
  入っている既存ログと同じ名前を保つため。改名するなら既存ログの扱いも決めること。
- `tools/migrate_jobs.py` は未作成（Phase 4 で旧 7 件は手作業で退避済みなので、
  必要性は下がっている）。

---

## 引き継ぎメモの更新義務

**フェーズを 1 つ終えたら、このファイルの「進行状況」表と該当セクションを必ず更新すること。**
次のチャットはこのファイルとプランファイルしか手がかりが無い。
新しく判明した事実（特に「未解決の疑問」の解消や、推測が外れたこと）も必ず書き残す。

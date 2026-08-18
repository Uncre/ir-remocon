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
| 2 | ESP32 通信レイヤ: 送信の直列化・失敗を握り潰さない | ⬜ 未着手 ← **次はここ** |
| 3 | 機器登録と IP 管理（`esp32_ip` → `device_id`） | ⬜ 未着手 |
| 4 | スケジューラ堅牢化（health / 中断可能アラーム / メタ情報） | ⬜ 未着手 |
| 5 | フロントエンド（タブUI・static分割・体感バグ修正） | ⬜ 未着手 |
| 6 | ESP32 ファーム（`esp/ir_remocon.ino`）※書き込みは後日 | ⬜ 未着手 |
| 7 | 周辺整備（README / fake_esp32 / migrate_jobs / tests） | ⬜ 未着手 |

> `tools/fake_esp32.py`（ESP32 スタブ）は Phase 2 完了直後に作る予定。以降の検証はこれ無しでは回らない。

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

### ⚠️ 現在は新旧が同居している

- **旧実装 `ir_remocon/ir_db_server.py` はまだ削除していない**（動く状態のまま残置）。
  最終的に Phase 5 完了後に削除する。それまでは参照用。
- **`ir_remocon/app/main.py` はまだ存在しない。** `pyproject.toml` の
  `[project.scripts] ir-remocon = "ir_remocon.app.main:run"` は先行して書いてあるだけで、
  Phase 2〜4 のどこかで `main.py` を作るまで `uv run ir-remocon` は動かない。
- `ir_remocon/app/routers/` もまだ無い。

---

## コマンド

```powershell
uv sync                                   # 依存を .venv に同期（初回/依存変更時）
uv run python -m ir_remocon.app.main      # サーバ起動（main.py 作成後）
uv run pytest -v                          # テスト（tests/ 作成後）
uv add <pkg>                              # 依存追加（pip は使わない）
```

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
| B | 送信失敗でも UI に「成功」と出る | `send_signal_to_esp32` が例外を握り潰して常に 200 | 2 |
| C | 連打すると全部タイムアウト | サーバ側に直列化なし + ESP 側が非同期ハンドラ内で `irsend.sendRaw()` を同期実行し TCP ごとブロック | 2 / 6 |
| D | 予約が数ヶ月間 1 件も発火していない | `sqlite3.OperationalError: database or disk is full` で APScheduler スレッドが死亡。誰も検知できなかった | 4 |
| E | IP を変えると既存予約が全滅 | ジョブ引数に IP が pickle されている（古いジョブが今も `192.168.1.16` を叩いている） | 3 |
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

---

## 引き継ぎメモの更新義務

**フェーズを 1 つ終えたら、このファイルの「進行状況」表と該当セクションを必ず更新すること。**
次のチャットはこのファイルとプランファイルしか手がかりが無い。
新しく判明した事実（特に「未解決の疑問」の解消や、推測が外れたこと）も必ず書き残す。

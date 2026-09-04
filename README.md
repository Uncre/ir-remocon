# ir-remocon

ESP32 と FastAPI で動く、自宅用のスマート赤外線リモコンです。ブラウザから赤外線信号の
学習・送信、機器管理、通常予約、目覚まし予約を操作できます。

現在の firmware は **v2.2.0** です。受信モジュールの OUT は **GPIO13**、赤外線 LED は
GPIO4 に接続します。

> このアプリにはユーザー認証がありません。`0.0.0.0` で待ち受けるため、信頼できる LAN
> または Tailscale 内だけで使い、インターネットへ直接公開しないでください。

## 必要なもの

- [uv](https://docs.astral.sh/uv/)
- ESP32（実際の赤外線学習・送信を行う場合）
- firmware の書き込みには Arduino IDE、またはビルド確認用の PlatformIO

Python は `.python-version` に合わせて uv が管理します。`pip install`、手作業の venv、
`requirements.txt` は使いません。

## セットアップと起動

リポジトリ直下で依存をロックどおりに同期します。

```powershell
uv sync --frozen
```

学習時に ESP32 から到達できる、このサーバの LAN 側アドレスを指定して起動します。
Windows PowerShell の例です。

```powershell
$env:IR_ADVERTISE_HOST = "192.168.1.110"
uv run ir-remocon
```

Linux の例です。

```bash
export IR_ADVERTISE_HOST=192.168.1.110
uv run ir-remocon
```

ブラウザで `http://<サーバのアドレス>:8102` を開きます。初回起動では機器 `esp32`
（host `192.168.1.4`）が既定として作られるため、設定タブで実機のアドレスへ変更し、
「接続テスト」を実行してください。

別のカレントディレクトリから起動する場合も、editable install により次の形で動きます。

```powershell
uv run --project C:\path\to\ir-remocon ir-remocon
```

### 環境変数

| 変数 | 既定値 | 用途 |
|---|---:|---|
| `IR_HOST` | `0.0.0.0` | FastAPI の待受アドレス |
| `IR_PORT` | `8102` | FastAPI のポート |
| `IR_ADVERTISE_HOST` | LAN IP を自動推測 | ESP32 に渡す学習コールバックのホスト。本番では明示推奨 |
| `IR_DB_PATH` | `ir_remocon/ir_database.db` | 学習済み信号と機器設定の DB |
| `IR_JOBS_DB_PATH` | `ir_remocon/jobs.db` | APScheduler の予約 DB |
| `IR_LOG_PATH` | `ir_remocon/ir_db_server.log` | ローテーション対象のログ |
| `IR_LOG_LEVEL` | `INFO` | ログレベル |
| `IR_TIMEZONE` | `Asia/Tokyo` | 予約のタイムゾーン |
| `IR_ESP32_CONNECT_TIMEOUT` | `2.0` | ESP32 への接続タイムアウト（秒） |
| `IR_ESP32_READ_TIMEOUT` | `10.0` | ESP32 の応答待ちタイムアウト（秒） |
| `IR_MIN_SEND_INTERVAL` | `0.3` | 同一機器への最小送信間隔（秒） |
| `IR_ESP32_LOCK_TIMEOUT` | `5.0` | 同一機器の送信ロック待ち上限（秒） |
| `IR_DEFAULT_FREQ` | `38` | 赤外線の既定キャリア周波数（kHz） |
| `IR_LEARN_TIMEOUT` | `15` | 学習の待受時間（秒）。ESP32 側にも渡される |
| `IR_MAX_ALARM_DURATION` | `1800` | 目覚ましの最大継続時間（秒） |
| `IR_SCHEDULER_WORKERS` | `4` | 予約ジョブ用スレッド数 |
| `IR_MISFIRE_GRACE_TIME` | `300` | 遅れた予約を実行できる猶予（秒） |

`uvicorn` の worker は必ず **1** のままにしてください。送信ロックとスケジューラは
プロセスローカルで、worker を増やすと同じ ESP32 への直列化と予約の一意実行が壊れます。
`reload=True` も使用しません。

## データとバックアップ

永続データは次の2ファイルです。どちらも `.gitignore` の対象で、Git からは復元できません。

- `ir_remocon/ir_database.db`: 学習済み信号と機器設定
- `ir_remocon/jobs.db`: 予約

特に学習済み信号は実機なしでは再取得できません。DB を移行・交換する前はサーバを停止し、
両方をバックアップしてください。

```powershell
New-Item -ItemType Directory -Force backup_before_refactor | Out-Null
Copy-Item ir_remocon\ir_database.db backup_before_refactor\ir_database.db
Copy-Item ir_remocon\jobs.db backup_before_refactor\jobs.db
```

Linux では `cp` で同様に退避します。動作確認で予約を作る場合は、本番ファイルを使わず
`IR_DB_PATH`、`IR_JOBS_DB_PATH`、`IR_LOG_PATH` を一時パスへ向けてください。

### 旧 jobs DB の移行

Phase 4 より前の jobs DB は、古い関数名と ESP32 の IP を pickle 内に保持しているため、
現行アプリでは実行できません。最初に読み取り専用で棚卸しします。

```powershell
uv run python tools/migrate_jobs.py path\to\jobs.db
```

表示された予約を確認し、アプリを停止してから次を実行します。

```powershell
uv run python tools/migrate_jobs.py path\to\jobs.db `
  --apply --confirm-server-stopped `
  --output-dir backup_before_refactor
```

JSON レポートと復元可能な `.bak` を作成・検証した後で、元 DB の旧ジョブだけを削除します。
必要な予約は JSON を見ながら UI で再登録してください。ジョブが 0 件なら DB を書き換えません。

APScheduler の state は pickle です。棚卸しのため unpickle するので、自分で管理している
信頼できる jobs DB だけを入力してください。

## 実機なしでの動作確認

`tools/fake_esp32.py` は firmware の `/ir/send`、`/mode`、`/status` と学習コールバックを
再現します。

ターミナル A:

```powershell
uv run python tools/fake_esp32.py --port 8080 --async-send
```

ターミナル B。必ず一時 DB と一時ログを使います。

```powershell
$env:IR_DB_PATH = "$env:TEMP\ir-remocon-scratch.db"
$env:IR_JOBS_DB_PATH = "$env:TEMP\ir-remocon-scratch-jobs.db"
$env:IR_LOG_PATH = "$env:TEMP\ir-remocon-scratch.log"
$env:IR_ADVERTISE_HOST = "127.0.0.1"
uv run ir-remocon
```

ブラウザで `http://127.0.0.1:8102` を開き、設定タブの機器 host を
`127.0.0.1:8080` に変更します。送信・学習・予約・接続テストを実機なしで確認できます。

主な障害再現オプションは次のとおりです。

| オプション | 再現内容 |
|---|---|
| `--send-duration 15` | 読み取りタイムアウトと結果不明の応答 |
| `--async-send` | firmware v2.2.0 と同じ 202 即応答 |
| `--fail-mode busy` | 機器側の 409 ビジー |
| `--fail-mode error` | 機器側の 500 |
| `--fail-mode hang` | 応答しない機器 |
| `--callback-fail` | 学習コールバックが届かない状態 |

## テスト

Windows 開発機では pytest の既定一時ディレクトリを使わず、専用パスを先に作ります。

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\pytest-ir" | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = "$env:TEMP\pytest-ir"
uv run pytest -q
uv run pytest -m integration -v
uv run pytest tests/test_frontend_assets.py -q
```

既定のテストは本番 DB、jobs DB、ログを参照しない安全網を持っています。`integration`
マーカーのテストも ESP32 スタブをテスト内で起動し、実機には接続しません。

## ESP32 firmware v2.2.0

firmware は `esp/ir_remocon/ir_remocon.ino` です。Wi-Fi 認証情報は追跡しません。

### 回路の作成

必要な部品や、赤外線の受信・送信用回路の組み方は次の記事を参考にしてください。

- [ESP32で赤外線リモコンを作る方法（IRremote v4.x）](https://qiita.com/yhotta240/items/df0f2f92b5dff1d9410b)

記事中のスケッチは IRremote v4.x を使用していますが、本プロジェクトの firmware は
IRremoteESP8266 を使用しています。記事は回路・部品・配線の参考とし、書き込みには
このリポジトリの `ir_remocon.ino` を使用してください。本プロジェクトの配線は、受信が
GPIO13、送信が GPIO4 です。

```powershell
Set-Location esp\ir_remocon
Copy-Item secrets.h.example secrets.h
```

`secrets.h` に SSID とパスワードを設定します。`ir_remocon.ino` 冒頭では次も確認します。

- `IR_RECV_PIN = 13` が実配線の受信モジュール OUT と一致すること
- `IR_SEND_PIN = 4` が赤外線 LED の配線と一致すること
- `STATIC_IP` がルーターの DHCP 割当範囲外であること
- `GATEWAY`、`SUBNET`、`DNS1` が実際の LAN と一致すること

Arduino IDE では `esp/ir_remocon` フォルダごと開き、次のライブラリを入れます。

- ArduinoJson **v6 系**（v7 ではなく、PlatformIO と同じ `6.21.x`）
- IRremoteESP8266 `2.8.x`
- ESPAsyncWebServer `3.7.x` と、その依存 AsyncTCP

PlatformIO は実機なしのコンパイル確認用です。

```powershell
$env:PYTHONIOENCODING = "utf-8"
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run
```

書き込み後はシリアルモニタを 115200bps で開き、次の順に確認します。

1. `取得方法 : 静的 IP` と IP アドレスが表示される。
2. ブラウザで `http://<ESP32のIP>/status` を開き、`firmware_version` が `2.2.0`。
3. サーバの設定タブで同じ IP を host に設定し、接続テストが成功する。
4. 学習タブで実リモコンを学習し、直後に送信して家電が反応する。
5. 連続操作で 409 が頻発しない。頻発する場合は `IR_MIN_SEND_INTERVAL` を調整する。

`last_recv_overflow: true` は、受信したものの波形が長すぎて安全に保存できず破棄したことを
意味します。UI 上は学習タイムアウトに見えるため、学習失敗時は設定タブの接続テスト結果も
確認してください。

## 開発文書

リファクタリングの実装履歴、設計上の不変条件、各 Phase の計画は
[`docs/refactoring/README.md`](docs/refactoring/README.md) を起点に参照してください。

"""アプリケーション設定。

設定値はすべてここに集約し、環境変数で上書きできるようにする。
特に重要なのは以下の 2 点。

1. ファイルパスはすべて **絶対パス** で解決する。
   旧実装は ``DATABASE_FILE = "ir_database.db"`` のような相対パスだったため、
   起動時のカレントディレクトリが違うと空の DB が新規作成されてしまい、
   「登録した信号が消えた」ように見える事故が起きうる状態だった。

2. 学習コールバック用のホスト名 (:data:`ADVERTISE_HOST`) を明示指定できる。
   旧実装は 8.8.8.8 への UDP ソケットで自 IP を推測していたが、
   Tailscale や Docker などで NIC が複数あるとLAN 側と違うアドレスを返す。
   その URL を ESP32 に渡すと ESP から到達できず、学習が黙って失敗する。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

# ir_remocon/app/config.py -> ir_remocon/
BASE_DIR = Path(__file__).resolve().parent.parent

# --- ファイルパス -------------------------------------------------------------
#: 赤外線信号と機器登録を保存する SQLite DB
DB_PATH = Path(os.environ.get("IR_DB_PATH") or BASE_DIR / "ir_database.db")
#: APScheduler のジョブストア (SQLAlchemyJobStore が使う)
JOBS_DB_PATH = Path(os.environ.get("IR_JOBS_DB_PATH") or BASE_DIR / "jobs.db")
#: ログの出力先
LOG_PATH = Path(os.environ.get("IR_LOG_PATH") or BASE_DIR / "ir_db_server.log")

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# --- サーバ -------------------------------------------------------------------
HOST = os.environ.get("IR_HOST", "0.0.0.0")
PORT = int(os.environ.get("IR_PORT", "8102"))
LOG_LEVEL = os.environ.get("IR_LOG_LEVEL", "INFO").upper()
TIMEZONE = os.environ.get("IR_TIMEZONE", "Asia/Tokyo")

# --- ESP32 通信 ---------------------------------------------------------------
#: ESP32 への接続確立タイムアウト(秒)。LAN 内なので短くてよい。
ESP32_CONNECT_TIMEOUT = float(os.environ.get("IR_ESP32_CONNECT_TIMEOUT", "2.0"))
#: レスポンス待ちタイムアウト(秒)。赤外線送信の実処理時間を含むので長めに取る。
ESP32_READ_TIMEOUT = float(os.environ.get("IR_ESP32_READ_TIMEOUT", "10.0"))
#: 同一機器への送信間隔の下限(秒)。連打でESP32を詰まらせないためのスロットリング。
MIN_SEND_INTERVAL = float(os.environ.get("IR_MIN_SEND_INTERVAL", "0.3"))
#: 赤外線の既定キャリア周波数(kHz)
DEFAULT_FREQ_KHZ = int(os.environ.get("IR_DEFAULT_FREQ", "38"))

# --- 学習 (受信) --------------------------------------------------------------
#: 学習モードの待ち受け時間(秒)。ESP32 側のタイムアウトと揃える。
LEARN_TIMEOUT_SEC = int(os.environ.get("IR_LEARN_TIMEOUT", "15"))

# --- 目覚ましアラーム ---------------------------------------------------------
#: 1 回のアラームの最大継続時間(秒)。既定 30 分。
#: 無制限だとスケジューラのワーカースレッドを延々占有してしまうため上限を設ける。
MAX_ALARM_DURATION_SEC = int(os.environ.get("IR_MAX_ALARM_DURATION", "1800"))

# --- スケジューラ -------------------------------------------------------------
#: 同時に走らせるジョブ数。目覚ましは実行中スレッドを保持するため 1 では足りない。
SCHEDULER_MAX_WORKERS = int(os.environ.get("IR_SCHEDULER_WORKERS", "4"))
#: 発火予定を過ぎたジョブを何秒まで許容して実行するか。
#: 既定の 1 秒だと、サーバ再起動や一時的な高負荷で予約が黙って消える。
MISFIRE_GRACE_TIME = int(os.environ.get("IR_MISFIRE_GRACE_TIME", "300"))


def detect_lan_ip() -> str:
    """LAN 側の自 IP を推測する (ADVERTISE_HOST 未指定時のフォールバック)。

    外部に実際のパケットは飛ばさず、ルーティングテーブル上どの NIC が使われるかを
    UDP ソケットの ``getsockname()`` から得るだけの定番手法。
    ただし VPN 等がデフォルトルートを握っていると誤った IP を返すので、
    確実性が必要なら ``IR_ADVERTISE_HOST`` を明示すること。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


#: ESP32 から見たこのサーバのホスト。学習時のコールバック URL 生成に使う。
ADVERTISE_HOST = os.environ.get("IR_ADVERTISE_HOST") or detect_lan_ip()


def callback_base_url() -> str:
    """ESP32 に渡すコールバック URL のベース。"""
    return f"http://{ADVERTISE_HOST}:{PORT}"

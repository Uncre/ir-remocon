"""ロギング設定。

旧実装は ``print()`` のみで、タイムスタンプもレベルもローテーションも無かった。
実際、ログが肥大化した状態で ``sqlite3.OperationalError: database or disk is full``
が発生してスケジューラスレッドが死亡した形跡がログに残っている。
ここではファイルへのローテーション出力と標準出力の両方を設定する。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging() -> None:
    """ルートロガーを設定する。多重呼び出しは無視される。"""
    global _configured
    if _configured:
        return

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # 5MB × 3 世代。ディスクを食い潰してDB書き込みごと巻き添えにするのを防ぐ。
    file_handler = RotatingFileHandler(
        config.LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # uvicorn は独自ハンドラを持つので、ルートに委譲させて出力先を一本化する。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # APScheduler の INFO は「ジョブを追加した」等が中心で冗長なため WARNING 以上に絞る。
    # ただしジョブの失敗/取りこぼしは scheduler.py のイベントリスナで別途拾っている。
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    _configured = True

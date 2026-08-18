"""SQLite への接続とスキーマ管理。

旧実装は全エンドポイントが ``sqlite3.connect()`` / ``try: ... finally: conn.close()``
を手書きしており、同じ 5 行が 8 箇所に散らばっていた。
ここでは :func:`get_conn` のコンテキストマネージャに一本化し、
commit / rollback / close の取りこぼしを構造的に防ぐ。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import config

logger = logging.getLogger(__name__)


def utcnow_iso() -> str:
    """タイムスタンプ列に入れる ISO8601 文字列 (UTC)。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """DB 接続を取得する。正常終了で commit、例外で rollback、必ず close。

    ``row_factory`` を設定済みなので、結果は ``row["name"]`` の形で参照できる。
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # 外部キー制約は接続ごとに有効化が必要 (SQLite の既定は OFF)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    """スキーマを作成・移行する。何度呼んでも安全 (冪等)。

    既存の ``ir_signals`` のデータは保持したまま、
    機器登録テーブル (``devices``) の追加と日時列の追加のみを行う。
    """
    with get_conn() as conn:
        # WAL は読み書きの同時実行に強く、スケジューラスレッドと HTTP スレッドが
        # 同時に DB を触るこのアプリと相性が良い。ロック待ちの上限も設定する。
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ir_signals (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL UNIQUE,
                raw_data  TEXT NOT NULL
            )
            """
        )

        # 機器 (ESP32) の登録。
        # 旧実装はリクエストのたびにフロントから IP を送らせ、予約ジョブには
        # 作成時の IP が焼き付いていた。そのため IP が変わると既存の予約が全滅する。
        # ここで機器を DB に持たせ、ジョブは device_id だけを保持する形に変える。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                host       TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # 既存 DB への列追加 (ALTER TABLE ... ADD COLUMN は IF NOT EXISTS が使えない)
        existing = _column_names(conn, "ir_signals")
        for column in ("created_at", "updated_at"):
            if column not in existing:
                conn.execute(f"ALTER TABLE ir_signals ADD COLUMN {column} TEXT")
                logger.info("ir_signals に %s 列を追加しました", column)

        # 機器が 1 台も無い初回起動時のみ、既定機器を作っておく。
        # (フロントの設定タブから編集できる)
        count = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
        if count == 0:
            now = utcnow_iso()
            conn.execute(
                "INSERT INTO devices (name, host, is_default, created_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?)",
                ("esp32", "192.168.1.4", now, now),
            )
            logger.info("既定の機器 'esp32' (192.168.1.4) を登録しました")

    logger.info("データベースを初期化しました: %s", config.DB_PATH)


def check_health() -> bool:
    """DB に読み書きできるかの簡易チェック (/api/health 用)。"""
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        logger.exception("DB のヘルスチェックに失敗しました")
        return False

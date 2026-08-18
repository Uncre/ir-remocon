"""DB アクセス層 (信号と機器の CRUD)。

旧実装は SQL がエンドポイント関数の中に直書きされていて、スケジューラから同じ
問い合わせをしたいときにコピーするしかなかった。ここに集約する。

**HTTPException をこの層に持ち込まない。** Phase 4 のスケジューラは HTTP の
コンテキストの外からリポジトリを呼ぶため、FastAPI に依存させると使えなくなる。
代わりに :class:`RepositoryError` 系のドメイン例外に ``http_status`` を持たせ、
HTTP への変換は ``main.py`` の例外ハンドラ 1 箇所だけで行う
(``esp32.py`` の例外階層と同じ方針)。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

from .db import get_conn, utcnow_iso

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# ドメイン例外
# -----------------------------------------------------------------------------
class RepositoryError(Exception):
    """DB 操作の失敗。``http_status`` はルータ層のハンドラが読む。"""

    http_status = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(RepositoryError):
    http_status = 404


class DuplicateName(RepositoryError):
    http_status = 409


# -----------------------------------------------------------------------------
# 赤外線信号
# -----------------------------------------------------------------------------
def list_signals() -> list[dict[str, Any]]:
    """信号の一覧 (id, name のみ)。

    旧実装は ``ORDER BY`` が無く、一覧の並びが SQLite の内部都合で変わっていた。
    ``COLLATE NOCASE`` は SQLite では ASCII にしか効かないので日本語名は
    コードポイント順になるが、ここで欲しいのは「安定した順序」なのでこれで足りる。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name FROM ir_signals ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def get_signal(name: str) -> dict[str, Any]:
    """信号 1 件を raw_data 込みで取得する。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, raw_data FROM ir_signals WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise NotFound(f"信号 '{name}' は登録されていません")
    return {"id": row["id"], "name": row["name"], "raw_data": _decode_raw(row["raw_data"], name)}


def get_signal_raw(name: str) -> list[int]:
    """送信のホットパス用。raw_data だけを引く。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT raw_data FROM ir_signals WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise NotFound(f"信号 '{name}' は登録されていません")
    return _decode_raw(row["raw_data"], name)


def _decode_raw(raw_json: str, name: str) -> list[int]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RepositoryError(f"信号 '{name}' の raw_data が壊れています: {exc}") from exc
    if not isinstance(data, list):
        raise RepositoryError(f"信号 '{name}' の raw_data が配列ではありません")
    return data


def create_signal(name: str, raw_data: list[int]) -> dict[str, Any]:
    now = utcnow_iso()
    with get_conn() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO ir_signals (name, raw_data, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (name, json.dumps(raw_data), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(f"信号 '{name}' は既に存在します") from exc
        new_id = cursor.lastrowid
    logger.info("信号を登録しました: %s (%d 要素)", name, len(raw_data))
    return {"id": new_id, "name": name, "raw_data": raw_data}


def update_signal(
    name: str,
    *,
    new_name: Optional[str] = None,
    raw_data: Optional[list[int]] = None,
) -> dict[str, Any]:
    """信号を更新する。リネームと raw_data の差し替えの両方に対応。

    旧実装は接続を保持したまま別の関数を呼んで 2 本目の接続を開いており、
    さらに既存名へのリネームで ``IntegrityError`` が素通りして 500 になっていた。
    ここでは 1 つのトランザクションで完結させ、衝突は 409 で返す。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM ir_signals WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise NotFound(f"信号 '{name}' は登録されていません")
        signal_id = row["id"]

        # 事前チェックは親切なメッセージのため。ただしチェックと UPDATE の間に
        # 別スレッドが割り込む可能性は原理的に消せないので IntegrityError も捕まえる。
        if new_name is not None and new_name != name:
            conflict = conn.execute(
                "SELECT 1 FROM ir_signals WHERE name = ? AND id != ?", (new_name, signal_id)
            ).fetchone()
            if conflict is not None:
                raise DuplicateName(f"信号 '{new_name}' は既に存在します")

        sets: list[str] = []
        params: list[Any] = []
        if new_name is not None:
            sets.append("name = ?")
            params.append(new_name)
        if raw_data is not None:
            sets.append("raw_data = ?")
            params.append(json.dumps(raw_data))
        sets.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(signal_id)

        try:
            conn.execute(f"UPDATE ir_signals SET {', '.join(sets)} WHERE id = ?", params)
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(f"信号 '{new_name}' は既に存在します") from exc

        updated = conn.execute(
            "SELECT id, name, raw_data FROM ir_signals WHERE id = ?", (signal_id,)
        ).fetchone()

    logger.info("信号を更新しました: %s -> %s", name, updated["name"])
    return {
        "id": updated["id"],
        "name": updated["name"],
        "raw_data": _decode_raw(updated["raw_data"], updated["name"]),
    }


def upsert_signal(name: str, raw_data: list[int]) -> dict[str, Any]:
    """学習コールバック用。同名があれば raw_data を差し替える。

    Phase 5 のコールバックルータから使う。今フェーズでは未使用だが、
    ``INSERT ... ON CONFLICT`` を 1 箇所に置いておく方が後で散らからない。
    """
    now = utcnow_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ir_signals (name, raw_data, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET raw_data = excluded.raw_data,"
            " updated_at = excluded.updated_at",
            (name, json.dumps(raw_data), now, now),
        )
        row = conn.execute(
            "SELECT id, name FROM ir_signals WHERE name = ?", (name,)
        ).fetchone()
    logger.info("信号を保存しました (upsert): %s (%d 要素)", name, len(raw_data))
    return {"id": row["id"], "name": row["name"], "raw_data": raw_data}


def delete_signal(name: str) -> None:
    with get_conn() as conn:
        cursor = conn.execute("DELETE FROM ir_signals WHERE name = ?", (name,))
        if cursor.rowcount == 0:
            raise NotFound(f"信号 '{name}' は登録されていません")
    logger.info("信号を削除しました: %s", name)


# -----------------------------------------------------------------------------
# 機器 (ESP32)
# -----------------------------------------------------------------------------
# 今フェーズでは読み取りのみ。作成/更新/削除と is_default の排他制御は Phase 3。
_DEVICE_COLUMNS = "id, name, host, is_default, created_at, updated_at"


def _row_to_device(row: sqlite3.Row) -> dict[str, Any]:
    device = dict(row)
    # SQLite は真偽値を 0/1 で持つので、ここで bool に直しておく。
    device["is_default"] = bool(device["is_default"])
    return device


def list_devices() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices ORDER BY id"
        ).fetchall()
    return [_row_to_device(row) for row in rows]


def get_device(device_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"機器 id={device_id} は登録されていません")
    return _row_to_device(row)


def get_default_device() -> dict[str, Any]:
    """既定機器を返す。

    既定フラグが 1 件も立っていない場合は最若番にフォールバックする。
    Phase 3 の機器編集でフラグが落ちる事故はあり得るが、そのせいで送信が
    全部死ぬのは割に合わない。``init_db()`` が最低 1 台を保証しているので
    このフォールバックは実質的に到達しない。
    """
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE is_default = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT {_DEVICE_COLUMNS} FROM devices ORDER BY id LIMIT 1"
            ).fetchone()
            if row is not None:
                logger.warning(
                    "既定機器のフラグが立っていません。最若番 '%s' を使います", row["name"]
                )
    if row is None:
        raise NotFound("機器が 1 台も登録されていません。設定タブで登録してください")
    return _row_to_device(row)


def resolve_device(device_id: Optional[int]) -> dict[str, Any]:
    """送信先の機器を決める。``device_id`` 省略時は既定機器。"""
    if device_id is None:
        return get_default_device()
    return get_device(device_id)

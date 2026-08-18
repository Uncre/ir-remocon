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
from .models import normalize_host

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


class ConstraintViolation(RepositoryError):
    """業務上の不変条件に反する操作。

    「最後の 1 台の機器を削除する」「唯一の既定機器から既定フラグを外す」など、
    DB 制約では表現できないがアプリとして許してはいけない操作を弾く。
    どちらも許すと :func:`resolve_device` が 404 を投げ始め、**送信が全部死ぬ**。
    """

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
# この層が守る不変条件は 2 つ。どちらも破れると送信経路が丸ごと死ぬ。
#
#   1. 機器は常に 1 台以上存在する
#   2. 既定機器は常にちょうど 1 台
#
# 1 が破れると resolve_device() が 404 を投げ始め、送信も予約も全滅する。
# 2 が破れても get_default_device() のフォールバックで即死はしないが、
# 「既定を外したのに送信は動く」という説明のつかない状態になる。
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
    """送信先の機器を決める。``device_id`` 省略時は既定機器。

    Phase 4 のスケジューラは **ジョブの発火のたびに** これを呼ぶ。
    ジョブ引数には ``device_id`` しか入れないので、host を変更すれば既存の予約すべてに
    即座に反映される (旧実装は作成時の IP が pickle されて固定され、IP が変わると
    予約が全滅していた = 不具合 E)。
    """
    if device_id is None:
        return get_default_device()
    return get_device(device_id)


def _set_default(conn: sqlite3.Connection, device_id: int, now: str) -> None:
    """``device_id`` だけを既定にする。

    「他を降ろす」と「自分を上げる」の 2 文を **同じトランザクション** で実行する。
    ``get_conn()`` が正常終了で commit / 例外で rollback するので、既定 0 台という
    中間状態が永続化されることはない。
    """
    conn.execute(
        "UPDATE devices SET is_default = 0, updated_at = ? WHERE is_default = 1 AND id != ?",
        (now, device_id),
    )
    conn.execute(
        "UPDATE devices SET is_default = 1, updated_at = ? WHERE id = ? AND is_default = 0",
        (now, device_id),
    )


def create_device(name: str, host: str, is_default: bool = False) -> dict[str, Any]:
    """機器を登録する。

    **疎通確認はしない。** ESP32 の電源が入っていなくても先に登録できるべきで、
    「登録できない = 機器が壊れている」と誤解させたくない。接続確認は
    :func:`esp32.get_status` を使う専用エンドポイントの仕事。
    """
    host = normalize_host(host)
    now = utcnow_iso()
    with get_conn() as conn:
        # 機器が 0 台の状態 (DB を直接いじった場合にしか起きない) からの復旧経路。
        # ここで既定フラグを立てておかないと、登録した直後なのに
        # get_default_device() が警告付きフォールバックに落ちる。
        count = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
        if count == 0:
            is_default = True

        try:
            cursor = conn.execute(
                "INSERT INTO devices (name, host, is_default, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, host, 1 if is_default else 0, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(f"機器 '{name}' は既に存在します") from exc
        new_id = cursor.lastrowid

        if is_default:
            _set_default(conn, new_id, now)

        row = conn.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE id = ?", (new_id,)
        ).fetchone()

    logger.info("機器を登録しました: %s (%s, 既定=%s)", name, host, is_default)
    return _row_to_device(row)


def update_device(
    device_id: int,
    *,
    name: Optional[str] = None,
    host: Optional[str] = None,
    is_default: Optional[bool] = None,
) -> dict[str, Any]:
    """機器を更新する。

    host の変更は **次の送信から即座に効く**。ジョブも画面も device_id しか
    保持していないため、再起動も予約の作り直しも不要 (不具合 E の解消)。
    """
    now = utcnow_iso()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, is_default FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"機器 id={device_id} は登録されていません")

        # 「既定を外す」は単独では許さない。外した結果 0 台になると
        # 「既定は無いのに送信は動く (最若番へのフォールバック)」という
        # 説明のつかない状態になる。別の機器を既定にすれば自動的に降りる。
        if is_default is False and row["is_default"]:
            raise ConstraintViolation(
                f"機器 '{row['name']}' は既定機器です。"
                "先に別の機器を既定に設定してください (既定は常に 1 台必要です)"
            )

        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if host is not None:
            sets.append("host = ?")
            params.append(normalize_host(host))
        if sets:
            sets.append("updated_at = ?")
            params.append(now)
            params.append(device_id)
            try:
                conn.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", params)
            except sqlite3.IntegrityError as exc:
                raise DuplicateName(f"機器 '{name}' は既に存在します") from exc

        if is_default:
            _set_default(conn, device_id, now)

        updated = conn.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE id = ?", (device_id,)
        ).fetchone()

    logger.info(
        "機器を更新しました: id=%d name=%s host=%s 既定=%s",
        device_id, updated["name"], updated["host"], bool(updated["is_default"]),
    )
    return _row_to_device(updated)


def delete_device(device_id: int) -> Optional[int]:
    """機器を削除する。既定機器を消した場合は最若番を昇格させ、その id を返す。

    :return: 昇格させた機器の id。昇格が起きなければ ``None``。
    :raises ConstraintViolation: 最後の 1 台を削除しようとした場合。

    最後の 1 台を守るのは、機器が 0 台になると :func:`resolve_device` が 404 を
    投げ始めて **送信も予約も全部死ぬ** から。UI からの操作 1 回でシステムを
    復旧不能にできてはいけない。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, is_default FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"機器 id={device_id} は登録されていません")

        count = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
        if count <= 1:
            raise ConstraintViolation(
                f"機器 '{row['name']}' は最後の 1 台なので削除できません "
                "(送信先が無くなります)。先に別の機器を登録してください"
            )

        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))

        promoted: Optional[int] = None
        if row["is_default"]:
            successor = conn.execute(
                "SELECT id, name FROM devices ORDER BY id LIMIT 1"
            ).fetchone()
            _set_default(conn, successor["id"], utcnow_iso())
            promoted = successor["id"]
            logger.info(
                "既定機器を削除したため '%s' (id=%d) を既定に昇格しました",
                successor["name"], promoted,
            )

    logger.info("機器を削除しました: %s (id=%d)", row["name"], device_id)
    return promoted

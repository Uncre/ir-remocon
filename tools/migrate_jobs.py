"""旧 APScheduler jobs DB を棚卸しし、安全に空へ移行する。

既定動作は読み取り専用で、予約の JSON 表現を標準出力へ出すだけ。元 DB から
ジョブを削除するには ``--apply`` と ``--confirm-server-stopped`` の両方が必要。
適用時は、JSON レポートと SQLite backup API による完全な DB バックアップを
作成・検証してから ``apscheduler_jobs`` の行だけを transaction で削除する。

APScheduler の job_state は pickle である。人が予約を再登録できる情報を得るため
このツールは state を unpickle するので、自分で管理している信頼できる jobs DB
だけを入力すること。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pickle
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOBS_DB = Path(
    os.environ.get("IR_JOBS_DB_PATH") or PROJECT_ROOT / "ir_remocon" / "jobs.db"
)
TABLE_NAME = "apscheduler_jobs"


class MigrationError(RuntimeError):
    """安全に棚卸し・移行できないときのエラー。"""


@dataclass(frozen=True)
class MigrationResult:
    """移行結果。ジョブが 0 件なら成果物のパスは None。"""

    report: dict[str, Any]
    report_path: Optional[Path]
    backup_path: Optional[Path]
    deleted_count: int


def _readonly_connection(path: Path) -> sqlite3.Connection:
    """存在する SQLite DB を、作成や WAL 更新をしない読み取り専用で開く。"""
    resolved = path.resolve()
    if not resolved.is_file():
        raise MigrationError(f"jobs DB が見つかりません: {resolved}")
    try:
        return sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise MigrationError(f"jobs DB を読み取り専用で開けません: {resolved}: {exc}") from exc


def _load_rows(path: Path) -> list[tuple[str, Optional[float], bytes]]:
    """APScheduler テーブルを検証し、全行を読み取る。"""
    try:
        with closing(_readonly_connection(path)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (TABLE_NAME,),
            ).fetchone()
            if table is None:
                raise MigrationError(
                    f"{path.resolve()} に {TABLE_NAME} テーブルがありません"
                )

            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
            }
            required = {"id", "next_run_time", "job_state"}
            if not required.issubset(columns):
                missing = ", ".join(sorted(required - columns))
                raise MigrationError(
                    f"{TABLE_NAME} の必須列がありません: {missing}"
                )

            rows = conn.execute(
                f"SELECT id, next_run_time, job_state FROM {TABLE_NAME} "
                "ORDER BY next_run_time, id"
            ).fetchall()
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise MigrationError(f"jobs DB の棚卸しに失敗しました: {path.resolve()}: {exc}") from exc

    return [(str(job_id), next_run, bytes(job_state)) for job_id, next_run, job_state in rows]


def _fingerprint(rows: Sequence[tuple[str, Optional[float], bytes]]) -> str:
    """棚卸し後のすり替わりを検知するため、行全体の digest を作る。"""
    digest = hashlib.sha256()
    for job_id, next_run, state in rows:
        for value in (job_id.encode("utf-8"), repr(next_run).encode("ascii"), state):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def _timestamp(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return f"invalid timestamp: {value!r}"


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """pickle 内の値を JSON で読める範囲へ保守的に変換する。"""
    if depth >= 20:
        return "<maximum nesting depth reached>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth=depth + 1) for item in value]
    return repr(value)


def _decode_state(blob: bytes) -> dict[str, Any]:
    """APScheduler job_state を読みやすい形にする。失敗時も raw を残す。"""
    try:
        state = pickle.loads(blob)  # noqa: S301 - CLI で信頼済みローカル DB のみを扱う
        if not isinstance(state, dict):
            raise TypeError(f"job_state が dict ではありません: {type(state).__name__}")
    except Exception as exc:  # noqa: BLE001 - 1 件の破損で残りを失わない
        return {
            "decode_error": f"{type(exc).__name__}: {exc}",
            "job_state_base64": base64.b64encode(blob).decode("ascii"),
        }

    ordered_fields = (
        "version",
        "id",
        "name",
        "func",
        "trigger",
        "executor",
        "args",
        "kwargs",
        "misfire_grace_time",
        "coalesce",
        "max_instances",
        "next_run_time",
    )
    decoded = {
        field: _json_safe(state[field]) for field in ordered_fields if field in state
    }
    extra = {
        str(key): _json_safe(value)
        for key, value in state.items()
        if key not in ordered_fields
    }
    if extra:
        decoded["extra"] = extra
    return decoded


def inventory(path: Path) -> tuple[dict[str, Any], str]:
    """DB を変更せずに JSON 化した棚卸し結果と fingerprint を返す。"""
    path = path.resolve()
    rows = _load_rows(path)
    fingerprint = _fingerprint(rows)
    report = {
        "format_version": 1,
        "source": str(path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_count": len(rows),
        "source_fingerprint_sha256": fingerprint,
        "jobs": [
            {
                "id": job_id,
                "next_run_time_epoch": next_run,
                "next_run_time_utc": _timestamp(next_run),
                "state": _decode_state(state),
            }
            for job_id, next_run, state in rows
        ],
    }
    return report, fingerprint


def _artifact_paths(source: Path, output_dir: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base = f"{source.stem}.phase7-{stamp}"
    return output_dir / f"{base}.json", output_dir / f"{base}.bak"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except OSError as exc:
        raise MigrationError(f"JSON レポートを書き込めません: {path}: {exc}") from exc


def _backup_database(source: Path, destination: Path) -> None:
    """WAL の内容も含む一貫したスナップショットを SQLite API で作る。"""
    try:
        with closing(_readonly_connection(source)) as source_conn:
            with closing(sqlite3.connect(destination)) as backup_conn:
                source_conn.backup(backup_conn)
    except (MigrationError, sqlite3.Error, OSError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"DB バックアップを作成できません: {destination}: {exc}") from exc


def _clear_if_unchanged(source: Path, expected_fingerprint: str) -> int:
    """排他 transaction 内で再確認し、棚卸し時と同じ場合だけ全ジョブを消す。"""
    try:
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            try:
                conn.execute("BEGIN EXCLUSIVE")
                rows = conn.execute(
                    f"SELECT id, next_run_time, job_state FROM {TABLE_NAME} "
                    "ORDER BY next_run_time, id"
                ).fetchall()
                normalized = [
                    (str(job_id), next_run, bytes(state))
                    for job_id, next_run, state in rows
                ]
                if _fingerprint(normalized) != expected_fingerprint:
                    raise MigrationError(
                        "棚卸し後に jobs DB が変更されました。サーバが停止していることを確認し、"
                        "最初からやり直してください"
                    )
                deleted = conn.execute(f"DELETE FROM {TABLE_NAME}").rowcount
                remaining = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
                if deleted != len(normalized) or remaining != 0:
                    raise MigrationError(
                        "旧ジョブの削除結果を検証できません。transaction を取り消しました"
                    )
                conn.commit()
                return deleted
            except Exception:
                conn.rollback()
                raise
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise MigrationError(f"旧ジョブを削除できません: {source}: {exc}") from exc


def migrate(source: Path, output_dir: Path) -> MigrationResult:
    """レポートとバックアップを作成・検証し、元 DB のジョブを空にする。"""
    source = source.resolve()
    output_dir = output_dir.resolve()
    report, fingerprint = inventory(source)

    if report["job_count"] == 0:
        return MigrationResult(report, None, None, 0)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrationError(f"出力ディレクトリを作れません: {output_dir}: {exc}") from exc

    report_path, backup_path = _artifact_paths(source, output_dir)
    _write_report(report_path, report)
    _backup_database(source, backup_path)

    backup_rows = _load_rows(backup_path)
    if _fingerprint(backup_rows) != fingerprint:
        raise MigrationError(
            f"DB バックアップの検証に失敗しました。元 DB は変更していません: {backup_path}"
        )

    deleted = _clear_if_unchanged(source, fingerprint)
    return MigrationResult(report, report_path, backup_path, deleted)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="旧 APScheduler jobs DB の棚卸し・バックアップ・クリア"
    )
    parser.add_argument(
        "jobs_db",
        nargs="?",
        type=Path,
        default=DEFAULT_JOBS_DB,
        help=f"対象の jobs DB (既定: {DEFAULT_JOBS_DB})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="JSON と DB バックアップの作成後、元 DB の旧ジョブを削除する",
    )
    parser.add_argument(
        "--confirm-server-stopped",
        action="store_true",
        help="サーバを停止済みであることを明示確認する (--apply と併用必須)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="移行成果物の出力先 (既定: jobs DB と同じ階層の backup_before_refactor)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_server_stopped:
        parser.error("--apply には --confirm-server-stopped が必要です")
    if args.confirm_server_stopped and not args.apply:
        parser.error("--confirm-server-stopped は --apply と一緒に指定してください")

    try:
        if not args.apply:
            report, _ = inventory(args.jobs_db)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                "\n読み取り専用の棚卸しです。元 DB は変更していません。",
                file=sys.stderr,
            )
            return 0

        output_dir = args.output_dir or args.jobs_db.resolve().parent / "backup_before_refactor"
        result = migrate(args.jobs_db, output_dir)
        if result.deleted_count == 0:
            print("旧ジョブは 0 件です。DB を変更せず終了しました。")
            return 0
        print(f"旧ジョブ {result.deleted_count} 件を退避して元 DB から削除しました。")
        print(f"JSON: {result.report_path}")
        print(f"DB backup: {result.backup_path}")
        return 0
    except MigrationError as exc:
        print(f"移行に失敗しました: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

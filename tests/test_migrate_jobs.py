"""``tools/migrate_jobs.py`` の非破壊性とバックアップ順序を固定する。"""

from __future__ import annotations

import json
import pickle
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
import migrate_jobs  # noqa: E402


def _create_jobs_db(path: Path, *, corrupt: bool = False) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE apscheduler_jobs (
                id VARCHAR(191) NOT NULL PRIMARY KEY,
                next_run_time FLOAT,
                job_state BLOB NOT NULL
            )
            """
        )
        states = [
            {
                "version": 1,
                "id": "daily-light",
                "name": "照明 ON",
                "func": "__main__:execute_ir_send",
                "trigger": "cron[hour='7', minute='30']",
                "args": ["room_light_turn_on", "192.168.1.16"],
                "kwargs": {},
                "next_run_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
            },
            {
                "version": 1,
                "id": "paused-job",
                "name": "停止中",
                "func": "__main__:execute_ir_send",
                "args": ["room_light_turn_off", "192.168.1.16"],
                "kwargs": {},
                "next_run_time": None,
            },
        ]
        for index, state in enumerate(states):
            blob = pickle.dumps(state)
            if corrupt and index == 1:
                blob = b"not-a-pickle"
            conn.execute(
                "INSERT INTO apscheduler_jobs (id, next_run_time, job_state) VALUES (?, ?, ?)",
                (state["id"], 1_767_225_600.0 if index == 0 else None, blob),
            )
    return path


def _job_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM apscheduler_jobs").fetchone()[0]


def test_inventory_is_read_only_and_decodes_useful_fields(tmp_path):
    source = _create_jobs_db(tmp_path / "jobs.db")
    before = source.read_bytes()

    report, fingerprint = migrate_jobs.inventory(source)

    assert source.read_bytes() == before
    assert report["job_count"] == 2
    assert report["source_fingerprint_sha256"] == fingerprint
    first = next(job for job in report["jobs"] if job["id"] == "daily-light")
    assert first["state"]["func"] == "__main__:execute_ir_send"
    assert first["state"]["args"] == ["room_light_turn_on", "192.168.1.16"]
    assert first["next_run_time_utc"].startswith("2026-")


def test_cli_default_is_read_only(tmp_path, capsys):
    source = _create_jobs_db(tmp_path / "jobs.db")
    before = source.read_bytes()

    assert migrate_jobs.main([str(source)]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["job_count"] == 2
    assert "元 DB は変更していません" in captured.err
    assert source.read_bytes() == before


def test_corrupt_state_is_preserved_as_base64(tmp_path):
    source = _create_jobs_db(tmp_path / "jobs.db", corrupt=True)

    report, _ = migrate_jobs.inventory(source)

    corrupt = next(job for job in report["jobs"] if job["id"] == "paused-job")
    assert "UnpicklingError" in corrupt["state"]["decode_error"]
    assert corrupt["state"]["job_state_base64"]


def test_migrate_backs_up_and_verifies_before_clearing(tmp_path):
    source = _create_jobs_db(tmp_path / "jobs.db")
    output_dir = tmp_path / "backups"

    result = migrate_jobs.migrate(source, output_dir)

    assert result.deleted_count == 2
    assert _job_count(source) == 0
    assert result.backup_path is not None
    assert _job_count(result.backup_path) == 2
    assert result.report_path is not None
    saved = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert saved["job_count"] == 2
    assert saved["source_fingerprint_sha256"] == result.report["source_fingerprint_sha256"]


def test_backup_failure_leaves_source_untouched(tmp_path, monkeypatch):
    source = _create_jobs_db(tmp_path / "jobs.db")

    def fail_backup(_source, _destination):
        raise migrate_jobs.MigrationError("backup failed")

    monkeypatch.setattr(migrate_jobs, "_backup_database", fail_backup)

    with pytest.raises(migrate_jobs.MigrationError, match="backup failed"):
        migrate_jobs.migrate(source, tmp_path / "backups")

    assert _job_count(source) == 2


def test_change_after_backup_aborts_without_clearing(tmp_path, monkeypatch):
    source = _create_jobs_db(tmp_path / "jobs.db")
    original_backup = migrate_jobs._backup_database

    def backup_then_change(source_path, destination):
        original_backup(source_path, destination)
        with sqlite3.connect(source_path) as conn:
            conn.execute(
                "INSERT INTO apscheduler_jobs (id, next_run_time, job_state) VALUES (?, ?, ?)",
                ("arrived-late", None, pickle.dumps({"id": "arrived-late"})),
            )

    monkeypatch.setattr(migrate_jobs, "_backup_database", backup_then_change)

    with pytest.raises(migrate_jobs.MigrationError, match="棚卸し後に jobs DB が変更"):
        migrate_jobs.migrate(source, tmp_path / "backups")

    assert _job_count(source) == 3


def test_empty_database_is_not_rewritten(tmp_path):
    source = _create_jobs_db(tmp_path / "jobs.db")
    with sqlite3.connect(source) as conn:
        conn.execute("DELETE FROM apscheduler_jobs")
    before = source.read_bytes()

    result = migrate_jobs.migrate(source, tmp_path / "backups")

    assert result.deleted_count == 0
    assert result.report_path is None
    assert result.backup_path is None
    assert source.read_bytes() == before
    assert not (tmp_path / "backups").exists()


def test_apply_requires_explicit_server_stopped_confirmation(tmp_path):
    source = _create_jobs_db(tmp_path / "jobs.db")

    with pytest.raises(SystemExit) as excinfo:
        migrate_jobs.main([str(source), "--apply"])

    assert excinfo.value.code == 2
    assert _job_count(source) == 2


def test_missing_apscheduler_table_is_rejected(tmp_path):
    source = tmp_path / "jobs.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")

    with pytest.raises(migrate_jobs.MigrationError, match="apscheduler_jobs"):
        migrate_jobs.inventory(source)

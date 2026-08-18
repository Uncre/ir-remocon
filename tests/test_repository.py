"""リポジトリ層のテスト。旧実装で 500 になっていた経路を中心に固定する。"""

from __future__ import annotations

import pytest

from ir_remocon.app import db, repository

RAW = [9000, 4500, 560]


# -----------------------------------------------------------------------------
# 信号
# -----------------------------------------------------------------------------
def test_create_and_get(temp_db):
    created = repository.create_signal("light_on", RAW)
    assert created["name"] == "light_on"
    assert created["raw_data"] == RAW

    fetched = repository.get_signal("light_on")
    assert fetched["raw_data"] == RAW
    assert fetched["id"] == created["id"]


def test_create_writes_timestamps(temp_db):
    """Phase 1 で列は足したが誰も書いていなかった created_at/updated_at を埋めること。"""
    repository.create_signal("light_on", RAW)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT created_at, updated_at FROM ir_signals WHERE name = 'light_on'"
        ).fetchone()
    assert row["created_at"] is not None
    assert row["updated_at"] is not None


def test_create_duplicate_raises_409(temp_db):
    repository.create_signal("light_on", RAW)
    with pytest.raises(repository.DuplicateName) as excinfo:
        repository.create_signal("light_on", RAW)
    assert excinfo.value.http_status == 409


def test_get_missing_raises_404(temp_db):
    with pytest.raises(repository.NotFound) as excinfo:
        repository.get_signal("nope")
    assert excinfo.value.http_status == 404


def test_list_is_ordered(temp_db):
    """旧実装は ORDER BY が無く、一覧の並びが安定しなかった。"""
    for name in ("zulu", "alpha", "Mike"):
        repository.create_signal(name, RAW)
    names = [signal["name"] for signal in repository.list_signals()]
    assert names == ["alpha", "Mike", "zulu"]


def test_list_omits_raw_data(temp_db):
    repository.create_signal("light_on", RAW)
    assert set(repository.list_signals()[0]) == {"id", "name"}


def test_rename_onto_existing_name_raises_409_not_500(temp_db):
    """バグ H: 旧実装は IntegrityError を捕まえておらず 500 になっていた。"""
    repository.create_signal("a", RAW)
    repository.create_signal("b", RAW)
    with pytest.raises(repository.DuplicateName) as excinfo:
        repository.update_signal("a", new_name="b")
    assert excinfo.value.http_status == 409
    # 失敗した更新でデータが壊れていないこと
    assert repository.get_signal("a")["raw_data"] == RAW


def test_rename_to_same_name_is_allowed(temp_db):
    repository.create_signal("a", RAW)
    updated = repository.update_signal("a", new_name="a", raw_data=[1, 2])
    assert updated["name"] == "a"
    assert updated["raw_data"] == [1, 2]


def test_update_raw_data_only(temp_db):
    repository.create_signal("a", RAW)
    updated = repository.update_signal("a", raw_data=[1, 2, 3])
    assert updated["name"] == "a"
    assert updated["raw_data"] == [1, 2, 3]


def test_update_missing_raises_404(temp_db):
    with pytest.raises(repository.NotFound):
        repository.update_signal("nope", raw_data=[1])


def test_delete(temp_db):
    repository.create_signal("a", RAW)
    repository.delete_signal("a")
    assert repository.list_signals() == []


def test_delete_missing_raises_404(temp_db):
    with pytest.raises(repository.NotFound):
        repository.delete_signal("nope")


def test_upsert_overwrites(temp_db):
    repository.create_signal("a", RAW)
    result = repository.upsert_signal("a", [7, 8])
    assert result["raw_data"] == [7, 8]
    assert repository.get_signal("a")["raw_data"] == [7, 8]
    assert len(repository.list_signals()) == 1


def test_get_signal_raw_is_decoded(temp_db):
    repository.create_signal("a", RAW)
    assert repository.get_signal_raw("a") == RAW


def test_corrupt_raw_data_raises_repository_error(temp_db):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO ir_signals (name, raw_data) VALUES ('bad', 'not json')"
        )
    with pytest.raises(repository.RepositoryError):
        repository.get_signal_raw("bad")


# -----------------------------------------------------------------------------
# 機器
# -----------------------------------------------------------------------------
def test_init_db_creates_default_device(temp_db):
    """init_db() が既定機器を 1 台入れているので、送信は最初から動く。"""
    device = repository.get_default_device()
    assert device["id"] == 1
    assert device["host"] == "192.168.1.4"
    assert device["is_default"] is True


def test_resolve_device_none_returns_default(temp_db):
    assert repository.resolve_device(None)["id"] == 1


def test_resolve_device_by_id(temp_db):
    assert repository.resolve_device(1)["id"] == 1


def test_resolve_unknown_device_raises_404(temp_db):
    with pytest.raises(repository.NotFound) as excinfo:
        repository.resolve_device(999)
    assert excinfo.value.http_status == 404


def test_default_falls_back_to_lowest_id_when_flag_lost(temp_db):
    """Phase 3 の機器編集で既定フラグが落ちても、送信が全部死なないこと。"""
    with db.get_conn() as conn:
        conn.execute("UPDATE devices SET is_default = 0")
    device = repository.get_default_device()
    assert device["id"] == 1


def test_no_devices_raises_404(temp_db):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM devices")
    with pytest.raises(repository.NotFound) as excinfo:
        repository.resolve_device(None)
    assert excinfo.value.http_status == 404

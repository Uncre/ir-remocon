"""``jobs.py`` (発火時に走る処理) のテスト。

**不具合 E と G の回帰ガードがここにある。**

- E: 旧実装はジョブ引数に作成時の IP を pickle していたため、ESP32 の DHCP
  アドレスが変わると既存の予約が全滅した。ログでは数ヶ月前のジョブが今も
  ``192.168.1.16`` を叩いている。
- G: 目覚ましが ``time.sleep`` でワーカースレッドを占有し、中断手段も無かった。
"""

from __future__ import annotations

import threading
import time as time_module
from datetime import time

import httpx
import pytest

from ir_remocon.app import config, esp32, jobs, repository, scheduler

RAW = [9000, 4500, 560]


@pytest.fixture
def signals(temp_db):
    repository.create_signal("on", RAW)
    repository.create_signal("off", RAW)
    return ("on", "off")


@pytest.fixture
def started(temp_db):
    sched = scheduler.start_scheduler()
    yield sched
    scheduler.shutdown_scheduler(wait=False)


def _hosts(mock_esp) -> list[str]:
    return [request.url.host for request in mock_esp.requests]


def _wait_for_alarm(timeout: float = 5.0) -> dict:
    deadline = time_module.monotonic() + timeout
    while time_module.monotonic() < deadline:
        running = jobs.list_running_alarms()
        if running:
            return running[0]
        time_module.sleep(0.01)
    raise AssertionError("アラームが登録されなかった")


# -----------------------------------------------------------------------------
# ★ 不具合 E の回帰ガード
# -----------------------------------------------------------------------------
def test_signal_job_resolves_host_at_fire_time(signals, mock_esp):
    """発火のたびに DB から host を引くこと。作成時の値を使わないこと。"""
    repository.update_device(1, host="192.168.1.77")

    jobs.run_signal_job(signal_name="on", device_id=1)

    assert _hosts(mock_esp) == ["192.168.1.77"]


def test_host_change_applies_to_existing_job(started, signals, mock_esp):
    """★ 予約を作った **後** に host を変えても、次の発火は新しい host へ飛ぶこと。

    旧実装が壊れていた核心。APScheduler が実際に呼ぶのと同じ
    ``job.func(**job.kwargs)`` で検証する。
    """
    result = scheduler.add_signal_job(
        signal_name="on", device_id=1, execute_time=time(8, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )
    job = started.get_job(result["id"])

    repository.update_device(1, host="192.168.1.200")
    job.func(**job.kwargs)

    assert _hosts(mock_esp) == ["192.168.1.200"], "作成時の host が焼き付いている (不具合 E)"


def test_signal_job_follows_default_device(signals, mock_esp):
    """device_id=None の予約は、既定機器を切り替えると宛先も追従すること。"""
    repository.create_device("spare", "192.168.1.5", is_default=True)

    jobs.run_signal_job(signal_name="on", device_id=None)

    assert _hosts(mock_esp) == ["192.168.1.5"]


def test_signal_job_raises_on_missing_signal(temp_db, mock_esp):
    """失敗を握り潰さないこと (旧実装は print して return していた = 不具合 D)。"""
    with pytest.raises(repository.NotFound):
        jobs.run_signal_job(signal_name="missing", device_id=None)
    assert mock_esp.requests == []


def test_signal_job_raises_on_send_failure(signals, mock_esp):
    mock_esp.handler = lambda request: httpx.Response(500)
    with pytest.raises(esp32.Esp32Error):
        jobs.run_signal_job(signal_name="on", device_id=None)


def test_signal_job_accepts_unknown_meta(signals, mock_esp):
    """meta は表示用。増えても発火が壊れないこと。"""
    jobs.run_signal_job(signal_name="on", device_id=None, meta={"kind": "signal", "x": 1})
    assert len(mock_esp.requests) == 1


# -----------------------------------------------------------------------------
# ★ 不具合 G の回帰ガード: 目覚ましが中断できること
# -----------------------------------------------------------------------------
def test_alarm_toggles_on_and_off(signals, mock_esp):
    jobs.run_wakeup_alarm(
        on_signal_name="on", off_signal_name="off",
        interval_seconds=0.01, duration_seconds=1, device_id=None,
    )
    assert len(mock_esp.requests) >= 2


def test_alarm_can_be_stopped_immediately(signals, mock_esp, monkeypatch):
    """``DELETE /api/alarms/{run_id}`` 相当で duration を待たずに止まること。

    旧実装は ``time.sleep`` で中断できず、一度鳴り始めたら
    duration_seconds の間止められなかった。
    """
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 60)
    thread = threading.Thread(
        target=jobs.run_wakeup_alarm,
        kwargs={
            "on_signal_name": "on", "off_signal_name": "off",
            "interval_seconds": 0.05, "duration_seconds": 60, "device_id": None,
        },
        daemon=True,
    )
    started_at = time_module.monotonic()
    thread.start()
    alarm = _wait_for_alarm()

    assert jobs.request_stop(alarm["run_id"]) is True

    thread.join(timeout=5)
    assert not thread.is_alive(), "停止通知でアラームが終わっていない"
    assert time_module.monotonic() - started_at < 5, "duration を待ってしまっている"


def test_alarm_is_unregistered_after_finish(signals, mock_esp):
    jobs.run_wakeup_alarm(
        on_signal_name="on", off_signal_name="off",
        interval_seconds=0.01, duration_seconds=1, device_id=None,
    )
    assert jobs.list_running_alarms() == []


def test_alarm_registry_exposes_details(signals, mock_esp, monkeypatch):
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 60)
    thread = threading.Thread(
        target=jobs.run_wakeup_alarm,
        kwargs={
            "on_signal_name": "on", "off_signal_name": "off",
            "interval_seconds": 0.05, "duration_seconds": 60, "device_id": None,
            "meta": {"job_id": "job-123"},
        },
        daemon=True,
    )
    thread.start()
    try:
        alarm = _wait_for_alarm()
        assert alarm["on_signal"] == "on"
        assert alarm["off_signal"] == "off"
        assert alarm["job_id"] == "job-123"
        assert alarm["ends_at"] > alarm["started_at"]
    finally:
        jobs.stop_all_alarms()
        thread.join(timeout=5)


def test_alarm_duration_is_capped_by_config(signals, mock_esp, monkeypatch):
    """設定の上限を超える duration は切り詰められること (ワーカーの占有防止)。"""
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 1)
    started_at = time_module.monotonic()

    jobs.run_wakeup_alarm(
        on_signal_name="on", off_signal_name="off",
        interval_seconds=0.05, duration_seconds=600, device_id=None,
    )

    assert time_module.monotonic() - started_at < 5


# -----------------------------------------------------------------------------
# 失敗時の挙動: 継続、連続 5 回で中止
# -----------------------------------------------------------------------------
def test_alarm_continues_after_single_failure(signals, mock_esp):
    """1 回の失敗では止めない (一時的な 409 で目覚ましが死ぬのは困る)。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(409)
        return httpx.Response(200, json={"status": "ok"})

    mock_esp.handler = handler

    jobs.run_wakeup_alarm(
        on_signal_name="on", off_signal_name="off",
        interval_seconds=0.01, duration_seconds=1, device_id=None,
    )

    assert calls["n"] > jobs.MAX_CONSECUTIVE_FAILURES


def test_alarm_aborts_after_consecutive_failures(signals, mock_esp, monkeypatch):
    """連続 5 回失敗で中止し、例外として送出すること。

    例外にすることで EVENT_JOB_ERROR に乗り、``/api/health`` の
    ``last_job_error`` に出る (黙って 30 分ログを埋めない)。
    """
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 60)
    mock_esp.handler = lambda request: httpx.Response(409)

    with pytest.raises(jobs.AlarmAborted) as excinfo:
        jobs.run_wakeup_alarm(
            on_signal_name="on", off_signal_name="off",
            interval_seconds=0.01, duration_seconds=60, device_id=None,
        )

    assert len(mock_esp.requests) == jobs.MAX_CONSECUTIVE_FAILURES
    assert str(jobs.MAX_CONSECUTIVE_FAILURES) in str(excinfo.value)
    assert jobs.list_running_alarms() == []


def test_alarm_aborts_immediately_on_missing_signal(signals, mock_esp, monkeypatch):
    """信号が消えているのは一時的な失敗ではない。5 回待たずに中止すること。"""
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 60)
    repository.delete_signal("off")

    with pytest.raises(jobs.AlarmAborted):
        jobs.run_wakeup_alarm(
            on_signal_name="on", off_signal_name="off",
            interval_seconds=0.01, duration_seconds=60, device_id=None,
        )

    # ON が 1 回飛んだところで OFF が見つからず中止 (5 回は待たない)
    assert len(mock_esp.requests) == 1


def test_alarm_uses_short_lock_timeout(signals, mock_esp, monkeypatch):
    """ロック待ちを短くして 1 拍スキップさせること (待ちを積み上げない)。"""
    captured: list[float] = []
    original = esp32.send_raw

    def spy(host, raw_data, freq=None, *, lock_timeout=None):
        captured.append(lock_timeout)
        return original(host, raw_data, freq, lock_timeout=lock_timeout)

    monkeypatch.setattr(jobs.esp32, "send_raw", spy)

    jobs.run_wakeup_alarm(
        on_signal_name="on", off_signal_name="off",
        interval_seconds=0.05, duration_seconds=1, device_id=None,
    )

    assert captured
    assert all(t == 0.1 for t in captured), captured
    assert all(t < config.ESP32_LOCK_TIMEOUT for t in captured)

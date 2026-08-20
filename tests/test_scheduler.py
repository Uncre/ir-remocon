"""``scheduler.py`` のテスト。**不具合 D の回帰ガードがここにある。**

旧実装はスケジューラのスレッドが死んでも API が 200 を返し続け、予約が
数ヶ月間 1 件も発火していないことに誰も気づけなかった。
「死んだら health に出る」ことを機械的に固定する。
"""

from __future__ import annotations

import time as time_module
from datetime import date, datetime, time, timedelta

import pytest

from ir_remocon.app import config, jobs, repository, scheduler


@pytest.fixture
def started(temp_db):
    """一時 DB / 一時 jobs.db でスケジューラを起動する。"""
    sched = scheduler.start_scheduler()
    yield sched
    scheduler.shutdown_scheduler(wait=False)


def _signal(name: str = "light_on") -> str:
    repository.create_signal(name, [9000, 4500, 560])
    return name


def _tomorrow() -> date:
    return (datetime.now() + timedelta(days=1)).date()


# -----------------------------------------------------------------------------
# トリガ生成 (旧実装で 21 行 x 2 に複製されていた部分)
# -----------------------------------------------------------------------------
def test_build_trigger_once():
    trigger = scheduler.build_trigger("once", time(7, 30), execute_date=_tomorrow())
    assert type(trigger).__name__ == "DateTrigger"
    assert trigger.run_date.hour == 7
    assert trigger.run_date.tzinfo is not None


def test_build_trigger_daily():
    trigger = scheduler.build_trigger("daily", time(8, 5))
    assert type(trigger).__name__ == "CronTrigger"
    assert str(trigger) == "cron[hour='8', minute='5']"


def test_build_trigger_weekly():
    trigger = scheduler.build_trigger("weekly", time(8, 5), repeat_days=["mon", "tue"])
    assert "day_of_week='mon,tue'" in str(trigger)


def test_build_trigger_rejects_past_date():
    """過去日時の予約は作らせない。

    misfire_grace_time 次第で即発火するか黙って捨てられるかが変わり、どちらも
    ユーザには「予約したのに動かなかった」に見える。フロントの UTC 日付バグ
    (不具合 F) はまさにこれを踏んでいた。
    """
    with pytest.raises(scheduler.InvalidSchedule) as excinfo:
        scheduler.build_trigger("once", time(0, 0), execute_date=date(2020, 1, 1))
    assert excinfo.value.http_status == 400


def test_build_trigger_rejects_unknown_repeat_type():
    with pytest.raises(scheduler.InvalidSchedule):
        scheduler.build_trigger("hourly", time(8, 0))


# -----------------------------------------------------------------------------
# 説明文の生成 (trigger.fields のインデックス直参照をやめた部分)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"repeat_type": "daily", "execute_time": time(8, 5)}, "毎日 @ 08:05"),
        (
            {"repeat_type": "weekly", "execute_time": time(8, 5), "repeat_days": ["mon", "sun"]},
            "毎週 [月,日] @ 08:05",
        ),
        (
            {"repeat_type": "once", "execute_time": time(7, 0), "execute_date": date(2026, 9, 1)},
            "一回のみ @ 2026-09-01 07:00",
        ),
    ],
)
def test_describe_schedule(kwargs, expected):
    assert scheduler.describe_schedule(**kwargs) == expected


# -----------------------------------------------------------------------------
# ジョブ登録
# -----------------------------------------------------------------------------
def test_add_signal_job_stores_device_id_not_host(started):
    """★ 不具合 E の回帰ガード: ジョブ引数に host を焼き付けないこと。"""
    _signal()
    result = scheduler.add_signal_job(
        signal_name="light_on", device_id=1, execute_time=time(8, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )

    job = started.get_job(result["id"])
    assert job.kwargs["device_id"] == 1
    assert job.args == ()
    # host / IP がジョブのどこにも入っていないこと
    assert "192.168" not in repr(job.kwargs)
    assert "host" not in job.kwargs


def test_add_signal_job_keeps_device_id_none(started):
    """既定機器の予約は None のまま保存する (既定を切り替えたら追従させるため)。"""
    _signal()
    result = scheduler.add_signal_job(
        signal_name="light_on", device_id=None, execute_time=time(8, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )
    assert started.get_job(result["id"]).kwargs["device_id"] is None


def test_add_signal_job_rejects_unknown_signal(started):
    with pytest.raises(repository.NotFound):
        scheduler.add_signal_job(
            signal_name="nope", device_id=None, execute_time=time(8, 0),
            execute_date=None, repeat_type="daily", repeat_days=None,
        )


def test_add_signal_job_rejects_unknown_device(started):
    _signal()
    with pytest.raises(repository.NotFound):
        scheduler.add_signal_job(
            signal_name="light_on", device_id=999, execute_time=time(8, 0),
            execute_date=None, repeat_type="daily", repeat_days=None,
        )


def test_add_job_without_scheduler_raises_503(temp_db):
    _signal()
    with pytest.raises(scheduler.SchedulerNotRunning) as excinfo:
        scheduler.add_signal_job(
            signal_name="light_on", device_id=None, execute_time=time(8, 0),
            execute_date=None, repeat_type="daily", repeat_days=None,
        )
    assert excinfo.value.http_status == 503


def test_remove_unknown_job_raises_404(started):
    with pytest.raises(scheduler.JobNotFound) as excinfo:
        scheduler.remove_job("does-not-exist")
    assert excinfo.value.http_status == 404


# -----------------------------------------------------------------------------
# 一覧 (不具合 H)
# -----------------------------------------------------------------------------
def test_list_jobs_tolerates_none_next_run(started):
    """``next_run`` が None のジョブが混ざっても 500 にならず末尾に来ること。

    旧実装は naive/aware の比較で TypeError になり、予約一覧が 500 だった。
    """
    _signal()
    paused = scheduler.add_signal_job(
        signal_name="light_on", device_id=None, execute_time=time(3, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )
    scheduler.add_signal_job(
        signal_name="light_on", device_id=None, execute_time=time(4, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )
    started.pause_job(paused["id"])

    listed = scheduler.list_jobs()

    assert len(listed) == 2
    assert listed[-1]["id"] == paused["id"]
    assert listed[-1]["next_run"] is None


def test_list_jobs_survives_deleted_device(started):
    """機器を削除しても一覧が壊れないこと (1 件のダングリングで全体を落とさない)。"""
    _signal()
    repository.create_device("spare", "192.168.1.5")
    result = scheduler.add_signal_job(
        signal_name="light_on", device_id=2, execute_time=time(8, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )
    repository.delete_device(2)

    listed = scheduler.list_jobs()

    assert listed[0]["id"] == result["id"]
    assert listed[0]["device_name"] == "(削除済み id=2)"


def test_list_jobs_labels_default_device(started):
    _signal()
    scheduler.add_signal_job(
        signal_name="light_on", device_id=None, execute_time=time(8, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )
    assert scheduler.list_jobs()[0]["device_name"] == "esp32 (既定)"


def test_list_jobs_exposes_description_and_kind(started):
    _signal("on")
    _signal("off")
    scheduler.add_wakeup_job(
        on_signal_name="on", off_signal_name="off", interval_seconds=1.0,
        duration_seconds=60, device_id=None, execute_time=time(6, 30),
        execute_date=None, repeat_type="weekly", repeat_days=["mon"],
    )
    job = scheduler.list_jobs()[0]
    assert job["kind"] == "wakeup"
    assert job["schedule_description"] == "毎週 [月] @ 06:30"
    assert job["name"] == "目覚まし: on/off"


def test_heartbeat_job_is_not_listed(started):
    """内部ハートビートが予約一覧や job_count に混ざらないこと。"""
    assert scheduler.list_jobs() == []
    assert scheduler.health()["job_count"] == 0
    # 'internal' ジョブストアには存在している
    assert started.get_job(scheduler._HEARTBEAT_JOB_ID, jobstore="internal") is not None


# -----------------------------------------------------------------------------
# ★ 不具合 D の回帰ガード: スケジューラの死を検知できること
# -----------------------------------------------------------------------------
def test_health_reports_running_scheduler(started):
    result = scheduler.health()
    assert result["scheduler_running"] is True
    assert result["ok"] is True
    assert result["db_ok"] is True
    assert result["device_count"] == 1
    assert result["default_device_name"] == "esp32"
    assert result["last_heartbeat"] is not None


def test_health_detects_stale_heartbeat(started, monkeypatch):
    """★ ``scheduler.running`` は状態フラグしか見ておらず、メインループの
    スレッドが例外で死んでも True のままになる (実測確認済み)。
    ハートビートが古ければ不健全と報告すること — これが無いと不具合 D の再来。
    """
    assert started.running is True  # APScheduler 的には「動いている」

    monkeypatch.setattr(
        scheduler, "_last_heartbeat_mono",
        time_module.monotonic() - scheduler.HEARTBEAT_STALE_AFTER_SEC - 1,
    )

    result = scheduler.health()
    assert result["scheduler_running"] is False, "死んだスケジューラを健全と報告している"
    assert result["ok"] is False


def test_health_reports_stopped_scheduler(temp_db):
    result = scheduler.health()
    assert result["scheduler_running"] is False
    assert result["ok"] is False


def test_heartbeat_job_updates_timestamp(started, monkeypatch):
    """ハートビートの中身が実際にタイムスタンプを進めること。"""
    monkeypatch.setattr(scheduler, "_last_heartbeat_mono", 0.0)
    assert scheduler.heartbeat_is_fresh() is False
    scheduler._heartbeat()
    assert scheduler.heartbeat_is_fresh() is True


# -----------------------------------------------------------------------------
# ジョブ失敗の可視化
# -----------------------------------------------------------------------------
def _boom() -> None:
    raise RuntimeError("ジョブが爆発しました")


def test_job_error_is_recorded_in_health(started):
    """例外で落ちたジョブが health の last_job_error に出ること。

    「黙って失敗し続ける」を潰すのが不具合 D の修正の本体。
    """
    assert scheduler.health()["last_job_error"] is None

    started.add_job(
        _boom, "date", run_date=datetime.now() + timedelta(seconds=0.1),
        id="boom", jobstore="internal",
    )

    deadline = time_module.monotonic() + 5
    while scheduler.last_job_error() is None and time_module.monotonic() < deadline:
        time_module.sleep(0.05)

    error = scheduler.health()["last_job_error"]
    assert error is not None, "ジョブの失敗が記録されていない"
    assert "boom" in error


def test_deleted_device_makes_job_fail_loudly(started):
    """機器を削除した予約は、発火時に黙って何もしないのではなく例外になること。"""
    _signal()
    repository.create_device("spare", "192.168.1.5")
    result = scheduler.add_signal_job(
        signal_name="light_on", device_id=2, execute_time=time(8, 0),
        execute_date=None, repeat_type="daily", repeat_days=None,
    )
    job = started.get_job(result["id"])
    repository.delete_device(2)

    with pytest.raises(repository.NotFound):
        job.func(**job.kwargs)


# -----------------------------------------------------------------------------
# アラームの参照/停止
# -----------------------------------------------------------------------------
def test_stop_unknown_alarm_raises_404(started):
    with pytest.raises(scheduler.AlarmNotFound) as excinfo:
        scheduler.stop_alarm("nope")
    assert excinfo.value.http_status == 404


def test_shutdown_notifies_running_alarms(started, monkeypatch, mock_esp):
    """終了時にアラームへ停止を通知すること (最大 30 分ブロックさせない)。"""
    import threading

    _signal("on")
    _signal("off")
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 30)
    thread = threading.Thread(
        target=jobs.run_wakeup_alarm,
        kwargs={
            "on_signal_name": "on", "off_signal_name": "off",
            "interval_seconds": 0.05, "duration_seconds": 30, "device_id": None,
        },
        daemon=True,
    )
    thread.start()
    deadline = time_module.monotonic() + 5
    while not jobs.list_running_alarms() and time_module.monotonic() < deadline:
        time_module.sleep(0.01)
    assert jobs.list_running_alarms(), "アラームが登録されていない"

    scheduler.shutdown_scheduler(wait=False)

    thread.join(timeout=5)
    assert not thread.is_alive(), "終了通知でアラームが止まっていない"

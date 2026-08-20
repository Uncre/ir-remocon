"""予約 API (``/api/schedule*``, ``/api/alarms``, ``/api/health``) のテスト。"""

from __future__ import annotations

import threading
import time as time_module
from datetime import datetime, timedelta

import pytest

from ir_remocon.app import config, jobs, repository, scheduler


@pytest.fixture
def signals(temp_db):
    repository.create_signal("on", [9000, 4500, 560])
    repository.create_signal("off", [9000, 4500, 560])


def _tomorrow() -> str:
    return (datetime.now() + timedelta(days=1)).date().isoformat()


DAILY = {"name": "on", "execute_time": "08:05:00", "repeat_type": "daily"}


# -----------------------------------------------------------------------------
# 単発予約
# -----------------------------------------------------------------------------
def test_create_daily_schedule(client, signals):
    response = client.post("/api/schedule", json=DAILY)

    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert "毎日 @ 08:05" in body["message"]


def test_create_once_schedule(client, signals):
    response = client.post(
        "/api/schedule",
        json={"name": "on", "execute_time": "07:30:00", "execute_date": _tomorrow(),
              "repeat_type": "once"},
    )
    assert response.status_code == 201


def test_create_weekly_schedule(client, signals):
    response = client.post(
        "/api/schedule",
        json={"name": "on", "execute_time": "08:05:00", "repeat_type": "weekly",
              "repeat_days": ["mon", "tue"]},
    )
    assert response.status_code == 201
    assert "毎週 [月,火]" in response.json()["message"]


def test_past_once_schedule_is_rejected(client, signals):
    """過去日時の予約は 400。黙って即発火/破棄させない (不具合 F への防御)。"""
    response = client.post(
        "/api/schedule",
        json={"name": "on", "execute_time": "07:30:00", "execute_date": "2020-01-01",
              "repeat_type": "once"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "InvalidSchedule"


def test_once_without_date_is_422(client, signals):
    response = client.post(
        "/api/schedule", json={"name": "on", "execute_time": "07:30:00", "repeat_type": "once"}
    )
    assert response.status_code == 422


def test_weekly_without_days_is_422(client, signals):
    response = client.post(
        "/api/schedule", json={"name": "on", "execute_time": "07:30:00", "repeat_type": "weekly"}
    )
    assert response.status_code == 422


def test_unknown_signal_is_404(client, temp_db):
    response = client.post("/api/schedule", json=DAILY)
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"


def test_unknown_device_is_404(client, signals):
    response = client.post("/api/schedule", json={**DAILY, "device_id": 999})
    assert response.status_code == 404


def test_legacy_esp32_ip_is_rejected(client, signals):
    """旧フロントのボディを黙って受理して既定機器に予約を作らないこと。"""
    response = client.post("/api/schedule", json={**DAILY, "esp32_ip": "192.168.1.99"})
    assert response.status_code == 422


# -----------------------------------------------------------------------------
# 目覚まし予約
# -----------------------------------------------------------------------------
WAKEUP = {
    "on_signal_name": "on", "off_signal_name": "off",
    "interval_seconds": 1.0, "duration_seconds": 180,
    "execute_time": "06:30:00", "repeat_type": "daily",
}


def test_create_wakeup_schedule(client, signals):
    response = client.post("/api/schedule/wakeup", json=WAKEUP)
    assert response.status_code == 201


def test_wakeup_duration_over_limit_is_422(client, signals, monkeypatch):
    """上限超過は黙って切り詰めず 422 で断ること。"""
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 60)
    response = client.post("/api/schedule/wakeup", json={**WAKEUP, "duration_seconds": 3600})

    assert response.status_code == 422
    assert "60" in str(response.json()["detail"])


def test_wakeup_unknown_off_signal_is_404(client, temp_db):
    repository.create_signal("on", [1, 2, 3])
    response = client.post("/api/schedule/wakeup", json=WAKEUP)
    assert response.status_code == 404


# -----------------------------------------------------------------------------
# 一覧と削除
# -----------------------------------------------------------------------------
def test_list_and_delete_schedules(client, signals):
    job_id = client.post("/api/schedule", json=DAILY).json()["id"]
    client.post("/api/schedule/wakeup", json=WAKEUP)

    listed = client.get("/api/schedules").json()
    assert len(listed) == 2
    entry = next(job for job in listed if job["id"] == job_id)
    assert entry["kind"] == "signal"
    assert entry["name"] == "単発: on"
    assert entry["device_name"] == "esp32 (既定)"
    assert entry["next_run"] is not None

    assert client.delete(f"/api/schedules/{job_id}").status_code == 200
    assert len(client.get("/api/schedules").json()) == 1


def test_list_is_sorted_by_next_run(client, signals):
    late = client.post("/api/schedule", json={**DAILY, "execute_time": "23:59:00"}).json()["id"]
    early = client.post("/api/schedule", json={**DAILY, "execute_time": "00:01:00"}).json()["id"]

    ids = [job["id"] for job in client.get("/api/schedules").json()]

    assert set(ids) == {late, early}
    runs = [job["next_run"] for job in client.get("/api/schedules").json()]
    assert runs == sorted(runs)


def test_delete_unknown_schedule_is_404(client, signals):
    response = client.delete("/api/schedules/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "JobNotFound"


# -----------------------------------------------------------------------------
# 実行中アラーム
# -----------------------------------------------------------------------------
def test_running_alarm_can_be_listed_and_stopped(client, signals, monkeypatch):
    monkeypatch.setattr(config, "MAX_ALARM_DURATION_SEC", 60)
    thread = threading.Thread(
        target=jobs.run_wakeup_alarm,
        kwargs={
            "on_signal_name": "on", "off_signal_name": "off",
            "interval_seconds": 0.05, "duration_seconds": 60, "device_id": None,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time_module.monotonic() + 5
        while not client.get("/api/alarms").json() and time_module.monotonic() < deadline:
            time_module.sleep(0.01)

        alarms = client.get("/api/alarms").json()
        assert len(alarms) == 1
        assert client.get("/api/health").json()["running_alarms"] == 1

        response = client.delete(f"/api/alarms/{alarms[0]['run_id']}")
        assert response.status_code == 200
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert client.get("/api/alarms").json() == []
    finally:
        jobs.stop_all_alarms()
        thread.join(timeout=5)


def test_stop_unknown_alarm_is_404(client, signals):
    response = client.delete("/api/alarms/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "AlarmNotFound"


# -----------------------------------------------------------------------------
# ヘルスチェック (不具合 D)
# -----------------------------------------------------------------------------
def test_health_is_ok(client, signals):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["scheduler_running"] is True
    assert body["db_ok"] is True
    assert body["job_count"] == 0
    assert body["device_count"] == 1
    assert body["default_device_name"] == "esp32"
    assert body["advertise_host"]
    # 設定タブに出す学習コールバックの URL。ESP32 から見えるアドレスかどうかを
    # ユーザが自力で確認できるようにするための情報。
    assert body["callback_base_url"].startswith("http://")


def test_health_counts_jobs(client, signals):
    client.post("/api/schedule", json=DAILY)
    assert client.get("/api/health").json()["job_count"] == 1


def test_health_returns_200_even_when_unhealthy(client, signals, monkeypatch):
    """不健全でも HTTP は 200。フロントは ``ok`` を見て赤バナーを出す。"""
    monkeypatch.setattr(scheduler, "_last_heartbeat_mono", 0.0)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["scheduler_running"] is False

"""予約 (スケジュール) と実行中の目覚ましアラーム。

旧実装との違いは、リクエストボディから ``esp32_ip`` が消えたこと。
``device_id`` (省略時は既定機器) を保存し、host は発火のたびに DB から引く。
``extra="forbid"`` なので旧フロントの ``esp32_ip`` 付きボディは 422 で弾かれる
— 黙って既定機器に予約を作るより、はっきり断る方が安全。

このルータにも ``try/except`` は無い。``JobLookupError`` → 404 の変換は
``scheduler.py`` の責務で、HTTP への写像は ``main.py`` の例外ハンドラが行う。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import scheduler
from ..models import (
    JobOut,
    RunningAlarmOut,
    ScheduleCreatedOut,
    SignalScheduleRequest,
    WakeupScheduleRequest,
)

router = APIRouter(prefix="/api", tags=["schedules"])


@router.post("/schedule", response_model=ScheduleCreatedOut, status_code=201)
def create_signal_schedule(req: SignalScheduleRequest) -> dict:
    """単発送信を予約する。信号・機器が存在しなければ 404。"""
    return scheduler.add_signal_job(
        signal_name=req.name,
        device_id=req.device_id,
        execute_time=req.execute_time,
        execute_date=req.execute_date,
        repeat_type=req.repeat_type,
        repeat_days=req.repeat_days,
    )


@router.post("/schedule/wakeup", response_model=ScheduleCreatedOut, status_code=201)
def create_wakeup_schedule(req: WakeupScheduleRequest) -> dict:
    """目覚まし (ON/OFF の繰り返し) を予約する。"""
    return scheduler.add_wakeup_job(
        on_signal_name=req.on_signal_name,
        off_signal_name=req.off_signal_name,
        interval_seconds=req.interval_seconds,
        duration_seconds=req.duration_seconds,
        device_id=req.device_id,
        execute_time=req.execute_time,
        execute_date=req.execute_date,
        repeat_type=req.repeat_type,
        repeat_days=req.repeat_days,
    )


@router.get("/schedules", response_model=list[JobOut])
def list_schedules() -> list[dict]:
    """予約の一覧 (次回実行の早い順、未定は末尾)。"""
    return scheduler.list_jobs()


@router.delete("/schedules/{job_id}")
def delete_schedule(job_id: str) -> dict:
    scheduler.remove_job(job_id)
    return {"message": f"予約 '{job_id}' を削除しました"}


@router.get("/alarms", response_model=list[RunningAlarmOut])
def list_alarms() -> list[dict]:
    """いま鳴っている目覚ましの一覧。

    プロセスメモリ上の情報なので、サーバを再起動すると消える (＝鳴り止む)。
    """
    return scheduler.list_running_alarms()


@router.delete("/alarms/{run_id}")
def stop_alarm(run_id: str) -> dict:
    """鳴っている目覚ましを止める (旧実装には手段が無かった = 不具合 G)。"""
    scheduler.stop_alarm(run_id)
    return {"message": f"目覚まし '{run_id}' を停止しました"}

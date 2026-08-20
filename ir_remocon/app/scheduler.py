"""予約スケジューラ (APScheduler 3.x) の設定・登録・監視。

このモジュールが潰すのは、調査で確定している 2 件の不具合。

**バグ D: 予約が数ヶ月間 1 件も発火していなかった**
    ``sqlite3.OperationalError: database or disk is full`` で APScheduler の
    スレッドが死に、以後 ``jobs.db`` の 7 件は ``next_run`` が数ヶ月前のまま
    停止していた。にもかかわらず API は 200 を返し続け、**アプリ側に検知する
    仕組みが 1 つも無かった**。ここではイベントリスナとハートビートで
    「黙って死ぬ」経路を塞ぐ。

**バグ E の残り: ジョブに IP が焼き付いていた**
    旧実装は ``args=[name, esp32_ip]`` で作成時の IP を pickle していた。
    ここではジョブ引数に ``device_id`` しか入れず、host は発火のたびに
    :func:`repository.resolve_device` が DB から引く (:mod:`.jobs` 参照)。

.. warning::
   **ジョブ関数の参照はモジュールパスの文字列として pickle される。**
   ``ir_remocon.app.jobs:run_signal_job`` / ``:run_wakeup_alarm`` を改名または
   移動すると、``jobs.db`` にある既存の予約は復元不能になり
   **APScheduler が起動時に黙って削除する** (実際に旧 ``__main__:execute_wakeup_alarm``
   の 7 件がこれで消える)。改名する場合は移行スクリプトを併せて用意すること。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time as time_module
import uuid
from datetime import date, datetime, time as time_type
from typing import Any, Optional, Sequence

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.util import astimezone

from . import config, db, jobs, repository

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# 例外
# -----------------------------------------------------------------------------
# esp32.Esp32Error / repository.RepositoryError と同じ方針。http_status を
# 属性に持たせ、HTTP への変換は main.py の例外ハンドラ 1 箇所だけで行う。
class SchedulerError(Exception):
    """スケジューラ操作の失敗の基底。"""

    http_status = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JobNotFound(SchedulerError):
    http_status = 404


class AlarmNotFound(SchedulerError):
    http_status = 404


class InvalidSchedule(SchedulerError):
    """予約の指定内容が不正 (過去日時など)。"""

    http_status = 400


class SchedulerNotRunning(SchedulerError):
    """スケジューラが動いていないので予約を受け付けられない。

    503 を返すのは意図的。「受け付けたが動かない」より「今は受け付けられない」の
    方が正直で、リトライ可能であることも伝わる。
    """

    http_status = 503


# -----------------------------------------------------------------------------
# ハートビート (バグ D の根治)
# -----------------------------------------------------------------------------
# scheduler.running は状態フラグを見ているだけで、メインループのスレッドが
# 例外で死んでも True を返し続ける (実測確認済み)。まさにバグ D の状況なので、
# これだけを health の根拠にすると同じ見落としをもう一度やることになる。
#
# そこで内部ジョブを定期的に走らせ、「最後に実際に発火した時刻」を健全性の根拠にする。
# ループスレッドとエグゼキュータの両方が生きていないと更新されない。
HEARTBEAT_INTERVAL_SEC = 60
#: これ以上ハートビートが古ければスケジューラは死んでいるとみなす
HEARTBEAT_STALE_AFTER_SEC = 180
_HEARTBEAT_JOB_ID = "_internal_heartbeat"

_last_heartbeat_mono: Optional[float] = None
_last_heartbeat_at: Optional[datetime] = None


def _heartbeat() -> None:
    """内部ジョブ。発火した事実そのものが成果物。"""
    _mark_heartbeat()
    logger.debug("スケジューラのハートビート")


def _mark_heartbeat() -> None:
    global _last_heartbeat_mono, _last_heartbeat_at
    _last_heartbeat_mono = time_module.monotonic()
    _last_heartbeat_at = datetime.now()


def heartbeat_is_fresh() -> bool:
    if _last_heartbeat_mono is None:
        return False
    return (time_module.monotonic() - _last_heartbeat_mono) <= HEARTBEAT_STALE_AFTER_SEC


# -----------------------------------------------------------------------------
# 直近のジョブエラー
# -----------------------------------------------------------------------------
_last_job_error: Optional[str] = None
_error_guard = threading.Lock()


def record_job_error(message: str) -> None:
    """/api/health に出す直近エラーを記録する。"""
    global _last_job_error
    with _error_guard:
        _last_job_error = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"


def last_job_error() -> Optional[str]:
    with _error_guard:
        return _last_job_error


def clear_job_error() -> None:
    global _last_job_error
    with _error_guard:
        _last_job_error = None


def _on_job_event(event: Any) -> None:
    """EVENT_JOB_ERROR / EVENT_JOB_MISSED を記録する。

    ジョブが名指しする機器が削除されていると発火時に ``repository.NotFound`` が
    飛ぶ。ここで拾わないと「予約はあるのに永遠に何も起きない」= バグ D の再来になる。
    """
    if event.code == EVENT_JOB_MISSED:
        message = (
            f"ジョブ {event.job_id} が予定時刻に実行されませんでした "
            f"(予定: {getattr(event, 'scheduled_run_time', '不明')})"
        )
        logger.warning(message)
    else:
        message = f"ジョブ {event.job_id} が失敗しました: {event.exception!r}"
        logger.error("%s\n%s", message, getattr(event, "traceback", ""))
    record_job_error(message)


# -----------------------------------------------------------------------------
# スケジューラ本体 (遅延生成の singleton)
# -----------------------------------------------------------------------------
# esp32.get_client() と同じ作法。uvicorn の workers は 1 固定なので
# プロセスローカルな singleton で問題ない (workers を増やすと同じジョブが
# 複数プロセスで多重発火するため、そもそも増やしてはいけない)。
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_guard = threading.Lock()


def _create_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(url=f"sqlite:///{config.JOBS_DB_PATH}"),
            # ハートビートは永続化しない。jobs.db に混ざると予約一覧にも
            # job_count にも出てしまい、ユーザから見て意味不明な項目になる。
            "internal": MemoryJobStore(),
        },
        executors={"default": ThreadPoolExecutor(max_workers=config.SCHEDULER_MAX_WORKERS)},
        job_defaults={
            # 停止中に溜まった発火を 1 回にまとめる (同じ信号を連打しない)
            "coalesce": True,
            # 同じ予約の多重実行を防ぐ。目覚ましは実行中スレッドを保持するので必須。
            "max_instances": 1,
            # 既定の 1 秒だと再起動や一時的な高負荷で予約が黙って消える
            "misfire_grace_time": config.MISFIRE_GRACE_TIME,
        },
        timezone=config.TIMEZONE,
    )


def start_scheduler() -> BackgroundScheduler:
    """スケジューラを生成して開始する (lifespan から呼ぶ)。

    起動前に ``jobs.db`` の生の行数をログに出す。復元できない古いジョブは
    APScheduler が ERROR ログ付きで削除するので、その前後の数を突き合わせれば
    「何件消えたか」が後から分かる。
    """
    global _scheduler
    with _scheduler_guard:
        if _scheduler is not None:
            return _scheduler
        _log_jobstore_inventory()
        sched = _create_scheduler()
        sched.add_listener(_on_job_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        sched.start()
        # 起動直後を「ハートビートが無いので不健全」と誤判定させない。
        _mark_heartbeat()
        sched.add_job(
            _heartbeat,
            "interval",
            seconds=HEARTBEAT_INTERVAL_SEC,
            id=_HEARTBEAT_JOB_ID,
            jobstore="internal",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        _scheduler = sched
    logger.info(
        "スケジューラを開始しました: jobs=%d / tz=%s / workers=%d",
        len(sched.get_jobs(jobstore="default")),
        config.TIMEZONE,
        config.SCHEDULER_MAX_WORKERS,
    )
    return sched


def _log_jobstore_inventory() -> None:
    """起動前の ``jobs.db`` の中身をログに残す (削除を黙らせないため)。"""
    if not config.JOBS_DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(config.JOBS_DB_PATH)
        try:
            rows = conn.execute(
                "SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("jobs.db の棚卸しに失敗しました (無視して続行します): %s", exc)
        return
    logger.info("起動前の jobs.db には %d 件のジョブがあります: %s", len(rows), [r[0] for r in rows])


def shutdown_scheduler(wait: bool = True) -> None:
    """スケジューラを停止する (lifespan から呼ぶ)。

    **先に実行中の目覚ましアラームへ停止を通知する。** そうしないと
    ``shutdown(wait=True)`` が最大 ``MAX_ALARM_DURATION_SEC`` (既定 30 分)
    ブロックし、Ctrl-C が効かないサーバになる。
    """
    global _scheduler
    with _scheduler_guard:
        sched = _scheduler
        _scheduler = None
    if sched is None:
        return

    stopped = jobs.stop_all_alarms()
    if stopped:
        logger.info("実行中の目覚まし %d 件に停止を通知しました", stopped)
    try:
        sched.shutdown(wait=wait)
    except SchedulerNotRunningError:
        pass
    logger.info("スケジューラを停止しました")


def reset_scheduler() -> None:
    """singleton を捨てる (テスト用)。"""
    shutdown_scheduler(wait=False)
    global _last_heartbeat_mono, _last_heartbeat_at
    _last_heartbeat_mono = None
    _last_heartbeat_at = None
    clear_job_error()


def get_scheduler() -> Optional[BackgroundScheduler]:
    return _scheduler


def require_scheduler() -> BackgroundScheduler:
    sched = _scheduler
    if sched is None or not sched.running:
        raise SchedulerNotRunning(
            "スケジューラが動いていないため予約を操作できません。サーバのログを確認してください"
        )
    return sched


def is_running() -> bool:
    """スケジューラが **実際に** 動いているか。

    ``scheduler.running`` だけでは不十分 (バグ D)。ハートビートの鮮度も見る。
    """
    sched = _scheduler
    if sched is None or not sched.running:
        return False
    return heartbeat_is_fresh()


# -----------------------------------------------------------------------------
# トリガ生成
# -----------------------------------------------------------------------------
#: 曜日コード -> 表示用の日本語
_WEEKDAY_JA = {
    "mon": "月", "tue": "火", "wed": "水", "thu": "木",
    "fri": "金", "sat": "土", "sun": "日",
}


def build_trigger(
    repeat_type: str,
    execute_time: time_type,
    execute_date: Optional[date] = None,
    repeat_days: Optional[Sequence[str]] = None,
) -> BaseTrigger:
    """繰り返し種別からトリガを組み立てる。

    旧実装は単発用と目覚まし用でこの 21 行を丸ごと複製していた。

    必須項目の検証 (once に日付 / weekly に曜日) は ``models.ScheduleBase`` が
    済ませている。ここでの ``raise`` は API 以外の経路から呼ばれた場合の保険。
    """
    tz = astimezone(config.TIMEZONE)

    if repeat_type == "once":
        if execute_date is None:
            raise InvalidSchedule("「一回のみ」の予約には日付が必要です")
        run_date = datetime.combine(execute_date, execute_time).replace(tzinfo=tz)
        # 過去日時を受け付けると misfire_grace_time 次第で即発火するか黙って
        # 捨てられる。どちらもユーザには「予約したのに動かなかった」に見える。
        # (フロント側の UTC 日付バグ = 不具合 F でこれが起きていた)
        if run_date <= datetime.now(tz):
            raise InvalidSchedule(
                f"指定した日時 ({run_date.strftime('%Y-%m-%d %H:%M')}) は既に過ぎています"
            )
        return DateTrigger(run_date=run_date, timezone=tz)

    if repeat_type == "daily":
        return CronTrigger(
            hour=execute_time.hour, minute=execute_time.minute, timezone=tz
        )

    if repeat_type == "weekly":
        if not repeat_days:
            raise InvalidSchedule("「曜日指定」の予約には曜日が必要です")
        return CronTrigger(
            day_of_week=",".join(repeat_days),
            hour=execute_time.hour,
            minute=execute_time.minute,
            timezone=tz,
        )

    raise InvalidSchedule(f"繰り返し種別が不正です: {repeat_type}")


def describe_schedule(
    repeat_type: str,
    execute_time: time_type,
    execute_date: Optional[date] = None,
    repeat_days: Optional[Sequence[str]] = None,
) -> str:
    """画面表示用のスケジュール説明を作る。

    **作成時に文字列へ焼いて ``kwargs["meta"]`` に保存する。** 旧実装は表示のたびに
    ``trigger.fields[5]`` のようなインデックス直参照で組み立てていて、
    APScheduler のバージョンが変わると壊れる作りだった。
    """
    time_str = f"{execute_time.hour:02d}:{execute_time.minute:02d}"
    if repeat_type == "once":
        return f"一回のみ @ {execute_date} {time_str}"
    if repeat_type == "daily":
        return f"毎日 @ {time_str}"
    if repeat_type == "weekly":
        days = ",".join(_WEEKDAY_JA.get(d, d) for d in (repeat_days or []))
        return f"毎週 [{days}] @ {time_str}"
    return f"不明なスケジュール @ {time_str}"


def _build_meta(
    kind: str,
    job_id: str,
    repeat_type: str,
    execute_time: time_type,
    execute_date: Optional[date],
    repeat_days: Optional[Sequence[str]],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "job_id": job_id,
        "repeat_type": repeat_type,
        "time": f"{execute_time.hour:02d}:{execute_time.minute:02d}",
        "date": execute_date.isoformat() if execute_date else None,
        "days": list(repeat_days) if repeat_days else None,
        "description": describe_schedule(repeat_type, execute_time, execute_date, repeat_days),
    }


# -----------------------------------------------------------------------------
# ジョブの登録
# -----------------------------------------------------------------------------
def add_signal_job(
    *,
    signal_name: str,
    device_id: Optional[int],
    execute_time: time_type,
    execute_date: Optional[date],
    repeat_type: str,
    repeat_days: Optional[Sequence[str]],
) -> dict[str, str]:
    """単発送信の予約を登録する。

    信号と機器の存在を **登録時に** 確認する (無ければ 404)。
    「登録はできたが永遠に失敗し続ける予約」を作らせないため。

    ``device_id=None`` は「既定機器」を意味し、**None のまま保存する**。
    既定機器を切り替えればこの予約の宛先も追従する。
    """
    repository.get_signal_raw(signal_name)
    repository.resolve_device(device_id)

    sched = require_scheduler()
    trigger = build_trigger(repeat_type, execute_time, execute_date, repeat_days)
    job_id = str(uuid.uuid4())
    meta = _build_meta("signal", job_id, repeat_type, execute_time, execute_date, repeat_days)

    sched.add_job(
        jobs.run_signal_job,
        trigger,
        id=job_id,
        jobstore="default",
        name=f"単発: {signal_name}",
        # host は入れない。入れた瞬間に不具合 E が復活する。
        kwargs={"signal_name": signal_name, "device_id": device_id, "meta": meta},
    )
    logger.info("予約を登録しました: %s (%s)", job_id, meta["description"])
    return {"id": job_id, "message": f"予約を登録しました ({meta['description']})"}


def add_wakeup_job(
    *,
    on_signal_name: str,
    off_signal_name: str,
    interval_seconds: float,
    duration_seconds: int,
    device_id: Optional[int],
    execute_time: time_type,
    execute_date: Optional[date],
    repeat_type: str,
    repeat_days: Optional[Sequence[str]],
) -> dict[str, str]:
    """目覚まし (ON/OFF の繰り返し) の予約を登録する。"""
    repository.get_signal_raw(on_signal_name)
    repository.get_signal_raw(off_signal_name)
    repository.resolve_device(device_id)

    sched = require_scheduler()
    trigger = build_trigger(repeat_type, execute_time, execute_date, repeat_days)
    job_id = str(uuid.uuid4())
    meta = _build_meta("wakeup", job_id, repeat_type, execute_time, execute_date, repeat_days)

    sched.add_job(
        jobs.run_wakeup_alarm,
        trigger,
        id=job_id,
        jobstore="default",
        name=f"目覚まし: {on_signal_name}/{off_signal_name}",
        kwargs={
            "on_signal_name": on_signal_name,
            "off_signal_name": off_signal_name,
            "interval_seconds": interval_seconds,
            "duration_seconds": duration_seconds,
            "device_id": device_id,
            "meta": meta,
        },
    )
    logger.info("目覚ましの予約を登録しました: %s (%s)", job_id, meta["description"])
    return {"id": job_id, "message": f"目覚ましを登録しました ({meta['description']})"}


def remove_job(job_id: str) -> None:
    sched = require_scheduler()
    try:
        sched.remove_job(job_id, jobstore="default")
    except JobLookupError as exc:
        raise JobNotFound(f"予約 '{job_id}' は存在しません") from exc
    logger.info("予約を削除しました: %s", job_id)


# -----------------------------------------------------------------------------
# ジョブの一覧
# -----------------------------------------------------------------------------
def list_jobs() -> list[dict[str, Any]]:
    """予約の一覧。``next_run`` 昇順、未定 (None) は末尾。

    機器を 1 回だけまとめて引くのは、ジョブごとに DB を叩かないため、かつ
    **削除済みの機器を指すジョブがあっても一覧全体を 500 にしない** ため
    (旧実装は naive/aware 比較の TypeError で一覧が 500 になっていた = 不具合 H)。
    """
    sched = require_scheduler()
    devices = {device["id"]: device for device in repository.list_devices()}
    default_device = next((d for d in devices.values() if d["is_default"]), None)

    result = [_job_to_dict(job, devices, default_device) for job in sched.get_jobs(jobstore="default")]
    # (None かどうか, 値) の順で比較する。None 同士はタプルの前半で等しくなり
    # 後半の比較に進まないので、None と datetime を比較する経路が存在しない。
    result.sort(key=lambda item: (item["next_run"] is None, item["next_run"]))
    return result


def _job_to_dict(
    job: Job,
    devices: dict[int, dict[str, Any]],
    default_device: Optional[dict[str, Any]],
) -> dict[str, Any]:
    kwargs = job.kwargs or {}
    meta = kwargs.get("meta") or {}
    device_id = kwargs.get("device_id")

    kind = meta.get("kind")
    if kind not in ("signal", "wakeup"):
        # meta を持たないジョブへの保険 (手動投入や将来の移行データ)
        kind = "wakeup" if "wakeup" in (job.func_ref or "") else "signal"

    return {
        "id": job.id,
        "kind": kind,
        "name": job.name or job.id,
        "schedule_description": meta.get("description", "不明なスケジュール"),
        # 属性名は next_run_time。APScheduler 3.11.3 に next_run は存在しない。
        "next_run": job.next_run_time,
        "device_id": device_id,
        "device_name": _device_label(device_id, devices, default_device),
    }


def _device_label(
    device_id: Optional[int],
    devices: dict[int, dict[str, Any]],
    default_device: Optional[dict[str, Any]],
) -> str:
    if device_id is None:
        if default_device is None:
            return "(既定機器が未設定)"
        return f"{default_device['name']} (既定)"
    device = devices.get(device_id)
    if device is None:
        # 機器が削除されると発火時に NotFound になる。一覧で先に見えるようにする。
        return f"(削除済み id={device_id})"
    return device["name"]


# -----------------------------------------------------------------------------
# 実行中の目覚ましアラーム
# -----------------------------------------------------------------------------
def list_running_alarms() -> list[dict[str, Any]]:
    return jobs.list_running_alarms()


def stop_alarm(run_id: str) -> None:
    """実行中の目覚ましを中断する。

    旧実装の ``time.sleep`` には中断手段が無く、一度鳴り始めたら
    ``duration_seconds`` の間止められなかった (不具合 G)。
    """
    if not jobs.request_stop(run_id):
        raise AlarmNotFound(f"実行中の目覚まし '{run_id}' は存在しません")


# -----------------------------------------------------------------------------
# ヘルスチェック
# -----------------------------------------------------------------------------
def health() -> dict[str, Any]:
    """アプリの健全性をまとめる。**常に値を返す** (例外を投げない)。

    ここは ``try/except`` を書いてよい数少ない場所。このエンドポイントの成果物は
    「壊れているかどうか」そのものなので、調査中の失敗は結果の一部であって
    API の失敗ではない (``GET /api/devices/{id}/status`` と同じ理由)。
    """
    scheduler_running = is_running()

    db_ok = db.check_health()
    device_count = 0
    default_device_name: Optional[str] = None
    if db_ok:
        try:
            devices = repository.list_devices()
            device_count = len(devices)
            default_device = next((d for d in devices if d["is_default"]), None)
            default_device_name = default_device["name"] if default_device else None
        except Exception:  # noqa: BLE001 - health は何があっても値を返す
            logger.exception("機器一覧の取得に失敗しました (health)")
            db_ok = False

    job_count = 0
    sched = _scheduler
    if sched is not None:
        try:
            job_count = len(sched.get_jobs(jobstore="default"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("ジョブ一覧の取得に失敗しました (health)")
            record_job_error(f"ジョブストアを読めません: {exc}")
            # jobs.db も DB。読めないなら db_ok を偽るべきではない。
            db_ok = False

    return {
        "ok": scheduler_running and db_ok,
        "scheduler_running": scheduler_running,
        "db_ok": db_ok,
        "job_count": job_count,
        "running_alarms": len(jobs.list_running_alarms()),
        "last_job_error": last_job_error(),
        "advertise_host": config.ADVERTISE_HOST,
        "callback_base_url": config.callback_base_url(),
        "device_count": device_count,
        "default_device_name": default_device_name,
        "last_heartbeat": _last_heartbeat_at,
    }

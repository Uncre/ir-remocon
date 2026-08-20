"""スケジューラの発火時に実行される関数。

.. warning::
   **このモジュール名と関数名は改名禁止。**
   APScheduler は ``ir_remocon.app.jobs:run_signal_job`` という文字列で関数を
   pickle する。改名・移動すると ``jobs.db`` の既存の予約は復元不能になり、
   起動時に APScheduler が削除する。

設計上の核心は 1 つ。**ジョブ引数に host を持たせない。**

旧実装は ``args=[signal_name, esp32_ip]`` で作成時の IP を pickle していたため、
ESP32 の DHCP アドレスが変わると既存の予約が全滅した (不具合 E)。実ログでは
数ヶ月前に作られたジョブが今も ``192.168.1.16`` を叩いて ``Connection refused``
を出し続けている。ここでは **発火のたびに** :func:`repository.resolve_device` で
DB から host を引く。機器の host を更新すれば、その瞬間からすべての予約に効く。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from . import config, esp32, repository

logger = logging.getLogger(__name__)

#: 目覚まし中、送信がこの回数だけ連続で失敗したら中止する。
#: 1 回で止めない理由: 一時的なロック競合 (409) で目覚ましが鳴らなくなるのは困る。
#: 無制限にしない理由: 機器の電源が落ちていると 30 分間ログが埋まる。
MAX_CONSECUTIVE_FAILURES = 5

#: 目覚まし中の 1 送信あたりのロック待ち上限。既定の 5 秒を使うと、他の送信と
#: 競合したときに interval を大幅に超えて待ち続けることになる。取れなければ
#: 1 拍スキップする方が「目覚まし」としては正しい。
MAX_ALARM_LOCK_TIMEOUT = 1.0


class AlarmAborted(RuntimeError):
    """目覚ましを異常終了させた。

    APScheduler の ``EVENT_JOB_ERROR`` に乗せて ``/api/health`` の
    ``last_job_error`` に出すために、握り潰さず送出する。
    """


# -----------------------------------------------------------------------------
# 実行中アラームの登録簿 (不具合 G の根治)
# -----------------------------------------------------------------------------
@dataclass
class AlarmRun:
    run_id: str
    job_id: Optional[str]
    on_signal: str
    off_signal: str
    started_at: datetime
    ends_at: datetime
    #: セットされたら即座に中断する。``time.sleep`` の代わりにこれを ``wait`` する。
    stop: threading.Event


_running_alarms: dict[str, AlarmRun] = {}
_alarms_guard = threading.Lock()


def list_running_alarms() -> list[dict[str, Any]]:
    with _alarms_guard:
        runs = list(_running_alarms.values())
    return [
        {
            "run_id": run.run_id,
            "job_id": run.job_id,
            "on_signal": run.on_signal,
            "off_signal": run.off_signal,
            "started_at": run.started_at,
            "ends_at": run.ends_at,
        }
        for run in sorted(runs, key=lambda r: r.started_at)
    ]


def request_stop(run_id: str) -> bool:
    """指定の目覚ましに停止を通知する。存在しなければ ``False``。"""
    with _alarms_guard:
        run = _running_alarms.get(run_id)
    if run is None:
        return False
    run.stop.set()
    logger.info("目覚まし %s に停止を通知しました", run_id)
    return True


def stop_all_alarms() -> int:
    """全アラームに停止を通知する (サーバ終了時)。通知した件数を返す。"""
    with _alarms_guard:
        runs = list(_running_alarms.values())
    for run in runs:
        run.stop.set()
    return len(runs)


def _register(run: AlarmRun) -> None:
    with _alarms_guard:
        _running_alarms[run.run_id] = run


def _unregister(run_id: str) -> None:
    with _alarms_guard:
        _running_alarms.pop(run_id, None)


# -----------------------------------------------------------------------------
# ジョブ本体
# -----------------------------------------------------------------------------
def run_signal_job(
    *,
    signal_name: str,
    device_id: Optional[int] = None,
    meta: Optional[dict] = None,
) -> None:
    """単発送信の予約が発火したときの処理。

    失敗は握り潰さず送出する。APScheduler の ``EVENT_JOB_ERROR`` リスナが
    ログと ``/api/health`` の ``last_job_error`` に載せる。旧実装は
    ``print`` して黙って終わっていたため、予約が動いていないことに
    数ヶ月間誰も気づけなかった (不具合 D)。

    ``meta`` は表示用の情報で、ここでは使わない。既定値を付けてあるのは、
    将来メタの受け渡し方を変えても pickle 済みの古いジョブが
    ``TypeError`` にならないようにするため。
    """
    raw_data = repository.get_signal_raw(signal_name)
    device = repository.resolve_device(device_id)
    logger.info(
        "予約実行: '%s' を %s (%s) へ送信します", signal_name, device["name"], device["host"]
    )
    esp32.send_raw(device["host"], raw_data)


def run_wakeup_alarm(
    *,
    on_signal_name: str,
    off_signal_name: str,
    interval_seconds: float,
    duration_seconds: int,
    device_id: Optional[int] = None,
    meta: Optional[dict] = None,
) -> None:
    """目覚まし: ON/OFF を ``interval`` 間隔で ``duration`` 秒繰り返す。

    旧実装との違いは 3 点。

    1. ``time.sleep`` ではなく :class:`threading.Event` を待つので、
       ``DELETE /api/alarms/{run_id}`` で即座に止められる (不具合 G)。
    2. 継続時間に上限がある (``config.MAX_ALARM_DURATION_SEC``)。
    3. 送信の成否を見る。連続失敗が続けば中止して記録する。
    """
    duration = min(int(duration_seconds), config.MAX_ALARM_DURATION_SEC)
    run = AlarmRun(
        run_id=uuid.uuid4().hex[:12],
        job_id=(meta or {}).get("job_id"),
        on_signal=on_signal_name,
        off_signal=off_signal_name,
        started_at=datetime.now(),
        ends_at=datetime.now() + timedelta(seconds=duration),
        stop=threading.Event(),
    )
    _register(run)
    logger.info(
        "目覚ましを開始します: run_id=%s ON='%s' OFF='%s' 間隔=%.2f秒 継続=%d秒",
        run.run_id, on_signal_name, off_signal_name, interval_seconds, duration,
    )
    try:
        _alarm_loop(run, interval_seconds, duration, device_id)
    finally:
        _unregister(run.run_id)


def _alarm_loop(
    run: AlarmRun,
    interval_seconds: float,
    duration: int,
    device_id: Optional[int],
) -> None:
    deadline = time.monotonic() + duration
    lock_timeout = max(0.1, min(interval_seconds, MAX_ALARM_LOCK_TIMEOUT))
    consecutive_failures = 0
    send_on = True

    while not run.stop.is_set() and time.monotonic() < deadline:
        signal_name = run.on_signal if send_on else run.off_signal

        # 信号と機器は毎回引き直す。実行中に機器の host を変えても追従する
        # (不具合 E の再発防止はここまで徹底する)。
        try:
            raw_data = repository.get_signal_raw(signal_name)
            device = repository.resolve_device(device_id)
        except repository.RepositoryError as exc:
            # 信号や機器が消えているのは一時的な失敗ではない。リトライせず中止する。
            raise AlarmAborted(
                f"目覚まし {run.run_id} を中止しました: {exc}"
            ) from exc

        try:
            esp32.send_raw(device["host"], raw_data, lock_timeout=lock_timeout)
            consecutive_failures = 0
        except esp32.Esp32Error as exc:
            consecutive_failures += 1
            logger.warning(
                "目覚まし %s: '%s' の送信に失敗しました (%d/%d): %s",
                run.run_id, signal_name, consecutive_failures,
                MAX_CONSECUTIVE_FAILURES, exc,
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise AlarmAborted(
                    f"目覚まし {run.run_id} を中止しました: "
                    f"{MAX_CONSECUTIVE_FAILURES} 回連続で送信に失敗しました ({exc})"
                ) from exc

        send_on = not send_on
        run.stop.wait(interval_seconds)

    if run.stop.is_set():
        logger.info("目覚まし %s は中断されました", run.run_id)
    else:
        logger.info("目覚まし %s が終了しました", run.run_id)

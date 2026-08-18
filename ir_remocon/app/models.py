"""API のリクエスト / レスポンススキーマ (Pydantic)。

旧実装は 1 行に複数のクラス定義を詰め込んでいて読めなかったため展開する。
あわせて、旧実装がエンドポイント内で ``if`` を並べて手書きしていた入力検証
(「once なら日付必須」「weekly なら曜日必須」など) をモデル側に移し、
FastAPI が自動で 422 を返すようにした。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

#: 繰り返し種別
RepeatType = Literal["once", "daily", "weekly"]

#: cron の曜日表記。APScheduler の day_of_week にそのまま渡せる形。
VALID_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# -----------------------------------------------------------------------------
# 赤外線信号
# -----------------------------------------------------------------------------
class SignalListItem(BaseModel):
    """一覧表示用 (raw_data は重いので含めない)。"""

    id: int
    name: str


class SignalDetail(SignalListItem):
    """個別取得用。raw_data はマイクロ秒単位の ON/OFF 時間の配列。"""

    raw_data: list[int]


class SignalCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    raw_data: Annotated[list[int], Field(min_length=1)]


class SignalUpdate(BaseModel):
    name: Optional[Annotated[str, Field(min_length=1, max_length=100)]] = None
    raw_data: Optional[Annotated[list[int], Field(min_length=1)]] = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "SignalUpdate":
        if self.name is None and self.raw_data is None:
            raise ValueError("name か raw_data のどちらかを指定してください")
        return self


# -----------------------------------------------------------------------------
# 機器 (ESP32)
# -----------------------------------------------------------------------------
def normalize_host(value: str) -> str:
    """機器ホストの表記ゆれを吸収する。

    設定画面に "http://192.168.1.50/" のように貼り付けられても動くようにする。
    ポート付き ("127.0.0.1:8080") はそのまま残す。
    """
    value = value.strip()
    for prefix in ("http://", "https://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.rstrip("/")


class DeviceBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=50)]
    #: "192.168.1.50" や "esp32.local"、"127.0.0.1:8080" のようにポート付きも許容する。
    host: Annotated[str, Field(min_length=1, max_length=255)]

    _normalize_host = field_validator("host")(normalize_host)


class DeviceCreate(DeviceBase):
    is_default: bool = False


class DeviceUpdate(BaseModel):
    name: Optional[Annotated[str, Field(min_length=1, max_length=50)]] = None
    host: Optional[Annotated[str, Field(min_length=1, max_length=255)]] = None
    is_default: Optional[bool] = None

    _normalize_host = field_validator("host")(normalize_host)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "DeviceUpdate":
        if self.name is None and self.host is None and self.is_default is None:
            raise ValueError("更新する項目を 1 つ以上指定してください")
        return self


class DeviceOut(DeviceBase):
    id: int
    is_default: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DeviceStatusOut(BaseModel):
    """ESP32 の /status を叩いた結果 (設定タブの「接続テスト」用)。"""

    reachable: bool
    detail: Optional[str] = None
    device: Optional[dict] = None


# -----------------------------------------------------------------------------
# 送信
# -----------------------------------------------------------------------------
class SendRequest(BaseModel):
    """送信先の指定。省略時は既定機器へ送る。

    旧実装ではフロントが毎回 ``esp32_ip`` を文字列で送っていたが、
    IP が変わるたびに全画面・全予約が壊れるため device_id 参照に変更した。
    """

    device_id: Optional[int] = None


# -----------------------------------------------------------------------------
# 予約 (スケジュール)
# -----------------------------------------------------------------------------
class ScheduleBase(SendRequest):
    execute_time: time
    execute_date: Optional[date] = None
    repeat_type: RepeatType
    repeat_days: Optional[list[str]] = None

    @field_validator("repeat_days")
    @classmethod
    def _validate_weekdays(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        invalid = [d for d in v if d not in VALID_WEEKDAYS]
        if invalid:
            raise ValueError(
                f"曜日の指定が不正です: {invalid} (使えるのは {list(VALID_WEEKDAYS)})"
            )
        # 重複を除きつつ月→日の順に正規化しておく (表示と cron 式を安定させるため)
        return [d for d in VALID_WEEKDAYS if d in set(v)]

    @model_validator(mode="after")
    def _check_repeat_requirements(self) -> "ScheduleBase":
        """繰り返し種別ごとの必須項目をここで担保する。"""
        if self.repeat_type == "once" and self.execute_date is None:
            raise ValueError("「一回のみ」の予約には execute_date が必要です")
        if self.repeat_type == "weekly" and not self.repeat_days:
            raise ValueError("「曜日指定」の予約には repeat_days が必要です")
        return self


class SignalScheduleRequest(ScheduleBase):
    """単発送信の予約。"""

    name: Annotated[str, Field(min_length=1)]


class WakeupScheduleRequest(ScheduleBase):
    """目覚まし (ON/OFF を交互に繰り返す) の予約。"""

    on_signal_name: Annotated[str, Field(min_length=1)]
    off_signal_name: Annotated[str, Field(min_length=1)]
    interval_seconds: Annotated[float, Field(gt=0, le=60)]
    duration_seconds: Annotated[int, Field(gt=0)]


class JobOut(BaseModel):
    id: str
    kind: Literal["signal", "wakeup"]
    #: 画面表示用のジョブ名 (例: "単発: room_light_turn_on")
    name: str
    #: 画面表示用のスケジュール説明 (例: "毎週 [月,火] @ 08:05")
    schedule_description: str
    next_run: Optional[datetime]
    device_id: Optional[int] = None
    device_name: Optional[str] = None


class ScheduleCreatedOut(BaseModel):
    id: str
    message: str


# -----------------------------------------------------------------------------
# 実行中の目覚ましアラーム
# -----------------------------------------------------------------------------
class RunningAlarmOut(BaseModel):
    run_id: str
    on_signal: str
    off_signal: str
    started_at: datetime
    ends_at: datetime


# -----------------------------------------------------------------------------
# 学習 (受信)
# -----------------------------------------------------------------------------
class LearnStartRequest(SendRequest):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    #: 既存の同名信号を上書きしてよいか。False で既存があれば 409。
    overwrite: bool = False


class LearnSessionOut(BaseModel):
    token: str
    name: str
    status: Literal["pending", "success", "timeout", "error"]
    message: Optional[str] = None
    timeout_seconds: int
    expires_at: datetime


class IRSignalCallback(BaseModel):
    """ESP32 が受信した信号を投げ返してくるときのボディ。"""

    format: str
    freq: int
    data: Annotated[list[int], Field(min_length=1)]


# -----------------------------------------------------------------------------
# ヘルスチェック
# -----------------------------------------------------------------------------
class HealthOut(BaseModel):
    """アプリの健全性。

    旧実装ではスケジューラのスレッドが例外で死んでも API は 200 を返し続け、
    予約が数ヶ月間 1 件も発火していないことに誰も気づけなかった。
    その再発を防ぐためのエンドポイント。
    """

    ok: bool
    scheduler_running: bool
    db_ok: bool
    job_count: int
    running_alarms: int
    last_job_error: Optional[str] = None
    advertise_host: str

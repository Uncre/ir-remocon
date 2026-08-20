"""API のリクエスト / レスポンススキーマ (Pydantic)。

旧実装は 1 行に複数のクラス定義を詰め込んでいて読めなかったため展開する。
あわせて、旧実装がエンドポイント内で ``if`` を並べて手書きしていた入力検証
(「once なら日付必須」「weekly なら曜日必須」など) をモデル側に移し、
FastAPI が自動で 422 を返すようにした。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import config

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


# ``extra="forbid"`` は入力モデルにだけ付ける。``{"is_defualt": true}`` のような
# タイプミスが 200 で通ると「既定にしたつもりが既定になっていない」という静かな
# 食い違いになる (SendRequest と同じ理由)。
# DeviceBase 側には付けない — DeviceOut が継承しているため、列を 1 つ足すたびに
# レスポンス検証が 500 になる罠を仕込むことになる。
class DeviceCreate(DeviceBase):
    model_config = ConfigDict(extra="forbid")

    is_default: bool = False


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class DeviceDeletedOut(BaseModel):
    """機器削除の結果。

    既定機器を削除すると別の機器が自動で既定に昇格する。それを黙ってやると
    「気づかないうちに送信先が変わっている」ことになるので、昇格先を明示して返す。
    """

    message: str
    #: 既定に昇格した機器の id。昇格が起きなければ None。
    new_default_device_id: Optional[int] = None


class DeviceStatusOut(BaseModel):
    """ESP32 の /status を叩いた結果 (設定タブの「接続テスト」用)。

    到達できない場合も **200 でこの形** を返す。このエンドポイントの成果物は
    「到達できたか」そのものなので、到達不可は API の失敗ではなくテストの正常な結果。
    """

    reachable: bool
    #: 実際に叩いた host。設定ミスをユーザが自力で気づけるように返す。
    host: str
    #: 到達できなかった理由 (reachable=True のときは None)。
    detail: Optional[str] = None
    #: ESP32 の /status が返した中身そのまま (reachable=False のときは None)。
    status: Optional[dict] = None


# -----------------------------------------------------------------------------
# 送信
# -----------------------------------------------------------------------------
class SendRequest(BaseModel):
    """送信先の指定。省略時は既定機器へ送る。

    旧実装ではフロントが毎回 ``esp32_ip`` を文字列で送っていたが、
    IP が変わるたびに全画面・全予約が壊れるため device_id 参照に変更した。

    ``extra="forbid"`` は必須。Pydantic v2 の既定 (``extra="ignore"``) のままだと、
    旧フロントが送る ``{"esp32_ip": "192.168.1.99"}`` が **422 にならず受理され**、
    ``device_id=None`` として既定機器に送られてしまう。つまり
    「ユーザは 192.168.1.99 を指定したのに 200 OK が返り、赤外線は別の機器に飛ぶ」
    という、まさにこのリファクタが潰そうとしている「嘘の成功」を新たに作ることになる。
    """

    model_config = ConfigDict(extra="forbid")

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

    @field_validator("duration_seconds")
    @classmethod
    def _limit_duration(cls, v: int) -> int:
        """継続時間の上限を検証する。

        アラームは実行中スケジューラのワーカースレッドを 1 本占有し続けるため、
        無制限だと他の予約を取りこぼす (不具合 G)。**黙って切り詰めず 422 で断る**
        — 「30 分で止まる」ことをユーザが知らないまま 3 時間の目覚ましを
        設定できてしまう方が悪い。

        ``Field(le=...)`` ではなく validator にしているのは、上限が定数ではなく
        設定値 (``IR_MAX_ALARM_DURATION``) だから。クラス定義時ではなく
        検証時に読むので、テストからも監視できる。
        """
        if v > config.MAX_ALARM_DURATION_SEC:
            raise ValueError(
                f"継続時間の上限は {config.MAX_ALARM_DURATION_SEC} 秒です (指定: {v} 秒)"
            )
        return v


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
    #: 発火元の予約 id。手動起動などで不明な場合は None。
    job_id: Optional[str] = None


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
    device_id: Optional[int] = None
    device_name: Optional[str] = None
    #: 受信できた raw データの要素数 (成功時のみ)。
    #: 画面に出すのは Phase 6 の切り分けのため — ESP 側の受信バッファは
    #: ``StaticJsonDocument<2048>`` で最大 1024 要素しか収容できず、
    #: 溢れると**黙って切り詰める**疑いがある。要素数が見えれば気づける。
    raw_length: Optional[int] = None


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
    #: ``scheduler.running`` だけでなく **ハートビートの鮮度** も見た結果。
    #: 状態フラグは、メインループのスレッドが例外で死んでも True のままになる。
    scheduler_running: bool
    #: ir_database.db と jobs.db の両方に読み書きできるか。
    db_ok: bool
    job_count: int
    running_alarms: int
    last_job_error: Optional[str] = None
    advertise_host: str
    #: ESP32 に実際に渡している学習コールバックの URL ベース。
    #: 設定タブに出す。ここが LAN 側の IP になっていないと学習は必ず失敗するが、
    #: 旧実装では画面のどこにも出ていなかったのでユーザに切り分けようが無かった。
    callback_base_url: str = ""
    #: 機器が 0 台になると送信も予約も全滅するので、設定ミスの自己診断用に出す。
    device_count: int = 0
    default_device_name: Optional[str] = None
    #: 最後にスケジューラの内部ジョブが発火した時刻。
    last_heartbeat: Optional[datetime] = None

"""学習 (赤外線信号の受信) セッションの管理。

``jobs.py`` が実行中アラームの登録簿を持つのと同じ層。**ルータを薄く保つため、
ESP32 への受信モード切り替えまでこのモジュールが行う。**

学習は「開始」と「結果」が別のリクエストで届く非同期フローになっている。

.. code-block:: text

   ブラウザ ──POST /api/learn──▶ サーバ ──PUT /mode──▶ ESP32
                                   │                      │
                                   │◀─POST /api/callback/ir_signal/{token}─┘
   ブラウザ ──GET /api/learn/{token}──▶ (pending / success / timeout / error)

旧実装はこの結果を待つ手段が無く、フロントが ``setTimeout(refreshAll, 16000)`` で
「16 秒経ったからたぶん終わっただろう」と当てずっぽうに判定していた。成否は
一切分からず、失敗しても画面には何も出なかった。

設計上の要点が 3 つある。

1. **コールバック URL には信号名ではなくトークンを入れる。**
   ファームは ``callback_url`` をサーバから受け取ってそのまま叩くだけ
   (``esp/temp.ino`` の ``http.begin(callbackUrl)``) なので、パス設計はサーバの自由。
   旧実装の ``/api/callback/ir_signal/{name}`` には 2 つ問題があった —
   日本語やスラッシュを含む信号名で URL が壊れること、そして **誰でも任意の信号を
   上書きできた** こと。トークン制なら進行中のセッション以外は 404 になる。

2. **タイムアウトは遅延評価する。** 現ファームは受信タイムアウト時に何も通知せず
   黙って idle に戻るので、サーバは自分の時計しか根拠を持たない。監視スレッドを
   立てる価値は無く、参照されたときに ``expires_at`` と比べれば足りる。

3. **期限切れ後に届いたコールバックは破棄する。ただし必ず WARNING に残す。**
   保存してしまうと、UI が既に「タイムアウトしました」と表示した後に信号が
   増える (次の学習と競合する)。一方でこのログは、未解決の疑問
   「学習が時々失敗する原因」を切り分けるための唯一の証拠になる。
   **コールバックが届いていないのか、遅れて届いているのかが区別できる。**
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from . import config, esp32, repository

logger = logging.getLogger(__name__)

#: 完了・失敗したセッションを登録簿に残しておく時間(秒)。
#: 学習直後にフロントがポーリングで結果を取りに来るので即座には消せないが、
#: 永久に残すとメモリが単調増加する。
SESSION_RETENTION_SEC = 600


# -----------------------------------------------------------------------------
# 例外
# -----------------------------------------------------------------------------
# esp32.Esp32Error / repository.RepositoryError / scheduler.SchedulerError と
# 同じ方針。http_status を属性に持たせ、HTTP への変換は main.py の例外ハンドラ
# 1 箇所だけで行う。
class LearnError(Exception):
    """学習セッション操作の失敗の基底。"""

    http_status = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LearnSessionNotFound(LearnError):
    """未知・期限切れ・既に終了したトークン。"""

    http_status = 404


class LearnAlreadyRunning(LearnError):
    """同じ機器で学習が進行中。

    ESP32 は ``currentMode`` 1 本の状態機械なので、2 つ目の受信モード切り替えは
    どのみち機器側から 409 が返る。先にここで断って理由を明示する。
    """

    http_status = 409


# -----------------------------------------------------------------------------
# セッション
# -----------------------------------------------------------------------------
@dataclass
class LearnSession:
    token: str
    name: str
    device_id: int
    device_name: str
    host: str
    overwrite: bool
    started_at: datetime
    expires_at: datetime
    #: 実際に記録された結果。実効ステータスは :attr:`status` を見ること。
    recorded_status: str = "pending"
    message: Optional[str] = None
    #: 受信できた raw データの要素数。Phase 6 で疑っている ESP 側の受信バッファ
    #: 切り詰め (``StaticJsonDocument<2048>`` に最大 1024 要素) の切り分けに使う。
    raw_length: Optional[int] = None

    @property
    def status(self) -> str:
        """実効ステータス。

        ``pending`` のまま期限を過ぎていれば ``timeout`` を返す。ファームは
        タイムアウトを通知してこないので、これがサーバの知りうる唯一の真実。
        """
        if self.recorded_status == "pending" and datetime.now() > self.expires_at:
            return "timeout"
        return self.recorded_status

    @property
    def timeout_seconds(self) -> int:
        return max(0, round((self.expires_at - self.started_at).total_seconds()))

    def as_dict(self) -> dict[str, Any]:
        status = self.status
        message = self.message
        if status == "timeout" and message is None:
            message = "時間内に信号を受信できませんでした"
        return {
            "token": self.token,
            "name": self.name,
            "status": status,
            "message": message,
            "timeout_seconds": self.timeout_seconds,
            "expires_at": self.expires_at,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "raw_length": self.raw_length,
        }


_sessions: dict[str, LearnSession] = {}
_guard = threading.Lock()


def reset() -> None:
    """登録簿をクリアする (テスト用)。"""
    with _guard:
        _sessions.clear()


def _prune_locked(now: datetime) -> None:
    """終了済みで古いセッションを捨てる (``_guard`` 保持中に呼ぶこと)。

    ``pending`` は消さない。まだ結果が届く可能性があるうちに消すと、
    コールバックが「未知のトークン」として弾かれてしまう。
    """
    cutoff = now - timedelta(seconds=SESSION_RETENTION_SEC)
    stale = [
        token
        for token, session in _sessions.items()
        if session.status != "pending" and session.expires_at < cutoff
    ]
    for token in stale:
        del _sessions[token]


def _pending_for_device_locked(device_id: int) -> Optional[LearnSession]:
    for session in _sessions.values():
        if session.device_id == device_id and session.status == "pending":
            return session
    return None


# -----------------------------------------------------------------------------
# 開始
# -----------------------------------------------------------------------------
def start(
    device: dict[str, Any],
    name: str,
    overwrite: bool = False,
    timeout_seconds: Optional[int] = None,
) -> LearnSession:
    """学習を開始する。ESP32 を受信モードに切り替えるところまで行う。

    **順序に意味がある。**

    1. 同名の信号が既にあれば ``overwrite`` を見て **ESP を叩く前に** 断る。
       15 秒待たせた末に「その名前は既にあります」と言うのは最悪の体験。
    2. セッション登録と「同じ機器で学習中か」の判定は同一のロック区間で行う。
       分けると 2 つの同時リクエストが両方とも検査を通過する。
    3. ESP への切り替えに失敗したら **セッションを取り消してから** 例外を伝播する。
       開始できていないものを ``pending`` として残すと、以後その機器で学習が
       始められなくなる (409 が出続ける)。

    失敗は握り潰さず送出する。HTTP への変換は ``main.py`` の例外ハンドラの仕事。
    """
    if timeout_seconds is None:
        timeout_seconds = config.LEARN_TIMEOUT_SEC

    if not overwrite and repository.signal_exists(name):
        raise repository.DuplicateName(
            f"信号 '{name}' は既に存在します (上書きするには overwrite を指定してください)"
        )

    now = datetime.now()
    session = LearnSession(
        token=secrets.token_urlsafe(16),
        name=name,
        device_id=device["id"],
        device_name=device["name"],
        host=device["host"],
        overwrite=overwrite,
        started_at=now,
        expires_at=now + timedelta(seconds=timeout_seconds),
    )

    with _guard:
        _prune_locked(now)
        running = _pending_for_device_locked(session.device_id)
        if running is not None:
            raise LearnAlreadyRunning(
                f"機器 '{session.device_name}' は学習中です "
                f"(信号名: '{running.name}')。終わるまで待つか、少し時間をおいてください"
            )
        _sessions[session.token] = session

    callback_url = f"{config.callback_base_url()}/api/callback/ir_signal/{session.token}"
    logger.info(
        "学習を開始します: name='%s' 機器=%s (%s) token=%s callback=%s",
        name, session.device_name, session.host, session.token, callback_url,
    )
    try:
        esp32.start_receive(session.host, callback_url, timeout_seconds * 1000)
    except Exception:
        with _guard:
            _sessions.pop(session.token, None)
        logger.warning("学習の開始に失敗したためセッションを取り消しました: token=%s", session.token)
        raise
    return session


# -----------------------------------------------------------------------------
# 参照
# -----------------------------------------------------------------------------
def get(token: str) -> LearnSession:
    with _guard:
        session = _sessions.get(token)
    if session is None:
        raise LearnSessionNotFound("この学習セッションは存在しません (期限切れの可能性があります)")
    return session


def list_pending() -> list[LearnSession]:
    """進行中のセッション一覧。

    フロントがページ再読込や別タブから開いたときに「学習中」を復元するために使う。
    復元できないと、受信モード中の ESP に送信して確実に 409 を踏むことになる。
    """
    with _guard:
        sessions = [s for s in _sessions.values() if s.status == "pending"]
    return sorted(sessions, key=lambda s: s.started_at)


# -----------------------------------------------------------------------------
# コールバック
# -----------------------------------------------------------------------------
def complete(
    token: str, raw_data: Sequence[int], freq: Optional[int] = None
) -> LearnSession:
    """ESP32 から届いた信号を保存し、セッションを ``success`` にする。

    :param freq: ESP が報告したキャリア周波数(kHz)。**保存はしない** —
        ``ir_signals`` に周波数の列が無く、送信時は常に
        ``config.DEFAULT_FREQ_KHZ`` を使うため。既定と違う値が来たときだけ
        WARNING に残す。黙って捨てると「学習はできたのに送信しても効かない」
        という切り分けの難しい症状になる。列の追加は Phase 7 の判断に委ねる。
    """
    now = datetime.now()
    with _guard:
        session = _sessions.get(token)
        if session is None:
            logger.warning(
                "未知のトークンへの学習コールバックを破棄しました: token=%s (%d 要素)",
                token, len(raw_data),
            )
            raise LearnSessionNotFound("この学習セッションは存在しません")
        status = session.status
        if status != "pending":
            # ★ここのログは消さないこと。「学習が時々失敗する」の原因が
            #   「コールバックが届いていない」のか「遅れて届いている」のかを
            #   区別できる唯一の証拠になる。
            logger.warning(
                "期限切れの学習セッションにコールバックが届いたため破棄しました: "
                "name='%s' token=%s 状態=%s 期限超過=%.1f秒 受信=%d 要素",
                session.name, token, status,
                (now - session.expires_at).total_seconds(), len(raw_data),
            )
            raise LearnSessionNotFound(
                f"学習セッションは既に終了しています (状態: {status})"
            )

    if freq is not None and freq != config.DEFAULT_FREQ_KHZ:
        logger.warning(
            "学習した信号のキャリア周波数が既定と異なります: name='%s' 受信=%dkHz "
            "既定=%dkHz。送信時は既定値を使うため、この信号は効かない可能性があります",
            session.name, freq, config.DEFAULT_FREQ_KHZ,
        )

    # DB 書き込みはロックの外で行う。SQLite が詰まったときに登録簿全体を
    # 止めてしまうと、他の機器の学習開始まで巻き添えになる。
    try:
        repository.upsert_signal(session.name, list(raw_data))
    except Exception as exc:
        with _guard:
            session.recorded_status = "error"
            session.message = f"受信できましたが保存に失敗しました: {exc}"
        logger.exception("学習した信号の保存に失敗しました: name='%s'", session.name)
        raise

    with _guard:
        # expires_at を過ぎていても success で上書きする。信号は実際に保存された
        # のだから、時計の都合で「タイムアウト」と報告するのは嘘になる。
        session.recorded_status = "success"
        session.raw_length = len(raw_data)
        session.message = f"{len(raw_data)} 要素を受信しました"
    logger.info(
        "学習が完了しました: name='%s' token=%s (%d 要素)",
        session.name, token, len(raw_data),
    )
    return session

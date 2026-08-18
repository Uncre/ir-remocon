"""ESP32 との HTTP 通信レイヤ。

このモジュールが潰すのは、調査で確定している 2 件の体感バグ。

**バグ B: 送信が失敗しても UI に「成功」と出る**
    旧実装は ``execute_ir_send()`` が例外を ``print`` して握り潰し、
    ルータは結果を見ずに常に ``{"status":"ok"}`` を返していた。実ログには
    ``Reason: timed out`` の直後に ``200 OK`` が並んでいる。
    ここでは失敗を必ず型付き例外にして送出する。**戻ってきたら成功** が不変条件。

**バグ C: 連打すると全部タイムアウトする**
    サーバ側に直列化が無く、6 リクエストが同時に ESP32 へ飛んでいた。
    ファーム側も非同期ハンドラ内で ``irsend.sendRaw()`` を同期実行して TCP ごと
    ブロックするため、後続が軒並みタイムアウトする (ファーム側の修正は Phase 6)。
    ここでは **機器ホストごとの Lock + 最小送信間隔** で直列化する。

すべて **同期実装**。FastAPI の ``def`` エンドポイントはスレッドプールで動くので
ブロックして問題なく、Phase 4 の APScheduler ワーカースレッドとも同じコードを
共有できる。async/sync の二重実装を避けるための意図的な選択。

.. warning::
   ロック登録簿はプロセスローカル。**uvicorn の ``workers`` は 1 でなければならない。**
   増やした瞬間にバグ C の修正が丸ごと無効化される。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence

import httpx

from . import config
from .models import normalize_host

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# 例外
# -----------------------------------------------------------------------------
# http_status を例外クラスの属性に持たせている。マッピングを 1 箇所に閉じ込め、
# 例外種別を増やしても main.py を編集せずに済ませるため。
class Esp32Error(Exception):
    """ESP32 通信の失敗の基底。"""

    #: ルータ層のハンドラが返す HTTP ステータス
    http_status = 502
    #: 「赤外線が実際に出たかどうか分からない」ケースを表す。
    #: UI が「失敗しました」と断言してはいけない状況を区別するために使う。
    outcome_unknown = False

    def __init__(self, host: str, message: str, *, esp_status: Optional[int] = None) -> None:
        super().__init__(f"{host}: {message}")
        self.host = host
        self.message = message
        self.esp_status = esp_status


class Esp32Unreachable(Esp32Error):
    """接続そのものが成立しない (接続拒否 / 経路なし / 名前解決失敗)。"""

    http_status = 502


class Esp32BadStatus(Esp32Error):
    """ESP32 が想定外のステータスを返した。"""

    http_status = 502


class Esp32Busy(Esp32Error):
    """機器がビジー。"""

    http_status = 409


class Esp32LocalBusy(Esp32Busy):
    """自サーバ側のロックが取れなかった (別の送信が処理中)。

    :class:`Esp32DeviceBusy` と HTTP ステータスは同じ 409 だが意味が違う。
    こちらは「システムは正常、飽和しただけ」。ログ調査と Phase 4 の health で
    この区別が要るので型を分けている。
    """


class Esp32DeviceBusy(Esp32Busy):
    """ESP32 自身が 409 を返した (受信モード中、または固まっている)。"""


class Esp32Timeout(Esp32Error):
    """応答待ちがタイムアウトした。**送信されたかどうかは不明**。

    現ファームは ``irsend.sendRaw()`` を同期実行し **終わってから** 応答するため、
    読み取りタイムアウトの時点で赤外線は既に出ている可能性が高い。
    「失敗しました」と断言するのは嘘になるので専用の型にしている。
    """

    http_status = 504
    outcome_unknown = True


# -----------------------------------------------------------------------------
# httpx クライアント
# -----------------------------------------------------------------------------
#: ESP32 が返しうる成功ステータス。
#: 200 は現ファーム (送信完了後に応答)、202 は Phase 6 のファーム (キュー投入後に即応答)。
#: 「2xx なら成功」と緩く判定しないのは、このリファクタの主題が
#: 「失敗を成功と言わない」ことだから。想定外の 2xx は Esp32BadStatus にする。
SUCCESS_STATUSES = frozenset({200, 202})

#: 接続エラー時のリトライ前の待機(秒)
_RETRY_BACKOFF = 0.2

_client: Optional[httpx.Client] = None
_client_guard = threading.Lock()


def _build_timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=config.ESP32_CONNECT_TIMEOUT,
        read=read,
        write=config.ESP32_CONNECT_TIMEOUT,
        pool=config.ESP32_CONNECT_TIMEOUT,
    )


def _default_timeout() -> httpx.Timeout:
    return _build_timeout(config.ESP32_READ_TIMEOUT)


def _status_timeout() -> httpx.Timeout:
    # 接続テストで 10 秒画面が固まるのは論外なので、疎通確認だけ短くする。
    return _build_timeout(min(3.0, config.ESP32_READ_TIMEOUT))


def get_client() -> httpx.Client:
    """共有 ``httpx.Client`` を返す (遅延生成)。

    毎回 ``httpx.Client()`` を作っていた旧実装をやめ、タイムアウト設定を
    1 箇所に集約する。``httpx.Client`` 自体はスレッドセーフ。
    """
    global _client
    if _client is None:
        with _client_guard:
            if _client is None:
                _client = httpx.Client(
                    timeout=_default_timeout(),
                    # keep-alive を無効にする (重要)。
                    # ESPAsyncWebServer は接続を積極的に閉じるため、プールに残った
                    # idle 接続を再利用した瞬間に RemoteProtocolError が出る。
                    # それを 502 にすると「1 バイトも送っていないのに送信失敗」と
                    # 報告することになり、新種の「たまに失敗する」を作り込んでしまう。
                    # 代償は LAN 内で TCP ハンドシェイク 1 往復(〜1ms)だけ。
                    # どのみち per-host ロックで直列化しているので再利用の利点は無い。
                    limits=httpx.Limits(max_keepalive_connections=0),
                )
    return _client


def set_client(client: Optional[httpx.Client]) -> None:
    """クライアントを差し替える (テスト用)。``None`` で次回に再生成させる。"""
    global _client
    with _client_guard:
        _client = client


def close_client() -> None:
    """共有クライアントを閉じる (lifespan の shutdown から呼ぶ)。"""
    global _client
    with _client_guard:
        if _client is not None:
            _client.close()
            _client = None


# -----------------------------------------------------------------------------
# 機器ごとの直列化 (バグ C の根治)
# -----------------------------------------------------------------------------
@dataclass
class _HostState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: 直近の送信が終わった時刻 (time.monotonic)
    last_send: float = 0.0


_states: dict[str, _HostState] = {}
_registry_guard = threading.Lock()


def _state_for(host: str) -> _HostState:
    with _registry_guard:
        return _states.setdefault(host, _HostState())


def reset_state() -> None:
    """ロック登録簿をクリアする (テスト用)。"""
    with _registry_guard:
        _states.clear()


@contextmanager
def _send_slot(host: str, lock_timeout: Optional[float]) -> Iterator[None]:
    """機器ホストへの排他アクセスを取り、最小送信間隔を守る。

    非自明な点が 3 つある。

    1. **インターバルの待機はロックの内側で行う。** 外でやると 2 スレッドが同時に
       「まだ 0.3 秒経っていない」を読んで両方が待ち、ロックを取った瞬間に
       連続送信してしまう。内側で計測して初めて間隔が構造的に保証される。
    2. **``last_send`` はリクエスト完了後に打つ** (開始時ではない)。現ファームでは
       応答＝赤外線送信完了なので「発射が終わってから次を始めるまでの実測ギャップ」
       になる。Phase 6 で 202 即応答になっても「キュー投入完了から次まで」に
       意味が劣化するだけで壊れない。
    3. **失敗しても ``last_send`` を更新する。** 失敗直後の ESP はむしろ詰まって
       いる可能性が高く、バックオフの必要性は成功時以上にある。
    """
    if lock_timeout is None:
        lock_timeout = config.ESP32_LOCK_TIMEOUT

    state = _state_for(host)
    if not state.lock.acquire(timeout=lock_timeout):
        raise Esp32LocalBusy(
            host, "この機器への送信が処理中です。少し待ってから再試行してください"
        )
    try:
        wait = config.MIN_SEND_INTERVAL - (time.monotonic() - state.last_send)
        if wait > 0:
            logger.debug("%s: 最小送信間隔のため %.3f 秒待機します", host, wait)
            time.sleep(wait)
        yield
    finally:
        state.last_send = time.monotonic()
        state.lock.release()


# -----------------------------------------------------------------------------
# リクエスト実行と例外分類
# -----------------------------------------------------------------------------
def _request(
    method: str,
    host: str,
    path: str,
    *,
    json_body: Optional[dict[str, Any]] = None,
    timeout: Optional[httpx.Timeout] = None,
) -> httpx.Response:
    """ESP32 に 1 リクエスト投げ、失敗を型付き例外に変換して返す。

    リトライは ``httpx.ConnectError`` のみ 1 回。TCP が張れていないなら赤外線は
    絶対に出ていないので原理的に安全。逆に **読み取りタイムアウトはリトライしない**
    — 現ファームは送信完了後に応答するので、再送はトグル型信号 (照明の ON/OFF 兼用
    ボタン等) の二度打ち＝状態の反転を意味する。

    ``ConnectTimeout`` もリトライしない。これは「ハンドシェイクの完了を待つのを
    諦めた」であって「SYN が届かなかった」ではないため、安全と言い切れない。
    """
    url = f"http://{normalize_host(host)}{path}"
    client = get_client()
    request_timeout = timeout if timeout is not None else _default_timeout()

    last_connect_error: Optional[httpx.ConnectError] = None
    for attempt in range(2):
        try:
            response = client.request(
                method, url, json=json_body, timeout=request_timeout
            )
            break
        except httpx.ConnectError as exc:
            last_connect_error = exc
            if attempt == 0:
                logger.info("%s への接続に失敗しました。1 回だけ再試行します: %s", url, exc)
                time.sleep(_RETRY_BACKOFF)
                continue
            raise Esp32Unreachable(host, f"接続できませんでした: {exc}") from exc
        except httpx.ConnectTimeout as exc:
            raise Esp32Unreachable(host, f"接続がタイムアウトしました: {exc}") from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            raise Esp32Timeout(
                host,
                "応答がありませんでした。赤外線が実際に送信されたかどうかは不明です",
            ) from exc
        except httpx.RequestError as exc:
            raise Esp32Unreachable(host, f"通信に失敗しました: {exc}") from exc
    else:  # pragma: no cover - ループは必ず break か raise で抜ける
        raise Esp32Unreachable(host, f"接続できませんでした: {last_connect_error}")

    if response.status_code in SUCCESS_STATUSES:
        return response

    detail = response.text.strip()[:200]
    if response.status_code == 409:
        raise Esp32DeviceBusy(
            host,
            "機器がビジーです (受信モード中か、前の送信が終わっていません)",
            esp_status=409,
        )
    raise Esp32BadStatus(
        host,
        f"機器が想定外の応答を返しました (HTTP {response.status_code}): {detail}",
        esp_status=response.status_code,
    )


# -----------------------------------------------------------------------------
# 公開 API
# -----------------------------------------------------------------------------
def send_raw(
    host: str,
    raw_data: Sequence[int],
    freq: Optional[int] = None,
    *,
    lock_timeout: Optional[float] = None,
) -> None:
    """赤外線信号の送信を ESP32 に要求する。

    成功時は ``None`` を返し、失敗時は :class:`Esp32Error` 系を送出する。
    「例外が出なければ成功」という不変条件そのものがバグ B の修正内容なので、
    呼び出し側は結果を判定するコードを書かないこと。

    .. note::
       戻ることが保証するのは **「ESP32 がリクエストを受理した」** ことであって、
       赤外線が実際に放射されたことではない (Phase 6 で 202 即応答になると顕著)。
       実発射の確認はファーム側の ``/status`` 拡張 (Phase 6) が担当する。

    :param lock_timeout: ロック待ちの上限(秒)。省略時は
        ``config.ESP32_LOCK_TIMEOUT``。Phase 4 の目覚ましアラームは
        interval が短いので、短い値を渡して 1 拍スキップさせる想定。
    """
    if freq is None:
        freq = config.DEFAULT_FREQ_KHZ
    payload = {"format": "raw", "freq": freq, "data": list(raw_data)}

    with _send_slot(host, lock_timeout):
        logger.info("%s へ赤外線を送信します (freq=%dkHz, %d 要素)", host, freq, len(payload["data"]))
        _request("POST", host, "/ir/send", json_body=payload)
    logger.info("%s への送信を機器が受理しました", host)


def start_receive(
    host: str,
    callback_url: str,
    timeout_ms: Optional[int] = None,
    *,
    lock_timeout: Optional[float] = None,
) -> None:
    """ESP32 を学習 (受信) モードに切り替える。

    :func:`send_raw` と **同じロック** を使う。ESP32 は ``currentMode`` 1 本の
    状態機械なので、自分の送信と学習開始が競合するのを防ぐ必要があるため。

    ただしロックは ESP が 202 を返した時点で解放される。ESP はその後 ``timeout_ms``
    の間受信モードに留まるので、その間の送信は ESP 側から本物の 409 が返り
    :class:`Esp32DeviceBusy` になる。これは正しい挙動 — 15 秒間ロックを保持して
    システム全体を止める方が明確に悪い。UI 側で学習中は送信ボタンを無効化すること。
    """
    if timeout_ms is None:
        timeout_ms = config.LEARN_TIMEOUT_SEC * 1000
    payload = {"mode": "receive", "timeout": timeout_ms, "callback_url": callback_url}

    with _send_slot(host, lock_timeout):
        logger.info(
            "%s を受信モードに切り替えます (timeout=%dms, callback=%s)",
            host, timeout_ms, callback_url,
        )
        _request("PUT", host, "/mode", json_body=payload)
    logger.info("%s が受信モードに入りました", host)


def get_status(host: str) -> dict[str, Any]:
    """ESP32 の ``/status`` を叩いて状態を取得する (設定タブの「接続テスト」用)。

    **ロックを取らない。** 読み取り専用でありどのモードでも応答するし、
    そもそも接続テストは「機器がビジーなときこそ」使いたいから。
    """
    response = _request("GET", host, "/status", timeout=_status_timeout())
    try:
        data = response.json()
    except ValueError as exc:
        raise Esp32BadStatus(host, f"応答が JSON ではありません: {response.text[:200]}") from exc
    if not isinstance(data, dict):
        raise Esp32BadStatus(host, f"応答が JSON オブジェクトではありません: {data!r}")
    return data

"""ESP32 通信レイヤの単体テスト。

バグ C (連打すると全部タイムアウトする) の修正、および
「二度打ちしない」という安全側の約束を機械的に固定する。
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from ir_remocon.app import config, esp32

RAW = [9000, 4500, 560, 1690, 560]


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok"})


# -----------------------------------------------------------------------------
# ペイロードと URL
# -----------------------------------------------------------------------------
def test_send_raw_payload_shape(mock_esp):
    esp32.send_raw("192.168.1.4", RAW, freq=38)

    assert len(mock_esp.requests) == 1
    request = mock_esp.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://192.168.1.4/ir/send"
    assert json.loads(request.content) == {"format": "raw", "freq": 38, "data": RAW}


def test_send_raw_normalizes_host(mock_esp):
    """設定画面に "http://..." や末尾スラッシュが貼られても動くこと。"""
    esp32.send_raw("http://192.168.1.4/", RAW)
    assert str(mock_esp.requests[0].url) == "http://192.168.1.4/ir/send"


def test_send_raw_keeps_port(mock_esp):
    esp32.send_raw("127.0.0.1:8080", RAW)
    assert str(mock_esp.requests[0].url) == "http://127.0.0.1:8080/ir/send"


def test_start_receive_payload_shape(mock_esp):
    mock_esp.handler = lambda req: httpx.Response(202, json={"status": "ok"})
    esp32.start_receive("192.168.1.4", "http://192.168.1.110:8102/cb/x", timeout_ms=15000)

    request = mock_esp.requests[0]
    assert request.method == "PUT"
    assert str(request.url) == "http://192.168.1.4/mode"
    assert json.loads(request.content) == {
        "mode": "receive",
        "timeout": 15000,
        "callback_url": "http://192.168.1.110:8102/cb/x",
    }


def test_get_status_returns_json(mock_esp):
    mock_esp.handler = lambda req: httpx.Response(200, json={"device_mode": "idle"})
    assert esp32.get_status("192.168.1.4") == {"device_mode": "idle"}
    assert str(mock_esp.requests[0].url) == "http://192.168.1.4/status"


# -----------------------------------------------------------------------------
# 例外分類
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, None),
        (202, None),                       # Phase 6 のファーム (キュー投入後に即応答)
        (400, esp32.Esp32BadStatus),
        (409, esp32.Esp32DeviceBusy),
        (500, esp32.Esp32BadStatus),
        (503, esp32.Esp32BadStatus),
        (204, esp32.Esp32BadStatus),       # 想定外の 2xx も成功扱いしない
    ],
)
def test_status_code_classification(mock_esp, status, expected):
    mock_esp.handler = lambda req: httpx.Response(status, json={"m": "x"})
    if expected is None:
        esp32.send_raw("h", RAW)
    else:
        with pytest.raises(expected):
            esp32.send_raw("h", RAW)


def test_connect_error_becomes_unreachable(mock_esp):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    mock_esp.handler = handler
    with pytest.raises(esp32.Esp32Unreachable):
        esp32.send_raw("h", RAW)


def test_read_timeout_becomes_timeout_with_unknown_outcome(mock_esp):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    mock_esp.handler = handler
    with pytest.raises(esp32.Esp32Timeout) as excinfo:
        esp32.send_raw("h", RAW)
    assert excinfo.value.outcome_unknown is True
    assert excinfo.value.http_status == 504


def test_connect_timeout_becomes_unreachable(mock_esp):
    def handler(request):
        raise httpx.ConnectTimeout("connect timed out", request=request)

    mock_esp.handler = handler
    with pytest.raises(esp32.Esp32Unreachable):
        esp32.send_raw("h", RAW)


def test_remote_protocol_error_becomes_unreachable(mock_esp):
    def handler(request):
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    mock_esp.handler = handler
    with pytest.raises(esp32.Esp32Unreachable):
        esp32.send_raw("h", RAW)


def test_bad_status_carries_esp_status(mock_esp):
    mock_esp.handler = lambda req: httpx.Response(500, text="boom")
    with pytest.raises(esp32.Esp32BadStatus) as excinfo:
        esp32.send_raw("h", RAW)
    assert excinfo.value.esp_status == 500
    assert excinfo.value.host == "h"


# -----------------------------------------------------------------------------
# リトライ (安全側の要)
# -----------------------------------------------------------------------------
def test_read_timeout_is_not_retried(mock_esp):
    """読み取りタイムアウトでリトライしないこと。

    現ファームは ``irsend.sendRaw()`` を **同期実行してから** 応答するため、
    タイムアウト時点で赤外線は既に出ている可能性が高い。ここで再送すると
    トグル型信号 (照明の ON/OFF 兼用ボタン等) を二度打ちして
    **状態を反転させてしまう**。これがリトライを禁じる実質的な理由。
    """
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    mock_esp.handler = handler
    with pytest.raises(esp32.Esp32Timeout):
        esp32.send_raw("h", RAW)
    assert len(mock_esp.requests) == 1


def test_connect_error_is_retried_once(mock_esp, monkeypatch):
    """接続エラーは 1 回だけ再試行する。TCP が張れていないなら赤外線は出ていない。"""
    monkeypatch.setattr(esp32, "_RETRY_BACKOFF", 0.0)

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    mock_esp.handler = handler
    with pytest.raises(esp32.Esp32Unreachable):
        esp32.send_raw("h", RAW)
    assert len(mock_esp.requests) == 2


def test_connect_error_recovers_on_retry(mock_esp, monkeypatch):
    monkeypatch.setattr(esp32, "_RETRY_BACKOFF", 0.0)

    def handler(request):
        if len(mock_esp.requests) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"status": "ok"})

    mock_esp.handler = handler
    esp32.send_raw("h", RAW)          # 例外が出なければ成功
    assert len(mock_esp.requests) == 2


def test_bad_status_is_not_retried(mock_esp):
    mock_esp.handler = lambda req: httpx.Response(500)
    with pytest.raises(esp32.Esp32BadStatus):
        esp32.send_raw("h", RAW)
    assert len(mock_esp.requests) == 1


# -----------------------------------------------------------------------------
# 直列化 (バグ C の根治)
# -----------------------------------------------------------------------------
class _ConcurrencyProbe:
    """ハンドラ内の同時実行数を数える。

    壁時計ではなく同時実行数で判定するのは、Windows や高負荷環境で
    flaky にならないようにするため。
    """

    def __init__(self, hold: float = 0.05) -> None:
        self.hold = hold
        self.lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.starts: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
            self.starts.append(time.monotonic())
        try:
            time.sleep(self.hold)
        finally:
            with self.lock:
                self.current -= 1
        return httpx.Response(200, json={"status": "ok"})


def _run_threads(target, count: int) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrapper() -> None:
        try:
            target()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=wrapper) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_send_raw_is_serialized_per_host(mock_esp, monkeypatch):
    """同一機器への 10 連打が直列化され、かつ全件成功すること。

    旧実装ではこれが同時に飛んで ESP を詰まらせ、全部タイムアウトしていた
    (ログに 6 リクエスト同時 → 全部 timed out の実例がある)。
    """
    monkeypatch.setattr(config, "ESP32_LOCK_TIMEOUT", 30.0)
    probe = _ConcurrencyProbe()
    mock_esp.handler = probe

    errors = _run_threads(lambda: esp32.send_raw("192.168.1.4", RAW), 10)

    assert errors == []
    assert probe.max_seen == 1, "同一機器への送信が直列化されていない"
    assert len(mock_esp.requests) == 10


def test_different_hosts_are_not_serialized(mock_esp, monkeypatch):
    """機器が違えば直列化しない (1 台の遅延が他機器を巻き込まない)。"""
    monkeypatch.setattr(config, "ESP32_LOCK_TIMEOUT", 30.0)
    probe = _ConcurrencyProbe(hold=0.2)
    mock_esp.handler = probe

    barrier = threading.Barrier(2)

    def send(host: str) -> None:
        barrier.wait(timeout=5)
        esp32.send_raw(host, RAW)

    threads = [
        threading.Thread(target=send, args=("192.168.1.4",)),
        threading.Thread(target=send, args=("192.168.1.5",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert probe.max_seen == 2


def test_min_send_interval_is_enforced(mock_esp, monkeypatch):
    """同一機器への送信間隔が MIN_SEND_INTERVAL 以上になること。"""
    monkeypatch.setattr(config, "MIN_SEND_INTERVAL", 0.3)
    probe = _ConcurrencyProbe(hold=0.0)
    mock_esp.handler = probe

    for _ in range(3):
        esp32.send_raw("192.168.1.4", RAW)

    gaps = [b - a for a, b in zip(probe.starts, probe.starts[1:])]
    assert len(gaps) == 2
    # スケジューラの粒度を考慮して少しだけ許容する
    assert all(gap >= 0.3 - 0.05 for gap in gaps), gaps


def test_min_send_interval_does_not_apply_across_hosts(mock_esp, monkeypatch):
    monkeypatch.setattr(config, "MIN_SEND_INTERVAL", 0.3)
    probe = _ConcurrencyProbe(hold=0.0)
    mock_esp.handler = probe

    started = time.monotonic()
    esp32.send_raw("host-a", RAW)
    esp32.send_raw("host-b", RAW)
    assert time.monotonic() - started < 0.3


def test_lock_timeout_raises_local_busy(mock_esp, monkeypatch):
    """ロックが取れなければ待ち続けずに Esp32LocalBusy を投げること。"""
    monkeypatch.setattr(config, "ESP32_LOCK_TIMEOUT", 0.05)
    release = threading.Event()
    entered = threading.Event()

    def handler(request):
        entered.set()
        release.wait(timeout=5)
        return httpx.Response(200, json={"status": "ok"})

    mock_esp.handler = handler
    blocker = threading.Thread(target=lambda: esp32.send_raw("192.168.1.4", RAW))
    blocker.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(esp32.Esp32LocalBusy) as excinfo:
            esp32.send_raw("192.168.1.4", RAW)
        assert excinfo.value.http_status == 409
    finally:
        release.set()
        blocker.join(timeout=5)


def test_lock_timeout_override_per_call(mock_esp, monkeypatch):
    """呼び出しごとに lock_timeout を上書きできること (Phase 4 のアラーム用)。"""
    monkeypatch.setattr(config, "ESP32_LOCK_TIMEOUT", 30.0)
    release = threading.Event()
    entered = threading.Event()

    def handler(request):
        entered.set()
        release.wait(timeout=5)
        return httpx.Response(200, json={"status": "ok"})

    mock_esp.handler = handler
    blocker = threading.Thread(target=lambda: esp32.send_raw("h", RAW))
    blocker.start()
    try:
        assert entered.wait(timeout=5)
        started = time.monotonic()
        with pytest.raises(esp32.Esp32LocalBusy):
            esp32.send_raw("h", RAW, lock_timeout=0.05)
        assert time.monotonic() - started < 1.0, "呼び出し側の lock_timeout が効いていない"
    finally:
        release.set()
        blocker.join(timeout=5)


def test_get_status_does_not_take_the_lock(mock_esp, monkeypatch):
    """接続テストは機器がビジーなときこそ使いたいので、ロックを取らないこと。"""
    monkeypatch.setattr(config, "ESP32_LOCK_TIMEOUT", 30.0)
    release = threading.Event()
    entered = threading.Event()

    def handler(request):
        if request.url.path == "/status":
            return httpx.Response(200, json={"device_mode": "send"})
        entered.set()
        release.wait(timeout=5)
        return httpx.Response(200, json={"status": "ok"})

    mock_esp.handler = handler
    blocker = threading.Thread(target=lambda: esp32.send_raw("h", RAW))
    blocker.start()
    try:
        assert entered.wait(timeout=5)
        assert esp32.get_status("h") == {"device_mode": "send"}
    finally:
        release.set()
        blocker.join(timeout=5)


def test_failure_still_updates_last_send(mock_esp, monkeypatch):
    """送信が失敗しても次の送信までバックオフすること。

    失敗直後の ESP はむしろ詰まっている可能性が高い。
    """
    monkeypatch.setattr(config, "MIN_SEND_INTERVAL", 0.3)
    calls: list[float] = []

    def handler(request):
        calls.append(time.monotonic())
        return httpx.Response(500)

    mock_esp.handler = handler
    for _ in range(2):
        with pytest.raises(esp32.Esp32BadStatus):
            esp32.send_raw("h", RAW)

    assert calls[1] - calls[0] >= 0.3 - 0.05

"""``POST /api/send/{name}`` のテスト。**バグ B の証明がここにある。**

旧実装のログにはこれが並んでいた::

    Error sending 'room_light_turn_off' to 192.168.1.4. Reason: timed out
    INFO: ... "POST /api/send/room_light_turn_off HTTP/1.1" 200 OK

送信が失敗しているのに 200 が返っていた。以下のテストが、失敗が二度と
200 にならないことを機械的に固定する。
"""

from __future__ import annotations

import threading

import httpx
import pytest

from ir_remocon.app import config, repository

RAW = [9000, 4500, 560, 1690]


@pytest.fixture
def signal(temp_db):
    repository.create_signal("light_on", RAW)
    return "light_on"


def _raise(exc_class):
    def handler(request):
        raise exc_class("boom", request=request)
    return handler


# -----------------------------------------------------------------------------
# 成功パス
# -----------------------------------------------------------------------------
def test_send_success(client, mock_esp, signal):
    response = client.post(f"/api/send/{signal}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["host"] == "192.168.1.4"
    assert body["device_id"] == 1
    assert len(mock_esp.requests) == 1


def test_send_accepts_202_from_firmware(client, mock_esp, signal):
    """Phase 6 のファームは 202 (キュー投入後に即応答) を返す。今から受け入れる。"""
    mock_esp.handler = lambda req: httpx.Response(202, json={"status": "ok"})
    assert client.post(f"/api/send/{signal}").status_code == 200


def test_send_with_explicit_device_id(client, mock_esp, signal):
    response = client.post(f"/api/send/{signal}", json={"device_id": 1})
    assert response.status_code == 200


# -----------------------------------------------------------------------------
# バグ B: 失敗が 200 にならないこと
# -----------------------------------------------------------------------------
FAILURE_CASES = [
    ("esp_returns_409", lambda: (lambda req: httpx.Response(409)), 409, "Esp32DeviceBusy"),
    ("esp_returns_400", lambda: (lambda req: httpx.Response(400)), 502, "Esp32BadStatus"),
    ("esp_returns_500", lambda: (lambda req: httpx.Response(500)), 502, "Esp32BadStatus"),
    ("connect_error", lambda: _raise(httpx.ConnectError), 502, "Esp32Unreachable"),
    ("connect_timeout", lambda: _raise(httpx.ConnectTimeout), 502, "Esp32Unreachable"),
    ("read_timeout", lambda: _raise(httpx.ReadTimeout), 504, "Esp32Timeout"),
    ("protocol_error", lambda: _raise(httpx.RemoteProtocolError), 502, "Esp32Unreachable"),
]


@pytest.mark.parametrize(
    ("label", "make_handler", "expected_status", "expected_error"),
    FAILURE_CASES,
    ids=[case[0] for case in FAILURE_CASES],
)
def test_failures_are_reported_honestly(
    client, mock_esp, signal, monkeypatch, label, make_handler, expected_status, expected_error
):
    monkeypatch.setattr("ir_remocon.app.esp32._RETRY_BACKOFF", 0.0)
    mock_esp.handler = make_handler()

    response = client.post(f"/api/send/{signal}")

    # ★ 回帰ガード: 失敗は絶対に 200 にならない
    assert response.status_code != 200, "送信失敗が成功として報告されている (バグ B の再発)"
    assert response.status_code == expected_status
    body = response.json()
    assert body["error"] == expected_error
    assert body["detail"]
    assert body["host"] == "192.168.1.4"


def test_read_timeout_reports_unknown_outcome(client, mock_esp, signal):
    """タイムアウト時は「失敗した」と断言せず「結果不明」と伝えること。

    現ファームは irsend.sendRaw() 完了後に応答するので、実際には
    発射済みの可能性がある。
    """
    mock_esp.handler = _raise(httpx.ReadTimeout)

    response = client.post(f"/api/send/{signal}")

    assert response.status_code == 504
    assert response.json()["outcome_unknown"] is True


def test_other_failures_do_not_claim_unknown_outcome(client, mock_esp, signal, monkeypatch):
    monkeypatch.setattr("ir_remocon.app.esp32._RETRY_BACKOFF", 0.0)
    mock_esp.handler = _raise(httpx.ConnectError)
    assert response_body(client, signal)["outcome_unknown"] is False


def response_body(client, signal):
    return client.post(f"/api/send/{signal}").json()


def test_local_lock_contention_returns_409(client, mock_esp, signal, monkeypatch):
    """連打でロックが取れなかった場合は 409 を返す (無限に待たない)。"""
    monkeypatch.setattr(config, "ESP32_LOCK_TIMEOUT", 0.05)
    release = threading.Event()
    entered = threading.Event()

    def handler(request):
        entered.set()
        release.wait(timeout=5)
        return httpx.Response(200, json={"status": "ok"})

    mock_esp.handler = handler
    blocker = threading.Thread(target=lambda: client.post(f"/api/send/{signal}"))
    blocker.start()
    try:
        assert entered.wait(timeout=5)
        response = client.post(f"/api/send/{signal}")
        assert response.status_code == 409
        assert response.json()["error"] == "Esp32LocalBusy"
    finally:
        release.set()
        blocker.join(timeout=5)


# -----------------------------------------------------------------------------
# 事前検証
# -----------------------------------------------------------------------------
def test_unknown_signal_returns_404(client, mock_esp, temp_db):
    response = client.post("/api/send/does_not_exist")
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"


def test_no_esp_call_when_signal_missing(client, mock_esp, temp_db):
    """存在しない信号名で ESP に無駄な通信をしないこと。"""
    client.post("/api/send/does_not_exist")
    assert len(mock_esp.requests) == 0


def test_unknown_device_returns_404(client, mock_esp, signal):
    response = client.post(f"/api/send/{signal}", json={"device_id": 999})
    assert response.status_code == 404
    assert len(mock_esp.requests) == 0


def test_legacy_esp32_ip_body_is_rejected(client, mock_esp, signal):
    """旧フロントの ``esp32_ip`` を黙って無視して既定機器に送らないこと。

    Pydantic の既定 (extra="ignore") のままだと、ユーザが指定した機器と
    実際の送信先が食い違ったまま 200 が返る — 新種の「嘘の成功」になる。
    """
    response = client.post(f"/api/send/{signal}", json={"esp32_ip": "192.168.1.99"})

    assert response.status_code == 422
    assert len(mock_esp.requests) == 0

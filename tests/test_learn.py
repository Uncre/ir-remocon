"""学習 (信号受信) API のテスト。

旧実装には結果を知る手段が無く「学習が時々失敗する」という体感だけが残っていた。
ここで守りたい不変条件は 3 つ。

1. **開始できていない学習を pending として残さない** (残すとその機器で二度と
   学習を始められなくなる)
2. **期限切れ後に届いたコールバックで信号を書き換えない** (UI が既に
   「タイムアウト」と表示した後に信号が増えるため)
3. **失敗を成功と報告しない** (このリファクタ全体の主題)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from ir_remocon.app import config, learn, repository

RAW = [9000, 4500, 560, 1690, 560]


def _mode_requests(mock_esp) -> list[httpx.Request]:
    return [r for r in mock_esp.requests if r.url.path == "/mode"]


def _callback_url(mock_esp) -> str:
    """ESP に渡した callback_url を取り出す。"""
    import json

    body = json.loads(_mode_requests(mock_esp)[-1].content)
    return body["callback_url"]


def _token_of(mock_esp) -> str:
    return _callback_url(mock_esp).rsplit("/", 1)[-1]


# -----------------------------------------------------------------------------
# 開始
# -----------------------------------------------------------------------------
def test_start_learn_returns_pending_session(client, mock_esp):
    response = client.post("/api/learn", json={"name": "living_on"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["name"] == "living_on"
    assert body["token"]
    assert body["timeout_seconds"] == config.LEARN_TIMEOUT_SEC
    assert body["device_id"] == 1
    assert body["device_name"] == "esp32"
    assert body["raw_length"] is None


def test_start_learn_switches_device_to_receive_mode(client, mock_esp):
    client.post("/api/learn", json={"name": "living_on"})

    requests = _mode_requests(mock_esp)
    assert len(requests) == 1
    assert requests[0].method == "PUT"

    import json

    body = json.loads(requests[0].content)
    assert body["mode"] == "receive"
    assert body["timeout"] == config.LEARN_TIMEOUT_SEC * 1000


def test_callback_url_carries_advertise_host_and_token(client, mock_esp):
    """★ コールバック URL がサーバの LAN アドレスを指していないと学習は必ず失敗する。

    未解決の疑問「学習が時々失敗する原因」の第一候補なので、URL の組み立てを
    テストで固定しておく。
    """
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]

    url = _callback_url(mock_esp)
    assert url.startswith(config.callback_base_url())
    assert config.ADVERTISE_HOST in url
    assert url.endswith(f"/api/callback/ir_signal/{token}")


def test_start_learn_uses_named_device(client, mock_esp):
    repository.create_device("second", "192.168.1.9")
    devices = {d["name"]: d["id"] for d in repository.list_devices()}

    client.post("/api/learn", json={"name": "x", "device_id": devices["second"]})

    assert _mode_requests(mock_esp)[0].url.host == "192.168.1.9"


def test_start_learn_with_unknown_device_is_404(client, mock_esp):
    response = client.post("/api/learn", json={"name": "x", "device_id": 999})

    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"
    assert _mode_requests(mock_esp) == []


# -----------------------------------------------------------------------------
# 上書きの制御 — ESP を叩く前に断ること
# -----------------------------------------------------------------------------
def test_existing_name_without_overwrite_is_409_before_touching_device(client, mock_esp):
    """★ 15 秒待たせた末に「その名前は既にあります」と言わない。"""
    repository.create_signal("living_on", RAW)

    response = client.post("/api/learn", json={"name": "living_on"})

    assert response.status_code == 409
    assert response.json()["error"] == "DuplicateName"
    assert _mode_requests(mock_esp) == [], "ESP を叩いてしまっている"


def test_existing_name_with_overwrite_is_accepted(client, mock_esp):
    repository.create_signal("living_on", RAW)

    response = client.post("/api/learn", json={"name": "living_on", "overwrite": True})

    assert response.status_code == 201
    assert len(_mode_requests(mock_esp)) == 1


def test_overwrite_replaces_raw_data(client, mock_esp):
    repository.create_signal("living_on", [1, 2, 3])
    token = client.post(
        "/api/learn", json={"name": "living_on", "overwrite": True}
    ).json()["token"]

    client.post(
        f"/api/callback/ir_signal/{token}",
        json={"format": "raw", "freq": 38, "data": RAW},
    )

    assert repository.get_signal_raw("living_on") == RAW


# -----------------------------------------------------------------------------
# 同時実行の排他
# -----------------------------------------------------------------------------
def test_second_learn_on_same_device_is_409(client, mock_esp):
    client.post("/api/learn", json={"name": "first"})

    response = client.post("/api/learn", json={"name": "second"})

    assert response.status_code == 409
    assert response.json()["error"] == "LearnAlreadyRunning"
    assert "first" in response.json()["detail"], "何と競合したか分かること"


def test_learn_on_another_device_is_allowed(client, mock_esp):
    """機器が違えば同時に学習できる (排他は機器ごと)。"""
    repository.create_device("second", "192.168.1.9")
    other_id = next(d["id"] for d in repository.list_devices() if d["name"] == "second")

    client.post("/api/learn", json={"name": "first"})
    response = client.post("/api/learn", json={"name": "second", "device_id": other_id})

    assert response.status_code == 201


def test_learn_can_restart_after_previous_session_expired(client, mock_esp, monkeypatch):
    monkeypatch.setattr(config, "LEARN_TIMEOUT_SEC", 0)
    client.post("/api/learn", json={"name": "first"})

    response = client.post("/api/learn", json={"name": "second"})

    assert response.status_code == 201


# -----------------------------------------------------------------------------
# ★ 開始に失敗したセッションを残さないこと
# -----------------------------------------------------------------------------
def test_unreachable_device_returns_502_and_leaves_no_session(client, mock_esp):
    """★ 残すと、その機器では二度と学習を始められなくなる (409 が出続ける)。"""
    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_esp.handler = _refuse

    response = client.post("/api/learn", json={"name": "living_on"})

    assert response.status_code == 502
    assert response.json()["error"] == "Esp32Unreachable"
    assert client.get("/api/learn").json() == []
    # 直後に再試行できること (排他が残っていない)
    mock_esp.handler = lambda req: httpx.Response(202, json={"status": "ok"})
    assert client.post("/api/learn", json={"name": "living_on"}).status_code == 201


def test_busy_device_returns_409_and_leaves_no_session(client, mock_esp):
    """ESP が受信/送信中なら本物の 409 が返る。これも開始できていない。"""
    mock_esp.handler = lambda req: httpx.Response(409, json={"message": "Device is busy."})

    response = client.post("/api/learn", json={"name": "living_on"})

    assert response.status_code == 409
    assert response.json()["error"] == "Esp32DeviceBusy"
    assert client.get("/api/learn").json() == []


# -----------------------------------------------------------------------------
# コールバック (機器 → サーバ)
# -----------------------------------------------------------------------------
def test_callback_saves_signal_and_marks_success(client, mock_esp):
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]

    response = client.post(
        f"/api/callback/ir_signal/{token}",
        json={"format": "raw", "freq": 38, "data": RAW},
    )

    assert response.status_code == 200
    assert response.json()["raw_length"] == len(RAW)
    assert repository.get_signal_raw("living_on") == RAW

    session = client.get(f"/api/learn/{token}").json()
    assert session["status"] == "success"
    assert session["raw_length"] == len(RAW)
    assert "5 要素" in session["message"]


def test_callback_with_unknown_token_is_404(client, mock_esp):
    response = client.post(
        "/api/callback/ir_signal/nope",
        json={"format": "raw", "freq": 38, "data": RAW},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "LearnSessionNotFound"


def test_late_callback_is_rejected_and_does_not_write(client, mock_esp, monkeypatch):
    """★ 期限切れ後に届いたコールバックで信号を作らない。

    UI は既に「タイムアウト」と表示している。ここで黙って信号が増えると、
    ユーザが再学習を始めた場合と競合する。
    """
    monkeypatch.setattr(config, "LEARN_TIMEOUT_SEC", 0)
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]

    response = client.post(
        f"/api/callback/ir_signal/{token}",
        json={"format": "raw", "freq": 38, "data": RAW},
    )

    assert response.status_code == 404
    assert repository.signal_exists("living_on") is False
    assert client.get(f"/api/learn/{token}").json()["status"] == "timeout"


def test_callback_is_rejected_twice(client, mock_esp):
    """2 回目のコールバックは既に success なので弾く。"""
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]
    payload = {"format": "raw", "freq": 38, "data": RAW}
    assert client.post(f"/api/callback/ir_signal/{token}", json=payload).status_code == 200

    assert client.post(f"/api/callback/ir_signal/{token}", json=payload).status_code == 404


def test_callback_with_empty_data_is_422(client, mock_esp):
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]

    response = client.post(
        f"/api/callback/ir_signal/{token}",
        json={"format": "raw", "freq": 38, "data": []},
    )

    assert response.status_code == 422
    assert repository.signal_exists("living_on") is False


def test_success_wins_over_expiry(client, mock_esp):
    """保存できた後に期限を過ぎても success のままであること。

    実際に信号は保存されたのだから、時計の都合で「タイムアウト」と報告するのは嘘。
    """
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]
    client.post(
        f"/api/callback/ir_signal/{token}",
        json={"format": "raw", "freq": 38, "data": RAW},
    )
    session = learn.get(token)
    session.expires_at = datetime.now() - timedelta(seconds=60)

    assert client.get(f"/api/learn/{token}").json()["status"] == "success"


# -----------------------------------------------------------------------------
# 状態の参照
# -----------------------------------------------------------------------------
def test_pending_session_becomes_timeout_after_expiry(client, mock_esp, monkeypatch):
    monkeypatch.setattr(config, "LEARN_TIMEOUT_SEC", 0)
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]

    body = client.get(f"/api/learn/{token}").json()

    assert body["status"] == "timeout"
    assert body["message"] == "時間内に信号を受信できませんでした"


def test_get_unknown_session_is_404(client, mock_esp):
    assert client.get("/api/learn/nope").status_code == 404


def test_list_returns_only_pending_sessions(client, mock_esp):
    token = client.post("/api/learn", json={"name": "living_on"}).json()["token"]
    assert [s["token"] for s in client.get("/api/learn").json()] == [token]

    client.post(
        f"/api/callback/ir_signal/{token}",
        json={"format": "raw", "freq": 38, "data": RAW},
    )

    assert client.get("/api/learn").json() == []


# -----------------------------------------------------------------------------
# 入力の検証
# -----------------------------------------------------------------------------
def test_legacy_esp32_ip_body_is_422(client, mock_esp):
    """旧フロントの ``{"esp32_ip": ...}`` は黙って既定機器に流さない。"""
    response = client.post("/api/learn", json={"name": "x", "esp32_ip": "192.168.1.99"})

    assert response.status_code == 422
    assert _mode_requests(mock_esp) == []


def test_empty_name_is_422(client, mock_esp):
    assert client.post("/api/learn", json={"name": ""}).status_code == 422


@pytest.mark.parametrize("name", ["リビングの照明 ON", "tv/power", "a#b?c"])
def test_awkward_names_work_end_to_end(client, mock_esp, name):
    """★ トークン制の効果。

    旧実装はコールバック URL に信号名をそのまま埋めていたため、スラッシュや
    日本語を含む名前では URL が壊れて学習が成立しなかった。
    """
    token = client.post("/api/learn", json={"name": name}).json()["token"]

    response = client.post(
        f"/api/callback/ir_signal/{token}",
        json={"format": "raw", "freq": 38, "data": RAW},
    )

    assert response.status_code == 200
    assert repository.get_signal_raw(name) == RAW


# -----------------------------------------------------------------------------
# health への露出
# -----------------------------------------------------------------------------
def test_health_exposes_callback_base_url(client, mock_esp):
    """★ 設定タブに出して「ESP に何を教えているか」をユーザに見せるための情報。"""
    body = client.get("/api/health").json()

    assert body["callback_base_url"] == config.callback_base_url()
    assert body["callback_base_url"].startswith("http://")

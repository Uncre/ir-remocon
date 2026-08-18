"""機器 (ESP32) 登録のテスト。**不具合 E の回帰ガードがここにある。**

旧実装は予約ジョブに IP が pickle されて固定されており、ESP の DHCP アドレスが
変わると既存の予約が全滅していた。実ログでは古いジョブが今も ``192.168.1.16`` を
叩いて ``Connection refused`` を出し続けている。

このフェーズの本質は「host は DB の 1 行にしかない」こと。それを機械的に固定する。
"""

from __future__ import annotations

import httpx
import pytest

from ir_remocon.app import db, repository

RAW = [9000, 4500, 560]


@pytest.fixture
def signal(temp_db):
    repository.create_signal("light_on", RAW)
    return "light_on"


def _raise_connect_error(request):
    raise httpx.ConnectError("boom", request=request)


# =============================================================================
# リポジトリ層
# =============================================================================
def test_create_and_read_roundtrip(temp_db):
    created = repository.create_device("living", "192.168.1.50")
    assert created["name"] == "living"
    assert created["host"] == "192.168.1.50"
    assert created["is_default"] is False

    fetched = repository.get_device(created["id"])
    assert fetched == created
    assert len(repository.list_devices()) == 2  # init_db の 1 台 + 今作った 1 台


def test_create_writes_timestamps(temp_db):
    created = repository.create_device("living", "192.168.1.50")
    assert created["created_at"] is not None
    assert created["updated_at"] is not None


def test_create_duplicate_name_raises_409(temp_db):
    with pytest.raises(repository.DuplicateName) as excinfo:
        repository.create_device("esp32", "192.168.1.50")  # init_db が作った名前
    assert excinfo.value.http_status == 409


def test_create_into_empty_table_forces_default(temp_db):
    """機器 0 台からの復旧経路。既定フラグが立たないと送信が警告付きになる。"""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM devices")

    created = repository.create_device("living", "192.168.1.50", is_default=False)

    assert created["is_default"] is True
    assert repository.get_default_device()["id"] == created["id"]


# --- host の正規化 -----------------------------------------------------------
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://192.168.1.50/", "192.168.1.50"),
        ("https://esp32.local", "esp32.local"),
        ("  127.0.0.1:8080  ", "127.0.0.1:8080"),
    ],
)
def test_host_is_normalized_on_write(temp_db, given, expected):
    """DB には常に裸の host を入れる。

    esp32.py のロック登録簿は host 文字列がキーなので、"192.168.1.4" と
    "http://192.168.1.4/" が別エントリになると同一機器への直列化 (バグ C の修正) が
    静かに壊れる。
    """
    created = repository.create_device("living", given)
    assert created["host"] == expected

    updated = repository.update_device(created["id"], host=given)
    assert updated["host"] == expected


# --- is_default の排他制御 ---------------------------------------------------
def test_creating_default_demotes_the_previous_one(temp_db):
    created = repository.create_device("living", "192.168.1.50", is_default=True)

    assert repository.get_device(1)["is_default"] is False
    assert repository.get_default_device()["id"] == created["id"]


def test_updating_default_demotes_the_previous_one(temp_db):
    created = repository.create_device("living", "192.168.1.50")

    repository.update_device(created["id"], is_default=True)

    assert repository.get_device(1)["is_default"] is False
    assert repository.get_default_device()["id"] == created["id"]
    # 既定はちょうど 1 台
    assert sum(d["is_default"] for d in repository.list_devices()) == 1


def test_unsetting_the_default_is_rejected(temp_db):
    """既定を単独で外させない。外すと「既定は無いのに送信は動く」状態になる。"""
    with pytest.raises(repository.ConstraintViolation) as excinfo:
        repository.update_device(1, is_default=False)
    assert excinfo.value.http_status == 409
    assert repository.get_device(1)["is_default"] is True  # DB は無変更


def test_unsetting_default_on_a_non_default_device_is_a_noop(temp_db):
    created = repository.create_device("living", "192.168.1.50")
    updated = repository.update_device(created["id"], is_default=False, name="living2")
    assert updated["is_default"] is False
    assert updated["name"] == "living2"


# --- 更新 --------------------------------------------------------------------
def test_update_host_only_keeps_name_and_default(temp_db):
    updated = repository.update_device(1, host="192.168.1.110")
    assert updated["host"] == "192.168.1.110"
    assert updated["name"] == "esp32"
    assert updated["is_default"] is True


def test_update_touches_updated_at_only(temp_db):
    before = repository.get_device(1)
    repository.update_device(1, host="10.0.0.1")
    after = repository.get_device(1)
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] is not None


def test_update_to_existing_name_raises_409(temp_db):
    created = repository.create_device("living", "192.168.1.50")
    with pytest.raises(repository.DuplicateName) as excinfo:
        repository.update_device(created["id"], name="esp32")
    assert excinfo.value.http_status == 409
    assert repository.get_device(created["id"])["name"] == "living"


def test_update_missing_raises_404(temp_db):
    with pytest.raises(repository.NotFound) as excinfo:
        repository.update_device(999, host="10.0.0.1")
    assert excinfo.value.http_status == 404


# --- 削除 --------------------------------------------------------------------
def test_delete_last_device_is_rejected(temp_db):
    """★ Phase 2 からの申し送り。0 台になると送信が全部死ぬ。"""
    with pytest.raises(repository.ConstraintViolation) as excinfo:
        repository.delete_device(1)
    assert excinfo.value.http_status == 409

    # 機器は残っており、送信先の解決も生きている
    assert len(repository.list_devices()) == 1
    assert repository.resolve_device(None)["id"] == 1


def test_delete_non_default_does_not_promote(temp_db):
    created = repository.create_device("living", "192.168.1.50")
    assert repository.delete_device(created["id"]) is None
    assert repository.get_default_device()["id"] == 1


def test_delete_default_promotes_lowest_id(temp_db):
    """既定を消しても送信経路が残ること。"""
    second = repository.create_device("living", "192.168.1.50")
    third = repository.create_device("bedroom", "192.168.1.51")

    promoted = repository.delete_device(1)  # 既定 (init_db が作った esp32)

    assert promoted == second["id"]
    assert repository.get_default_device()["id"] == second["id"]
    assert repository.get_device(third["id"])["is_default"] is False
    assert sum(d["is_default"] for d in repository.list_devices()) == 1


def test_delete_missing_raises_404(temp_db):
    with pytest.raises(repository.NotFound) as excinfo:
        repository.delete_device(999)
    assert excinfo.value.http_status == 404


# =============================================================================
# HTTP 層
# =============================================================================
def test_crud_roundtrip(client):
    created = client.post(
        "/api/devices", json={"name": "living", "host": "http://192.168.1.50/"}
    )
    assert created.status_code == 201
    device_id = created.json()["id"]
    assert created.json()["host"] == "192.168.1.50"

    assert client.get(f"/api/devices/{device_id}").json()["name"] == "living"
    assert len(client.get("/api/devices").json()) == 2

    updated = client.put(f"/api/devices/{device_id}", json={"host": "192.168.1.51"})
    assert updated.status_code == 200
    assert updated.json()["host"] == "192.168.1.51"

    deleted = client.delete(f"/api/devices/{device_id}")
    assert deleted.status_code == 200
    assert deleted.json()["new_default_device_id"] is None


def test_collection_path_has_no_redirect(client):
    assert client.get("/api/devices", follow_redirects=False).status_code == 200


def test_duplicate_name_returns_409(client):
    response = client.post("/api/devices", json={"name": "esp32", "host": "10.0.0.1"})
    assert response.status_code == 409
    assert response.json()["error"] == "DuplicateName"


def test_delete_last_device_returns_409(client):
    response = client.delete("/api/devices/1")
    assert response.status_code == 409
    assert response.json()["error"] == "ConstraintViolation"
    assert response.json()["detail"]


def test_unsetting_default_returns_409(client):
    response = client.put("/api/devices/1", json={"is_default": False})
    assert response.status_code == 409
    assert response.json()["error"] == "ConstraintViolation"


def test_delete_default_reports_the_promotion(client):
    """昇格を黙ってやらない (送信先が変わったことがレスポンスに出る)。"""
    second = client.post("/api/devices", json={"name": "living", "host": "10.0.0.1"}).json()

    response = client.delete("/api/devices/1")

    assert response.status_code == 200
    assert response.json()["new_default_device_id"] == second["id"]


def test_missing_device_returns_404(client):
    assert client.get("/api/devices/999").status_code == 404
    assert client.put("/api/devices/999", json={"host": "10.0.0.1"}).status_code == 404
    assert client.delete("/api/devices/999").status_code == 404


def test_empty_update_is_422(client):
    assert client.put("/api/devices/1", json={}).status_code == 422


def test_typo_in_field_name_is_422(client):
    """``extra="forbid"`` の効果。

    黙って無視すると「既定にしたつもりが既定になっていない」という静かな
    食い違いになる (SendRequest で潰したのと同じ類のバグ)。
    """
    response = client.post(
        "/api/devices",
        json={"name": "living", "host": "10.0.0.1", "is_defualt": True},
    )
    assert response.status_code == 422
    assert client.put("/api/devices/1", json={"hostname": "10.0.0.1"}).status_code == 422


# -----------------------------------------------------------------------------
# 接続テスト
# -----------------------------------------------------------------------------
def test_status_reports_reachable_device(client, mock_esp):
    mock_esp.handler = lambda req: httpx.Response(
        200, json={"mode": "idle", "send_count": 3}
    )

    response = client.get("/api/devices/1/status")

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["host"] == "192.168.1.4"
    assert body["status"] == {"mode": "idle", "send_count": 3}
    assert body["detail"] is None


def test_status_reports_unreachable_as_200_not_502(client, mock_esp, monkeypatch):
    """到達不可は「テストの正常な結果」。502 にすると操作の失敗と区別できない。"""
    monkeypatch.setattr("ir_remocon.app.esp32._RETRY_BACKOFF", 0.0)
    mock_esp.handler = _raise_connect_error

    response = client.get("/api/devices/1/status")

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is False
    assert body["host"] == "192.168.1.4"
    assert body["detail"]
    assert body["status"] is None


def test_status_for_missing_device_is_404(client, mock_esp):
    """捕まえているのが Esp32Error だけであることの証明 (NotFound は素通り)。"""
    response = client.get("/api/devices/999/status")
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"
    assert len(mock_esp.requests) == 0


# -----------------------------------------------------------------------------
# ★ 不具合 E の回帰ガード
# -----------------------------------------------------------------------------
def test_host_change_takes_effect_immediately(client, mock_esp, signal):
    """host を変えたら次の送信からその機器に飛ぶこと。

    旧実装は予約ジョブに IP が焼き付いていたため、IP が変わると予約が全滅した。
    新実装では host は DB の 1 行にしかなく、送信時に毎回解決される。
    """
    client.post(f"/api/send/{signal}")
    assert mock_esp.requests[-1].url.host == "192.168.1.4"

    assert client.put("/api/devices/1", json={"host": "192.168.1.50"}).status_code == 200

    client.post(f"/api/send/{signal}")
    assert mock_esp.requests[-1].url.host == "192.168.1.50"  # 再起動なしで切り替わる


def test_send_follows_the_new_default_device(client, mock_esp, signal):
    """既定機器を切り替えると、device_id を省略した送信の宛先も変わること。"""
    client.post("/api/devices", json={"name": "living", "host": "192.168.1.51", "is_default": True})

    client.post(f"/api/send/{signal}")

    assert mock_esp.requests[-1].url.host == "192.168.1.51"


def test_send_to_deleted_device_is_404_not_a_silent_default(client, mock_esp, signal):
    """削除済みの機器を指定したら黙って既定に送らないこと。"""
    created = client.post("/api/devices", json={"name": "living", "host": "10.0.0.1"}).json()
    client.delete(f"/api/devices/{created['id']}")

    response = client.post(f"/api/send/{signal}", json={"device_id": created["id"]})

    assert response.status_code == 404
    assert len(mock_esp.requests) == 0

"""信号 CRUD の HTTP 層のテスト。ドメイン例外がステータスに正しく変換されること。"""

from __future__ import annotations

RAW = [9000, 4500, 560]


def test_crud_roundtrip(client):
    created = client.post("/api/signals", json={"name": "light_on", "raw_data": RAW})
    assert created.status_code == 201
    assert created.json()["raw_data"] == RAW

    assert client.get("/api/signals").json() == [{"id": created.json()["id"], "name": "light_on"}]
    assert client.get("/api/signals/light_on").json()["raw_data"] == RAW

    renamed = client.put("/api/signals/light_on", json={"name": "light_off"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "light_off"

    assert client.delete("/api/signals/light_off").status_code == 200
    assert client.get("/api/signals").json() == []


def test_collection_path_has_no_redirect(client):
    """prefix ありのコレクションパスは "" にしてある (307 を出さない)。"""
    response = client.get("/api/signals", follow_redirects=False)
    assert response.status_code == 200


def test_list_is_sorted(client):
    for name in ("zulu", "alpha"):
        client.post("/api/signals", json={"name": name, "raw_data": RAW})
    assert [s["name"] for s in client.get("/api/signals").json()] == ["alpha", "zulu"]


def test_duplicate_name_returns_409(client):
    client.post("/api/signals", json={"name": "a", "raw_data": RAW})
    response = client.post("/api/signals", json={"name": "a", "raw_data": RAW})
    assert response.status_code == 409
    assert response.json()["error"] == "DuplicateName"


def test_rename_collision_returns_409_not_500(client):
    """旧実装ではここが 500 だった (バグ H)。"""
    client.post("/api/signals", json={"name": "a", "raw_data": RAW})
    client.post("/api/signals", json={"name": "b", "raw_data": RAW})
    response = client.put("/api/signals/a", json={"name": "b"})
    assert response.status_code == 409


def test_missing_signal_returns_404(client):
    assert client.get("/api/signals/nope").status_code == 404
    assert client.put("/api/signals/nope", json={"raw_data": RAW}).status_code == 404
    assert client.delete("/api/signals/nope").status_code == 404


def test_empty_update_is_422(client):
    client.post("/api/signals", json={"name": "a", "raw_data": RAW})
    assert client.put("/api/signals/a", json={}).status_code == 422


def test_empty_raw_data_is_422(client):
    assert client.post("/api/signals", json={"name": "a", "raw_data": []}).status_code == 422


# ``/`` の配信は Phase 5 で UI 実装に切り替わった。
# 内容の検証は tests/test_frontend_assets.py に移動している。

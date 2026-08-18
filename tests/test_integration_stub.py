"""``tools/fake_esp32.py`` を実際に起動して、実ソケット経由で検証する。

``MockTransport`` では再現できないものだけをここで見る:

- 閉じたポートへの本物の接続拒否
- ファームが同期ブロックする状況での直列化
- 読み取りタイムアウトの実挙動

既定ではスキップされる (``pyproject.toml`` の ``addopts``)。実行するには::

    uv run pytest -m integration -v
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from ir_remocon.app import config, esp32

# tools/ はパッケージではないので sys.path に足して import する
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fake_esp32  # noqa: E402

pytestmark = pytest.mark.integration


def _bind_socket() -> socket.socket:
    """空きポートに bind 済みのソケットを返す。

    「空きポートを調べて閉じ、あとで bind し直す」やり方は Windows で
    その隙に別プロセスに取られて ``WinError 10048`` になる。ソケットを
    握ったまま uvicorn に渡してレースを消す。
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def _free_port() -> int:
    """接続拒否テスト用に「誰も listen していないポート番号」を得る。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ThreadedServer:
    """uvicorn をバックグラウンドスレッドで起動する (subprocess より速く安定)。"""

    def __init__(self, app) -> None:
        self._socket = _bind_socket()
        self.port = self._socket.getsockname()[1]
        cfg = uvicorn.Config(app, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(
            target=lambda: self.server.run(sockets=[self._socket]), daemon=True
        )

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def __enter__(self) -> "_ThreadedServer":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started, "サーバが起動しませんでした"
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self._socket.close()


def _StubServer(**kwargs) -> _ThreadedServer:
    opts = fake_esp32.Options(host="127.0.0.1", **kwargs)
    return _ThreadedServer(fake_esp32.build_app(opts))


@pytest.fixture(autouse=True)
def _real_client():
    """MockTransport ではなく本物の httpx.Client を使う。"""
    esp32.set_client(None)
    yield
    esp32.close_client()


def test_send_succeeds_against_stub():
    with _StubServer(send_duration=0.05) as stub:
        esp32.send_raw(stub.host, [9000, 4500, 560])
        assert esp32.get_status(stub.host)["send_count"] == 1


def test_closed_port_is_unreachable(monkeypatch):
    """本物の接続拒否経路。スタブを起動しないだけで再現できる。"""
    monkeypatch.setattr(esp32, "_RETRY_BACKOFF", 0.0)
    port = _free_port()
    with pytest.raises(esp32.Esp32Unreachable):
        esp32.send_raw(f"127.0.0.1:{port}", [1, 2, 3])


def test_slow_firmware_causes_timeout_without_retry(monkeypatch):
    """応答が読み取りタイムアウトを超えたら 504 相当。かつ再送しないこと。

    再送するとトグル型信号を二度打ちして状態が反転する。スタブの send_count で
    「1 回しか届いていない」ことを確認する。
    """
    monkeypatch.setattr(config, "ESP32_READ_TIMEOUT", 1.0)
    with _StubServer(send_duration=3.0) as stub:
        with pytest.raises(esp32.Esp32Timeout) as excinfo:
            esp32.send_raw(stub.host, [1, 2, 3])
        assert excinfo.value.outcome_unknown is True

        # スタブ側では送信が完走している = 赤外線は実際に出ている。
        # 「失敗しました」と断言してはいけない状況そのもの。
        time.sleep(2.5)
        assert esp32.get_status(stub.host)["send_count"] == 1


def test_burst_is_serialized_and_all_succeed(monkeypatch):
    """10 連打しても全部成功し、ESP 側で 409 が 1 件も起きないこと。

    旧実装はここで 6 件同時に飛ばして全部タイムアウトさせていた。
    """
    monkeypatch.setattr(config, "ESP32_LOCK_TIMEOUT", 60.0)
    monkeypatch.setattr(config, "MIN_SEND_INTERVAL", 0.0)
    errors: list[BaseException] = []

    with _StubServer(send_duration=0.1) as stub:
        def send() -> None:
            try:
                esp32.send_raw(stub.host, [1, 2, 3])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=send) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == [], f"連打で失敗が出た: {errors}"
        status = esp32.get_status(stub.host)
        assert status["send_count"] == 10
        # スタブ側の同時実行数が 1 = サーバ側の直列化が ESP を守れている
        assert status["queue_len"] == 0


def test_firmware_202_is_accepted():
    """Phase 6 のファーム (キュー投入後に即 202) を先取りで検証する。"""
    with _StubServer(send_duration=0.05, send_status=202) as stub:
        esp32.send_raw(stub.host, [1, 2, 3])   # 例外が出なければ成功


def test_stub_busy_maps_to_device_busy():
    with _StubServer(fail_mode="busy") as stub:
        with pytest.raises(esp32.Esp32DeviceBusy):
            esp32.send_raw(stub.host, [1, 2, 3])


def test_stub_error_maps_to_bad_status():
    with _StubServer(fail_mode="error") as stub:
        with pytest.raises(esp32.Esp32BadStatus) as excinfo:
            esp32.send_raw(stub.host, [1, 2, 3])
        assert excinfo.value.esp_status == 500


def test_start_receive_triggers_callback():
    """学習フローの配線 (Phase 5 の下準備)。スタブが本物同様コールバックを撃つ。"""
    received: list[dict] = []
    callback_ready = threading.Event()

    from fastapi import FastAPI

    receiver = FastAPI()

    @receiver.post("/cb")
    def callback(payload: dict) -> dict:
        received.append(payload)
        callback_ready.set()
        return {"status": "ok"}

    with _ThreadedServer(receiver) as callback_server:
        with _StubServer(callback_delay=0.3) as stub:
            esp32.start_receive(
                stub.host, f"http://{callback_server.host}/cb", timeout_ms=5000
            )
            assert callback_ready.wait(timeout=10), "コールバックが届かなかった"

    assert received[0]["format"] == "raw"
    assert len(received[0]["data"]) > 0

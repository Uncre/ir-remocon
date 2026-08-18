"""テスト共通の fixture。

要点は 2 つ。

1. **本番 DB を絶対に掴ませない。** ``.gitignore`` に ``*.db`` があり
   ``ir_database.db`` は未追跡なので、壊すと復旧手段が無い (学習済み信号の
   再取得には実機が要る)。``config.DB_PATH`` を差し替えたうえで、
   差し替え漏れを autouse fixture で検出する。
2. ``TestClient`` は必ず ``with`` で使う。この形にしないと lifespan が走らず
   ``init_db()`` が呼ばれない (テーブルが無いだけの静かな失敗になる)。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from ir_remocon.app import config, db, esp32

#: 触ってはいけない本番 DB
PRODUCTION_DB = config.BASE_DIR / "ir_database.db"


@pytest.fixture(autouse=True)
def _guard_production_db(monkeypatch, tmp_path):
    """既定で必ず一時 DB を指させる。

    個別のテストが ``temp_db`` を取り忘れても本番 DB に書き込まないようにする
    安全網 (AGENTS.md の絶対ルール 3「DB を触る前にバックアップ」の自動化)。
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "guard.db")
    yield
    assert config.DB_PATH != PRODUCTION_DB, "テストが本番 DB を指しています"


@pytest.fixture(autouse=True)
def _isolate_esp32(monkeypatch):
    """ESP32 通信層のプロセスグローバルな状態をテストごとにリセットする。"""
    esp32.reset_state()
    esp32.set_client(None)
    # 最小送信間隔は既定で無効化する。スイート全体が秒単位で遅くなるのを防ぐため。
    # 間隔そのものを検証するテストだけが明示的に戻す。
    monkeypatch.setattr(config, "MIN_SEND_INTERVAL", 0.0)
    yield
    esp32.reset_state()
    esp32.set_client(None)


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """スキーマ適用済みの一時 DB を用意する。

    ``db.get_conn()`` は ``config.DB_PATH`` を呼び出しのたびに評価するので、
    属性の差し替えだけで済む (import 順の罠が無い)。
    ``init_db()`` が既定機器 id=1 / host=192.168.1.4 を投入する。
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    return config.DB_PATH


@pytest.fixture
def mock_esp(request):
    """``httpx.MockTransport`` を仕込み、送られたリクエストを記録する。

    使い方::

        def test_x(mock_esp):
            mock_esp.handler = lambda req: httpx.Response(200, json={})
            ...
            assert len(mock_esp.requests) == 1
    """

    class _MockEsp:
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.handler = lambda req: httpx.Response(200, json={"status": "ok"})

        def _dispatch(self, req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return self.handler(req)

    mock = _MockEsp()
    esp32.set_client(httpx.Client(transport=httpx.MockTransport(mock._dispatch)))
    yield mock
    esp32.set_client(None)


@pytest.fixture
def client(temp_db, mock_esp):
    """アプリの TestClient。一時 DB と MockTransport を仕込んだ状態。"""
    # config.DB_PATH を差し替えたあとに import する必要は無い (db.py が都度参照するため)
    from ir_remocon.app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        # lifespan が setup_logging() / init_db() を呼ぶが、init_db() は冪等。
        # lifespan 内で esp32.close_client() されるので client は最後に差し戻す。
        esp32.set_client(httpx.Client(transport=httpx.MockTransport(mock_esp._dispatch)))
        yield test_client

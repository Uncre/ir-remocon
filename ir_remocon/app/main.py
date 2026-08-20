"""FastAPI アプリの組み立てと起動。

例外 → HTTP ステータスの変換をここ 1 箇所に集約している。理由は 3 つ。

1. Phase 3 (機器の接続テスト) と Phase 5 (学習開始) が同じマッピングを必要とする。
   1 箇所なら食い違いようがない。
2. ルータから ESP32 の知識が消え、「例外を握り潰す ``try/except``」を書く場所
   自体が無くなる → **バグ B の再発を構造的に防げる**。
3. Phase 4 のスケジューラは HTTP レイヤ抜きで ``esp32`` を直接呼ぶ。だから
   マッピングを ``esp32.py`` に置いてはいけない。例外ハンドラはちょうど
   HTTP 境界そのものにあたる。

.. warning::
   **uvicorn の ``workers`` は永久に 1 でなければならない。**
   ``esp32.py`` のロック登録簿はプロセスローカルなので、ワーカーを増やした
   瞬間に送信の直列化 (バグ C の修正) が丸ごと無効化される。同じ理由で
   ``reload=True`` も使わない。アプリを文字列ではなく **オブジェクト** で
   渡しているのは、これを構造的に強制するため。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, esp32, learn, repository, scheduler
from .db import init_db
from .logging_conf import setup_logging
from .routers import devices, health, schedules, send, signals
from .routers import learn as learn_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動と終了の処理 (deprecated な ``@app.on_event`` は使わない)。

    ``setup_logging()`` を ``init_db()`` より **先に** 呼ぶのが要点。逆にすると
    ``init_db()`` の「列を追加しました」「既定機器を登録しました」といった INFO が
    ハンドラ未設定のルートロガーに落ちて消える。スキーマ移行が黙って起きるのは、
    まさにこのリファクタが潰したい類の事象。
    """
    setup_logging()
    logger.info(
        "起動します: DB=%s / ADVERTISE_HOST=%s / PORT=%s",
        config.DB_PATH, config.ADVERTISE_HOST, config.PORT,
    )
    init_db()
    scheduler.start_scheduler()
    try:
        yield
    finally:
        # スケジューラを先に止める。shutdown_scheduler() が実行中の目覚ましに
        # 停止を通知してからジョブの終了を待つので、ここで ESP32 クライアントを
        # 先に閉じると「終了処理中の送信」が握れなくなる。
        scheduler.shutdown_scheduler()
        esp32.close_client()
        logger.info("終了しました")


def create_app() -> FastAPI:
    """アプリを組み立てる。

    ファクトリにしてあるのは、テストが一時 DB を指した状態で組み立て直せるようにするため。
    ここでは I/O を一切行わない (実処理はすべて lifespan)。
    """
    app = FastAPI(
        title="Smart IR Remote Backend API",
        version="0.2.0",
        lifespan=lifespan,
    )

    # 旧実装は allow_origins=["*"] と allow_credentials=True を併用していた。
    # Starlette はこの組み合わせで Origin をそのまま反響させるため、事実上
    # 「任意オリジンから資格情報付きリクエスト可」になる。このアプリは Tailscale
    # 経由で LAN 外からも開かれるので、無用なリスクは負わない。
    # 認証もクッキーも使っていないので allow_credentials は不要。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Starlette の例外ハンドラ探索は type(exc).__mro__ を辿るので、
    # 基底クラスを 1 つ登録すれば全サブクラスに効く。
    # Esp32LocalBusy 等を追加してもこのファイルは無変更でよい。
    @app.exception_handler(esp32.Esp32Error)
    def _handle_esp32_error(request: Request, exc: esp32.Esp32Error) -> JSONResponse:
        logger.warning("ESP32 通信エラー [%s] %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=exc.http_status,
            # キーに "detail" を選ぶのは意図的。FastAPI の HTTPException と同じキーなので
            # フロントのエラー表示が 1 本のコードパスで済む。
            content={
                "detail": exc.message,
                "error": type(exc).__name__,
                "host": exc.host,
                "outcome_unknown": exc.outcome_unknown,
            },
        )

    @app.exception_handler(repository.RepositoryError)
    def _handle_repository_error(
        request: Request, exc: repository.RepositoryError
    ) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("DB エラー [%s] %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=exc.http_status,
            content={"detail": exc.message, "error": type(exc).__name__},
        )

    @app.exception_handler(scheduler.SchedulerError)
    def _handle_scheduler_error(
        request: Request, exc: scheduler.SchedulerError
    ) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("スケジューラエラー [%s] %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=exc.http_status,
            content={"detail": exc.message, "error": type(exc).__name__},
        )

    @app.exception_handler(learn.LearnError)
    def _handle_learn_error(request: Request, exc: learn.LearnError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("学習エラー [%s] %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=exc.http_status,
            content={"detail": exc.message, "error": type(exc).__name__},
        )

    app.include_router(signals.router)
    app.include_router(send.router)
    app.include_router(devices.router)
    app.include_router(schedules.router)
    app.include_router(health.router)
    app.include_router(learn_router.router)

    # 存在しないディレクトリを StaticFiles に渡すとマウント時点で RuntimeError に
    # なりサーバが起動しない。check_dir=False で黙らせる手もあるが、それだと
    # 設定ミスが永遠に静かな 404 になる。
    if config.STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
    else:
        logger.error(
            "static ディレクトリが見つかりません: %s (UI は表示できません)",
            config.STATIC_DIR,
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """UI を返す。

        **Jinja2 は使わない。** 旧実装は ``Jinja2Templates`` 経由で配信し、
        テンプレート変数 ``signals`` を渡していたが、``index.html`` は
        ``{{ }}`` を一度も使っておらず、毎リクエスト DB を引くだけの
        死んだクエリだった。画面の中身は全部 ``static/js/`` が API から
        取ってくるので、素のファイルを返せば機能的に等価。

        ``Cache-Control: no-cache`` を付けるのは、JS/CSS への参照が
        この HTML に書かれているため。ここが古いまま配信されると、更新した
        ``app.js`` をブラウザが読みに行かない。``/static`` 側は
        ``StaticFiles`` が ETag を付けるので、中身が変わらなければ 304 で済む。
        """
        return FileResponse(
            config.TEMPLATES_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    favicon_path = config.STATIC_DIR / "favicon.ico"
    if favicon_path.is_file():
        # ブラウザは /static/favicon.ico の指定とは別に /favicon.ico も取りに来る。
        # 旧実装では templates/ に置かれたまま、どのルートからも配信されず
        # 常に 404 になっていた。
        @app.get("/favicon.ico", include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(favicon_path)

    return app


app = create_app()


def run() -> None:
    """``uv run ir-remocon`` のエントリポイント。"""
    setup_logging()
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        # log_config=None は必須。既定の log_config は uvicorn.* ロガーに自前の
        # ハンドラを再設定し、logging_conf.py の handlers.clear() を上書きして
        # 無効化してしまう (ログがファイルに残らなくなる)。
        log_config=None,
    )


if __name__ == "__main__":
    run()

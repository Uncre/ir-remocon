"""ESP32 のスタブ。実機なしでサーバ側の全フローを検証するために使う。

実機は現在停止しており開発環境も無いため、これが以降の全フェーズの検証基盤になる。

``esp/temp.ino`` の挙動を意図的に忠実に真似ている。特に重要なのが
**``/ir/send`` が同期でブロックすること** — 実ファームは ESPAsyncWebServer の
非同期ハンドラ内で ``irsend.sendRaw()`` を同期実行しており、これがバグ C
(連打すると全部タイムアウトする) の原因の半分。ここでブロックしないと
サーバ側の直列化が効いているかどうか検証できない。

``ir_remocon`` は import しない。ハードウェアの代役なので、任意のサーバに
向けられる独立した道具であるべき。

使い方::

    uv run python tools/fake_esp32.py --port 8080
    uv run python tools/fake_esp32.py --send-duration 15     # 読み取りタイムアウト(504)を再現
    uv run python tools/fake_esp32.py --send-status 202      # Phase 6 のファームを先取り
    uv run python tools/fake_esp32.py --fail-mode busy       # 常に 409
    uv run python tools/fake_esp32.py --fail-mode error --fail-rate 0.3   # 3 割で 500

接続拒否 (502) を試したいときは、単にスタブを起動しないか別ポートを指せばよい。
そのためのフラグは用意していない。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger("fake_esp32")

#: 学習コールバックで返すダミー波形 (NEC 風の適当な長さ)
DUMMY_RAW = [
    9000, 4500, 560, 560, 560, 1690, 560, 560, 560, 1690, 560, 1690, 560, 560,
    560, 560, 560, 1690, 560, 560, 560, 1690, 560, 1690, 560, 560, 560, 1690,
    560, 560, 560, 560, 560, 1690, 560, 560, 560, 39000,
]


@dataclass
class Options:
    host: str = "127.0.0.1"
    port: int = 8080
    #: /ir/send がブロックする秒数。実ファームの irsend.sendRaw() 相当。
    send_duration: float = 0.2
    #: /ir/send の成功ステータス。202 にすると Phase 6 のファームを模擬できる。
    send_status: int = 200
    #: none / busy / error / bad-request / hang / drop
    fail_mode: str = "none"
    #: fail_mode を適用する確率 (0.0-1.0)。1.0 なら常に。
    fail_rate: float = 1.0
    #: False にすると ESP 側の 409 を返さなくなる (直列化の有無を比較するため)
    reject_concurrent: bool = True
    #: 受信モードに入ってからコールバックを撃つまでの秒数
    callback_delay: float = 3.0
    #: True にするとコールバックを一切送らない (学習失敗の切り分け用)
    callback_fail: bool = False


@dataclass
class State:
    """実ファームの ``currentMode`` 相当の状態機械。"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    mode: str = "idle"          # idle / receive / send
    send_count: int = 0
    last_send_ok: bool = True
    #: 同時に /ir/send ハンドラに入っている数。直列化の検証に使う。
    concurrent: int = 0
    max_concurrent: int = 0


def _should_fail(opts: Options) -> bool:
    if opts.fail_mode == "none":
        return False
    return random.random() < opts.fail_rate


def _send_callback(url: str, opts: Options) -> None:
    """学習コールバックを撃つ。実ファームの HTTPClient.POST 相当。"""
    body = json.dumps({"format": "raw", "freq": 38, "data": DUMMY_RAW}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("コールバック成功: %s -> HTTP %s", url, resp.status)
    except urllib.error.HTTPError as exc:
        logger.error("コールバック失敗: %s -> HTTP %s %s", url, exc.code, exc.read()[:200])
    except Exception as exc:  # noqa: BLE001 - 実機同様、何が起きてもログだけ残して続行
        logger.error("コールバック失敗: %s -> %s", url, exc)


def build_app(opts: Options) -> FastAPI:
    """スタブの FastAPI アプリを組み立てる。

    関数に切り出してあるのは、テストからそのまま import して
    ``uvicorn.Server`` をスレッド起動でき、subprocess なしで実ソケット経由の
    統合テストが書けるようにするため。
    """
    app = FastAPI(title="fake ESP32", version="1.0.0")
    state = State()

    @app.get("/status")
    def get_status() -> dict[str, Any]:
        with state.lock:
            mode = state.mode
            count = state.send_count
            ok = state.last_send_ok
        return {
            "status": "ok",
            "device_mode": mode,
            "wifi_ssid": "fake-ssid",
            "ip_address": opts.host,
            # Phase 6 のファームが返す予定のフィールド (前方互換の確認用)
            "send_count": count,
            "last_send_ok": ok,
            "queue_len": 0,
        }

    @app.post("/ir/send")
    def ir_send(payload: dict[str, Any]) -> Response:
        if opts.fail_mode != "none" and _should_fail(opts):
            return _failure_response(opts, state)

        if payload.get("format") != "raw" or not isinstance(payload.get("data"), list):
            logger.warning("不正なボディ: %s", str(payload)[:200])
            return JSONResponse(
                {"status": "error", "message": "Invalid request body."}, status_code=400
            )

        with state.lock:
            if opts.reject_concurrent and state.mode != "idle":
                logger.warning("ビジー中に送信要求が来ました (mode=%s) -> 409", state.mode)
                return JSONResponse(
                    {"status": "error", "message": "Device is busy."}, status_code=409
                )
            state.mode = "send"
            state.concurrent += 1
            state.max_concurrent = max(state.max_concurrent, state.concurrent)
            concurrent_now = state.concurrent

        try:
            logger.info(
                "送信中 (%d 要素, %.2f 秒ブロック, 同時実行=%d)",
                len(payload["data"]), opts.send_duration, concurrent_now,
            )
            # 実ファームはここで irsend.sendRaw() を同期実行し、TCP ごとブロックする。
            time.sleep(opts.send_duration)
        finally:
            with state.lock:
                state.concurrent -= 1
                state.send_count += 1
                state.last_send_ok = True
                state.mode = "idle"

        return JSONResponse(
            {"status": "ok", "message": "Signal sent successfully."},
            status_code=opts.send_status,
        )

    @app.put("/mode")
    def set_mode(payload: dict[str, Any]) -> Response:
        if opts.fail_mode != "none" and _should_fail(opts):
            return _failure_response(opts, state)

        with state.lock:
            if state.mode != "idle":
                logger.warning("ビジー中にモード変更が来ました (mode=%s) -> 409", state.mode)
                return JSONResponse(
                    {"status": "error", "message": "Device is busy."}, status_code=409
                )

            if payload.get("mode") != "receive" or not payload.get("callback_url"):
                return JSONResponse(
                    {"status": "error", "message": "Invalid request body."}, status_code=400
                )

            state.mode = "receive"

        timeout_ms = int(payload.get("timeout", 10000))
        callback_url = str(payload["callback_url"])
        logger.info(
            "受信モードに入りました (timeout=%dms, callback=%s)", timeout_ms, callback_url
        )

        def _finish() -> None:
            if opts.callback_fail:
                logger.warning("--callback-fail のためコールバックを送りません")
            elif opts.callback_delay * 1000 > timeout_ms:
                logger.warning(
                    "コールバック遅延 %.1fs がタイムアウト %.1fs を超えたため送りません",
                    opts.callback_delay, timeout_ms / 1000,
                )
            else:
                _send_callback(callback_url, opts)
            with state.lock:
                state.mode = "idle"
            logger.info("アイドルに戻りました")

        delay = min(opts.callback_delay, timeout_ms / 1000)
        threading.Timer(delay, _finish).start()

        return JSONResponse(
            {"status": "ok", "message": "Switching to receive mode."}, status_code=202
        )

    # 検証を楽にするための、実機には無い補助エンドポイント
    @app.get("/_stub/stats")
    def stub_stats() -> dict[str, Any]:
        with state.lock:
            return {
                "send_count": state.send_count,
                "max_concurrent": state.max_concurrent,
                "mode": state.mode,
            }

    @app.post("/_stub/reset")
    def stub_reset() -> dict[str, str]:
        with state.lock:
            state.send_count = 0
            state.max_concurrent = 0
            state.concurrent = 0
            state.mode = "idle"
        return {"status": "ok"}

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        response = await call_next(request)
        logger.info(
            "%s %s -> %d (%.3fs)",
            request.method, request.url.path, response.status_code,
            time.monotonic() - started,
        )
        return response

    return app


def _failure_response(opts: Options, state: State) -> Response:
    """``--fail-mode`` に応じた失敗を返す。"""
    if opts.fail_mode == "busy":
        return JSONResponse({"status": "error", "message": "Device is busy."}, status_code=409)
    if opts.fail_mode == "error":
        return JSONResponse({"status": "error", "message": "Internal failure."}, status_code=500)
    if opts.fail_mode == "bad-request":
        return JSONResponse({"status": "error", "message": "Invalid request body."}, status_code=400)
    if opts.fail_mode == "hang":
        # 応答を返さずに固まる。サーバ側の読み取りタイムアウト(504)を確実に起こす。
        logger.warning("--fail-mode hang: 応答せずに 300 秒固まります")
        time.sleep(300)
        return JSONResponse({"status": "error"}, status_code=500)
    if opts.fail_mode == "drop":
        # ボディを返さずに接続を切る。httpx 側では RemoteProtocolError になる。
        logger.warning("--fail-mode drop: ボディ無しで接続を切ります")
        return Response(status_code=200, content=b"", headers={"Content-Length": "999"})
    raise ValueError(f"未知の fail_mode: {opts.fail_mode}")


def parse_args(argv: Optional[list[str]] = None) -> Options:
    parser = argparse.ArgumentParser(description="ESP32 のスタブ (実機なしの検証用)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--send-duration", type=float, default=0.2,
        help="/ir/send がブロックする秒数。読み取りタイムアウト(504)を試すなら 15 など",
    )
    parser.add_argument(
        "--send-status", type=int, default=200,
        help="/ir/send の成功ステータス。202 で Phase 6 のファームを模擬",
    )
    parser.add_argument(
        "--fail-mode", default="none",
        choices=["none", "busy", "error", "bad-request", "hang", "drop"],
    )
    parser.add_argument("--fail-rate", type=float, default=1.0, help="fail-mode を適用する確率")
    parser.add_argument(
        "--no-reject-concurrent", action="store_true",
        help="ESP 側の 409 を無効化する (サーバ側の直列化だけを見たいとき)",
    )
    parser.add_argument("--callback-delay", type=float, default=3.0)
    parser.add_argument(
        "--callback-fail", action="store_true", help="学習コールバックを一切送らない",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s [fake_esp32] %(message)s",
        datefmt="%H:%M:%S",
    )
    return Options(
        host=args.host,
        port=args.port,
        send_duration=args.send_duration,
        send_status=args.send_status,
        fail_mode=args.fail_mode,
        fail_rate=args.fail_rate,
        reject_concurrent=not args.no_reject_concurrent,
        callback_delay=args.callback_delay,
        callback_fail=args.callback_fail,
    )


def main(argv: Optional[list[str]] = None) -> None:
    opts = parse_args(argv)
    logger.info(
        "スタブを起動します http://%s:%d (send_duration=%.2fs, send_status=%d, fail_mode=%s)",
        opts.host, opts.port, opts.send_duration, opts.send_status, opts.fail_mode,
    )
    uvicorn.run(build_app(opts), host=opts.host, port=opts.port, log_config=None)


if __name__ == "__main__":
    main()

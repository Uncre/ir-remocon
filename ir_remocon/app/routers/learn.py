"""赤外線信号の学習 (受信)。

旧実装の ``POST /api/receive`` は ESP32 を受信モードにして 202 を返すだけで、
**結果を知る手段が無かった**。フロントは ``setTimeout(refreshAll, 16000)`` で
16 秒後に一覧を引き直すだけだったので、学習できたのか失敗したのかは画面に
一切出なかった (「学習が時々失敗する」という体感の温床)。

ここではセッションにトークンを発行し、``GET /api/learn/{token}`` を
ポーリングすれば ``pending`` → ``success`` / ``timeout`` / ``error`` の
**実際の結果**が取れるようにしてある。

フローと設計判断は :mod:`ir_remocon.app.learn` の docstring を参照。
**このルータに ``try/except`` は無い。**
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import learn, repository
from ..models import IRSignalCallback, LearnSessionOut, LearnStartRequest

router = APIRouter(prefix="/api", tags=["learn"])


@router.post("/learn", response_model=LearnSessionOut, status_code=201)
def start_learn(payload: LearnStartRequest) -> dict:
    """学習を開始し、ESP32 を受信モードに切り替える。

    201 が返った時点で ESP は受信待ちに入っている。ユーザは
    ``timeout_seconds`` 以内にリモコンのボタンを押す必要がある。
    """
    device = repository.resolve_device(payload.device_id)
    session = learn.start(device, payload.name, payload.overwrite)
    return session.as_dict()


@router.get("/learn", response_model=list[LearnSessionOut])
def list_learn_sessions() -> list[dict]:
    """進行中の学習セッション一覧。

    フロントが再読込されたときに「学習中」の状態を復元するために使う。
    復元できないと、受信モード中の ESP に送信して必ず 409 を踏むことになる。
    """
    return [session.as_dict() for session in learn.list_pending()]


@router.get("/learn/{token}", response_model=LearnSessionOut)
def get_learn_session(token: str) -> dict:
    """学習の状態。フロントのポーリング先。"""
    return learn.get(token).as_dict()


@router.post("/callback/ir_signal/{token}")
def receive_ir_signal(token: str, payload: IRSignalCallback) -> dict:
    """ESP32 が受信した信号を受け取る (機器 → サーバ)。

    パスに信号名ではなくトークンを入れているのが旧実装との違い。
    ファームは ``callback_url`` をサーバから渡されてそのまま叩くだけなので、
    パスの中身はサーバが自由に決められる。旧実装の ``{name}`` 形式では
    **誰でも任意の信号を上書きできた** うえ、日本語やスラッシュを含む名前で
    URL が壊れていた。

    未知・期限切れのトークンは 404。ファームはこの応答コードをシリアルに
    出すだけで再送はしない。
    """
    session = learn.complete(token, payload.data, freq=payload.freq)
    return {
        "status": "ok",
        "name": session.name,
        "raw_length": session.raw_length,
    }

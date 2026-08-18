"""赤外線信号の送信。

**このルータに ``try/except`` が 1 つも無いことが、バグ B の修正そのもの。**
``return`` に到達する経路は成功しかない。旧実装は

.. code-block:: python

    def send_signal_to_esp32(name, req_body):
        execute_ir_send(name, req_body.esp32_ip)   # 中で例外を print して握り潰す
        return {"status": "ok"}                    # ← 常に成功と答える

という形で、実ログには ``Reason: timed out`` の直後に ``200 OK`` が並んでいた。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from .. import esp32, repository
from ..models import SendRequest

router = APIRouter(prefix="/api/send", tags=["send"])


@router.post("/{name}")
def send_signal(name: str, req: Optional[SendRequest] = None) -> dict:
    """登録済みの信号を機器へ送る。省略時は既定機器へ送る。

    失敗は例外として送出され、``main.py`` のハンドラが 409 / 502 / 504 に
    変換する。ここで握り潰さないことが要点。
    """
    # 信号の存在確認を機器解決より先に行う。存在しない信号名で ESP に
    # 無駄な通信をしないため。
    raw_data = repository.get_signal_raw(name)
    device = repository.resolve_device(req.device_id if req else None)

    esp32.send_raw(device["host"], raw_data)

    # host / device_id を返すのは、UI が「どこへ送ったか」を表示できるようにするため。
    # 機器の設定ミスにユーザが自力で気づける。
    return {
        "status": "ok",
        "name": name,
        "device_id": device["id"],
        "device_name": device["name"],
        "host": device["host"],
    }

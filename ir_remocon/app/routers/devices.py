"""機器 (ESP32) の登録・管理。

**このルータが不具合 E の解消そのもの。** 旧実装はフロントが送信のたびに
``esp32_ip`` を文字列で送り、予約ジョブには作成時の IP が pickle されて固定されていた。
そのため ESP の DHCP アドレスが変わった瞬間に既存の予約が全滅し、実ログでは古いジョブが
今も ``192.168.1.16`` を叩いて ``Connection refused`` を出し続けている。

ここで host を DB の 1 行にまとめたことで、``PUT /api/devices/{id}`` の 1 回で
**すべての送信と予約の宛先が即座に切り替わる** (再起動も予約の作り直しも不要)。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import esp32, repository
from ..models import (
    DeviceCreate,
    DeviceDeletedOut,
    DeviceOut,
    DeviceStatusOut,
    DeviceUpdate,
)

# prefix があるとき、コレクションのパスは "/" ではなく "" にする (307 リダイレクト回避)。
router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("", response_model=list[DeviceOut])
def list_devices() -> list[dict]:
    return repository.list_devices()


@router.post("", response_model=DeviceOut, status_code=201)
def create_device(payload: DeviceCreate) -> dict:
    return repository.create_device(payload.name, payload.host, payload.is_default)


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(device_id: int) -> dict:
    return repository.get_device(device_id)


@router.put("/{device_id}", response_model=DeviceOut)
def update_device(device_id: int, payload: DeviceUpdate) -> dict:
    return repository.update_device(
        device_id, name=payload.name, host=payload.host, is_default=payload.is_default
    )


@router.delete("/{device_id}", response_model=DeviceDeletedOut)
def delete_device(device_id: int) -> dict:
    promoted = repository.delete_device(device_id)
    message = f"機器 id={device_id} を削除しました"
    if promoted is not None:
        message += f" (機器 id={promoted} を既定に昇格しました)"
    return {"message": message, "new_default_device_id": promoted}


@router.get("/{device_id}/status", response_model=DeviceStatusOut)
def device_status(device_id: int) -> dict:
    """機器への疎通確認 (設定タブの「接続テスト」ボタン)。

    .. note::
       **AGENTS.md の「ルータに try/except を書かない」に対する、意図的な唯一の例外。**

       あのルールはバグ B (送信失敗を握り潰して 200 と報告していた) の再発防止だが、
       ここは逆向き。このエンドポイントの成果物は「到達できたか」そのものなので、
       到達不可は API の失敗ではなく **テストの正常な結果**。502 にしてしまうと
       「疎通確認という操作が失敗した」のか「機器に到達できなかった」のかを
       フロントが区別できなくなる。

       捕まえるのは :class:`esp32.Esp32Error` だけ。機器 id が存在しない場合の
       ``NotFound`` はそのまま伝播させて 404 にする (握り潰しの範囲を最小に保つ)。

    :func:`esp32.get_status` はロックを取らず、最大 3 秒のタイムアウトで叩く。
    機器がビジーなときこそ押したいボタンだし、画面が 10 秒固まるのは論外なため。
    """
    device = repository.get_device(device_id)
    try:
        status = esp32.get_status(device["host"])
    except esp32.Esp32Error as exc:
        return {
            "reachable": False,
            "host": device["host"],
            "detail": exc.message,
            "status": None,
        }
    return {"reachable": True, "host": device["host"], "detail": None, "status": status}

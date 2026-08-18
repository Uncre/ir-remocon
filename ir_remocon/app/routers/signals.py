"""赤外線信号の CRUD。

旧実装との違いは、SQL とエラー処理がここに無いこと。本体はすべて
``repository`` への 1 行の委譲で、``HTTPException`` も書かない
(``repository`` のドメイン例外を ``main.py`` のハンドラが変換する)。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import repository
from ..models import SignalCreate, SignalDetail, SignalListItem, SignalUpdate

# prefix があるとき、コレクションのパスは "/" ではなく "" にする。
# "/" にすると URL が /api/signals/ になり、/api/signals へのアクセスが
# 307 リダイレクトになってしまう。
router = APIRouter(prefix="/api/signals", tags=["signals"])


@router.get("", response_model=list[SignalListItem])
def list_signals() -> list[dict]:
    return repository.list_signals()


@router.post("", response_model=SignalDetail, status_code=201)
def create_signal(payload: SignalCreate) -> dict:
    return repository.create_signal(payload.name, payload.raw_data)


@router.get("/{name}", response_model=SignalDetail)
def get_signal(name: str) -> dict:
    return repository.get_signal(name)


@router.put("/{name}", response_model=SignalDetail)
def update_signal(name: str, payload: SignalUpdate) -> dict:
    return repository.update_signal(
        name, new_name=payload.name, raw_data=payload.raw_data
    )


@router.delete("/{name}")
def delete_signal(name: str) -> dict:
    repository.delete_signal(name)
    return {"message": f"信号 '{name}' を削除しました"}

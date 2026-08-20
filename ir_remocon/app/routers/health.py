"""アプリの健全性。

**不具合 D への直接の答え。** 旧実装ではスケジューラのスレッドが
``database or disk is full`` で死んでも API は 200 を返し続け、予約が数ヶ月間
1 件も発火していないことに誰も気づけなかった。

.. note::
   **状態が悪くても HTTP は 200 を返す。** 不健全は「ヘルスチェックという操作の
   失敗」ではなく **結果** なので、``ok`` フィールドで表現する
   (``GET /api/devices/{id}/status`` と同じ判断)。フロントは HTTP ステータス
   ではなく ``ok`` を見て赤バナーを出すこと。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import scheduler
from ..models import HealthOut

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> dict:
    return scheduler.health()

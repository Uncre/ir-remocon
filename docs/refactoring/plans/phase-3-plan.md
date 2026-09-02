# Phase 3: 機器登録と IP 管理

> Codex 移管メモ: これは Claude Code で作成した計画の履歴スナップショット。
> 現在の作業規約は `../../../AGENTS.md`、実装後の確定事項は `../HANDOFF.md` を優先する。

親計画: `master-plan.md`
引き継ぎ: `../HANDOFF.md`（Phase 2 完了時点）

---

## Context

**なぜやるか（不具合 E の根治）**

旧実装は送信先の ESP32 の IP をフロントが毎回リクエストボディに載せ、予約ジョブには
`scheduler.add_job(..., args=[req.name, req.esp32_ip])` で**作成時の IP が pickle されて固定**
されていた。結果として ESP の DHCP アドレスが変わった瞬間に既存の予約が全滅し、実ログでは
古いジョブが今も `192.168.1.16` を叩いて `Connection refused` を出し続けている。

Phase 1 で `devices` テーブルと Pydantic モデル（`DeviceCreate` / `DeviceUpdate` / `DeviceOut` /
`DeviceStatusOut`）、Phase 2 で `repository` の**読み取り**関数（`list_devices` / `get_device` /
`get_default_device` / `resolve_device`）と `esp32.get_status()` は用意済み。
**残っているのは書き込み系（作成 / 更新 / 削除）と、それを公開する HTTP ルータだけ。**

**この Phase を終えたときに成立していること**

- 設定タブ（Phase 5）から機器の host を編集でき、変更が**次の送信から即座に**反映される
- 機器を消しても送信経路が壊れない（最後の 1 台は削除できない／既定は必ず 1 台存在する）
- ESP に疎通確認できる（接続テスト）
- Phase 4 のスケジューラが `device_id` だけをジョブに保存すれば E が構造的に再発しない状態

**この Phase でやらないこと**

- ジョブ引数の `device_id` 化そのもの → **Phase 4**（スケジューラがまだ存在しない）。
  Phase 3 が用意するのは `repository.resolve_device(device_id)` を発火時に呼べる状態まで。
- リクエストボディの `esp32_ip` → `device_id` 置換は **Phase 2 で完了済み**
  （`SendRequest` に `extra="forbid"`、`Schedule*` / `LearnStart*` はすべてこれを継承）。
- フロントの設定タブ UI → Phase 5。

**スキーマ変更なし。** `devices` テーブルは Phase 1 で作成済みなのでマイグレーションは発生しない。

---

## 決定事項（本計画で確定）

| 論点 | 決定 |
|---|---|
| 接続テストで到達不可のとき | **200 + `reachable: false`**（到達不可は「テストの正常な結果」） |
| 最後の既定機器を `is_default: false` にする PUT | **409 で拒否**（先に別の機器を既定にさせる） |
| 既定機器の DELETE（他に機器がある） | **最若番を自動昇格**し、昇格先 id をレスポンスに明記 |
| 最後の 1 台の DELETE | **409 で拒否**（Phase 2 からの申し送り） |

---

## 実装

### 1. `ir_remocon/app/repository.py` — 機器の書き込み系を追加

既存の例外階層（`RepositoryError` / `NotFound` / `DuplicateName`）に 1 つ足す:

```python
class ConstraintViolation(RepositoryError):
    """業務上の不変条件に反する操作（最後の 1 台の削除など）。"""
    http_status = 409
```

`main.py` の例外ハンドラは基底 `RepositoryError` を登録済みで、Starlette が `__mro__` を辿るため
**`main.py` は無変更で 409 になる**（Phase 2 の設計どおり）。

追加する関数:

| 関数 | 挙動 |
|---|---|
| `create_device(name, host, is_default=False)` | `normalize_host()` を通してから INSERT。`sqlite3.IntegrityError` → `DuplicateName`（`devices.name` は UNIQUE）。**テーブルが空なら `is_default` を強制的に 1 にする**（0 台状態からの復旧経路） |
| `update_device(device_id, *, name=None, host=None, is_default=None)` | 存在しなければ `NotFound`。host は正規化。`is_default=True` なら他を降ろす。`is_default=False` かつ対象が現在の既定 → `ConstraintViolation` |
| `delete_device(device_id)` | 存在しなければ `NotFound`。残り 1 台なら `ConstraintViolation`。既定を消したら最若番を昇格し `{"promoted_device_id": int \| None}` を返す |

内部ヘルパ `_set_default(conn, device_id)`:

```sql
UPDATE devices SET is_default = 0, updated_at = ? WHERE is_default = 1 AND id != ?;
UPDATE devices SET is_default = 1, updated_at = ? WHERE id = ?;
```

`get_conn()` が正常終了で commit・例外で rollback するので、**「他を降ろす」と「自分を上げる」は
1 トランザクションで原子的**。中間状態（既定 0 台）が永続化されることはない。

**host は必ず正規化して保存する。** `models.normalize_host()` を repository でも通す
（`esp32.py` が既に `from .models import normalize_host` している前例あり）。理由は 2 つ:
API 経由なら Pydantic が正規化するが、Phase 4 のスケジューラや `tools/` から直接呼ばれた
場合に素通りする。そして `esp32._states` の**ロック登録簿は host 文字列がキー**なので、
`192.168.1.4` と `http://192.168.1.4/` が別エントリになると同一機器への直列化（バグ C の修正）が
静かに壊れる。

> 補足: host を変更すると `esp32._states` に旧 host のエントリが残る。ロック 1 個分のメモリなので
> 実害なし。掃除は入れない（掃除中に旧 host へ進行中の送信があるとロックを取り違える）。

### 2. `ir_remocon/app/models.py` — 小改訂

- `DeviceStatusOut` を実際に返す形に合わせる: `reachable: bool` / **`host: str`（追加）** /
  `detail: Optional[str]` / **`device: dict` → `status: Optional[dict]` に改名**。
  `host` を返すのは、接続テストが「どの host を叩いたか」を UI に見せられるようにするため
  （設定ミスをユーザが自力で気づける。`POST /api/send` が `host` を返しているのと同じ意図）。
- `DeviceDeletedOut { message: str, new_default_device_id: Optional[int] }` を追加。
- **`DeviceCreate` / `DeviceUpdate` に `model_config = ConfigDict(extra="forbid")` を付ける。**
  `SendRequest` と同じ理由: `{"name": "esp32", "host": "...", "is_defualt": true}` のような
  タイプミスが 200 で通り、「既定にしたつもりが既定になっていない」という静かな食い違いを生む。
  **`DeviceBase` には付けない** — `DeviceOut` が継承しており、レスポンス検証で列追加のたびに
  500 になる罠を作らないため。

### 3. `ir_remocon/app/routers/devices.py`（新規）

`signals.py` と同じ薄さ。`prefix="/api/devices"`、コレクションのパスは `""`（307 回避）。

| メソッド | パス | response_model |
|---|---|---|
| GET | `""` | `list[DeviceOut]` |
| POST | `""` | `DeviceOut` (201) |
| GET | `/{device_id}` | `DeviceOut` |
| PUT | `/{device_id}` | `DeviceOut` |
| DELETE | `/{device_id}` | `DeviceDeletedOut` |
| GET | `/{device_id}/status` | `DeviceStatusOut` |

**接続テストだけが、このプロジェクト唯一の `try/except` を持つルータになる:**

```python
@router.get("/{device_id}/status", response_model=DeviceStatusOut)
def device_status(device_id: int) -> dict:
    """機器への疎通確認。

    AGENTS.md の「ルータに try/except を書かない」に対する **意図的な唯一の例外**。
    このエンドポイントの成果物は「到達できたか」そのものなので、到達不可は
    API の失敗ではなく **テストの正常な結果**。バグ B（送信失敗を成功と報告する）とは
    逆で、ここで 502 にすると「疎通確認が失敗した」のか「機器に到達できなかった」のかを
    フロントが区別できなくなる。

    捕まえるのは esp32.Esp32Error だけ。機器 id が存在しない場合の NotFound は
    そのまま伝播させて 404 にする（握り潰しの範囲を最小に保つ）。
    """
    device = repository.get_device(device_id)
    try:
        status = esp32.get_status(device["host"])
    except esp32.Esp32Error as exc:
        return {"reachable": False, "host": device["host"], "detail": exc.message, "status": None}
    return {"reachable": True, "host": device["host"], "detail": None, "status": status}
```

`esp32.get_status()` は **ロックを取らず**、`_status_timeout()`（最大 3 秒）で叩く設計に
Phase 2 でなっている。接続テストで画面が 10 秒固まらないし、機器がビジーなときこそ使える。

**機器の登録・更新時に疎通確認はしない。** ESP の電源が入っていない状態でも先に登録できるべき
（Phase 6 で静的 IP を焼く前に、サーバ側の設定を用意しておく運用になる）。

### 4. `ir_remocon/app/main.py`

- `from .routers import devices, send, signals` → `app.include_router(devices.router)`
- 暫定トップの `"phase": 2` → `3`

---

## テスト

### `tests/test_devices.py`（新規）

既存の `temp_db` / `client` / `mock_esp` fixture をそのまま使う（`conftest.py` は無変更）。

**リポジトリ層**
- 作成 → 一覧 → 取得 → 更新 → 削除の往復
- 同名で作成 → `DuplicateName` (409)
- host の正規化: `"http://192.168.1.50/"` → `"192.168.1.50"` で保存される
- `is_default=True` で作成／更新すると**他の機器の既定フラグが降りる**（既定は常に 1 台）
- 最後の既定を `is_default=False` にする → `ConstraintViolation` (409)、DB は無変更
- 既定機器を削除 → 最若番が昇格し `promoted_device_id` が返る
- 既定でない機器を削除 → 昇格は起きない (`None`)
- 最後の 1 台を削除 → `ConstraintViolation` (409)、機器は残る **★ Phase 2 からの申し送り**
- 存在しない id の更新／削除 → `NotFound` (404)
- `created_at` / `updated_at` が埋まる。更新で `updated_at` だけが進む

**HTTP 層**
- CRUD 往復（201 / 200 / 200 / 200）
- `GET /api/devices` が 307 を出さない（`""` パス）
- 409 / 404 が正しいステータスと `error` キーで返る
- 更新ボディが空 `{}` → 422（`_at_least_one_field`）
- `{"is_defualt": true}` のようなタイプミス → 422（`extra="forbid"` の効果）
- `GET /{id}/status`: 到達可 → `200 {"reachable": true, "host": ..., "status": {...}}`
- `GET /{id}/status`: `mock_esp` を `httpx.ConnectError` にして → **200 かつ `reachable: false`、`detail` に理由**
- `GET /{id}/status`: 存在しない id → **404**（Esp32Error だけを捕まえていることの証明）

**★ 不具合 E の回帰ガード（このフェーズの主目的）**
```python
def test_host_change_takes_effect_immediately(client, mock_esp, signal):
    client.post(f"/api/send/{signal}")
    assert mock_esp.requests[-1].url.host == "192.168.1.4"

    client.put("/api/devices/1", json={"host": "192.168.1.50"})

    client.post(f"/api/send/{signal}")
    assert mock_esp.requests[-1].url.host == "192.168.1.50"  # 再起動なしで切り替わる
```

### 既存テストの修正

- `tests/test_routers_signals.py::test_root_does_not_serve_legacy_ui` が
  `json()["phase"] == 2` を見ている。**フェーズ番号ではなく「HTML を返していないこと」を
  検証する形に直す**（`"text/html" not in response.headers["content-type"]`）。
  毎フェーズこのテストを書き換える運用は無意味なので、ここで断つ。

---

## 検証手順

### 1. 自動テスト

```powershell
uv run pytest -v                    # 単体（既存 84 件 + 追加分がすべて緑）
uv run pytest -m integration -v     # 実ソケット（既存 8 件が壊れていないこと）
```

> `tmp_path` で `PermissionError (WinError 5)` が出る場合は開発機側の既知問題。
> `$env:PYTEST_DEBUG_TEMPROOT` に別ディレクトリを指定すれば回避できる（AGENTS.md 参照）。

### 2. 手動 E2E（実機不要・本番 DB を触らない）

```powershell
# ターミナル A: ESP32 スタブ
uv run python tools/fake_esp32.py --port 8080

# ターミナル B: 検証用 DB を指してサーバ起動（本番 ir_database.db は絶対に使わない）
$env:IR_DB_PATH = "$env:TEMP\ir_phase3.db"
$env:IR_ADVERTISE_HOST = "127.0.0.1"
uv run ir-remocon
```

ターミナル C から（`/docs` の Swagger UI でも同じことができる）:

```powershell
$b = "http://127.0.0.1:8102/api"

irm "$b/devices"                                                    # 初期 1 台 (192.168.1.4)
irm "$b/devices/1/status"                                           # → reachable:false (未起動の IP)
irm "$b/devices/1" -Method Put -ContentType application/json -Body '{"host":"http://127.0.0.1:8080/"}'
irm "$b/devices/1"                                                  # → host が "127.0.0.1:8080" に正規化されている
irm "$b/devices/1/status"                                           # → reachable:true + ESP の /status 中身
irm "$b/devices" -Method Post -ContentType application/json -Body '{"name":"living","host":"127.0.0.1:8080","is_default":true}'
irm "$b/devices"                                                    # → id=2 が既定、id=1 の既定は降りている
irm "$b/devices/1" -Method Delete                                   # → 200（既定でないので昇格なし）
irm "$b/devices/2" -Method Delete                                   # → 409 最後の 1 台
```

確認する挙動:

| 確認項目 | 期待 |
|---|---|
| **E の根治** | 信号を 1 件登録 → 送信 → host を変更 → 再送信で**スタブ側のログの届き先が切り替わる** |
| **接続テスト（不可）** | スタブを落として `/devices/{id}/status` → **200 かつ `reachable:false`**（502 ではない） |
| **接続テスト（可）** | スタブ起動中 → `reachable:true` と `status.send_count` が見える |
| **既定は常に 1 台** | 2 台目を既定にすると 1 台目の `is_default` が `false` になる |
| **既定を外せない** | `{"is_default": false}` を既定機器に PUT → 409 |
| **最後の 1 台** | 全部消そうとすると最後で 409。**送信が死なない** |
| **既定の削除** | 2 台ある状態で既定を削除 → `new_default_device_id` が返り、送信が継続できる |
| **host 正規化** | `http://` 付き・末尾スラッシュ付きで登録しても DB は裸の host |

### 3. 本番 DB を汚していないことの確認

```powershell
Remove-Item Env:\IR_DB_PATH
uv run python -c "from ir_remocon.app import repository; print(repository.list_signals(), repository.list_devices())"
# → room_light_turn_on / room_light_turn_off が 2 件、機器は esp32 (192.168.1.4) のまま
```

---

## 完了時にやること（AGENTS.md の更新義務）

`AGENTS.md` に以下を反映してから報告し、**Phase 4 には勝手に進まない**。

- 進行状況テーブルの Phase 3 を ✅ にし、次を Phase 4 に
- 「Phase 3 で完成したもの」節（ファイル一覧 + 実測で確認できた事実の表）
- 「⚠️ 現在は新旧が同居している」節から「機器の編集 API はまだ無い（Phase 3）」の記述を削除し、
  検証時の host 変更手順を `PUT /api/devices/{id}` に差し替える
- 確定不具合の表で **E を ✅ 済みに**（ただし「ジョブ引数の device_id 化は Phase 4」と併記）
- **Phase 4 への申し送り**:
  - ジョブ引数は `[signal_name, device_id]` / `[on, off, interval, duration, device_id]`。
    **発火時に `repository.resolve_device(device_id)` を呼ぶ**（host を焼き付けない）
  - 参照先の機器が削除済みだった場合、ジョブ実行は `NotFound` になる。
    Phase 4 の `EVENT_JOB_ERROR` リスナと `/api/health` の `last_job_error` で可視化すること
    （黙って失敗させると不具合 D の再来になる）
  - `/api/health` に機器台数と既定機器を含めるか検討
- ルータの `try/except` は接続テスト 1 箇所のみ、という例外を明記

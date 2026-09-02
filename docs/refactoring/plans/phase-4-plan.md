# Phase 4: スケジューラ堅牢化

> Codex 移管メモ: これは Claude Code で作成した計画の履歴スナップショット。
> 現在の作業規約は `../../../AGENTS.md`、実装後の確定事項は `../HANDOFF.md` を優先する。

対象リポジトリ: `c:\Users\UncrewedSloth\Desktop\python_desktop\ir-remocon`
全体計画: `master-plan.md` / 引き継ぎ: `../HANDOFF.md`

## Context

Phase 1〜3 で土台・ESP32 通信・機器登録まで新パッケージ (`ir_remocon/app/`) に移した。
**新サーバにはまだ予約機能が無く、`jobs.db` を一切開いていない。** ここを埋めるのが Phase 4。

同時に、旧実装で確定している 3 つの不具合を潰す。

- **D: 予約が数ヶ月間 1 件も発火していない。** `sqlite3.OperationalError: database or disk is full`
  で APScheduler のスレッドが死に、以後 API は 200 を返し続けた。**誰も検知できなかったことが本体。**
- **G: 目覚ましがワーカースレッドを占有する。** `time.sleep(duration)` で保持し、中断手段も無い。
  実ログに `Run time of job ... was missed` が出ている。
- **E の残り。** host 解決は Phase 3 で DB の 1 行に集約済み。ジョブ引数を `device_id` にするのは今フェーズ。
- **H の一部。** `next_run` が `None` のジョブで予約一覧が 500 になる naive/aware 比較、
  `trigger.fields[5]` のインデックス直参照。

到達点: 予約・目覚ましが新 API で動き、**スケジューラが死んだら画面に出る**状態にする。

### 事前の決定事項（ユーザー確認済み）

- 旧 `jobs.db` の 7 件（すべて `__main__:execute_wakeup_alarm` 参照 = 復元不能、宛先は死んだ
  `192.168.1.16`）は **バックアップしたうえで破棄**。APScheduler が起動時に自動削除する
  （`_get_jobs()` が復元失敗ジョブを ERROR ログ付きで DELETE することを実装で確認済み）。
- 目覚まし中の送信失敗は **継続、連続 5 回で中止**して `last_job_error` に記録。

---

## 実装するもの

### 新規ファイル

| ファイル | 役割 |
|---|---|
| `ir_remocon/app/scheduler.py` | **本フェーズの中心。** スケジューラの生成/起動/停止、`build_trigger()`、ジョブ登録・削除・一覧、イベントリスナ、ハートビート、health 集計、専用例外 |
| `ir_remocon/app/jobs.py` | 発火時に走る関数 (`run_signal_job` / `run_wakeup_alarm`) と実行中アラームの登録簿 |
| `ir_remocon/app/routers/schedules.py` | 予約 CRUD + 実行中アラームの参照/停止 |
| `ir_remocon/app/routers/health.py` | `GET /api/health` |
| `tests/test_scheduler.py` / `tests/test_jobs.py` / `tests/test_schedules_api.py` | 追加テスト |

### 変更するファイル

| ファイル | 変更 |
|---|---|
| `ir_remocon/app/main.py` | lifespan でスケジューラ起動/停止、`SchedulerError` の例外ハンドラ追加、ルータ 2 本を登録、`index()` の `phase` を 4 に |
| `ir_remocon/app/models.py` | `duration_seconds` の上限検証を追加、`HealthOut` に `device_count` / `default_device_name` / `last_heartbeat` を追加 |
| `ir_remocon/app/repository.py` | `find_device(id) -> Optional[dict]`（例外を投げない版）を追加 |
| `tests/conftest.py` | `JOBS_DB_PATH` を一時ファイルに固定する autouse ガード、スケジューラ singleton のリセット |
| `AGENTS.md` | 進行状況表・Phase 4 セクション・申し送りの更新（**フェーズ完了の必須作業**） |

既存モデル `JobOut` / `ScheduleCreatedOut` / `RunningAlarmOut` / `HealthOut`、および
`config.py` のスケジューラ設定（`SCHEDULER_MAX_WORKERS` / `MISFIRE_GRACE_TIME` /
`MAX_ALARM_DURATION_SEC` / `TIMEZONE`）は Phase 1 で用意済み。**そのまま使う。**

---

## 設計の要点

### 1. ジョブ引数は kwargs のみ。host を焼き付けない

```python
scheduler.add_job(
    jobs.run_signal_job, trigger, id=job_id, name="単発: light_on",
    kwargs={"signal_name": name, "device_id": device_id, "meta": {...}},
)
```

- `device_id=None` は「既定機器」を意味し、**None のまま保存する**。既定機器を切り替えれば
  その予約も追従する。作成時に具体的な id へ解決してはいけない。
- 実行関数は発火のたびに `repository.resolve_device(device_id)` → `esp32.send_raw(device["host"], ...)`。
  host を引数に入れた瞬間に不具合 E が復活する。
- **関数参照は `ir_remocon.app.jobs:run_signal_job` という文字列で pickle される。**
  モジュール名・関数名を変えると既存の予約が全部復元不能になる（今回まさにそれで旧 7 件が消える）。
  改名禁止をコード内コメントと `AGENTS.md` に明記する。

### 2. 表示用メタ情報は作成時に焼いて `kwargs["meta"]` に入れる

`{"kind": "signal"|"wakeup", "repeat_type", "time": "08:05", "date", "days", "description": "毎週 [月,火] @ 08:05"}`

旧実装の `trigger.fields[5]` のようなインデックス直参照は廃止（APScheduler のバージョン差で壊れる）。
一覧はこの `meta` を読むだけ。`run_*` 関数は `meta` を受け取るがログ以外には使わない
（既定値 `None` を付けて、将来メタを増やしても古いジョブが `TypeError` にならないようにする）。

### 3. トリガ生成を 1 本化

`build_trigger(repeat_type, execute_time, execute_date, repeat_days, tz)` を `scheduler.py` に切り出す。
旧実装は単発用と目覚まし用で 21 行を丸ごと複製していた。検証は Pydantic 側で済んでいる
（`once` に日付必須 / `weekly` に曜日必須は `models.ScheduleBase` が 422 にする）ので、
ここでは組み立てだけを行う。

### 4. ハートビートでスケジューラの死を検知する（D の根治）

**`scheduler.running` は状態フラグしか見ていない。** メインループのスレッドが例外で死んでも
`True` を返し続けることを実測で確認した（`s._thread = None` にしても `running` は `True`）。
つまり `scheduler.running` だけでは不具合 D をもう一度見逃す。

対策: 60 秒間隔の内部ジョブでモジュール変数のタイムスタンプを更新し、
**180 秒以上古ければ `scheduler_running = false`** と判定する。

- 内部ジョブは `MemoryJobStore` の `'internal'` ジョブストアに置く。
  → `jobs.db` に永続化されず、`get_jobs(jobstore='default')` にも `job_count` にも出ない。
- 実行経路まで含めて生きていることを証明できる（ループスレッドだけでなくエグゼキュータも）。
- 検知の遅れは最大 3 分。家庭用リモコンとしては十分。

### 5. スケジューラ設定

```python
BackgroundScheduler(
    jobstores={'default': SQLAlchemyJobStore(url=f'sqlite:///{config.JOBS_DB_PATH}'),
               'internal': MemoryJobStore()},
    executors={'default': ThreadPoolExecutor(max_workers=config.SCHEDULER_MAX_WORKERS)},
    job_defaults={'coalesce': True, 'max_instances': 1,
                  'misfire_grace_time': config.MISFIRE_GRACE_TIME},
    timezone=config.TIMEZONE,
)
```

`esp32.get_client()` と同じく **遅延生成の singleton** にし、テスト用に
`get_scheduler()` / `shutdown_scheduler()` / `reset_scheduler()` を用意する（既存の作法に合わせる）。

### 6. イベントリスナ

`EVENT_JOB_ERROR | EVENT_JOB_MISSED` を登録し、ログ出力 + 直近エラー（文字列と時刻）を保持。
`/api/health` の `last_job_error` に出す。

**特に重要**: ジョブが名指しする機器が削除されると発火時に `repository.NotFound` が飛ぶ。
これを黙って失敗させると不具合 D の再来なので、必ずここで可視化する。

### 7. 目覚ましアラーム（G の根治）

- `run_wakeup_alarm()` は `run_id` を発行し、`threading.Event` とともに
  `jobs._running_alarms` に登録する。`time.sleep()` ではなく **`event.wait(interval)`** で待つ
  → `DELETE /api/alarms/{run_id}` で即座に抜けられる。
- 各送信は `esp32.send_raw(host, raw, lock_timeout=min(interval, 1.0))`。
  ロックが取れなければ 1 拍スキップして待ちを積み上げない（この引数は Phase 2 で用意済み）。
- 送信失敗は握り潰さずログに出し、**連続 5 回失敗で中止**。`last_job_error` に記録。
  1 回の失敗で止めない（一時的な 409 で目覚ましが鳴らなくなるのは困る）。
- `duration_seconds` は `config.MAX_ALARM_DURATION_SEC`（既定 1800）を上限とし、
  超過は **作成時に 422**（黙って切り詰めない）。`models.py` の `field_validator` で
  検証時に config を読む形にする（`Field(le=...)` だとテストで monkeypatch できない）。
- 実行中アラームの登録簿はプロセスメモリ。サーバ再起動で消える（＝鳴り止む）。これは許容し、明記する。
- lifespan の shutdown で **全アラームに停止を通知してから** `scheduler.shutdown(wait=True)`。
  そうしないと終了が最大 30 分ブロックする。

### 8. 一覧の堅牢化（H）

- ソートキーは `(job.next_run_time is None, job.next_run_time)`。`None` を比較対象から外す。
  **属性名は `next_run_time`**（3.11.3 に `next_run` は存在しないことを実測確認済み）。
- `device_name` の解決には新設の `repository.find_device()` を使う。機器が削除済みでも
  `"(削除済み id=3)"` と表示し、**ダングリングなジョブ 1 件で一覧全体を 500 にしない**。

### 9. 例外とルータ

`scheduler.py` に `SchedulerError`（500）/ `JobNotFound`（404）/ `SchedulerNotRunning`（503）を定義し、
`main.py` に基底クラスのハンドラを 1 つ追加する。Starlette は `__mro__` を辿るのでこれで全サブクラスに効く。
**ルータには `try/except` を書かない**（既存ルール）。`JobLookupError` の変換は `scheduler.py` の責務。

ジョブ作成時に信号と機器の存在を確認する（`repository.get_signal_raw()` / `resolve_device()`）
→ 存在しなければ即 404。「登録はできたが永遠に失敗し続ける予約」を作らせない。

### 10. `/api/health` は常に 200 を返す

`ok: false` を本文で表現する。`GET /api/devices/{id}/status` と同じ理由 —
このエンドポイントの成果物は「健全かどうか」そのものなので、不健全は API の失敗ではない。
フロントは HTTP ステータスではなく `ok` を見て赤バナーを出す（Phase 5）。

---

## API

| メソッド | パス | 備考 |
|---|---|---|
| POST | `/api/schedule` | `SignalScheduleRequest` → 201 `ScheduleCreatedOut` |
| POST | `/api/schedule/wakeup` | `WakeupScheduleRequest` → 201 |
| GET | `/api/schedules` | `list[JobOut]`。`next_run` 昇順、`None` は末尾 |
| DELETE | `/api/schedules/{job_id}` | 無ければ 404 |
| GET | `/api/alarms` | `list[RunningAlarmOut]`（実行中のみ） |
| DELETE | `/api/alarms/{run_id}` | 実行中アラームの中断。無ければ 404 |
| GET | `/api/health` | `HealthOut`。常に 200 |

ボディから `esp32_ip` は完全に消える（`SendRequest` を継承しているので `extra="forbid"` で 422）。

---

## テスト

`tests/conftest.py` に **`config.JOBS_DB_PATH` を `tmp_path` に固定する autouse ガード**を足す
（既存の `_guard_production_db` と同じ形）。これが無いと本番 `jobs.db` をテストが書き換える。

固定したい振る舞い:

1. **[E の回帰ガード]** ジョブ作成 → `PUT /api/devices/1 {"host": ...}` → `jobs.run_signal_job()` を
   直接呼ぶ → **新しい host に送信される**こと。旧実装が壊れていた核心。
2. **[D の回帰ガード]** ハートビートが古い / スケジューラ停止時に `/api/health` の
   `scheduler_running` が `false` になること。逆に正常時は `true`。
3. `build_trigger()` の 3 種類（once の `DateTrigger`、daily/weekly の `CronTrigger`）と、
   `once` に日付なし・`weekly` に曜日なしが 422 になること。
4. 一覧のソート: `next_run_time = None` のジョブが混ざっても 500 にならず末尾に来ること。
5. 一覧: 機器を削除しても該当ジョブが `(削除済み ...)` 表示で残り、200 が返ること。
6. アラーム: `DELETE /api/alarms/{run_id}` で `duration` を待たずに終了すること
   （`interval` を 0.01 秒などに縮めて実時間で検証）。
7. アラーム: 連続 5 回失敗で中止し、`last_job_error` に出ること。
8. `duration_seconds` が上限超過で 422（`config.MAX_ALARM_DURATION_SEC` を monkeypatch）。
9. 存在しない信号名 / 機器 id で予約を作ると 404。
10. `{"esp32_ip": ...}` を含む予約ボディが 422。

---

## 検証手順

### 事前作業（実装の最初に行う）

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item ir_remocon\jobs.db "backup_before_refactor\jobs.phase4-$stamp.db"
Copy-Item ir_remocon\ir_database.db "backup_before_refactor\ir_database.phase4-$stamp.db"
```

### 自動テスト

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\pytest-ir" | Out-Null   # 先に作ること
$env:PYTEST_DEBUG_TEMPROOT = "$env:TEMP\pytest-ir"
uv run pytest -q
uv run pytest -m integration -q
```

> 開発機の `pytest-of-UncrewedSloth` はアクセス権が壊れており、この 2 行が無いと
> `tmp_path` を使う全テストが落ちる（コード側の問題ではない。AGENTS.md 参照）。

### スタブを使った手動確認（実機不要・本番 DB を触らない）

```powershell
# ターミナル A
uv run python tools/fake_esp32.py --port 8080
# ターミナル B
$env:IR_DB_PATH = "$env:TEMP\ir_scratch.db"; $env:IR_JOBS_DB_PATH = "$env:TEMP\ir_scratch_jobs.db"
$env:IR_ADVERTISE_HOST = "127.0.0.1"; uv run ir-remocon
# ターミナル C
curl.exe -s -X PUT "http://127.0.0.1:8102/api/devices/1" -H "Content-Type: application/json" -d '{\"host\":\"127.0.0.1:8080\"}'
```

確認する項目:

1. 2 分後の `once` 予約を入れて `GET /api/schedules` に出ること → 待って**実際に発火**し、
   スタブ側に送信が届くこと。発火後にジョブが一覧から消えること。
2. **[E の最終確認]** 毎日予約を入れた状態で `PUT /api/devices/1` の host を別ポートに変更 →
   次の発火が**新しい host** に飛ぶこと（サーバ再起動も予約の作り直しも無し）。
3. 目覚ましを開始 → `GET /api/alarms` に出る → `DELETE /api/alarms/{run_id}` で
   duration を待たずに止まること。サーバ終了 (Ctrl-C) が即座に返ること。
4. スタブを止めた状態で目覚まし → 5 回失敗で中止し、`/api/health` の `last_job_error` に出ること。
5. `/api/health` が `scheduler_running: true` / `job_count` / `device_count` を返すこと。
6. `curl.exe -s -w "\nHTTP %{http_code}\n"` で 404 / 422 / 409 の `detail` が日本語で読めること
   （PowerShell の `Invoke-RestMethod` はエラー本文を読めない）。

### 本番 DB での最終確認

`IR_DB_PATH` / `IR_JOBS_DB_PATH` を外して起動し、
- 既存信号 2 件・機器 1 件が無傷
- 旧 7 件の削除が **ERROR ログに残っている**こと（黙って消えていないこと）
- 起動直後の `/api/schedules` が空、`/api/health` が `ok: true`

を確認する。

---

## 完了時の必須作業

`AGENTS.md` を更新する（プロジェクトの絶対ルール）。

- 進行状況表: Phase 4 を ✅ に、次は Phase 5
- 「Phase 4 で完成したもの」節: ファイル表 + 実測で確認できた事実
- **改名禁止の申し送り**: `ir_remocon.app.jobs:run_signal_job` / `run_wakeup_alarm` は
  pickle された文字列参照。改名 = 既存予約の全消失
- Phase 5 への申し送り: `/api/health` の `ok` を見て赤バナー、`scheduler_running=false` の意味、
  アラームは `DELETE /api/alarms/{run_id}` で止められること、予約作成の 404/422 の出し分け
- 未解決の疑問（学習失敗の原因）の状態を再確認して書き残す

そのうえで**手を止めて報告する**（Phase 5 へは進まない）。

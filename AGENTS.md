# AGENTS.md — ir-remocon

ESP32 + FastAPI による自宅用スマート赤外線リモコン。
現在は段階的なリファクタリング中で、Phase 1〜6 は完了、Phase 7 は未着手。

このファイルは Codex が自動読込するための、短く強制力のある作業規約である。
長い経緯・検証結果・実機手順はここへ重複させず、次の文書を正本とする。

- 文書の読み順と索引: `docs/refactoring/README.md`
- 現在までの詳細な申し送り: `docs/refactoring/HANDOFF.md`
- 全体計画: `docs/refactoring/plans/master-plan.md`
- Phase 別計画: `docs/refactoring/plans/phase-2-plan.md` 〜 `phase-6-plan.md`

## 作業開始時の必須手順

1. このファイルを最後まで読む。
2. `docs/refactoring/README.md` を読む。
3. `git status --short --branch` と `git branch -vv --all` で現在地を確認する。
4. コード・DB・依存関係を変更する場合は `docs/refactoring/HANDOFF.md` の関連節を読む。
5. Phase に関わる作業では、全体計画と該当 Phase の計画を読む。Phase 7 には個別計画がまだ無いので、着手前に現状を再調査して計画を合意する。

ユーザーの未コミット変更は勝手に破棄・上書き・コミットしない。別ブランチの統合、rebase、reset、削除も明示依頼なしに行わない。

## 絶対に守るルール

1. **Python 環境は uv で管理する。** `pip install`、`python -m venv`、`requirements.txt` は使わない。依存追加は `uv add <pkg>`、削除は `uv remove <pkg>`、実行は `uv run ...`。`uv.lock` は追跡対象。
2. **Phase の区切りで必ず停止して報告する。** 明示的な了承なしに次 Phase へ進まない。
3. **DB を触る前にバックアップを取る。** `ir.db` の学習済み信号は実機なしでは再取得できない。`jobs.db` も予約を試すと変更されるため、検証では必ず一時パスへ逃がす。
4. **ESP32 実機での検証は代行できない。** USB 接続、書き込み、赤外線送受信が必要なら手順を示してユーザーへ依頼する。コンパイル成功を実機成功として扱わない。
5. **秘密情報をコミットしない。** Wi-Fi 情報は `esp/ir_remocon/secrets.h` に置き、`secrets.h.example` だけを追跡する。
6. **失敗を成功として扱わない。** ESP32 通信失敗、結果不明、ビジー状態を握り潰さず、既存の型付き例外と HTTP 契約を維持する。

## 現在の Git 状態に関する注意

2026-09-02 の文書整理開始時点では、`master` と `ir-receive-debug` は Phase 6 のコミット
`b756b56` から別々に 1 コミット進んでいる。

- `master`: `2641139 gitignoreを編集`
- `ir-receive-debug`: `528bfb6 閾値以下長さの信号を破棄する`
- 現在の作業ブランチ: `ir-receive-debug`
- リモート: 未設定

`ir-receive-debug` にはファーム v2.1.0 のノイズ除外、受信待受継続、
`secrets.h.example` 導入がある。一方、`master` の `.gitignore` 修正はこのブランチへ未統合。
さらに作業ツリーにはユーザーによる未コミットの `.gitignore` 変更がある。
この状態を解消する操作は文書整理の範囲外であり、勝手に merge / cherry-pick / commit しない。

Git の状態は変わり得るため、上記を盲信せず毎回コマンドで再確認すること。

## 重要な設計不変条件

### ESP32 通信

- `uvicorn` の worker は常に 1。`reload=True` も使わない。送信ロックはプロセスローカルであり、複数プロセスにすると直列化が壊れる。
- ESP32 通信は同期実装を維持する。FastAPI の `def` と APScheduler のワーカースレッドで同じコードを共有する設計。
- 読み取りタイムアウト後は再送しない。トグル信号を二度送る危険がある。リトライは接続確立前の `ConnectError` のみ 1 回。
- httpx の keep-alive は無効のままにする。ESPAsyncWebServer の切断済み接続再利用による `RemoteProtocolError` を避けるため。
- `esp32.py` に HTTP レイヤの都合を持ち込まない。HTTP 変換は例外ハンドラ側、スケジューラは通信層を直接利用する。

### API・repository

- ルータに広い `try/except` を書かない。例外から HTTP への変換は `main.py` に集約する。
- 例外は `GET /api/devices/{id}/status` の `Esp32Error` 捕捉だけ。疎通不可を `reachable:false` という正常な検査結果にするための意図的な例外。
- Pydantic 入力モデルの `extra="forbid"` を維持する。未知フィールドを無視すると誤った既定機器へ送信し得る。
- 機器は常に 1 台以上、既定機器は常にちょうど 1 台。host は repository でも `normalize_host()` を通す。

### スケジューラ

- ジョブに host / IP を保存しない。kwargs は `signal_name`、`device_id`、`meta` を維持し、発火時に DB から host を解決する。
- `ir_remocon.app.jobs`、`run_signal_job`、`run_wakeup_alarm`、保存済み kwargs のキー名を改名・移動しない。APScheduler が文字列参照で pickle している。
- APScheduler は 3.x（`>=3.10,<4`）を維持する。
- `scheduler.running` を健全性の根拠にしない。ハートビートを確認する `scheduler.is_running()` を使う。
- `/api/health` は不健全でも HTTP 200 を返し、`ok` で表現する。`last_job_error` は過去の失敗であり、現在の `ok` とは別概念。
- 終了時は実行中アラームへ停止通知してから `scheduler.shutdown(wait=True)` を行う。

### フロントエンド

- ビルド不要の ES modules を維持する。バンドラ、トランスパイラ、npm を追加しない。
- `required`、`toISOString()`、`innerHTML`、旧 `esp32_ip` を再導入しない。
- 非表示の `<fieldset>` は `hidden` と `disabled` を一緒に切り替え、全フォームは `novalidate` を維持する。
- URL に値を埋め込むときは `seg()` を通す。
- フロント変更後は `tests/test_frontend_assets.py` の回帰ガードを抑制せず、実装を直す。
- ポーリングは画面が visible のときだけ行う。短周期ポーリングには終了条件と連続失敗時の打ち切りを設ける。

### ESP32 ファーム

- `callback_url` はサーバから受け取った値をそのまま使う。信号名を付加したり再構築しない。
- 送信の排他は「バッファコピー完了後に `pendingSend = true`」「送信完了後に最後に `MODE_IDLE`」の順序で成立している。順番を変えない。
- `request->_tempObject` 用の受信バッファは `malloc` で確保する。ライブラリ側が `free()` する。
- 大きい JSON document はヒープに置く。AsyncTCP タスクのスタックへ載せない。
- 受信データが溢れた場合は切り詰めて学習させない。
- ArduinoJson は v6 系を使う。PlatformIO の Flash 使用率も機能追加ごとに確認する。
- ファーム v2.1.0 の実機検証結果は未確定。詳細は `docs/refactoring/HANDOFF.md` に追記してから状態を更新する。

## 検証コマンド

Windows 開発機では pytest の既定一時ディレクトリの権限が壊れているため、先に専用ディレクトリを作る。

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\pytest-ir" | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = "$env:TEMP\pytest-ir"
uv run pytest -q
uv run pytest -m integration -v
uv run pytest tests/test_frontend_assets.py -q
```

統合確認は本番 DB・本番 jobs DB・本番ログを使わず、`IR_DB_PATH`、
`IR_JOBS_DB_PATH`、`IR_LOG_PATH` を一時ファイルへ向けて `tools/fake_esp32.py` を使う。

```powershell
uv run python tools/fake_esp32.py --port 8080
```

ファームのコンパイル確認:

```powershell
cd esp\ir_remocon
$env:PYTHONIOENCODING = "utf-8"
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run
```

## 文書の更新義務

- Phase 完了時は `docs/refactoring/HANDOFF.md` の進行状況、該当節、未解決事項を更新する。
- 作業ルールや重大な不変条件が変わった場合だけ、この `AGENTS.md` も更新する。
- 新しい長文の調査記録を `AGENTS.md` に積み上げない。`docs/refactoring/` に置き、ここからリンクする。
- 推測が外れた事実も残す。同じ調査を次のチャットで繰り返さないため。
- Phase 境界では検証結果、未検証範囲、DBへの影響、残作業を報告して停止する。

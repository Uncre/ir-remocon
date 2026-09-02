# Phase 7 計画 — 周辺整備と運用の仕上げ

## 目的

Phase 1〜6 で完成したアプリと firmware v2.2.0 を、開発機と本番 Linux のどちらでも
安全に再現・運用できる状態へ仕上げる。Phase 7 は新機能の追加ではなく、利用者向け文書、
旧ジョブの退避手段、不要な依存と危険な旧スクリプトの整理、回帰テストを対象とする。

着手前の 2026-09-02 時点では `master` の HEAD は `ca80296`、作業ツリーは clean。
基準テストは単体 226 件、ESP32 スタブ統合 11 件がすべて成功した。

## 再調査で分かったこと

全体計画にあった Phase 7 の項目のうち、次は既に完了している。

- `pyproject.toml`、`uv.lock`、`.python-version` と editable install
- `tools/fake_esp32.py` の送信・状態・学習コールバック
- 信号 CRUD、トリガ生成、ジョブ説明、`next_run=None`、送信失敗、host 変更のテスト
- `.venv`、Python キャッシュ、ログ、SQLite WAL、DB、firmware secrets の ignore

残っている問題は次のとおり。

- ルート `README.md` が無く、導入・起動・本番配置・実機作業の手順が散在している。
- `jinja2` は旧 UI 削除後も直接依存に残っているが、現 UI は `FileResponse` で配信している。
- `ir_remocon/ir_request_test.py` は死んだ IP と未定義の `requests` 依存を持ち、import だけで
  実機へ連続送信する。波形の正本でもなく、安全なツールとして利用できない。
- 旧 `jobs.db` は Phase 4 で手作業退避済みだが、再現可能な移行ツールが無い。

## 実装範囲

### 1. 利用者向け README

ルートに `README.md` を作り、次を一箇所にまとめる。

- uv によるセットアップと起動
- Windows / Linux の環境変数設定
- 設定値一覧と重要な運用上の制約
- DB の場所、バックアップ、旧ジョブの移行
- `fake_esp32.py` を使った実機不要の検証
- Arduino IDE / PlatformIO の使い分け、必要ライブラリ、`secrets.h`
- GPIO13、静的 IP、firmware v2.2.0 の書き込み後確認
- 本番 `uncre-switch` への配置時チェック

### 2. 旧 jobs DB の安全な移行ツール

`tools/migrate_jobs.py` を追加する。

- 既定動作は読み取り専用の棚卸しで、JSON を標準出力へ出す。
- APScheduler の job state を可能な範囲で読み、人が予約を再登録できる情報へ変換する。
- decode できない state があっても処理全体を止めず、エラーと Base64 を JSON に残す。
- 削除は `--apply` と `--confirm-server-stopped` の両方がある場合だけ許可する。
- 適用時は JSON レポートと SQLite backup API による DB バックアップを先に作る。
- バックアップの行数を検証してから、元 DB の `apscheduler_jobs` の行だけを transaction で削除する。
- テーブル構造や DB ファイル自体は削除しない。
- 本番 jobs DB では実行せず、一時 DB のテストで dry-run、backup、clear、失敗時の非破壊性を固定する。

### 3. 不要物の整理

- `uv remove jinja2` で直接依存を削除し、`uv.lock` を更新する。
- `ir_remocon/ir_request_test.py` を削除する。必要な実機送信は UI、API、
  `fake_esp32.py` を利用する。既存の学習済み波形は DB と退避済み DB に残る。
- `ir_db_server.log` の名前は過去の障害記録との連続性を保つため変更しない。

### 4. 文書と回帰テスト

- 本計画を文書索引へ追加する。
- 移行ツールの単体テストを追加する。
- Phase 完了時に `HANDOFF.md` の進行状況、実施内容、検証結果、除外事項を更新する。
- `AGENTS.md` は Phase 7 の状態と、存在しなくなった一時ファイルに関する注意だけを更新する。

## 今回は行わないこと

- 受信バッファ溢れ専用のエラーコールバックは追加しない。GPIO13 修正後は実機で学習から
  再送信まで成功しており、現在は `/status` の `last_recv_overflow` で診断できる。
  実際の overflow 再発が無い段階で firmware・API・UI の契約を広げない。
- `freq` を DB に保存しない。IRremoteESP8266 は搬送波を測定できず、firmware は 38kHz 固定で
  報告しているため、現時点では保存値に根拠が無い。
- firmware は変更しない。したがって Phase 7 の完了条件に実機書き込みや PlatformIO build は
  含めない。
- 本番 `ir_database.db` と `jobs.db` は変更しない。移行ツールの本番適用はサーバ停止と
  ユーザーの明示操作を必要とする運用作業として残す。

## 実装順序

1. 本計画と文書索引を追加する。
2. `migrate_jobs.py` と一時 DB を使うテストを作る。
3. README を作り、依存と旧スクリプトを整理する。
4. `uv lock --locked`、単体、統合、フロント回帰テストを実行する。
5. `HANDOFF.md` と `AGENTS.md` を更新し、Phase 7 の区切りで停止する。

## 完了条件

- README の手順だけでセットアップ、起動、スタブ検証、firmware 準備、本番配置の判断ができる。
- ジョブ移行ツールが明示確認なしに元 DB を変更しない。
- 適用時は JSON と復元可能な DB バックアップが削除より先に作られる。
- `jinja2` と危険な旧送信スクリプトが直接の実行経路から無くなる。
- 単体・統合・フロント回帰テストがすべて成功する。
- 本番 DB、本番 jobs DB、実機に変更を加えていないことを完了報告に明記する。

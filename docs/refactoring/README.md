# リファクタリング文書ガイド

このディレクトリは、Claude Code で進めていた `ir-remocon` のリファクタリングを
Codex へ引き継ぐための、リポジトリ内で完結する文書セットです。

ルートの `AGENTS.md` は Codex が毎回自動読込する短い作業規約です。
詳細な履歴や計画は自動読込へ詰め込まず、作業内容に応じてここから明示的に読みます。

## 読む順番

### 状況確認・説明だけ

1. `AGENTS.md`
2. このファイル
3. 必要な場合だけ `HANDOFF.md` の関連節

### コード、依存関係、DB、ファームを変更する作業

1. `AGENTS.md`
2. このファイル
3. `HANDOFF.md` の関連節と「設計方針」
4. 変更対象に対応する Phase 計画
5. `git status --short --branch` と `git branch -vv --all`

### 新しい Phase の計画・実装・レビュー

1. `AGENTS.md`
2. このファイル
3. `HANDOFF.md` 全文
4. `plans/master-plan.md` 全文
5. 関連する Phase 計画の全文

Phase 7 の個別計画は、既存文書と現行コードを再調査し、2026-09-02 にユーザーと
範囲を合意して作成しました。Phase の区切りでは必ず停止します。

## 文書一覧

| ファイル | 役割 | 更新方針 |
|---|---|---|
| `../../AGENTS.md` | Codex が自動読込する必須ルールと重大な不変条件 | 短く保つ。長い履歴を追加しない |
| `README.md` | 読み順、索引、文書の扱い | 文書構成を変えたときに更新 |
| `HANDOFF.md` | Phase 1〜7 の実装履歴、検証結果、未解決事項、実機手順 | Phase 完了時と新事実判明時に更新 |
| `plans/master-plan.md` | リファクタリング全体の元計画 | 原則として履歴スナップショット |
| `plans/phase-2-plan.md` | ESP32 通信レイヤの元計画 | 同上 |
| `plans/phase-3-plan.md` | 機器登録・IP管理の元計画 | 同上 |
| `plans/phase-4-plan.md` | スケジューラ堅牢化の元計画 | 同上 |
| `plans/phase-5-plan.md` | フロントエンド・学習APIの元計画 | 同上 |
| `plans/phase-6-plan.md` | ESP32ファームの元計画 | 同上 |
| `plans/phase-7-plan.md` | 周辺整備・運用文書・旧 jobs DB 移行の実施計画 | Phase 7 の確定事項を記録 |

Phase 1 は全体計画の中で扱われ、独立した Phase 1 計画書はありません。

## Claude Code から移したファイル

計画書は 2026-09-02 に次のローカルパスから複製しました。元ファイルは削除していません。
今後のエージェント作業では、環境依存の `.claude` 側ではなくリポジトリ内のコピーを参照します。

| 旧ファイル名 | リポジトリ内の名前 |
|---|---|
| `esp32-smooth-waffle.md` | `plans/master-plan.md` |
| `phase2-ethereal-cocke.md` | `plans/phase-2-plan.md` |
| `phase3-logical-neumann.md` | `plans/phase-3-plan.md` |
| `phase4-refactored-widget.md` | `plans/phase-4-plan.md` |
| `phase5-logical-crescent.md` | `plans/phase-5-plan.md` |
| `phase6-compiled-marshmallow.md` | `plans/phase-6-plan.md` |

計画書内に Claude Code 固有の表現が残っていても、内容の来歴を保つためそのままにしています。
実際の作業規約はルート `AGENTS.md` が優先されます。

## 更新時の原則

- 実装済みの事実、当時の計画、今後の提案を混同しない。
- 未検証、実機のみ検証可能、推測、確定事項を明記する。
- 推測が外れた場合も削除せず、外れた事実と新しい根拠を `HANDOFF.md` に残す。
- 一時的な Git ブランチ名や未コミット差分は、作業開始時にコマンドで再確認する。
- DB を使う検証結果には、本番 DB か一時 DB かを明記する。
- Phase の完了報告には、実行した検証、未検証範囲、DBへの影響、残作業を含める。

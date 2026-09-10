# 初版の検証記録 — 2026-09-09

## 2026-09-10追記：mDNS設定定数

`MDNS_HOSTNAME`をスケッチ冒頭に追加。WiFi.setHostnameとMDNS.beginに共用し、
mDNS成功時は設定名のURL、失敗時はIP利用の案内を表示する。
PlatformIO compile_check成功：Flash 957,101 / 2,097,152 bytes、RAM 50,584 / 327,680 bytes。
`node tests/ui.test.cjs`成功。新しい名前での実機アクセス・Arduino IDE全体ビルドは未検証。
既存のArduinoJson先行includeを配布ZIPへ反映（旧ZIPはIDEの依存検出漏れが起きた）。
以下は初版時点の記録。

ユーザー承認済みの単体版構成を`esp/ir_remocon_standalone/`へ実装。
既存のfirmware v2.2.0のraw変換・GPIO13受信・GPIO4送信の考え方を引き継ぎ、
サーバ連携をESP32内の保存とAPIへ置き換えた。既存コード、DB、Wi-Fi秘密情報は変更していない。

## 実行結果

| 検証 | 結果 |
|---|---|
| `pio run -e compile_check` | SUCCESS |
| Flash（アプリ領域2MiB） | 957,001 / 2,097,152 bytes、45.6% |
| 静的RAM | 50,584 / 327,680 bytes、15.4% |
| `node tests/ui.test.cjs` | 成功 |
| 実際の`signal_store.cpp`をg++でホスト実行 | 成功 |
| 既存`tests/test_frontend_assets.py` | 27 passed、既知のStarlette非推奨警告1件 |
| 秘密情報・成果物のignore | `secrets.h`と`.pio`を除外 |

最初のビルドで見つかった`send_P`の非推奨警告は現行`send`へ置き換えて解消。
PlatformIO 7.0.1、Arduino core 2.0.17、依存4ライブラリの版をiniで固定した。
ビルドは既存ユーザーディレクトリのPlatformIOキャッシュアクセスのため権限拡張して実行した。
既存フロントのpytestもuvキャッシュアクセスのため権限拡張して実行した。

画面テストは埋め込みJS本体をNodeの模擬DOM・APIで実行し、学習、送信、ビジー、文字列の安全な表示、
非表示時のポーリング停止、再起動での結果不明、送信の自動再試行なし、5連続失敗で停止、12件上限を確認。
実ブラウザでのレイアウト・アクセシビリティ・実ネットワーク経由のテストは未実施。

保存テストは模擬LittleFS・Preferencesに対し、実装本体を実行。1024要素の往復、範囲外拒否、
上書き拒否、書き込み失敗、rename失敗、切り詰めたファイルの拒否、他信号の保持、
初期化後のマウント失敗で自動フォーマットしないことを確認。
バイナリは版と長さを検証するが、チェックサムはなく、同じ長さの内容変化をすべて検知するものではない。

再実行（このフォルダから。g++が必要）：

```powershell
pio run -e compile_check
node tests/ui.test.cjs
g++ -std=c++17 -Wall -Wextra -I tests/native signal_store.cpp tests/native/store_test.cpp -o .pio/store_test.exe
& .pio/store_test.exe
```

## 未検証・設計上の境界

- ダミー認証情報でのコンパイルのみ。Arduino IDEからの書き込み、実機動作は未検証。
- HTTPの入力検証とmutexによる送受信排他は実装・コード確認済みだが、実ESP32上の負荷試験は未実施。
- LittleFSの実Flashへの永続化・電源断耐性、Wi-Fi再接続、mDNS、動的ヒープ最大使用量は実機確認が必要。
- 初期化済みの保存領域はマウント失敗時に消去しない。初回だけ専用`irdata`領域を初期化する。
  初回判定はNVSの印に依存するため、NVSだけを消去する運用はしない。
- 原版では受信開始をHTTPハンドラで行っていたが、この独立版ではIR操作を`loop()`へまとめた。
  202の直後に`loop()`が受信開始する。教材の実機チェックで学習開始から捕捉まで確認する。
- Flash容量確保のため独自パーティションを同梱。既存ファームと同じ実機を使えば書き換わる。
- 通常のファーム更新時に学習信号を維持する想定。全Flash消去、パーティション変更時は保証しない。
- 信号バックアップ／インポート機能は初版にない。既存SQLite DBも移行しない。
- 送信は38kHz固定。ファームの送信処理完了と、家電が反応したことは別。
- 講座用の回路は部品データシートに基づく案。到達距離、採用ボード、部品による動作は未測定。

次の作業はREADMEの講師向けチェックに沿った実機確認。予約などの拡張は初版の対象外。

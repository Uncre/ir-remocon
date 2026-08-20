/**
 * 複数のタブが共有するデータの読み込み。
 *
 * 信号一覧はリモコン・学習・予約の 3 タブが使い、機器一覧はヘッダの送信先
 * セレクタと設定タブが使う。読み込みをここに集めておかないと、各タブが
 * 勝手に fetch して同じものを何度も取りに行くことになる。
 *
 * app.js は tab-*.js を import するので、共有データの置き場を app.js に
 * すると循環参照になる。だから独立したモジュールにしてある。
 */

import { api } from './api.js';
import { emit, reconcileTargetDevice, state } from './state.js';

export async function refreshSignals() {
  state.signals = await api.get('/api/signals');
  emit('signals');
}

export async function refreshDevices() {
  state.devices = await api.get('/api/devices');
  // 記憶していた送信先が削除済みなら既定へ戻す（そのまま送ると 404 になる）
  reconcileTargetDevice();
  emit('devices');
}

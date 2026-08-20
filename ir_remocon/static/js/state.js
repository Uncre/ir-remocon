/**
 * タブをまたいで共有する状態と、その購読。
 *
 * 主役は 2 つ。
 *
 * 1. **送信先の機器**（`targetDeviceId`）。null は「既定機器に任せる」意味で、
 *    そのときリクエストから device_id を省く。旧実装のように画面の入力欄へ
 *    IP を直書きさせない — IP は DB の機器 1 行に集約され、設定タブで変えれば
 *    送信も予約も即座に追従する。
 *
 * 2. **学習中フラグ**（`learning`）。ESP32 は currentMode 1 本の状態機械なので、
 *    受信モード中の送信は必ず 409 になる。押せてから断られるより、押せない方が
 *    分かりやすい。
 */

const STORAGE_KEY = 'ir-remocon.targetDeviceId';

export const state = {
  /** @type {Array} /api/devices の内容 */
  devices: [],
  /** @type {Array} /api/signals の内容 */
  signals: [],
  /** @type {number|null} null なら既定機器に任せる */
  targetDeviceId: readStoredTarget(),
  /** @type {object|null} 進行中の学習セッション */
  learning: null,
  /** @type {object|null} /api/health の内容 */
  health: null,
};

function readStoredTarget() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null || raw === '') return null;
    const value = Number(raw);
    return Number.isInteger(value) ? value : null;
  } catch (error) {
    return null; // プライベートモード等で localStorage が使えない場合
  }
}

export function setTargetDevice(deviceId) {
  state.targetDeviceId = deviceId;
  try {
    if (deviceId === null) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, String(deviceId));
  } catch (error) { /* 保存できなくても動作には影響しない */ }
  emit('target');
}

/**
 * 記憶していた送信先がまだ存在するか確かめる。
 *
 * 機器を削除した後も localStorage には古い id が残る。そのまま送ると 404 に
 * なるので、消えていたら既定へ戻す。
 */
export function reconcileTargetDevice() {
  if (state.targetDeviceId === null) return;
  if (!state.devices.some((device) => device.id === state.targetDeviceId)) {
    setTargetDevice(null);
  }
}

/** リクエストボディに載せる送信先。既定機器のときはキー自体を省く。 */
export function deviceIdPayload() {
  return state.targetDeviceId === null ? {} : { device_id: state.targetDeviceId };
}

export const isLearning = () => state.learning !== null;

export function setLearning(session) {
  state.learning = session;
  emit('learning');
}

// ---------------------------------------------------------------------------
// 最小限の購読機構
// ---------------------------------------------------------------------------
const listeners = new Map();

export function on(topic, handler) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(handler);
}

export function emit(topic) {
  const handlers = listeners.get(topic);
  if (!handlers) return;
  for (const handler of handlers) {
    try {
      handler();
    } catch (error) {
      // 1 つの購読者の失敗で他を巻き添えにしない
      console.error(`購読者 (${topic}) でエラー:`, error);
    }
  }
}

/**
 * エントリポイント。
 *
 * タブの切り替え、共通ヘッダ（送信先セレクタ）、初回読み込み、
 * 健全性のポーリングを受け持つ。個々の画面は tab-*.js にある。
 *
 * ビルド工程は無い。ES modules をブラウザがそのまま読む。バンドラも
 * トランスパイラも入れないこと — この規模で得られるものより、環境が
 * 増えることの負債の方が大きい。
 */

import { refreshDevices, refreshSignals } from './data.js';
import { $, $$, fillSelect } from './dom.js';
import { refreshHealth } from './health.js';
import { createPoller } from './poll.js';
import { on, setTargetDevice, state } from './state.js';
import { initLearnTab } from './tab-learn.js';
import { initRemoteTab } from './tab-remote.js';
import { initSchedulesTab } from './tab-schedules.js';
import { initSettingsTab } from './tab-settings.js';
import { toastError } from './toast.js';

const TAB_STORAGE_KEY = 'ir-remocon.activeTab';
const HEALTH_INTERVAL = 30000;

function initTabs() {
  const buttons = $$('.tab');
  const names = buttons.map((button) => button.dataset.tab);

  function activate(name) {
    const target = names.includes(name) ? name : names[0];
    for (const button of buttons) {
      const selected = button.dataset.tab === target;
      button.setAttribute('aria-selected', String(selected));
      $(`#panel-${button.dataset.tab}`).hidden = !selected;
    }
    try {
      window.localStorage.setItem(TAB_STORAGE_KEY, target);
    } catch (error) { /* 保存できなくても動作には影響しない */ }
  }

  for (const button of buttons) {
    button.addEventListener('click', () => activate(button.dataset.tab));
  }

  let stored = null;
  try {
    stored = window.localStorage.getItem(TAB_STORAGE_KEY);
  } catch (error) { /* 読めなければ既定のタブ */ }
  activate(stored);
}

/**
 * ヘッダの送信先セレクタ。
 *
 * 先頭の「既定機器」は device_id を送らないことを意味する。既定を切り替えれば
 * 追従するので、機器が 1 台のうちはこれで十分。明示的に選んだ場合だけ
 * device_id を載せる。
 */
function initDevicePicker() {
  const select = $('#target-device');

  select.addEventListener('change', () => {
    setTargetDevice(select.value === '' ? null : Number(select.value));
  });

  function sync() {
    const options = state.devices.map((device) => ({
      value: String(device.id),
      label: device.is_default ? `${device.name}（既定）` : device.name,
    }));
    fillSelect(select, options, '既定の機器を使う');
    select.value = state.targetDeviceId === null ? '' : String(state.targetDeviceId);
  }

  on('devices', sync);
  on('target', sync);
  sync();
}

async function loadInitialData() {
  const results = await Promise.allSettled([
    refreshDevices(),
    refreshSignals(),
    refreshHealth(),
  ]);
  const failure = results.find((result) => result.status === 'rejected');
  if (failure) toastError(failure.reason, 'サーバからデータを取得できませんでした');
}

function main() {
  initTabs();
  initDevicePicker();
  initRemoteTab();
  initLearnTab();
  initSchedulesTab();
  initSettingsTab();

  loadInitialData();
  createPoller('health', refreshHealth, HEALTH_INTERVAL).start();
}

main();

/**
 * 設定タブ — 機器の登録・編集・接続テストと、サーバの状態表示。
 *
 * 旧実装はここが「ESP32 の IP アドレス」というテキスト入力 1 個で、その値を
 * 送信のたびにリクエストへ載せていた。IP が変わると画面も既存の予約も全部
 * 壊れる（不具合 E）。今は機器が DB の 1 行になっていて、ここで host を
 * 変えれば送信も予約も次の 1 回から新しい宛先を向く。
 */

import { api, guard, seg } from './api.js';
import { refreshDevices } from './data.js';
import { $, el, emptyRow, render } from './dom.js';
import { formatDateTime } from './format.js';
import { on, setTargetDevice, state } from './state.js';
import { toastError, toastSuccess, toastWarn } from './toast.js';

/** 編集中の機器 id。null なら新規追加。 */
let editingId = null;
/** 接続テストの結果（機器 id -> {reachable, host, detail, status}） */
const testResults = new Map();

export function initSettingsTab() {
  $('#device-add').addEventListener('click', () => openForm(null));
  $('#device-cancel').addEventListener('click', closeForm);
  $('#device-form').addEventListener('submit', onSubmit);

  on('devices', renderDevices);
  on('health', renderServerInfo);
  renderDevices();
  renderServerInfo();
}

// ---------------------------------------------------------------------------
// 機器一覧
// ---------------------------------------------------------------------------
function renderDevices() {
  const list = $('#device-list');
  if (state.devices.length === 0) {
    render(list, emptyRow('機器がありません。追加してください。'));
    return;
  }
  render(list, state.devices.map(renderDeviceRow));
}

function renderDeviceRow(device) {
  const result = testResults.get(device.id);

  return el('li', { class: 'list-item' }, [
    el('div', { class: 'item-main' }, [
      el('div', { class: 'item-title' }, [
        device.name,
        ' ',
        device.is_default ? el('span', { class: 'badge badge-default', text: '既定' }) : null,
      ]),
      el('span', { class: 'item-sub', text: device.host }),
      result ? renderTestResult(result) : null,
    ]),
    el('div', { class: 'item-actions' }, [
      el('button', {
        type: 'button', class: 'btn btn-secondary btn-small', text: '接続テスト',
        onclick: (event) => testDevice(device, event.currentTarget),
      }),
      el('button', {
        type: 'button', class: 'btn btn-secondary btn-small', text: '編集',
        onclick: () => openForm(device),
      }),
      el('button', {
        type: 'button', class: 'btn btn-danger btn-small', text: '削除',
        onclick: (event) => deleteDevice(device, event.currentTarget),
      }),
    ]),
  ]);
}

function renderTestResult(result) {
  const parts = [
    el('span', {
      class: result.reachable ? 'badge badge-ok' : 'badge badge-ng',
      text: result.reachable ? '応答あり' : '応答なし',
    }),
    // どのアドレスを叩いたかを必ず出す。設定ミスに自力で気づけるように。
    ` ${result.host}`,
  ];
  if (result.detail) parts.push(el('span', { class: 'item-sub', text: result.detail }));
  if (result.status) {
    parts.push(el('span', { class: 'item-sub', text: describeStatus(result.status) }));
  }
  return el('span', { class: 'item-sub' }, parts);
}

function describeStatus(status) {
  return Object.entries(status)
    .map(([key, value]) => `${key}=${value}`)
    .join(' / ');
}

/**
 * 接続テスト。
 *
 * ★このエンドポイントは到達できなくても HTTP 200 を返す。判定は `reachable`
 *   の真偽で行うこと。ステータスコードで判断すると常に「成功」になる。
 *   到達できないことは API の失敗ではなく、テストの正常な結果。
 */
async function testDevice(device, button) {
  await guard(`test:${device.id}`, button, async () => {
    try {
      const result = await api.get(`/api/devices/${seg(device.id)}/status`);
      testResults.set(device.id, result);
      renderDevices();
      if (result.reachable) toastSuccess(`${device.name}（${result.host}）から応答がありました。`);
      else toastWarn(`${device.name}（${result.host}）から応答がありません。${result.detail || ''}`);
    } catch (error) {
      toastError(error);
    }
  });
}

// ---------------------------------------------------------------------------
// 追加・編集
// ---------------------------------------------------------------------------
function openForm(device) {
  editingId = device === null ? null : device.id;
  $('#device-form-title').textContent = device === null ? '機器を追加' : `「${device.name}」を編集`;
  $('#device-name').value = device === null ? '' : device.name;
  $('#device-host').value = device === null ? '' : device.host;
  $('#device-default').checked = device === null ? false : device.is_default;
  $('#device-form').hidden = false;
  $('#device-name').focus();
}

function closeForm() {
  editingId = null;
  $('#device-form').hidden = true;
}

async function onSubmit(event) {
  event.preventDefault();
  const name = $('#device-name').value.trim();
  const host = $('#device-host').value.trim();
  const isDefault = $('#device-default').checked;

  if (name === '') { toastWarn('名前を入力してください。'); return; }
  if (host === '') { toastWarn('ホストを入力してください。'); return; }

  await guard('device:save', $('#device-save'), async () => {
    try {
      if (editingId === null) {
        await api.post('/api/devices', { name, host, is_default: isDefault });
        toastSuccess(`機器「${name}」を追加しました。`);
      } else {
        await api.put(`/api/devices/${seg(editingId)}`, { name, host, is_default: isDefault });
        // host を変えた場合、既存の予約もすべてこの瞬間から新しい宛先を向く。
        toastSuccess(`機器「${name}」を更新しました。予約も次の 1 回から新しい宛先になります。`);
        testResults.delete(editingId);
      }
      closeForm();
      await refreshDevices();
    } catch (error) {
      // 既定を外そうとした場合は 409 ConstraintViolation。サーバが
      //「先に別の機器を既定にしてください」と書いているのでそのまま出す。
      toastError(error);
    }
  });
}

async function deleteDevice(device, button) {
  if (!window.confirm(`機器「${device.name}」を削除しますか？\nこの機器を指している予約は発火時に失敗するようになります。`)) {
    return;
  }
  await guard(`device:${device.id}`, button, async () => {
    try {
      const result = await api.del(`/api/devices/${seg(device.id)}`);
      testResults.delete(device.id);
      // 既定機器を消すとサーバが別の機器を自動で昇格させる。
      // 送信先が変わったことを黙って隠さない。
      if (result.new_default_device_id !== null && result.new_default_device_id !== undefined) {
        toastWarn(result.message, { duration: 8000 });
      } else {
        toastSuccess(result.message);
      }
      if (state.targetDeviceId === device.id) setTargetDevice(null);
      await refreshDevices();
    } catch (error) {
      // 最後の 1 台は 409。機器が 0 台になると送信も予約も全滅するため。
      toastError(error);
    }
  });
}

// ---------------------------------------------------------------------------
// サーバの状態
// ---------------------------------------------------------------------------
function renderServerInfo() {
  const box = $('#server-info');
  const health = state.health;
  if (health === null) {
    render(box, [
      el('dt', { text: '状態' }),
      el('dd', { text: 'サーバに接続できません' }),
    ]);
    return;
  }

  const rows = [
    ['状態', health.ok ? '正常' : '異常（バナーを確認してください）'],
    ['スケジューラ', health.scheduler_running ? '稼働中' : '停止'],
    ['データベース', health.db_ok ? '正常' : '読み書きできません'],
    ['予約の件数', String(health.job_count)],
    ['鳴っている目覚まし', String(health.running_alarms)],
    ['機器の台数', String(health.device_count)],
    ['既定の機器', health.default_device_name || '(未設定)'],
    // ★ 学習が失敗するときの切り分けの要。ESP32 から見えるアドレスかどうかを
    //   ユーザが自分で確かめられるようにする。
    ['学習コールバック', `${health.callback_base_url || '(不明)'}/api/callback/ir_signal/...`],
    ['最終ハートビート', formatDateTime(health.last_heartbeat)],
    ['直近のジョブエラー', health.last_job_error || 'なし'],
  ];

  render(box, rows.flatMap(([term, value]) => [
    el('dt', { text: term }),
    el('dd', { text: value }),
  ]));
}

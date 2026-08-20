/**
 * 予約タブ — 予約の作成・一覧・削除と、鳴っている目覚ましの停止。
 *
 * ここが体感バグの主犯だった場所（不具合 A）。旧実装は非表示の select に
 * required を残していたため、モードを切り替えると **見えない必須項目が未入力の
 * まま制約検証に引っかかり、ブラウザが submit を握り潰していた**。submit
 * イベント自体が発火しないので JS のエラー表示も出ず、「押しても何も起きない」
 * ように見えた。リロード直後は必ず再発し、両方のモードを一度ずつ触ると通る
 * ようになる — これが「時々効かない」の正体。
 *
 * 対策は dom.js の toggleFieldset()（hidden と disabled を必ず一緒に動かす）と
 * <form novalidate>、そして検証を JS に一本化すること。
 */

import { api, guard, seg } from './api.js';
import { $, $$, el, emptyRow, fillSelect, render, toggleFieldset } from './dom.js';
import {
  combineLocal, formatDateTime, formatRemaining,
  localDateValue, localTimeValue, parseServerDate,
} from './format.js';
import { createPoller } from './poll.js';
import { deviceIdPayload, on, state } from './state.js';
import { toastError, toastSuccess, toastWarn } from './toast.js';

const ALARM_IDLE_INTERVAL = 30000;
const ALARM_ACTIVE_INTERVAL = 5000;

let jobs = [];
let alarms = [];
let jobPoller = null;
let alarmPoller = null;
let alarmTicker = null;

export function initSchedulesTab() {
  $('#schedule-kind').addEventListener('change', syncFormMode);
  $('#repeat-type').addEventListener('change', syncFormMode);
  $('#schedule-form').addEventListener('submit', onSubmit);

  // ★ 既定値は必ず現地時刻で作る（不具合 F）。format.js を参照。
  $('#schedule-date').value = localDateValue();
  $('#schedule-time').value = localTimeValue();
  syncFormMode();

  on('signals', fillSignalSelects);
  fillSignalSelects();

  jobPoller = createPoller('jobs', refreshJobs, 60000);
  alarmPoller = createPoller('alarms', refreshAlarms, ALARM_IDLE_INTERVAL);
  jobPoller.start();
  alarmPoller.start();
  refreshJobs();
  refreshAlarms();
}

function fillSignalSelects() {
  const options = state.signals.map((signal) => ({ value: signal.name, label: signal.name }));
  for (const id of ['#signal-select', '#on-signal-select', '#off-signal-select']) {
    // 選択済みの信号が一覧に残っていれば選択を維持する（学習で信号が
    // 増えるたびに選び直させない）
    fillSelect($(id), options, '-- 信号を選択 --');
  }
}

// ---------------------------------------------------------------------------
// フォームのモード切り替え
// ---------------------------------------------------------------------------
function syncFormMode() {
  const isWakeup = $('#schedule-kind').value === 'wakeup';
  toggleFieldset($('#fs-signal'), !isWakeup);
  toggleFieldset($('#fs-wakeup'), isWakeup);

  const repeat = $('#repeat-type').value;
  toggleFieldset($('#fs-date'), repeat === 'once');
  toggleFieldset($('#fs-weekday'), repeat === 'weekly');
}

// ---------------------------------------------------------------------------
// 予約の作成
// ---------------------------------------------------------------------------
async function onSubmit(event) {
  event.preventDefault();
  const button = $('#schedule-submit');
  const payload = buildPayload();
  if (payload === null) return; // 検証で弾いた（理由はトースト済み）

  await guard('schedule:create', button, async () => {
    try {
      const result = await api.post(payload.path, payload.body);
      toastSuccess(result.message || '予約を登録しました。');
      await refreshJobs();
    } catch (error) {
      // 400 InvalidSchedule（過去日時）/ 422（継続時間の上限超過）も
      // サーバが日本語で理由を書いているのでそのまま出す。
      toastError(error);
    }
  });
}

function buildPayload() {
  const repeatType = $('#repeat-type').value;
  const time = $('#schedule-time').value;
  if (time === '') {
    toastWarn('時刻を入力してください。');
    return null;
  }

  const body = { ...deviceIdPayload(), repeat_type: repeatType, execute_time: time };

  if (repeatType === 'once') {
    const date = $('#schedule-date').value;
    if (date === '') {
      toastWarn('日付を入力してください。');
      return null;
    }
    // サーバも過去日時を 400 で断るが、往復を待たせず先に伝える。
    const at = combineLocal(date, time);
    if (at !== null && at.getTime() <= Date.now()) {
      toastWarn('過去の日時は予約できません。日付と時刻を確認してください。');
      return null;
    }
    body.execute_date = date;
  }

  if (repeatType === 'weekly') {
    const days = $$('#weekday-group input:checked').map((box) => box.value);
    if (days.length === 0) {
      toastWarn('曜日を 1 つ以上選んでください。');
      return null;
    }
    body.repeat_days = days;
  }

  if ($('#schedule-kind').value === 'signal') {
    const name = $('#signal-select').value;
    if (name === '') {
      toastWarn('信号を選んでください。');
      return null;
    }
    return { path: '/api/schedule', body: { ...body, name } };
  }

  const onSignal = $('#on-signal-select').value;
  const offSignal = $('#off-signal-select').value;
  if (onSignal === '' || offSignal === '') {
    toastWarn('ON と OFF の信号を両方選んでください。');
    return null;
  }
  const interval = Number($('#interval-seconds').value);
  const duration = Number($('#duration-seconds').value);
  if (!Number.isFinite(interval) || interval <= 0) {
    toastWarn('間隔は 0 より大きい数値を入力してください。');
    return null;
  }
  if (!Number.isInteger(duration) || duration <= 0) {
    toastWarn('継続時間は 1 以上の整数を入力してください。');
    return null;
  }

  return {
    path: '/api/schedule/wakeup',
    body: {
      ...body,
      on_signal_name: onSignal,
      off_signal_name: offSignal,
      interval_seconds: interval,
      duration_seconds: duration,
    },
  };
}

// ---------------------------------------------------------------------------
// 予約一覧
// ---------------------------------------------------------------------------
async function refreshJobs() {
  jobs = await api.get('/api/schedules');
  renderJobs();
}

function renderJobs() {
  const list = $('#job-list');
  if (jobs.length === 0) {
    render(list, emptyRow('予約はありません。'));
    return;
  }
  render(list, jobs.map(renderJobRow));
}

function renderJobRow(job) {
  // device_name が "(削除済み id=N)" になっている予約は、発火しても必ず失敗する。
  // 黙って並べず赤字で出す。
  const deviceMissing = typeof job.device_name === 'string' && job.device_name.startsWith('(削除済み');

  return el('li', { class: 'list-item' }, [
    el('div', { class: 'item-main' }, [
      el('div', { class: 'item-title', text: job.name }),
      el('span', { class: 'item-sub', text: job.schedule_description }),
      el('span', { class: 'item-sub', text: `次回: ${formatDateTime(job.next_run)}` }),
      el('span', {
        class: deviceMissing ? 'item-sub is-danger' : 'item-sub',
        text: `送信先: ${job.device_name || '(不明)'}`,
      }),
    ]),
    el('div', { class: 'item-actions' }, [
      el('button', {
        type: 'button', class: 'btn btn-danger btn-small', text: '削除',
        onclick: (event) => deleteJob(job, event.currentTarget),
      }),
    ]),
  ]);
}

async function deleteJob(job, button) {
  if (!window.confirm(`予約「${job.name}」を削除しますか？`)) return;
  await guard(`job:${job.id}`, button, async () => {
    try {
      await api.del(`/api/schedules/${seg(job.id)}`);
      toastSuccess('予約を削除しました。');
      await refreshJobs();
    } catch (error) {
      toastError(error);
    }
  });
}

// ---------------------------------------------------------------------------
// 鳴っている目覚まし（不具合 G — 旧実装には止める手段が無かった）
// ---------------------------------------------------------------------------
async function refreshAlarms() {
  alarms = await api.get('/api/alarms');
  // 鳴っている間だけ速く見に行く。普段は 30 秒で十分。
  alarmPoller.setInterval(alarms.length > 0 ? ALARM_ACTIVE_INTERVAL : ALARM_IDLE_INTERVAL);
  renderAlarms();
  syncAlarmTicker();
}

/** 残り時間の表示だけは 1 秒ごとに動かす（取得は 5 秒間隔のまま）。 */
function syncAlarmTicker() {
  if (alarms.length > 0 && alarmTicker === null) {
    alarmTicker = setInterval(renderAlarms, 1000);
  } else if (alarms.length === 0 && alarmTicker !== null) {
    clearInterval(alarmTicker);
    alarmTicker = null;
  }
}

function renderAlarms() {
  const card = $('#alarm-card');
  card.hidden = alarms.length === 0;
  if (alarms.length === 0) return;

  render($('#alarm-list'), alarms.map((alarm) => {
    const endsAt = parseServerDate(alarm.ends_at);
    const remaining = endsAt === null ? 0 : (endsAt.getTime() - Date.now()) / 1000;
    return el('li', { class: 'list-item' }, [
      el('div', { class: 'item-main' }, [
        el('div', { class: 'item-title', text: `${alarm.on_signal} / ${alarm.off_signal}` }),
        el('span', { class: 'item-sub', text: `残り ${formatRemaining(remaining)}` }),
      ]),
      el('div', { class: 'item-actions' }, [
        el('button', {
          type: 'button', class: 'btn btn-danger btn-small', text: '今すぐ止める',
          onclick: (event) => stopAlarm(alarm, event.currentTarget),
        }),
      ]),
    ]);
  }));
}

async function stopAlarm(alarm, button) {
  await guard(`alarm:${alarm.run_id}`, button, async () => {
    try {
      await api.del(`/api/alarms/${seg(alarm.run_id)}`);
      toastSuccess('目覚ましを止めました。');
      await refreshAlarms();
    } catch (error) {
      toastError(error);
    }
  });
}

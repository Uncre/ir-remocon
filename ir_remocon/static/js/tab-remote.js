/**
 * リモコンタブ — 登録済みの信号を送る / 名前を変える / 消す。
 */

import { api, guard, seg } from './api.js';
import { refreshSignals } from './data.js';
import { $, el, emptyRow, render } from './dom.js';
import { deviceIdPayload, isLearning, on, state } from './state.js';
import { toastError, toastSuccess } from './toast.js';

export function initRemoteTab() {
  on('signals', renderList);
  on('learning', renderList);
  on('target', renderList);
  renderList();
}

function renderList() {
  const list = $('#signal-list');
  const note = $('#remote-lock-note');
  note.hidden = !isLearning();

  if (state.signals.length === 0) {
    render(list, emptyRow('登録済みの信号がありません。「学習」タブから追加してください。'));
    return;
  }

  render(list, state.signals.map(renderRow));
}

function renderRow(signal) {
  const sendButton = el('button', {
    type: 'button',
    class: 'btn btn-primary btn-small',
    text: '送信',
    // 学習中は ESP が受信モードなので送信は必ず 409 になる。押せない方が親切。
    disabled: isLearning(),
    onclick: (event) => sendSignal(signal.name, event.currentTarget),
  });

  return el('li', { class: 'list-item' }, [
    el('div', { class: 'item-main' }, [
      el('div', { class: 'item-title', text: signal.name }),
    ]),
    el('div', { class: 'item-actions' }, [
      sendButton,
      el('button', {
        type: 'button', class: 'btn btn-secondary btn-small', text: '名前変更',
        onclick: (event) => renameSignal(signal.name, event.currentTarget),
      }),
      el('button', {
        type: 'button', class: 'btn btn-danger btn-small', text: '削除',
        onclick: (event) => deleteSignal(signal.name, event.currentTarget),
      }),
    ]),
  ]);
}

async function sendSignal(name, button) {
  await guard(`send:${name}`, button, async () => {
    try {
      const result = await api.post(`/api/send/${seg(name)}`, deviceIdPayload());
      // どこへ送ったかまで出す。送信先の設定ミスにユーザが自力で気づけるように。
      // 「送信しました」と断定しないのは、ファーム v2.0.0 が 202 (キュー投入) を
      // 即返す設計で、赤外線が実際に放射されたことまでは保証しないため。
      toastSuccess(`「${name}」を ${result.device_name}（${result.host}）へ送信を指示しました。`);
    } catch (error) {
      toastError(error);
    }
  });
}

async function renameSignal(name, button) {
  const input = window.prompt(`「${name}」の新しい名前:`, name);
  if (input === null) return;
  const newName = input.trim();
  if (newName === '' || newName === name) return;

  await guard(`rename:${name}`, button, async () => {
    try {
      await api.put(`/api/signals/${seg(name)}`, { name: newName });
      toastSuccess(`「${name}」を「${newName}」に変更しました。`);
      await refreshSignals();
    } catch (error) {
      // 既存名への変更は 409 DuplicateName。サーバの日本語の理由をそのまま出す。
      toastError(error);
    }
  });
}

async function deleteSignal(name, button) {
  if (!window.confirm(`「${name}」を削除しますか？\nこの信号を使っている予約は発火時に失敗するようになります。`)) {
    return;
  }
  await guard(`delete:${name}`, button, async () => {
    try {
      await api.del(`/api/signals/${seg(name)}`);
      toastSuccess(`「${name}」を削除しました。`);
      await refreshSignals();
    } catch (error) {
      toastError(error);
    }
  });
}

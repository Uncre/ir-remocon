/**
 * トースト表示。
 *
 * 種別を 4 つに分けているのが要点。旧実装は success / error / info の 3 つで、
 * 「失敗ではないが成功でもない」応答を表現できなかった。
 *
 *   success … 実際に成功した
 *   error   … 実際に失敗した
 *   warn    … 失敗と言い切れない。409 ビジー / 504 結果不明。**赤にしない**
 *   info    … 進行中の案内
 *
 * 409（ロック待ち超過）は「飽和したので正直に断った」状態であって故障ではない。
 * 504 は「送ったが応答が返らなかった」であり、赤外線が出たかどうかは不明。
 * どちらも「失敗しました」と描くと、ユーザは実態と違う判断をしてしまう。
 */

import { $, el } from './dom.js';

const DEFAULT_DURATION = 4500;

export function toast(message, tone = 'info', { title = null, duration = DEFAULT_DURATION } = {}) {
  const container = $('#toasts');
  if (container === null) return;

  const node = el('div', { class: `toast toast-${tone}`, role: 'status' }, [
    title ? el('div', { class: 'toast-title', text: title }) : null,
    el('div', { text: message }),
  ]);
  container.append(node);

  const remove = () => node.remove();
  const timer = setTimeout(remove, duration);
  node.addEventListener('click', () => {
    clearTimeout(timer);
    remove();
  });
  return node;
}

export const toastSuccess = (message, options) => toast(message, 'success', options);
export const toastInfo = (message, options) => toast(message, 'info', options);
export const toastWarn = (message, options) => toast(message, 'warn', options);

/**
 * 例外をトーストにする。
 *
 * ApiError なら describe() の判定（tone / title）に従う。それ以外
 * （ネットワーク断など）は素直にエラー扱い。
 */
export function toastError(error, fallback = '操作に失敗しました') {
  if (error && typeof error.describe === 'function') {
    const { tone, title, message } = error.describe();
    return toast(message, tone, { title, duration: tone === 'warn' ? 6000 : 7000 });
  }
  const message = error && error.message ? error.message : fallback;
  return toast(message, 'error', { duration: 7000 });
}

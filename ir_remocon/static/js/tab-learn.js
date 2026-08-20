/**
 * 学習タブ — ESP32 を受信モードにして、実際に届いた信号を保存する。
 *
 * ★旧実装との一番の違いは「結果が分かること」。
 *   旧フロントは受信モードにした後 `setTimeout(refreshAll, 16000)` で 16 秒後に
 *   一覧を引き直すだけだった。成功したのか、時間切れだったのか、そもそも ESP に
 *   届いていなかったのかは画面のどこにも出ない。「学習が時々失敗する」という
 *   体感だけが残り、切り分けようが無かった。
 *
 *   今はサーバがセッションを持っているので、GET /api/learn/{token} を
 *   ポーリングすれば pending / success / timeout / error の実結果が取れる。
 */

import { api, guard, seg } from './api.js';
import { refreshSignals } from './data.js';
import { $, el, render } from './dom.js';
import { formatRemaining, parseServerDate } from './format.js';
import { createPoller } from './poll.js';
import { deviceIdPayload, setLearning, state } from './state.js';
import { toastError, toastSuccess, toastWarn } from './toast.js';

/** 画面に出しているセッション（進行中とは限らない。結果表示にも使う） */
let displayed = null;
let poller = null;
/** ポーリングの連続失敗回数。回線の瞬断で諦めないための猶予。 */
let pollFailures = 0;
const MAX_POLL_FAILURES = 5;

export function initLearnTab() {
  $('#learn-form').addEventListener('submit', onSubmit);
  poller = createPoller('learn', pollOnce, 1000);
  restorePendingSession();
}

/**
 * 進行中のセッションを復元する。
 *
 * 再読込や別タブから開いた直後は「学習中」を知らないので、そのままだと
 * 受信モードの ESP に送信して必ず 409 を踏む。サーバに聞けば分かる。
 */
async function restorePendingSession() {
  try {
    const sessions = await api.get('/api/learn');
    if (sessions.length === 0) return;
    displayed = sessions[0];
    pollFailures = 0;
    setLearning(displayed);
    render$();
    poller.start();
  } catch (error) {
    // 復元できなくても操作はできる。黙って諦める。
    console.warn('学習セッションの復元に失敗:', error);
  }
}

async function onSubmit(event) {
  event.preventDefault();
  const input = $('#learn-name');
  const name = input.value.trim();
  const button = $('#learn-submit');

  // 検証は JS 側に一本化してある（HTML の required は使わない）。
  if (name === '') {
    toastWarn('信号の名前を入力してください。');
    input.focus();
    return;
  }

  await guard('learn:start', button, async () => {
    try {
      const session = await api.post('/api/learn', {
        ...deviceIdPayload(),
        name,
        overwrite: $('#learn-overwrite').checked,
      });
      input.value = '';
      displayed = session;
      pollFailures = 0;
      setLearning(session);
      render$();
      poller.start();
    } catch (error) {
      // 409 DuplicateName（同名あり）も 409 LearnAlreadyRunning もここに来る。
      // サーバが日本語で理由と次の操作を書いているのでそのまま出す。
      toastError(error);
      if (error.error === 'LearnAlreadyRunning') restorePendingSession();
    }
  });
}

async function pollOnce() {
  if (displayed === null || displayed.status !== 'pending') {
    poller.stop();
    return;
  }

  // ★ ここで例外を外に出してはいけない。poll.js は失敗しても淡々と再実行するので、
  //   1 秒間隔のこのポーリングだけは自分で打ち切らないと無限リトライになる。
  //   実際に起きうるのは「学習中にサーバが再起動してセッションが消えた」場合
  //   （登録簿はプロセスメモリなので 404 が返り続ける）。
  let session;
  try {
    session = await api.get(`/api/learn/${seg(displayed.token)}`);
    pollFailures = 0;
  } catch (error) {
    const gone = error.status === 404;
    pollFailures += 1;
    if (!gone && pollFailures < MAX_POLL_FAILURES) return; // 一時的な失敗は見送る

    poller.stop();
    setLearning(null);
    displayed = {
      ...displayed,
      status: 'error',
      message: gone
        ? '学習セッションが失われました（サーバが再起動した可能性があります）。もう一度やり直してください。'
        : `状態を確認できません: ${error.message}`,
    };
    render$();
    return;
  }

  displayed = session;
  render$();

  if (session.status === 'pending') return;

  poller.stop();
  setLearning(null);

  if (session.status === 'success') {
    toastSuccess(`「${session.name}」を学習しました（${session.raw_length} 要素）。`);
    await refreshSignals();
  } else if (session.status === 'timeout') {
    toastWarn('時間内に信号を受信できませんでした。もう一度お試しください。');
  } else {
    toastError(new Error(session.message || '学習に失敗しました'));
  }
}

// ---------------------------------------------------------------------------
// 表示
// ---------------------------------------------------------------------------
function render$() {
  const box = $('#learn-status');
  const submit = $('#learn-submit');

  if (displayed === null) {
    box.hidden = true;
    submit.disabled = false;
    return;
  }
  box.hidden = false;
  box.className = `learn-status is-${displayed.status}`;
  submit.disabled = displayed.status === 'pending';
  render(box, buildStatusNodes(displayed));
}

function buildStatusNodes(session) {
  if (session.status === 'pending') return buildPending(session);

  const headlines = {
    success: `「${session.name}」を学習しました`,
    timeout: '信号を受信できませんでした',
    error: '学習に失敗しました',
  };
  const nodes = [
    el('p', { class: 'learn-headline', text: headlines[session.status] || session.status }),
  ];
  if (session.message) nodes.push(el('div', { text: session.message }));

  if (session.status === 'timeout') {
    nodes.push(el('p', {
      class: 'hint',
      text: 'ESP32 の受信部にリモコンを向け、開始してから時間内にボタンを押してください。'
        + '何度やっても届かない場合は、設定タブの「学習コールバック」が ESP32 から'
        + '見える LAN 側のアドレスになっているか確認してください。',
    }));
  }
  nodes.push(el('button', {
    type: 'button', class: 'btn btn-secondary btn-small', text: '閉じる',
    onclick: () => { displayed = null; render$(); },
  }));
  return nodes;
}

function buildPending(session) {
  const expires = parseServerDate(session.expires_at);
  const remaining = expires === null ? 0 : (expires.getTime() - Date.now()) / 1000;
  const ratio = session.timeout_seconds > 0
    ? Math.max(0, Math.min(1, remaining / session.timeout_seconds))
    : 0;

  return [
    el('p', { class: 'learn-headline', text: `「${session.name}」を待っています` }),
    el('div', {
      text: `${session.device_name} の受信部にリモコンを向けて、ボタンを押してください。`,
    }),
    el('div', { class: 'learn-countdown', text: formatRemaining(remaining) }),
    el('div', { class: 'progress' }, [
      el('div', { class: 'progress-bar', style: `width: ${(ratio * 100).toFixed(1)}%` }),
    ]),
  ];
}

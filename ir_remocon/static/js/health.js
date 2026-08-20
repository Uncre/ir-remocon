/**
 * 健全性バナー。
 *
 * ★ /api/health は不健全でも HTTP 200 を返す。判定は `ok` の真偽で行うこと。
 *   ステータスコードで判断すると、何が起きても「健全」に見える。
 *
 * ★ `ok` と `last_job_error` は別物なので、別の見せ方をする。
 *     ok:false        … 機構そのものが動いていない → 赤。予約は 1 件も発火しない
 *     last_job_error  … 機構は動いているが直近のジョブが失敗した → 黄
 *   同一視して両方赤にすると、「1 回失敗しただけ」と「数ヶ月動いていない」が
 *   同じ見た目になる。不具合 D は後者が誰にも気づかれずに放置された事象なので、
 *   その区別こそがこのバナーの存在理由。
 */

import { api } from './api.js';
import { $, el, render } from './dom.js';
import { emit, state } from './state.js';

export async function refreshHealth() {
  try {
    state.health = await api.get('/api/health');
  } catch (error) {
    // health 自体が取れないのはサーバが落ちているとき。それも赤で伝える。
    state.health = null;
  }
  renderBanners();
  emit('health');
}

function renderBanners() {
  const container = $('#banners');
  const health = state.health;
  const banners = [];

  if (health === null) {
    banners.push(banner('danger', 'サーバに接続できません',
      'ページを開いたままサーバが停止した可能性があります。復旧すると自動で消えます。'));
    render(container, banners);
    return;
  }

  if (!health.ok) {
    // 何が落ちているのかを具体的に書く。「異常です」だけでは次の行動が決まらない。
    const causes = [];
    if (!health.scheduler_running) causes.push('スケジューラが動いていません');
    if (!health.db_ok) causes.push('データベースに読み書きできません');
    banners.push(banner('danger', '予約が動作していません',
      `${causes.join(' / ') || '原因不明'}。この状態では予約は 1 件も実行されません。`
      + 'サーバのログを確認し、再起動してください。'));
  }

  if (health.device_count === 0) {
    banners.push(banner('danger', '機器が 1 台も登録されていません',
      '送信も予約もできません。設定タブから機器を追加してください。'));
  }

  if (health.last_job_error) {
    banners.push(banner('warn', '直近の予約実行が失敗しました', health.last_job_error));
  }

  render(container, banners);
}

function banner(tone, title, message) {
  return el('div', { class: `banner banner-${tone}`, role: 'alert' }, [
    el('div', { class: 'banner-body' }, [
      el('strong', { text: title }),
      el('span', { text: message }),
    ]),
  ]);
}

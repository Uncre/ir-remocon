/**
 * 定期実行。
 *
 * ★タブが見えていないときは動かさない。
 *
 * この画面はスマホから Tailscale 経由で開く使い方をしている。バックグラウンドで
 * ポーリングを続けると、モバイル回線とバッテリーを黙って消費し続けることになる。
 * `visibilitychange` で止め、復帰時に即座に 1 回走らせれば、体感は変わらないまま
 * 無駄が消える。
 *
 * setInterval ではなく setTimeout の連鎖にしてあるのは、応答が遅いときに
 * 実行が重なるのを防ぐため（回線が細いときほど重なりやすい）。
 */

const pollers = new Set();

export function createPoller(name, run, intervalMs) {
  const poller = {
    name,
    intervalMs,
    timer: null,
    running: false,
    stopped: true,
  };

  async function tick() {
    poller.timer = null;
    if (poller.stopped || document.visibilityState !== 'visible') return;
    if (poller.running) return;

    poller.running = true;
    try {
      await run();
    } catch (error) {
      // ポーリングの失敗はトーストにしない。回線が切れているだけで画面が
      // トーストで埋まると、本当の操作の結果が読めなくなる。
      console.warn(`ポーリング (${name}) に失敗:`, error);
    } finally {
      poller.running = false;
      schedule();
    }
  }

  function schedule() {
    if (poller.stopped || poller.timer !== null) return;
    poller.timer = setTimeout(tick, poller.intervalMs);
  }

  poller.start = () => {
    poller.stopped = false;
    schedule();
  };

  poller.stop = () => {
    poller.stopped = true;
    if (poller.timer !== null) {
      clearTimeout(poller.timer);
      poller.timer = null;
    }
  };

  /** 間隔を変える（鳴っている目覚ましがある間だけ速くする、等）。 */
  poller.setInterval = (ms) => {
    if (poller.intervalMs === ms) return;
    poller.intervalMs = ms;
    if (poller.timer !== null) {
      clearTimeout(poller.timer);
      poller.timer = null;
      schedule();
    }
  };

  /** 今すぐ 1 回走らせ、次回を測り直す。操作直後に呼ぶ。 */
  poller.runNow = () => {
    if (poller.timer !== null) {
      clearTimeout(poller.timer);
      poller.timer = null;
    }
    return tick();
  };

  pollers.add(poller);
  return poller;
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  for (const poller of pollers) {
    if (!poller.stopped) poller.runNow();
  }
});

/**
 * 日時・時間の整形。
 *
 * ★ここに toISOString() を書いてはいけない（不具合 F の再発）。
 *
 * 旧実装は日付の既定値を `now.toISOString().split('T')[0]` で作っていた。
 * toISOString() は UTC を返すので、JST では **00:00〜08:59 の間だけ前日の
 * 日付**が入る。しかも隣の行の時刻は toTimeString()（現地時刻）で作られて
 * いたため、日付と時刻でタイムゾーンが食い違っていた。結果、朝に「一回のみ」
 * の予約を作ると過去日時になり、misfire 次第で即発火するか黙って捨てられるか
 * が変わる（どちらもユーザには「予約したのに動かなかった」に見える）。
 *
 * 現在のサーバは過去日時の once を 400 で断るので黙って消えることは無いが、
 * そもそも正しい既定値を出すのがこちらの仕事。
 * tests/test_frontend_assets.py が toISOString の不在を見張っている。
 */

const pad = (n) => String(n).padStart(2, '0');

/** <input type="date"> に入れる値（現地時刻の YYYY-MM-DD）。 */
export function localDateValue(date = new Date()) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** <input type="time"> に入れる値（現地時刻の HH:MM）。 */
export function localTimeValue(date = new Date()) {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * サーバが返す日時文字列を Date にする。
 *
 * サーバからは 2 種類来る。
 *   - naive（例: "2026-08-19T20:55:35.369103"）— learn / health。現地時刻として解釈される
 *   - aware（例: "2026-08-20T08:05:00+09:00"）— 予約の next_run
 * どちらも Date のコンストラクタが正しく扱うが、マイクロ秒（小数 6 桁）は
 * 実装によって解釈が揺れるのでミリ秒に丸めておく。
 */
export function parseServerDate(value) {
  if (!value) return null;
  const normalized = String(value).replace(/(\.\d{3})\d+/, '$1');
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 一覧に出す日時。 */
export function formatDateTime(value) {
  const date = parseServerDate(value);
  if (date === null) return '—';
  return date.toLocaleString('ja-JP', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

/** 残り時間を「1分30秒」の形にする。 */
export function formatRemaining(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes > 0 ? `${minutes}分${rest}秒` : `${rest}秒`;
}

/** 現地時刻で「日付 + 時刻」を Date にする（過去日時の事前チェック用）。 */
export function combineLocal(dateValue, timeValue) {
  if (!dateValue || !timeValue) return null;
  const [year, month, day] = dateValue.split('-').map(Number);
  const [hour, minute] = timeValue.split(':').map(Number);
  if ([year, month, day, hour, minute].some((n) => Number.isNaN(n))) return null;
  return new Date(year, month - 1, day, hour, minute, 0, 0);
}

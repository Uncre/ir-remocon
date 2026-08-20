/**
 * API クライアント。
 *
 * このファイルの本体はエラーの解釈。サーバは失敗を握り潰さない設計になった
 * ので、フロント側がその情報を捨てないことが対になる。
 *
 * サーバのエラー応答は 2 形態ある。
 *
 *   ドメイン例外 : {detail: string, error: "Esp32Unreachable" 等,
 *                   host?: string, outcome_unknown?: boolean}
 *   FastAPI 422  : {detail: [{type, loc, msg, input}, ...]}   ← **配列**
 *
 * 旧実装は `throw new Error(err.detail)` としていたため、422 では
 * `error.message` が "[object Object]" になっていた。実際、旧フロントが
 * 新 API を叩くと必ず 422 になるので、この整形が無いと画面には
 * 「予約エラー: [object Object]」しか出ない。
 */

/** リクエスト先。相対パスにはしない（旧実装は先頭の / を消していて配置に依存していた）。 */
const BASE = '';

export class ApiError extends Error {
  constructor(status, body) {
    super(formatDetail(status, body));
    this.name = 'ApiError';
    this.status = status;
    this.detail = this.message;
    this.error = body && typeof body.error === 'string' ? body.error : null;
    this.host = body && typeof body.host === 'string' ? body.host : null;
    this.outcomeUnknown = Boolean(body && body.outcome_unknown);
  }

  /**
   * トーストの描き分け。
   *
   * ★「失敗」と言い切ってよいのかを、ここ 1 箇所で決める。
   */
  describe() {
    if (this.outcomeUnknown) {
      return {
        tone: 'warn',
        title: '送信できたか不明です',
        message:
          '機器が時間内に応答しませんでした。赤外線が出たかどうかは分かりません。'
          + '家電の状態を目で確認してください。',
      };
    }
    if (this.error === 'Esp32LocalBusy' || this.error === 'Esp32DeviceBusy') {
      return {
        tone: 'warn',
        title: '機器がビジーです',
        message: '少し待ってからもう一度お試しください。',
      };
    }
    if (this.error === 'Esp32Unreachable') {
      return {
        tone: 'error',
        title: '機器に接続できません',
        message: `${this.detail}（設定タブの「接続テスト」で確認できます）`,
      };
    }
    if (this.status === 422) {
      return { tone: 'error', title: '入力を確認してください', message: this.detail };
    }
    return { tone: 'error', title: null, message: this.detail };
  }
}

function formatDetail(status, body) {
  if (body && Array.isArray(body.detail)) {
    // FastAPI の検証エラー。loc の先頭は "body" / "query" なので落とす。
    const parts = body.detail.map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : '';
      const msg = item.msg || '不正な値です';
      return where ? `${where}: ${msg}` : msg;
    });
    return parts.join(' / ') || `HTTP ${status}`;
  }
  if (body && typeof body.detail === 'string') return body.detail;
  if (body && typeof body.message === 'string') return body.message;
  return `HTTP ${status}`;
}

async function request(method, path, body = undefined) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(BASE + path, options);
  } catch (cause) {
    // サーバ自体に届いていない（ネットワーク断・サーバ停止）
    throw new ApiError(0, { detail: 'サーバに接続できません。起動しているか確認してください。' });
  }

  const text = await response.text();
  let parsed = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch (cause) {
      parsed = { detail: text.slice(0, 300) };
    }
  }

  if (!response.ok) throw new ApiError(response.status, parsed);
  return parsed === null ? {} : parsed;
}

/** パスパラメータは必ずこれを通す。信号名に / や # が入ると URL が壊れるため。 */
export const seg = (value) => encodeURIComponent(String(value));

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
  del: (path) => request('DELETE', path),
};

// ---------------------------------------------------------------------------
// 多重送信ガード
// ---------------------------------------------------------------------------
const inFlight = new Set();

/**
 * 同じキーの処理が走っている間、後続の呼び出しを黙って捨てる。
 *
 * 連打対策はサーバ側（機器ごとのロック + 最小送信間隔）にも入っているが、
 * そちらは「飽和したら 409 で断る」ところまでしかできない。ボタンを押した
 * 本人にとっては、押せてしまってから断られるより押せない方が分かりやすい。
 *
 * @param {string} key
 * @param {HTMLElement|null} button - 押下中 disabled + スピナーにする要素
 * @param {Function} run
 */
export async function guard(key, button, run) {
  if (inFlight.has(key)) return undefined;
  inFlight.add(key);
  if (button) {
    button.disabled = true;
    button.classList.add('is-busy');
  }
  try {
    return await run();
  } finally {
    inFlight.delete(key);
    if (button) {
      button.disabled = false;
      button.classList.remove('is-busy');
    }
  }
}

export const isBusy = (key) => inFlight.has(key);

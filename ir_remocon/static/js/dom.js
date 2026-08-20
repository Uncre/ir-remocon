/**
 * DOM 組み立てヘルパー。
 *
 * ★このプロジェクトの JS は innerHTML を一切使わない。
 *
 * 旧実装は信号名を未エスケープでテンプレート文字列に埋め、`innerHTML` に
 * 代入していた（信号一覧・select の option・予約一覧の 3 箇所）。信号名は
 * 学習時やリネームでユーザが自由に付けられるので、`<img src=x onerror=...>`
 * のような名前を付ければそのまま実行される状態だった。
 *
 * escapeHtml() を通す方針もあるが、それは「毎回通し忘れないこと」に依存する。
 * el() で必ず textContent / setAttribute 経由にすれば、そもそも解釈される
 * 経路が存在しなくなる。tests/test_frontend_assets.py が innerHTML の不在を
 * 見張っている。
 */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) =>
  Array.from(root.querySelectorAll(selector));

/**
 * 要素を作る。
 *
 * @param {string} tag
 * @param {object} props - class / text / dataset / on* / 真偽属性 / その他属性
 * @param {Array|Node|string} children
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);

  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;

    if (key === 'class') {
      node.className = value;
    } else if (key === 'text') {
      // ★ 文字列は必ずここを通る。HTML として解釈される経路が無い
      node.textContent = value;
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (typeof node[key] === 'boolean') {
      node[key] = value;
    } else {
      node.setAttribute(key, String(value));
    }
  }

  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** 子要素を丸ごと差し替える。空配列でクリアになる。 */
export function render(node, children) {
  node.replaceChildren(...[].concat(children).filter(Boolean));
}

/** 一覧が空のときの行。 */
export function emptyRow(message) {
  return el('li', { class: 'empty', text: message });
}

/**
 * select の選択肢を組み直す。可能なら選択状態を維持する。
 *
 * @param {HTMLSelectElement} select
 * @param {Array<{value: string, label: string}>} options
 * @param {string|null} placeholder - 先頭に置く未選択項目
 */
export function fillSelect(select, options, placeholder = null) {
  const previous = select.value;
  const nodes = [];
  if (placeholder !== null) {
    nodes.push(el('option', { value: '', text: placeholder }));
  }
  for (const option of options) {
    nodes.push(el('option', { value: option.value, text: option.label }));
  }
  render(select, nodes);
  if (options.some((o) => o.value === previous)) {
    select.value = previous;
  } else if (placeholder !== null) {
    select.value = '';
  }
}

/**
 * fieldset の表示を切り替える。
 *
 * ★ hidden と disabled を **必ず両方** 動かすこと（不具合 A の根治）。
 *   hidden だけだと、見えない入力がブラウザの制約検証に残り submit が
 *   握り潰される。disabled だけだと画面に残ってしまう。disabled な
 *   fieldset の子孫は制約検証の対象外かつ送信対象外になる、というのが
 *   HTML の仕様上の正攻法。
 */
export function toggleFieldset(fieldset, active) {
  fieldset.hidden = !active;
  fieldset.disabled = !active;
}

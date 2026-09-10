#pragma once
#include <Arduino.h>
const char WEB_UI[] PROGMEM = R"IRHTML(<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>わたしの赤外線リモコン</title>
<style>
:root{font-family:system-ui,sans-serif;color:#183b38;background:#f2f5ef;line-height:1.7;color-scheme:light}
*{box-sizing:border-box}body{margin:0}main{max-width:680px;margin:auto;padding:32px 20px}
h1{font-size:1.8rem;line-height:1.4;margin:6px 0}h2{font-size:1.2rem;margin-top:0}
.eyebrow{font-size:.8rem;letter-spacing:.15em;color:#466b63}header{margin-bottom:24px}
section{background:white;border:1px solid #d6e0d6;border-radius:20px;padding:24px;margin:20px 0}
button,input{font:inherit;border-radius:10px;padding:10px 14px}button{cursor:pointer;border:1px solid #1d6253;background:#1d6253;color:white;min-height:46px}
button:disabled{opacity:.45;cursor:wait}.secondary{background:white;color:#36584e;border-color:#b8cbc0}
input{width:100%;border:1px solid #9aad9f;margin:8px 0 14px}label{display:block}
ul{padding:0;list-style:none}li{display:flex;align-items:center;gap:10px;padding:14px 0;border-bottom:1px solid #e5ebe1;flex-wrap:wrap}
.name{flex:1;min-width:130px;overflow-wrap:anywhere}.hint{font-size:.9rem;color:#526c63}
#message{background:#e3eddf;border-radius:12px;padding:16px;overflow-wrap:anywhere}
#countdown{font-variant-numeric:tabular-nums;font-weight:600}button:focus-visible,input:focus-visible{outline:3px solid #d99a22;outline-offset:3px}
footer{font-size:.85rem;color:#526c63}@media(max-width:420px){main{padding:20px 14px}section{padding:18px}h1{font-size:1.5rem}}
</style>
</head>
<body><main>
<header><div class="eyebrow">ESP32 · MY FIRST CIRCUIT</div><h1>わたしの赤外線リモコン</h1>
<p class="hint">いつものリモコンを覚えさせて、スマートフォンから操作しよう。</p></header>
<p id="message" role="status" aria-live="polite">ESP32に接続しています…</p>
<p id="countdown" aria-live="off"></p>
<button id="refresh" class="secondary" type="button">状態を確認</button>
<section><h2>登録済みリモコン <span id="total"></span></h2>
<ul id="signals"></ul><p class="hint">送信したら、家電が反応したか目で確認してください。</p></section>
<section><h2>新しいボタンを覚えさせる</h2>
<form id="learn" novalidate><label for="name">ボタンの名前</label>
<input id="name" name="name" placeholder="例：照明をつける" autocomplete="off" maxlength="60">
<p class="hint">開始してから15秒以内に、受信部にリモコンを向けてボタンを短く押します。最大12個まで登録できます。</p>
<button id="start" type="submit" disabled>学習を開始</button></form></section>
<footer>同じWi-Fiから使えます。学習したボタンは電源を切っても残ります。</footer>
<script type="module">
const $ = id => document.getElementById(id);
const seg = value => encodeURIComponent(String(value));
let state = null, signals = [], locked = true, requestBusy = false;
let timer = null, failures = 0, deadline = 0, expected = null, polling = false, checking = false;
const tell = text => { $('message').textContent = text; };
async function api(path, method = 'GET', body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const options = {method, signal: controller.signal, headers:{'X-IR-Request':'1'}};
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) { const error = new Error(result.message || '操作できませんでした。'); error.status = response.status; throw error; }
    return result;
  } catch (error) {
    if (!error.status) throw new Error(method === 'GET'
      ? 'ESP32の状態を確認できません。同じWi-Fiと電源を確認してください。'
      : '操作結果が不明です。自動で再送しません。家電や登録一覧を確認してください。');
    throw error;
  } finally { clearTimeout(timeout); }
}
function render() {
  $('start').disabled = locked || requestBusy || checking || signals.length >= 12;
  $('name').disabled = locked || requestBusy;
  $('total').textContent = `(${signals.length}/12)`;
  $('signals').replaceChildren();
  if (!signals.length) { const item = document.createElement('li'); item.textContent = 'まだ登録されていません。下のフォームから覚えさせましょう。'; $('signals').append(item); }
  for (const signal of signals) {
    const item = document.createElement('li'), name = document.createElement('span');
    name.className = 'name'; name.textContent = signal.name;
    const send = document.createElement('button'); send.type = 'button'; send.textContent = '送信';
    send.disabled = locked || requestBusy || checking || !signal.valid;
    send.addEventListener('click', () => act('/api/send', 'POST', {id:signal.id}));
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '削除'; remove.className = 'secondary';
    remove.disabled = locked || requestBusy || checking;
    remove.addEventListener('click', () => {
      if (confirm(`「${signal.name}」を削除しますか？もう一度使うには学習し直します。`)) act(`/api/signals?id=${seg(signal.id)}`, 'DELETE');
    });
    item.append(name, send, remove); $('signals').append(item);
  }
}
function stop() { polling = false; clearTimeout(timer); timer = null; }
function schedule() {
  clearTimeout(timer);
  if (polling && document.visibilityState === 'visible') timer = setTimeout(check, 1000);
}
async function check() {
  if (checking || requestBusy || document.visibilityState !== 'visible') return;
  checking = true; render();
  try {
    state = await api('/api/status'); failures = 0;
    locked = state.mode !== 'idle' || !state.storage_ok;
    $('countdown').textContent = state.mode === 'learn' ? `あと ${Math.ceil(state.remaining_ms / 1000)} 秒` : '';
    if (expected && (expected.boot !== state.boot || expected.operation !== state.operation)) {
      tell('ESP32が再起動したか、別の操作が行われました。前の操作結果は不明です。家電や登録一覧を確認してください。');
      expected = null;
    } else tell(state.storage_ok ? state.message : '保存領域を開けません。シリアルモニターを確認してください。');
    if (state.mode === 'idle') {
      stop(); expected = null;
      if (state.storage_ok) signals = await api('/api/signals');
    } else if (Date.now() > deadline) {
      stop(); tell('操作の完了を確認できません。状態を確認ボタンで再確認してください。');
    } else { polling = true; schedule(); }
  } catch (error) {
    locked = true;
    if (++failures >= 5 || Date.now() > deadline) { stop(); tell(error.message); }
    else { polling = true; schedule(); }
  } finally { checking = false; render(); }
}
async function act(path, method, body) {
  if (locked || requestBusy || checking) return;
  requestBusy = true; render(); stop();
  try {
    const result = await api(path, method, body);
    expected = result.operation === undefined ? null : result;
    tell(expected ? '受け付けました。結果を確認しています…' : '削除しました。');
    locked = Boolean(expected); failures = 0; deadline = Date.now() + 25000;
    polling = true; schedule();
  } catch (error) { locked = true; tell(error.message + ' 「状態を確認」で再確認できます。'); }
  finally { requestBusy = false; render(); }
}
$('learn').addEventListener('submit', event => {
  event.preventDefault();
  const name = $('name').value.trim();
  if (!name || new TextEncoder().encode(name).length > 60) { tell('名前を60バイト以内（日本語20文字程度）で入力してください。'); return; }
  act('/api/learn', 'POST', {name});
});
$('refresh').addEventListener('click', () => {
  if (requestBusy) return;
  stop(); failures = 0; deadline = Date.now() + 25000; polling = true; check();
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { deadline = Date.now() + 25000; failures = 0; polling = true; check(); }
  else clearTimeout(timer);
});
deadline = Date.now() + 25000; polling = true; check();
</script></main></body></html>)IRHTML";

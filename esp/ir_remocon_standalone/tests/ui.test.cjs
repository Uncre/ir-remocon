// Execute the actual embedded module with a minimal DOM and simulated API.
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = readFileSync(join(__dirname, '../web_ui.h'), 'utf8');
const script = source.match(/<script type="module">([\s\S]*?)<\/script>/)[1];
class Element {
  children = []; events = {}; textContent = ''; disabled = false; value = '';
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, fn) { this.events[name] = fn; }
}
const elements = Object.fromEntries([...source.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
const timers = new Map(); let timerId = 0, fail = false, posts = 0;
const status = {mode:'idle', outcome:'idle', storage_ok:true, message:'ready', boot:'a', operation:0};
let rows = [{id:0, name:'<img src=x onerror=alert(1)>', valid:true}];
const document = {visibilityState:'visible', getElementById:id => {
  assert.ok(elements[id], `Missing DOM id ${id}`); return elements[id];
}, createElement:() => new Element(), addEventListener:(name, fn) => { document[name] = fn; }};
const context = vm.createContext({document, console, TextEncoder, AbortController, Date,
  confirm:() => true, setTimeout:fn => { timers.set(++timerId, fn); return timerId; },
  clearTimeout:id => timers.delete(id), fetch:async (path, options) => {
    if (options.method === 'POST') posts++;
    if (fail) throw new Error('offline');
    let body;
    if (path === '/api/status') body = {...status};
    else if (path === '/api/signals') body = rows;
    else if (options.method === 'POST') { status.operation++; status.mode = path.endsWith('learn') ? 'learn' : 'send'; status.remaining_ms = 15000; body = {boot:status.boot, operation:status.operation}; }
    else body = {};
    return {ok:true, json:async () => body};
  }});
vm.runInContext(script, context);
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const run = code => vm.runInContext(code, context);
(async () => {
  await settle();
  assert.equal(elements.start.disabled, false);
  assert.equal(elements.signals.children[0].children[0].textContent, rows[0].name);
  assert.equal(timers.size, 0, 'Idle does not poll');
  elements.name.value = '照明'; elements.learn.events.submit({preventDefault(){}});
  await settle(); assert.equal(posts, 1);
  await run("act('/api/send', 'POST', {id:0})"); assert.equal(posts, 1, 'Busy does not send');
  await run('check()'); assert.equal(elements.countdown.textContent, 'あと 15 秒');
  document.visibilityState = 'hidden'; document.visibilitychange();
  assert.equal(timers.size, 0, 'Hidden document stops timer');
  status.mode = 'idle'; status.message = '学習して保存しました'; status.outcome = 'success';
  rows.push({id:1, name:'照明', valid:true});
  document.visibilityState = 'visible'; document.visibilitychange(); await settle();
  assert.equal(elements.total.textContent, '(2/12)'); assert.equal(timers.size, 0);
  fail = true;
  await run("act('/api/send', 'POST', {id:0})");
  assert.match(elements.message.textContent, /操作結果が不明/); assert.equal(posts, 2);
  assert.equal(timers.size, 0, 'Uncertain mutation is not retried');
  await run("act('/api/send', 'POST', {id:0})"); assert.equal(posts, 2);
  fail = false; elements.refresh.events.click(); await settle();
  await run("act('/api/send', 'POST', {id:0})");
  status.boot = 'reboot'; status.mode = 'idle'; status.operation = 0;
  await run('check()'); assert.match(elements.message.textContent, /前の操作結果は不明/);
  fail = true; run('deadline = Date.now() + 60000; polling = true; failures = 0');
  for (let i = 0; i < 5; i++) await run('check()');
  assert.equal(timers.size, 0, 'Repeated failures stop polling');
  assert.equal(elements.start.disabled, true);
  fail = false; elements.refresh.events.click(); await settle();
  rows = Array.from({length:12}, (_,id) => ({id, name:String(id), valid:true}));
  await run('check()'); assert.equal(elements.start.disabled, true, 'Capacity disables learning');
  console.log('UI checks passed: learn, send, busy, text escaping, visibility, restart, no retry, failure cutoff, capacity.');
})().catch(error => { console.error(error); process.exitCode = 1; });

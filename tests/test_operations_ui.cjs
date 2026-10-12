const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

function element() {
  return { value: '', checked: true, disabled: false, dataset: { logCap: '1024' }, children: [], listeners: {},
    textContent: '', setAttribute(key, value) { this[key] = value; },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(kind, callback) { this.listeners[kind] = callback; },
  };
}

async function harness() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  get('log-tail').value = '200';
  const streams = [];
  const timers = new Map();
  let nextTimer = 0;
  class EventSource {
    constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
    addEventListener(kind, callback) { this.listeners[kind] = callback; }
    close() { this.closed = true; }
    emit(kind, data) { this.listeners[kind]({ data: JSON.stringify(data) }); }
  }
  const window = { location: {}, addEventListener() {}, Overseer: {
    requestJson: async url => url.includes('history')
      ? { project: 'demo', instance: 'one', records: [] }
      : [{ id: 'abc', name: '<script>web</script>' }],
    clearMessage() {}, showError(message) { throw Error(message); },
  } };
  const context = vm.createContext({
    document: { getElementById: get, querySelector: () => element(), createElement: element, createTextNode: text => text },
    window, EventSource, TextEncoder, URLSearchParams,
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async () => ({ ok: true, text: async () => '2026-10-12T10:00:00.000000001Z <script>alert(1)</script>\n' }),
  });
  vm.runInContext(readFileSync('overseer/static/operations.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return { get, streams, timers, window };
}

test('event pause/resume keeps the cursor and restart resets it', async () => {
  const h = await harness();
  const first = h.streams[0];
  first.emit('record', { id: 7, source: 'alert', kind: 'exit', service: '<b>web</b>', timestamp: '2026-10-12T10:00:00Z', message: '<script>untrusted</script>' });
  assert.equal(h.get('timeline').children.length, 1);
  h.get('events-pause').listeners.click();
  assert.equal(first.closed, true);
  first.emit('end', {});
  assert.equal(h.timers.size, 0);
  h.get('events-pause').listeners.click();
  const second = h.streams.at(-1);
  assert.equal(second.url, '/api/events?after=7');
  second.emit('status', { project: 'demo', status: 'connected', instance: 'two' });
  assert.equal(second.closed, true);
  assert.equal(h.streams.at(-1).url, '/api/events?after=0');
});

test('logs support literal text, timestamps, search, reconnect cursor and a byte cap', async () => {
  const h = await harness();
  h.get('log-container').value = 'abc';
  await h.get('log-container').listeners.change();
  assert.match(h.get('log-output').textContent, /<script>alert\(1\)<\/script>/);
  h.get('log-timestamps').checked = false;
  h.get('log-timestamps').listeners.change();
  assert.equal(h.get('log-output').textContent, '<script>alert(1)</script>');
  h.get('logs-live').listeners.click();
  const live = h.streams.at(-1);
  assert.match(live.url, /follow=1/);
  assert.match(live.url, /since=1791799200/);
  live.emit('log', { text: '2026-10-12T10:00:01.000000001Z visible', truncated: false });
  live.emit('log', { text: '2026-10-12T10:00:01.000000001Z visible', truncated: false });
  assert.equal(h.get('log-output').textContent.match(/visible/g).length, 1);
  for (let i = 0; i < 30; i++) live.emit('log', { text: 'line ' + i + 'x'.repeat(100), truncated: false });
  assert.ok(new TextEncoder().encode(h.get('log-output').textContent).length <= 1024);
  h.get('log-search').value = 'line 29';
  h.get('log-search').listeners.input();
  assert.match(h.get('log-output').textContent, /^line 29/);
  live.emit('end', {});
  assert.equal(h.timers.size, 1);
  h.get('logs-live').listeners.click();
  assert.equal(h.timers.size, 0);
  h.get('logs-download').listeners.click();
  assert.match(h.window.location.href, /download=1/);
});

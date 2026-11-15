const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const createStore = require('./demo-store.js');

test('sample loading and simulated incoming stay isolated per tab', async () => {
  const first = createStore();
  const second = createStore();
  assert.equal((await first.request('/api/state')).messages.length, 0);
  const loaded = await first.request('/api/demo/load', {});
  assert.deepEqual(loaded.messages.map((m) => m.platform), ['WhatsApp', 'Telegram', 'MAX']);
  assert.equal((await second.request('/api/state')).messages.length, 0);
  loaded.messages[0].text = 'modified copy';
  assert.notEqual((await first.request('/api/state')).messages[0].text, 'modified copy');
  const next = await first.request('/demo/next', {});
  assert.equal(next.messages.length, 4);
  assert.match(next.messages[3].text, /^Учебный пример:/);
  assert.equal(new Set(next.messages.map((m) => m.id)).size, 4);
});

test('draft, status and stale revisions are handled safely', async () => {
  const store = createStore();
  const state = await store.request('/api/demo/load', {});
  const first = state.messages[0];
  const saved = await store.request(`/api/messages/${first.id}/draft`, { text: 'Учебный ответ', version: first.version });
  assert.equal(saved.messages[0].draft, 'Учебный ответ');
  await assert.rejects(store.request(`/api/messages/${first.id}/draft`, { text: 'obsolete', version: first.version }), { status: 409 });
  assert.equal((await store.request('/api/state')).messages[0].draft, 'Учебный ответ');
  const status = await store.request(`/api/messages/${first.id}/status`, { status: 'ready', version: saved.messages[0].version });
  assert.equal(status.messages[0].status, 'ready');
  assert.equal(status.messages[0].draft, 'Учебный ответ');
  await assert.rejects(store.request(`/api/messages/${first.id}/status`, { status: 'sent', version: status.messages[0].version }), { status: 400 });
});

test('reload/reset cannot reuse a stale version or lose a draft on adding a sample', async () => {
  const store = createStore();
  const first = (await store.request('/api/demo/load', {})).messages[0];
  const saved = await store.request(`/api/messages/${first.id}/draft`, { text: 'Пример', version: first.version });
  const added = await store.request('/demo/next', {});
  assert.equal(added.messages[0].draft, 'Пример');
  await store.request('/api/demo/load', {});
  await assert.rejects(store.request(`/api/messages/${first.id}/draft`, { text: 'stale', version: saved.messages[0].version }), { status: 409 });
  assert.equal((await createStore().request('/api/state')).messages.length, 0);
});

test('invalid actions and large drafts are rejected', async () => {
  const store = createStore();
  const m = (await store.request('/api/demo/load', {})).messages[0];
  await assert.rejects(store.request('/api/anything', {}), { status: 404 });
  await assert.rejects(store.request('/demo/next', { value: 'unexpected' }), { status: 400 });
  await assert.rejects(store.request(`/api/messages/${m.id}/draft`, { text: 'x'.repeat(2001), version: m.version }), { status: 400 });
  await assert.rejects(store.request(`/api/messages/${m.id}/draft`, { text: 'x', version: m.version, extra: 'x' }), { status: 400 });
});

test('frontend does not send data or persist it in browser storage', () => {
  const app = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
  const store = fs.readFileSync(path.join(__dirname, 'demo-store.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  for (const code of [app, store]) {
    assert.doesNotMatch(code, /\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|\bEventSource\b|\bsendBeacon\b|\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|\bdocument\.cookie\b|\.innerHTML\s*=/);
  }
  assert.match(html, /connect-src 'none'/);
  assert.match(html, /только в памяти этой вкладки/);
  assert.match(html, /Мессенджеры не подключены/);
  assert.match(html, /id="next-demo"/);
  assert.doesNotMatch(html, /https?:\/\//);
});

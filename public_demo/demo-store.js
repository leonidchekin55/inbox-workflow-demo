/* A self-contained, per-tab training store. It never talks to a server. */
(function (root, createStore) {
  if (typeof module === 'object' && module.exports) module.exports = createStore;
  else root.demoStore = createStore();
})(globalThis, function createStore() {
  'use strict';
  const templates = [
    { id: 'received', label: 'Обращение получено', text: 'Здравствуйте! Спасибо за обращение. Уточню детали и вернусь с ответом.' },
    { id: 'clarify', label: 'Нужно уточнение', text: 'Здравствуйте! Подскажите, пожалуйста, дополнительные детали вашего вопроса.' },
    { id: 'waiting', label: 'Ожидание ответа', text: 'Здравствуйте! Напоминаю о нашем вопросе. Когда будет удобно, дайте, пожалуйста, ответ.' },
  ];
  const channels = ['WhatsApp', 'Telegram', 'MAX'];
  const statuses = new Set(['new', 'in_progress', 'ready', 'done']);
  let rows = [];
  let sequence = 0;
  let revision = 0;
  function error(status) { const result = new Error('Invalid demo action'); result.status = status; throw result; }
  function snapshot() { return { messages: rows.map((row) => ({...row})), templates: templates.map((row) => ({...row})) }; }
  function createMessage(index) {
    const channel = channels[index % channels.length];
    const text = [
      'Учебный пример: хочу уточнить порядок обращения.',
      'Учебный пример: подскажите, какие шаги предусмотрены?',
      'Учебный пример: ожидаю уточнения по запросу.',
    ][index % 3];
    sequence += 1;
    return { id: `demo-${sequence}`, platform: channel, account_id: String(index % 3 + 1),
      chat_id: `demo-chat-${sequence}`, text, status: 'new', draft: '',
      created_at: new Date().toISOString(), version: ++revision };
  }
  async function request(path, body) {
    if (path === '/api/state' && body === undefined) return snapshot();
    if (path === '/api/demo/load' || path === '/demo/next') {
      if (!body || Object.keys(body).length !== 0) return error(400);
      if (path === '/api/demo/load') {
        sequence = 0;
        rows = [0, 1, 2].map(createMessage);
        rows[1].status = 'in_progress';
        rows[2].status = 'ready';
      } else {
        if (rows.length >= 100) return error(400);
        rows.push(createMessage(sequence % 3));
      }
      return snapshot();
    }
    const route = /^\/api\/messages\/([a-z0-9-]{1,40})\/(draft|status)$/.exec(path);
    if (!route || !body || typeof body !== 'object' || Array.isArray(body)) return error(404);
    const message = rows.find((row) => row.id === route[1]);
    if (!message) return error(404);
    const field = route[2] === 'draft' ? 'text' : 'status';
    if (Object.keys(body).sort().join(',') !== [field, 'version'].sort().join(',')) return error(400);
    if (!Number.isSafeInteger(body.version) || body.version < 1) return error(400);
    if (body.version !== message.version) return error(409);
    if (field === 'text') {
      if (typeof body.text !== 'string' || body.text.length > 2000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(body.text)) return error(400);
      message.draft = body.text;
    } else {
      if (!statuses.has(body.status)) return error(400);
      message.status = body.status;
    }
    message.version = ++revision;
    return snapshot();
  }
  return Object.freeze({ request });
});

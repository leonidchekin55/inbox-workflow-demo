(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const platforms = { whatsapp: 'WhatsApp', telegram: 'Telegram', max: 'MAX' };
  const statuses = { new: 'Новое', in_progress: 'В работе', ready: 'Готово к проверке', done: 'Завершено' };
  let state = { messages: [], templates: [] };
  let selectedId = null;
  let savedDraft = '';
  let busy = false;
  let conflicted = false;
  let conflictSnapshot = null;

  function feedback(text, isError = false) {
    $('feedback').textContent = text;
    $('feedback').classList.toggle('error', isError);
  }
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = String(text);
    if (className) el.className = className;
    return el;
  }
  function nameOfPlatform(value) {
    return platforms[String(value || '').toLowerCase()] || 'Другой канал';
  }
  function nameOfStatus(value) {
    return statuses[value] || 'Неизвестен';
  }
  function selectedMessage() {
    return state.messages.find((message) => String(message.id) === selectedId);
  }
  function dirtyDraft() {
    return selectedMessage() && $('draft').value !== savedDraft;
  }
  function setBusy(value) {
    busy = value;
    for (const control of document.querySelectorAll('button, input, select, textarea')) control.disabled = value;
    if (!value && conflicted) {
      $('save-draft').disabled = true;
      for (const control of document.querySelectorAll('[data-status]')) control.disabled = true;
    }
    document.body.setAttribute('aria-busy', String(value));
  }
  function resetConflict() {
    conflicted = false;
    conflictSnapshot = null;
    $('conflict').hidden = true;
    $('conflict-current').hidden = true;
    $('conflict-continue').hidden = true;
    $('conflict-use-saved').hidden = true;
  }
  function showConflict() {
    conflicted = true;
    conflictSnapshot = null;
    $('conflict').hidden = false;
    $('conflict-current').hidden = true;
    $('conflict-continue').hidden = true;
    $('conflict-use-saved').hidden = true;
    feedback('Обращение изменилось в другой вкладке. Сохранение не выполнено; ваш текст остался в поле.', true);
  }
  function dateLabel(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
  }
  function renderList() {
    const list = $('message-list');
    list.replaceChildren();
    const query = $('search').value.trim().toLocaleLowerCase('ru');
    const platform = $('channel-filter').value;
    const status = $('status-filter').value;
    const matches = state.messages.filter((message) => {
      const channel = String(message.platform || '').toLowerCase();
      const searchable = [message.text, message.account_id, nameOfPlatform(channel)].join(' ').toLocaleLowerCase('ru');
      return (!platform || channel === platform) && (!status || message.status === status) && searchable.includes(query);
    });
    $('result-count').textContent = String(matches.length);
    $('hidden-selection').hidden = !selectedId || matches.some((message) => String(message.id) === selectedId);
    if (!matches.length) {
      list.append(node('p', state.messages.length ? 'Нет подходящих обращений. Измените фильтры.' : 'Пока нет сообщений. Загрузите учебные примеры.', 'empty-list'));
      return;
    }
    for (const message of matches) {
      const id = String(message.id);
      const item = node('button', null, 'message-item' + (id === selectedId ? ' active' : ''));
      item.type = 'button';
      item.setAttribute('aria-pressed', String(id === selectedId));
      const top = node('div', null, 'message-top');
      top.append(node('span', nameOfPlatform(message.platform), 'message-platform'));
      const statusClass = Object.hasOwn(statuses, message.status) ? message.status : '';
      top.append(node('span', nameOfStatus(message.status), `badge ${statusClass}`));
      item.append(top, node('p', `Аккаунт: ${message.account_id || 'не указан'}`, 'message-account'));
      item.append(node('p', message.text || 'Сообщение без текста', 'message-preview'));
      const date = dateLabel(message.created_at);
      if (date) item.append(node('div', date, 'message-date'));
      item.addEventListener('click', () => selectMessage(id));
      list.append(item);
    }
  }
  function renderDetail() {
    const message = selectedMessage();
    $('detail-empty').hidden = Boolean(message);
    $('detail-content').hidden = !message;
    if (!message) return;
    $('detail-channel').textContent = nameOfPlatform(message.platform);
    $('detail-account').textContent = `Аккаунт: ${message.account_id || 'не указан'}`;
    $('detail-text').textContent = message.text || 'Сообщение без текста';
    $('detail-status').textContent = nameOfStatus(message.status);
    $('detail-status').className = 'badge ' + (Object.hasOwn(statuses, message.status) ? message.status : '');
    savedDraft = typeof message.draft === 'string' ? message.draft : '';
    $('draft').value = savedDraft;
    $('copy-hint').textContent = 'Если копирование недоступно, выделите текст и скопируйте вручную.';
    const select = $('template-select');
    select.replaceChildren(node('option', 'Выберите шаблон'));
    select.firstChild.value = '';
    for (const template of state.templates) {
      const option = node('option', template.label || 'Шаблон');
      option.value = String(template.id);
      select.append(option);
    }
  }
  function selectMessage(id) {
    if (busy || id === selectedId) return;
    if (dirtyDraft() && !window.confirm('В черновике есть несохранённые изменения. Перейти к другому обращению и потерять их?')) return;
    resetConflict();
    selectedId = id;
    feedback('');
    renderList();
    renderDetail();
    setBusy(false);
  }
  function acceptState(data) {
    if (!data || !Array.isArray(data.messages) || !Array.isArray(data.templates)) throw new Error('Invalid state');
    state = data;
    if (selectedId && !selectedMessage()) selectedId = null;
    renderList();
    renderDetail();
  }
  async function request(path, body) {
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    const options = { headers: { Accept: 'application/json' }, cache: 'no-store' };
    if (body !== undefined) {
      if (!token || token === '__CSRF_TOKEN__') throw new Error('Session missing');
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = token;
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      const error = new Error(`Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }
  async function perform(path, body, success) {
    if (busy) return;
    setBusy(true);
    feedback('Сохраняем изменения…');
    try {
      const data = await request(path, body);
      acceptState(data);
      resetConflict();
      feedback(success);
    } catch (error) {
      if (error.status === 409) showConflict();
      else feedback('Не удалось выполнить действие. Проверьте соединение с локальным сервером и попробуйте снова. Несохранённый текст остался в поле.', true);
    } finally {
      setBusy(false);
    }
  }
  for (const id of ['search', 'channel-filter', 'status-filter']) $(id).addEventListener('input', renderList);
  $('clear-filters').addEventListener('click', () => {
    $('search').value = '';
    $('channel-filter').value = '';
    $('status-filter').value = '';
    renderList();
  });
  $('load-demo').addEventListener('click', () => {
    if (!window.confirm('Загрузка заменит все текущие учебные сообщения и черновики. Продолжить?')) return;
    perform('/api/demo/load', {}, 'Учебные сообщения загружены.');
  });
  $('insert-template').addEventListener('click', () => {
    const choice = state.templates.find((template) => String(template.id) === $('template-select').value);
    if (!choice) { feedback('Сначала выберите шаблон.', true); return; }
    if ($('draft').value && !window.confirm('Заменить текущий текст выбранным шаблоном?')) return;
    $('draft').value = typeof choice.text === 'string' ? choice.text : '';
    $('draft').focus();
    feedback('Шаблон подставлен. Проверьте его и сохраните черновик.');
  });
  $('save-draft').addEventListener('click', () => {
    const message = selectedMessage();
    if (message && !conflicted) perform(`/api/messages/${encodeURIComponent(String(message.id))}/draft`, { text: $('draft').value, version: message.version }, 'Черновик сохранён в памяти сервера.');
  });
  $('conflict-refresh').addEventListener('click', async () => {
    if (busy || !conflicted) return;
    setBusy(true);
    try {
      const fresh = await request('/api/state');
      if (!fresh || !Array.isArray(fresh.messages) || !Array.isArray(fresh.templates)) throw new Error('Invalid state');
      const oldMessage = selectedMessage();
      const current = fresh.messages.find((message) => String(message.id) === selectedId);
      $('conflict-current').hidden = false;
      $('conflict-continue').hidden = true;
      $('conflict-use-saved').hidden = true;
      conflictSnapshot = null;
      if (!current || !oldMessage || !Number.isInteger(current.version) || current.version < 1 ||
          ['platform', 'account_id', 'chat_id', 'text'].some((key) => current[key] !== oldMessage[key])) {
        $('conflict-message').textContent = 'Обращение больше недоступно или изменилось. Скопируйте свой текст вручную и проверьте список.';
        $('conflict-status').textContent = '';
        $('conflict-draft').textContent = '—';
        return;
      }
      $('conflict-message').textContent = `Сообщение: ${current.text || 'без текста'}`;
      $('conflict-status').textContent = `Актуальный статус: ${nameOfStatus(current.status)}`;
      $('conflict-draft').textContent = current.draft || 'Пустой черновик';
      conflictSnapshot = fresh;
      $('conflict-continue').hidden = false;
      $('conflict-use-saved').hidden = false;
      feedback('Актуальные данные показаны отдельно. Ваш текст в поле не изменён.');
    } catch (_error) {
      feedback('Не удалось получить актуальные данные. Ваш текст остался в поле.', true);
    } finally {
      setBusy(false);
    }
  });
  $('conflict-continue').addEventListener('click', () => {
    if (busy || !conflicted || !conflictSnapshot) return;
    const localDraft = $('draft').value;
    const fresh = conflictSnapshot;
    acceptState(fresh);
    $('draft').value = localDraft;
    resetConflict();
    setBusy(false);
    feedback('Актуальная версия открыта, ваш текст сохранён в поле. Чтобы применить его, отдельно нажмите «Сохранить черновик».');
  });
  $('conflict-use-saved').addEventListener('click', () => {
    if (busy || !conflicted || !conflictSnapshot) return;
    const current = conflictSnapshot.messages.find((message) => String(message.id) === selectedId);
    if (!current) return;
    const currentDraft = typeof current.draft === 'string' ? current.draft : '';
    if ($('draft').value !== currentDraft && !window.confirm('Заменить текст в поле сохранённым черновиком? Несохранённый текст из поля будет потерян.')) return;
    acceptState(conflictSnapshot);
    resetConflict();
    setBusy(false);
    feedback('Открыт сохранённый черновик. Данные на сервере не изменялись.');
  });
  $('copy-draft').addEventListener('click', async () => {
    const field = $('draft');
    if (!field.value.trim()) { $('copy-hint').textContent = 'Сначала заполните черновик.'; field.focus(); return; }
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(field.value);
      $('copy-hint').textContent = 'Текст скопирован. Отправка из приложения недоступна.';
    } catch (_error) {
      field.focus(); field.select();
      $('copy-hint').textContent = 'Текст выделен. Скопируйте его вручную.';
    }
  });
  for (const button of document.querySelectorAll('[data-status]')) button.addEventListener('click', () => {
    const message = selectedMessage();
    if (!message || conflicted) return;
    if (dirtyDraft()) { feedback('Сначала сохраните черновик, чтобы не потерять изменения.', true); $('draft').focus(); return; }
    const nextStatus = button.dataset.status;
    if (!Object.hasOwn(statuses, nextStatus)) return;
    perform(`/api/messages/${encodeURIComponent(String(message.id))}/status`, { status: nextStatus, version: message.version }, `Статус изменён: ${nameOfStatus(nextStatus)}. Сообщение не отправлено.`);
  });
  window.addEventListener('beforeunload', (event) => {
    if (dirtyDraft()) { event.preventDefault(); event.returnValue = ''; }
  });
  setBusy(true);
  request('/api/state').then((data) => {
    acceptState(data);
    feedback('Учебная панель готова.');
  }).catch(() => {
    feedback('Не удалось загрузить данные. Проверьте, что локальный сервер запущен, и обновите страницу.', true);
  }).finally(() => setBusy(false));
})();

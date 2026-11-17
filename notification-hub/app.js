'use strict';
const list = document.querySelector('#alerts');
const empty = document.querySelector('#empty');
const statusLine = document.querySelector('#status');
const format = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'medium', timeStyle: 'short'});
const delivery = {pending: 'Ожидает отправки', inflight: 'Отправляется', delivered: 'Принято почтовым сервером'};

async function refresh() {
  try {
    const response = await fetch('/api/alerts', {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось получить данные');
    const {alerts} = await response.json();
    list.replaceChildren();
    empty.hidden = alerts.length !== 0;
    for (const alert of alerts) {
      const row = document.createElement('li');
      const summary = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = alert.kind === 'call' ? 'Звонок' : 'Сообщение';
      const source = document.createElement('small');
      source.textContent = `${alert.platform} · ${alert.account_id}`;
      summary.append(title, source);
      const time = document.createElement('time');
      time.dateTime = alert.occurred_at;
      time.textContent = format.format(new Date(alert.occurred_at));
      const state = document.createElement('span');
      state.className = 'badge';
      if (alert.mail_state === 'uncertain' || (alert.mail_state === 'started' && alert.status !== 'inflight')) {
        state.textContent = 'Нужна проверка почты';
      } else if (alert.mail_state === 'started') {
        state.textContent = 'Отправляется или ожидает проверки';
      } else {
        state.textContent = delivery[alert.status] || 'Неизвестно';
      }
      row.append(summary, time, state);
      list.append(row);
    }
    statusLine.textContent = `Обновлено ${format.format(new Date())}`;
    statusLine.classList.remove('error');
  } catch {
    statusLine.textContent = 'Нет связи с локальным приёмником. Проверьте, что программа запущена.';
    statusLine.classList.add('error');
  }
}
document.querySelector('#refresh').addEventListener('click', refresh);
refresh();
setInterval(refresh, 15000);

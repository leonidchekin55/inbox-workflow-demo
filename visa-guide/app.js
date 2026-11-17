'use strict';

const STORAGE_KEY = 'visa-guide-v1';
const defaultTemplates = [
  ['Услуги по стране', 'Здравствуйте! Уточните, пожалуйста, в какую страну и с какой целью планируете поездку. Проверю, чем сможем помочь.'],
  ['Выдают ли визы', 'Здравствуйте! Уточните, пожалуйста, страну и цель поездки. Я проверю актуальные условия подачи и выдачи виз для вашей ситуации.'],
  ['Список документов', 'Здравствуйте! Для подготовки списка документов уточните, пожалуйста, страну, цель поездки и планируемые даты. После этого я пришлю подходящий список.'],
  ['Куда отправить документы', 'Здравствуйте! Документы можно направить [указать согласованный способ отправки]. Пожалуйста, дождитесь подтверждения перечня документов для вашей ситуации.'],
  ['Консультация', 'Здравствуйте! Для записи на консультацию подскажите, пожалуйста, удобное время и страну, по которой нужен совет. Я проверю доступные варианты.'],
  ['Сроки рассмотрения', 'Здравствуйте! Сроки зависят от страны, типа визы и обстоятельств заявки. Уточните страну и предполагаемую дату поездки, я проверю актуальную информацию.'],
  ['Биометрия', 'Здравствуйте! Уточните, пожалуйста, страну и тип визы. Я проверю, требуется ли биометрия и где её можно сдать.']
];
const id = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const initialData = () => ({version: 1, countries: [], templates: defaultTemplates.map(([title, text]) => ({id: id(), title, text}))});
const fields = {
  countries: ['country', 'visa', 'documents', 'timing', 'biometrics', 'location', 'source', 'checked'],
  templates: ['title', 'text']
};
const $ = (selector) => document.querySelector(selector);
const form = $('#form');
let data;
let snapshot;
let section = 'countries';
let selectedId = null;
let formBaseline = '';
let savingEnabled = true;

function validData(value) {
  if (!value || value.version !== 1 || !Array.isArray(value.countries) || !Array.isArray(value.templates)) return false;
  return Object.keys(fields).every((key) => {
    const seen = new Set();
    return value[key].every((item) => {
      if (!item || typeof item.id !== 'string' || !item.id || seen.has(item.id)) return false;
      seen.add(item.id);
      return fields[key].every((name) => typeof item[name] === 'string' && item[name].length <= 50000);
    });
  });
}

function feedback(message, error = false) {
  const target = $('#feedback');
  target.textContent = message;
  target.classList.toggle('error', error);
}

function readStorage() {
  try {
    snapshot = localStorage.getItem(STORAGE_KEY);
    data = snapshot === null ? initialData() : JSON.parse(snapshot);
    if (!validData(data)) throw new Error('invalid data');
  } catch {
    data = initialData();
    snapshot = null;
    savingEnabled = false;
    feedback('Не удалось прочитать данные браузера. Проверьте доступ к хранилищу. Текущие записи не перезаписываются.', true);
  }
}

function persist(next) {
  try {
    if (!savingEnabled) throw new Error('storage unavailable');
    if (localStorage.getItem(STORAGE_KEY) !== snapshot) {
      feedback('Данные изменились в другой вкладке. Скопируйте несохранённый текст, затем обновите страницу.', true);
      return false;
    }
    const nextSnapshot = JSON.stringify(next);
    localStorage.setItem(STORAGE_KEY, nextSnapshot);
    snapshot = nextSnapshot;
    data = next;
    return true;
  } catch {
    feedback('Не удалось сохранить. Проверьте доступ и свободное место в хранилище браузера.', true);
    return false;
  }
}

function currentValues() {
  return Object.fromEntries(fields[section].map((name) => [name, form.elements[name].value]));
}
function isDirty() {
  return !form.hidden && JSON.stringify(currentValues()) !== formBaseline;
}
function mayLeave() {
  return !isDirty() || confirm('В записи есть несохранённые изменения. Перейти без сохранения?');
}
function showEditor(item) {
  selectedId = item?.id ?? null;
  form.hidden = !item;
  $('#empty').hidden = Boolean(item);
  $('#country-fields').hidden = section !== 'countries';
  $('#template-fields').hidden = section !== 'templates';
  $('#copy').hidden = section !== 'templates';
  $('#form-title').textContent = section === 'countries' ? 'Страна и виза' : 'Шаблон ответа';
  if (item) {
    for (const name of fields[section]) form.elements[name].value = item[name] || '';
    formBaseline = JSON.stringify(currentValues());
  }
  renderList();
}
function renderList() {
  const container = $('#items');
  container.replaceChildren();
  const query = $('#search').value.trim().toLocaleLowerCase('ru');
  const matches = data[section].filter((item) => fields[section].some((name) => item[name].toLocaleLowerCase('ru').includes(query)));
  if (!matches.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-list';
    empty.textContent = query ? 'Записи не найдены.' : 'Пока нет записей. Нажмите «Добавить».';
    container.append(empty);
    return;
  }
  for (const item of matches) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `item${item.id === selectedId ? ' active' : ''}`;
    const title = document.createElement('strong');
    title.textContent = section === 'countries' ? item.country : item.title;
    button.append(title);
    if (section === 'countries') {
      const description = document.createElement('span');
      description.textContent = [item.visa, item.checked ? `Проверено ${item.checked}` : 'Дата проверки не указана'].filter(Boolean).join(' · ');
      button.append(description);
    }
    button.addEventListener('click', () => { if (mayLeave()) showEditor(item); });
    container.append(button);
  }
}
function switchSection(next) {
  if (section === next || !mayLeave()) return;
  section = next;
  $('#countries-tab').classList.toggle('active', section === 'countries');
  $('#templates-tab').classList.toggle('active', section === 'templates');
  $('#countries-tab').setAttribute('aria-pressed', String(section === 'countries'));
  $('#templates-tab').setAttribute('aria-pressed', String(section === 'templates'));
  $('#list-title').textContent = section === 'countries' ? 'Страны и визы' : 'Шаблоны ответов';
  $('#search').value = '';
  showEditor(null);
}
$('#countries-tab').addEventListener('click', () => switchSection('countries'));
$('#templates-tab').addEventListener('click', () => switchSection('templates'));
$('#search').addEventListener('input', renderList);
$('#add').addEventListener('click', () => {
  if (!mayLeave()) return;
  showEditor({id: id(), ...Object.fromEntries(fields[section].map((name) => [name, '']))});
  form.elements[section === 'countries' ? 'country' : 'title'].focus();
});
form.addEventListener('submit', (event) => {
  event.preventDefault();
  const values = currentValues();
  const required = section === 'countries' ? 'country' : 'title';
  if (!values[required].trim()) {
    feedback(section === 'countries' ? 'Укажите страну.' : 'Укажите название шаблона.', true);
    form.elements[required].focus();
    return;
  }
  const next = structuredClone(data);
  const index = next[section].findIndex((item) => item.id === selectedId);
  const item = {id: selectedId, ...values};
  if (index < 0) next[section].push(item); else next[section][index] = item;
  if (persist(next)) {
    formBaseline = JSON.stringify(values);
    renderList();
    feedback('Сохранено в этом браузере.');
  }
});
$('#remove').addEventListener('click', () => {
  const exists = data[section].some((item) => item.id === selectedId);
  if (!confirm(exists ? 'Удалить эту запись?' : 'Закрыть несохранённую запись?')) return;
  if (!exists) { showEditor(null); return; }
  const next = structuredClone(data);
  next[section] = next[section].filter((item) => item.id !== selectedId);
  if (persist(next)) { showEditor(null); feedback('Запись удалена.'); }
});
$('#copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(form.elements.text.value);
    feedback('Текст скопирован. Проверьте его перед использованием.');
  } catch {
    form.elements.text.select();
    feedback('Выделите текст и скопируйте его вручную.', true);
  }
});
$('#export').addEventListener('click', () => {
  const file = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(file);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'visa-guide-backup.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  feedback(isDirty() ? 'Копия сохранена без несохранённых изменений в форме.' : 'Копия сохранена.');
});
$('#import').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  if (!mayLeave()) return;
  try {
    if (file.size > 5 * 1024 * 1024) throw new Error('large file');
    const incoming = JSON.parse(await file.text());
    if (!validData(incoming)) throw new Error('invalid file');
    if (!confirm('Заменить все текущие записи данными из выбранной копии?')) return;
    if (persist(incoming)) { showEditor(null); feedback('Копия загружена.'); }
  } catch {
    feedback('Не удалось прочитать копию. Выберите JSON-файл, сохранённый из этого справочника.', true);
  }
});
window.addEventListener('beforeunload', (event) => {
  if (isDirty()) { event.preventDefault(); event.returnValue = ''; }
});
readStorage();
renderList();

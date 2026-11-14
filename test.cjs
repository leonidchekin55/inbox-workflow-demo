const fs=require('fs'),vm=require('vm'),assert=require('assert');
const js=fs.readFileSync('app.js','utf8');
class E{constructor(id=''){this.id=id;this.children=[];this.listeners={};this.value='';this.textContent='';this.hidden=false}addEventListener(n,f){this.listeners[n]=f}append(...x){this.children.push(...x)}replaceChildren(){this.children=[]}focus(){}select(){this.selected=true}reset(){ids.channel.value='WhatsApp';ids.account.value='Не указан';ids.status.value='Новое';ids.description.value='';ids.reminder.value=''}click(){this.listeners.click?.({preventDefault(){}})}}
const ids={};const names=['open-count','overdue-count','done-count','feedback','list','search','filter-channel','filter-status','count','form','form-title','save','cancel','channel','account','status','description','reminder','examples','draft-kind','generate','copy','draft','copy-status'];for(const id of names)ids[id]=new E(id);ids.form.reset();ids['draft-kind'].value='received';let saved={};const ctx={document:{getElementById:id=>ids[id],createElement:t=>new E(t)},localStorage:{getItem:k=>saved[k]||null,setItem:(k,v)=>saved[k]=v},confirm:()=>true,window:{scrollTo(){},isSecureContext:false},navigator:{},setInterval:()=>{},Date,Math,JSON};vm.runInNewContext(js,ctx);ids.description.value='Тестовая заявка';ids.channel.value='MAX';ids.account.value='3';ids.form.listeners.submit({preventDefault(){}});assert.equal(ids.count.textContent,'Показано 1 из 1');assert.equal(ids['open-count'].textContent,'1');assert.equal(JSON.parse(Object.values(saved)[0])[0].account,'3');ids.search.value='не найдено';ids.search.listeners.input();assert.equal(ids.count.textContent,'Показано 0 из 1');ids.search.value='';ids.search.listeners.input();ids['filter-channel'].value='MAX';ids['filter-channel'].listeners.input();assert.equal(ids.count.textContent,'Показано 1 из 1');let card=ids.list.children[0];card.children.at(-1).children[0].click();ids.description.value='Исправленная заявка';ids.form.listeners.submit({preventDefault(){}});assert.equal(JSON.parse(Object.values(saved)[0])[0].description,'Исправленная заявка');card=ids.list.children[0];card.children.at(-1).children[1].click();assert.equal(ids.count.textContent,'Показано 0 из 0');ids['filter-channel'].value='';ids.examples.click();assert.equal(ids.count.textContent,'Показано 2 из 2');ids.generate.click();assert(ids.draft.value.includes('Здравствуйте'));ids.copy.click();assert(ids.draft.selected);assert(ids['copy-status'].textContent.includes('вручную'));console.log('PASS: добавление, сохранение, поиск, фильтр канала, редактирование, удаление, учебные примеры, создание черновика и ручное копирование');

// Не перезаписывать сохранённые данные, если их не удалось прочитать.
for (const initial of ['{bad json', '{"not":"an array"}']) {
  const isolated = Object.fromEntries(names.map(id => [id, new E(id)]));
  isolated.channel.value = 'Telegram';
  isolated.account.value = '1';
  isolated.status.value = 'Новое';
  isolated.description.value = 'Проверочная задача';
  let raw = initial;
  let writes = 0;
  const broken = {
    ...ctx,
    document: {getElementById: id => isolated[id], createElement: t => new E(t)},
    localStorage: {getItem: () => raw, setItem: (_key, value) => {raw = value; writes++;}},
    setInterval: () => {},
  };
  vm.runInNewContext(js, broken);
  isolated.form.listeners.submit({preventDefault() {}});
  assert.equal(writes, 0, 'Ошибка чтения не должна приводить к перезаписи данных');
  assert.equal(raw, initial);
  assert.match(isolated.feedback.textContent, /Сохранение отключено/);
}

// Сроки пересчитываются и сортируются при переходе через время напоминания.
{
  const isolated = Object.fromEntries(names.map(id => [id, new E(id)]));
  const reference = new Date('2035-01-01T12:00').getTime();
  let now = reference;
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const entries = [
    {id:'future', channel:'Telegram', account:'1', status:'Новое', description:'Наступит позже', reminder:'2035-01-01T12:01'},
    {id:'done', channel:'MAX', account:'2', status:'Завершено', description:'Завершённая', reminder:'2035-01-01T11:30'},
    {id:'overdue', channel:'WhatsApp', account:'3', status:'В работе', description:'Просроченная', reminder:'2035-01-01T11:59'},
  ];
  let tick;
  const timed = {
    ...ctx,
    document: {getElementById: id => isolated[id], createElement: t => new E(t)},
    localStorage: {getItem: () => JSON.stringify(entries), setItem: () => {}},
    Date: FakeDate,
    setInterval: (fn, ms) => {assert.equal(ms, 60000); tick = fn;},
  };
  vm.runInNewContext(js, timed);
  assert.equal(isolated['open-count'].textContent, '2');
  assert.equal(isolated['overdue-count'].textContent, '1');
  assert.equal(isolated['done-count'].textContent, '1');
  assert.equal(isolated.list.children[0].children[1].textContent, 'Просроченная');
  now = reference + 120000;
  assert.equal(typeof tick, 'function');
  tick();
  assert.equal(isolated['overdue-count'].textContent, '2');
  assert.equal(isolated.list.children[0].children[1].textContent, 'Наступит позже');
  assert.equal(isolated.list.children[2].children[1].textContent, 'Завершённая');
}
console.log('PASS: сохранность повреждённого хранилища и автоматическое обновление просрочек');

/*
  Мини-апп Orbit. Один файл без сборки и без библиотек — намеренно.

  Собирать нечего: это пять экранов, читающих готовые цифры. Любая сборка
  означала бы, что поправить строчку нельзя без node и установки пакетов, а
  систему чинят чаще, чем переписывают. Графики рисуются вручную в SVG по той
  же причине: подключать библиотеку графиков ради двух линий значит поставить
  работу приложения в зависимость от чужого адреса в интернете.
*/
'use strict';

const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

// Подпись Telegram уходит с КАЖДЫМ запросом, а не обменивается на сессию.
// Так на сервере нет состояния, которое можно украсть или забыть протухнуть.
const INIT_DATA = (tg && tg.initData) || '';
const BUILD = window.ORBIT_BUILD || 'dev';

// replaceChildren есть не во всех встроенных браузерах Android. Без замены
// на старом телефоне первый же экран падал бы молча — кнопки на месте,
// ничего не происходит.
if (!Element.prototype.replaceChildren) {
  Element.prototype.replaceChildren = function (...nodes) {
    while (this.firstChild) this.removeChild(this.firstChild);
    nodes.forEach((node) => this.appendChild(node));
  };
}

// Ошибка на телефоне уходит в лог на маке. Отсюда телефон не видно никак,
// и без этого «ничего не нажимается» остаётся загадкой.
function report(what) {
  try {
    const text = ('[' + BUILD + '] ' + what).slice(0, 500);
    if (navigator.sendBeacon) navigator.sendBeacon('/api/clientlog', text);
    else fetch('/api/clientlog', { method: 'POST', body: text, keepalive: true });
  } catch (_) { /* сообщить об ошибке — не повод падать самим */ }
}
window.addEventListener('error', (event) => {
  report('error: ' + event.message + ' @' + (event.lineno || '?'));
  banner('Что-то сломалось на странице. Обновите пульт — я уже знаю об ошибке.');
});
window.addEventListener('unhandledrejection', (event) => {
  report('promise: ' + (event.reason && event.reason.message || event.reason));
});

// Нажатие чувствуется пальцем. В Telegram у кнопок нет «нажатого» вида, и
// без отклика кажется, что ничего не произошло, — человек жмёт второй раз.
function tap() {
  try { tg && tg.HapticFeedback && tg.HapticFeedback.selectionChanged(); } catch (_) {}
}

class Forbidden extends Error {}

// Запрос с таймаутом и повтором. Туннель к маку рвётся, и один потерянный
// запрос не должен превращаться в «Не получилось» на весь экран: повторяем
// дважды с паузой. Отказ в доступе не повторяем — он от повтора не пройдёт.
async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers['X-Init-Data'] = INIT_DATA;
  if (opts.body) opts.headers['Content-Type'] = 'application/json';
  let last = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const guard = new AbortController();
    const timer = setTimeout(() => guard.abort(), 15000);
    try {
      const res = await fetch(path, Object.assign({}, opts, { signal: guard.signal }));
      const data = await res.json().catch(() => ({}));
      if (res.status === 403) throw new Forbidden(data.error || 'Нет доступа');
      if (res.ok) return data;
      last = new Error(data.error || ('Сервер ответил ' + res.status));
      if (res.status < 500) throw last;
    } catch (error) {
      if (error instanceof Forbidden) { denied(error.message); throw error; }
      last = error.name === 'AbortError' ? new Error('Пульт не ответил за 15 секунд') : error;
    } finally {
      clearTimeout(timer);
    }
    await new Promise((done) => setTimeout(done, 800 * (attempt + 1)));
  }
  report('api ' + path + ': ' + (last && last.message));
  throw last || new Error('Не получилось');
}

async function post(path, body) {
  return api(path, { method: 'POST', body: JSON.stringify(body) });
}

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

function toast(text) {
  const old = document.querySelector('.toast');
  if (old) old.remove();
  const node = el('div', 'toast', text);
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 2600);
}

function banner(text) {
  if (document.querySelector('.banner')) return;
  const node = el('div', 'banner', text);
  document.body.prepend(node);
}

// Отказ в доступе — отдельный экран, а не «Не получилось» в каждом блоке.
// Причина пишется словами: «ссылка устарела» чинится переоткрытием пульта,
// «подпись не пришла» — тем, что его открыли не кнопкой из бота.
function denied(reason) {
  if (document.querySelector('.denied')) return;
  const box = el('div', 'denied');
  box.appendChild(el('h1', '', 'Нет доступа'));
  box.appendChild(el('p', '', reason));
  box.appendChild(el('p', 'hint',
    INIT_DATA ? 'Закройте пульт и откройте заново кнопкой «Пульт» в боте.'
              : 'Пульт открыт не из Telegram. Откройте его кнопкой «Пульт» в боте.'));
  document.body.appendChild(box);
}

// Ошибка в блоке — с кнопкой «Повторить». Без неё единственный способ
// попробовать снова — закрыть пульт и открыть заново.
function fail(box, error) {
  if (error instanceof Forbidden) return;
  const wrap = el('div', 'empty');
  wrap.appendChild(el('p', '', error.message || 'Не получилось прочитать'));
  const again = el('button', 'btn', 'Повторить');
  again.addEventListener('click', () => { tap(); if (loaders[shown]) loaders[shown](); });
  wrap.appendChild(again);
  box.replaceChildren(wrap);
}

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

function when(raw) {
  if (!raw) return '';
  const at = new Date(raw);
  if (isNaN(at)) return '';
  const today = new Date();
  const sameDay = at.toDateString() === today.toDateString();
  return sameDay
    ? at.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
    : at.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
}

// Полная дата со временем. Для ответа клиента коротких «22.09» мало: по ним
// не понять, ответили час назад или неделю, а от этого зависит, бежать ли
// отвечать прямо сейчас.
function fullWhen(raw) {
  if (!raw) return '';
  const at = new Date(raw);
  if (isNaN(at)) return '';
  const stamp = at.toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', year: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
  const days = Math.floor((Date.now() - at.getTime()) / 86400000);
  if (days <= 0) return stamp;
  if (days === 1) return stamp + ' · вчера';
  return stamp + ' · ' + days + ' ' + plural(days, 'день', 'дня', 'дней') + ' назад';
}

// --- Логотип в шапке: тот же файл, что и иконка, чтобы не расходились --------
// Знак приходит вместе со страницей: отдельный запрос за ним на рваном
// туннеле оставлял шапку пустой.
if (window.ORBIT_LOGO) $('mark').innerHTML = window.ORBIT_LOGO;
else fetch('/logo.svg').then((r) => r.text()).then((svg) => { $('mark').innerHTML = svg; })
  .catch(() => {});

// --- Переключение экранов ----------------------------------------------------

const loaders = {};
let shown = 'home';

function show(name) {
  shown = name;
  document.querySelectorAll('.view').forEach((view) => {
    view.hidden = view.id !== 'view-' + name;
  });
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.setAttribute('aria-selected', String(tab.dataset.view === name));
  });
  window.scrollTo(0, 0);
  if (loaders[name]) loaders[name]();
}

$('tabs').addEventListener('click', (event) => {
  const tab = event.target.closest('.tab');
  if (tab) { tap(); show(tab.dataset.view); }
});

// Группа «чипов» ведёт себя как радиокнопки. Разметка одна на четыре фильтра.
function chips(boxId, onPick) {
  $(boxId).addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    tap();
    $(boxId).querySelectorAll('.chip').forEach((other) => {
      other.setAttribute('aria-pressed', String(other === chip));
    });
    onPick(chip.dataset);
  });
}

// --- Главное -----------------------------------------------------------------

loaders.home = async () => {
  const needs = $('needs');
  try {
    const data = await api('/api/overview');
    $('who').textContent = (data.dev ? 'режим разработки' : data.who) + ' · ' + BUILD;
    $('today').textContent = data.day;

    if (!data.attention.length) {
      needs.replaceChildren(
        Object.assign(el('p', 'calm'), {
          innerHTML: '<b>Всё идёт само</b>Ничего не ждёт вашего участия.',
        })
      );
    } else {
      const list = el('ul', 'needs');
      data.attention.forEach((line) => {
        const item = el('li', line.startsWith('🛑') ? 'hot' : '');
        item.appendChild(el('span', 'dot'));
        item.appendChild(el('span', '', line.replace(/^[^\wА-Яа-я]+/, '')));
        list.appendChild(item);
      });
      needs.replaceChildren(list);
    }

    const t = data.today;
    $('tiles').replaceChildren(
      tile(t.letters, 'писем ушло'),
      tile(t.mailed + ' / ' + t.warmup, 'прогрев ящика'),
      tile(t.bids, 'откликов'),
      tile(t.spent + ' ₽', 'потрачено'),
      tile(t.seen, 'заказов просмотрено'),
      tile(data.queues.partner + data.queues.client, 'писем ждут'),
    );
  } catch (error) { fail(needs, error); }

  try {
    const digest = await api('/api/digest');
    $('digest-day').textContent = digest.day;
    $('digest').textContent = digest.text || 'За сегодня записей пока нет.';
  } catch (error) { $('digest').textContent = 'Сводка не прочиталась.'; }
};

function tile(value, title) {
  const box = el('div', 'tile');
  box.appendChild(el('div', 'n', String(value)));
  box.appendChild(el('div', 't', title));
  return box;
}

// --- Воронка ------------------------------------------------------------------

let days = 7;
chips('period', (data) => { days = Number(data.days); loaders.funnel(); });

loaders.funnel = async () => {
  try {
    const data = await api('/api/funnel?days=' + days);
    drawChart(data.series);

    const ex = data.exchange;
    $('money').replaceChildren(
      tile(ex ? ex.seen : '—', 'заказов просмотрено'),
      tile(ex ? ex.sent : '—', 'откликов'),
      tile(ex ? ex.spent + ' ₽' : '—', 'потрачено'),
      tile(ex && ex.per_reply != null ? ex.per_reply + ' ₽' : '—', 'цена ответа'),
    );

    const box = $('funnels');
    box.replaceChildren();
    data.sides.forEach((side) => {
      const panel = el('div', 'panel');
      const head = el('header');
      head.appendChild(el('h2', '', side.title));
      const total = side.stages.reduce((sum, s) => sum + s.count, 0);
      head.appendChild(el('span', 'eyebrow', total + ' ' + plural(total, 'запись', 'записи', 'записей')));
      panel.appendChild(head);

      const funnel = el('div', 'funnel');
      const top = Math.max(1, ...side.stages.map((s) => s.count));
      side.stages.forEach((stage) => {
        const step = el('div', 'step');
        step.appendChild(el('span', '', stage.title));
        const bar = el('div', 'bar');
        const fill = el('span');
        fill.style.width = Math.round((stage.count / top) * 100) + '%';
        bar.appendChild(fill);
        step.appendChild(bar);
        step.appendChild(el('span', 'num', String(stage.count)));
        funnel.appendChild(step);
      });
      panel.appendChild(funnel);
      box.appendChild(panel);
    });
  } catch (error) { fail($('funnels'), error); }
};

function drawChart(series) {
  const box = $('chart');
  const W = 320, H = 120, PAD = 22;
  const top = Math.max(1, ...series.map((d) => Math.max(d.sent, d.reply)));
  const stepX = series.length > 1 ? (W - PAD * 2) / (series.length - 1) : 0;
  const x = (i) => PAD + i * stepX;
  const y = (v) => H - PAD - (v / top) * (H - PAD * 2);

  const path = (key) => series.map((d, i) => (i ? 'L' : 'M') + x(i) + ' ' + y(d[key])).join(' ');
  const area = path('sent') + ' L' + x(series.length - 1) + ' ' + y(0) + ' L' + x(0) + ' ' + y(0) + ' Z';

  // Подписи ставим только по краям и в середине: на тридцати днях все даты
  // сливаются в серую полосу и перестают читаться вовсе.
  const marks = [0, Math.floor((series.length - 1) / 2), series.length - 1]
    .filter((i, at, all) => i >= 0 && all.indexOf(i) === at);

  const parts = [
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="События по дням">`,
    `<line class="grid" x1="${PAD}" y1="${y(top)}" x2="${W - PAD}" y2="${y(top)}"/>`,
    `<text class="lbl" x="2" y="${y(top) + 3}">${top}</text>`,
    `<line class="axis" x1="${PAD}" y1="${y(0)}" x2="${W - PAD}" y2="${y(0)}"/>`,
    `<path class="area" d="${area}"/>`,
    `<path class="line" d="${path('sent')}"/>`,
    `<path class="line" style="opacity:.35" d="${path('reply')}"/>`,
  ];
  series.forEach((d, i) => {
    if (d.sent) parts.push(`<circle class="dot" cx="${x(i)}" cy="${y(d.sent)}" r="2.5"/>`);
  });
  marks.forEach((i) => {
    const day = series[i].day.slice(8) + '.' + series[i].day.slice(5, 7);
    const anchor = i === 0 ? 'start' : i === series.length - 1 ? 'end' : 'middle';
    parts.push(`<text class="lbl" x="${x(i)}" y="${H - 4}" text-anchor="${anchor}">${day}</text>`);
  });
  parts.push('</svg>');
  box.innerHTML = parts.join('');
}

// --- Отклики -------------------------------------------------------------------

let source = '', bidKind = 'sent';
chips('sources', (data) => { source = data.source || ''; loaders.bids(); });
chips('kindsbid', (data) => { bidKind = data.kind; loaders.bids(); });

const BID_PILLS = {
  sent: ['ok', 'отправлен'],
  rejected: ['', 'не взяли'],
  failed: ['bad', 'не ушёл'],
  planned: ['wait', 'в очереди'],
  unavailable: ['', 'закрыт'],
  dry_run: ['wait', 'проверка'],
};

loaders.bids = async () => {
  const box = $('bids');
  box.replaceChildren(el('p', 'loading', 'Читаю…'));
  try {
    const data = await api(
      '/api/bids?days=60&kind=' + bidKind + (source ? '&source=' + source : '')
    );
    if (!data.rows.length) {
      const nothing = bidKind === 'replied'
        ? 'За два месяца никто не ответил'
        : 'За два месяца откликов не было';
      box.replaceChildren(el('p', 'empty', data.why || nothing));
      return;
    }
    box.replaceChildren(...data.rows.map((row) => {
      const item = el('a', 'row');
      item.href = row.url;
      item.target = '_blank';
      item.rel = 'noopener';
      const line = el('div', 'line1');
      line.appendChild(el('span', 'title', row.title));
      line.appendChild(el('span', 'meta', when(row.at)));
      item.appendChild(line);
      const marks = el('div', 'line1');
      const [tone, label] = BID_PILLS[row.status] || ['', row.status];
      marks.appendChild(el('span', 'pill ' + tone, label));
      const money = [row.source === 'fl' ? 'FL.ru' : 'Profi.ru'];
      if (row.cost) money.push(Math.round(row.cost) + ' ₽');
      if (row.budget) money.push(row.budget);
      marks.appendChild(el('span', 'meta', money.join(' · ')));
      item.appendChild(marks);
      if (row.replied_at) {
        const answered = el('div', 'answered');
        answered.appendChild(el('span', 'pill ok', 'клиент ответил'));
        answered.appendChild(el('span', 'meta', fullWhen(row.replied_at)));
        item.appendChild(answered);
      }
      if (row.text || row.reason) item.appendChild(el('div', 'sub', row.text || row.reason));
      return item;
    }));
  } catch (error) { fail(box, error); }
};

// --- База ----------------------------------------------------------------------

let kind = 'partner', stage = 'all', query = '';
chips('kinds', (data) => { kind = data.kind; loaders.base(); });
chips('stages', (data) => { stage = data.stage; loaders.base(); });

let typing = null;
$('q').addEventListener('input', (event) => {
  query = event.target.value.trim();
  clearTimeout(typing);
  // Ждём, пока человек допечатает: запрос на каждую букву — это десять
  // запросов на одно слово и мигающий список под пальцами.
  typing = setTimeout(loaders.base, 300);
});

loaders.base = async () => {
  const box = $('contacts');
  try {
    const data = await api(
      '/api/contacts?kind=' + kind + '&stage=' + stage + '&q=' + encodeURIComponent(query)
    );
    if (!data.rows.length) {
      box.replaceChildren(el('p', 'empty', 'Никого не нашлось'));
      return;
    }
    box.replaceChildren(...data.rows.map((row) => {
      const item = el('button', 'row');
      const line = el('div', 'line1');
      line.appendChild(el('span', 'title', row.name));
      line.appendChild(el('span', 'meta', row.stage_title));
      item.appendChild(line);
      const where = [row.city, row.emails[0] || row.phones[0] || ''].filter(Boolean);
      if (where.length) item.appendChild(el('div', 'sub', where.join(' · ')));
      item.addEventListener('click', () => openCard(row.pk));
      return item;
    }));
  } catch (error) { fail(box, error); }
};

async function openCard(pk) {
  let card;
  try { card = await api('/api/contact/' + pk); }
  catch (error) { toast(error.message); return; }

  const sheet = el('div', 'sheet');
  const head = el('header', 'top');
  const back = el('button', 'btn', '← Назад');
  back.addEventListener('click', () => sheet.remove());
  head.appendChild(back);
  head.appendChild(el('span', 'name', card.stage_title));
  sheet.appendChild(head);

  const panel = el('div', 'panel');
  panel.style.margin = '16px';
  const title = el('header');
  title.appendChild(el('h1', '', card.name));
  panel.appendChild(title);

  const kv = el('dl', 'kv');
  const add = (key, value) => {
    if (!value) return;
    kv.appendChild(el('dt', '', key));
    kv.appendChild(el('dd', '', value));
  };
  add('Город', card.city);
  add('Сайт', card.site);
  add('Почта', card.emails.join(', '));
  add('Телефон', card.phones.join(', '));
  add('Телеграм', card.telegram.join(', '));
  add('Заметка', card.note);
  panel.appendChild(kv);

  if (card.draft) {
    panel.appendChild(el('header', '', 'Письмо в очереди'));
    panel.appendChild(el('div', 'letter', card.draft.subject + '\n\n' + card.draft.body));
    const btns = el('div', 'btns');
    const send = el('button', 'btn main', 'Отправить');
    send.addEventListener('click', async () => {
      send.disabled = true;
      try {
        const res = await post('/api/send', { draft_pk: card.draft.pk });
        toast(res.ok ? 'Письмо ушло' : res.why);
        if (res.ok) { sheet.remove(); loaders.base(); }
      } catch (error) { toast(error.message); }
      send.disabled = false;
    });
    const drop = el('button', 'btn risky', 'Убрать из очереди');
    drop.addEventListener('click', async () => {
      try {
        await post('/api/drop', { draft_pk: card.draft.pk });
        toast('Убрал');
        sheet.remove();
        loaders.base();
      } catch (error) { toast(error.message); }
    });
    btns.appendChild(send);
    btns.appendChild(drop);
    panel.appendChild(btns);
  }

  const stages = el('div', 'btns');
  [['replied', 'Ответили'], ['call', 'На созвон'], ['deal', 'В работе'], ['dead', 'Закрыть']]
    .forEach(([code, label]) => {
      const btn = el('button', 'btn' + (code === 'dead' ? ' risky' : ''), label);
      btn.addEventListener('click', async () => {
        try {
          await post('/api/stage', { pk: card.pk, stage: code });
          toast('Стадия: ' + label);
          sheet.remove();
          loaders.base();
        } catch (error) { toast(error.message); }
      });
      stages.appendChild(btn);
    });
  panel.appendChild(stages);
  sheet.appendChild(panel);

  if (card.history.length) {
    const box = el('div', 'panel');
    box.style.margin = '0 16px';
    box.appendChild(Object.assign(el('header'), { innerHTML: '<h2>История</h2>' }));
    const list = el('ul', 'hist');
    card.history.forEach((row) => {
      const item = el('li');
      item.appendChild(el('span', '', row.text || row.kind));
      item.appendChild(el('span', 'when', when(row.at)));
      list.appendChild(item);
    });
    box.appendChild(list);
    sheet.appendChild(box);
  }

  document.body.appendChild(sheet);
}

// --- Система ---------------------------------------------------------------------

loaders.system = async () => {
  const box = $('queue');
  try {
    const data = await api('/api/queue?kind=partner');
    $('queue-count').textContent = data.autosend ? 'отправка включена' : 'отправка вручную';
    if (!data.rows.length) {
      box.replaceChildren(el('p', 'empty', 'Очередь пуста'));
    } else {
      box.replaceChildren(...data.rows.map((row) => {
        const item = el('div', 'row');
        item.style.cursor = 'default';
        const line = el('div', 'line1');
        line.appendChild(el('span', 'title', row.name));
        line.appendChild(el('span', 'pill ' + (row.can_send ? 'ok' : 'wait'),
          row.can_send ? 'готово' : 'ждёт'));
        item.appendChild(line);
        item.appendChild(el('div', 'sub', row.can_send ? row.subject : row.why));
        if (row.can_send) {
          const btns = el('div', 'btns');
          btns.style.padding = '8px 0 0';
          btns.style.borderTop = 'none';
          const send = el('button', 'btn main', 'Отправить');
          send.addEventListener('click', async () => {
            send.disabled = true;
            try {
              const res = await post('/api/send', { draft_pk: row.draft_pk });
              toast(res.ok ? 'Письмо ушло' : res.why);
              loaders.system();
            } catch (error) { toast(error.message); send.disabled = false; }
          });
          btns.appendChild(send);
          item.appendChild(btns);
        }
        return item;
      }));
    }
  } catch (error) { fail(box, error); }

  try {
    const data = await api('/api/health');
    const box = $('health');
    box.replaceChildren();
    data.groups.forEach((group) => {
      box.appendChild(Object.assign(el('div', 'eyebrow', group.title),
        { style: 'padding:12px 14px 4px' }));
      const rows = el('div', 'rows');
      group.checks.forEach((check) => {
        const item = el('div', 'row');
        item.style.cursor = 'default';
        const line = el('div', 'line1');
        line.appendChild(el('span', 'title', check.title));
        const tone = check.ok === null ? '' : check.ok ? 'ok' : 'bad';
        const label = check.ok === null ? 'не проверил' : check.ok ? 'работает' : 'встало';
        line.appendChild(el('span', 'pill ' + tone, label));
        item.appendChild(line);
        if (check.note) item.appendChild(el('div', 'sub', check.note));
        rows.appendChild(item);
      });
      box.appendChild(rows);
    });
  } catch (error) { fail($('health'), error); }

  try {
    const data = await api('/api/alerts');
    $('switches').replaceChildren(...data.rows.map((row) => {
      const item = el('div', 'switch');
      const left = el('div');
      left.appendChild(el('div', 't', row.title));
      if (row.why) left.appendChild(el('div', 'w', row.why));
      item.appendChild(left);
      const toggle = el('button', 'toggle');
      toggle.setAttribute('role', 'switch');
      toggle.setAttribute('aria-checked', String(row.instant));
      toggle.setAttribute('aria-label', row.title);
      toggle.addEventListener('click', async () => {
        const next = toggle.getAttribute('aria-checked') !== 'true';
        toggle.setAttribute('aria-checked', String(next));
        try {
          await post('/api/alerts', { code: row.code, instant: next });
        } catch (error) {
          toggle.setAttribute('aria-checked', String(!next));
          toast(error.message);
        }
      });
      item.appendChild(toggle);
      return item;
    }));
  } catch (error) { fail($('switches'), error); }
};

show('home');

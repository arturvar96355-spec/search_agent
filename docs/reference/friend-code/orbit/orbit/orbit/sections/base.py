"""Общая механика рассылочных вкладок «Партнёры» и «Клиенты».

Обе вкладки устроены одинаково: список контактов по стадиям, карточка, очередь
готовых писем, отправка и лимиты. Отличаются они только адресатом и текстом,
который пишет модель. Поэтому вся механика живёт здесь, а partners.py и
clients.py — это несколько строк настроек поверх.

Генерация текста платная и небыстрая (несколько секунд на письмо), поэтому
пачка готовится в отдельном потоке: бот не должен замирать на минуту, пока
модель пишет двадцать писем. Поток открывает свою связь с базой — sqlite не
разрешает делить одно соединение между потоками.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from loguru import logger

from .. import llm, mailcheck, outreach
from ..config import get_settings
from ..models import (
    STAGE_ICONS,
    STAGE_TITLES,
    Channel,
    Contact,
    Draft,
    Kind,
    Stage,
    looks_foreign,
)
from ..registry import Health
from ..store import Store
from ..ui import PAGE_SIZE, Screen, clip, counted, esc, pager

# Стадии, которые показываем в списке вкладки. «Готовы к отправке» живёт
# на отдельном экране очереди, поэтому здесь её нет.
LIST_STAGES = (Stage.NEW, Stage.WRITTEN, Stage.REPLIED, Stage.CALL, Stage.DEAL, Stage.DEAD)


@dataclass(frozen=True)
class Tab:
    """Настройки одной рассылочной вкладки."""

    code: str            # «p» или «c» — префикс кнопок
    kind: Kind
    title: str
    icon: str
    unit_one: str        # «агентство», «компания»
    unit_few: str
    unit_many: str
    hint: str            # одна строка: кого мы тут собираем и зачем


def _btn(tab: Tab, action: str, arg: str = "") -> str:
    return f"{tab.code}:{action}:{arg}" if arg else f"{tab.code}:{action}"


# --- Откуда пришли ----------------------------------------------------------
#
# «Назад» обязана возвращать туда, откуда пришли. Своей памяти у бота нет:
# Telegram присылает только код кнопки, поэтому происхождение экрана едет
# прямо в этом коде — коротким хвостом. Раньше хвоста не было, и карточка,
# открытая из «Ответили», из очереди или из поиска, одинаково выбрасывала
# владельца в начало всей базы: найденное место в списке терялось, и до
# нужного контакта приходилось долистывать заново.
#
# Формат хвоста (латиница и точка — в callback_data всего 64 байта):
#   l<стадия>.<страница>  — список стадии
#   q<страница>           — очередь писем
#   k<смещение>           — «кому звонили»
#   p<номер>              — карточка обзвона
#   c<pk>                 — карточка контакта
#   пусто                 — неизвестно, ведём в список всей базы

def origin_of_list(stage_code: str, page: int) -> str:
    return f"l{stage_code or 'all'}.{page}"


def origin_of_card(pk: int | str, inner: str = "") -> str:
    """Метка «пришли с карточки контакта», помнящая, откуда пришла сама карточка.

    Голое `c<pk>` теряло путь: с письма «назад» вело на карточку, а карточка
    уже не знала, из очереди её открыли или из списка, и уводила в начало всей
    базы. Хвост храним через точку — так же, как страницу в метке списка, и
    разбирается он тем же способом.
    """
    return f"c{pk}.{inner}" if inner else f"c{pk}"


def _split_origin(arg: str, parts: int = 1) -> tuple[list[str], str]:
    """Делит аргумент кнопки на «свои» части и хвост-происхождение.

    parts — сколько частей принадлежит самому действию: у карточки это один
    номер, у «mark» — стадия и номер.
    """
    pieces = (arg or "").split(":")
    own = pieces[:parts]
    while len(own) < parts:
        own.append("")
    rest = pieces[parts:]
    return own, rest[0] if rest else ""


def _with_origin(code: str, origin: str) -> str:
    return f"{code}:{origin}" if origin else code


def back_target(tab: Tab, origin: str) -> str:
    """Куда ведёт «назад» по метке происхождения."""
    if origin.startswith("l"):
        stage_code, _, page = origin[1:].partition(".")
        return _btn(tab, "list", f"{stage_code or 'all'}:{page or '0'}")
    if origin.startswith("q"):
        return _btn(tab, "queue", origin[1:] or "0")
    if origin.startswith("k"):
        return _btn(tab, "calls", origin[1:] or "0")
    if origin.startswith("p"):
        return _btn(tab, "ph", origin[1:] or "0")
    if origin.startswith("c"):
        pk, _, inner = origin[1:].partition(".")
        return _btn(tab, "card", _with_origin(pk, inner))
    return _btn(tab, "list", "all:0")


# --- Главный экран вкладки --------------------------------------------------

def home(tab: Tab, store: Store) -> Screen:
    counts = store.stage_counts(tab.kind)
    total = sum(counts.values())
    queued = store.queue_size(tab.kind)
    sent_today = store.sent_today(tab.kind)
    # Сравниваем однородное: слева касания вкладки, справа норма касаний.
    # Раньше справа стояла норма, урезанная прогревом ЯЩИКА, и на строке
    # встречались две разные величины — «3 из 3» при настроенных двадцати.
    limit = outreach.tab_limit(store, tab.kind)

    lines = [
        f"<b>{tab.icon} {tab.title}</b>",
        tab.hint,
        "",
        f"Всего в базе: <b>{total}</b> · написали {counts[Stage.WRITTEN]} · "
        f"ответили {counts[Stage.REPLIED]}",
        f"Сегодня отправлено: <b>{sent_today}</b> из {limit}",
    ]
    warmup = outreach.warmup_limit(store, tab.kind)
    mailed = store.mailed_today()
    if warmup < outreach.MAX_DAILY:
        # Прогрев ящика — общий на обе вкладки, и он часто ниже нормы вкладки.
        # Без этой строки владелец видит «0 из 20» и не понимает, почему
        # отправка встала после третьего письма.
        lines.append(f"Прогрев ящика: писем сегодня {mailed} из {warmup}")
    if not total:
        lines += [
            "",
            "База пустая. Нажмите «Загрузить из парсера» — Orbit прочитает выгрузки "
            "и разложит их по стадиям.",
        ]

    rows = [
        [(f"✉️ Очередь · {queued}", _btn(tab, "queue", "0")),
         (f"💬 Ответили · {counts[Stage.REPLIED]}", _btn(tab, "list", "replied:0"))],
        [(f"📇 Вся база · {total}", _btn(tab, "list", "all:0")),
         ("🆕 Новые · " + str(counts[Stage.NEW]), _btn(tab, "list", "new:0"))],
        [("✍️ Подготовить письма", _btn(tab, "prep"))],
        [("⚙️ Лимит и отправка", _btn(tab, "cfg")), ("⬇️ Загрузить из парсера", _btn(tab, "imp"))],
        [("⬅️ В главное меню", "home")],
    ]
    return Screen("\n".join(lines), rows)


# --- Списки -----------------------------------------------------------------

def _stage_from(arg: str) -> Stage | None:
    """«all» — весь срез вкладки, иначе конкретная стадия."""
    if arg in ("", "all"):
        return None
    try:
        return Stage(arg)
    except ValueError:
        return None


def show_list(tab: Tab, store: Store, arg: str) -> Screen:
    """Список контактов: «<стадия>:<страница>»."""
    stage_code, _, page_raw = arg.partition(":")
    stage = _stage_from(stage_code)
    page = _to_int(page_raw)

    total = store.count(tab.kind, stage)
    page = _clamp_page(page, total)
    items = store.contacts(tab.kind, stage, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    title = STAGE_TITLES[stage] if stage else "Вся база"
    icon = STAGE_ICONS[stage] if stage else "📇"
    lines = [f"<b>{icon} {title}</b> · {counted(total, tab.unit_one, tab.unit_few, tab.unit_many)}", ""]
    if not items:
        lines.append("Пусто.")

    origin = origin_of_list(stage_code or "all", page)
    rows: list[list[tuple[str, str]]] = []
    for contact in items:
        lines.append(_line(contact))
        rows.append([(_short(contact.name), _btn(tab, "card", f"{contact.pk}:{origin}"))])

    rows += pager(f"{tab.code}:list:{stage_code}", page, total)
    rows += _stage_switcher(tab, stage)
    rows.append([("⬅️ Назад", _btn(tab, "home"))])
    return Screen(clip("\n".join(lines)), rows)


def _stage_switcher(tab: Tab, current: Stage | None) -> list[list[tuple[str, str]]]:
    """Быстрое переключение между стадиями — не возвращаясь в меню вкладки."""
    # С подписью, а не одним значком: по 🆕 и 💬 без слов не догадаться, куда
    # ведёт кнопка, и владелец тыкает наугад. Значок оставляем — он помогает
    # найти нужную кнопку взглядом, но объясняет её слово.
    #
    # По две в ряд. Четыре подписи вроде «📞 Готовы к созвону» в одном ряду
    # Telegram ужимает по ширине экрана телефона, и от каждой остаётся значок
    # с обрубком слова — то есть ровно тот безымянный ряд значков, от которого
    # подписи и должны были избавить.
    buttons = [
        (f"{STAGE_ICONS[stage]} {STAGE_TITLES[stage]}", _btn(tab, "list", f"{stage.value}:0"))
        for stage in (Stage.NEW, Stage.WRITTEN, Stage.REPLIED, Stage.CALL)
        if stage is not current
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def _line(contact: Contact) -> str:
    """Одна строка списка: название, где и чем можем достать."""
    marks = []
    if contact.best_email:
        marks.append("✉️")
    if contact.best_telegram:
        marks.append("💬")
    if contact.primary_phone and not contact.reachable:
        marks.append("📞")
    where = f" · {esc(contact.city)}" if contact.city else ""
    return f"{STAGE_ICONS[contact.stage]} <b>{esc(contact.name)}</b>{where} {''.join(marks)}"


# Как называем события в истории контакта. Значок нужен, чтобы строка
# читалась взглядом, а не вычитывалась.
EVENT_LABELS = {
    "sent": "📤 письмо ушло",
    "reply": "💬 ответил",
    "call": "📞 звонок",
    "bounce": "↩️ письмо вернулось",
    "followup": "🔁 письмо вдогонку",
    "stop": "🚫 просил не писать",
    "autoreply": "🤖 автоответ",
    "channel": "🔀 сменили канал",
}
# Сколько строк истории показываем. Больше — экран превращается в простыню,
# а нужное («когда последний раз писали») уезжает вниз.
HISTORY_LIMIT = 4


def _history_lines(store: Store, contact: Contact) -> list[str]:
    """Последние события по контакту: что и когда мы с ним делали.

    Без истории карточка отвечает только на «где он сейчас», а владельцу перед
    звонком нужно «что уже было»: писали ли, отвечал ли, не вернулось ли
    письмо. Иначе это приходится вспоминать или не вспоминать вовсе.
    """
    if contact.pk is None:
        return []
    # Смены стадии пропускаем: текущая стадия уже написана строкой выше, а в
    # истории они дублировали соседнее событие теми же словами («письмо ушло»
    # и сразу «stage — написали»). Берём с запасом, чтобы после отсева
    # осталось чем заполнить список.
    rows = [
        row for row in store.events(contact.pk, limit=HISTORY_LIMIT * 3)
        if row["kind"] != "stage"
    ][:HISTORY_LIMIT]
    out = []
    for row in rows:
        label = EVENT_LABELS.get(row["kind"], row["kind"])
        day = str(row["at"])[8:10] + "." + str(row["at"])[5:7]
        note = f" — {esc(str(row['text'])[:60])}" if row["text"] else ""
        out.append(f"· {day} {label}{note}")
    return out


def _since_line(store: Store, contact: Contact) -> str:
    """« · 3 дня назад» к стадии. Пусто, если срок неизвестен.

    Берём последнее СОБЫТИЕ по контакту, а не поле updated_at: последнее
    трогает любая правка, включая нашу же проверку адреса, и «написали
    сегодня» появлялось у письма недельной давности.
    """
    from datetime import datetime

    from ..store import MSK

    if contact.pk is None:
        return ""
    row = store.conn.execute(
        "SELECT at FROM events WHERE contact_pk = ? ORDER BY pk DESC LIMIT 1", (contact.pk,)
    ).fetchone()
    if row is None or not row["at"]:
        return ""
    try:
        when = datetime.fromisoformat(row["at"])
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=MSK)
    days = (datetime.now(MSK) - when).days
    if days <= 0:
        return " · сегодня"
    if days == 1:
        return " · вчера"
    return f" · {days} {counted(days, 'день', 'дня', 'дней').split(' ', 1)[1]} назад"


def _short(name: str, limit: int = 28) -> str:
    name = name.strip()
    return name if len(name) <= limit else name[: limit - 1] + "…"


# --- Карточка ---------------------------------------------------------------

def card(tab: Tab, store: Store, arg: str) -> Screen:
    (pk_raw,), origin = _split_origin(arg)
    contact = store.get_contact(_to_int(pk_raw))
    if contact is None:
        return Screen("Контакт не найден.", [[("⬅️ Назад", back_target(tab, origin))]])

    lines = [f"<b>{esc(contact.name)}</b>"]
    place = " · ".join(filter(None, [esc(contact.city), esc(contact.meta.get("rubric"))]))
    if place:
        lines.append(place)
    if contact.url:
        lines.append(f'<a href="{esc(contact.url)}">{esc(contact.site)}</a>')

    lines.append("")
    # Стадия без давности мало что говорит: «написали» вчера и «написали» три
    # недели назад требуют разных действий, а по одной подписи их не отличить.
    lines.append(
        f"{STAGE_ICONS[contact.stage]} {STAGE_TITLES[contact.stage]}"
        f"{_since_line(store, contact)}"
    )

    contacts_line = []
    # Тот же адрес, что уйдёт в письмо: забракованный проверкой ящик в карточке
    # показывать нельзя — владелец решит, что пишем именно на него.
    shown_email = mailcheck.known_address(store, contact)
    if shown_email:
        # Почта на домене, не связанном с сайтом компании, — повод присмотреться:
        # из выгрузок так приходили адреса совсем других фирм.
        # Спрашиваем про показанный адрес, а не про первого кандидата: письмо
        # уйдёт именно на него.
        mark = " ⚠️ другой домен" if looks_foreign(contact.site, shown_email) else ""
        contacts_line.append(f"✉️ {esc(shown_email)}{mark}")
    if contact.best_telegram:
        handle = contact.best_telegram.lstrip("@")
        contacts_line.append(f'💬 <a href="https://t.me/{esc(handle)}">{esc(contact.best_telegram)}</a>')
    if contact.primary_phone:
        contacts_line.append(f"📞 {esc(contact.primary_phone)}")
    if contacts_line:
        lines.append(" · ".join(contacts_line))
    else:
        lines.append("Контактов нет: писать и звонить некуда.")

    for label, key in (("С чем идём", "pitch"), ("Рейтинг", "rating"), ("Отзывов", "reviews")):
        value = contact.meta.get(key)
        if value:
            lines.append(f"{label}: {esc(value)}")
    complaints = contact.meta.get("complaints") or []
    if complaints:
        lines.append("Из отзывов: " + esc("; ".join(str(c) for c in complaints[:2])))
    if contact.source_url:
        lines.append(f'<a href="{esc(contact.source_url)}">Карточка на карте</a>')
    if contact.note:
        lines.append(f"\nЗаметка: {esc(contact.note)}")

    history = _history_lines(store, contact)
    if history:
        lines += ["", "<b>Что было:</b>", *history]

    draft = store.draft_for(contact.pk or 0)
    if draft:
        lines += ["", "<b>Готовый текст:</b>", esc(draft.preview)]

    # Метку происхождения тащим дальше во все кнопки карточки: после отметки
    # стадии или правки письма владелец остаётся на той же карточке, и «назад»
    # обязана вести туда же, откуда он в неё вошёл.
    tail = f":{origin}" if origin else ""
    rows: list[list[tuple[str, str]]] = []
    if draft:
        # В метку письма кладём и путь самой карточки: иначе «назад» с письма
        # возвращает на карточку, а та уже не помнит, откуда её открыли.
        from_card = origin_of_card(contact.pk, origin)
        rows.append([("👀 Показать письмо", _btn(tab, "draft", f"{draft.pk}:{from_card}")),
                     ("♻️ Переписать", _btn(tab, "gen", f"{contact.pk}{tail}"))])
    elif contact.reachable:
        rows.append([("✍️ Написать письмо", _btn(tab, "gen", f"{contact.pk}{tail}"))])

    rows.append([("🚫 Не писать", _btn(tab, "dead", f"{contact.pk}{tail}")),
                 ("💬 Ответил", _btn(tab, "mark", f"replied:{contact.pk}{tail}"))])
    rows.append([("📞 Готов к созвону", _btn(tab, "mark", f"call:{contact.pk}{tail}")),
                 ("🤝 В работу", _btn(tab, "mark", f"deal:{contact.pk}{tail}"))])
    rows.append([("⬅️ Назад", back_target(tab, origin))])
    return Screen(clip("\n".join(lines)), rows)


# --- Очередь ----------------------------------------------------------------

def queue(tab: Tab, store: Store, arg: str) -> Screen:
    page = _to_int(arg)
    total = store.queue_size(tab.kind)
    page = _clamp_page(page, total)
    items = store.queue(tab.kind, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    # Остаток считает outreach: там же, где его считает и проверка перед самой
    # отправкой. Своя арифметика на экране («лимит минус касания») обещала
    # больше, чем уйдёт, — или меньше, если владелец писал руками в телеграм.
    limit = outreach.tab_limit(store, tab.kind)
    left = outreach.letters_left_today(store, tab.kind)

    in_flight = store.approved_count(tab.kind)
    lines = [f"<b>✉️ Готовы к отправке · {total}</b>",
             f"Сегодня осталось: {left} из {limit}"]
    if in_flight:
        lines.append(f"⏳ Уже в отправке: {in_flight} (уходят по одному, с паузами)")
    lines.append("")
    if not items:
        lines.append("Очередь пустая. Нажмите «Подготовить письма» на вкладке.")
    if not get_settings().smtp_ready:
        lines.append("⚠️ Почта не настроена: письма готовятся, но отправить их нечем.")
        lines.append("")

    here = f"q{page}"
    rows: list[list[tuple[str, str]]] = []
    for draft, contact in items:
        channel = "✉️" if draft.channel is Channel.EMAIL else "💬"
        lines.append(f"{channel} <b>{esc(contact.name)}</b>")
        if draft.subject:
            lines.append(f"«{esc(draft.subject)}»")
        lines.append(esc(draft.preview))
        if draft.error:
            lines.append(f"⚠️ {esc(draft.error)}")
        lines.append("")
        # Подписи у кнопок обязательны. Рядом стоят «отправить» и «удалить»,
        # и голые ✅ с 🗑 в одном ряду — это потерянное письмо или отправка,
        # которую не планировали: отозвать нельзя ни то, ни другое.
        rows.append([
            (f"👀 {_short(contact.name, 14)}", _btn(tab, "draft", f"{draft.pk}:{here}")),
            ("✅ Отправить", _btn(tab, "send", f"{draft.pk}:{here}")),
            ("🗑 Убрать", _btn(tab, "drop", f"{draft.pk}:{here}")),
        ])

    rows += pager(f"{tab.code}:queue", page, total)
    if items:
        # Считаем ТОЛЬКО почту: телеграм фоновый отправщик отправить не может
        # (бот не пишет первым), и «Отправить все (5)» при трёх письмах и двух
        # телеграм-черновиках обещало пять, а уходило три. Цифра на кнопке
        # должна совпадать с тем, что произойдёт по нажатию.
        #
        # И только те, что ещё ждут нажатия: уже одобренные лежат в том же
        # статусе 'draft', пока их не заберёт отправщик. Они попадали в счёт, и
        # кнопка обещала работу, которой не было, — нажатие отвечало
        # «Отправлять нечего» при семёрке на самой кнопке.
        waiting = store.queue_size(tab.kind, channel=Channel.EMAIL, waiting_only=True)
        if waiting:
            rows.append([(f"✅ Отправить все ({min(waiting, left)})", _btn(tab, "sendall"))])
        rows.append([("🗑 Очистить очередь", _btn(tab, "clear"))])
    rows.append([("⬅️ Назад", _btn(tab, "home"))])
    return Screen(clip("\n".join(lines)), rows)


def show_draft(tab: Tab, store: Store, arg: str) -> Screen:
    """Письмо целиком, как оно уйдёт адресату."""
    (pk_raw,), origin = _split_origin(arg)
    # Письмо открывают из двух мест: из очереди и из карточки контакта.
    # Возвращать надо туда же — иначе владелец, заглянувший в текст из
    # карточки, оказывается в общей очереди и ищет карточку заново.
    back = ("⬅️ К очереди", _btn(tab, "queue", "0"))
    if origin.startswith("c"):
        back = ("⬅️ К карточке", back_target(tab, origin))
    elif origin.startswith("q"):
        back = ("⬅️ К очереди", back_target(tab, origin))

    draft = store.get_draft(_to_int(pk_raw))
    if draft is None:
        return Screen("Письмо не найдено.", [[back]])
    contact = store.get_contact(draft.contact_pk)
    if contact is None:
        return Screen("Контакт не найден.", [[back]])

    # Именно проверенный адрес, а не первый по списку: если у компании два
    # ящика и первый забракован проверкой, письмо уйдёт на второй — и владелец
    # должен видеть в карточке тот же адрес, что получит адресат.
    where = (
        mailcheck.known_address(store, contact)
        if draft.channel is Channel.EMAIL
        else contact.best_telegram
    )
    # Пустое «→ » читается как «всё в порядке, адрес просто не влез». Если
    # писать некуда, это надо сказать словами: письмо стоит в очереди зря.
    lines = [f"<b>{esc(contact.name)}</b> → {esc(where) if where else '⚠️ писать некуда'}"]
    if draft.channel is Channel.EMAIL and looks_foreign(contact.site, where):
        # Последний экран перед нажатием «Отправить» — здесь это и надо сказать.
        lines.append(
            f"⚠️ Домен почты не связан с сайтом {esc(contact.site)} — "
            f"проверьте, та ли это компания."
        )
    if draft.subject:
        lines.append(f"Тема: <b>{esc(draft.subject)}</b>")
    lines += ["", esc(outreach.compose(draft, contact))]

    tail = f":{origin}" if origin else ""
    rows: list[list[tuple[str, str]]] = []
    if draft.status == "draft" and draft.channel is Channel.TELEGRAM and contact.best_telegram:
        handle = contact.best_telegram.lstrip("@")
        # Бот не может написать первым — открываем чат владельцу, текст выше
        # копируется одним касанием.
        rows.append([("💬 Открыть чат", f"url:https://t.me/{handle}")])
        rows.append([("✅ Отправил", _btn(tab, "manual", f"{draft.pk}{tail}")),
                     ("🗑 Удалить", _btn(tab, "drop", f"{draft.pk}{tail}"))])
    elif draft.status == "draft":
        rows.append([("✅ Отправить", _btn(tab, "send", f"{draft.pk}{tail}")),
                     ("♻️ Переписать", _btn(tab, "gen", f"{contact.pk}{tail}")),
                     ("🗑 Убрать", _btn(tab, "drop", f"{draft.pk}{tail}"))])
    rows.append([back])
    return Screen(clip("\n".join(lines)), rows)


# --- Действия ---------------------------------------------------------------

def _queue_page(origin: str) -> str:
    """Страница очереди, с которой пришли. Неизвестно — первая."""
    return origin[1:] if origin.startswith("q") and origin[1:].isdigit() else "0"


def send_one(tab: Tab, store: Store, arg: str) -> Screen:
    (pk_raw,), origin = _split_origin(arg)
    result = outreach.send(store, _to_int(pk_raw))
    screen = queue(tab, store, _queue_page(origin))
    screen.alert = "Отправлено" if result.ok else result.reason
    if result.manual:
        screen.alert = "Телеграм отправляется вручную: откройте письмо и нажмите «Открыть чат»"
    return screen


def send_all(tab: Tab, store: Store) -> Screen:
    """Ставит очередь на отправку. Письма уходят по одному, с паузами.

    Разом их слать нельзя: двадцать писем, ушедших за секунду, — первый
    признак рассылки и для получателя, и для спам-фильтра. Поэтому здесь
    только отметка, а темп держит фоновый отправщик.
    """
    # Только почта. Телеграм фоновый отправщик отправить не может (бот не
    # пишет первым — ограничение платформы), а одобренный телеграм-черновик
    # съедал дневную квоту, навсегда висел «в отправке» и вытеснял из этого
    # захода почтовые письма, стоявшие следом. Телеграм уходит только кнопкой
    # «Отправил» после ручной отправки.
    approved = outreach.approve_queue(store, tab.kind, channel=Channel.EMAIL)
    screen = queue(tab, store, "0")
    if not approved:
        left = outreach.letters_left_today(store, tab.kind)
        screen.alert = (
            "Суточный лимит исчерпан, письма уйдут завтра" if left <= 0
            else "Отправлять нечего: очередь пуста или письма уже в отправке"
        )
        return screen

    minutes = max(1, round(approved * sum(outreach.PAUSE_RANGE) / 2 / 60))
    screen.alert = f"Поставил в отправку {approved}. Уйдут примерно за {minutes} мин."
    return screen


def mark_manual(tab: Tab, store: Store, arg: str) -> Screen:
    (pk_raw,), origin = _split_origin(arg)
    result = outreach.mark_sent_manually(store, _to_int(pk_raw))
    screen = queue(tab, store, _queue_page(origin))
    # Показываем ровно то, что вернула отправка. Она записывает свершившийся
    # факт всегда, но вместе с ним отдаёт предупреждение: суточный лимит
    # превышен, контакт просил не писать, разговор уже идёт. Раньше при ok
    # подставлялась своя бодрая строка, и предупреждение до владельца не
    # доходило вовсе — то есть он узнавал о выходе за рамки ниоткуда.
    screen.alert = result.reason or "Записал как отправленное"
    return screen


def drop(tab: Tab, store: Store, arg: str) -> Screen:
    (pk_raw,), origin = _split_origin(arg)
    dropped = store.drop_draft(_to_int(pk_raw))
    screen = queue(tab, store, _queue_page(origin))
    screen.alert = "Убрал из очереди" if dropped else "Уже нет в очереди"
    return screen


def clear(tab: Tab, store: Store) -> Screen:
    """Спрашивает подтверждение. Само удаление — в clear_yes().

    Кнопка стоит прямо под «Отправить все» и сносит все готовые письма разом,
    а каждое из них — оплаченный вызов модели и текст, написанный под
    конкретную компанию. Промах пальцем не должен стоить всей пачки.
    """
    total = store.queue_size(tab.kind)
    if not total:
        screen = queue(tab, store, "0")
        screen.alert = "Очередь и так пуста"
        return screen
    text = (
        f"<b>🗑 Очистить очередь?</b>\n\n"
        f"Удалю {counted(total, 'письмо', 'письма', 'писем')} — все готовые тексты "
        f"пропадут, и писать их придётся заново.\n\nЭто необратимо."
    )
    rows = [
        [("🗑 Да, удалить всё", _btn(tab, "clearyes"))],
        [("← Оставить как есть", _btn(tab, "queue", "0"))],
    ]
    return Screen(text, rows)


def clear_yes(tab: Tab, store: Store) -> Screen:
    """Само удаление — только после подтверждения."""
    count = store.clear_queue(tab.kind)
    screen = queue(tab, store, "0")
    screen.alert = f"Очередь очищена: {count}"
    return screen


def mark_stage(tab: Tab, store: Store, arg: str) -> Screen:
    """«<стадия>:<pk>[:<откуда>]» — двигаем контакт по воронке руками."""
    (stage_code, pk_raw), origin = _split_origin(arg, parts=2)
    pk = _to_int(pk_raw)
    back = _with_origin(str(pk), origin)
    try:
        stage = Stage(stage_code)
    except ValueError:
        return card(tab, store, back)
    store.set_stage(pk, stage, "вручную")
    screen = card(tab, store, back)
    screen.alert = STAGE_TITLES[stage]
    return screen


def mark_dead(tab: Tab, store: Store, arg: str) -> Screen:
    (pk_raw,), origin = _split_origin(arg)
    pk = _to_int(pk_raw)
    store.set_stage(pk, Stage.DEAD, "закрыт вручную")
    draft = store.draft_for(pk)
    if draft and draft.pk:
        store.drop_draft(draft.pk)  # письмо в очереди больше не нужно
    screen = card(tab, store, _with_origin(str(pk), origin))
    screen.alert = "Больше не пишем"
    return screen


# --- Генерация текстов ------------------------------------------------------

# Контакты, по которым письмо пишется прямо сейчас.
#
# Модель отвечает по десять-двадцать секунд, и всё это время бот молчит:
# сообщение не правится, пока обработчик не вернулся. Владелец жмёт
# «Написать письмо» второй раз — нажатие ЖДЁТ в очереди обновлений, а потом
# заказывает у модели ВТОРОЙ текст по тому же контакту. Оба стоят денег, оба
# отвечают «Письмо готово», а в очередь ложится только последний: на живой
# базе это прямой расход за текст, который никто не увидит.
_writing_now: set[int] = set()
_writing_lock = threading.Lock()


def generate_one(tab: Tab, store: Store, arg: str) -> Screen:
    """Пишет письмо одному контакту. Синхронно: это несколько секунд."""
    (pk_raw,), origin = _split_origin(arg)
    pk = _to_int(pk_raw)
    contact = store.get_contact(pk)
    if contact is None:
        return Screen("Контакт не найден.", [[("⬅️ Назад", back_target(tab, origin))]])

    ok, reason = write_for(store, contact)
    # Если пришли сюда с письма ЭТОГО же контакта, метку разворачиваем: иначе
    # карточка получает «назад» на саму себя, и выйти из неё нельзя вовсе.
    if origin.startswith(f"c{pk}"):
        origin = origin[len(f"c{pk}"):].lstrip(".")
    screen = card(tab, store, _with_origin(str(pk), origin))
    screen.alert = "Письмо готово" if ok else reason
    return screen


def write_for(store: Store, contact: Contact) -> tuple[bool, str]:
    """Одно письмо в очередь. Возвращает (получилось, причина).

    Замок на контакт — здесь, а не в кнопке. Писать письмо просят из двух
    мест: владелец кнопкой и фоновая пачка. Пока модель отвечает (десять-
    двадцать секунд), они успевают встретиться на одном контакте, и тогда за
    один и тот же текст мы платим дважды, а в очередь ложится только
    последний. Замок в обработчике кнопки от пачки не спасал: у неё свой
    поток и свой вызов.
    """
    if contact.pk is None:
        return False, "контакт не сохранён"

    with _writing_lock:
        if contact.pk in _writing_now:
            return False, "письмо этому контакту уже пишется"
        _writing_now.add(contact.pk)
    try:
        return _write_for(store, contact)
    finally:
        with _writing_lock:
            _writing_now.discard(contact.pk)


def _write_for(store: Store, contact: Contact) -> tuple[bool, str]:
    """Сама подготовка письма. Замок держит write_for."""
    if contact.pk is None:
        return False, "контакт не сохранён"

    # Перечитываем карточку перед решением. Пачку писем готовит фоновый поток,
    # и список он получает заранее — а пишет по одному, по несколько секунд на
    # письмо. За эти минуты владелец успевает нажать «Не писать» или ответить
    # клиенту. Со снимком, снятым до начала работы, мы бы этого не увидели:
    # закрытому контакту готовилось бы письмо, а тому, кто уже ответил, ушло
    # бы холодное.
    fresh = store.get_contact(contact.pk)
    if fresh is None:
        return False, "контакт исчез из базы"
    contact = fresh

    if contact.stage is Stage.DEAD:
        return False, "контакт закрыт"
    if contact.stage in outreach.ALREADY_TALKING:
        return False, "разговор уже идёт — холодное письмо здесь лишнее"
    if not contact.reachable:
        return False, "писать некуда, только телефон"

    # Проверяем до обращения к модели: письмо сети, которой уже написали, не
    # только выглядит спамом, но и стоит денег за генерацию.
    siblings = store.written_siblings(
        contact.kind, contact.name, skip_pk=contact.pk, site=contact.site
    )
    if siblings:
        store.set_stage(contact.pk, Stage.DEAD, f"той же сети уже писали: {siblings[0].name}"[:200])
        return False, f"этой сети уже писали ({len(siblings)} филиалов в базе)"

    # Адрес проверяем до модели по той же причине, что и сеть: письмо на
    # мёртвый домен стоит денег за генерацию, а потом ещё и репутации домена
    # за возврат. Телеграм-контакты проверять нечем и незачем.
    channel = contact.channel
    if channel is Channel.EMAIL:
        address, verdict = mailcheck.pick_address(store, contact)
        if address is None:
            if not verdict.permanent:
                # Проверить не смогли — контакт не виноват, вернёмся к нему позже.
                return False, f"адрес не проверить: {verdict.reason}"
            if contact.best_telegram:
                # Почта мёртвая, но телеграм живой. Закрывать такой контакт —
                # значит терять компанию из-за одного канала связи, до которой
                # можно достучаться другим.
                channel = Channel.TELEGRAM
                store.log_event(contact.pk, "channel", f"почта не годится: {verdict.reason}"[:200])
            else:
                store.set_stage(contact.pk, Stage.DEAD, f"адрес не годится: {verdict.reason}"[:200])
                return False, f"адрес не годится: {verdict.reason}"

    letter = llm.write_letter(contact, channel)
    if letter is None:
        return False, "модель не ответила"
    if not letter.decided:
        # Ответ есть, а решения в нём не видно. Контакт не трогаем: закрыть его
        # тут — значит потерять живую компанию из-за одной кривой строки.
        return False, "модель ответила непонятно"
    if not letter.write:
        # Отказ модели — это решение, а не сбой: контакт закрываем, чтобы он
        # не всплывал в каждой следующей пачке.
        store.set_stage(contact.pk, Stage.DEAD, f"модель: {letter.reason}"[:200])
        return False, f"пропускаем: {letter.reason}"
    if not letter.body.strip():
        return False, "модель вернула пустой текст"

    # И ещё раз, уже после модели. Проверка выше стояла ДО обращения к ней, а
    # модель отвечает по десять-двадцать секунд на письмо: ровно в это окно
    # владелец и нажимает «Не писать», прочитав карточку. Со старой проверкой
    # письмо всё равно ложилось в очередь, а `set_stage(QUEUED)` возвращал уже
    # закрытый контакт в работу — и следующий проход отправщика писал человеку,
    # которого владелец только что закрыл руками.
    fresh = store.get_contact(contact.pk)
    if fresh is None:
        return False, "контакт исчез из базы, пока писалось письмо"
    if fresh.stage is Stage.DEAD:
        logger.info("«{}»: закрыт, пока писалось письмо — в очередь не кладу", contact.name)
        return False, "контакт закрыли, пока писалось письмо"
    if fresh.stage in outreach.ALREADY_TALKING:
        logger.info("«{}»: ответил, пока писалось письмо — холодное не кладу", contact.name)
        return False, "контакт ответил, пока писалось письмо"

    store.save_draft(Draft(
        contact_pk=contact.pk,
        channel=channel,
        subject=letter.subject,
        body=letter.body,
    ))
    store.set_stage(contact.pk, Stage.QUEUED, "текст готов")
    return True, "готово"


def prepare(tab: Tab, store: Store) -> Screen:
    """Готовит пачку писем в фоне: столько, сколько осталось по лимиту."""
    limit = outreach.tab_limit(store, tab.kind)
    left = outreach.letters_left_today(store, tab.kind)
    already = store.queue_size(tab.kind)
    need = max(0, left - already)

    # Причины «готовить нечего» разные, и путать их нельзя: при исчерпанном
    # лимите ждать нужно до завтра, а при полной очереди — просто отправить.
    if left == 0:
        screen = home(tab, store)
        screen.alert = f"Суточный лимит исчерпан: {limit} писем. Следующие уйдут завтра"
        return screen
    if need == 0:
        screen = home(tab, store)
        screen.alert = f"В очереди уже {already} — этого хватит на сегодня"
        return screen

    candidates = [
        contact for contact in store.contacts(tab.kind, Stage.NEW, limit=need * 3)
        if contact.reachable
    ]
    if not candidates:
        screen = home(tab, store)
        screen.alert = "Некому писать: новых контактов с почтой или телеграмом нет"
        return screen

    screen = home(tab, store)
    if not _spawn_writer(store.path, candidates[:need], tab.title):
        # Называем вкладку: замок один на обе, и «уже готовлю» без имени
        # отправляло владельца искать письма не в той очереди.
        busy = _writing_tab[0] if _writing_tab else ""
        screen.alert = (
            f"Сейчас пишется пачка «{busy}» — счёт за модель общий, дождитесь её"
            if busy and busy != tab.title
            else "Уже готовлю письма — загляните в очередь через минуту"
        )
        return screen
    screen.alert = f"Готовлю {min(need, len(candidates))} писем, загляните в очередь через минуту"
    return screen


# Пачку пишем по одной за раз. Кнопка нажимается второй раз слишком легко:
# экран не обновился, палец соскользнул — а очередь наполняется не мгновенно,
# и второй поток видит те же контакты в том же «новом» состоянии. Модель
# отвечает дважды по каждой компании, и оба ответа платные. Ключ на все системы
# один, так что это не мелочь: лишний расход тут останавливает и биржи, и почту.
#
# Замок ОДИН на обе вкладки, и это тоже осознанно: платит за письма один и тот
# же счёт, и две пачки разом опустошают его вдвое быстрее. Но кто именно занял
# замок, надо называть вслух: владелец нажимал «Подготовить письма» у клиентов,
# получал «уже готовлю» и шёл искать письма в клиентской очереди, а писалась
# в это время пачка партнёров.
_writer_lock = threading.Lock()
_writing_tab: list[str] = []


def _spawn_writer(db_path, contacts: list[Contact], tab_title: str = "") -> bool:
    """Пишет пачку в отдельном потоке со своей связью с базой.

    False — если пачка уже пишется. Замок берём здесь, в вызывающем потоке:
    возьми его сам работник, два нажатия подряд успели бы проскочить оба.
    """
    if not _writer_lock.acquire(blocking=False):
        return False
    _writing_tab[:] = [tab_title]

    def worker() -> None:
        # Связь с базой открываем ВНУТРИ try. Раньше она открывалась строкой
        # выше, и отказ на ней (занятый файл, кончившееся место, битый диск)
        # уносил поток вместе с замком: кнопка «Подготовить письма» после
        # этого молча отвечала «уже готовлю» до перезапуска бота, а не
        # готовила ничего.
        store = None
        try:
            store = Store(db_path)
            for contact in contacts:
                try:
                    ok, reason = write_for(store, contact)
                except Exception as exc:  # noqa: BLE001 — одно письмо не рушит пачку
                    logger.warning("«{}»: письмо не получилось: {}", contact.name, exc)
                    continue
                if not ok:
                    logger.info("«{}»: {}", contact.name, reason)
        except Exception as exc:  # noqa: BLE001 — упавшая пачка не должна вешать кнопку
            logger.error("Пачка писем не пошла: {}", exc)
            _tell_batch_died(exc)
        finally:
            if store is not None:
                store.close()
            _writing_tab.clear()
            _writer_lock.release()

    try:
        threading.Thread(target=worker, name="orbit-writer", daemon=True).start()
    except Exception:
        # Поток не завёлся — замок надо снять, иначе кнопка умрёт до перезапуска.
        _writing_tab.clear()
        _writer_lock.release()
        raise
    return True


def _tell_batch_died(exc: Exception) -> None:
    """Говорит владельцу, что пачка писем не пошла.

    Экран уже пообещал «Готовлю N писем, загляните в очередь через минуту».
    Если поток умер, в очереди не появится ничего, и без этой строки владелец
    узнает об этом, только заглянув и не найдя писем — а решит, что модель
    просто медленная.
    """
    from .. import ui
    from ..notify import tell

    try:
        tell(
            f"⚠️ <b>Пачка писем не пошла</b>\n"
            f"В очереди ничего не появится — попробуйте ещё раз.\n"
            f"<i>{ui.esc(str(exc))}</i>"
        )
    except Exception as tell_exc:  # noqa: BLE001 — не смогли сказать, но кнопку освободили
        logger.warning("Не отправилось сообщение о неудавшейся пачке: {}", tell_exc)


# --- Настройки вкладки ------------------------------------------------------

def config(tab: Tab, store: Store) -> Screen:
    # Здесь показываем НАСТРОЕННОЕ значение, а не сегодняшний потолок: экран
    # про настройку, и кнопки ниже меняют именно его. Урезанное прогревом
    # число выглядело так, будто кнопка «20» не сработала.
    limit = outreach.tab_limit(store, tab.kind)
    settings = get_settings()

    lines = [
        f"<b>⚙️ {tab.title}: отправка</b>",
        "",
        f"Писем в день: <b>{limit}</b>",
    ]
    warmup = outreach.warmup_limit(store, tab.kind)
    if warmup < limit:
        lines.append(
            f"Сегодня из ящика уйдёт не больше <b>{warmup}</b>: ящик ещё прогревается. "
            f"Это ограничение снимется само."
        )
    lines += [
        "",
        "<b>Режимы по каналам</b>",
    ]

    rows: list[list[tuple[str, str]]] = [
        [("10", _btn(tab, "lim", "10")), ("20", _btn(tab, "lim", "20")),
         ("50", _btn(tab, "lim", "50")), ("100", _btn(tab, "lim", "100"))],
    ]

    for channel, title in ((Channel.EMAIL, "✉️ Почта"), (Channel.TELEGRAM, "💬 Телеграм")):
        auto = outreach.autosend_on(store, channel)
        ready, why = outreach.channel_ready(channel)
        state = "сама" if (auto and ready) else "по кнопке"
        lines.append(f"{title}: <b>{state}</b>" + (f" · {esc(why)}" if not ready else ""))
        if channel not in outreach.SELF_SENDING:
            # Канал не умеет отправлять сам в принципе — переключать нечего.
            # Кнопка тут стояла и загоралась «🟢 сама» ровно там, где строка
            # над ней говорила «по кнопке»: владелец видел зелёный знак,
            # ждал, что письма пойдут сами, и не дожидался ничего.
            continue
        if auto and not ready:
            # Включено, но канал не настроен: не «сама» и не «по кнопке», а
            # «включено и не работает». Зелёный знак здесь означал бы, что
            # письма уходят, — а не уходит ни одно.
            mark = "🟡 включено, но не шлёт"
        else:
            mark = "🟢 сама" if auto else "🔴 по кнопке"
        rows.append([(f"{title}: {mark}", _btn(tab, "auto", channel.value))])

    lines += [
        "",
        "Прогрев считается по дням, когда письма действительно уходили: ящик, "
        "отправивший пачку и замолчавший на неделю, для почтовых служб снова "
        "новый. Резкий рост объёма — прямая дорога в спам вместе с доменом.",
    ]
    if not settings.smtp_ready:
        lines += [
            "",
            "Чтобы письма уходили, впишите в <code>orbit/.env</code> доступ к ящику: "
            "SMTP_HOST, SMTP_USER, SMTP_PASSWORD. Пароль нужен «для приложений», "
            "а не основной пароль от почты.",
        ]

    rows.append([("⬅️ Назад", _btn(tab, "home"))])
    return Screen("\n".join(lines), rows)


def set_limit(tab: Tab, store: Store, arg: str) -> Screen:
    value = max(0, min(outreach.MAX_DAILY, _to_int(arg)))
    store.set_state(outreach.LIMIT_KEYS[tab.kind], str(value))
    screen = config(tab, store)
    screen.alert = f"Лимит: {value} писем в день"
    return screen


def toggle_autosend(tab: Tab, store: Store, arg: str = "") -> Screen:
    """Переключает автоотправку в одном канале."""
    try:
        channel = Channel(arg) if arg else Channel.EMAIL
    except ValueError:
        channel = Channel.EMAIL

    if channel not in outreach.SELF_SENDING:
        # Кнопки для такого канала на экране нет, но старое нажатие могло
        # прилететь из истории чата. Включать нечего — говорим прямо.
        screen = config(tab, store)
        _ready, why = outreach.channel_ready(channel)
        screen.alert = f"Так не выйдет: {why}"
        return screen

    new_value = not outreach.autosend_on(store, channel)
    outreach.set_autosend(store, channel, new_value)

    screen = config(tab, store)
    title = "Почта" if channel is Channel.EMAIL else "Телеграм"
    if new_value:
        ready, why = outreach.channel_ready(channel)
        screen.alert = f"{title}: отправка сама" if ready else f"{title}: включено, но {why}"
    else:
        screen.alert = f"{title}: отправка по кнопке"
    return screen


# --- Импорт -----------------------------------------------------------------

def do_import(tab: Tab, store: Store) -> Screen:
    from .. import importer

    directory = get_settings().leads_path / "output"
    rows, written, broken = importer.import_dir(store, directory)
    screen = home(tab, store)
    # «Не смогли прочитать» и «нечего читать» — разные беды с разным лечением.
    # Первую чинит парсер, вторую — путь к каталогу; раньше обе выглядели
    # одинаково, и владелец искал каталог, который был на месте.
    names = ", ".join(broken[:3]) + ("…" if len(broken) > 3 else "")
    if not rows and broken:
        screen.alert = f"Ни одна выгрузка не прочиталась ({len(broken)}): {names}"
    elif not rows:
        screen.alert = f"В {directory} выгрузок не нашлось"
    elif broken:
        screen.alert = f"Прочитано {rows}, в базе {written} · не прочиталось {len(broken)}: {names}"
    else:
        screen.alert = f"Прочитано {rows}, в базе {written}"
    return screen


# --- Состояние вкладки ------------------------------------------------------

def health(tab: Tab, store: Store) -> Health:
    total = store.count(tab.kind)
    if not total:
        return Health(False, "база пустая", ["загрузите выгрузки парсера"])
    sent = store.sent_today(tab.kind)
    limit = outreach.tab_limit(store, tab.kind)
    queued = store.queue_size(tab.kind)
    details = [f"в базе {total}", f"в очереди {queued}"]
    if not get_settings().smtp_ready:
        return Health(False, "почта не настроена", details)
    return Health(True, f"сегодня {sent} из {limit}", details)


def summary_line(tab: Tab, store: Store) -> str:
    total = store.count(tab.kind)
    replied = store.stage_counts(tab.kind)[Stage.REPLIED]
    sent = store.sent_today(tab.kind)
    return f"{total} в базе · сегодня {sent} · ответили {replied}"


def _clamp_page(page: int, total: int) -> int:
    """Не даёт открыть страницу, которой больше нет.

    «Назад» помнит, на какой странице был владелец, — и это правильно. Но
    список за это время мог укоротиться: письма ушли, контакты закрылись,
    ответивший переехал в другую стадию. Раньше в такой ситуации открывался
    экран «Пусто.» без листалки (листать нечего) и без пути назад к первой
    странице: единственной кнопкой оставалось «в меню вкладки».
    """
    last = max(0, (max(0, total) - 1) // PAGE_SIZE)
    return min(max(0, page), last)


def _to_int(raw: str) -> int:
    """Число из аргумента кнопки. Мусор и отрицательные — это ноль.

    Отрицательное значение кнопками не появляется, но приходит из
    самодельного кода: срез списка с отрицательным началом молча отдаёт
    хвост, и владелец видит не тот кусок базы, что просил.
    """
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


# --- Роутер вкладки ---------------------------------------------------------

def route(tab: Tab, store: Store, action: str, arg: str) -> Screen:
    """Общие действия обеих вкладок. Своё вкладка обрабатывает до вызова."""
    handlers = {
        "home": lambda: home(tab, store),
        "list": lambda: show_list(tab, store, arg),
        "card": lambda: card(tab, store, arg),
        "queue": lambda: queue(tab, store, arg),
        "draft": lambda: show_draft(tab, store, arg),
        "send": lambda: send_one(tab, store, arg),
        "sendall": lambda: send_all(tab, store),
        "manual": lambda: mark_manual(tab, store, arg),
        "drop": lambda: drop(tab, store, arg),
        "clear": lambda: clear(tab, store),
        "clearyes": lambda: clear_yes(tab, store),
        "mark": lambda: mark_stage(tab, store, arg),
        "dead": lambda: mark_dead(tab, store, arg),
        "gen": lambda: generate_one(tab, store, arg),
        "prep": lambda: prepare(tab, store),
        "cfg": lambda: config(tab, store),
        "lim": lambda: set_limit(tab, store, arg),
        "auto": lambda: toggle_autosend(tab, store, arg),
        "imp": lambda: do_import(tab, store),
    }
    handler = handlers.get(action)
    if handler is None:
        return home(tab, store)
    return handler()

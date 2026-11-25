"""Вкладка «Клиенты» — компании с карт.

Живой бизнес из 2ГИС и Яндекс.Карт: клиники, автосервисы, салоны, магазины.
Они ничего у нас не спрашивали, поэтому письмо идёт от их боли, а не от нашего
списка услуг (см. knowledge/clients.md).

Сверх общей механики здесь есть «Телефоны»: компании, у которых нашёлся только
номер. Писать им некуда, поэтому Orbit собирает по каждой профиль с зацепкой
для звонка и даёт листать их по одной.
"""
from __future__ import annotations

from loguru import logger

from ..models import Kind, Stage
from ..registry import Health, Section
from ..store import Store, now_msk
from ..ui import Screen, clip, esc
from . import base

TAB = base.Tab(
    code="c",
    kind=Kind.CLIENT,
    title="Клиенты",
    icon="🏢",
    unit_one="компания",
    unit_few="компании",
    unit_many="компаний",
    hint="Компании с карт: пишем тем, у кого есть почта или телеграм.",
)

# Заметка, по которой видно, что контакт отложен на повторный звонок.
CALLBACK_NOTE = "перезвонить"

# Что записали в карточке после звонка.
CALLED_AT = "called_at"
CALL_RESULT = "call_result"
CALL_TRIES = "call_tries"

# Чем кончился разговор. Код короткий: он едет в callback_data, где всего
# 64 байта на всё вместе с номером карточки.
RESULTS: dict[str, tuple[str, str, Stage | None]] = {
    # код: (кнопка, что пишем в списке, куда двигаем стадию)
    "talk": ("🗣 Поговорили", "поговорили", Stage.WRITTEN),
    "warm": ("🔥 Заинтересовались", "заинтересовались", Stage.REPLIED),
    "no": ("🚫 Отказ", "отказ", Stage.DEAD),
    "miss": ("📵 Не дозвонился", "не дозвонился", None),
}
# Сколько звонков в списке на одном экране.
PAGE = 8


def handle(store: Store, action: str, arg: str) -> Screen:
    """Своё у вкладки — обзвон; остальное общее."""
    if action == "ph":
        return phones(store, arg)
    if action == "called":
        return ask_result(store, arg)
    if action == "res":
        return save_result(store, arg)
    if action == "calls":
        return called_list(store, arg)
    if action == "later":
        return mark_later(store, arg)
    if action == "tip":
        return call_tip(store, arg)
    if action == "skip":
        return mark_not_ours(store, arg)
    return base.route(TAB, store, action, arg)


# --- Обзвон -----------------------------------------------------------------

def _to_call(store: Store) -> list:
    """Компании, которым писать некуда: только телефон.

    Те, кому уже звонили и не дозвонились, остаются в списке, но уходят
    в конец: сначала свежие, потом повторные попытки.
    """
    items = [
        contact
        for contact in store.contacts(Kind.CLIENT, Stage.NEW, limit=10**9, only_reachable=False)
        if contact.primary_phone and not _postponed(contact)
    ]
    return sorted(items, key=lambda contact: bool(contact.meta.get(CALLED_AT)))


def _postponed(contact) -> bool:
    """Компания отложена кнопкой «Перезвонить», и срок ещё не настал."""
    from ..store import today_msk

    when = str(contact.meta.get("remind_after") or "").strip()
    return bool(when) and when > today_msk()


def _called(store: Store) -> list:
    """Кому уже звонили — в порядке от свежих к старым."""
    items = [
        contact
        for contact in store.contacts(Kind.CLIENT, limit=10**9, only_reachable=False)
        if contact.meta.get(CALLED_AT)
    ]
    return sorted(items, key=lambda contact: contact.meta.get(CALLED_AT, ""), reverse=True)


def phones(store: Store, arg: str) -> Screen:
    """Профиль для звонка. Листается по одному: «ph:<номер по порядку>»."""
    items = _to_call(store)
    total = len(items)
    if not total:
        return Screen(
            "<b>📞 Обзвон</b>\n\nПусто: компаний, у которых есть только телефон, в базе нет.",
            [[("⬅️ Назад", "c:home")]],
        )

    index = max(0, min(base._to_int(arg), total - 1))
    contact = items[index]

    lines = [f"<b>📞 Обзвон · {index + 1} из {total}</b>", "", f"<b>{esc(contact.name)}</b>"]
    place = " · ".join(filter(None, [esc(contact.city), esc(contact.meta.get("rubric"))]))
    if place:
        lines.append(place)
    lines.append(f"<b>{esc(contact.primary_phone)}</b>")

    facts = []
    if contact.meta.get("rating"):
        facts.append(f"⭐ {esc(contact.meta['rating'])}")
    if contact.meta.get("reviews"):
        facts.append(f"{esc(contact.meta['reviews'])} отзывов")
    if contact.meta.get("entity"):
        facts.append(esc(contact.meta["entity"]))
    if facts:
        lines.append(" · ".join(facts))

    site_note = "есть" if contact.site else (contact.meta.get("has_site") or "неизвестно")
    lines.append(f"Сайт: {esc(site_note)}")
    if contact.url:
        lines.append(f'<a href="{esc(contact.url)}">{esc(contact.site)}</a>')
    if contact.source_url:
        lines.append(f'<a href="{esc(contact.source_url)}">Карточка на карте</a>')

    complaints = contact.meta.get("complaints") or []
    if complaints:
        lines += ["", "<b>Из отзывов:</b>"]
        lines += [f"• {esc(str(c))}" for c in complaints[:3]]
    if contact.note:
        lines += ["", f"<b>С чего начать:</b> {esc(contact.note)}"]

    # Номер в списке едет в кнопке: без него любое действие возвращало
    # владельца на первую карточку, и до восьмой компании приходилось
    # листать заново.
    rows = [
        [("✅ Позвонил", f"c:called:{contact.pk}:{index}"),
         ("📅 Перезвонить", f"c:later:{contact.pk}:{index}")],
        [("🚫 Не подходит", f"c:skip:{contact.pk}:{index}"),
         ("💡 Подсказка", f"c:tip:{contact.pk}:{index}")],
        _navigation(index, total),
        [("⬅️ Назад", "c:home")],
    ]
    return Screen(clip("\n".join(lines)), [row for row in rows if row])


def _navigation(index: int, total: int) -> list[tuple[str, str]]:
    row = []
    if index > 0:
        row.append(("⬅️", f"c:ph:{index - 1}"))
    row.append((f"{index + 1}/{total}", "noop"))
    if index < total - 1:
        row.append(("➡️", f"c:ph:{index + 1}"))
    return row


def _pk_and_index(arg: str) -> tuple[int, str]:
    """«12:7» → (12, «7»). Без номера — начало списка."""
    raw_pk, _, index = arg.partition(":")
    return base._to_int(raw_pk), index or "0"


def ask_result(store: Store, arg: str) -> Screen:
    """Спрашивает, чем кончился звонок.

    Без итога отметка «позвонил» бесполезна: через неделю по такому списку
    не понять, кому перезванивать, а кого можно забыть.
    """
    pk, index = _pk_and_index(arg)
    contact = store.get_contact(pk)
    if contact is None:
        return phones(store, index)

    text = (
        f"<b>📞 {esc(contact.name)}</b>\n"
        f"{esc(contact.primary_phone or '')}\n\n"
        "Чем закончился разговор?"
    )
    rows = [
        [(label, f"c:res:{code}:{pk}:{index}")]
        for code, (label, _short, _stage) in RESULTS.items()
    ]
    rows.append([("⬅️ Назад к обзвону", f"c:ph:{index}")])
    return Screen(text, rows)


def save_result(store: Store, arg: str) -> Screen:
    """Записывает итог звонка и убирает карточку из очереди обзвона.

    Индекс не сдвигаем: под тем же номером окажется следующая компания, и
    список подтянется сам. Владелец жал «дальше» ровно один раз.
    """
    code, _, rest = arg.partition(":")
    pk, index = _pk_and_index(rest)
    contact = store.get_contact(pk)
    if contact is None or code not in RESULTS:
        return phones(store, index)

    _label, short, stage = RESULTS[code]
    contact.meta[CALLED_AT] = now_msk()
    contact.meta[CALL_RESULT] = short
    contact.meta[CALL_TRIES] = int(contact.meta.get(CALL_TRIES) or 0) + 1
    store.set_meta(pk, contact.meta)
    store.log_event(pk, "call", short)
    if stage is not None:
        store.set_stage(pk, stage, f"звонок: {short}")

    logger.info("Звонок «{}»: {}", contact.name, short)
    # Под тем же номером окажется следующая компания — владелец продолжает
    # с того места, где остановился.
    screen = phones(store, index)
    screen.alert = f"Записал: {short}"
    return screen


def called_list(store: Store, arg: str) -> Screen:
    """Кому уже звонили: дата, итог и сколько было попыток."""
    items = _called(store)
    if not items:
        return Screen(
            "<b>📋 Кому звонили</b>\n\nПока никому. Отметки появляются после кнопки «Позвонил».",
            [[("📞 К обзвону", "c:ph:0")], [("⬅️ Назад", "c:home")]],
        )

    offset = max(0, min(base._to_int(arg), (len(items) - 1) // PAGE * PAGE))
    page = items[offset : offset + PAGE]

    lines = [f"<b>📋 Кому звонили · {len(items)}</b>", ""]
    for contact in page:
        when = str(contact.meta.get(CALLED_AT, ""))[:16].replace("T", " ")
        result = esc(str(contact.meta.get(CALL_RESULT, "")))
        tries = int(contact.meta.get(CALL_TRIES) or 1)
        again = f" · попыток {tries}" if tries > 1 else ""
        lines.append(f"• <b>{esc(contact.name)}</b> — {result}{again}")
        lines.append(f"  {when} · {esc(contact.primary_phone or '')}")

    # Хвост «k<смещение>» — чтобы «назад» из карточки вернула на эту же
    # страницу списка звонков, а не в начало общей базы.
    rows = [[(contact.name[:28], f"c:card:{contact.pk}:k{offset}")] for contact in page]
    nav = []
    if offset:
        nav.append(("⬅️", f"c:calls:{max(0, offset - PAGE)}"))
    if offset + PAGE < len(items):
        nav.append(("➡️", f"c:calls:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([("📞 К обзвону", "c:ph:0"), ("⬅️ Назад", "c:home")])
    return Screen(clip("\n".join(lines)), rows)


# Через сколько дней возвращаться к компании, если владелец нажал «Перезвонить».
CALLBACK_DAYS = 1


def mark_later(store: Store, arg: str) -> Screen:
    """«Перезвонить» — это срок, а не заметка.

    Кнопка не откладывала ничего: срок нигде не сохранялся, компания
    оставалась первой в списке обзвона и всплывала сразу же. Хуже того,
    словом «перезвонить» затиралась заметка-зацепка, которую до этого
    посчитала кнопка «С чего начать» — то есть нажатие уносило работу.
    """
    from datetime import date, timedelta

    from ..store import today_msk

    pk, index = _pk_and_index(arg)
    contact = store.get_contact(pk)
    when = (date.fromisoformat(today_msk()) + timedelta(days=CALLBACK_DAYS)).isoformat()
    if contact is not None:
        contact.meta["remind_after"] = when
        contact.meta["remind_reason"] = CALLBACK_NOTE
        store.set_meta(pk, contact.meta)
    store.log_event(pk, "call", f"{CALLBACK_NOTE} {when}")
    screen = phones(store, index)
    screen.alert = f"Перезвоним {when} — из списка обзвона убрал"
    return screen


def mark_not_ours(store: Store, arg: str) -> Screen:
    """«Не подходит» прямо из обзвона: закрываем и остаёмся в списке.

    Раньше эта кнопка уводила в общую карточку рассылки, и владелец, разбирая
    список на обзвон, после каждой компании вылетал в чужой экран.
    """
    pk, index = _pk_and_index(arg)
    contact = store.get_contact(pk)
    store.set_stage(pk, Stage.DEAD, "не подходит (из обзвона)")
    screen = phones(store, index)
    screen.alert = f"Закрыл: {contact.name[:40]}" if contact else "Закрыл"
    return screen


def call_tip(store: Store, arg: str) -> Screen:
    """С чего начать разговор. Считается по собранным данным, без обращения к модели.

    Платить за подсказку не за что: всё, на чём она строится, парсер уже принёс.
    Жалобы из отзывов, отсутствие сайта и рейтинг говорят сами за себя, а
    выдумывать за компанию то, чего мы не знаем, нельзя.
    """
    pk, index = _pk_and_index(arg)
    contact = store.get_contact(pk)
    if contact is None:
        return phones(store, index)

    tip = _offline_tip(contact)
    store.set_note(pk, tip)
    logger.info("Подсказка к звонку «{}»: {}", contact.name, tip[:80])
    # Возвращаемся к той же карточке: подсказку заказывали для неё, и
    # показать её надо там же, а не на первой компании списка.
    screen = phones(store, index)
    screen.alert = "Подсказка готова"
    return screen


def _offline_tip(contact) -> str:
    """Зацепка без обращения к модели: по тому, что уже собрал парсер."""
    complaints = contact.meta.get("complaints") or []
    if complaints:
        return "В отзывах жалуются, что сложно дозвониться. С этого и заходить: обращения мимо телефона."
    if not contact.site and (contact.meta.get("has_site") or "").startswith("нет"):
        return "Сайта нет, только карточка на карте: заявки идут лишь в рабочие часы."
    if contact.meta.get("rating"):
        return (
            f"Рейтинг {contact.meta['rating']}, поток есть. "
            "Вопрос в том, куда падают заявки помимо звонков."
        )
    return "Данных мало. Начать стоит с вопроса, откуда сейчас приходят клиенты."


# --- Экран вкладки ----------------------------------------------------------

def home(store: Store) -> Screen:
    """К общему экрану добавляем обзвон: он есть только здесь."""
    screen = base.home(TAB, store)
    to_call = len(_to_call(store))
    called = len(_called(store))
    if to_call:
        # Строка и кнопка считают ОДИН И ТОТ ЖЕ список. Раньше строку писал
        # общий экран по своему счёту — «все контакты вкладки, кому нельзя
        # написать», включая тех, у кого нет и телефона, и тех, с кем разговор
        # давно идёт. На живой базе строка обещала 52, а кнопка открывала 31,
        # и понять, какая из двух цифр настоящая, было нельзя.
        screen.text += (
            f"\nТолько телефон, писать некуда: <b>{to_call}</b> — кнопка «Телефоны»"
        )
    row = [(f"📞 Телефоны · {to_call}", "c:ph:0")]
    if called:
        row.append((f"📋 Звонили · {called}", "c:calls:0"))
    screen.rows.insert(2, row)
    return screen


def health(store: Store) -> Health:
    return base.health(TAB, store)


def load() -> Section:
    return Section(
        code=TAB.code,
        title=TAB.title,
        icon=TAB.icon,
        handle=lambda store, action, arg: (
            home(store) if action in ("home", "") else handle(store, action, arg)
        ),
        health=health,
        commands=[
            ("clients", "Клиенты: компании с карт", "c:home"),
            ("phones", "Клиенты: список на обзвон", "c:ph:0"),
            ("called", "Клиенты: кому уже звонили", "c:calls:0"),
        ],
    )

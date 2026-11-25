"""Цепочка писем вдогонку тем, кто не ответил.

Одно письмо ловит только того, кто оказался у почты именно в тот день. У всех,
кто этим живёт, касаний несколько: заметная часть ответов приходит вовсе не на
первое письмо, а на второе и третье. Поэтому здесь не «письмо вдогонку», а
короткая цепочка — первое письмо плюс три касания, у каждого свой повод.

Чего в цепочке нет и не будет: повторов первого письма, обиды («вы не
ответили»), давления и бесконечности. Последнее касание прямо говорит, что оно
последнее, и на этом мы отстаём от человека навсегда.

Цепочка обрывается сразу, как только контакт ответил, отказался или письмо
вернулось: `due()` смотрит только на тех, кто остался на стадии «написали».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from . import mailcheck
from .models import Channel, Contact, Draft, Kind, Stage
from .store import MSK, Store, now_msk
from .ui import esc


@dataclass(frozen=True)
class Touch:
    """Одно касание цепочки.

    after_days — сколько ждём после предыдущего нашего письма, а не от начала:
    так пауза не съезжает, если первое письмо ушло с задержкой.
    angle — с чем идём в этот раз. Уходит в промт: без него модель на втором
    и третьем касании пишет одно и то же другими словами.
    """

    after_days: float
    angle: str
    last: bool = False


# Агентствам даём больше времени: решение о субподряде у них почти всегда
# проходит через нескольких человек. Компаниям с карт отвечает обычно один
# человек — там пауза короче.
CHAIN: dict[Kind, tuple[Touch, ...]] = {
    Kind.PARTNER: (
        Touch(3, "Совсем коротко: дошло ли письмо и есть ли смысл продолжать разговор. "
                 "Ничего нового не рассказываем."),
        Touch(5, "Один довод, которого не было в первом письме: как у нас устроен "
                 "субподряд — работаем под их брендом, с их клиентом напрямую не "
                 "общаемся, объём и сроки фиксируем письменно до старта."),
        Touch(9, "Спокойно закрываем тему: оставляем контакт на случай, когда "
                 "понадобятся руки под проект, и больше не пишем.", last=True),
    ),
    Kind.CLIENT: (
        Touch(2, "Совсем коротко: дошло ли письмо и актуальна ли тема. "
                 "Ничего нового не рассказываем."),
        Touch(4, "Один довод, которого не было в первом письме: что происходит "
                 "сейчас — человек ищет такую услугу, находит карточку без сайта "
                 "и уходит к тем, у кого посмотреть есть что."),
        Touch(8, "Спокойно закрываем тему: оставляем контакт на будущее и "
                 "больше не пишем.", last=True),
    ),
}

# Сколько касаний всего, считая первое письмо. Для сообщений владельцу.
def total_touches(kind: Kind) -> int:
    return len(CHAIN.get(kind, ())) + 1


# Пометки в карточке: сколько касаний вдогонку уже готовили и когда последнее.
TOUCH_KEY = "touch_step"
TOUCH_AT = "touch_at"
# Пометка прежней, одноразовой версии. У контактов, которых догоняли до
# цепочки, она означает ровно одно сделанное касание.
FOLLOWUP_KEY = "followup_at"

# Сколько писем готовим за один заход по каждому типу. Ограничение то же, что
# у обычной рассылки: пачка одинаковых писем за минуту выглядит как рассылка.
BATCH = 10

# После скольких дней в очереди касание считается протухшим и переписывается.
# Каждое касание цепочки написано про «недавно»: первое спрашивает, дошло ли
# письмо, второе добавляет довод к разговору, который человек ещё помнит.
# Пролежав месяц, такой текст уходит враньём — у нас ровно так и вышло, когда
# всё стояло из-за мёртвого прокси: письмо «писал вам недавно» уехало через
# сорок шесть дней после первого. Две недели — верхняя граница, за которой
# получатель прошлого письма уже точно не помнит.
STALE_DAYS = 14


def _days_since(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=MSK)
    return (datetime.now(MSK) - when).total_seconds() / 86400.0


def step_of(contact: Contact) -> int:
    """Сколько касаний вдогонку этому контакту уже сделали."""
    raw = contact.meta.get(TOUCH_KEY)
    if raw is None:
        return 1 if contact.meta.get(FOLLOWUP_KEY) else 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("Кривая отметка касания в карточке «{}»: {}", contact.name, raw)
        return 0


def next_touch(contact: Contact) -> Touch | None:
    """Какое касание следующее. None — цепочка кончилась."""
    chain = CHAIN.get(contact.kind, ())
    step = step_of(contact)
    return chain[step] if step < len(chain) else None


def _waited_days(store: Store, contact: Contact) -> float | None:
    """Сколько дней прошло с последнего нашего касания.

    Считаем и от подготовки письма, и от его настоящей отправки — берём
    меньшее. Одной подготовки мало: черновик мог неделю пролежать в очереди,
    пока владелец не нажал «Отправить», и если мерить только от подготовки,
    следующее касание готовится сразу после отправки предыдущего — человек
    получает два письма подряд, ровно та долбёжка, от которой цепочка и
    должна защищать. А одной отправки мало по обратной причине: пока черновик
    лежит неотправленным, наезжать на него следующим касанием тоже нельзя.
    """
    marks = [
        contact.meta.get(TOUCH_AT),
        contact.meta.get(FOLLOWUP_KEY),
        store.last_sent_at(contact.pk or 0),
    ]
    passed = [days for days in (_days_since(mark) for mark in marks) if days is not None]
    return min(passed) if passed else None


def drop_stale(store: Store, kind: Kind) -> list[str]:
    """Сносит из очереди письма, пролежавшие дольше STALE_DAYS.

    Возвращает имена контактов, у которых письмо снято, — их напишут заново.
    Два случая, и убирать их надо по-разному:

    Касание вдогонку (контакт на стадии «написали») — сносим, не трогая
    стадию, и откатываем шаг цепочки на одно назад. Шаг растёт от ПОДГОТОВКИ
    письма, а это письмо так и не ушло: не откатив, мы молча сожгли бы
    касание, и человек не получил бы ничего. Стадию не трогаем потому, что
    первое письмо ему уже уходило — вернув его в «новые», мы написали бы
    холодное первое письмо второй раз.

    Первое письмо — наоборот, возвращаем контакт в «новые». Текст опирается
    на то, что мы увидели на их сайте месяц назад, и переписать его должна
    обычная подготовка писем с нуля.
    """
    if kind not in CHAIN:
        return []

    dropped: list[str] = []
    for draft, contact in store.queue(kind, limit=10**9):
        if draft.pk is None or contact.pk is None:
            continue
        age = _days_since(draft.created_at)
        if age is None or age < STALE_DAYS:
            continue
        chain_letter = contact.stage is Stage.WRITTEN
        removed = (
            store.forget_draft(draft.pk) if chain_letter else store.drop_draft(draft.pk)
        )
        if not removed:
            # Письмо ушло прямо сейчас, между чтением очереди и удалением.
            # Так и надо: отправленное не сносим.
            continue
        if chain_letter:
            _step_back(store, contact)
        # Вид события свой, не "followup". Тот в сводке называется «писем
        # вдогонку», и снятые черновики подмешивались к отправленным письмам:
        # шесть выброшенных текстов читались как шесть ушедших.
        store.log_event(
            contact.pk, "stale",
            f"письмо пролежало в очереди {age:.0f} дней и протухло — напишем заново",
        )
        logger.info("«{}»: текст протух за {:.0f} дней, снял из очереди",
                    contact.name, age)
        dropped.append(contact.name)
    return dropped


def due(store: Store, kind: Kind) -> list[Contact]:
    """Кому пора следующее касание.

    Условия: письмо ушло, ответа не было, цепочка не кончилась и пауза выдержана.
    """
    if kind not in CHAIN:
        return []

    ready = []
    # Смотрим ВСЮ стадию, а не первые пятьсот записей. Выборка идёт по номеру
    # в базе, то есть от самых старых, и почти все они — с кончившейся
    # цепочкой: три касания сделаны, больше не пишем. Стоит таким накопиться
    # пятьсот, и окно занято ими целиком — цепочка молча умирает для всех, кому
    # написали позже. Дешевле от лимита не становилось: contacts() всё равно
    # читает стадию целиком и режет список уже в памяти.
    for contact in store.contacts(kind, Stage.WRITTEN, limit=10**9):
        if contact.pk is None or not contact.reachable:
            continue
        if store.draft_for(contact.pk) is not None:
            # Прошлое касание готово, но ещё не ушло — ждёт кнопки владельца.
            # Готовить поверх него следующее нельзя: счётчик касаний растёт от
            # подготовки, и цепочка из трёх писем сгорала целиком, не отправив
            # ни одного. Человек при этом не получал вообще ничего.
            continue
        touch = next_touch(contact)
        if touch is None:
            continue
        waited = _waited_days(store, contact)
        if waited is not None and waited >= touch.after_days:
            ready.append(contact)
    return ready


def write_one(store: Store, contact: Contact) -> tuple[bool, str]:
    """Готовит очередное касание и кладёт в очередь."""
    from . import llm

    if contact.pk is None:
        return False, "контакт не сохранён"
    touch = next_touch(contact)
    if touch is None:
        return False, "цепочка уже кончилась"

    # Адрес проверяем и здесь. Первое письмо проверку прошло, но с тех пор
    # прошли дни: домен мог закрыться. Без этой проверки цепочка касаний
    # обходила её стороной и через три дня писала туда, куда обычная
    # подготовка письма уже отказывалась.
    channel = contact.channel
    if channel is Channel.EMAIL:
        address, verdict = mailcheck.pick_address(store, contact)
        if address is None:
            if not verdict.permanent:
                return False, f"адрес не проверить: {verdict.reason}"
            if not contact.best_telegram:
                store.set_stage(contact.pk, Stage.DEAD, f"адрес не годится: {verdict.reason}"[:200])
                return False, f"адрес не годится: {verdict.reason}"
            channel = Channel.TELEGRAM

    step = step_of(contact) + 1
    letter = llm.write_letter(
        contact,
        channel,
        followup=llm.Followup(number=step, angle=touch.angle, last=touch.last),
        earlier=store.sent_letters(contact.pk),
    )
    if letter is None:
        return False, "модель не ответила"
    if not letter.decided:
        # Невнятный ответ — не отказ. Цепочку не рвём, вернёмся к ней позже.
        return False, "модель ответила непонятно"
    if not letter.write:
        # Модель решила, что писать не о чем. Цепочку закрываем целиком:
        # если довода нет сейчас, через неделю он не появится.
        _mark(store, contact, len(CHAIN.get(contact.kind, ())))
        return False, f"пропускаем: {letter.reason}"
    if not letter.body.strip():
        return False, "модель вернула пустой текст"

    store.save_draft(Draft(
        contact_pk=contact.pk,
        channel=channel,
        subject=letter.subject,
        body=letter.body,
    ))
    _mark(store, contact, step)
    store.log_event(
        contact.pk, "followup",
        f"касание {step + 1} из {total_touches(contact.kind)} готово",
    )
    return True, f"касание {step + 1} из {total_touches(contact.kind)}"


def _mark(store: Store, contact: Contact, step: int) -> None:
    contact.meta[TOUCH_KEY] = step
    contact.meta[TOUCH_AT] = now_msk()
    store.set_meta(contact.pk or 0, contact.meta)


def _step_back(store: Store, contact: Contact) -> None:
    """Возвращает цепочку на шаг назад, НЕ трогая время последнего касания.

    Отдельно от _mark ровно из-за времени. Снятое письмо так и не ушло, и
    часы ожидания идут по-прежнему от последнего настоящего касания. Поставь
    мы здесь «сейчас» — замена писалась бы только через положенную паузу
    после уборки, то есть молчание тянулось бы ещё дольше, хотя человек не
    получил от нас вообще ничего.
    """
    contact.meta[TOUCH_KEY] = max(0, step_of(contact) - 1)
    store.set_meta(contact.pk or 0, contact.meta)


def run(store: Store, *, limit: int = BATCH) -> str | None:
    """Готовит очередные касания по всем типам. Текст — владельцу, если есть что сказать."""
    from . import outreach

    prepared: list[str] = []
    prepared_channels: list = []
    for kind in CHAIN:
        # Сначала убираем протухшее, потом смотрим, кому пора. Порядок важен:
        # due() пропускает контакт, у которого письмо уже лежит в очереди, —
        # не сняв старое, мы бы никогда не написали новое.
        drop_stale(store, kind)
        for contact in due(store, kind)[:limit]:
            ok, reason = write_one(store, contact)
            logger.info("Вдогонку «{}»: {}", contact.name, reason)
            if ok:
                draft = store.draft_for(contact.pk or 0)
                if draft is not None:
                    prepared_channels.append(draft.channel)
                # Имена приходят из выгрузки парсера: в них попадаются «&» и
                # скобки, а сообщение уходит с разметкой HTML.
                prepared.append(f"· {esc(contact.name)} — {reason}")

    if not prepared:
        return None

    # Дальше письма живут по общим правилам: включена автоотправка — уйдут
    # сами с паузами, выключена — будут ждать кнопки.
    #
    # Про телеграм так говорить нельзя ни при каком положении тумблера: бот не
    # может написать человеку первым, и такие касания уходят только руками
    # владельца. Обещание «уйдут сами» означало, что он их просто не отправит:
    # ждал автоматики, а её там нет и не будет.
    from .models import Channel

    by_hand = sum(
        1 for name in prepared_channels if name is Channel.TELEGRAM
    )
    if by_hand == len(prepared):
        how = "телеграм отправляется вашими руками: откройте очередь"
    elif outreach.autosend_on(store) and outreach.channel_ready(Channel.EMAIL)[0]:
        how = "почта уйдёт сама" + (
            f", телеграм ({by_hand}) отправляете вы" if by_hand else ""
        )
    elif outreach.autosend_on(store):
        # Тумблер включён, а канал не настроен: не уйдёт ни одно письмо.
        # «Уйдёт сама» здесь значило бы, что владелец их просто не отправит —
        # ждал автоматики, а её нет. Ровно та же проверка, что на экране
        # настроек: ответы должны совпадать.
        why = outreach.channel_ready(Channel.EMAIL)[1]
        how = f"отправка включена, но не работает: {why} — письма ждут в очереди"
    else:
        how = "ждут кнопки «Отправить»" + (
            f", из них телеграм ({by_hand}) — вашими руками" if by_hand else ""
        )
    names = "\n".join(prepared[:10])
    tail = "" if len(prepared) <= 10 else f"\n…и ещё {len(prepared) - 10}"
    return (
        f"📮 <b>Письма вдогонку</b> — {len(prepared)}\n"
        f"Не ответили на прошлое письмо, идём следующим касанием.\n\n{names}{tail}\n\n{how}."
    )


def days_left(store: Store, contact: Contact) -> float | None:
    """Сколько дней осталось до следующего касания. Для карточки в боте."""
    touch = next_touch(contact)
    if touch is None or contact.pk is None:
        return None
    waited = _waited_days(store, contact)
    if waited is None:
        return None
    return max(0.0, touch.after_days - waited)

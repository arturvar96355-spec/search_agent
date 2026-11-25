"""Слежение за ответами клиентов.

Задача простая: как только заказчик что-то написал по заказу, где у нас стоит
отклик, владелец должен получить в Telegram «клиент написал по заказу N,
проверьте личный кабинет». Ссылку тоже прикладываем — открыть быстрее, чем искать.

Как это работает: по каждому заказу с отправленным откликом открывается
страница, и на ней СЧИТАЮТСЯ СООБЩЕНИЯ — по отметкам времени. Стало больше,
чем в прошлый раз, — значит кто-то написал. Первый проход только запоминает
число и молчит, иначе на старте прилетело бы уведомление по каждому старому
отклику.

Хэша здесь больше нет, хотя когда-то был именно он. Хэш всей страницы скакал
между загрузками — реклама, счётчики, подгрузка блоков, — и давал ложное
«клиент написал» на пустом месте. Количество сообщений меняется только когда
кто-то действительно написал.

Почему не IMAP: он требует отдельного ящика и пароля приложения, а сессии бирж
уже есть. Когда доступы к почте появятся, добавим её вторым источником — тогда
уведомления будут приходить и о том, что биржа прислала письмом.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from . import notify
from .models import Order, Source
from .store import Store, as_utc

# Ключ в таблице state, под которым лежит отпечаток переписки по заказу.
STATE_PREFIX = "convo"

# Сколько ждём стабилизации страницы.
SETTLE_TIMEOUT_MS = 25000
# Попыток открыть страницу. Мобильный прокси меняет IP каждые 5 минут,
# и в момент смены туннель рвётся с ERR_TUNNEL_CONNECTION_FAILED.
NAV_ATTEMPTS = 3
NAV_RETRY_PAUSE_MS = 6000
# Сколько переписок подряд должны не прочитаться, чтобы бросить площадку до
# следующего прохода. Одна не читается по своей причине: заказ снят, страница
# удалена. Три подряд — это уже не заказы, а площадка: слетевший вход, бан
# прокси, редизайн. Дальше каждая попытка стоит по полминуты (три захода с
# паузами), и на десятке заказов проход стоит пять минут — всё это время
# вторая площадка ждёт своей очереди с браузером, а на биржах решает скорость.
DEAD_STREAK = 3

@dataclass
class ReplyCheck:
    checked: int = 0
    new_replies: int = 0
    # Сколько ЗАКАЗОВ не прочиталось. Считаем отдельно от errors: там кроме
    # заказов бывает и строка про всю площадку («подряд не прочиталось три»),
    # а тревога о слепоте называет владельцу именно число заказов.
    missed: int = 0
    errors: list[str] | None = None


# Отметка отдельного сообщения в переписке: дата или «Сегодня/Вчера» рядом со временем.
_MESSAGE_MARK = re.compile(
    r"(\d{2}\.\d{2}\.\d{4}\s*в\s*\d{1,2}:\d{2}"
    r"|\bСегодня\b|\bВчера\b"
    r"|\d{1,2}\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*)",
    re.IGNORECASE,
)


# Приписка, которую Profi.ru сама добавляет под наш отклик: цена из анкеты и
# контакты специалиста. Отдельным сообщением она не является, но время у неё
# своё, и по отметкам времени она неотличима от ответа клиента.
# Вместе с ней вырезаем и её отметку времени строкой выше: без этого отметка
# осиротеет и всё равно посчитается за отдельное сообщение.
#
# Каждая строка приписки должна совпасть ЦЕЛИКОМ, от края до края. Иначе под
# шаблон попадает живое сообщение клиента: «Стоимость от 50 000 до 80 000 руб.
# / услугу — согласны?» и «Стоимость от 50 000 до 80 000 руб.\nКонтакты:
# телеграм @ivan» вырезались вместе со своими отметками времени. Счётчик не
# рос, и ответ заказчика мы просто не замечали — то есть человек ждал ответа,
# а мы про него не знали.
#
# Отличают приписку две вещи, и обе строгие: единица измерения из анкеты —
# именно «усл.» с точкой и концом строки сразу за ней (клиент пишет «услугу»,
# «услуга», «за услугу»), и слово «Контакты:» отдельной строкой, за которым
# ничего нет (клиент после двоеточия всегда что-то пишет). Хотя бы одна из
# двух примет обязательна.
# Снято с живой страницы 26.08.2026: «Стоимость от 100000 до 160000 руб. / усл.»
# и следом «Контакты:» с телефоном.
_PRICE_LINE = r"[ \t]*Стоимость\s+от\s+[\d\s\u00a0\u202f]+до\s+[\d\s\u00a0\u202f]*руб\.?"
_CONTACTS_TAIL = r"(?:\n[ \t]*Контакты\s*:[ \t]*$(?:\n[ \t]*\+?[\d\s()\-]{7,}[ \t]*$)?)"
_PLATFORM_FOOTER = re.compile(
    r"(?:^[^\n]*(?:Сегодня|Вчера|\d{2}\.\d{2}\.\d{4}\s*в\s*\d{1,2}:\d{2}|\d{1,2}:\d{2})[^\n]*\n)?"
    + _PRICE_LINE
    + r"(?:"
    + r"[ \t]*/[ \t]*усл\.[ \t]*$" + _CONTACTS_TAIL + r"?"
    + r"|[ \t]*$" + _CONTACTS_TAIL
    + r")",
    re.IGNORECASE | re.MULTILINE,
)


def count_messages(conversation_text: str) -> int:
    """Сколько сообщений в переписке.

    Считаем отметки времени, а не хэшируем текст: хэш всей страницы скакал
    между загрузками (реклама, счётчики, подгрузка блоков) и давал ложное
    «клиент написал». Количество сообщений меняется только когда кто-то
    действительно написал.

    Приписку самой площадки («Стоимость от … Контакты: …») вырезаем: её
    Profi.ru добавляет под наш же отклик, а по отметке времени она выглядит
    ровно как ответ клиента — владелец получал уведомления на пустом месте.
    """
    text = _PLATFORM_FOOTER.sub("", conversation_text or "")
    return len(_MESSAGE_MARK.findall(text))


def _now_utc() -> str:
    """Время проверки машинной строкой: по ней сортируется круговая очередь."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_key(order: Order) -> str:
    return f"{STATE_PREFIX}:{order.source.value}:{order.ext_id}"


def read_stable(page, order: Order) -> str | None:
    """Открывает заказ и снимает переписку, переживая обрыв прокси.

    None — прочитать не удалось. Это важно отличать от «переписка не изменилась»:
    обрыв туннеля при ротации IP не должен трактоваться ни как ответ клиента,
    ни как его отсутствие.
    """
    for attempt in range(1, NAV_ATTEMPTS + 1):
        try:
            page.goto(order.url, wait_until="domcontentloaded")
            _settle(page)
            text = extract_conversation(page, order)
            if text.strip():
                return text
            logger.debug("[{}] попытка {}: пустая переписка", order.key, attempt)
        except Exception as exc:  # noqa: BLE001 — обрыв прокси между попытками ожидаем
            logger.debug("[{}] попытка {} не удалась: {}", order.key, attempt, exc)
        if attempt < NAV_ATTEMPTS:
            page.wait_for_timeout(NAV_RETRY_PAUSE_MS)
    return None


def platform_is_dead(misses: int, order: Order) -> bool:
    """Столько непрочитанных переписок подряд — это площадка, а не заказы.

    Один на оба прохода по страницам (ответы и напоминания): порог, слова и
    решение должны быть одни и те же, иначе один проход бросает мёртвую
    площадку, а второй продолжает стучаться в неё те же пять минут.
    """
    if misses < DEAD_STREAK:
        return False
    logger.error(
        "[{}] подряд не прочиталось переписок: {} — бросаю площадку до следующего прохода",
        order.source.value, misses,
    )
    return True


def _settle(page) -> None:
    """Ждёт, пока страница перестанет дорисовываться.

    wait_until="load" здесь не годится: через мобильный прокси событие load
    не наступает за 30 секунд из-за аналитики и долгих соединений, и текст
    снимался с недогруженной страницы — отпечаток скакал между проходами
    и давал ложное «клиент написал».
    """
    page.wait_for_function(
        "() => (document.body.innerText || '').length > 400", timeout=SETTLE_TIMEOUT_MS
    )
    # Ждём, пока объём текста перестанет расти: два одинаковых замера подряд.
    page.wait_for_function(
        """() => {
            const now = (document.body.innerText || '').length;
            const prev = window.__fbidLen;
            window.__fbidLen = now;
            return prev === now;
        }""",
        timeout=SETTLE_TIMEOUT_MS,
        polling=700,
    )


# Где на странице начинается переписка. Хэшировать страницу целиком нельзя:
# на ней меняется число откликов других фрилансеров, рейтинги и баннеры —
# и владельцу прилетало «клиент написал», когда никто ничего не писал.
CONVERSATION_START = {
    Source.FL: ("Ваш отклик",),
    # Только «Детали заказа». Запасной маркер по нашему собственному тексту
    # («Здравствуйте») обрезал регион выше отметки времени, счётчик сообщений
    # падал до нуля, и реальный ответ клиента терялся.
    Source.PROFI: ("Детали заказа",),
}
CONVERSATION_END = (
    "Похожие заказы",
    "Профи.ру для специалистов",
    "Другие заказы",
    "© FL.ru",
    "Посмотреть другие заказы",
)


def extract_conversation(page, order: Order) -> str:
    """Только блок переписки со страницы заказа. Пусто — прочитать не удалось."""
    try:
        body = page.inner_text("body")
    except Exception as exc:  # noqa: BLE001 — не прочитали страницу — считаем переписку пустой
        logger.warning("[{}] не прочитал страницу: {}", order.key, exc)
        return ""

    start = -1
    for marker in CONVERSATION_START.get(order.source, ()):
        start = body.find(marker)
        if start >= 0:
            break
    if start < 0:
        # Блока переписки нет — отклик мог не сохраниться или вёрстка изменилась.
        return ""
    body = body[start:]

    for stop in CONVERSATION_END:
        index = body.find(stop)
        if index > 0:
            body = body[:index]
    return body


# --- Кто что написал --------------------------------------------------------

# Биржа раскладывает сообщения по группам отправителей: у всех наших сообщений
# один и тот же data-message-group-id, у клиента — свой. Это единственный
# надёжный признак: по тексту наше сообщение от клиентского не отличить, и
# владелец, написавший клиенту руками, приезжал в систему как «клиент ответил».
_MESSAGES_JS = """() => {
    const nodes = [...document.querySelectorAll('[data-message-content-id]')];
    return nodes.map(node => ({
        group: node.closest('[data-message-group-id]')
            ?.getAttribute('data-message-group-id') || '',
        text: (node.innerText || '').replace(/\\s+/g, ' ').trim(),
    })).filter(item => item.text);
}"""

# Сколько знаков нашего отклика ищем в переписке, чтобы опознать свою группу.
PROBE_CHARS = 40


@dataclass(frozen=True)
class Line:
    """Одно сообщение переписки с понятным автором."""

    ours: bool
    text: str


def read_messages(page) -> list[dict]:
    """Сообщения переписки с идентификатором автора. Пусто — разметка другая."""
    try:
        return page.evaluate(_MESSAGES_JS)
    except Exception as exc:  # noqa: BLE001 — разметка сменилась, не повод падать
        logger.debug("сообщения по разметке не разобрались: {}", exc)
        return []


def label_messages(messages: list[dict], own_text: str = "") -> list[Line]:
    """Помечает, где мы, а где клиент.

    Свою группу узнаём по тексту отклика, а если он не нашёлся — по первому
    сообщению: переписку на бирже всегда начинаем мы, клиенту просто некуда
    написать раньше.
    """
    if not messages:
        return []

    probe = " ".join((own_text or "").split())[:PROBE_CHARS]
    ours = None
    if probe:
        ours = next((m["group"] for m in messages if probe in m["text"]), None)
    if ours is None:
        ours = messages[0]["group"]
    return [Line(m["group"] == ours, m["text"]) for m in messages]


def transcript(lines: list[Line]) -> str:
    """Переписка с подписями — то, что видит модель."""
    return "\n\n".join(f"{'Мы' if line.ours else 'Клиент'}: {line.text}" for line in lines)


def last_client_message(lines: list[Line]) -> str:
    """Последнее сообщение клиента дословно. Пусто — не разобрали.

    Владельцу в карточку нужен сам текст: по нему видно, бежать отвечать
    сейчас или после обеда. Пересказ модели одной строкой этого не заменяет.
    """
    for line in reversed(lines or []):
        if not line.ours and line.text.strip():
            return line.text.strip()
    return ""


def client_wrote_last(lines: list[Line]) -> bool:
    """Последнее слово за клиентом. Иначе отвечать не на что."""
    return bool(lines) and not lines[-1].ours


def check_replies(store: Store, context, orders: list[tuple[Order, int]]) -> ReplyCheck:
    """Обходит заказы с нашими откликами и уведомляет о новых сообщениях."""
    stats = ReplyCheck(errors=[])
    page = context.pages[0] if context.pages else context.new_page()

    misses = 0
    for order, _order_pk in orders:
        # Отметка о заходе — до чтения страницы, а не после. Именно она двигает
        # заказ в конец круговой очереди. Раньше она ставилась только у
        # прочитавшихся: заказ, чья страница не открывается (снят с биржи,
        # закрыт заказчиком, отдаётся с ошибкой), навсегда оставался первым и
        # съедал места в проходе — за ним живые переписки не проверялись вовсе.
        # Ничего не теряется: на следующем полном обороте очередь дойдёт до
        # него снова.
        store.set_state(f"{CHECKED_PREFIX}:{order.source.value}:{order.ext_id}", _now_utc())

        text = read_stable(page, order)
        if text is None:
            # Молчаливый пропуск здесь опаснее всего: если страница стабильно
            # не читается, ответ клиента мы просто никогда не увидим.
            logger.warning(
                "[{}] переписка не прочиталась за {} попыток — пропускаю",
                order.key,
                NAV_ATTEMPTS,
            )
            stats.errors.append(f"{order.key}: страница не прочиталась")
            stats.missed += 1
            misses += 1
            if platform_is_dead(misses, order):
                # Не «эти заказы плохие», а «площадка не отвечает». Остальные
                # переписки не трогаем: их отметки о заходе не переставлены,
                # и следующий проход начнёт ровно с них.
                stats.errors.append(
                    f"{order.source.value}: подряд не прочиталось {misses} переписок — "
                    f"похоже, слетел вход или забанен прокси"
                )
                break
            continue

        stats.checked += 1
        misses = 0
        own_text = store.bid_text(_order_pk)
        lines = label_messages(read_messages(page), own_text)
        _warn_if_bid_missing(store, order, text, own_text, lines)
        key = _state_key(order)
        messages = count_messages(text)
        previous_raw = store.get_state(key)

        if previous_raw is None:
            # Первый проход по заказу: запоминаем и молчим.
            store.set_state(key, str(messages))
            continue

        try:
            previous = int(previous_raw)
        except ValueError:
            # В базе остался старый формат (хэш) — просто переписываем на счётчик.
            store.set_state(key, str(messages))
            continue

        if messages <= previous:
            # Сообщений стало МЕНЬШЕ. Само по себе это невозможно: переписка не
            # укорачивается. Значит мы прочитали страницу хуже прежнего —
            # открылась заглушка прокси, вход, кусок вёрстки.
            #
            # Понижать отметку в такой момент нельзя, и это не осторожность, а
            # прямое следствие: после нуля любое нормальное чтение даёт «стало
            # на три больше» — то есть ложное «клиент написал», карточку
            # владельцу и платный разбор переписки моделью. На живой базе так и
            # вышло: у четырёх заказов отметка сбилась в ноль, пока прокси
            # лежал. Понижаем, только если страница разобралась по разметке
            # (lines непусто) — тогда меньшему числу можно верить.
            if messages < previous:
                if lines:
                    store.set_state(key, str(messages))
                else:
                    logger.info(
                        "[{}] сообщений насчиталось меньше прежнего ({} было {}) — "
                        "страница прочиталась плохо, отметку не трогаю",
                        order.key, messages, previous,
                    )
            continue

        store.set_state(key, str(messages))

        # Созвон ищем ДО развилки «кто писал последним». Договорённость
        # складывается из двух реплик, и закрывающей бывает как клиентская
        # («давайте в 18»), так и наша («хорошо, в 18 наберу»). Проверяй мы
        # только приход клиента — половина договорённостей прошла бы мимо.
        _tell_about_meeting(store, order, transcript(lines) if lines else text)

        if lines and not client_wrote_last(lines):
            # Последним писали мы. Чаще всего это владелец написал клиенту
            # руками: сообщений на странице стало больше, но отвечать не на что.
            # Раньше система принимала такое за ответ клиента и двигала заказ
            # в «Начали общение», а модель отвечала на собственную реплику.
            logger.info("[{}] прибавилось наше сообщение, не клиентское", order.key)
            continue

        stats.new_replies += 1
        logger.info(
            "[{}] новых сообщений: {} (было {}, стало {})",
            order.key,
            messages - previous,
            previous,
            messages,
        )
        try:
            delivered = _prepare_answer(
                store, order, _order_pk, transcript(lines) if lines else text,
                said=last_client_message(lines),
            )
        except Exception as exc:  # noqa: BLE001 — один заказ не рушит обход
            logger.warning("[{}] ответ клиента не обработан: {}", order.key, exc)
            delivered = False
        if not delivered:
            # Владелец так и не узнал, что клиент написал, — а счётчик уже
            # переписан, и следующий проход увидит «было столько же, стало
            # столько же». Откатываем, иначе сообщение живого клиента пропадёт
            # совсем. Важно: notify.send сбой Телеграма не бросает, а
            # возвращает False — ловить тут только исключения было бы слепотой
            # ровно в том месте, ради которого откат и писался.
            store.set_state(key, str(previous))
            logger.warning(
                "[{}] владелец не уведомлён — вернул счётчик, проверю на следующем",
                order.key,
            )

    # Площадку берём из самих заказов: у всех в пачке она одна.
    _warn_if_blind(store, stats, orders[0][0].source if orders else None)
    return stats


def _tell_about_meeting(store: Store, order: Order, conversation: str) -> None:
    """Смотрит, не договорились ли о созвоне, и говорит владельцу когда.

    Падать здесь нельзя ни при каких обстоятельствах: это довесок к обходу
    переписок, и уронить из-за него проверку ответов клиентов — плохая
    сделка. Всё, что пошло не так, уходит в лог.
    """
    from . import meeting

    try:
        # known — про созвон по этому заказу уже говорили. Тогда дешёвый отсев
        # пропускает и переписку без слова «созвон»: переносят его словами
        # «давайте лучше в 19:30», и иначе перенос прошёл бы мимо.
        told = store.get_state(meeting.told_key(order))
        found = meeting.find(order, conversation, known=bool(told))
        if found is None or meeting.already_told(store, order, found):
            return
        if notify.send(notify.meeting_card(
            order, found.line, moved=bool(told), confirmed=found.confirmed,
        )):
            meeting.remember(store, order, found)
        else:
            # Не записываем — значит на следующем проходе попробуем снова.
            # Созвон это то, ради чего всё остальное и делается: потерять его
            # из-за одной сетевой заминки нельзя.
            logger.warning("[{}] про созвон не сказали — повторю на следующем проходе",
                           order.key)
    except Exception as exc:  # noqa: BLE001 — довесок не рушит обход переписок
        logger.warning("[{}] разбор созвона не удался: {}", order.key, exc)


def _prepare_answer(
    store: Store, order: Order, order_pk: int, conversation: str, *, said: str = ""
) -> bool:
    """Что делать с ответом клиента: ответить самим или позвать владельца.

    Развилка по деньгам (правило владельца от 23.08.2026): заказ дешевле
    порога модель ведёт сама, дороже порога и по договорной цене — только
    показывает владельцу. Обе ветки ниже.

    Возвращает, дошло ли уведомление (или что уведомлять осознанно нечего):
    по False вызывающий код откатывает счётчик сообщений, чтобы вернуться к
    этому ответу на следующем проходе.

    Кто решает, есть ли вообще ответ клиента, — модель, а не счётчик сообщений.
    Счётчик растёт и от приписки самой площадки, и от сообщений владельца, если
    он написал клиенту руками. Модель видит переписку целиком и понимает, кто
    писал последним.

    Если модель не справилась совсем (упал API, не разобрала JSON), владелец
    всё равно получает «клиент написал» и идёт смотреть: молчать в этом месте
    нельзя, иначе ответ клиента останется незамеченным.
    """
    from . import dialog
    from .bidder import paused

    if paused(store):
        # Стоп-кран обещает владельцу: «софт больше никому не пишет и не
        # тратит деньги». Он гасил только отклики, а переписка с живым
        # клиентом шла как ни в чём не бывало — и модель писала от имени
        # владельца ровно после того, как он нажал «Остановить». Карточку
        # владельцу отдаём: клиент-то написал, и знать об этом надо.
        logger.info("[{}] стоп-кран включён — отвечаю только карточкой владельцу", order.key)
        dialog.mark_client_replied(store, order)
        return notify.send(notify.client_replied_card(order, said=said))

    try:
        exchanges = dialog.exchanges_done(store, order)
        answer = dialog.next_message(order, conversation, exchanges, nudge=0)
    except Exception as exc:  # noqa: BLE001 — сбой модели не должен глушить уведомление
        logger.warning("[{}] переписка не разобралась: {}", order.key, exc)
        answer = None

    if answer is None:
        # Модель не ответила — но мы всё равно говорим владельцу «клиент
        # написал». Значит и в воронке это должно быть ответом клиента:
        # отметку ставила только ветка с разобранным ответом, и при молчащей
        # модели ответ пропадал из отчёта совсем, а напоминания продолжали
        # ходить к человеку, который нам уже написал. Что сказали владельцу и
        # что показываем в цифрах — должно совпадать.
        dialog.mark_client_replied(store, order)
        return notify.send(notify.client_replied_card(order, said=said))

    if answer.action == dialog.ACT_SILENT:
        # Молчим совсем, и это осознанно. Счётчик сообщений на странице растёт
        # не только от ответов клиента: Profi.ru сама дописывает под наш отклик
        # блок «Стоимость от … Контакты: …», а владелец мог написать клиенту
        # руками. Модель это отличает — раз она говорит, что нового сообщения
        # нет, беспокоить владельца незачем.
        logger.info("[{}] нового сообщения от клиента нет: {}", order.key, answer.reason)
        return True  # осознанное молчание — это успех, счётчик откатывать не надо

    if answer.action == dialog.ACT_CLOSED:
        # Клиент ответил и закрыл тему: отказал или сказал «спасибо, подумаю».
        # Это не молчание, а результат — и молчать о нём нельзя. Раньше оба
        # случая приходили одним словом «молчать»: отказ исчезал в логе,
        # владелец о нём не узнавал, заказ навсегда оставался в «Начали
        # общение», а напоминания продолжали ходить к человеку, сказавшему
        # «нет». Отметка об ответе клиента их и прекращает.
        logger.info("[{}] разговор окончен: {}", order.key, answer.reason)
        dialog.mark_client_replied(store, order)
        return notify.send(notify.conversation_closed_card(
            order, reason=answer.reason, said=said
        ))

    # Модель ответила по существу — значит клиент действительно написал.
    # Именно эта отметка двигает заказ во вкладку «Начали общение».
    dialog.mark_client_replied(store, order)
    if answer.ready_for_call:
        # Отсюда Orbit берёт вкладку «Готовы к созвону».
        dialog.mark_ready_for_call(store, order)

    if answer.needs_human:
        # Клиент дошёл до созвона, договора или торга. Заголовок другой, но
        # действие то же: зовём владельца.
        #
        # И передаём разговор насовсем. Раньше это была только карточка: на
        # следующее сообщение клиента модель отвечала снова — поверх того, что
        # владелец успел написать сам, — и клиент видел в одной переписке двух
        # разных собеседников.
        dialog.mark_owner_leads(store, order, answer.reason or "модель передала разговор")
        return notify.send(notify.handover_card(
            order, answer.reason, answer.client_asked, ready_for_call=answer.ready_for_call
        ))

    # Развилка по деньгам. Дешёвый заказ модель доводит сама, дорогой — только
    # показывает владельцу: цена неудачной формулировки на крупной сумме выше
    # любой экономии времени, а договорную цену считаем дорогой (не знаем —
    # значит молчим).
    if dialog.answers_itself(store, order):
        problem = dialog.check_reply_rules(answer.reply, order)
        if problem:
            logger.warning("[{}] ответ не прошёл проверку: {}", order.key, problem)
            return notify.send(notify.client_replied_card(order, said=said))
        dialog.save_draft(store, order_pk, answer)
        dialog.approve(store, order_pk)
        # Дальше возвращаем True при любом исходе уведомления, и это важно.
        # False здесь означает «откати счётчик сообщений и разберись заново на
        # следующем проходе» — а ответ клиенту уже поставлен в отправку. Второй
        # разбор той же переписки мог бы поставить в отправку второй ответ, и
        # клиент получил бы два сообщения на один вопрос. Непришедшая карточка
        # владельцу — беда меньшая, и о ней остаётся запись в журнале.
        if not notify.send(notify.answer_sending_card(order, answer, said=said)):
            logger.error(
                "[{}] ответ клиенту поставлен в отправку, но карточка владельцу не дошла",
                order.key,
            )
        return True

    if dialog.expensive_for_auto(store, order):
        # Именно дорогой заказ — карточка про деньги. Остальные причины
        # (старая переписка, выключенный порог) идут ниже обычной карточкой:
        # называть их «крупным клиентом» значит врать владельцу.
        return notify.send(notify.expensive_client_card(
            order, asked=answer.client_asked, said=said,
            ready_for_call=answer.ready_for_call,
        ))

    return notify.send(notify.client_replied_card(
        order, asked=answer.client_asked, ready_for_call=answer.ready_for_call, said=said
    ))


def nudge_silent_clients(store: Store, context, orders: list[tuple[Order, int]]) -> int:
    """Напоминает о себе там, где клиент молчит.

    Два касания на заказ: первое почти через сутки после отклика, второе —
    ещё через сутки. Третьего нет: дальше это назойливость.
    """
    from . import dialog
    from .bidder import paused

    if paused(store):
        # Напоминание — это сообщение клиенту от имени владельца, а стоп-кран
        # обещает, что таких больше не будет. Молчим целиком: тут даже
        # карточку слать не о чем, клиент ничего не писал.
        logger.info("Стоп-кран включён — напоминания молчунам не шлю")
        return 0

    page = context.pages[0] if context.pages else context.new_page()
    sent = 0
    misses = 0
    for order, order_pk in orders:
        if not dialog.nudge_due(store, order):
            continue
        number = dialog.nudges_sent(store, order) + 1
        text = read_stable(page, order)
        if text is None:
            logger.warning("[{}] переписка не прочиталась — напоминание отложено", order.key)
            misses += 1
            if platform_is_dead(misses, order):
                break
            continue
        misses = 0
        lines = label_messages(read_messages(page), store.bid_text(order_pk))
        if lines and client_wrote_last(lines):
            # Клиент всё-таки ответил, просто мы этого ещё не разобрали.
            # Напоминать человеку, который написал сам, — худшее, что можно.
            #
            # Отмечаем именно ОТВЕТ, а не касание. Здесь стояло mark_nudged, и
            # оно сжигало напоминание вхолостую: два таких случая — и обоих
            # касаний больше нет, а если клиент потом замолчит, напомнить ему
            # уже нечем. Отметка об ответе прекращает напоминания по верной
            # причине и заодно доносит ответ до воронки.
            logger.info("[{}] клиент ответил — напоминание отменяю", order.key)
            dialog.mark_client_replied(store, order)
            continue

        # Дешёвый заказ модель ведёт сама — значит и напоминание пишет сама:
        # молчащий клиент это та же переписка, просто следующий её ход.
        # Дорогой и договорной остаётся владельцу, по тому же правилу, что и
        # ответы: цена неудачной формулировки на крупной сумме выше экономии
        # времени.
        if dialog.answers_itself(store, order):
            if _nudge_by_model(store, order, order_pk, lines, number):
                sent += 1
                continue
            logger.info("[{}] напоминание сама не написала — зову владельца", order.key)

        # Владельцу — карточка «клиент молчит» со ссылкой на чат: текст пишет он.
        #
        # Касание засчитываем только после доставленной карточки. Отметка до
        # отправки — это оплаченный чек за неоказанную услугу: карточка не
        # дошла (Телеграм лёг), а часы второго касания уже пошли от первого,
        # которого владелец не видел.
        hours = dialog.hours_since_our_message(store, order)
        delivered = notify.send(
            notify.silent_client_card(
                order, number,
                placed=store.bid_time(order_pk),
                days_silent=int(hours // 24) if hours else 0,
            ),
            markup={"inline_keyboard": [[{"text": "🔗 Открыть чат", "url": order.url}]]},
        )
        if delivered:
            dialog.mark_nudged(store, order)
            sent += 1
        else:
            logger.warning("[{}] карточка «клиент молчит» не дошла — попробую позже", order.key)
    return sent


def _nudge_by_model(
    store: Store, order: Order, order_pk: int, lines: list[Line], number: int
) -> bool:
    """Просит модель написать напоминание и ставит его в отправку.

    False — не вышло: тогда вызывающий зовёт владельца обычной карточкой.
    Молчание модели («клиент уже отказал», «заказ закрыт») — это тоже False,
    но касание при этом засчитываем: писать туда больше нечего, а держать
    заказ в очереди напоминаний значит возвращаться к нему каждый проход.
    """
    from . import dialog

    try:
        answer = dialog.next_message(
            order, transcript(lines) if lines else "", dialog.exchanges_done(store, order),
            nudge=number,
        )
    except Exception as exc:  # noqa: BLE001 — сбой модели не должен гасить напоминание
        logger.warning("[{}] напоминание не написалось: {}", order.key, exc)
        return False

    if answer is None:
        return False
    if answer.action == dialog.ACT_SILENT:
        logger.info("[{}] напоминать не о чем: {}", order.key, answer.reason)
        dialog.mark_nudged(store, order)
        return True

    problem = dialog.check_reply_rules(answer.reply, order)
    if problem:
        logger.warning("[{}] напоминание не прошло проверку правил: {}", order.key, problem)
        return False

    dialog.save_draft(store, order_pk, answer, nudge=True)
    if not dialog.approve(store, order_pk):
        return False
    # Касание засчитываем здесь, а не после доставки: разрешение уже лежит в
    # базе, и отправкой займётся сборщик. Не отметить — значит на следующем
    # проходе написать клиенту второе напоминание вдогонку первому.
    dialog.mark_nudged(store, order)
    notify.send(notify.answer_sending_card(order, answer, nudge=True))
    return True


def bid_is_on_the_page(page_text: str, own_text: str, lines: list[Line]) -> bool:
    """Стоит ли наш отклик на странице заказа.

    Проверка нужна каждый проход, а не только при отправке: биржа снимает
    отклики (заказ ушёл в архив, кончился пакет, модерация), и тогда система
    считает, что мы в игре, а нас там нет.
    """
    if any(line.ours for line in lines):
        return True
    probe = " ".join((own_text or "").split())[:PROBE_CHARS]
    if not probe:
        # Текста отклика нет — судить не по чему, тревогу не поднимаем.
        return True
    return probe in " ".join((page_text or "").split())


def _warn_if_bid_missing(
    store: Store, order: Order, page_text: str, own_text: str, lines: list[Line]
) -> None:
    """Только пишет в журнал: владельцу об этом сообщать не надо.

    Решение владельца от 09.08.2026. Отклик пропадает со страницы сплошь и
    рядом по причинам, на которые всё равно не повлиять: заказ забрал другой
    исполнитель, заказчик его снял, биржа отправила в архив. Тревога по
    такому поводу — шум, а шум приучает не читать уведомления вообще, и тогда
    незамеченным пройдёт важное. В журнале запись остаётся: если понадобится
    разобраться, почему отклик не сработал, она будет под рукой.
    """
    if bid_is_on_the_page(page_text, own_text, lines):
        return
    if not lines:
        # Страница открылась, но разобрать переписку не вышло. Это слепота, а
        # не пропажа: про неё отдельно предупреждает _warn_if_blind.
        return
    logger.info("[{}] отклика на странице не видно — заказ, похоже, ушёл", order.key)


# Ключ, под которым помним, когда в последний раз жаловались на слепоту.
# ПО КАЖДОЙ ПЛОЩАДКЕ отдельно: ключ был один на всех, и жалоба про FL глушила
# жалобу про Profi на половину суток. Площадки ломаются порознь — вёрстка
# едет у одной, сессия слетает у другой, — и вторая беда просто не доезжала
# до владельца.
BLIND_ALERT_PREFIX = "replies:blind_alert"
# Раз в сколько часов повторять жалобу, если ничего не изменилось.
BLIND_ALERT_HOURS = 12


def blind_alert_key(source: Source | None) -> str:
    return f"{BLIND_ALERT_PREFIX}:{source.value}" if source else BLIND_ALERT_PREFIX


def _warn_if_blind(store: Store, stats: ReplyCheck, source: Source | None = None) -> None:
    """Предупреждает, если переписки перестали читаться.

    Самый опасный сценарий этого модуля — тихий отказ: вёрстка биржи поехала,
    страницы не читаются, и владелец просто перестаёт узнавать, что клиент
    написал. Внешне это неотличимо от «клиенты не пишут».
    """
    failed = stats.missed
    # Тревога — только при ПОЛНОЙ слепоте, как и написано в её тексте. Прежнее
    # условие срабатывало, едва ошибок становилось не меньше, чем успехов:
    # при трёх обрывах прокси и двух прочитанных переписках владельцу
    # приходило «не удалось прочитать ни одну» — ложь, которая приучает
    # не верить тревогам. Частичные обрывы — норма мобильного прокси, о них
    # говорит лог.
    if failed == 0 or stats.checked > 0:
        return

    from datetime import datetime, timedelta, timezone

    key = blind_alert_key(source)
    when = as_utc(store.get_state(key))
    if when and datetime.now(timezone.utc) - when < timedelta(hours=BLIND_ALERT_HOURS):
        return

    # Отметку «уже жаловались» ставим только после ДОСТАВЛЕННОЙ жалобы.
    # Иначе один сбой Телеграма глушил тревогу о слепоте на половину суток —
    # ровно ту тревогу, которая отличает «клиенты молчат» от «мы ослепли».
    where = f" на {source.value}" if source else ""
    delivered = notify.send_alert(
        f"Не удалось прочитать ни одну переписку{where} ({failed} заказов). "
        "Ответы клиентов сейчас не отслеживаются — проверь, жива ли сессия биржи "
        "и открываются ли заказы вручную."
    )
    if delivered:
        store.set_state(key, datetime.now(timezone.utc).isoformat(timespec="seconds"))



# Сколько дней после отклика ещё ждём ответа. Дальше заказ либо взят кем-то,
# либо заброшен: гонять по нему браузер каждые пять минут — это время прохода
# и лишний трафик мобильного прокси.
WATCH_DAYS = 21
# Сколько заказов проверяем за раз. Каждый — это открытие страницы в браузере.
WATCH_LIMIT = 30
# Под этим ключом помним, когда заказ проверяли в последний раз. Нужен, чтобы
# очередь проверки была круговой и ни один заказ в ней не застревал навечно.
CHECKED_PREFIX = "convocheck"


def orders_awaiting_reply(
    store: Store, source: Source, *, days: int = WATCH_DAYS, limit: int = WATCH_LIMIT
) -> list[tuple[Order, int]]:
    """Заказы с отправленным откликом, по которым ещё имеет смысл ждать ответа."""
    from datetime import datetime, timedelta, timezone

    from .models import BidStatus

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    # Порядок — по давности ПРОВЕРКИ, а не по номеру отклика. Сортировка по
    # pk DESC отдавала одни и те же тридцать самых новых заказов на каждом
    # проходе: как только откликов набиралось больше лимита, всё, что старше,
    # переставало открываться вообще — навсегда, а не через WATCH_DAYS. Клиент
    # по такому заказу мог написать, и никто бы не узнал. Теперь очередь
    # круговая: дольше всех не проверявшийся идёт первым, и за несколько
    # проходов обходятся все.
    rows = store.conn.execute(
        f"""SELECT b.order_pk, o.* FROM bids b JOIN orders o ON o.pk = b.order_pk
           WHERE b.status = ? AND o.source = ? AND b.created_at >= ?
           ORDER BY COALESCE(
               (SELECT s.value FROM state s
                WHERE s.key = '{CHECKED_PREFIX}:' || o.source || ':' || o.ext_id),
               ''
           ) ASC, b.pk DESC
           LIMIT ?""",
        (BidStatus.SENT.value, source.value, cutoff, limit),
    ).fetchall()
    return [(store._row_to_order(row), int(row["order_pk"])) for row in rows]

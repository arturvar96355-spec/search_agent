"""Доставка в Telegram: карточка заказа, отчёт об отклике, тревоги.

Здесь только исходящие сообщения. Приём команд от тебя (стоп-кран, /status)
добавим на шаге 4, когда появится что останавливать.
"""
from __future__ import annotations

import html
import re

import httpx
from loguru import logger

from .config import get_settings
from .models import Order

API = "https://api.telegram.org/bot{token}/sendMessage"


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _attr(value: str) -> str:
    """Экранирование для значения внутри кавычек: href="…".

    Кавычку тут экранировать обязательно, а в обычном тексте — вредно: там она
    превратилась бы в «&quot;» прямо в цитате клиента. Одна кавычка в ссылке
    разваливает всю разметку, и Telegram отвергает сообщение целиком — то есть
    карточка не приходит вовсе, молча.
    """
    return html.escape(value or "", quote=True)


def _clip(text: str, limit: int) -> str:
    """Обрезка по границе слова. ЭКРАНИРУЕТ САМА — оборачивать в _esc не надо.

    Двойное экранирование не ломает разметку, но портит текст: «А & Б» в
    карточке превращалось в «А &amp;amp; Б», и владелец читал не то, что
    написал клиент.

    Резать посередине слова нельзя: «интервью за возн» читается как опечатка
    и подрывает доверие к карточке целиком.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return _esc(text)
    head = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return _esc(head) + "…"


def _now_hhmm() -> str:
    """Время по Москве. На карточке оно решает: бежать сейчас или после обеда."""
    from datetime import datetime

    from .store import MSK

    return datetime.now(MSK).strftime("%H:%M")


def _days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


# Telegram обрезает сообщения длиннее 4096 символов и возвращает ошибку.
TELEGRAM_LIMIT = 4096


# Теги, которые Telegram требует закрывать. Незакрытый тег — это не «текст
# без жирного», а отказ разобрать сообщение целиком: карточка не приходит.
# Дефис в имени тега значим: «tg-spoiler» и «tg-emoji» — обычные теги
# Telegram. По [a-zA-Z]+ они не находились вовсе, хотя в списке ниже стояли:
# обрезка их не закрывала, и обрезанная карточка не приходила совсем.
_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
# Полный набор, который Telegram понимает в режиме HTML. Такой же список есть
# в Orbit — там он общий на проект; здесь свой, потому что это отдельный
# проект со своим окружением, а задан этот набор самим Telegram.
_PAIRED = frozenset({
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote",
})


# Метка на месте отрезанного хвоста.
_CUT_MARK = "\n\n… сообщение обрезано"


def fit_to_limit(text: str) -> str:
    """Подгоняет сообщение под лимит Telegram, не ломая HTML-разметку.

    Публичная: тем же лимитом ограничены ответы бота, и резать их надо так же.

    Запас в восемьдесят знаков был угадан на глаз, и хвост из закрывающих
    тегов в него не всегда влезал: на тексте с сотней открытых тегов функция
    возвращала вдвое больше лимита — то есть ровно то, от чего защищает.
    Telegram такое сообщение отвергает целиком, и карточка не приходит вовсе.
    Поэтому длину хвоста считаем, а не угадываем, и проверяем результат.
    """
    if len(text) <= TELEGRAM_LIMIT:
        return text

    budget = TELEGRAM_LIMIT - len(_CUT_MARK)
    # Режем С УЧЁТОМ хвоста закрывающих тегов, а не вычитаем его из уже
    # отрезанного куска: во втором случае условие не выполнялось никогда
    # (длина куска и есть room), и обычная карточка с одной цитатой теряла
    # всю разметку и все ссылки.
    room = budget
    for _ in range(4):
        cut = _without_half_tag(text[:room])
        tail = _closing_tags(cut)
        if len(cut) + len(tail) <= budget:
            return cut + tail + _CUT_MARK
        room = budget - len(tail)
        if room <= 0:
            break
    # Разметка съела всё место. Текст важнее оформления: отдаём без тегов.
    return re.sub(r"<[^>]+>", "", text)[:budget] + _CUT_MARK


def _without_half_tag(text: str) -> str:
    """Убирает тег, разрезанный посередине.

    Разрез приходится на произвольное место, и однажды он приходится внутрь
    тега: в конце остаётся «<b» или «<blockqu». Telegram на такой разметке
    отвергает СООБЩЕНИЕ ЦЕЛИКОМ — то есть карточка не приходит вовсе, и
    выглядит это как «бот промолчал».
    """
    opened = text.rfind("<")
    if opened >= 0 and ">" not in text[opened:]:
        return text[:opened]
    return text


def _closing_tags(text: str) -> str:
    """Хвост из закрывающих тегов для всего, что осталось открытым.

    Разбираем разметку по-настоящему, а не считаем вхождения трёх известных
    тегов. Прежний список не знал про <i>, которым набраны половина подписей
    в карточках, а закрывал теги в порядке своего списка — то есть мог выдать
    «</blockquote></b>» там, где <b> лежал ВНУТРИ цитаты.
    """
    stack: list[str] = []
    for match in _TAG.finditer(text):
        name = match.group(1).lower()
        if name not in _PAIRED:
            continue
        if match.group(0).startswith("</"):
            if name in stack:
                # Закрываем ближайший подходящий, а заодно всё, что он накрыл.
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return "".join(f"</{name}>" for name in reversed(stack))


def send(text: str, *, disable_preview: bool = True, markup: dict | None = None) -> bool:
    """Отправляет сообщение. Никогда не роняет вызывающий код: Telegram лёг — работаем дальше.

    markup — кнопки под сообщением. Нужны там, где от владельца ждут действия
    прямо из уведомления: например, отправить клиенту подготовленный ответ.
    """
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram не настроен (нет токена или chat_id) — сообщение в лог: {}", text)
        return False
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": fit_to_limit(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if markup:
        payload["reply_markup"] = markup
    url = API.format(token=settings.telegram_bot_token)
    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
    except Exception as exc:  # noqa: BLE001 — доставка не должна ронять сбор
        logger.error("Не смог отправить в Telegram: {}", exc)
        return False
    if resp.status_code == 200:
        return True
    logger.error("Telegram вернул {}: {}", resp.status_code, resp.text[:300])

    # Чаще всего это разбор разметки: в текст попала угловая скобка из чужого
    # заголовка или из сообщения клиента. Здесь ходят вопросы клиентов и
    # готовые ответы на них — потерять карточку целиком хуже, чем показать её
    # без выделений. Повторяем один раз, простым текстом.
    # Слова разбираем по всему списку, а не по двум. Неизвестный тег Telegram
    # называет иначе — «unsupported start tag "script"», — и ни «parse», ни
    # «entity» в этом ответе нет: карточка не повторялась вовсе и пропадала
    # совсем. А сюда как раз приходят чужие тексты — заголовки заказов и
    # сообщения клиентов, где угловая скобка не редкость.
    trouble = resp.text.lower()
    if not any(word in trouble for word in ("parse", "entity", "tag", "unsupported")):
        return False
    payload["parse_mode"] = None
    payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))
    try:
        retry = httpx.post(url, json=payload, timeout=15.0)
    except Exception as exc:  # noqa: BLE001 — не вышло и так, но живём дальше
        logger.error("Повтор без разметки тоже не прошёл: {}", exc)
        return False
    if retry.status_code == 200:
        logger.info("Отправил без разметки — Telegram не принял HTML")
        return True
    logger.error("Повтор без разметки: {}", retry.text[:300])
    return False


SOURCE_TITLES = {"fl": "FL.ru", "profi": "Profi.ru", "youdo": "Юду"}


def order_card(order: Order) -> str:
    """Карточка нового заказа."""
    from .scoring import score_order

    lines = [f"🆕 <b>{_esc(order.title)}</b>"]

    meta = [SOURCE_TITLES.get(order.source.value, order.source.value)]
    if order.category:
        meta.append(_esc(order.category))
    lines.append(" · ".join(meta))

    # Рейтинг сразу в карточке: по нему видно, уйдёт отклик прямо сейчас
    # или заказ ждёт вечернего добора.
    score = score_order(order)
    lines.append(f"⭐ рейтинг {score.total:.1f} из 10")

    if order.budget_value:
        lines.append(f"💰 {order.budget_value:,.0f} ₽".replace(",", " "))
    elif order.budget_raw:
        lines.append(f"💰 {_esc(order.budget_raw)}")
    else:
        lines.append("💰 по договорённости")

    if order.bid_price:
        lines.append(f"💸 отклик стоит {order.bid_price:,.0f} ₽".replace(",", " "))

    if order.description:
        snippet = order.description.strip()
        if len(snippet) > 600:
            snippet = snippet[:600].rsplit(" ", 1)[0] + "…"
        lines.append("")
        lines.append(_esc(snippet))

    lines.append("")
    lines.append(f'<a href="{_attr(order.url)}">Открыть заказ</a>')
    return "\n".join(lines)


def send_order(order: Order) -> bool:
    return send(order_card(order), disable_preview=True)


def send_alert(text: str) -> bool:
    """Тревога: слетела сессия, кончился баланс, упёрлись в лимит."""
    return send(f"⚠️ <b>Внимание</b>\n\n{_esc(text)}")


def conversation_closed_card(order: "Order", *, reason: str = "", said: str = "") -> str:
    """Клиент ответил и закрыл тему: отказ или «спасибо, подумаю».

    Отдельная карточка, а не тишина. Это результат по заказу, и владелец
    должен его видеть: по таким сообщениям видно, на чём разговоры кончаются,
    а без них воронка показывает вечное «начали общение».
    """
    lines = [
        "🔚 <b>Разговор окончен</b>",
        f"<b>{_clip(order.title, 90)}</b>",
        SOURCE_TITLES.get(order.source.value, order.source.value),
    ]
    if said:
        lines += ["", "<b>Клиент написал:</b>", f"<blockquote>{_clip(said, 400)}</blockquote>"]
    if reason:
        lines += ["", f"<i>{_clip(reason, 200)}</i>"]
    lines += ["", "Больше сюда не пишем. Заказ остаётся в отчётах.",
              f'<a href="{_attr(order.url)}">Открыть заказ</a>']
    return fit_to_limit("\n".join(lines))


def client_replied_card(
    order: "Order", asked: str = "", *, ready_for_call: bool = False, said: str = ""
) -> str:
    """Главное уведомление: клиент что-то написал по заказу.

    Показываем САМО сообщение клиента, а не только пересказ модели одной
    строкой. Отвечает владелец, и решение «бежать сейчас или после обеда» он
    принимает по тексту: «сколько стоит» и «нам это больше не нужно» требуют
    разного. Раньше за текстом приходилось идти на биржу — лишний шаг ровно
    на самом важном событии воронки.

    Согласие на созвон выносим отдельным заголовком: это самое ценное событие
    воронки, и терять его среди обычных ответов нельзя.
    """
    head = (
        "📞 <b>КЛИЕНТ ГОТОВ НА СОЗВОН</b>"
        if ready_for_call
        else "📨 <b>Клиент ответил</b>"
    )
    lines = [
        f"{head} · {_now_hhmm()}",
        "",
        f"<b>{_clip(order.title, 70)}</b>",
        SOURCE_TITLES.get(order.source.value, order.source.value),
    ]
    if said:
        lines += ["", "<blockquote>" + _clip(said, 600) + "</blockquote>"]
    elif asked:
        lines += ["", f"<i>Спрашивает: {_esc(asked)}</i>"]
    lines += ["", f'<a href="{_attr(order.url)}">Открыть переписку</a>']
    return "\n".join(lines)


def silent_client_card(
    order: "Order", number: int, *, placed: str = "", days_silent: int = 0
) -> str:
    """Клиент молчит вторые сутки — может, стоит напомнить о себе.

    Карточка владельцу, а не сообщение клиенту. Приходит она только там, где
    разговор ведёт человек: дороже порога автоответа, по договорной цене или
    когда модель не смогла написать напоминание сама. Текст мы тут не готовим
    намеренно — писать будет владелец, и чужая заготовка ему только мешала бы.
    """
    when = f"Откликнулись {placed}. " if placed else ""
    silence = f" — {days_silent} {_days_word(days_silent)}" if days_silent else ""
    return "\n".join([
        f"🔕 <b>Клиент молчит{silence}</b> (напоминание {number} из 2)",
        "",
        f"<b>{_clip(order.title, 70)}</b>",
        SOURCE_TITLES.get(order.source.value, order.source.value),
        "",
        f"<i>{when}Ответа не было. Если считаете нужным — напомните о себе.</i>",
        "",
        f'<a href="{_attr(order.url)}">Открыть чат</a>',
    ])


# Карточки «заготовка ответа, отправьте кнопкой» здесь больше нет. Она жила
# при среднем положении тумблера — «модель пишет, владелец нажимает», — а его
# убрало правило по деньгам от 23.08.2026: теперь либо модель отправляет сама
# (дешёвые заказы), либо не пишет клиенту вовсе (дорогие). Промежуточного
# состояния не осталось, и карточка вводила бы в заблуждение обещанием кнопки,
# которой нет.


def expensive_client_card(
    order: "Order", *, asked: str = "", said: str = "", ready_for_call: bool = False
) -> str:
    """Клиент с крупным бюджетом ответил — разговор ведёт владелец, не модель.

    Отдельная карточка, а не обычная «клиент ответил»: на этих заказах
    владелец должен понять с первого взгляда, что отвечать надо ему и что
    сумма стоит того, чтобы бросить остальное. Деньги выносим в заголовок.
    """
    money = (
        f"{order.budget_value:,.0f} ₽".replace(",", " ")
        if order.budget_value
        else "цена договорная"
    )
    head = (
        f"📞 <b>ГОТОВ НА СОЗВОН · {money}</b>"
        if ready_for_call
        else f"💎 <b>Крупный клиент ответил · {money}</b>"
    )
    # Причину называем точно. «Сумма выше порога» на договорном заказе — враньё:
    # там сумма неизвестна, и это другая причина молчать.
    why = (
        "<i>Отвечаете вы: сумма выше порога автоответа.</i>"
        if order.budget_value
        else "<i>Отвечаете вы: цену заказчик не назвал, а вслепую я не пишу.</i>"
    )
    lines = [
        f"{head} · {_now_hhmm()}",
        "",
        f"<b>{_clip(order.title, 70)}</b>",
        SOURCE_TITLES.get(order.source.value, order.source.value),
        "",
        why,
    ]
    if said:
        lines += ["", "<blockquote>" + _clip(said, 600) + "</blockquote>"]
    elif asked:
        lines += ["", f"<i>Спрашивает: {_esc(asked)}</i>"]
    lines += ["", f'<a href="{_attr(order.url)}">Открыть переписку</a>']
    return "\n".join(lines)


def answer_sending_card(
    order: "Order", answer, *, nudge: bool = False, said: str = ""
) -> str:
    """Модель отвечает клиенту сама — владельцу показываем, что именно уходит.

    Уведомление всё равно нужно: это его имя и его аккаунт, и он должен видеть
    каждое сообщение, ушедшее от лица студии, даже когда не нажимал кнопку.
    """
    if nudge:
        # Настоящее время, а не прошедшее: карточка уходит в момент решения,
        # а сообщение клиенту — следом, и оно может не уйти вовсе (чат закрыт,
        # сессия слетела). «Напомнил» о неотправленном — это неправда, и
        # проверять её владелец пойдёт только когда клиент так и не ответит.
        head = "🔔 <b>Напоминаю о себе</b>"
    else:
        from .dialog import ACT_CALL

        head = (
            "📞 <b>Зову клиента на созвон</b>"
            if answer.action == ACT_CALL
            else "💬 <b>Отвечаю клиенту</b>"
        )

    lines = [f"{head} · {_now_hhmm()}", "", f"<b>{_clip(order.title, 70)}</b>",
             SOURCE_TITLES.get(order.source.value, order.source.value)]
    if said and not nudge:
        # Что написал клиент — дословно. Владелец не нажимал кнопку, значит
        # он должен видеть обе реплики: и вопрос, и наш ответ на него.
        lines += ["", "<blockquote>" + _clip(said, 400) + "</blockquote>"]
    elif answer.client_asked and not nudge:
        lines += ["", f"<i>Спросил: {_esc(answer.client_asked)}</i>"]
    lines += ["", "<blockquote>" + _esc(answer.reply) + "</blockquote>",
              "", f'<a href="{_attr(order.url)}">Открыть переписку</a>']
    return "\n".join(lines)


def handover_card(
    order: "Order", reason: str, asked: str = "", *, ready_for_call: bool = False
) -> str:
    """Дальше нужен живой человек: созвон, договор, торг или спорный вопрос.

    Согласие на созвон выносим отдельным заголовком. Это самое ценное событие
    во всей воронке, и оно не должно теряться среди одинаковых «нужен ваш
    ответ»: пропущенный час здесь стоит дороже, чем весь остальной софт.
    """
    lines = [
        "📞 <b>КЛИЕНТ ГОТОВ НА СОЗВОН</b>" if ready_for_call else "🙋 <b>Нужен ваш ответ</b>",
        "",
        f"<b>{_esc(order.title)}</b>",
        SOURCE_TITLES.get(order.source.value, order.source.value),
    ]
    if asked:
        lines += ["", f"<i>Спросил: {_esc(asked)}</i>"]
    if reason:
        lines += ["", f"Почему не отвечаю сам: {_esc(reason)}"]
    lines += ["", f'<a href="{_attr(order.url)}">Открыть заказ</a>']
    return "\n".join(lines)


def meeting_card(order: "Order", when: str, *, moved: bool = False,
                 confirmed: bool = True) -> str:
    """Время созвона — и что с ним делать.

    Время выносим первой строкой и крупно. Владелец читает это с телефона
    между делами, и единственное, что ему нужно унести из сообщения, —
    во сколько звонить.

    Три разных заголовка, и разница между ними не косметическая:

    · подтверждено обеими — можно ставить в календарь и забыть;
    · время назвал клиент, мы не ответили — ждут НАС, и это горит;
    · перенесли — второе сообщение по тому же заказу без пометки читается
      как повтор первого, и человек идёт к старому времени.
    """
    if moved:
        head = "🔄 <b>СОЗВОН ПЕРЕНЕСЛИ</b>"
    elif confirmed:
        head = "📞 <b>ДОГОВОРИЛИСЬ О СОЗВОНЕ</b>"
    else:
        head = "📞 <b>КЛИЕНТ ЗОВЁТ НА СОЗВОН</b>"
    lines = [head, "", f"<b>{_esc(when)}</b>"]
    if not confirmed:
        lines += ["", "Мы это время ещё не подтвердили — ответьте клиенту."]
    lines += [
        "",
        _esc(order.title),
        SOURCE_TITLES.get(order.source.value, order.source.value),
        "",
        f'<a href="{_attr(order.url)}">Открыть переписку</a>',
    ]
    return "\n".join(lines)


def message_sent_card(order: "Order", text: str) -> str:
    """Короткая расписка: сообщение действительно ушло клиенту.

    Текст здесь НЕ повторяем. Владелец уже видел его целиком в карточке
    «Отвечаю клиенту», которая уходит в момент решения, — а следом приходила
    вторая с тем же текстом слово в слово. Две одинаковые простыни подряд по
    каждому клиенту превращают чат в ленту, которую перестают читать.

    Начало текста тоже не показываем. У коротких ответов «первые шестьдесят
    знаков» — это и есть весь ответ, и расписка снова превращается в его
    копию. Владельцу здесь нужен один факт: сообщение дошло.

    Аргумент text оставлен ради совместимости с вызывающим кодом.
    """
    return "\n".join([
        f"✅ <b>Ответ ушёл клиенту</b> · {_now_hhmm()}",
        f"<b>{_clip(order.title, 60)}</b> · "
        f"{SOURCE_TITLES.get(order.source.value, order.source.value)}",
        f'<a href="{_attr(order.url)}">Открыть переписку</a>',
    ])


def bid_card(order: "Order", outcome, *, sent: bool) -> str:
    """Карточка отклика: что отправлено (или подготовлено) и ссылка на заказ.

    Ради этого сообщения всё и затевалось — заголовок, заход и ссылка,
    чтобы можно было сразу открыть переписку на бирже.
    """
    draft = getattr(outcome, "draft", None)
    label = (getattr(draft, "label", "") or order.title).strip()

    confirmed = getattr(outcome, "confirmed", True)
    if sent and not confirmed:
        # Зелёная галочка тут была бы враньём: деньги списаны, а нашего отклика
        # на странице заказа биржа не показала. Повторять нельзя — заплатим
        # второй раз, — но и делать вид, что всё хорошо, тоже.
        head = "⚠️ <b>Отклик оплачен, но биржа его не подтвердила</b>"
    elif sent:
        head = "✅ <b>Отклик отправлен</b>"
    else:
        head = "📝 <b>Заход готов</b> (не отправлен)"
    lines = [head, "", f"<b>{_esc(label)}</b>"]
    if sent and not confirmed:
        lines.insert(1, "Проверь заказ руками: повторно отправлять не буду.")

    meta = [SOURCE_TITLES.get(order.source.value, order.source.value)]
    if order.category:
        meta.append(_esc(order.category))
    lines.append(" · ".join(meta))

    if order.budget_value:
        lines.append(f"💰 бюджет клиента {order.budget_value:,.0f} ₽".replace(",", " "))
    score = getattr(outcome, "score", None)
    if score is not None:
        lines.append(f"⭐ {_esc(score.explain())}")
    if sent and outcome.cost:
        lines.append(f"💸 списано {outcome.cost:,.0f} ₽".replace(",", " "))
    estimate = getattr(draft, "estimate", "")
    if estimate:
        lines.append(f"📊 наша оценка: {_esc(estimate)} (внутреннее)")
    if not sent:
        lines.append(f"ℹ️ {_esc(outcome.reason)}")

    if outcome.text:
        lines.append("")
        lines.append("<blockquote>" + _esc(outcome.text) + "</blockquote>")

    lines.append("")
    lines.append(f'<a href="{_attr(order.url)}">Открыть заказ</a>')
    return "\n".join(lines)

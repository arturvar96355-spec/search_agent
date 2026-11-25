"""Ловит в переписке договорённость о созвоне и говорит владельцу время.

Зачем. Переписку с клиентом ведёт владелец сам, и время созвона всплывает
посреди разговора: «могу в 17:30» — «давайте в 18». Договорённость при этом
нигде не записана: она лежит в чате на бирже, вперемешку с обсуждением задачи.
Через день таких разговоров пять, и вспомнить, кому во сколько звонить, можно
только перечитав все.

Как. Разбор стоит денег, поэтому модель зовём не на каждое сообщение:
сначала дешёвая проверка — есть ли в переписке вообще похожее на время и на
слова про звонок. Нет — уходим молча, не потратив ни копейки.

Что считается договорённостью. Только ДВУСТОРОННЕЕ согласие: один предложил,
другой подтвердил. «Давайте созвонимся как-нибудь» — не договорённость, и
сообщать о ней значит приучить владельца не верить этим сообщениям.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from .config import get_settings
from .llm import api_timeout, extract_json, make_client, model_trouble, out_of_money
from .models import Order

# Отметка в базе: о какой договорённости владельцу уже сказали. Держим само
# время, а не «да/нет»: если созвон перенесли, это НОВАЯ новость, и промолчать
# о переносе хуже, чем не сказать вовсе — владелец придёт к старому времени.
TOLD_PREFIX = "meeting_told"

# Сколько последних сообщений показываем модели. Договорённость о времени
# живёт в конце разговора; тащить туда всё обсуждение задачи — это платить за
# чтение того, что на ответ не влияет.
TAIL_MESSAGES = 12

# Дешёвый отсев до модели. Оба условия обязательны: и намёк на время, и слова
# про созвон. По одному времени срабатывало бы на сроках проекта («сделаем за
# 3 дня к 15 числа»), а по одному слову «созвон» — на «давайте созвонимся,
# когда будет ТЗ», где никакого времени нет.
_WHEN = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b"                       # 17:30, 9.00
    r"|\bв\s+\d{1,2}(\s*(час|ч)\w*)?\b"           # в 18, в 9 часов
    r"|\b(сегодня|завтра|послезавтра|понедельник\w*|вторник\w*|сред[ауы]\w*"
    r"|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b"
    r"|\b\d{1,2}\s*(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр"
    r"|октябр|ноябр|декабр)\w*",
    re.IGNORECASE,
)
_CALL = re.compile(
    r"созвон\w*|созвонимся|созвониться|звон(ок|ка|ке|ком)?\b|позвон\w+"
    r"|набер\w*|zoom|зум|google\s*meet|гугл\s*мит|телемост|скайп|skype"
    r"|встрет\w+|встреч\w+",
    re.IGNORECASE,
)

PROMPT = """Ты читаешь переписку исполнителя с клиентом на фриланс-бирже.

Твоя единственная задача: понять, лежит ли на столе КОНКРЕТНОЕ время созвона
или встречи, и какое.

Время считается лежащим на столе, когда одна из сторон назвала его и при этом
видно намерение, а не размышление вслух: «давайте в 18:00», «наберу в 17:30»,
«ок, завтра в 12». Встречное время («я в 17:30» — «давайте в 18:00») тоже
считается: оно названо прямо и ждёт только подтверждения.

Отдельно скажи, подтвердили ли его ОБЕ стороны.

Это НЕ время созвона:
- «давайте созвонимся» без времени;
- сроки работы, оплаты или сдачи — они про задачу, а не про звонок;
- созвон, который уже состоялся;
- время, названное и тут же отклонённое («в 18 не могу»).

Ответь одним объектом JSON, без пояснений вокруг:
{"agreed": true/false,
 "confirmed": true/false,
 "when": "время словами из переписки: «завтра в 18:00»",
 "who": "кто кому звонит, если сказано, иначе пустая строка",
 "how": "телефон, zoom, что назвали, иначе пустая строка"}

agreed — время названо и от него не отказались.
confirmed — обе стороны явно согласились именно на это время.
Если времени нет — {"agreed": false, "confirmed": false, "when": "", "who": "", "how": ""}.
Поле when заполняй так, как сказано в переписке, не пересчитывая в дату."""


@dataclass(frozen=True)
class Meeting:
    """Договорённость о созвоне."""

    when: str
    who: str = ""
    how: str = ""
    # Согласились ли ОБЕ стороны. False — время назвал кто-то один, и ждут
    # нас: это не менее срочно, чем готовая договорённость, но сказать об
    # этом надо иначе — иначе владелец решит, что всё решено без него.
    confirmed: bool = True

    @property
    def line(self) -> str:
        """Одной строкой для карточки владельцу."""
        parts = [self.when]
        if self.how:
            parts.append(self.how)
        if self.who:
            parts.append(self.who)
        return " · ".join(part for part in parts if part)


def looks_like_a_call(conversation: str, *, known: bool = False) -> bool:
    """Стоит ли вообще звать модель. Дешёвая проверка до всяких трат.

    known — про созвон по этому заказу уже говорили. Тогда достаточно одного
    времени: переносят его словами «давайте лучше в 19:30», и слова «созвон»
    там может не быть вовсе. Без этой поблажки перенос проходил бы мимо —
    а это ровно тот случай, где молчание хуже всего.
    """
    text = conversation or ""
    if not _WHEN.search(text):
        return False
    return known or bool(_CALL.search(text))


def tail(conversation: str, limit: int = TAIL_MESSAGES) -> str:
    """Последние сообщения переписки. Разделитель — пустая строка, как в transcript."""
    parts = [part for part in (conversation or "").split("\n\n") if part.strip()]
    return "\n\n".join(parts[-limit:])


def find(order: Order, conversation: str, *, known: bool = False) -> Meeting | None:
    """Ищет время созвона. None — его нет или спросить не вышло."""
    if not looks_like_a_call(conversation, known=known):
        return None
    if model_trouble():
        # Модель уже молчит — второй запрос ничего не даст, а времени отнимет.
        logger.debug("[{}] модель недоступна, созвон в переписке не ищу", order.key)
        return None

    settings = get_settings()
    try:
        client = make_client(timeout=api_timeout())
    except ImportError:
        logger.error("Пакет anthropic не установлен: pip install -e '.[llm]'")
        return None

    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=300,
            system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": tail(conversation)}],
        )
    except Exception as exc:  # noqa: BLE001 — упавший разбор не рушит проход
        if out_of_money(exc):
            # Отдельная ветка намеренно: «кончились деньги» чинится за минуту
            # пополнением счёта, а «модель не ответила» — нет. Своей тревоги
            # здесь не шлём, её уже отправит общая проверка.
            logger.error("[{}] созвон не разобран: кончились деньги", order.key)
        else:
            logger.warning("[{}] созвон в переписке не разобрался: {}", order.key, exc)
        return None

    text = "\n".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    data = extract_json(text)
    if not data.get("agreed"):
        return None
    when = str(data.get("when") or "").strip()
    if not when:
        # «Договорились, но когда — не сказано» это не договорённость, а шум.
        # Сообщение без времени владельцу бесполезно: звонить всё равно нечему.
        logger.debug("[{}] модель сказала «договорились», но без времени", order.key)
        return None
    return Meeting(
        when=when[:120],
        who=str(data.get("who") or "").strip()[:80],
        how=str(data.get("how") or "").strip()[:60],
        confirmed=bool(data.get("confirmed")),
    )


def told_key(order: Order) -> str:
    return f"{TOLD_PREFIX}:{order.source.value}:{order.ext_id}"


def already_told(store, order: Order, meeting: Meeting) -> bool:
    """Говорили ли уже про ЭТУ договорённость.

    Сравниваем само время, а не факт. Перенос созвона — новость не меньшая,
    чем сама договорённость: промолчав о нём, мы отправим владельца к старому
    времени, то есть сделаем хуже, чем если бы не говорили вовсе.
    """
    return (store.get_state(told_key(order)) or "") == meeting.when


def remember(store, order: Order, meeting: Meeting) -> None:
    store.set_state(told_key(order), meeting.when)

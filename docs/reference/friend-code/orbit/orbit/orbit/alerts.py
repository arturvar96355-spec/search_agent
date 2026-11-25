"""Что дёргает владельца сразу, а что подождёт до вечерней сводки.

Правило одно: сообщение приходит тогда, когда без человека дальше не поедет.
Всё остальное — не сообщение, а строка в итогах дня. Разница не в важности:
«письмо ушло» важно для дела, но требовать внимания в ту же секунду ему
незачем — владелец посмотрит вечером или нажмёт кнопку.

Почему реестр, а не `if` по месту. Таких сообщений полтора десятка, они
разбросаны по почте, расписанию и биржам, и раньше каждое решало за себя —
«слать или не слать» было зашито в код. Понять, что именно приходит, можно
было только вычитав все файлы; выключить лишнее — только правкой кода.
Здесь всё перечислено в одном месте, и у каждого есть переключатель.

Тихое событие НЕ пропадает: оно ложится в журнал дня и вечером выходит
строкой в сводке. Молчание здесь — про время доставки, а не про сокрытие.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from .store import Store

# Вид события в журнале. Сводка забирает их по нему.
QUIET_KIND = "quiet"

# Ключ переключателя в настройках. Пусто — берём значение по умолчанию.
STATE_PREFIX = "alert:"


@dataclass(frozen=True)
class Alert:
    """Один вид сообщения.

    instant — дёргать ли владельца сразу. Значение по умолчанию; владелец
    может переключить любое, и его выбор перевесит.
    """

    code: str
    title: str
    instant: bool
    why: str = ""


# --- Дёргаем сразу: без человека дальше не поедет ----------------------------

MODEL_DOWN = Alert("model", "Модель не отвечает", True, "встают все три системы")
MAILBOX_DOWN = Alert("mailbox", "Ящик не читается", True, "ответы клиентов не разбираются")
SESSION_LOST = Alert("session", "Слетел вход на биржу", True, "чинится только руками")
SOURCE_DOWN = Alert("source", "Площадка не отвечает", True, "сбор и отклики оттуда встали")
MONEY_OUT = Alert("money", "Кончились деньги на бирже", True, "отклики встали")
CLIENT_REPLIED = Alert("client", "Клиент ответил", True, "человек написал и ждёт")
CALL_AGREED = Alert("call", "Договорились о созвоне", True, "у встречи есть время")
LETTER_REPLY = Alert("reply", "Ответ на письмо", True, "человек написал и ждёт")
TASK_FAILED = Alert("task", "Задача не выполнилась", True, "сама не починится")
BATCH_DIED = Alert("batch", "Пачка писем не пошла", True, "экран уже пообещал письма")

# --- Ждут вечера: это отчёт о сделанном, а не просьба вмешаться --------------

BACKUP_DONE = Alert("backup", "Бэкап сделан", False, "приходил в 4:30 ночи")
FOLLOWUP_READY = Alert("followup", "Письма вдогонку готовы", False, "лежат в очереди")
LETTER_SENT = Alert("sent", "Письмо ушло", False, "видно в карточке контакта")
ADDRESS_BAD = Alert("address", "Адрес не годится", False, "письмо снято, контакт закрыт")
LETTER_DROPPED = Alert("dropped", "Письмо снято с отправки", False, "адрес не принял")
LETTER_UNCLEAR = Alert("unclear", "Судьба письма неизвестна", False, "проверяется руками")
BID_SENT = Alert("bid", "Отклик отправлен", False, "видно по кнопке «Отклики за сегодня»")
BID_FAILED = Alert("bidfail", "Отклик не отправился", False, "заказ остаётся в очереди")
BID_SKIPPED = Alert("skip", "Хороший заказ пропущен", False, "видно по кнопке «Очередь»")
WE_ANSWERED = Alert("answered", "Мы ответили клиенту", False, "видно в переписке")

ALL: tuple[Alert, ...] = (
    MODEL_DOWN, MAILBOX_DOWN, SESSION_LOST, SOURCE_DOWN, MONEY_OUT,
    CLIENT_REPLIED, CALL_AGREED, LETTER_REPLY, TASK_FAILED, BATCH_DIED,
    BACKUP_DONE, FOLLOWUP_READY, LETTER_SENT, ADDRESS_BAD, LETTER_DROPPED,
    LETTER_UNCLEAR, BID_SENT, BID_FAILED, BID_SKIPPED, WE_ANSWERED,
)

BY_CODE = {alert.code: alert for alert in ALL}


def is_instant(store: Store, alert: Alert) -> bool:
    """Дёргать ли владельца этим сообщением прямо сейчас."""
    raw = store.get_state(STATE_PREFIX + alert.code)
    if raw in ("0", "1"):
        return raw == "1"
    return alert.instant


def set_instant(store: Store, alert: Alert, value: bool) -> None:
    store.set_state(STATE_PREFIX + alert.code, "1" if value else "0")


def deliver(store: Store, alert: Alert, text: str) -> None:
    """Доставляет сообщение так, как для него настроено.

    Сразу — в телеграм. Тихо — строкой в журнал дня, откуда его заберёт
    вечерняя сводка. Телеграм не ответил — тоже кладём в журнал: потерять
    событие совсем хуже, чем показать его на несколько часов позже.
    """
    if is_instant(store, alert):
        from .notify import tell

        if tell(text):
            return
        logger.warning("«{}» не доставилось сразу — уйдёт в вечернюю сводку", alert.title)
    quietly(store, text)


def quietly(store: Store, text: str) -> None:
    """Кладёт строку в журнал дня, ничего не отправляя."""
    store.log_event(None, QUIET_KIND, text)

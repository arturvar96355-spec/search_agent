"""Юду (youdo.com).

Открытая доска заданий, но через обычный HTTP её не взять: перед сайтом стоит
ServicePipe и отдаёт JS-челлендж, а на серверные IP — сразу 403. Проверено
05.08.2026: curl и headless-Chromium получают «Доступ ограничен», а тот же
Chromium с профилем и мобильным прокси открывает доску нормально. Поэтому Юду
читается браузером, как Profi.ru.

Что здесь ЕСТЬ: сбор заданий с общей доски, разбор карточки и страницы задания,
уведомления в Telegram.

Чего здесь НЕТ: отправки отклика. Форма отклика видна только под аккаунтом, а
владелец в него ещё не входил (06.08.2026). Поэтому can_bid = False, и площадка
не попадает в список тех, куда уходят отклики — случайно потратить деньги
на неоткалиброванной форме нельзя.

Отдельно про деньги: на Юду типичный бюджет задания — 3-15 тысяч, то есть ниже
порога min_budget. Ждать оттуда потока крупных заказов не стоит; смысл площадки
в том, что изредка там появляются заказы на 50-150 тысяч.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from ..models import Order, Source

BASE_URL = "https://youdo.com"
FEED_URL = f"{BASE_URL}/tasks-all-opened-all"

# Карточка задания в списке. Классы у Next.js с хвостом-хэшем, поэтому цепляемся
# за устойчивый префикс, а не за полное имя класса.
CARD_SELECTOR = 'li[class*="TasksList_listItem"]'
TASK_LINK_RE = re.compile(r"/t(\d+)")

# «до 4 500 ₽», «до 145 000 руб.»
_BUDGET_RE = re.compile(r"до\s+([\d\s  ]+)\s*(?:₽|руб)", re.IGNORECASE)
# «Создано 1 ч. 4 мин. назад», «Создано 12 мин. назад», «Создано 2 дн. назад»
_AGE_RE = re.compile(
    r"создано\s+(?:(\d+)\s*дн)?\.?\s*(?:(\d+)\s*ч)?\.?\s*(?:(\d+)\s*мин)?", re.IGNORECASE
)
_REVIEWS_RE = re.compile(r"отзыв\w*\s*:?\s*(\d+)", re.IGNORECASE)
# Ссылка на профиль заказчика — единственный надёжный ключ для дедупа по клиенту.
_CLIENT_RE = re.compile(r"/u(\d+)")

REMOTE_MARKER = "можно выполнить удаленно"
NO_REVIEWS_MARKER = "нет отзывов"

# Блок задания на странице: текст между «Нужно» и кнопкой отклика.
_TASK_BODY_RE = re.compile(r"\nНужно\s*\n(.+?)\n(?:Откликнуться|Хотите найти)", re.DOTALL)

NAV_TIMEOUT_MS = 60_000
SETTLE_MS = 6_000


class NotAuthorized(RuntimeError):
    """Юду показал гостевую страницу или заблокировал доступ."""


@dataclass
class Card:
    """Разобранная карточка из списка. Поля — ровно то, что Юду показывает."""

    title: str = ""
    location: str = ""
    budget_raw: str = ""
    budget_value: float | None = None
    client_name: str = ""
    reviews: int = 0
    remote: bool = False
    extra: list[str] = field(default_factory=list)


def parse_budget(text: str) -> tuple[str, float | None]:
    """«до 4 500 ₽» → («до 4 500 ₽», 4500.0). Не нашли — (пусто, None).

    None значит «сумма не указана», а НЕ ноль: обнулять такие задания нельзя,
    иначе порог по бюджету выкинет все договорные.
    """
    match = _BUDGET_RE.search(text or "")
    if not match:
        return "", None
    digits = re.sub(r"[^\d]", "", match.group(1))
    if not digits:
        return match.group(0).strip(), None
    return match.group(0).strip(), float(digits)


def parse_card(text: str) -> Card:
    """Разбирает текст карточки.

    Порядок строк снят с живой доски 06.08.2026:
        заголовок / место или «Можно выполнить удаленно» / когда начать /
        [Сделка без риска] / до N ₽ / имя заказчика / отзывы
    Порядок может поехать при редизайне, поэтому каждую строку опознаём по
    содержимому, а не по номеру.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    card = Card()
    if not lines:
        return card

    card.title = lines[0]
    body = lines[1:]

    for line in body:
        lowered = line.lower()
        if REMOTE_MARKER in lowered:
            card.remote = True
        if not card.budget_raw:
            raw, value = parse_budget(line)
            if raw:
                card.budget_raw, card.budget_value = raw, value

    # Отзывы и имя заказчика идут последними: «Николай Б.» затем «1» или
    # «Нет отзывов». Число в самом низу карточки — это всегда отзывы.
    if body:
        tail = body[-1].lower()
        if tail.isdigit():
            card.reviews = int(tail)
            card.client_name = body[-2] if len(body) >= 2 else ""
        elif NO_REVIEWS_MARKER in tail:
            card.reviews = 0
            card.client_name = body[-2] if len(body) >= 2 else ""

    card.location = next(
        (line for line in body if not line.lower().startswith("до ") and "," in line), ""
    )
    card.extra = body
    return card


def parse_age(text: str, *, now: datetime | None = None) -> datetime | None:
    """«Создано 1 ч. 4 мин. назад» → момент публикации.

    Юду не показывает точное время публикации, только возраст. Не разобрали —
    None, и заказ считается заказом без даты: фильтр по свежести его пропустит,
    а рейтинг даст среднюю оценку, а не максимум.
    """
    match = _AGE_RE.search(text or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=days, hours=hours, minutes=minutes)


def parse_task_body(page_text: str) -> str:
    """Текст задания со страницы. Пусто — значит вёрстка поехала."""
    match = _TASK_BODY_RE.search(page_text or "")
    return match.group(1).strip() if match else ""


def is_blocked(page_text: str) -> bool:
    """Страница защиты вместо доски: дальше идти нельзя."""
    lowered = (page_text or "").lower()
    return "доступ ограничен" in lowered or "403 forbidden" in lowered


class YoudoSource:
    source_enum = Source.YOUDO
    name = Source.YOUDO.value
    # Перед сайтом стоит защита, обычному HTTP-клиенту отдаёт 403.
    needs_browser = True
    # Отправка отклика не откалибрована: форма видна только под аккаунтом.
    # Пока False — площадка собирает заказы и шлёт уведомления, но не тратит деньги.
    can_bid = False

    # --- Лента -------------------------------------------------------------

    def fetch_feed(self, context) -> list[Order]:
        """Задания с общей доски. Отбор по профилю делают фильтры, не мы."""
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(FEED_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)

        body = page.inner_text("body")
        if is_blocked(body):
            raise NotAuthorized(
                "Юду закрыл доступ защитой. Обычно помогает мобильный прокси: "
                "проверь PROXY_URL и что PROXY_FOR_BROWSER=true"
            )

        cards = page.evaluate(
            """(selector) => [...document.querySelectorAll(selector)].map(li => {
                const a = li.querySelector('a[href*="/t"]');
                return {href: a ? a.getAttribute('href') : '', text: li.innerText || ''};
            })""",
            CARD_SELECTOR,
        )
        orders = [order for card in cards if (order := self._card_to_order(card)) is not None]
        logger.info("[youdo] на доске {} заданий", len(orders))
        return orders

    @staticmethod
    def _card_to_order(raw: dict) -> Order | None:
        href = (raw.get("href") or "").split("?")[0]
        match = TASK_LINK_RE.search(href)
        if not match:
            return None

        card = parse_card(raw.get("text", ""))
        if not card.title:
            return None

        return Order(
            source=Source.YOUDO,
            ext_id=match.group(1),
            url=f"{BASE_URL}{href}",
            title=card.title,
            description="",
            category="",
            budget_raw=card.budget_raw,
            budget_value=card.budget_value,
            description_full=False,
            raw={
                "client_name": card.client_name,
                "reviews": card.reviews,
                "remote": card.remote,
                "location": card.location,
            },
        )

    # --- Полное задание ----------------------------------------------------

    def fetch_details(self, context, order: Order) -> Order:
        """Догружает текст задания, категорию, возраст и заказчика.

        Ошибку не поднимаем: задание с коротким описанием лучше, чем упавший проход.
        """
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(order.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(SETTLE_MS)
            text = page.inner_text("body")
            html = page.content()
        except Exception as exc:  # noqa: BLE001 — обрыв прокси здесь ожидаем
            logger.warning("[youdo] не открыл {}: {}", order.url, exc)
            return order

        if is_blocked(text):
            raise NotAuthorized("Юду закрыл доступ защитой на странице задания")

        body = parse_task_body(text)
        if body:
            order.description = body
            order.description_full = True

        published = parse_age(text)
        if published is not None:
            order.published_at = published

        if not order.budget_value:
            raw, value = parse_budget(text)
            if raw:
                order.budget_raw, order.budget_value = raw, value

        reviews = _REVIEWS_RE.search(text)
        if reviews:
            order.raw["reviews"] = int(reviews.group(1))

        client = _CLIENT_RE.search(html)
        if client:
            order.client_key = f"u{client.group(1)}"

        return order

    # --- Отклик ------------------------------------------------------------

    def submit_bid(self, context, order: Order, text: str, **kwargs) -> bool:
        """Заглушка: форма отклика не откалибрована.

        Намеренно не пытаемся «угадать» кнопку: на Юду отклик списывает деньги
        со счёта исполнителя, и слепой клик — это трата вслепую.
        """
        raise NotImplementedError(
            "Отклик на Юду не настроен: нужен вход в аккаунт и калибровка формы. "
            "Запусти `fbid login youdo`, дальше правится fbid/sources/youdo.py"
        )

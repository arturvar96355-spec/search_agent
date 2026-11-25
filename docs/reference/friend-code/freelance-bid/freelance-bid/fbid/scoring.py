"""Рейтинг заказа от 0 до 10: кому отклик летит сразу, а кто ждёт вечера.

Логика владельца (06.08.2026): сложность задачи не важна — важно, чтобы у
заказчика были деньги, и идеально, если ему нужно ровно то, что у нас на
витрине (сайт, CRM, бот, интеграции, автоматизация). Набрал 7 из 10 —
отклик уходит в ту же секунду. Меньше 7 — заказ откладывается и получает
свой шанс вечером, если остались лимиты и деньги.

Поэтому веса такие:

    деньги       0-5.0   главный вес, всё остальное вместе его не перебивает
    витрина      0-2.0   заказ ровно про то, что мы уже умеем и показываем
    свежесть     0-2.0   на свежий заказ откликов ещё мало, нас прочитают
    доверие      0-1.0   отзывы, телефон, позиция в очереди — «чуть ниже» весом

Рейтинг НИЧЕГО не разрешает сам по себе: он только выбирает очередь. Все
предохранители на деньги живут в bidder.py и filters.py и работают отдельно.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .filters import matches
from .models import Order

# Потолки по каждой составляющей. Сумма ровно 10.
MONEY_MAX = 5.0
SERVICE_MAX = 2.0
FRESH_MAX = 2.0
TRUST_MAX = 1.0

# Бюджет, ниже которого заказ считаем мелким. Совпадает с min_budget из
# filters.yaml, но передаётся явно — рейтинг не должен зависеть от файла правил.
DEFAULT_BUDGET_FLOOR = 20_000.0
# Во сколько раз бюджет должен превысить порог, чтобы дать максимум по деньгам.
# Пятикратный порог (100 000 ₽ при пороге 20 000 ₽) — это уже крупный заказ,
# дальше расти шкале некуда. С десятикратным множителем свежий заказ на 30 000 ₽
# по нашему профилю не дотягивал до порога мгновенного отклика, а он должен.
BUDGET_TOP_MULTIPLIER = 5.0

# Заказ без указанной суммы: до генерации захода мы не знаем, сколько там денег.
# Даём ровно столько, сколько дал бы заказ на пороговую сумму, и пересчитываем
# рейтинг после того, как модель оценит работу.
NEUTRAL_MONEY = MONEY_MAX / 2

# Сколько минут заказ считается «только что опубликован».
FRESH_FULL_MINUTES = 30.0
# Через сколько часов свежесть падает до нуля.
FRESH_ZERO_HOURS = 6.0

# То, что мы показываем на uniqore.ru и продаём в первую очередь. Такой заказ
# нам продать проще всего: есть кейс, есть демо, есть готовые куски.
SHOWCASE = (
    "сайт",
    "лендинг",
    "landing",
    "интернет-магазин",
    "магазин",
    "crm",
    "срм",
    "amocrm",
    "битрикс24",
    "бот",
    "чат-бот",
    "телеграм",
    "telegram",
    "интеграц",
    "автоматизац",
    "личный кабинет",
    "админк",
    "парсинг",
    "парсер",
    "мобильное приложение",
    "приложение",
    "api",
    "нейросет",
    "ии-агент",
    "ai-агент",
    "gpt",
)

# Разработка, но не витрина: тоже наше, просто продавать чуть дольше.
CORE_DEV = (
    "разработ",
    "программир",
    "скрипт",
    "доработ",
    "верстк",
    "вёрстк",
    "python",
    "php",
    "javascript",
    "react",
    "база данных",
    "sql",
    "сервер",
    "devops",
    "1с",
    "тестирован",
    "рефактор",
)

SHOWCASE_SCORE = SERVICE_MAX          # 2.0 — ровно наша витрина
CORE_SCORE = SERVICE_MAX * 0.6        # 1.2 — наше, но не с витрины
FALLBACK_SCORE = SERVICE_MAX * 0.3    # 0.6 — прошло фильтры, но по чём непонятно


@dataclass(frozen=True)
class Score:
    """Разложенный рейтинг. Хранится по частям, чтобы было видно, за что балл."""

    money: float
    service: float
    freshness: float
    trust: float
    # На чём сработала витрина — попадает в лог и в карточку Telegram.
    service_hit: str = ""
    # Бюджет посчитан по нашей оценке, а не по сумме от заказчика.
    money_is_estimate: bool = False

    @property
    def total(self) -> float:
        return round(self.money + self.service + self.freshness + self.trust, 2)

    def is_hot(self, threshold: float) -> bool:
        return self.total >= threshold

    def explain(self) -> str:
        """Однострочная расшифровка для логов и Telegram."""
        money = f"деньги {self.money:.1f}"
        if self.money_is_estimate:
            money += " (наша оценка)"
        service = f"витрина {self.service:.1f}"
        if self.service_hit:
            service += f" ({self.service_hit})"
        return (
            f"{self.total:.1f}/10 · {money} · {service} · "
            f"свежесть {self.freshness:.1f} · доверие {self.trust:.1f}"
        )

    def __str__(self) -> str:
        return self.explain()


def score_order(
    order: Order,
    *,
    now: datetime | None = None,
    budget_floor: float | None = None,
) -> Score:
    """Считает рейтинг заказа. Ничего не решает и не тратит — только сортирует."""
    floor = budget_floor if budget_floor and budget_floor > 0 else DEFAULT_BUDGET_FLOOR
    money, is_estimate = _money_score(order, floor)
    service, hit = _service_score(order)
    return Score(
        money=money,
        service=service,
        freshness=_freshness_score(order, now=now),
        trust=_trust_score(order),
        service_hit=hit,
        money_is_estimate=is_estimate,
    )


def order_budget(order: Order) -> float | None:
    """Сумма заказа: явный бюджет заказчика или наша оценка работы по ТЗ."""
    if order.budget_value:
        return float(order.budget_value)
    estimated = order.raw.get("estimated_budget")
    try:
        value = float(estimated)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _money_score(order: Order, floor: float) -> tuple[float, bool]:
    """0-5 за деньги. Логарифм: заказ за миллион не должен обнулять всё остальное."""
    budget = order_budget(order)
    if budget is None:
        # Договорной заказ до генерации захода: считаем как пороговый, а после
        # оценки модели рейтинг пересчитается уже по цифре.
        return NEUTRAL_MONEY, True

    is_estimate = not order.budget_value
    if budget <= 0:
        return 0.0, is_estimate

    # На пороге даём половину шкалы, на BUDGET_TOP_MULTIPLIER порогов —
    # максимум. Множитель именно отсюда, а не из головы: здесь стояло
    # «на десятикратном», а константа равна пяти.
    ratio = math.log10(budget / floor) / math.log10(BUDGET_TOP_MULTIPLIER)
    raw = NEUTRAL_MONEY + NEUTRAL_MONEY * ratio
    return round(_clamp(raw, 0.0, MONEY_MAX), 2), is_estimate


def _service_score(order: Order) -> tuple[float, str]:
    """0-2 за попадание в витрину. Ищем и в заголовке, и в ТЗ, и в категории."""
    haystack = f"{order.title}\n{order.description}\n{order.category}"
    hit = next((word for word in SHOWCASE if matches(haystack, word)), None)
    if hit:
        return SHOWCASE_SCORE, hit
    hit = next((word for word in CORE_DEV if matches(haystack, word)), None)
    if hit:
        return CORE_SCORE, hit
    return FALLBACK_SCORE, ""


def _freshness_score(order: Order, *, now: datetime | None = None) -> float:
    """0-2 за свежесть. Полный балл первые полчаса, ноль через шесть часов."""
    if order.published_at is None:
        # Дату не разобрали. Это не повод ни поощрять, ни наказывать заказ:
        # даём середину, иначе площадка без дат провалилась бы вся целиком.
        return round(FRESH_MAX / 2, 2)

    published = order.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_hours = (now - published).total_seconds() / 3600
    if age_hours <= FRESH_FULL_MINUTES / 60:
        return FRESH_MAX
    decay = (age_hours - FRESH_FULL_MINUTES / 60) / (FRESH_ZERO_HOURS - FRESH_FULL_MINUTES / 60)
    return round(_clamp(FRESH_MAX * (1 - decay), 0.0, FRESH_MAX), 2)


# Сколько заказчик на бирже — как это пишет FL.ru на странице заказа. Человек,
# зарегистрировавшийся на этой неделе, ещё никого не нанимал: он не успел. Это
# не значит, что заказ плохой, — значит, что мгновенный отклик ему покупается
# вслепую, а отклики конечны и стоят денег.
NEWCOMER_MARKS = ("меньше недели", "менее недели", "меньше месяца", "менее месяца")


def newcomer(order: Order) -> str:
    """Сколько заказчик на бирже, если он там совсем недавно. Иначе пустая строка.

    Пустая строка значит «не новичок ИЛИ площадка не сказала» — и это
    намеренно одно и то же: вывод о человеке делаем только по прочитанному,
    а молчание разметки выводом не считаем.
    """
    raw = str(order.raw.get("client_registered") or "").strip().lower()
    if not raw:
        return ""
    return raw if any(mark in raw for mark in NEWCOMER_MARKS) else ""


def _trust_score(order: Order) -> float:
    """0-1 за заказчика и позицию в очереди.

    Вес намеренно маленький: владелец просил считать репутацию «чуть ниже»
    остального. Пустой профиль не должен хоронить заказ с большим бюджетом.
    """
    score = 0.0

    reviews = order.raw.get("reviews")
    if isinstance(reviews, (int, float)) and reviews > 0:
        score += min(0.4, 0.1 + float(reviews) * 0.05)

    if order.raw.get("phone_confirmed"):
        score += 0.2

    rank = order.raw.get("bid_rank")
    if isinstance(rank, int) and rank > 0:
        # Первым быть заметно лучше, чем пятым: клиент читает сверху.
        score += max(0.0, 0.4 - (rank - 1) * 0.1)

    return round(_clamp(score, 0.0, TRUST_MAX), 2)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

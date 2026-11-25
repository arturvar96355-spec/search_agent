"""Тесты рейтинга заказа.

Рейтинг решает, уйдёт отклик прямо сейчас или заказ подождёт вечера. Ошибка
здесь не тратит деньги напрямую, но раздаёт конечный суточный лимит не тем —
а это упущенные заказы.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fbid.models import Order, Source
from fbid.scoring import (
    FRESH_MAX,
    MONEY_MAX,
    SERVICE_MAX,
    TRUST_MAX,
    Score,
    order_budget,
    score_order,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def order(**kwargs) -> Order:
    data = {
        "source": Source.FL,
        "ext_id": "1",
        "url": "https://www.fl.ru/projects/1/x.html",
        "title": "Нужен сайт-каталог",
        "description": "Каталог с корзиной",
        "category": "Сайты",
        "budget_value": 45_000.0,
        "published_at": NOW,
    }
    data.update(kwargs)
    return Order(**data)


# --- Шкала -----------------------------------------------------------------

def test_score_never_leaves_zero_ten():
    """Сумма частей не должна вылезать за 10 ни при каких данных."""
    extreme = order(
        budget_value=100_000_000.0,
        raw={"reviews": 999, "phone_confirmed": True, "bid_rank": 1},
    )
    score = score_order(extreme, now=NOW)
    assert 0 <= score.total <= 10
    assert score.money <= MONEY_MAX
    assert score.service <= SERVICE_MAX
    assert score.freshness <= FRESH_MAX
    assert score.trust <= TRUST_MAX


def test_parts_sum_to_total():
    score = score_order(order(), now=NOW)
    parts = score.money + score.service + score.freshness + score.trust
    assert score.total == pytest.approx(parts, abs=0.01)


# --- Деньги главнее всего --------------------------------------------------

def test_more_money_means_higher_score():
    small = score_order(order(budget_value=20_000.0), now=NOW)
    big = score_order(order(budget_value=200_000.0), now=NOW)
    assert big.money > small.money
    assert big.total > small.total


def test_budget_below_floor_scores_lower_than_floor():
    cheap = score_order(order(budget_value=5_000.0), now=NOW)
    at_floor = score_order(order(budget_value=20_000.0), now=NOW)
    assert cheap.money < at_floor.money


def test_negotiable_order_gets_neutral_money_until_estimated():
    """Пока модель не оценила работу, договорной заказ судить по деньгам нечем."""
    score = score_order(order(budget_value=None), now=NOW)
    assert score.money_is_estimate is True
    assert score.money == pytest.approx(MONEY_MAX / 2)


def test_our_estimate_counts_as_money():
    estimated = order(budget_value=None, raw={"estimated_budget": 150_000})
    score = score_order(estimated, now=NOW)
    assert score.money > MONEY_MAX / 2
    assert score.money_is_estimate is True


def test_order_budget_prefers_client_number():
    assert order_budget(order(budget_value=30_000.0)) == 30_000.0
    assert order_budget(order(budget_value=None, raw={"estimated_budget": 12_000})) == 12_000.0
    assert order_budget(order(budget_value=None)) is None
    assert order_budget(order(budget_value=None, raw={"estimated_budget": "чепуха"})) is None


# --- Витрина ---------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    ["Нужен лендинг", "Настроить CRM", "Телеграм-бот для записи", "Интеграция с 1С по API"],
)
def test_showcase_orders_get_full_service_points(title):
    score = score_order(order(title=title, description="", category=""), now=NOW)
    assert score.service == SERVICE_MAX, score.explain()


def test_plain_dev_order_scores_lower_than_showcase():
    showcase = score_order(order(title="Нужен сайт", description="", category=""), now=NOW)
    plain = score_order(
        order(title="Рефакторинг легаси-модуля", description="", category=""), now=NOW
    )
    assert plain.service < showcase.service
    assert plain.service > 0  # заказ всё равно наш, просто продавать дольше


# --- Свежесть --------------------------------------------------------------

def test_fresh_order_beats_stale_one():
    fresh = score_order(order(published_at=NOW - timedelta(minutes=5)), now=NOW)
    stale = score_order(order(published_at=NOW - timedelta(hours=5)), now=NOW)
    assert fresh.freshness == FRESH_MAX
    assert stale.freshness < fresh.freshness


def test_very_old_order_gets_no_freshness_but_stays_valid():
    old = score_order(order(published_at=NOW - timedelta(days=3)), now=NOW)
    assert old.freshness == 0
    assert old.total > 0


def test_unknown_date_is_neither_rewarded_nor_punished():
    unknown = score_order(order(published_at=None), now=NOW)
    assert 0 < unknown.freshness < FRESH_MAX


def test_naive_datetime_does_not_crash():
    naive = order(published_at=datetime(2026, 8, 6, 11, 0))
    assert score_order(naive, now=NOW).freshness > 0


# --- Доверие: вес намеренно маленький --------------------------------------

def test_trust_cannot_outweigh_money():
    """Заказчик с отзывами и первым местом не должен перебивать крупный бюджет."""
    trusted_but_small = score_order(
        order(budget_value=20_000.0, raw={"reviews": 50, "phone_confirmed": True, "bid_rank": 1}),
        now=NOW,
    )
    rich_but_unknown = score_order(order(budget_value=200_000.0, raw={}), now=NOW)
    assert rich_but_unknown.total > trusted_but_small.total


def test_first_in_queue_beats_tenth():
    first = score_order(order(raw={"bid_rank": 1}), now=NOW)
    tenth = score_order(order(raw={"bid_rank": 10}), now=NOW)
    assert first.trust > tenth.trust


def test_empty_profile_is_not_punished_into_zero():
    assert score_order(order(raw={}), now=NOW).total > 0


# --- Порог -----------------------------------------------------------------

def test_hot_when_fresh_good_money_and_our_profile():
    """Свежий заказ на 45 000 ₽ по нашей витрине обязан улетать сразу."""
    score = score_order(order(budget_value=45_000.0), now=NOW)
    assert score.is_hot(7.0), score.explain()


def test_minimal_budget_order_waits_for_evening():
    """Заказ ровно по нижней границе — не повод тратить лимит в обед."""
    score = score_order(order(budget_value=20_000.0), now=NOW)
    assert not score.is_hot(7.0), score.explain()


def test_explain_mentions_every_part():
    text = score_order(order(), now=NOW).explain()
    for word in ("деньги", "витрина", "свежесть", "доверие"):
        assert word in text


def test_is_hot_uses_threshold():
    score = Score(money=3.0, service=2.0, freshness=2.0, trust=0.0)
    assert score.is_hot(7.0) is True
    assert score.is_hot(7.1) is False

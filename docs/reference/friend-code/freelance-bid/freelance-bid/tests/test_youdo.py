"""Разбор доски Юду. Все образцы сняты с живой страницы 06.08.2026."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fbid.sources.youdo import (
    YoudoSource,
    is_blocked,
    parse_age,
    parse_budget,
    parse_card,
    parse_task_body,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

REMOTE_CARD = """Создать сайт для НКО на Tilda (соисполнитель)
Можно выполнить удаленно
Начать 6 августа, 3:16
до 15 000 ₽
Елена С.
1"""

LOCAL_CARD = """Демонтаж стяжки пола
КП Александровы пруды
Начать 12 августа, 10:10
до 145 000 ₽
Елена
Нет отзывов"""


# --- Бюджет ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("до 4 500 ₽", 4500.0),
        ("до 145 000 ₽", 145000.0),
        ("до 12 000 руб.", 12000.0),
        ("цена договорная", None),
        ("", None),
    ],
)
def test_parse_budget(text, expected):
    assert parse_budget(text)[1] == expected


def test_missing_budget_is_none_not_zero():
    """Ноль вместо «не указано» выкинул бы все договорные заказы порогом."""
    assert parse_budget("Начать 6 августа")[1] is None


# --- Карточка --------------------------------------------------------------

def test_remote_card_is_parsed():
    card = parse_card(REMOTE_CARD)
    assert card.title == "Создать сайт для НКО на Tilda (соисполнитель)"
    assert card.budget_value == 15000.0
    assert card.remote is True
    assert card.client_name == "Елена С."
    assert card.reviews == 1


def test_local_card_is_marked_not_remote():
    card = parse_card(LOCAL_CARD)
    assert card.remote is False
    assert card.reviews == 0
    assert card.client_name == "Елена"


def test_empty_card_does_not_crash():
    assert parse_card("").title == ""
    assert parse_card("   \n  ").title == ""


# --- Возраст задания -------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("Создано 1 ч. 4 мин. назад", timedelta(hours=1, minutes=4)),
        ("Создано 12 мин. назад", timedelta(minutes=12)),
        ("Создано 2 дн. назад", timedelta(days=2)),
    ],
)
def test_parse_age(text, delta):
    assert parse_age(text, now=NOW) == NOW - delta


def test_unparsed_age_is_none():
    """Не разобрали — пусть заказ считается без даты, а не свежим по ошибке."""
    assert parse_age("Открыто, 3 просмотра", now=NOW) is None


# --- Страница задания ------------------------------------------------------

def test_task_body_is_extracted():
    page = "Бюджет\nБольшой — до 4 500 ₽\nНужно\nСобрать каталог с выгрузкой\nв 1С\nОткликнуться"
    assert parse_task_body(page) == "Собрать каталог с выгрузкой\nв 1С"


def test_missing_body_returns_empty():
    assert parse_task_body("совсем другая страница") == ""


# --- Защита площадки -------------------------------------------------------

def test_block_page_is_detected():
    assert is_blocked("YouDo 403 Forbidden Доступ к youdo.com ограничен") is True
    assert is_blocked("Все задания на сервисе") is False


# --- Заказ из карточки -----------------------------------------------------

def test_card_becomes_order():
    order = YoudoSource._card_to_order(
        {"href": "/t15052001?searchRequestId=abc", "text": REMOTE_CARD}
    )
    assert order is not None
    assert order.ext_id == "15052001"
    assert order.url == "https://youdo.com/t15052001"
    assert order.budget_value == 15000.0
    assert order.raw["remote"] is True


def test_card_without_link_is_skipped():
    assert YoudoSource._card_to_order({"href": "/promo", "text": REMOTE_CARD}) is None


def test_youdo_cannot_bid_yet():
    """Форма отклика не откалибрована — площадка не должна тратить деньги."""
    assert YoudoSource.can_bid is False


def test_youdo_not_in_bidding_sources(monkeypatch):
    from fbid import config
    from fbid.pipeline import bidding_sources

    monkeypatch.setenv("YOUDO_ENABLED", "true")
    monkeypatch.setenv("PROFI_ENABLED", "false")
    config.get_settings.cache_clear()
    try:
        names = [adapter.name for adapter, _ in bidding_sources()]
        assert "youdo" not in names
    finally:
        config.get_settings.cache_clear()

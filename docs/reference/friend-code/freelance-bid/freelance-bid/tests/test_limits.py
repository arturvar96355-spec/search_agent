"""Тесты лимитов, которые правятся из Telegram.

Каждая проверка здесь про деньги: лимит, который можно поднять кнопкой без
границы, — это открытый кран к балансу Profi.ru.
"""
from __future__ import annotations

import pytest

from fbid import limits
from fbid.store import Store
from fbid.tgbot import Action, apply_pending_limit, handle


@pytest.fixture()
def store():
    """База ровно по тому пути, что в настройках.

    Это важно: фильтры и часть кода читают лимиты своим соединением по
    settings.db_path, и если тест пишет в другой файл, правка «не доезжает» —
    ровно как было бы в бою, если бы бот и цикл смотрели в разные базы.
    """
    from fbid.config import get_settings

    s = Store(get_settings().db_path)
    yield s
    s.close()


# --- Значения по умолчанию и правки ---------------------------------------

def test_without_override_value_comes_from_env(store):
    # В тестах .env подменён conftest: PROFI_DAILY_BUDGET=1000.
    assert limits.get("profi_daily_budget", store) == 1000
    assert limits.overridden("profi_daily_budget", store) is False


def test_override_wins_over_env(store):
    limits.set_value(store, "profi_daily_budget", 2500)
    assert limits.get("profi_daily_budget", store) == 2500
    assert limits.overridden("profi_daily_budget", store) is True


def test_reset_returns_to_env(store):
    limits.set_value(store, "profi_daily_budget", 2500)
    limits.reset(store, "profi_daily_budget")
    assert limits.get("profi_daily_budget", store) == 1000
    assert limits.overridden("profi_daily_budget", store) is False


# --- Границы: главное здесь -----------------------------------------------

def test_value_above_ceiling_is_clamped(store):
    """Лишний ноль в сообщении не должен открыть кран к балансу."""
    applied = limits.set_value(store, "profi_daily_budget", 200_000)
    spec = limits.SPECS["profi_daily_budget"]
    assert applied == spec.maximum
    assert limits.get("profi_daily_budget", store) == spec.maximum


def test_negative_value_is_clamped_to_minimum(store):
    assert limits.set_value(store, "profi_max_bid_price", -50) == 0


def test_integer_limits_stay_integer(store):
    assert limits.set_value(store, "profi_daily_bids", 7.9) == 7


def test_broken_value_in_db_falls_back_to_env(store):
    store.set_state(limits.PREFIX + "profi_daily_budget", "непонятно")
    assert limits.get("profi_daily_budget", store) == 1000


def test_unknown_limit_raises():
    with pytest.raises(KeyError):
        limits.get("нет_такого")


# --- Разбор присланного числа ---------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [("1500", 1500), ("1 500", 1500), ("2500 ₽", 2500), ("1,5", 1.5), ("  700  ", 700)],
)
def test_to_number_understands_human_input(text, expected):
    assert limits.to_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "много", "₽", "-"])
def test_to_number_rejects_nonsense(text):
    with pytest.raises(ValueError):
        limits.to_number(text)


# --- Минимальный бюджет живёт в filters.yaml, но правится из бота ----------

def test_min_budget_override_reaches_filters(store):
    from fbid.filters import FilterSet

    limits.set_value(store, "min_budget", 35_000)
    assert FilterSet.load().common.min_budget == 35_000

    limits.reset(store, "min_budget")
    assert FilterSet.load().common.min_budget == 20_000


# --- Экран лимитов в боте --------------------------------------------------

def test_limits_screen_lists_every_limit(store):
    """Каждый лимит должен быть кнопкой — иначе до него не добраться."""
    from fbid.tgbot import LIMIT_ORDER, limits_keyboard

    buttons = limits_keyboard(store)["inline_keyboard"]
    codes = {b["callback_data"] for row in buttons for b in row}
    for key in limits.SPECS:
        assert f"limit:{key}" in codes, f"лимит {key} недоступен из бота"
    assert len(LIMIT_ORDER) == len(limits.SPECS)


def test_limits_screen_marks_overridden(store):
    from fbid.tgbot import limits_keyboard

    limits.set_value(store, "profi_daily_budget", 1200)
    labels = [b["text"] for row in limits_keyboard(store)["inline_keyboard"] for b in row]
    assert any("1200" in label and "✏️" in label for label in labels)


def test_button_flow_changes_limit(store):
    """Нажал кнопку → прислал число → лимит изменился."""
    text, _ = handle(Action("limit", "profi_daily_budget"), store)
    assert "Пришли новое значение" in text

    answer, _ = apply_pending_limit(store, "1750")
    assert "1750" in answer
    assert limits.get("profi_daily_budget", store) == 1750


def test_non_number_does_not_change_limit(store):
    handle(Action("limit", "profi_daily_budget"), store)
    answer, _ = apply_pending_limit(store, "как дела")
    assert "не число" in answer
    assert limits.get("profi_daily_budget", store) == 1000


def test_clamped_value_is_reported_honestly(store):
    """Владелец должен узнать, что его цифру урезали до потолка."""
    handle(Action("limit", "profi_daily_budget"), store)
    answer, _ = apply_pending_limit(store, "999999")
    assert "20000" in answer.replace(" ", "")
    assert "допустимо" in answer.lower()


def test_message_without_pending_edit_is_not_swallowed(store):
    assert apply_pending_limit(store, "1750") is None


def test_cancel_forgets_pending_edit(store):
    handle(Action("limit", "profi_daily_budget"), store)
    handle(Action("limit_cancel"), store)
    assert apply_pending_limit(store, "1750") is None
    assert limits.get("profi_daily_budget", store) == 1000


def test_stale_pending_edit_expires(store, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from fbid import tgbot

    handle(Action("limit", "profi_daily_budget"), store)
    old = (datetime.now(timezone.utc) - timedelta(seconds=tgbot.PENDING_TTL + 60)).isoformat()
    store.set_state(tgbot.PENDING_KEY, f"profi_daily_budget|{old}")

    assert apply_pending_limit(store, "1750") is None
    assert limits.get("profi_daily_budget", store) == 1000


def test_reset_button_clears_override(store):
    limits.set_value(store, "profi_daily_budget", 1750)
    text, _ = handle(Action("limit_reset", "profi_daily_budget"), store)
    assert "1000" in text
    assert limits.get("profi_daily_budget", store) == 1000


def test_broken_value_is_not_shown_as_edited(store):
    """Битая запись — это не «правка из бота»: get() её всё равно игнорирует."""
    store.set_state(limits.PREFIX + "profi_daily_budget", "непонятно")
    assert limits.overridden("profi_daily_budget", store) is False
    assert limits.get("profi_daily_budget", store) == 1000

    from fbid.tgbot import limits_keyboard

    labels = [b["text"] for row in limits_keyboard(store)["inline_keyboard"] for b in row]
    budget_label = next(label for label in labels if "Бюджет Profi" in label)
    assert "✏️" not in budget_label


# --- Не заданный порог бюджета не должен выглядеть заданным --------------------

def test_missing_budget_bound_is_shown_as_unset(tmp_path, monkeypatch):
    """В filters.yaml порога нет — фильтр по бюджету не режет вовсе.

    Бот при этом показывал заводское «20 000 ₽», и владелец искал ошибку в
    фильтрах, глядя на заказы за пятьсот рублей, которые «не должны были
    пройти». Экран обязан говорить то же, что делает код.
    """
    from fbid import limits
    from fbid.filters import FilterSet

    empty = tmp_path / "filters.yaml"
    empty.write_text("common:\n  min_budget: null\n  max_budget: null\n", encoding="utf-8")
    monkeypatch.setattr("fbid.filters.DEFAULT_FILTERS_PATH", empty)

    assert FilterSet.load(empty).common.min_budget is None, "фильтр не режет"
    assert limits.in_force("min_budget") is None
    assert limits.display("min_budget") == "не задан"


def test_a_real_bound_is_still_shown_with_the_number():
    from fbid import limits

    assert limits.SPECS["min_budget"].format(20_000) == "20000 ₽"


def test_zero_is_shown_as_zero_not_as_unset(store):
    """«Не задан» и «ноль» ведут себя противоположно — путать их нельзя.

    При пустом max_budget фильтр не режет ничего, при нулевом — режет ВСЁ.
    Владелец ставил ноль кнопкой, видел «не задан» и потом искал, почему ни
    один заказ не проходит.
    """
    from fbid import limits

    limits.set_value(store, "max_budget", 0)

    assert limits.in_force("max_budget", store) == 0.0
    assert limits.display("max_budget", store) == "0 ₽"

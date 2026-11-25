"""Что дёргает владельца сразу, а что ждёт вечерней сводки.

Главное здесь — тихое событие не пропадает. Молчание в этом реестре про
время доставки, а не про сокрытие: строка ложится в журнал дня и вечером
выходит в итогах.
"""
from __future__ import annotations

import pytest

from orbit import alerts, digest, notify


@pytest.fixture
def telegram(monkeypatch):
    """Перехватывает всё, что ушло бы в телеграм."""
    said: list[str] = []
    monkeypatch.setattr(notify, "tell", lambda text, **kw: said.append(text) or True)
    return said


def test_urgent_alert_goes_straight_to_telegram(store, telegram):
    alerts.deliver(store, alerts.MODEL_DOWN, "Модель молчит")
    assert telegram == ["Модель молчит"]


def test_quiet_alert_waits_for_the_evening(store, telegram):
    alerts.deliver(store, alerts.BID_SENT, "Отклик на заказ про сайт")
    assert telegram == [], "по отправленному отклику владельца не дёргаем"
    assert store.quiet_notes() == ["Отклик на заказ про сайт"]


def test_the_owner_can_switch_any_alert_both_ways(store, telegram):
    """Переключатель работает в обе стороны: это его смысл."""
    alerts.set_instant(store, alerts.BID_SENT, True)
    alerts.deliver(store, alerts.BID_SENT, "теперь дёргаем")
    assert telegram == ["теперь дёргаем"]

    alerts.set_instant(store, alerts.MODEL_DOWN, False)
    alerts.deliver(store, alerts.MODEL_DOWN, "теперь молчим")
    assert telegram == ["теперь дёргаем"], "выбор владельца важнее умолчания"
    assert "теперь молчим" in store.quiet_notes()


def test_undelivered_urgent_alert_falls_back_to_the_digest(store, monkeypatch):
    """Телеграм не ответил — событие не теряем, отдаём вечерней сводке.

    Потерять срочное сообщение совсем хуже, чем показать его позже: иначе
    падение сети на одну минуту стирает единственный след о поломке.
    """
    monkeypatch.setattr(notify, "tell", lambda text, **kw: False)
    alerts.deliver(store, alerts.MAILBOX_DOWN, "Ящик не читается")
    assert store.quiet_notes() == ["Ящик не читается"]


def test_every_alert_has_its_own_code():
    """Два события с одним кодом делили бы один переключатель."""
    codes = [alert.code for alert in alerts.ALL]
    assert len(codes) == len(set(codes)), codes


def test_a_quiet_day_with_quiet_notes_is_not_a_silent_day(store, monkeypatch):
    """День без событий, но со снятым адресом — не повод промолчать.

    Иначе тихий канал становится ямой: владелец не дёрнут сейчас и не узнает
    потом, и «мы решили сказать вечером» превращается в «не сказали вовсе».
    """
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)
    assert digest.build(store) is None, "совсем пустой день — молчим"

    alerts.deliver(store, alerts.ADDRESS_BAD, "🚫 Адрес не годится\nМедиасфера")
    text = digest.build(store)
    assert text is not None, "сводка обязана уйти"
    assert "Без сообщений за день" in text
    assert "Медиасфера" in text


def test_multiline_card_becomes_one_line_in_the_digest(store, monkeypatch):
    """Карточки писались для телеграма поодиночке — в сводке их десяток."""
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)
    alerts.deliver(
        store, alerts.LETTER_DROPPED,
        "🗑 Письмо снял с отправки\nМедиасфера\nПричина: 550 User unknown\n\nДальше нельзя.",
    )
    text = digest.build(store)
    assert "· 🗑 Письмо снял с отправки — Медиасфера" in text
    assert "550 User unknown" not in text, "подробности читаются в карточке контакта"


def test_exchange_notes_reach_the_digest_too(store, monkeypatch):
    """Отложенное с бирж живёт в чужой базе — сводка обязана его забрать.

    Без этого половина тихого канала проваливалась бы в никуда: отклики
    считает fbid, а сводку собирает Orbit.
    """
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)
    from orbit.sections import freelance

    monkeypatch.setattr(freelance, "quiet_notes_now", lambda day: ["📨 Отклик — Сайт на Tilda"])

    text = digest.build(store)
    assert text is not None
    assert "Сайт на Tilda" in text


def test_a_broken_exchange_base_does_not_break_the_digest(store, monkeypatch):
    """База бирж недоступна — сводка всё равно уходит, просто без её строк."""
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)
    from orbit.sections import freelance

    def broken(_day):
        raise RuntimeError("база занята")

    monkeypatch.setattr(freelance, "quiet_notes_now", broken)
    alerts.deliver(store, alerts.ADDRESS_BAD, "🚫 Адрес не годится\nМедиасфера")

    text = digest.build(store)
    assert text is not None, "своя половина сводки от чужой базы не зависит"
    assert "Медиасфера" in text

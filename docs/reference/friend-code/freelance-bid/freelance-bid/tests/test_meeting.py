"""Договорённость о созвоне, выловленная из переписки.

Переписку ведёт владелец сам, и время созвона всплывает посреди разговора.
Нигде не записанное, через день оно теряется среди пяти других разговоров.

Здесь проверяется две вещи: что дешёвый отсев не пускает к модели лишнего
(разбор стоит денег) и что перенос созвона — это новость, а не повтор.
"""
from __future__ import annotations

from typing import ClassVar

import pytest

from fbid import meeting
from fbid.models import Order, Source


@pytest.fixture
def store(tmp_path):
    """Своя база на тест: отметки «уже сказали» живут именно в ней."""
    from fbid.store import Store

    opened = Store(tmp_path / "fbid.db")
    try:
        yield opened
    finally:
        opened.close()


def order() -> Order:
    return Order(
        source=Source.PROFI, ext_id="777",
        url="https://profi.ru/backoffice/n.php?o=777",
        title="Сайт на Tilda", description="Нужен лендинг",
    )


# --- Дешёвый отсев до модели -------------------------------------------------

@pytest.mark.parametrize("talk", [
    "Мы: Готов созвониться\n\nКлиент: Давайте завтра в 18:00",
    "Мы: Когда удобно?\n\nКлиент: Наберите в 17:30",
    "Клиент: давайте в понедельник созвонимся",
    "Мы: скину ссылку на zoom\n\nКлиент: да, в 15.00 ок",
])
def test_a_call_with_a_time_goes_to_the_model(talk):
    assert meeting.looks_like_a_call(talk)


@pytest.mark.parametrize(("name", "talk"), [
    ("сроки проекта, а не звонок",
     "Клиент: нужно сделать за 3 дня, к 15 октября\n\nМы: успеем"),
    ("созвон без времени",
     "Клиент: давайте созвонимся, когда будет ТЗ"),
    ("просто обсуждение задачи",
     "Клиент: сколько стоит лендинг на тильде?\n\nМы: от 40 000 ₽"),
    ("пустая переписка", ""),
])
def test_the_model_is_not_called_for_nothing(name, talk):
    """Каждый лишний разбор — деньги. Оба признака обязательны."""
    assert not meeting.looks_like_a_call(talk), name


def test_only_the_tail_of_a_long_talk_is_sent():
    """Договорённость живёт в конце разговора — платить за начало незачем."""
    talk = "\n\n".join(f"Клиент: сообщение {n}" for n in range(30))
    sent = meeting.tail(talk, limit=5)
    assert sent.count("\n\n") == 4
    assert "сообщение 29" in sent
    assert "сообщение 25" in sent
    assert "сообщение 24" not in sent


# --- Что уже сказали ---------------------------------------------------------

def test_the_same_agreement_is_told_once(store):
    found = meeting.Meeting(when="завтра в 18:00")
    assert not meeting.already_told(store, order(), found)
    meeting.remember(store, order(), found)
    assert meeting.already_told(store, order(), found)


def test_a_moved_call_is_news_again(store):
    """Перенос — новость не меньшая, чем сама договорённость.

    Промолчав о нём, мы отправим владельца к старому времени: это хуже, чем
    если бы мы не говорили вовсе.
    """
    meeting.remember(store, order(), meeting.Meeting(when="завтра в 18:00"))
    moved = meeting.Meeting(when="завтра в 19:30")
    assert not meeting.already_told(store, order(), moved)


def test_the_card_puts_the_time_first():
    """Владелец читает это с телефона: унести он должен ровно время."""
    from fbid import notify

    card = notify.meeting_card(order(), "завтра в 18:00 · zoom")
    assert "ДОГОВОРИЛИСЬ О СОЗВОНЕ" in card
    assert "<b>завтра в 18:00 · zoom</b>" in card
    assert "Открыть переписку" in card

    again = notify.meeting_card(order(), "в 19:30", moved=True)
    assert "ПЕРЕНЕСЛИ" in again, "второе сообщение не должно читаться как повтор"

    waiting = notify.meeting_card(order(), "сегодня в 18:00", confirmed=False)
    assert "КЛИЕНТ ЗОВЁТ" in waiting
    assert "не подтвердили" in waiting, "владелец должен понять, что ждут его"


def test_the_line_keeps_only_what_is_known():
    assert meeting.Meeting(when="в 18:00").line == "в 18:00"
    assert meeting.Meeting(when="в 18:00", how="zoom").line == "в 18:00 · zoom"
    assert meeting.Meeting(
        when="в 18:00", how="телефон", who="клиент звонит"
    ).line == "в 18:00 · телефон · клиент звонит"


# --- Разбор ответа модели ------------------------------------------------------

def test_agreement_without_a_time_is_not_an_agreement(monkeypatch):
    """«Договорились», но когда — не сказано: звонить всё равно нечему."""
    monkeypatch.setattr(meeting, "model_trouble", lambda: None)
    monkeypatch.setattr(meeting, "make_client", lambda **kw: _fake_client(
        '{"agreed": true, "confirmed": true, "when": "", "who": "", "how": ""}'
    ))
    talk = "Мы: созвонимся?\n\nКлиент: да, давайте завтра в 18:00"
    assert meeting.find(order(), talk) is None


def test_a_real_agreement_comes_back_parsed(monkeypatch):
    monkeypatch.setattr(meeting, "model_trouble", lambda: None)
    monkeypatch.setattr(meeting, "make_client", lambda **kw: _fake_client(
        '{"agreed": true, "confirmed": true, "when": "завтра в 18:00",'
        ' "who": "", "how": "zoom"}'
    ))
    talk = "Мы: могу в 17:30\n\nКлиент: давайте в 18:00, zoom"
    found = meeting.find(order(), talk)
    assert found is not None
    assert found.when == "завтра в 18:00"
    assert found.how == "zoom"
    assert found.confirmed


def test_a_time_named_by_the_client_alone_still_reaches_the_owner(monkeypatch):
    """Клиент назвал время, мы не ответили — сказать надо, и срочнее всего.

    Живой случай: «готов в 17:30» — «в 18:00 давайте». Формально это ещё не
    договорённость, обе стороны не подтвердили. Но ждут при этом НАС, и
    промолчать здесь значит промолчать ровно тогда, когда нужен человек.
    """
    monkeypatch.setattr(meeting, "model_trouble", lambda: None)
    monkeypatch.setattr(meeting, "make_client", lambda **kw: _fake_client(
        '{"agreed": true, "confirmed": false, "when": "сегодня в 18:00",'
        ' "who": "", "how": ""}'
    ))
    talk = "Мы: готов созвониться в 17:30\n\nКлиент: В 18:00 давайте"
    found = meeting.find(order(), talk)
    assert found is not None and not found.confirmed


def test_a_reschedule_needs_no_call_word(monkeypatch):
    """«Давайте лучше в 19:30» — переносят так, без слова «созвон».

    Пока про созвон по заказу уже говорили, одного времени достаточно: иначе
    перенос прошёл бы мимо дешёвого отсева, а это худшее из молчаний —
    владелец придёт к старому времени.
    """
    talk = "Мы: договорились, завтра в 18:00\n\nКлиент: давайте лучше в 19:30"
    assert not meeting.looks_like_a_call(talk)
    assert meeting.looks_like_a_call(talk, known=True)


def test_a_silent_model_costs_nothing(monkeypatch):
    """Модель уже молчит — второго запроса не делаем."""
    monkeypatch.setattr(meeting, "model_trouble", lambda: "кончились деньги")

    def forbidden(**kw):
        raise AssertionError("модель звать не должны")

    monkeypatch.setattr(meeting, "make_client", forbidden)
    assert meeting.find(order(), "Мы: созвон завтра в 18:00\n\nКлиент: да") is None


def _fake_client(answer: str):
    class Block:
        type = "text"
        text = answer

    class Response:
        content: ClassVar = [Block()]
        stop_reason = "end_turn"

    class Messages:
        def create(self, **kw):
            return Response()

    class Client:
        messages = Messages()

    return Client()

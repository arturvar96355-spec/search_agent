"""Разбор ответа: интерес, отказ, «позже», не тот человек.

Модель здесь подменена: проверяется не она, а что мы делаем с её ответом.
Цена ошибки в обе стороны разная и обе неприятные — закрыть контакт, который
на самом деле заинтересовался, или продолжать долбить того, кто отказался.
Поэтому всё, в чём модель не уверена, должно доходить до владельца целиком и
ничего не менять в базе.
"""
from __future__ import annotations

import pytest

from orbit import inbox, llm
from orbit.llm import ReplyVerdict
from orbit.models import Contact, Kind, Stage


@pytest.fixture()
def agency(store) -> Contact:
    contact = Contact(
        kind=Kind.PARTNER, key="mediasfera.ru", name="Медиасфера",
        emails=["hello@mediasfera.ru"], stage=Stage.WRITTEN,
    )
    contact.pk = store.upsert_contact(contact)
    return store.get_contact(contact.pk)


@pytest.fixture()
def says(monkeypatch):
    """Модель отвечает тем, что попросил тест."""

    def answer(kind: str, **fields):
        verdict = ReplyVerdict(kind=kind, **fields)
        monkeypatch.setattr(llm, "classify_reply", lambda *a, **kw: verdict)
        return verdict

    return answer


def letter(**kwargs) -> inbox.Letter:
    data = {
        "uid": 1,
        "sender": "hello@mediasfera.ru",
        "subject": "Re: Вопрос по сотрудничеству",
        "body": "Здравствуйте! Расскажите подробнее.",
    }
    data.update(kwargs)
    return inbox.Letter(**data)


# --- Интерес ------------------------------------------------------------------


def test_interest_starts_the_conversation(store, agency, says):
    says(llm.INTEREST, reason="просит подробности")
    text = inbox.handle(store, letter())
    assert "Интересно" in text
    assert store.get_contact(agency.pk).stage is Stage.REPLIED


def test_interest_says_who_answers(store, agency, says):
    """Бот в такой разговор не лезет: отвечает владелец своими словами."""
    says(llm.INTEREST)
    assert "владелец" in inbox.handle(store, letter())


# --- Отказ --------------------------------------------------------------------


def test_refusal_closes_the_contact(store, agency, says):
    says(llm.REFUSAL, reason="есть свои разработчики")
    text = inbox.handle(store, letter(body="Спасибо, у нас свои разработчики."))
    assert "Отказ" in text
    assert store.get_contact(agency.pk).stage is Stage.DEAD


def test_refusal_reason_is_kept(store, agency, says):
    says(llm.REFUSAL, reason="есть свои разработчики")
    inbox.handle(store, letter())
    events = store.events(agency.pk)
    assert any(row["kind"] == "refusal" for row in events)


def test_refusal_stops_the_followup_chain(store, agency, says):
    """Закрытый контакт выпадает из цепочки касаний — это и есть смысл отказа."""
    from orbit import followup

    says(llm.REFUSAL)
    inbox.handle(store, letter())
    assert store.get_contact(agency.pk) not in followup.due(store, Kind.PARTNER)


# --- «Позже» ------------------------------------------------------------------


def test_later_remembers_the_date(store, agency, says):
    says(llm.LATER, when="после Нового года", when_date="2027-01-10")
    text = inbox.handle(store, letter(body="Вернитесь после Нового года."))
    contact = store.get_contact(agency.pk)
    assert contact.meta[inbox.REMIND_KEY] == "2027-01-10"
    assert "Вернуться" in text


def test_later_keeps_the_contact_alive(store, agency, says):
    says(llm.LATER, when="осенью", when_date="2026-09-01")
    inbox.handle(store, letter())
    assert store.get_contact(agency.pk).stage is Stage.REPLIED


def test_later_without_a_date_is_still_marked(store, agency, says):
    """Срок не назвали — это всё равно «позже», просто напоминать не по чему."""
    says(llm.LATER, reason="сейчас не до этого")
    text = inbox.handle(store, letter())
    assert store.get_contact(agency.pk).meta[inbox.REMIND_KEY] == ""
    assert "срок не назвали" in text


def test_reminder_surfaces_on_its_day(store, agency, says):
    says(llm.LATER, when="в сентябре", when_date="2026-09-01")
    inbox.handle(store, letter())

    assert store.due_reminders("2026-08-31") == []
    due = store.due_reminders("2026-09-01")
    assert [c.name for c in due] == ["Медиасфера"]


def test_reminder_shows_the_words_of_the_person(store, agency, says):
    from orbit import digest

    says(llm.LATER, when="после Нового года", when_date="2026-09-01")
    inbox.handle(store, letter())
    text = digest.build(store, day="2026-09-01") or ""
    assert "после Нового года" in text


def test_closed_contact_is_not_reminded_about(store, agency, says):
    """Передумали и закрыли контакт — напоминание про него не всплывает."""
    says(llm.LATER, when="в сентябре", when_date="2026-09-01")
    inbox.handle(store, letter())
    store.set_stage(agency.pk, Stage.DEAD, "передумали")
    assert store.due_reminders("2026-09-01") == []


# --- Не тот человек -----------------------------------------------------------


def test_wrong_person_does_not_close_the_contact(store, agency, says):
    says(llm.WRONG_PERSON, hint="Пётр, p.ivanov@mediasfera.ru")
    text = inbox.handle(store, letter(body="Я этим не занимаюсь, напишите Петру."))
    assert "не тот человек" in text.lower()
    assert store.get_contact(agency.pk).stage is Stage.REPLIED


def test_wrong_person_shows_where_they_sent_us(store, agency, says):
    says(llm.WRONG_PERSON, hint="p.ivanov@mediasfera.ru")
    assert "p.ivanov@mediasfera.ru" in inbox.handle(store, letter())


def test_new_address_is_not_written_to_by_itself(store, agency, says):
    """Адрес из чужого письма в базу не кладём: это решение владельца."""
    says(llm.WRONG_PERSON, hint="p.ivanov@mediasfera.ru")
    inbox.handle(store, letter())
    assert store.get_contact(agency.pk).emails == ["hello@mediasfera.ru"]


# --- Когда разбор не удался ---------------------------------------------------


def test_silent_model_keeps_the_old_behaviour(store, agency, monkeypatch):
    """Модель не ответила — ведём себя как раньше: показываем письмо владельцу."""
    monkeypatch.setattr(llm, "classify_reply", lambda *a, **kw: None)
    text = inbox.handle(store, letter())
    assert "Ответили на письмо" in text
    assert store.get_contact(agency.pk).stage is Stage.REPLIED


def test_broken_model_does_not_lose_the_letter(store, agency, monkeypatch):
    """Разбор упал — письмо всё равно доходит до владельца."""

    def explode(*args, **kwargs):
        raise RuntimeError("API прилёг")

    monkeypatch.setattr(llm, "classify_reply", explode)
    text = inbox.handle(store, letter())
    assert "Ответили на письмо" in text
    assert store.get_contact(agency.pk).stage is Stage.REPLIED


def test_unclear_answer_changes_nothing_extra(store, agency, says):
    says(llm.UNCLEAR, reason="не разобрать")
    text = inbox.handle(store, letter(body="?"))
    assert "Ответили на письмо" in text
    assert store.get_contact(agency.pk).stage is Stage.REPLIED


def test_stop_word_wins_over_classification(store, agency, monkeypatch):
    """«Не пишите нам» закрывает контакт до всякого разбора — и без запроса к модели."""

    def explode(*args, **kwargs):
        raise AssertionError("модель звать не должны")

    monkeypatch.setattr(llm, "classify_reply", explode)
    text = inbox.handle(store, letter(body="Стоп, не пишите нам больше."))
    assert "не писать" in text
    assert store.get_contact(agency.pk).stage is Stage.DEAD


# --- Что уходит в модель ------------------------------------------------------


def test_our_letter_goes_with_the_reply(store, agency, monkeypatch):
    """«Да, давайте» без нашего письма разбирать не по чему."""
    from orbit.models import Channel, Draft

    draft = Draft(contact_pk=agency.pk, channel=Channel.EMAIL, subject="Тема", body="Наш текст")
    store.save_draft(draft)
    store.mark_sent(store.draft_for(agency.pk).pk)

    seen: dict = {}
    monkeypatch.setattr(
        llm, "classify_reply",
        lambda contact, subject, body, our_letter=None: seen.update(our_letter=our_letter),
    )
    inbox.handle(store, letter())
    assert "Наш текст" in (seen.get("our_letter") or "")


def test_reply_text_comes_without_the_quote(store, agency, monkeypatch):
    """В модель уходит ответ, а не наше письмо, процитированное целиком."""
    seen: dict = {}
    monkeypatch.setattr(
        llm, "classify_reply",
        lambda contact, subject, body, our_letter=None: seen.update(body=body),
    )
    inbox.handle(store, letter(body="Нам это не нужно.\n\n> Наше исходное письмо"))
    assert "Нам это не нужно." in seen["body"]
    assert "исходное письмо" not in seen["body"]


# --- Разбор ответа модели -----------------------------------------------------


@pytest.mark.parametrize("kind", ["", "не знаю", "REFUSAL", "спам", None])
def test_unknown_kind_falls_back_to_unclear(kind):
    """Незнакомый тип — повод показать владельцу, а не закрыть контакт."""
    assert llm._to_verdict({"kind": kind}).kind == llm.UNCLEAR


def test_known_kinds_pass_through():
    for kind in llm.REPLY_KINDS:
        assert llm._to_verdict({"kind": kind}).kind == kind


def test_kind_is_case_insensitive():
    assert llm._to_verdict({"kind": " Отказ "}).kind == llm.REFUSAL


@pytest.mark.parametrize("raw", ["не знаю", "", "через месяц", "2026-13-40", "01.09.2026", None])
def test_broken_date_is_dropped(raw):
    """Лучше напоминание без срока, чем срок, взявшийся ниоткуда."""
    assert llm._to_verdict({"kind": llm.LATER, "when_date": raw}).when_date == ""


def test_good_date_is_kept():
    assert llm._to_verdict({"kind": llm.LATER, "when_date": "2026-09-01"}).when_date == "2026-09-01"


# --- Отказ не трогает то, что уже в работе ------------------------------------


@pytest.mark.parametrize("stage", [Stage.CALL, Stage.DEAL, Stage.REPLIED])
def test_refusal_does_not_close_a_live_deal(store, agency, says, stage):
    """«Нам это не нужно» в переписке о сделке значит не то, что в холодном письме.

    Закрывать разговор, созвон или заказ в работе по разбору письма нельзя:
    это решение владельца, он видит уведомление целиком.
    """
    store.set_stage(agency.pk, stage, "руками")
    says(llm.REFUSAL, reason="не нужно")
    text = inbox.handle(store, letter(body="Эту часть брать не будем."))
    assert "Отказ" in text                       # владельцу сказали
    assert store.get_contact(agency.pk).stage is stage   # но ничего не тронули


def test_refusal_is_logged_even_when_it_does_not_close(store, agency, says):
    store.set_stage(agency.pk, Stage.DEAL, "руками")
    says(llm.REFUSAL, reason="не нужно")
    inbox.handle(store, letter())
    assert any(row["kind"] == "refusal" for row in store.events(agency.pk))


def test_refusal_still_closes_a_cold_contact(store, agency, says):
    says(llm.REFUSAL, reason="есть свои разработчики")
    inbox.handle(store, letter())
    assert store.get_contact(agency.pk).stage is Stage.DEAD


# --- Второе «позже» не стирает первый срок ------------------------------------


def test_second_later_without_a_date_keeps_the_first(store, agency, says):
    """«Вернитесь в сентябре», потом «сейчас не до этого» — срок остаётся."""
    says(llm.LATER, when="в сентябре", when_date="2026-09-01")
    inbox.handle(store, letter())

    says(llm.LATER, reason="сейчас не до этого")
    inbox.handle(store, letter(uid=2, body="Сейчас совсем не до этого."))

    assert store.get_contact(agency.pk).meta[inbox.REMIND_KEY] == "2026-09-01"


def test_new_date_replaces_the_old_one(store, agency, says):
    says(llm.LATER, when="в сентябре", when_date="2026-09-01")
    inbox.handle(store, letter())
    says(llm.LATER, when="в декабре", when_date="2026-12-01")
    inbox.handle(store, letter(uid=2))
    assert store.get_contact(agency.pk).meta[inbox.REMIND_KEY] == "2026-12-01"


# --- Ответ с личного ящика ------------------------------------------------------


def test_reply_from_a_free_domain_hits_nobody(store, agency, says):
    """gmail.com — не компания, а полмира: по домену там сопоставлять нечего."""
    other = Contact(kind=Kind.CLIENT, key="other", name="Чужая компания",
                    emails=["director@gmail.com"], stage=Stage.WRITTEN)
    other.pk = store.upsert_contact(other)

    says(llm.REFUSAL, reason="не нужно")
    result = inbox.handle(store, letter(sender="kto-to@gmail.com", body="Нам не нужно."))
    assert result is None
    assert store.get_contact(other.pk).stage is Stage.WRITTEN


def test_reply_from_the_same_corporate_domain_still_matches(store, agency, says):
    """А вот директор с того же домена компании — это по-прежнему она."""
    says(llm.INTEREST)
    text = inbox.handle(store, letter(sender="d.petrov@mediasfera.ru"))
    assert text and "Медиасфера" in text


# --- Дата от модели --------------------------------------------------------------


def test_date_without_dashes_is_normalised():
    """«20260901» база сравнила бы как строку и срок не наступил бы никогда."""
    assert llm._to_verdict({"kind": llm.LATER, "when_date": "20260901"}).when_date == "2026-09-01"

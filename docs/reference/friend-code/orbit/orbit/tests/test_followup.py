"""Цепочка писем вдогонку.

Главное здесь — не переусердствовать. Цепочка идёт только по молчунам, каждое
касание ждёт своей паузы, а после последнего письма мы отстаём навсегда.
Второе по важности — касания не должны быть одинаковыми: у каждого свой повод,
и модель видит, что мы уже писали.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from orbit import followup, llm
from orbit.models import Channel, Contact, Draft, Kind, Stage
from orbit.store import MSK


@pytest.fixture(autouse=True)
def model_writes_something(monkeypatch):
    """Модель отвечает коротким письмом — сеть в тестах не нужна."""
    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact, channel=None, followup=None, earlier=None: llm.Letter(
            llm.VERDICT_WRITE, "молчит", "Короткий вопрос", "Здравствуйте! Тема ещё актуальна?"
        ),
    )


def written_partner(store, *, days_ago: float, name: str = "Медиасфера", key: str = "m.ru"):
    """Агентство, которому письмо ушло `days_ago` дней назад."""
    contact = Contact(
        kind=Kind.PARTNER, key=key, name=name, site=key,
        emails=[f"hello@{key}"], stage=Stage.WRITTEN,
    )
    contact.pk = store.upsert_contact(contact)
    draft_pk = store.save_draft(Draft(
        contact_pk=contact.pk, channel=Channel.EMAIL, subject="Тема", body="Первое письмо",
    ))
    when = (datetime.now(MSK) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    store.mark_sent(draft_pk)
    store.conn.execute("UPDATE drafts SET sent_at = ? WHERE pk = ?", (when, draft_pk))
    store.conn.commit()
    store.set_stage(contact.pk, Stage.WRITTEN, "написали")
    return store.get_contact(contact.pk)


def send_pending(store, contact):
    """Отправляет подготовленное касание.

    В жизни между двумя касаниями письмо уходит — либо само, либо кнопкой
    владельца. Пока оно висит в очереди, следующее касание не готовится:
    иначе цепочка сгорает, не отправив ни одного письма.
    """
    draft = store.draft_for(contact.pk)
    if draft is not None:
        store.mark_sent(draft.pk)
    return store.get_contact(contact.pk)


def age_marks(store, contact, *, days: float):
    """Отматывает назад и отправку, и отметку последнего касания."""
    when = (datetime.now(MSK) - timedelta(days=days)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE drafts SET sent_at = ? WHERE contact_pk = ?", (when, contact.pk))
    store.conn.commit()
    fresh = store.get_contact(contact.pk)
    if fresh.meta.get(followup.TOUCH_AT):
        fresh.meta[followup.TOUCH_AT] = when
        store.set_meta(fresh.pk, fresh.meta)
    return store.get_contact(contact.pk)


# --- Когда пора --------------------------------------------------------------

def test_partner_waits_three_days(store):
    """Агентствам даём три дня: решение у них проходит через нескольких человек."""
    contact = written_partner(store, days_ago=2)
    assert followup.due(store, Kind.PARTNER) == []

    age_marks(store, contact, days=3.1)
    assert len(followup.due(store, Kind.PARTNER)) == 1


def test_replied_contact_is_not_chased(store):
    """Ответил — значит разговор идёт, вдогонку писать нечего."""
    contact = written_partner(store, days_ago=5)
    store.set_stage(contact.pk, Stage.REPLIED, "ответили")
    assert followup.due(store, Kind.PARTNER) == []


def test_dead_contact_is_not_chased(store):
    contact = written_partner(store, days_ago=5)
    store.set_stage(contact.pk, Stage.DEAD, "отказ")
    assert followup.due(store, Kind.PARTNER) == []


def test_contact_without_channel_is_skipped(store):
    """Писать некуда — вдогонку тем более."""
    contact = written_partner(store, days_ago=5)
    store.conn.execute("UPDATE contacts SET emails = '[]' WHERE pk = ?", (contact.pk,))
    store.conn.commit()
    assert followup.due(store, Kind.PARTNER) == []


def test_client_is_chased_sooner_than_partner():
    """Компании с карт отвечают быстрее — им пишем раньше."""
    assert followup.CHAIN[Kind.CLIENT][0].after_days < followup.CHAIN[Kind.PARTNER][0].after_days


# --- Сама цепочка ------------------------------------------------------------

def test_second_touch_waits_its_own_pause(store):
    """Подготовили касание — следующее не наезжает на него на следующий день."""
    contact = written_partner(store, days_ago=5)
    followup.write_one(store, contact)
    contact = send_pending(store, contact)
    assert followup.due(store, Kind.PARTNER) == []

    contact = age_marks(store, contact, days=6)
    assert len(followup.due(store, Kind.PARTNER)) == 1


def test_chain_ends_and_never_comes_back(store):
    """После последнего касания контакт не всплывает больше никогда."""
    contact = written_partner(store, days_ago=5)
    for step in range(len(followup.CHAIN[Kind.PARTNER])):
        due = followup.due(store, Kind.PARTNER)
        assert len(due) == 1, f"касание {step + 1} не подошло"
        ok, _reason = followup.write_one(store, due[0])
        assert ok is True
        contact = send_pending(store, contact)
        contact = age_marks(store, contact, days=30)

    assert followup.due(store, Kind.PARTNER) == []
    assert followup.next_touch(store.get_contact(contact.pk)) is None


def test_unsent_touch_blocks_the_next_one(store):
    """Цепочка не должна сгорать, не отправив ни одного письма.

    Счётчик касаний растёт от подготовки. При выключенной автоотправке (а она
    выключена по умолчанию) три касания готовились по расписанию и висели в
    очереди, счётчик доходил до конца — и человек не получал вообще ничего.
    """
    contact = written_partner(store, days_ago=5)
    followup.write_one(store, contact)

    contact = age_marks(store, contact, days=30)
    assert followup.due(store, Kind.PARTNER) == []          # письмо ещё не ушло
    assert followup.step_of(store.get_contact(contact.pk)) == 1

    contact = send_pending(store, contact)
    contact = age_marks(store, contact, days=30)
    assert len(followup.due(store, Kind.PARTNER)) == 1      # ушло — можно дальше


def test_last_touch_says_it_is_the_last():
    chain = followup.CHAIN[Kind.PARTNER]
    assert chain[-1].last is True
    assert not any(touch.last for touch in chain[:-1])


def test_each_touch_has_its_own_angle():
    """Одинаковые поводы — это одно и то же письмо трижды."""
    for kind, chain in followup.CHAIN.items():
        angles = [touch.angle for touch in chain]
        assert len(set(angles)) == len(angles), f"повторяющийся повод у {kind.value}"


def test_touch_number_grows_in_the_card(store):
    contact = written_partner(store, days_ago=5)
    assert followup.step_of(contact) == 0

    followup.write_one(store, contact)
    assert followup.step_of(store.get_contact(contact.pk)) == 1


def test_old_single_followup_counts_as_first_touch(store):
    """У кого догоняли до цепочки — продолжаем со второго касания, а не с первого."""
    contact = written_partner(store, days_ago=9)
    contact.meta[followup.FOLLOWUP_KEY] = (
        datetime.now(MSK) - timedelta(days=6)
    ).isoformat(timespec="seconds")
    store.set_meta(contact.pk, contact.meta)

    fresh = store.get_contact(contact.pk)
    assert followup.step_of(fresh) == 1
    assert followup.next_touch(fresh) is followup.CHAIN[Kind.PARTNER][1]


# --- Что уходит в модель -----------------------------------------------------

def test_model_gets_touch_number_and_angle(store, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact_, channel=None, followup=None, earlier=None: seen.update(
            followup=followup, earlier=earlier
        ) or llm.Letter(llm.VERDICT_WRITE, "", "Тема", "Текст"),
    )
    contact = written_partner(store, days_ago=5)
    followup.write_one(store, contact)

    assert seen["followup"].number == 1
    assert seen["followup"].angle == followup.CHAIN[Kind.PARTNER][0].angle
    assert seen["earlier"] == ["Первое письмо"]


def test_brief_forbids_repeating_and_shows_earlier_letters():
    contact = Contact(kind=Kind.PARTNER, key="m.ru", name="Медиасфера", emails=["a@m.ru"])
    brief = llm.contact_brief(
        contact, Channel.EMAIL,
        followup=llm.Followup(number=3, angle="закрываем тему", last=True),
        earlier=["Первое письмо про субподряд"],
    )

    assert "вдогонку №3" in brief.lower()
    assert "закрываем тему" in brief
    assert "последнее письмо" in brief.lower()
    assert "не повторяй" in brief.lower()
    assert "субподряд" in brief


def test_first_letter_brief_has_no_followup_words():
    contact = Contact(kind=Kind.PARTNER, key="m.ru", name="Медиасфера", emails=["a@m.ru"])
    assert "вдогонку" not in llm.contact_brief(contact, Channel.EMAIL).lower()


def test_model_can_refuse_and_whole_chain_stops(store, monkeypatch):
    """Модель решила, что писать не о чем — не возвращаемся ни через день, ни через неделю."""
    contact = written_partner(store, days_ago=5)
    monkeypatch.setattr(
        llm, "write_letter",
        lambda *a, **kw: llm.Letter(llm.VERDICT_SKIP, "у них свой отдел разработки", "", ""),
    )
    ok, reason = followup.write_one(store, contact)

    assert ok is False
    assert "пропускаем" in reason
    assert store.queue(Kind.PARTNER) == []
    age_marks(store, contact, days=40)
    assert followup.due(store, Kind.PARTNER) == []


# --- Очередь и отчёт ---------------------------------------------------------

def test_followup_lands_in_queue(store):
    contact = written_partner(store, days_ago=5)
    followup.write_one(store, contact)

    queue = store.queue(Kind.PARTNER)
    assert len(queue) == 1
    draft, whom = queue[0]
    assert whom.pk == contact.pk
    assert "актуальна" in draft.body


def test_run_reports_to_owner(store):
    written_partner(store, days_ago=5, name="Медиасфера", key="m.ru")
    written_partner(store, days_ago=5, name="Вектор", key="v.ru")

    text = followup.run(store)

    assert text and "Письма вдогонку" in text
    assert "Медиасфера" in text and "Вектор" in text
    assert "касание 2 из 4" in text
    assert len(text) <= 3900


def test_run_is_silent_when_nobody_is_due(store):
    written_partner(store, days_ago=1)
    assert followup.run(store) is None


def test_days_left_counts_down(store):
    contact = written_partner(store, days_ago=1)
    left = followup.days_left(store, contact)
    assert left is not None and 1.5 < left < 2.5


def test_days_left_is_empty_when_chain_is_over(store):
    contact = written_partner(store, days_ago=5)
    contact.meta[followup.TOUCH_KEY] = len(followup.CHAIN[Kind.PARTNER])
    store.set_meta(contact.pk, contact.meta)
    assert followup.days_left(store, store.get_contact(contact.pk)) is None


def test_old_finished_chains_do_not_starve_the_new_ones(store, monkeypatch):
    """Контакты с кончившейся цепочкой не должны занимать собой всё окно.

    Выборка шла по первым пятистам записям стадии, то есть от самых старых —
    а это как раз те, кому уже написали все три раза. Накопись их пятьсот, и
    касания молча переставали готовиться для всех, кому написали позже.
    """
    from datetime import datetime, timedelta

    from orbit.models import Channel, Contact, Draft, Kind, Stage
    from orbit.store import MSK

    def written(key: str, *, days_ago: int, step: int | None) -> int:
        contact = Contact(
            kind=Kind.PARTNER, key=key, name=key, site=key,
            emails=[f"hello@{key}"], stage=Stage.WRITTEN,
        )
        pk = store.upsert_contact(contact)
        draft_pk = store.save_draft(Draft(
            contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Первое",
        ))
        store.mark_sent(draft_pk)
        when = (datetime.now(MSK) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        store.conn.execute("UPDATE drafts SET sent_at = ? WHERE pk = ?", (when, draft_pk))
        store.conn.execute(
            "INSERT INTO events (contact_pk, kind, text, at) VALUES (?, 'sent', 'почта', ?)",
            (pk, when),
        )
        store.conn.commit()
        store.set_stage(pk, Stage.WRITTEN, "написали")
        if step is not None:
            fresh = store.get_contact(pk)
            fresh.meta[followup.TOUCH_KEY] = step
            fresh.meta[followup.TOUCH_AT] = when
            store.set_meta(pk, fresh.meta)
        return pk

    # Пятьсот старых с исчерпанной цепочкой — ровно столько, сколько было окно.
    for number in range(500):
        written(f"old{number}.ru", days_ago=60, step=len(followup.CHAIN[Kind.PARTNER]))
    fresh_pk = written("fresh.ru", days_ago=10, step=None)

    ready = followup.due(store, Kind.PARTNER)

    assert fresh_pk in [contact.pk for contact in ready], "новый контакт обязан попасть в касания"


def test_telegram_touch_is_not_promised_to_send_itself(store, monkeypatch):
    """Бот не может написать в телеграм первым — обещать «уйдут сами» нельзя.

    Владелец, прочитав такое, просто не отправит: он ждёт автоматики, а её
    там нет и не будет.
    """
    from datetime import datetime, timedelta

    from orbit import outreach
    from orbit.models import Channel, Contact, Draft, Kind, Stage
    from orbit.store import MSK

    contact = Contact(
        kind=Kind.PARTNER, key="tgonly", name="Студия",
        telegram=["@studio"], stage=Stage.WRITTEN,
    )
    pk = store.upsert_contact(contact)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Первое сообщение",
    ))
    outreach.mark_sent_manually(store, draft_pk)
    when = (datetime.now(MSK) - timedelta(days=5)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE drafts SET sent_at = ? WHERE pk = ?", (when, draft_pk))
    store.conn.execute(
        "INSERT INTO events (contact_pk, kind, text, at) VALUES (?, 'sent', 'телеграм', ?)",
        (pk, when),
    )
    store.conn.commit()
    store.set_stage(pk, Stage.WRITTEN, "написали")
    outreach.set_autosend(store, Channel.EMAIL, True)

    text = followup.run(store)

    assert text, "касание должно подготовиться"
    assert "уйдут сами" not in text
    assert "рук" in text, text


def test_a_broken_mailbox_is_not_called_automatic(store, monkeypatch):
    """«Почта уйдёт сама» при ненастроенном ящике значит «никто их не отправит».

    Тумблер включён, а доступа к ящику в .env нет: не уйдёт ни одно письмо, и
    владелец, прочитав «уйдёт сама», просто не откроет очередь.
    """
    from orbit import followup, outreach
    from orbit.models import Channel, Contact, Kind, Stage

    contact = Contact(kind=Kind.CLIENT, key="firma.ru", name="Фирма",
                      emails=["a@firma.ru"], stage=Stage.WRITTEN)
    pk = store.upsert_contact(contact)

    monkeypatch.setattr(followup, "due", lambda store_, kind: [store.get_contact(pk)]
                        if kind is Kind.CLIENT else [])
    monkeypatch.setattr(followup, "write_one", lambda store_, contact_: (True, "касание 2 из 4"))
    monkeypatch.setattr(outreach, "autosend_on", lambda store_, channel=Channel.EMAIL: True)
    monkeypatch.setattr(
        outreach, "channel_ready",
        lambda channel: (False, "нет доступа к ящику в .env") if channel is Channel.EMAIL
        else (False, "бот не может написать первым, нужен отдельный аккаунт"),
    )

    text = followup.run(store)
    assert text and "уйдёт сама" not in text, text
    assert "не работает" in text, text


# --- Протухший текст ----------------------------------------------------------


def age_draft(store, contact, *, days: float) -> None:
    """Отматывает назад момент НАПИСАНИЯ письма, лежащего в очереди."""
    when = (datetime.now(MSK) - timedelta(days=days)).isoformat(timespec="seconds")
    store.conn.execute(
        "UPDATE drafts SET created_at = ? WHERE contact_pk = ? AND status = 'draft'",
        (when, contact.pk),
    )
    store.conn.commit()


def test_stale_touch_is_dropped_and_chain_steps_back(store):
    """Касание, пролежавшее месяц, снимается — и цепочка не теряет шага.

    Живой случай: всё стояло из-за мёртвого прокси, а письмо «писал вам
    недавно» уехало через сорок шесть дней после первого. Шаг цепочки растёт
    от подготовки письма, поэтому просто выбросить черновик нельзя: касание
    сгорело бы молча, и человек не получил бы вообще ничего.
    """
    contact = written_partner(store, days_ago=40)
    assert followup.write_one(store, contact)[0]
    contact = store.get_contact(contact.pk)
    assert followup.step_of(contact) == 1

    age_draft(store, contact, days=followup.STALE_DAYS + 1)
    assert followup.drop_stale(store, Kind.PARTNER) == [contact.name]

    contact = store.get_contact(contact.pk)
    assert store.draft_for(contact.pk) is None
    assert followup.step_of(contact) == 0
    # Стадию не трогаем: холодное первое письмо второй раз ему писать нельзя.
    assert contact.stage is Stage.WRITTEN


def test_fresh_touch_is_left_alone(store):
    """Свежее письмо уборка не трогает."""
    contact = written_partner(store, days_ago=40)
    assert followup.write_one(store, contact)[0]
    age_draft(store, contact, days=followup.STALE_DAYS - 1)

    assert followup.drop_stale(store, Kind.PARTNER) == []
    assert store.draft_for(contact.pk) is not None


def test_stale_first_letter_returns_contact_to_new(store):
    """А первое письмо, наоборот, возвращает контакт в «новые».

    Его текст опирается на то, что мы видели на сайте месяц назад, и
    переписать его должна обычная подготовка писем с нуля.
    """
    contact = Contact(
        kind=Kind.PARTNER, key="new.ru", name="Новое агентство", site="new.ru",
        emails=["hi@new.ru"], stage=Stage.NEW,
    )
    contact.pk = store.upsert_contact(contact)
    store.save_draft(Draft(
        contact_pk=contact.pk, channel=Channel.EMAIL, subject="Тема", body="Первое письмо",
    ))
    age_draft(store, contact, days=followup.STALE_DAYS + 5)

    assert followup.drop_stale(store, Kind.PARTNER) == [contact.name]
    assert store.draft_for(contact.pk) is None
    assert store.get_contact(contact.pk).stage is Stage.NEW


def test_run_clears_stale_before_looking_for_due(store):
    """Уборка идёт до поиска тех, кому пора, иначе новое письмо не напишется.

    due() пропускает контакт, у которого письмо уже лежит в очереди. Не сняв
    протухшее первым делом, мы бы не написали новое никогда.
    """
    contact = written_partner(store, days_ago=40)
    assert followup.write_one(store, contact)[0]
    age_draft(store, contact, days=followup.STALE_DAYS + 1)
    contact = age_marks(store, contact, days=40)

    followup.run(store)

    draft = store.draft_for(contact.pk)
    assert draft is not None, "после уборки должно появиться свежее касание"
    assert followup.step_of(store.get_contact(contact.pk)) == 1

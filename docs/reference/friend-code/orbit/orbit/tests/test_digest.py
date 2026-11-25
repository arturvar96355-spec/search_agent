"""Воронка и вечерняя сводка.

Проверяем, что цифры собираются из обеих баз, что отсутствие биржи не ломает
сводку и что в тихий день бот молчит, а не присылает пустой отчёт.
"""
from __future__ import annotations

import pytest

from orbit import digest
from orbit.models import Contact, Kind, Stage


@pytest.fixture(autouse=True)
def no_exchange(monkeypatch):
    """По умолчанию биржи нет: у неё своя база, и тесты в неё не лезут.

    Гасим и exchange_connected: «системы нет» и «система есть, но молчит» —
    разные новости, и сводка ведёт себя по-разному. Здесь мы описываем именно
    первое.
    """
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=7: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)


class FakeFunnel:
    """Воронка биржи, как её видит Orbit.

    Поля перечислены руками, потому что Orbit читает чужой объект из соседнего
    проекта. Раз так — список обязан совпадать с настоящим, иначе экран
    воронки падает у владельца, а тесты этого не замечают. За совпадением
    следит отдельный тест ниже.
    """

    median_speed = 7.0
    fast_share = 40.0
    seen = 300
    rejected = 210
    sent = 12
    replies = 3
    spent = 640.0
    reply_rate = 25.0
    cost_per_reply = 213.0
    decided = 222
    dry_run = 0
    failed = 0
    skipped = 0
    unavailable = 0
    speed_minutes = [7.0]
    dropped_early = 78
    waiting = 6
    reasons = [("не наш профиль", 120), ("низкий бюджет", 40)]
    our_failures = [("не удалось сгенерировать заход", 9)]


def add(store, kind: Kind, stage: Stage, key: str) -> Contact:
    contact = Contact(kind=kind, key=key, name=key, emails=[f"a@{key}"], stage=stage)
    contact.pk = store.upsert_contact(contact)
    store.set_stage(contact.pk, stage, "тест")
    return store.get_contact(contact.pk)


# --- Воронка ---------------------------------------------------------------

def test_funnel_shows_all_three_systems(store, monkeypatch):
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=7: FakeFunnel())
    add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    add(store, Kind.CLIENT, Stage.REPLIED, "firma.ru")

    text = digest.funnel_text(store)

    assert "Партнёры" in text and "Клиенты" in text and "Фриланс" in text
    assert "откликнулись 12" in text
    assert "один ответ 213 ₽" in text


def test_funnel_numbers_add_up(store):
    """Сумма по стадиям должна сходиться с «всего» — иначе это выглядит как ошибка."""
    add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    add(store, Kind.PARTNER, Stage.DEAD, "svoya-razrabotka.ru")

    text = digest.funnel_text(store)

    assert "сейчас в базе 2" in text
    assert "не подошли: 1" in text


def test_funnel_survives_broken_exchange(store):
    """Биржа не отвечает — воронка обязана открыться и сказать об этом."""
    add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    text = digest.funnel_text(store)

    assert "Биржа не отвечает" in text
    assert "Партнёры" in text


def test_funnel_names_top_rejection_reasons(store, monkeypatch):
    """По причинам отсева видно, что подкрутить в фильтрах."""
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=7: FakeFunnel())
    assert "не наш профиль" in digest.funnel_text(store)


def test_funnel_fits_telegram_limit(store, monkeypatch):
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=7: FakeFunnel())
    for index in range(60):
        add(store, Kind.CLIENT, Stage.NEW, f"firma{index}.ru")
    assert len(digest.funnel_text(store)) <= 3900


# --- Сводка ----------------------------------------------------------------

def test_quiet_day_stays_quiet(store):
    """Ничего не происходило — сообщение не шлём вовсе."""
    assert digest.build(store) is None


def test_digest_counts_what_happened_today(store):
    contact = add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    store.log_event(contact.pk, "sent", "почта")
    store.log_event(contact.pk, "reply", "ответили")
    store.log_event(contact.pk, "call", "поговорили")

    text = digest.build(store)

    assert text and "Итоги дня" in text
    assert "писем ушло: 1" in text
    assert "ответов на письма: 1" in text
    assert "звонков: 1" in text


def test_digest_mentions_exchange(store, monkeypatch):
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: FakeFunnel())
    text = digest.build(store)

    assert text and "откликов отправлено: 12" in text
    assert "потрачено: 640 ₽" in text


def test_digest_names_the_silent_ones(store):
    """Молчуны в сводке — по именам, а не цифрой.

    Решение владельца от 22.08.2026: «писем ушло: 6» не говорит, с кем завтра
    продолжать разговор. Имя и давность — говорят.
    """
    contact = add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    store.log_event(contact.pk, "sent", "почта")
    # Молчуном человек становится не в день письма, а со следующего: письмо
    # ушло час назад, он мог его ещё не открыть. Состариваем именно событие
    # отправки — срок молчания считается по нему, а не по правке карточки.
    store.conn.execute(
        "UPDATE events SET at = '2026-08-10T12:00:00' WHERE contact_pk = ? AND kind = 'sent'",
        (contact.pk,),
    )
    store.conn.commit()

    text = digest.build(store)
    assert "Молчат" in text
    assert contact.name in text
    assert "с 10.08" in text, "давность считается по дате письма"


def test_written_today_is_not_called_silent(store):
    """Компания, которой написали час назад, — ещё не молчун."""
    contact = add(store, Kind.PARTNER, Stage.WRITTEN, "fresh.ru")
    store.log_event(contact.pk, "sent", "почта")

    text = digest.build(store)
    assert "Кому написали сегодня" in text
    assert "Молчат" not in text, "сегодняшних в молчуны записывать рано"


def test_exchange_orders_do_not_flood_the_silent_list(store):
    """Биржевые заказы в «Молчат» не попадают — у сборщика своя логика молчунов."""
    for i in range(3):
        add(store, Kind.ORDER, Stage.WRITTEN, f"order{i}.ru")
    partner = add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    store.log_event(partner.pk, "sent", "почта")

    text = digest.build(store)
    assert partner.name in text
    assert "order0.ru" not in text and "order1" not in str(text)


def test_yesterday_events_do_not_leak_into_today(store):
    contact = add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    store.log_event(contact.pk, "sent", "почта")
    store.conn.execute("UPDATE events SET at = '2026-08-01T10:00:00+03:00'")
    store.conn.commit()

    assert digest.build(store) is None


def test_event_counts_are_per_day(store):
    contact = add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    store.log_event(contact.pk, "sent", "почта")
    store.log_event(contact.pk, "sent", "почта")

    assert store.event_counts()["sent"] == 2
    assert store.event_counts("2020-01-01") == {}


# --- Напоминания не должны теряться -------------------------------------------


def _remind(store, name, date, note="вернитесь позже"):
    from orbit import inbox
    from orbit.models import Contact, Kind, Stage

    contact = Contact(kind=Kind.PARTNER, key=f"{name}.ru", name=name,
                      emails=[f"h@{name}.ru"], stage=Stage.REPLIED)
    pk = store.upsert_contact(contact)
    contact = store.get_contact(pk)
    contact.meta[inbox.REMIND_KEY] = date
    contact.meta[inbox.REMIND_NOTE] = note
    store.set_meta(pk, contact.meta)
    return pk


@pytest.fixture()
def quiet(monkeypatch):
    """Тихий день: биржа молчит, событий нет."""
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=7: None)


def test_reminder_survives_a_quiet_day(store, quiet):
    """Человек просил вернуться именно сегодня — «ничего не происходило» не повод молчать.

    Ранний выход «сегодня пусто — сводки нет» стоял до сбора напоминаний, и
    срок, назначенный на тихий день, пропадал совсем.
    """
    _remind(store, "Медиасфера", "2026-09-01", note="после Нового года")
    text = digest.build(store, day="2026-09-01") or ""
    assert "Просили вернуться" in text
    assert "после Нового года" in text


def test_truly_empty_day_is_still_silent(store, quiet):
    """Без напоминаний тихий день по-прежнему не беспокоит владельца."""
    assert digest.build(store, day="2026-09-01") is None


def test_todays_reminder_is_not_pushed_out_by_old_ones(store, quiet):
    """Просроченные копятся, пока владелец не сработал: сегодняшнее должно быть видно."""
    for day in range(1, 13):
        _remind(store, f"старый{day:02d}", f"2026-03-{day:02d}")
    _remind(store, "СегодняВажный", "2026-09-01")

    text = digest.build(store, day="2026-09-01") or ""
    assert "СегодняВажный" in text


def test_fake_funnel_has_everything_the_screen_reads():
    """Заглушка обязана повторять настоящую воронку биржи.

    Экран воронки читает чужой объект из соседнего проекта по именам полей.
    Разъехались — и у владельца падает весь экран, а тесты этого не видят,
    потому что проверяют заглушку. Поэтому сверяем их явно.
    """
    from fbid.stats import Funnel

    real = Funnel()
    for name in dir(real):
        if name.startswith("_"):
            continue
        assert hasattr(FakeFunnel, name), f"заглушка отстала от настоящей воронки: {name}"


def test_no_one_who_replied_is_lost_from_the_digest(store, monkeypatch):
    """Список ответивших обрезается — но владелец обязан узнать, скольких не видит.

    У соседних списков хвост «и ещё N» был, а у этого нет. Здесь он нужнее
    всех: это люди, которые ждут ответа, и тринадцатый из них просто не
    существовал для владельца.
    """
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=7: None)
    for number in range(15):
        contact = add(store, Kind.PARTNER, Stage.REPLIED, f"agency{number}.ru")
        store.log_event(contact.pk, "reply", "ответили")

    text = digest.build(store)

    assert text
    assert "Ответили сегодня" in text
    assert "и ещё 3" in text


def test_silence_is_counted_from_the_letter_not_from_any_edit(store):
    """Импорт выгрузки не должен обнулять список молчунов.

    Раньше срок считался по updated_at, а его трогает любая правка карточки:
    импорт, запись проверки адреса, заметка. После нажатия «Загрузить из
    парсера» список «Молчат» опустевал целиком — ровно тогда, когда по нему
    и надо работать.
    """
    from orbit.models import Contact, Kind, Stage

    contact = Contact(
        kind=Kind.PARTNER, key="quiet.ru", name="Молчун", emails=["a@quiet.ru"],
    )
    pk = store.upsert_contact(contact)
    store.set_stage(pk, Stage.WRITTEN, "написали")
    store.conn.execute(
        "INSERT INTO events (contact_pk, kind, text, at) VALUES (?, 'sent', 'почта', ?)",
        (pk, "2026-08-09T10:00:00"),
    )
    store.conn.commit()

    assert store.waiting_contacts() == [("Молчун", "2026-08-09")]

    # Повторный импорт того же файла — карточка «обновилась» сегодня.
    store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="quiet.ru", name="Молчун", emails=["a@quiet.ru"],
    ))

    assert store.waiting_contacts() == [("Молчун", "2026-08-09")], "срок обязан остаться прежним"


def test_a_broken_exchange_is_worth_a_digest_on_a_quiet_day(store, monkeypatch):
    """Молчание вечером не должно означать сразу и «всё тихо», и «мы ослепли».

    Цифры недоступной биржи превращались в нули, «ничего не произошло»
    становилось правдой, и сводка не уходила вовсе — вместе со строкой
    «биржа не отвечает».
    """
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: True)

    text = digest.build(store)

    assert text, "о слепоте надо сказать даже в пустой день"
    assert "биржа не отвечает" in text


def test_a_missing_exchange_is_not_news(store, monkeypatch):
    """Системы бирж у владельца просто нет — говорить об этом каждый вечер незачем."""
    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)

    assert digest.build(store) is None


# --- День, когда всё встало, не должен выглядеть как тихий день ---------------


def test_a_quiet_day_with_a_dead_model_still_gets_a_digest(store, monkeypatch):
    """Модель молчит — сводка обязана уйти и назвать причину.

    Без модели не пишутся письма, не разбираются ответы и не уходят отклики.
    Цифры при этом честные нули, «сегодня ничего не происходило» формально
    правда, и сводка просто не уходила: день, когда всё встало, выглядел
    точно как день, когда просто не было заказов.
    """
    from orbit import digest
    from orbit.schedule import MODEL_STATE

    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)

    assert digest.build(store) is None, "без поломок в тихий день молчим"

    store.set_state(MODEL_STATE, "кончились деньги на счёте модели")
    text = digest.build(store)
    assert text is not None, "о вставшей модели надо сказать вечером ещё раз"
    assert "Модель молчит" in text
    assert "кончились деньги на счёте модели" in text


def test_a_working_model_adds_no_alarm_line(store, monkeypatch):
    """А когда модель жива, лишней строки в сводке быть не должно."""
    from orbit import digest
    from orbit.models import Contact, Kind

    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)

    pk = store.upsert_contact(Contact(kind=Kind.CLIENT, key="a.ru", name="А",
                                      emails=["a@a.ru"]))
    store.log_event(pk, "sent", "почта")

    text = digest.build(store)
    assert text and "Модель молчит" not in text


def test_a_day_of_scanning_without_a_single_bid_still_gets_a_digest(store, monkeypatch):
    """Биржа перебрала двести заказов и не отправила ни одного — это новость.

    Считался только отправленный отклик, и такой день выглядел как «ничего не
    происходило»: сводка не уходила вовсе. А это ровно тот день, о котором
    надо знать — значит всё отсеяли фильтры, кончились деньги или встал
    автоотклик.
    """
    from orbit import digest

    class Funnel:
        seen = 200
        sent = 0
        spent = 0.0
        replies = 0
        rejected = 3
        skipped = 5
        unavailable = 0
        dry_run = 0
        failed = 0
        reasons = []
        our_failures = []
        dropped_early = 192

    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: Funnel())
    monkeypatch.setattr(digest, "exchange_connected", lambda: True)

    text = digest.build(store)
    assert text is not None, "молчать в такой день нельзя"
    assert "заказов просмотрено: 200" in text


def test_a_truly_empty_day_stays_quiet(store, monkeypatch):
    """А день, когда и смотреть было нечего, по-прежнему тихий."""
    from orbit import digest

    class Funnel:
        seen = 0
        sent = 0
        spent = 0.0
        replies = 0

    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: Funnel())
    monkeypatch.setattr(digest, "exchange_connected", lambda: True)

    assert digest.build(store) is None


def test_the_funnel_says_which_numbers_are_a_snapshot(store, monkeypatch):
    """Заголовок обещал «за N дней» всему экрану — а это была правда лишь для биржи.

    Кнопка «За 30 дней» цифры партнёров и клиентов не меняла вовсе, и владелец
    видел одни и те же числа, думая, что смотрит разные периоды.
    """
    add(store, Kind.PARTNER, Stage.WRITTEN, "agency.ru")
    monkeypatch.setattr(digest, "exchange_funnel", lambda days: _fake_funnel())

    week = digest.funnel_text(store, days=7)
    month = digest.funnel_text(store, days=30)

    assert "сейчас в базе" in week, week
    assert "за 7 дней" in week
    assert "за 30 дней" in month
    # Снимок по партнёрам от периода не зависит — и подписан он честно.
    assert week.split("💼")[0] == month.split("💼")[0]


def test_a_huge_digest_still_fits_the_screen(store, monkeypatch):
    """Сводка на большой базе обязана открываться кнопкой.

    Длиной её никто не ограничивал: на базе с сотней событий она перерастала
    лимит Telegram, и экран переставал открываться вовсе — нажатие выглядело
    как сломанная кнопка.
    """
    from orbit.models import Contact, Kind
    from orbit.ui import MAX_TEXT

    monkeypatch.setattr(digest, "exchange_funnel", lambda days=1: None)
    monkeypatch.setattr(digest, "exchange_connected", lambda: False)

    for n in range(300):
        pk = store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"c{n}.ru", name=f"Компания с длинным именем номер {n}",
            emails=[f"a@c{n}.ru"],
        ))
        store.log_event(pk, "sent", "почта")
        store.log_event(pk, "reply", "ответ клиента длиной в несколько слов подряд")

    text = digest.build(store)
    assert text and len(text) <= MAX_TEXT, len(text or "")


def _fake_funnel():
    """Минимальная воронка биржи — экрану хватит её полей."""
    class Funnel:
        seen = 100
        sent = 2
        rejected = 5
        replies = 0
        spent = 0.0
        dropped_early = 93
        reasons = []
        our_failures = []
        median_speed = None

        @property
        def reply_rate(self):
            return 0.0

        @property
        def cost_per_reply(self):
            return None

    return Funnel()


def test_the_evening_report_says_the_exchange_login_is_gone(store, monkeypatch):
    """Слетевший вход — не «тихий день», а «туда не ушло ни одного отклика».

    Главный экран об этом говорил, сводка молчала — а вечером читают именно
    её, не открывая бота. День со слетевшим входом выглядел как обычный.
    """
    from orbit import digest
    from orbit.sections import freelance

    monkeypatch.setattr(freelance, "lost_sessions_now", lambda: ["fl"])

    text = digest.build(store)
    assert text, "сводка обязана уйти: вход слетел, это новость"
    assert "вход на FL.ru слетел" in text
    assert "fbid login fl" in text


def test_zero_bids_explain_themselves(store, monkeypatch):
    """«Откликов отправлено: 0» восемь вечеров подряд читалось как тихий день.

    А площадка в это время не отвечала вовсе из-за мёртвого прокси. Цифра,
    которая не называет причину, хуже её отсутствия: она успокаивает.
    """
    from orbit import digest
    from orbit.sections import freelance

    monkeypatch.setattr(freelance, "lost_sessions_now", lambda: [])
    monkeypatch.setattr(freelance, "down_sources_now", lambda: ["profi"])

    text = digest.build(store)
    assert text, "сводка обязана уйти: площадка молчит, это новость"
    assert "Profi.ru не отвечает" in text
    assert "check-proxy" in text

"""Разбор входящей почты.

Настоящий ящик тут не нужен: проверяем разбор письма и решения по нему.
Письма собраны из тех же кусков, что приходят живьём — с кодированными
заголовками, цитатой нашего письма и отчётом почтового сервера.
"""
from __future__ import annotations

import email

import pytest

from orbit import inbox
from orbit.models import Contact, Kind, Stage

BOUNCE_BODY = """This is the mail system at host smtp.beget.com.

I'm sorry to have to inform you that your message could not
be delivered to one or more recipients.

<hello@mediasfera.ru>: host mx.mediasfera.ru said: 550 5.1.1 User unknown
"""


def letter(**kwargs) -> inbox.Letter:
    data = {
        "uid": 1,
        "sender": "hello@mediasfera.ru",
        "subject": "Re: Вопрос по сотрудничеству",
        "body": "Здравствуйте! Интересно, расскажите подробнее.",
    }
    data.update(kwargs)
    return inbox.Letter(**data)


@pytest.fixture()
def agency_in_db(store) -> Contact:
    contact = Contact(
        kind=Kind.PARTNER, key="mediasfera.ru", name="Медиасфера",
        emails=["hello@mediasfera.ru"], stage=Stage.WRITTEN,
    )
    contact.pk = store.upsert_contact(contact)
    return store.get_contact(contact.pk)


# --- Что это за письмо -----------------------------------------------------

def test_plain_answer_is_a_reply():
    assert letter().kind == inbox.REPLY


def test_mailer_daemon_is_a_bounce():
    assert letter(sender="mailer-daemon@beget.com", subject="Undelivered Mail").kind == inbox.BOUNCE


def test_russian_bounce_subject_is_caught():
    assert letter(sender="postmaster@mail.ru", subject="Возврат письма").kind == inbox.BOUNCE


def test_vacation_autoreply_is_not_a_reply():
    assert letter(subject="Автоответ: я в отпуске").kind == inbox.AUTO


def test_auto_submitted_header_is_enough():
    assert letter(subject="Re: письмо", auto_submitted=True).kind == inbox.AUTO


def test_failed_address_is_taken_from_bounce_body():
    bounce = letter(sender="mailer-daemon@beget.com", subject="Undelivered", body=BOUNCE_BODY)
    assert bounce.failed_address() == "hello@mediasfera.ru"


# --- Разбор письма ---------------------------------------------------------

def test_encoded_subject_is_readable():
    raw = (
        b"From: =?utf-8?B?0JzQtdC00LjQsNGB0YTQtdGA0LA=?= <hello@mediasfera.ru>\r\n"
        b"Subject: =?utf-8?B?0J7RgtCy0LXRgg==?=\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"\xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82"
    )
    message = email.message_from_bytes(raw)
    assert inbox._text(message.get("Subject")) == "Ответ"
    assert inbox.body_of(message) == "Привет"


def test_html_only_letter_is_readable():
    raw = (
        b"Subject: hi\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><p>Hello</p></body></html>"
    )
    assert "Hello" in inbox.body_of(email.message_from_bytes(raw))


def test_quoted_original_is_cut_off():
    """В уведомление должен попасть ответ, а не наше же письмо целиком."""
    text = (
        "Да, интересно.\n\n"
        "07.08.2026 в 10:00 UniqOre написал:\n"
        "> Здравствуйте! Мы студия разработки...\n"
        "> Работаем белым субподрядом."
    )
    assert inbox.strip_quoted(text) == "Да, интересно."


# --- Решения ---------------------------------------------------------------

def test_reply_moves_contact_and_calls_owner(store, agency_in_db):
    text = inbox.handle(store, letter())

    assert store.get_contact(agency_in_db.pk).stage is Stage.REPLIED
    assert text and "Ответили на письмо" in text
    assert "Медиасфера" in text
    assert "расскажите подробнее" in text


def test_reply_does_not_pull_stage_backwards(store, agency_in_db):
    """Уже созвонились — ответ на письмо не должен вернуть контакт назад."""
    store.set_stage(agency_in_db.pk, Stage.CALL, "договорились")
    inbox.handle(store, letter())
    assert store.get_contact(agency_in_db.pk).stage is Stage.CALL


def test_bounce_closes_the_contact(store, agency_in_db):
    text = inbox.handle(store, letter(
        sender="mailer-daemon@beget.com", subject="Undelivered Mail", body=BOUNCE_BODY,
    ))

    updated = store.get_contact(agency_in_db.pk)
    assert updated.stage is Stage.DEAD
    assert updated.meta[inbox.BOUNCED_KEY]
    assert text and "вернулось" in text


def test_autoreply_is_recorded_but_silent(store, agency_in_db):
    """«Я в отпуске» — не разговор: пометка есть, владельца не дёргаем."""
    text = inbox.handle(store, letter(subject="Автоответ: в отпуске до 20 августа"))

    assert text is None
    updated = store.get_contact(agency_in_db.pk)
    assert updated.stage is Stage.WRITTEN
    assert updated.meta[inbox.AUTOREPLY_KEY]


def test_letter_from_stranger_is_ignored(store, agency_in_db):
    """Обычная почта в ящике не должна превращаться в уведомления."""
    assert inbox.handle(store, letter(sender="spam@example.com")) is None


def test_bounce_for_unknown_address_does_not_hit_random_contact(store, agency_in_db):
    bounce = letter(
        sender="mailer-daemon@beget.com", subject="Undelivered",
        body="<kto-to@drugoy.ru>: 550 User unknown",
    )
    assert inbox.handle(store, bounce) is None
    assert store.get_contact(agency_in_db.pk).stage is Stage.WRITTEN


def test_email_lookup_is_exact(store, agency_in_db):
    """«old-hello@mediasfera.ru» — другой адрес, не наш контакт."""
    assert store.contact_by_email("hello@mediasfera.ru").pk == agency_in_db.pk
    assert store.contact_by_email("old-hello@mediasfera.ru") is None
    assert store.contact_by_email("") is None


def test_check_is_quiet_without_mail_settings(store):
    """Почта не настроена — расписание не должно ни падать, ни писать владельцу."""
    assert inbox.check(store) is None


def _broken_mailbox(monkeypatch):
    settings = inbox.get_settings()
    monkeypatch.setattr(settings, "imap_host", "imap.beget.com")
    monkeypatch.setattr(settings, "smtp_user", "partnership@uniqore.ru")
    monkeypatch.setattr(settings, "smtp_password", "секрет")
    assert settings.imap_ready is True

    def boom(*args, **kwargs):
        raise OSError("сервер не отвечает")

    monkeypatch.setattr(inbox, "fetch", boom)


def test_a_broken_mailbox_is_not_an_empty_one(store, monkeypatch):
    """Ящик не прочитался — это новость, а не «писем нет».

    Пока ящик молчит, ответы клиентов не разбираются вовсе: ни «нам
    интересно», ни «не пишите», ни возвраты. Возврат None делал это
    неотличимым от спокойного дня.
    """
    _broken_mailbox(monkeypatch)
    with pytest.raises(inbox.MailboxUnavailable):
        inbox.check(store)


def test_the_owner_hears_about_a_silent_mailbox_once(store, monkeypatch):
    """Расписание жалуется один раз и говорит, когда починилось."""
    from orbit import schedule

    _broken_mailbox(monkeypatch)
    said: list[str] = []
    monkeypatch.setattr("orbit.notify.tell", lambda text, **kw: said.append(text) or True)

    for _ in range(6):
        assert schedule._job_inbox(store) is None
    assert len(said) == 1, said
    assert "не читается" in said[0]
    assert store.get_state(schedule.INBOX_STATE)

    # Починилось — об этом надо сказать отдельно.
    monkeypatch.setattr(inbox, "fetch", lambda store_, limit=30: [])
    assert schedule._job_inbox(store) is None
    assert len(said) == 2 and "снова читается" in said[1]
    assert not store.get_state(schedule.INBOX_STATE)


def test_an_undelivered_mailbox_alarm_is_repeated(store, monkeypatch):
    """Не дошло — значит не сказали: повторяем на следующем заходе."""
    from orbit import schedule

    _broken_mailbox(monkeypatch)
    tries: list[str] = []
    monkeypatch.setattr("orbit.notify.tell", lambda text, **kw: tries.append(text) and False)

    schedule._job_inbox(store)
    schedule._job_inbox(store)
    assert len(tries) == 2
    assert store.get_state(schedule.INBOX_STATE), "правда о ящике пишется всегда"
    assert not store.get_state(schedule.INBOX_TOLD)


def test_several_replies_come_as_one_message(store, agency_in_db):
    """Пять ответов подряд — одно сообщение, а не пять уведомлений."""
    second = Contact(
        kind=Kind.PARTNER, key="vektor.ru", name="Вектор",
        emails=["hi@vektor.ru"], stage=Stage.WRITTEN,
    )
    second.pk = store.upsert_contact(second)

    letters = [letter(uid=1), letter(uid=2, sender="hi@vektor.ru", body="И нам интересно")]
    text = "\n\n———\n\n".join(t for x in letters if (t := inbox.handle(store, x)))

    assert "Медиасфера" in text and "Вектор" in text
    assert len(text) <= 3900


# --- Дефекты, найденные аудитом 07.08.2026 ---------------------------------

def test_reply_from_another_address_of_the_same_company(store, agency_in_db):
    """Писали на info@, ответил директор со своего адреса — это тот же контакт."""
    text = inbox.handle(store, letter(sender="d.petrov@mediasfera.ru", body="Давайте обсудим"))

    assert text and "Медиасфера" in text
    assert store.get_contact(agency_in_db.pk).stage is Stage.REPLIED


def test_reply_from_a_similar_but_foreign_domain_is_ignored(store, agency_in_db):
    assert inbox.handle(store, letter(sender="ivan@mediasfera-spb.ru")) is None
    assert store.get_contact(agency_in_db.pk).stage is Stage.WRITTEN


def test_stop_request_closes_the_contact(store, agency_in_db):
    """«Не пишите нам больше» — закрываем контакт, а не начинаем разговор."""
    text = inbox.handle(store, letter(body="Стоп, не пишите нам больше"))

    assert store.get_contact(agency_in_db.pk).stage is Stage.DEAD
    assert text and "не писать" in text


def test_angle_brackets_in_reply_do_not_break_notification(store, agency_in_db):
    """Подпись с адресом в угловых скобках ломала разбор HTML в Telegram."""
    text = inbox.handle(store, letter(body="Ок!\n\nС уважением, Иван <i.petrov@mediasfera.ru>"))

    assert text and "&lt;i.petrov@mediasfera.ru&gt;" in text
    assert "<i.petrov" not in text


def test_bounce_closes_all_contacts_with_the_same_address(store, agency_in_db):
    """Общая почта у филиалов: вернулось для одного — мёртвая для всех."""
    twin = Contact(
        kind=Kind.CLIENT, key="medsi-lenina", name="Медси на Ленина",
        emails=["hello@mediasfera.ru"], stage=Stage.WRITTEN,
    )
    twin.pk = store.upsert_contact(twin)

    text = inbox.handle(store, letter(
        sender="mailer-daemon@beget.com", subject="Undelivered", body=BOUNCE_BODY,
    ))

    assert store.get_contact(twin.pk).stage is Stage.DEAD
    assert text and "ещё 1" in text


def test_big_batch_is_not_lost(store, monkeypatch, agency_in_db):
    """Писем больше лимита — остальные ждут, а не пропадают.

    Раньше отметка «прочитано до» двигалась по максимальному найденному UID,
    и всё, что не поместилось в один заход, терялось навсегда.
    """
    fetched = []

    class FakeBox:
        def login(self, *a):
            return None

        def select(self, *a, **kw):
            return None

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b" ".join(str(i).encode() for i in range(1, 41))]
            uid = int(args[0])
            fetched.append(uid)
            raw = (
                b"From: hello@mediasfera.ru\r\nSubject: hi\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                + "привет".encode()
            )
            return "OK", [(b"1", raw)]

        def logout(self):
            return None

    monkeypatch.setattr(inbox, "connect", lambda settings=None: FakeBox())

    first = inbox.fetch(store, limit=30)
    assert len(first) == 30
    assert store.get_state(inbox.LAST_UID_KEY) == "30"

    second = inbox.fetch(store, limit=30)
    assert [item.uid for item in second] == list(range(31, 41)), "остальные письма должны дойти"


# --- Возврат письма: временный или окончательный -------------------------------


DELAY_BODY = """This is the mail system at host smtp.beget.com.

####################################################################
# THIS IS A WARNING ONLY.  YOU DO NOT NEED TO RESEND YOUR MESSAGE.  #
####################################################################

Your message could not be delivered for more than 4 hours.
<hello@mediasfera.ru>: connect to mx.mediasfera.ru: Connection timed out
"""


def test_delay_report_is_not_a_death_sentence(store, agency_in_db):
    """«Пока не доставили» — сервер ещё пытается, письмо может дойти.

    По такому отчёту контакт закрывался навсегда, хотя ничего окончательного
    не произошло. Настоящий отказ приходит отдельным письмом позже.
    """
    result = inbox.handle(store, letter(
        sender="mailer-daemon@beget.com",
        subject="Delayed Mail (still being retried)",
        body=DELAY_BODY,
    ))
    assert result is None
    assert store.get_contact(agency_in_db.pk).stage is not Stage.DEAD


def test_temporary_smtp_code_is_a_delay(store, agency_in_db):
    """Код 4.x.x — временная беда, 5.x.x — окончательная."""
    assert letter(subject="Delivery Status Notification", body="Status: 4.4.1 timed out").temporary_bounce
    assert not letter(subject="Delivery Status Notification", body="Status: 5.1.1 unknown").temporary_bounce


def test_real_bounce_still_closes_a_cold_contact(store, agency_in_db):
    result = inbox.handle(store, letter(
        sender="mailer-daemon@beget.com", subject="Undelivered Mail", body=BOUNCE_BODY,
    ))
    assert result and "вернулось" in result
    assert store.get_contact(agency_in_db.pk).stage is Stage.DEAD


def test_bounce_does_not_close_a_live_conversation(store, agency_in_db):
    """Разговор уже идёт, а вернулось письмо на общий ящик — решает владелец."""
    store.set_stage(agency_in_db.pk, Stage.DEAL, "взяли в работу")
    result = inbox.handle(store, letter(
        sender="mailer-daemon@beget.com", subject="Undelivered Mail", body=BOUNCE_BODY,
    ))
    assert result and "не закрываю" in result
    assert store.get_contact(agency_in_db.pk).stage is Stage.DEAL


# --- Мёртвый адрес — это адрес, а не компания ----------------------------------


def _with_addresses(store, key, *emails, telegram=None, stage=Stage.WRITTEN):
    contact = Contact(kind=Kind.PARTNER, key=key, name=key, emails=list(emails),
                      telegram=list(telegram or []), stage=stage)
    return store.upsert_contact(contact)


def test_bounce_only_removes_the_dead_address(store):
    """У компании второй ящик — она достижима, закрывать её нельзя."""
    pk = _with_addresses(store, "двa-ящика", "info@shared.ru", "sales@own.ru")
    store.close_same_email("info@shared.ru")

    contact = store.get_contact(pk)
    assert contact.stage is not Stage.DEAD
    assert contact.emails == ["sales@own.ru"]


def test_bounce_keeps_a_telegram_contact_alive(store):
    pk = _with_addresses(store, "с-телеграмом", "info@shared.ru",
                         telegram=["https://t.me/live_chat"])
    store.close_same_email("info@shared.ru")

    contact = store.get_contact(pk)
    assert contact.stage is not Stage.DEAD
    assert contact.emails == []
    assert contact.best_telegram


def test_close_same_email_closes_a_contact_with_nothing_left(store):
    """Общий ящик умер, других каналов у контакта нет — закрываем.

    Имя у теста своё: тёзка ниже проверяет тот же исход, но через разбор
    самого возврата. Раньше они назывались одинаково, и этот молча не
    запускался вовсе — pytest оставляет из двух только последний.
    """
    pk = _with_addresses(store, "один-ящик", "info@shared.ru")
    assert store.close_same_email("info@shared.ru") == 1
    assert store.get_contact(pk).stage is Stage.DEAD


@pytest.mark.parametrize("stage", [Stage.REPLIED, Stage.CALL, Stage.DEAL])
def test_bounce_does_not_close_a_live_conversation_sibling(store, stage):
    """Возврат по общему ящику ничего не говорит о разговоре, который уже идёт."""
    pk = _with_addresses(store, "в-работе", "info@shared.ru", stage=stage)
    assert store.close_same_email("info@shared.ru") == 0
    assert store.get_contact(pk).stage is stage


def test_bounce_is_written_into_the_history(store):
    pk = _with_addresses(store, "история", "info@shared.ru", "sales@own.ru")
    store.close_same_email("info@shared.ru")
    assert any("не принимает письма" in row["text"] for row in store.events(pk))


# --- Ящик пересоздали на сервере -----------------------------------------------


class _Box:
    """Ящик, который отдаёт заданную метку поколения и три письма."""

    def __init__(self, validity: bytes, uids=(1, 2, 3)):
        self.validity = validity
        self.uids = uids

    def login(self, *a):
        return None

    def select(self, *a, **kw):
        return None

    def status(self, _mailbox, _what):
        return "OK", [b'"INBOX" (UIDVALIDITY ' + self.validity + b")"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b" ".join(str(i).encode() for i in self.uids)]
        raw = (
            b"From: hello@mediasfera.ru\r\nSubject: hi\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + "привет".encode()
        )
        return "OK", [(b"1", raw)]

    def logout(self):
        return None


def test_recreated_mailbox_does_not_swallow_all_mail(store, monkeypatch, agency_in_db):
    """Ящик пересоздали — нумерация писем началась заново, читаем с начала.

    Номера писем уникальны только внутри одного поколения ящика. Сервер меняет
    метку UIDVALIDITY при переносе или восстановлении из копии, и нумерация
    идёт с единицы. Без проверки метки запрос «дай всё после 900-го» не вернёт
    ничего и не вернёт уже никогда: почта работает, ответы клиентов лежат в
    ящике, а система молчит — снаружи это выглядит как «нам не пишут».
    """
    monkeypatch.setattr(inbox, "connect", lambda settings=None: _Box(b"111"))
    assert len(inbox.fetch(store, limit=10)) == 3
    assert store.get_state(inbox.LAST_UID_KEY) == "3"

    # Тот же ящик, но забрались вперёд: новых писем нет — отметку не трогаем.
    store.set_state(inbox.LAST_UID_KEY, "900")
    monkeypatch.setattr(inbox, "connect", lambda settings=None: _Box(b"111", uids=()))
    assert inbox.fetch(store, limit=10) == []
    assert store.get_state(inbox.LAST_UID_KEY) == "900"

    # А теперь ящик пересоздан: метка другая, читаем с начала.
    monkeypatch.setattr(inbox, "connect", lambda settings=None: _Box(b"222"))
    assert len(inbox.fetch(store, limit=10)) == 3, "письма из нового поколения должны дойти"
    assert store.get_state(inbox.UID_VALIDITY_KEY) == "222"


def test_mailbox_without_the_mark_is_read_as_before(store, monkeypatch, agency_in_db):
    """Сервер не отдал метку — это не повод обнулять место и перечитывать всё."""
    class Silent(_Box):
        def status(self, _mailbox, _what):
            raise OSError("сервер не поддерживает STATUS")

    store.set_state(inbox.LAST_UID_KEY, "2")
    monkeypatch.setattr(inbox, "connect", lambda settings=None: Silent(b"", uids=(1, 2, 3)))
    letters = inbox.fetch(store, limit=10)
    assert [item.uid for item in letters] == [3], "перечитывать старое не должны"


def test_bounce_does_not_kill_a_contact_with_other_channels(store, monkeypatch):
    """Мёртвым стал адрес, а не компания.

    Возврат закрывал адресата навсегда, даже когда у него оставался второй
    ящик или живой телеграм — компанию теряли из-за одного канала связи.
    """
    from orbit.models import Contact, Kind, Stage

    contact = Contact(
        kind=Kind.PARTNER, key="two.ru", name="Два канала",
        emails=["dead@two.ru", "alive@two.ru"], telegram=["@two"],
        stage=Stage.WRITTEN,
    )
    pk = store.upsert_contact(contact)

    text = inbox.handle(store, inbox.Letter(
        uid=1, sender="MAILER-DAEMON@beget.com",
        subject="Undelivered Mail Returned to Sender",
        body="<dead@two.ru>: host mx.two.ru said: 550 5.1.1 User unknown",
    ))

    fresh = store.get_contact(pk)
    assert fresh.stage is not Stage.DEAD, "контакт с живыми каналами закрывать нельзя"
    assert "dead@two.ru" not in fresh.emails, "мёртвый адрес обязан исчезнуть"
    assert "alive@two.ru" in fresh.emails
    assert text and "вычеркнул" in text


def test_bounce_closes_a_contact_with_nothing_left(store):
    """А если больше достучаться нечем — закрываем, это правильный исход."""
    from orbit.models import Contact, Kind, Stage

    contact = Contact(
        kind=Kind.PARTNER, key="one.ru", name="Один адрес",
        emails=["only@one.ru"], stage=Stage.WRITTEN,
    )
    pk = store.upsert_contact(contact)

    inbox.handle(store, inbox.Letter(
        uid=2, sender="MAILER-DAEMON@beget.com",
        subject="Undelivered Mail Returned to Sender",
        body="<only@one.ru>: 550 5.1.1 User unknown",
    ))
    assert store.get_contact(pk).stage is Stage.DEAD


# --- Вычеркнутый адрес не должен вернуться из выгрузки -------------------------

def test_import_does_not_resurrect_a_bounced_address(store):
    """Письмо вернулось, адрес вычеркнули — импорт не имеет права его вернуть.

    В файле выгрузки парсера адрес по-прежнему есть: о возврате знает только
    наша база. Раньше повторное «Загрузить из парсера» подмешивало мёртвый
    ящик обратно, и письмо уходило на него второй раз — то есть мы сами
    добавляли себе ещё один возврат в репутацию домена.
    """
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="two.ru", name="Два адреса",
        emails=["dead@two.ru", "alive@two.ru"],
    ))
    store.set_emails(pk, ["alive@two.ru"])

    # Тот же файл выгрузки, тот же мёртвый адрес.
    store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="two.ru", name="Два адреса",
        emails=["dead@two.ru", "alive@two.ru"],
    ))

    fresh = store.get_contact(pk)
    assert "dead@two.ru" not in fresh.emails
    assert "alive@two.ru" in fresh.emails


def test_a_live_address_is_still_added_by_import(store):
    """Запрет касается только вычеркнутых: новые адреса импорт добавляет как раньше."""
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="one.ru", name="Один", emails=["a@one.ru"],
    ))
    store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="one.ru", name="Один", emails=["b@one.ru"],
    ))

    assert set(store.get_contact(pk).emails) == {"a@one.ru", "b@one.ru"}


def test_last_letter_is_really_the_last(store, agency):
    """Разбор ответа обязан видеть последнее наше письмо, а не третье.

    Выборка шла от самого старого и обрезалась лимитом: с четвёртого письма
    список замирал на первых трёх, а разбор берёт из него последний элемент
    как «наше последнее письмо».
    """
    from orbit.models import Channel, Draft

    pk = store.upsert_contact(agency)
    for number in range(1, 6):
        draft_pk = store.save_draft(Draft(
            contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body=f"письмо {number}",
        ))
        store.mark_sent(draft_pk)

    letters = store.sent_letters(pk)
    assert letters[-1] == "письмо 5", letters
    assert len(letters) == 3
    assert letters == ["письмо 3", "письмо 4", "письмо 5"], "порядок должен остаться прямым"


# --- Окончательный отказ со статусом 4.x --------------------------------------

EXPIRED_BOUNCE = """This is the mail system at host smtp.beget.com.

I'm sorry to have to inform you that your message could not
be delivered to one or more recipients.

Final-Recipient: rfc822; hello@mediasfera.ru
Action: failed
Status: 4.4.1
Diagnostic-Code: X-Postfix; connect to mx.mediasfera.ru: Connection timed out
"""

DELAYED_BOUNCE = """This is the mail system at host smtp.beget.com.

THIS IS A WARNING ONLY. YOU DO NOT NEED TO RESEND YOUR MESSAGE.

Final-Recipient: rfc822; hello@mediasfera.ru
Action: delayed
Status: 4.4.1
Diagnostic-Code: X-Postfix; connect to mx.mediasfera.ru: Connection timed out
"""


def test_expired_message_is_a_final_refusal(store, agency_in_db):
    """Postfix отдаёт истёкшее письмо с Action: failed, но статусом 4.4.1.

    Сервер получателя всё это время отвечал «занят», поэтому код остался
    временным. По коду такой отчёт неотличим от промежуточного «пока не
    доставили» — и окончательный отказ читался как задержка: адрес оставался
    живым, владельцу не говорили ничего, а письмо не дошло и не дойдёт.
    """
    bounce = letter(
        sender="mailer-daemon@beget.com",
        subject="Undelivered Mail Returned to Sender",
        body=EXPIRED_BOUNCE,
    )
    assert bounce.temporary_bounce is False

    text = inbox.handle(store, bounce)
    assert text and "вернулось" in text
    assert store.get_contact(agency_in_db.pk).stage is Stage.DEAD


def test_delay_warning_is_still_only_a_warning(store, agency_in_db):
    """Промежуточный отчёт закрывать контакт по-прежнему не должен."""
    bounce = letter(
        sender="mailer-daemon@beget.com",
        subject="Delivery Status Notification (Delay)",
        body=DELAYED_BOUNCE,
    )
    assert bounce.temporary_bounce is True

    inbox.handle(store, bounce)
    assert store.get_contact(agency_in_db.pk).stage is Stage.WRITTEN


def test_html_reply_with_an_empty_text_part_is_not_lost():
    """Пустая text/plain рядом с настоящим html — обычное дело у почтовых клиентов.

    Пустая строка делала список частей непустым, запасной путь к html
    пропускался, и живой ответ клиента приезжал владельцу как «(пустое
    письмо)» — то есть терялся целиком.
    """
    raw = (
        "Subject: Re\r\n"
        'Content-Type: multipart/alternative; boundary="ГРАНИЦА"\r\n\r\n'
        "--ГРАНИЦА\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "\r\n"
        "--ГРАНИЦА\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><p>Да, интересно</p></body></html>\r\n"
        "--ГРАНИЦА--\r\n"
    ).encode()
    body = inbox.body_of(email.message_from_bytes(raw))
    assert "интересно" in body


# --- Пачка ответов: счётчик обязан пережить обрезку ----------------------------

def test_the_counter_survives_the_length_limit():
    """«…и ещё N» стояло в конце и срезалось обрезкой первым.

    Пропадала ровно та строка, которая была единственным признаком, что
    ответов было больше показанных. Письма при этом уже отмечены
    прочитанными: второй раз их никто не покажет.
    """
    from orbit.notify import _clip

    messages = [f"Ответ номер {number}. " + "текст " * 150 for number in range(12)]
    joined = inbox._one_message(messages)
    final = _clip(joined)

    assert "Писем за этот заход: 12" in final
    assert final.index("Писем за этот заход") < 200, "счётчик обязан быть в самом начале"


def test_a_small_batch_stays_plain():
    """Три коротких ответа — просто три ответа, без служебной шапки."""
    joined = inbox._one_message(["первый", "второй", "третий"])

    assert "Писем за этот заход" not in joined
    assert joined.count("———") == 2


def test_the_batch_shows_more_than_five_when_they_fit():
    """Короткие ответы не должны обрезаться на пятом ради круглого числа."""
    messages = [f"Короткий ответ {number}" for number in range(9)]
    joined = inbox._one_message(messages)

    assert all(f"Короткий ответ {number}" in joined for number in range(9))


def test_address_dead_for_the_whole_network_stays_dead(store):
    """Общий ящик сети вычеркнут у всех — импорт не должен вернуть его никому.

    Адрес убирался своим UPDATE мимо памяти о мёртвых ящиках, и следующее
    «Загрузить из парсера» возвращало его всем филиалам разом.
    """
    from orbit.models import Contact, Kind, Stage

    pks = []
    for number in range(3):
        contact = Contact(
            kind=Kind.CLIENT, key=f"branch{number}", name=f"Филиал {number}",
            emails=["info@shared.ru", f"b{number}@own.ru"], stage=Stage.WRITTEN,
        )
        pks.append(store.upsert_contact(contact))

    store.close_same_email("info@shared.ru")

    for number, pk in enumerate(pks):
        store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"branch{number}", name=f"Филиал {number}",
            emails=["info@shared.ru", f"b{number}@own.ru"],
        ))
        assert "info@shared.ru" not in store.get_contact(pk).emails, f"филиал {number}"
        assert f"b{number}@own.ru" in store.get_contact(pk).emails


# --- Отчёт о доставке — не возврат ---------------------------------------------


_SUCCESS_DSN = """This is the mail system at host mail.uniqore.ru.

Your message was successfully delivered.

Final-Recipient: rfc822; info@mediasfera.ru
Action: delivered
Status: 2.0.0
Remote-MTA: dns; mx.mediasfera.ru
"""


def test_successful_delivery_report_is_not_a_bounce():
    """«Доставлено» и «не доставлено» приходят с одной темы и одного адреса.

    Отчёт об успешной доставке (RFC 3464, Action: delivered) шлют шлюзы и
    серверы с включёнными подтверждениями. Тема у него та же — «Delivery
    Status Notification», отправитель тот же mailer-daemon. Раньше он
    разбирался как окончательный отказ: живой адрес вычёркивался, а компания
    с одним ящиком закрывалась навсегда ровно в ту минуту, когда письмо дошло.
    """
    letter = inbox.Letter(
        uid=b"1",
        sender="mailer-daemon@uniqore.ru",
        subject="Delivery Status Notification (Success)",
        body=_SUCCESS_DSN,
    )
    assert letter.delivery_succeeded
    assert letter.kind == inbox.OTHER, "это не возврат и не ответ человека"


def test_successful_delivery_report_leaves_the_contact_alone(store):
    """Контакт после отчёта о доставке остаётся с живым адресом и стадией."""
    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="mediasfera.ru", name="Медиасфера",
        emails=["info@mediasfera.ru"], stage=Stage.WRITTEN,
    ))
    letter = inbox.Letter(
        uid=b"1",
        sender="mailer-daemon@uniqore.ru",
        subject="Delivery Status Notification (Success)",
        body=_SUCCESS_DSN,
    )
    assert inbox.handle(store, letter) is None, "владельца этим тревожить нечем"

    contact = store.get_contact(pk)
    assert contact.emails == ["info@mediasfera.ru"], "адрес вычёркивать не за что"
    assert contact.stage is Stage.WRITTEN, "стадию трогать незачем"


@pytest.mark.parametrize("action", ["relayed", "expanded"])
def test_relayed_and_expanded_are_not_bounces_either(action):
    """Шлюз передал дальше, список рассылки развернул — оба не отказ."""
    body = _SUCCESS_DSN.replace("Action: delivered", f"Action: {action}")
    letter = inbox.Letter(uid=b"1", sender="mailer-daemon@uniqore.ru",
                          subject="Delivery Status Notification", body=body)
    assert letter.kind == inbox.OTHER


def test_a_real_bounce_is_still_a_bounce():
    """Проверка выше не должна проглотить настоящий отказ."""
    body = _SUCCESS_DSN.replace("Action: delivered", "Action: failed").replace(
        "Status: 2.0.0", "Status: 5.1.1"
    )
    letter = inbox.Letter(uid=b"1", sender="mailer-daemon@uniqore.ru",
                          subject="Undelivered Mail Returned to Sender", body=body)
    assert not letter.delivery_succeeded
    assert letter.kind == inbox.BOUNCE
    assert not letter.temporary_bounce


# --- Ответ клиента из html-письма ---------------------------------------------


def _html_letter(html: str):
    """Письмо без текстовой части — только html, как шлют Outlook и Битрикс.

    Собираем и разбираем обратно из байтов — так же, как приходит из ящика:
    на строке, собранной руками, кодировка тела не проверяется вовсе.
    """
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "Re: сотрудничество"
    message.set_content(html, subtype="html", charset="utf-8")
    return email.message_from_bytes(message.as_bytes())


def test_styles_do_not_eat_the_reply():
    """Содержимое <style> — не текст письма.

    Резали одним `<[^>]+>`, а у style и script содержимое лежит МЕЖДУ тегами:
    ответ клиента приезжал владельцу начинённым таблицами стилей, и на письмах
    из Outlook настоящий текст в предпросмотр уже не помещался.
    """
    html = (
        "<html><head><title>Письмо</title>"
        "<style>body{font-family:Arial;color:#333}p{margin:0 0 12px}</style></head>"
        "<body><p>Здравствуйте! Нам это интересно.</p>"
        "<script>window.track('open')</script>"
        "<p>Позвоните завтра.</p></body></html>"
    )
    text = inbox.body_of(_html_letter(html))
    assert "font-family" not in text and "Arial" not in text
    assert "window.track" not in text
    assert "Здравствуйте! Нам это интересно." in text
    assert "Позвоните завтра." in text


def test_paragraphs_do_not_glue_together():
    """Абзацы письма должны остаться абзацами, а не одной лентой."""
    html = "<body><p>Первая строка.</p><p>Вторая строка.</p></body>"
    text = inbox.body_of(_html_letter(html))
    assert "Первая строка." in text and "Вторая строка." in text
    assert "Первая строка.Вторая" not in text
    assert "\n" in text, "между абзацами должен остаться перевод строки"


def test_html_entities_come_back_as_characters():
    """«&nbsp;» и «&amp;» в предпросмотре должны читаться, а не мозолить глаза."""
    html = "<body><p>Иванов&nbsp;&amp;&nbsp;Ко &mdash; согласны.</p></body>"
    text = inbox.body_of(_html_letter(html))
    assert "&nbsp;" not in text and "&amp;" not in text
    assert "Иванов" in text and "Ко" in text and "согласны" in text


def test_comments_are_not_part_of_the_reply():
    """Комментарии Outlook (<!--[if mso]-->) — не слова клиента."""
    html = "<body><!--[if mso]><table><tr><td>служебное<![endif]--><p>Готовы работать.</p></body>"
    text = inbox.body_of(_html_letter(html))
    assert "служебное" not in text
    assert "Готовы работать." in text


# --- Достижим ли контакт после возврата ---------------------------------------


def test_a_free_mail_leftover_does_not_count_as_a_live_channel(store):
    """Второй адрес на mail.ru — не канал: писать туда холодные письма нельзя.

    Достижимость считалась длиной списка адресов. Непустой список объявлял
    компанию живой: владельцу сообщалось «остальные каналы живы — попробую
    через них», а на деле писать было некуда, и контакт зависал навсегда.
    """
    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="mediasfera.ru", name="Медиасфера",
        emails=["hello@mediasfera.ru", "ivan@mail.ru"], stage=Stage.WRITTEN,
    ))
    letter = inbox.Letter(
        uid=b"1", sender="mailer-daemon@uniqore.ru",
        subject="Undelivered Mail Returned to Sender",
        body="Final-Recipient: rfc822; hello@mediasfera.ru\nAction: failed\nStatus: 5.1.1\n",
    )
    said = inbox.handle(store, letter)

    contact = store.get_contact(pk)
    assert contact.emails == ["ivan@mail.ru"], "мёртвый адрес обязан уйти"
    assert not contact.reachable, "личный ящик каналом рассылки не является"
    assert contact.stage is Stage.DEAD, "писать некуда — контакт закрыт"
    assert said and "Больше туда не пишем" in said, said


def test_a_second_corporate_address_keeps_the_contact_alive(store):
    """А второй корпоративный ящик — настоящий живой канал."""
    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="mediasfera.ru", name="Медиасфера",
        emails=["hello@mediasfera.ru", "sales@mediasfera.ru"], stage=Stage.WRITTEN,
    ))
    letter = inbox.Letter(
        uid=b"1", sender="mailer-daemon@uniqore.ru",
        subject="Undelivered Mail Returned to Sender",
        body="Final-Recipient: rfc822; hello@mediasfera.ru\nAction: failed\nStatus: 5.1.1\n",
    )
    said = inbox.handle(store, letter)

    contact = store.get_contact(pk)
    assert contact.stage is Stage.WRITTEN
    assert contact.reachable
    assert said and "остальные каналы живы" in said, said


def test_bounce_on_a_shared_mailbox_cleans_the_siblings(store):
    """Возврат нашёл филиал в разговоре — соседи по общему ящику всё равно чистятся.

    Ящик на сайте сети один на все филиалы, а contact_by_email отдаёт первую
    подходящую строку без всякого порядка: кто из филиалов попадётся, дело
    случая. Раньше, если попадался тот, с кем уже идёт разговор, разбор
    выходил раньше вызова close_same_email — и мёртвый адрес оставался у всех
    остальных. Им туда продолжали уходить холодные письма, каждое за деньги и
    каждое обратно.
    """
    talking = _with_addresses(store, "филиал-в-разговоре", "hello@mediasfera.ru",
                              stage=Stage.DEAL)
    cold = _with_addresses(store, "филиал-холодный", "hello@mediasfera.ru",
                           stage=Stage.WRITTEN)

    result = inbox.handle(store, letter(
        sender="mailer-daemon@beget.com", subject="Undelivered Mail", body=BOUNCE_BODY,
    ))

    assert result and "не закрываю" in result
    assert "Закрыл ещё 1" in result
    assert store.get_contact(talking).stage is Stage.DEAL
    assert store.get_contact(cold).stage is Stage.DEAD
    assert "hello@mediasfera.ru" not in store.get_contact(cold).emails


def test_the_model_reads_the_whole_reply_not_the_preview(store, agency_in_db, monkeypatch):
    """Разбор получает письмо целиком, а не то, что помещается в уведомление.

    PREVIEW — ширина экрана, а не глубина разбора. На семистах знаках обрезался
    как раз хвост, ради которого разбор и делается: вступление помещалось, а
    срок «вернёмся в сентябре» — уже нет.
    """
    seen: dict[str, str] = {}

    def remember(contact, subject, body, our_letter=None):
        seen["body"] = body
        return None

    monkeypatch.setattr(inbox.llm, "classify_reply", remember)

    tail = "вернёмся к этому в сентябре"
    body = "Здравствуйте! " + "Спасибо за письмо, изучаем предложение. " * 40 + tail
    assert len(body) > inbox.PREVIEW

    inbox.handle(store, letter(body=body))

    assert tail in seen["body"], "модель не увидела хвост письма"


def test_a_recreated_mailbox_does_not_re_read_the_archive(store, monkeypatch, agency_in_db):
    """Место в ящике потеряно — читаем последние сутки, а не весь архив.

    Пересозданный ящик обнуляет отметку, и запрос «дай всё» поднимал всю
    переписку за месяцы: владельцу сотни уведомлений о письмах, на которые он
    отвечал полгода назад, модели — деньги за каждое, а стадии контактов
    менялись задним числом. Свежий ответ при этом всё равно должен дойти.
    """
    seen: list[str] = []

    class Watching(_Box):
        def uid(self, command, *args):
            if command == "search":
                seen.append(str(args[-1]))
            return super().uid(command, *args)

    store.set_state(inbox.LAST_READ_KEY, "2026-08-20")
    store.set_state(inbox.LAST_UID_KEY, "900")
    store.set_state(inbox.UID_VALIDITY_KEY, "111")

    monkeypatch.setattr(inbox, "connect", lambda settings=None: Watching(b"222"))
    assert len(inbox.fetch(store, limit=10)) == 3, "свежие письма всё равно должны дойти"
    assert "SINCE 19-Aug-2026" in seen[-1], seen

    # А когда место в ящике известно, никакого SINCE быть не должно: там
    # отметка сама отсекает прочитанное, а лишнее условие обрежет письмо,
    # которое пролежало в очереди дольше суток.
    seen.clear()
    monkeypatch.setattr(inbox, "connect", lambda settings=None: Watching(b"222", uids=(4,)))
    inbox.fetch(store, limit=10)
    assert "SINCE" not in seen[-1], seen

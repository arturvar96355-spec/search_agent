"""Проверки перед отправкой — здесь цена ошибки самая высокая.

Отправленное письмо не отзывается. Поэтому каждая проверка описана отдельным
тестом: превышение лимита, закрытый контакт, ненастроенная почта, повторная
отправка одного и того же письма.
"""
from __future__ import annotations

import smtplib

import pytest

from orbit import config, outreach
from orbit.models import Channel, Contact, Draft, Kind, Stage


@pytest.fixture()
def prepared(store, agency):
    """Агентство с готовым письмом в очереди."""
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте, по делу."
    ))
    return store.get_draft(draft_pk), store.get_contact(pk)


@pytest.fixture()
def smtp_ready(monkeypatch):
    """Как будто почта настроена (сама отправка при этом замокана в тестах)."""
    monkeypatch.setenv("SMTP_HOST", "smtp.yandex.ru")
    monkeypatch.setenv("SMTP_USER", "partnership@uniqore.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "секрет")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


# --- Что мешает отправке ----------------------------------------------------

def test_without_smtp_letter_does_not_leave(store, prepared):
    draft, contact = prepared
    verdict = outreach.can_send(store, draft, contact)
    assert not verdict.ok
    assert "почта не настроена" in verdict.reason


def test_with_smtp_letter_is_allowed(store, prepared, smtp_ready):
    draft, contact = prepared
    assert outreach.can_send(store, draft, contact).ok


def test_daily_limit_blocks(store, prepared, smtp_ready):
    """Норма касаний вкладки исчерпана — письмо не уходит."""
    draft, contact = prepared
    # Ручные телеграм-отправки: норму касаний они тратят, прогрев ящика — нет.
    for _ in range(outreach.tab_limit(store, Kind.PARTNER)):
        store.add_send(Kind.PARTNER, by_mail=False)
    verdict = outreach.can_send(store, draft, contact)
    assert not verdict.ok
    assert "суточный лимит исчерпан" in verdict.reason


def test_warmup_blocks_the_mailbox_separately(store, prepared, smtp_ready):
    """Норма прогрева ЯЩИКА считается отдельно и только по письмам.

    Две нормы меряют разное: касания — про компанию, прогрев — про ящик.
    Одна общая цифра сваливала их в кучу, и ручной телеграм съедал ящику
    прогревную норму, которой он не пользовался.
    """
    draft, contact = prepared
    allowed = outreach.warmup_limit(store, Kind.PARTNER)
    assert allowed < outreach.tab_limit(store, Kind.PARTNER), "иначе проверка ничего не покажет"

    for _ in range(allowed):
        store.add_send(Kind.CLIENT, by_mail=True)  # ящик один на обе вкладки

    verdict = outreach.can_send(store, draft, contact)
    assert not verdict.ok
    assert "прогрев ящика" in verdict.reason, verdict.reason


def test_dead_contact_is_never_written_to(store, prepared, smtp_ready):
    """«Стоп» в ответе закрывает контакт навсегда."""
    draft, contact = prepared
    store.set_stage(contact.pk, Stage.DEAD)
    verdict = outreach.can_send(store, draft, store.get_contact(contact.pk))
    assert not verdict.ok


def test_already_sent_draft_is_not_sent_twice(store, prepared, smtp_ready):
    draft, contact = prepared
    store.mark_sent(draft.pk)
    verdict = outreach.can_send(store, store.get_draft(draft.pk), contact)
    assert not verdict.ok
    assert "уже отправлено" in verdict.reason


def test_telegram_needs_hands(store, agency, smtp_ready):
    """Бот не может написать первым — это ограничение Telegram, не сбой."""
    agency.emails = []
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Привет, по делу."
    ))
    verdict = outreach.can_send(store, store.get_draft(draft_pk), store.get_contact(pk))
    assert verdict.ok and verdict.manual


# --- Отправка ---------------------------------------------------------------

def test_send_marks_and_counts(store, prepared, smtp_ready, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        outreach, "deliver_email", lambda to, subject, body: sent.append((to, subject, body))
    )
    draft, contact = prepared

    result = outreach.send(store, draft.pk)

    assert result.ok
    assert sent and sent[0][0] == "hello@mediasfera.ru"
    assert store.sent_today(Kind.PARTNER) == 1
    assert store.get_contact(contact.pk).stage is Stage.WRITTEN
    assert store.queue_size(Kind.PARTNER) == 0


def test_broken_smtp_does_not_lose_the_letter(store, prepared, smtp_ready, monkeypatch):
    """Почта отвалилась — письмо остаётся в очереди с пометкой, а не исчезает."""
    def boom(*_args):
        raise RuntimeError("сервер отверг пароль")

    monkeypatch.setattr(outreach, "deliver_email", boom)
    draft, _contact = prepared

    result = outreach.send(store, draft.pk)

    assert not result.ok
    assert store.queue_size(Kind.PARTNER) == 1
    assert store.sent_today(Kind.PARTNER) == 0
    assert "пароль" in (store.get_draft(draft.pk).error or "")


def test_manual_send_counts_toward_limit(store, agency):
    """Отправленное руками в телеграме — это тоже письмо адресату."""
    agency.emails = []
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Привет."
    ))
    result = outreach.mark_sent_manually(store, draft_pk)
    assert result.ok
    assert store.sent_today(Kind.PARTNER) == 1
    assert store.get_contact(pk).stage is Stage.WRITTEN


# --- Текст письма -----------------------------------------------------------

def test_signature_added(store, prepared):
    draft, contact = prepared
    text = outreach.compose(draft, contact)
    assert outreach.SIGNATURE in text


def test_no_unsubscribe_footer_anywhere(store, prepared, agency):
    """Приписка «ответьте стоп» выдаёт рассылку с порога.

    Убрана 07.08.2026 после первой живой проверки: человек, написавший одному
    адресату, такое не добавляет. Право отказаться при этом сохраняется —
    ответ «стоп» по-прежнему закрывает контакт навсегда.
    """
    draft, contact = prepared
    for text in (
        outreach.compose(draft, contact),
        outreach.compose(Draft(contact_pk=1, channel=Channel.TELEGRAM, subject="", body="Привет."), agency),
    ):
        assert "стоп" not in text.lower()
        assert "отпис" not in text.lower()


def test_stop_reply_still_closes_the_contact():
    """Убрали приписку, а не саму возможность отказаться."""
    assert outreach.looks_like_stop("стоп, не интересно")


# --- Лимиты -----------------------------------------------------------------

def test_limit_default_and_override(store):
    assert outreach.tab_limit(store, Kind.PARTNER) == 20
    store.set_state(outreach.LIMIT_KEYS[Kind.PARTNER], "35")
    assert outreach.tab_limit(store, Kind.PARTNER) == 35


def test_limit_cannot_exceed_hard_cap(store):
    """Кнопкой можно поднять лимит, но не до бесконечности: домен дороже."""
    store.set_state(outreach.LIMIT_KEYS[Kind.PARTNER], "100000")
    assert outreach.tab_limit(store, Kind.PARTNER) == outreach.MAX_DAILY


def test_broken_limit_falls_back_to_default(store):
    store.set_state(outreach.LIMIT_KEYS[Kind.CLIENT], "двадцать")
    assert outreach.tab_limit(store, Kind.CLIENT) == 20


def test_autosend_off_by_default(store):
    assert outreach.autosend_on(store, Channel.EMAIL) is False
    assert outreach.autosend_on(store, Channel.TELEGRAM) is False


def test_channels_toggle_independently(store):
    """Почта и телеграм включаются раздельно: у них разные риски."""
    outreach.set_autosend(store, Channel.EMAIL, True)
    assert outreach.autosend_on(store, Channel.EMAIL) is True
    assert outreach.autosend_on(store, Channel.TELEGRAM) is False

    outreach.set_autosend(store, Channel.TELEGRAM, True)
    outreach.set_autosend(store, Channel.EMAIL, False)
    assert outreach.autosend_on(store, Channel.EMAIL) is False
    assert outreach.autosend_on(store, Channel.TELEGRAM) is True


def test_old_setting_still_applies_to_mail(store):
    """У того, кто уже включил автоотправку, она не выключится после обновления."""
    store.set_state(outreach.LEGACY_AUTOSEND_KEY, "1")
    assert outreach.autosend_on(store, Channel.EMAIL) is True


def test_telegram_channel_is_not_ready_by_itself(store):
    """Бот не может написать человеку первым — это ограничение Telegram."""
    ready, why = outreach.channel_ready(Channel.TELEGRAM)
    assert ready is False
    assert "не может написать первым" in why


def test_mail_channel_ready_only_with_smtp(store, smtp_ready):
    assert outreach.channel_ready(Channel.EMAIL)[0] is True


@pytest.mark.parametrize("text", ["стоп", "Стоп, спасибо", "не пишите мне", "отписка"])
def test_stop_words_recognized(text):
    assert outreach.looks_like_stop(text)


@pytest.mark.parametrize("text", ["интересно, расскажите", "", "а сколько стоит?"])
def test_normal_replies_are_not_stop(text):
    assert not outreach.looks_like_stop(text)


# --- Дефекты, найденные аудитом 07.08.2026 ---------------------------------

def test_telegram_draft_does_not_block_the_mail_queue(store, agency, company, smtp_ready, monkeypatch):
    """Черновик в телеграм стоит первым — почта всё равно должна уходить.

    Бот не может написать в телеграм первым, поэтому такой черновик остаётся
    одобренным навсегда. Раньше отправщик каждый круг «отправлял» именно его,
    а живые письма не уходили вовсе.
    """
    telegram_contact = company
    telegram_contact.kind = Kind.PARTNER
    telegram_contact.key = "tg-agency.ru"
    telegram_contact.emails = []
    telegram_contact.telegram = ["https://t.me/agency_msk"]
    tg_pk = store.upsert_contact(telegram_contact)
    store.save_draft(Draft(
        contact_pk=tg_pk, channel=Channel.TELEGRAM, subject="", body="Привет!"
    ))

    mail_pk = store.upsert_contact(agency)
    store.save_draft(Draft(
        contact_pk=mail_pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте."
    ))

    outreach.approve_queue(store, Kind.PARTNER)
    sent = []
    monkeypatch.setattr(outreach, "deliver_email", lambda to, s, b: sent.append(to))

    result = outreach.send_next(store, Kind.PARTNER)

    assert result is not None and result.ok
    assert sent == ["hello@mediasfera.ru"], "уйти должно письмо, а не телеграм-черновик"


def test_dead_address_is_dropped_after_three_tries(store, prepared, smtp_ready, monkeypatch):
    """Три неудачи подряд — снимаем письмо с отправки.

    Иначе мёртвый адрес превращается в бесконечный поток подключений к
    почтовому серверу, и хостер блокирует ящик целиком.
    """
    draft, _contact = prepared

    def refuse(*_args, **_kwargs):
        # Именно отказ ПОЛУЧАТЕЛЯ: наши собственные сбои (пароль, сеть) счётчик
        # не трогают — иначе одна опечатка в .env хоронит всю очередь.
        import smtplib

        raise smtplib.SMTPRecipientsRefused({"hello@mediasfera.ru": (550, b"User unknown")})

    monkeypatch.setattr(outreach, "deliver_email", refuse)

    for _ in range(outreach.MAX_ATTEMPTS):
        store.approve_draft(draft.pk)
        outreach.send(store, draft.pk)

    after = store.get_draft(draft.pk)
    assert after.status == "failed"
    assert after.approved_at is None
    assert "попыток" in (after.error or "")
    assert store.approved_drafts(Kind.PARTNER) == []


def test_letter_is_not_sent_after_the_client_answered(store, prepared, smtp_ready):
    """Пока письмо ждало очереди, человек ответил — отправлять его уже поздно."""
    draft, contact = prepared
    store.set_stage(contact.pk, Stage.REPLIED, "ответили")

    verdict = outreach.can_send(store, draft, store.get_contact(contact.pk))

    assert not verdict.ok
    assert "разговор" in verdict.reason


def test_failure_does_not_skip_the_pause(store, prepared, smtp_ready, monkeypatch):
    """После неудачи пауза обязательна — иначе получается штурм сервера."""
    draft, _contact = prepared
    store.approve_draft(draft.pk)
    monkeypatch.setattr(
        outreach, "deliver_email",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("сеть отвалилась")),
    )
    import time as real_time

    pauses = []
    monkeypatch.setattr(real_time, "sleep", pauses.append)
    outreach._sender_round(store)

    assert pauses, "после неудачной отправки отправщик обязан подождать"


# --- Прогрев ящика ----------------------------------------------------------

def test_new_mailbox_starts_with_a_few_letters(store, smtp_ready):
    """Первый день — три письма, а не двадцать.

    Новый ящик, разославший двадцать холодных писем в первый же день,
    уезжает в спам вместе с доменом: истории отправок у него нет, и для
    почтового сервера это выглядит как взломанный аккаунт.
    """
    assert outreach.letters_left_today(store, Kind.PARTNER) == outreach.WARMUP_STEPS[0]


def _sent_on(store, *days: str) -> None:
    """Помечает дни, в которые из ящика уходили письма."""
    for day in days:
        store.conn.execute(
            "INSERT OR REPLACE INTO sends (day, kind, count, by_mail) VALUES (?, 'partner', 1, 1)",
            (day,),
        )
    store.conn.commit()


def test_warmup_grows_with_days_that_really_sent(store, smtp_ready):
    """Ступень прогрева — это дни ОТПРАВОК, а не дни календаря.

    Ящик, отправивший восемь писем в один день и замолчавший на три недели,
    прогретым не стал. Календарный отсчёт объявлял его прогретым на пятый
    день, и первый заход после паузы шёл с потолка в двести писем.
    """
    from datetime import date, timedelta

    from orbit.store import today_msk

    today = date.fromisoformat(today_msk())
    assert outreach.letters_left_today(store, Kind.PARTNER) == outreach.WARMUP_STEPS[0]
    for step, expected in enumerate(outreach.WARMUP_STEPS[1:], start=1):
        _sent_on(store, (today - timedelta(days=step)).isoformat())
        assert outreach.letters_left_today(store, Kind.PARTNER) == expected


def test_a_long_silence_starts_the_warmup_over(store, smtp_ready):
    """Ящик, молчавший неделю, для принимающей стороны снова новый."""
    from datetime import date, timedelta

    from orbit.store import today_msk

    today = date.fromisoformat(today_msk())
    # Пять дней подряд отправляли — прогрев пройден…
    _sent_on(store, *[(today - timedelta(days=n)).isoformat() for n in range(20, 15, -1)])
    # …но с тех пор прошло больше недели.
    assert outreach.letters_left_today(store, Kind.PARTNER) == outreach.WARMUP_STEPS[0]


def test_after_warmup_the_usual_limit_applies(store, smtp_ready):
    from datetime import date, timedelta

    from orbit.store import today_msk

    today = date.fromisoformat(today_msk())
    _sent_on(store, *[(today - timedelta(days=n)).isoformat() for n in range(6, 0, -1)])
    assert outreach.letters_left_today(store, Kind.PARTNER) == 20


def test_warmup_does_not_apply_without_mail(store):
    """Почта не настроена — прогревать нечего, лимит обычный."""
    assert outreach.tab_limit(store, Kind.PARTNER) == 20


def test_warmup_clock_starts_at_the_first_letter_not_at_the_screen(
    store, prepared, smtp_ready, monkeypatch
):
    """Открыть экран — не то же самое, что начать рассылку.

    Отсчёт прогрева ставила сама функция лимита, а её зовёт отрисовка экрана.
    Достаточно было открыть вкладку и уйти: часы шли, письма не уходили, и
    через неделю прогрев считался пройденным — первый настоящий заход начался
    бы с потолка в двести писем с холодного ящика.
    """
    from orbit.store import today_msk

    for _ in range(3):
        assert outreach.letters_left_today(store, Kind.PARTNER) == outreach.WARMUP_STEPS[0]
    assert not store.mail_days(), "часы пошли от одного взгляда"

    monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: None)
    draft, _contact = prepared
    store.approve_draft(draft.pk)
    assert outreach.send(store, draft.pk).ok

    assert store.get_state(outreach.WARMUP_KEY) == today_msk()


# --- Куда именно писать -----------------------------------------------------

def test_role_mailbox_wins_over_personal(agency):
    """info@ — обращение к организации, ivanov@ — персональные данные.

    По 152-ФЗ это разные вещи, поэтому при выборе берём обезличенный.
    """
    agency.emails = ["p.ivanov@mediasfera.ru", "info@mediasfera.ru"]
    assert agency.best_email == "info@mediasfera.ru"
    assert agency.personal_only is False


def test_personal_mailbox_is_used_when_there_is_nothing_else(agency):
    agency.emails = ["p.ivanov@mediasfera.ru"]
    assert agency.best_email == "p.ivanov@mediasfera.ru"
    assert agency.personal_only is True


def test_role_mailbox_with_a_suffix_is_still_a_role_mailbox():
    from orbit.models import is_role_email

    assert is_role_email("info.msk@firma.ru") is True
    assert is_role_email("sales-spb@firma.ru") is True
    assert is_role_email("ivanov@firma.ru") is False
    assert is_role_email("info@gmail.com") is False


# --- Счётчик попыток меряет доставку, а не отказы до неё -----------------------


def _queued_draft(store):
    from orbit.models import Channel, Contact, Draft, Kind

    contact = Contact(kind=Kind.PARTNER, key="k", name="Компания", emails=["info@a.ru"])
    pk = store.upsert_contact(contact)
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
    draft = store.draft_for(pk)
    store.approve_draft(draft.pk)
    return draft.pk


def test_refusal_before_delivery_is_not_an_attempt(store, monkeypatch):
    """Почта не настроена — письмо не собиралось, попыткой доставки это не было.

    Иначе три отказа подряд снимали письмо с очереди навсегда за то, что мы
    сами его придержали.
    """
    draft_pk = _queued_draft(store)
    for _ in range(5):
        store.approve_draft(draft_pk)
        outreach.send(store, draft_pk)
    draft = store.get_draft(draft_pk)
    assert draft.attempts == 0
    assert draft.status == "draft"      # письмо всё ещё ждёт своего часа


def test_silent_dns_does_not_burn_the_attempts(store, smtp_ready, monkeypatch):
    """DNS молчит — это временно, письмо должно дождаться."""
    from orbit import mailcheck

    draft_pk = _queued_draft(store)
    monkeypatch.setattr(
        mailcheck, "domain_state",
        lambda domain: (mailcheck.DomainState.UNKNOWN, "DNS не ответил вовремя"),
    )
    for _ in range(5):
        store.approve_draft(draft_pk)
        outreach.send(store, draft_pk)
    assert store.get_draft(draft_pk).attempts == 0
    assert store.get_draft(draft_pk).status == "draft"


def test_real_delivery_failure_still_counts(store, smtp_ready, monkeypatch):
    """А отказ почтового сервера — считается, и после трёх письмо снимаем."""

    def refuse(*args, **kwargs):
        raise RuntimeError("550 mailbox unavailable")

    monkeypatch.setattr(outreach, "deliver_email", refuse)
    draft_pk = _queued_draft(store)
    for _ in range(outreach.MAX_ATTEMPTS):
        store.approve_draft(draft_pk)
        outreach.send(store, draft_pk)
    draft = store.get_draft(draft_pk)
    assert draft.attempts == outreach.MAX_ATTEMPTS
    assert draft.status == "failed"


# --- Двойное нажатие «Отправил» ------------------------------------------------


def _telegram_draft(store):
    """Сообщение в телеграм: только их владелец отправляет руками."""
    from orbit.models import Channel, Contact, Draft, Kind

    contact = Contact(kind=Kind.PARTNER, key="tg", name="Компания",
                      telegram=["https://t.me/company_chat"])
    pk = store.upsert_contact(contact)
    store.save_draft(Draft(contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Текст"))
    return store.draft_for(pk).pk


def test_manual_send_counts_once(store):
    """Экран не обновился, палец соскользнул — второе нажатие не съедает норму."""
    from orbit.models import Kind

    draft_pk = _telegram_draft(store)
    assert outreach.mark_sent_manually(store, draft_pk).ok
    before = store.sent_today(Kind.PARTNER)

    result = outreach.mark_sent_manually(store, draft_pk)
    assert not result.ok
    assert "уже отмечено" in result.reason
    assert store.sent_today(Kind.PARTNER) == before


# --- «Стоп» не только первым словом --------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Стоп, не пишите",
        "Здравствуйте.\nСтоп, не пишите нам больше",
        "Добрый день!\n\nНе пишите нам, пожалуйста.",
        "Отписка",
        "unsubscribe",
    ],
)
def test_stop_is_found_in_the_first_lines(body):
    """Человек здоровается, а просьбу отстать пишет второй строкой.

    Раньше смотрели только самое первое слово письма, и такой ответ не
    ловился вовсе — мы продолжали писать тому, кто попросил перестать.
    """
    assert outreach.looks_like_stop(body)


@pytest.mark.parametrize(
    "body",
    [
        "Интересно, расскажите подробнее",
        "Спасибо! Пришлите примеры.\nЖдём.",
        "Здравствуйте\n> стоп\n> это цитата нашего письма",
        # Граница — пять первых непустых строк (STOP_LINES). Дальше идёт уже
        # разговор, и «стоп» там относится к чему угодно, только не к нам.
        # Пять, а не три, потому что вежливый отказ пишут после приветствия и
        # «спасибо за предложение» — на трёх строках он не помещался.
        "а\nб\nв\nг\nд\nстоп",
    ],
)
def test_normal_answer_is_not_a_stop(body):
    """Обратная ошибка дороже вдвойне: закрыть живой контакт по одному слову."""
    assert not outreach.looks_like_stop(body)


def test_manual_send_records_the_fact_even_over_the_limit(store, monkeypatch):
    """Сообщение уже ушло руками — отказ его записать не вернёт его назад.

    Раньше кнопка при исчерпанном лимите отвечала отказом, и база расходилась
    с реальностью: черновик оставался «неотправленным», и тот же текст мог
    уйти человеку второй раз. Факт записываем всегда, а про выход за рамки
    говорим в ответе.
    """
    from orbit import config
    from orbit.models import Kind

    monkeypatch.setenv("DAILY_LIMIT_PARTNERS", "0")
    config.get_settings.cache_clear()
    try:
        draft_pk = _telegram_draft(store)
        result = outreach.mark_sent_manually(store, draft_pk)
        assert result.ok, "свершившийся факт обязан записаться"
        assert "лимит" in result.reason, "но владельцу говорим, что вышли за рамки"
        assert store.sent_today(Kind.PARTNER) == 1
        assert store.get_draft(draft_pk).status == "sent"
    finally:
        config.get_settings.cache_clear()


def test_manual_send_does_not_revive_a_closed_contact(store):
    """Факт записывается, но закрытый контакт не воскресает."""
    from orbit.models import Stage

    draft_pk = _telegram_draft(store)
    contact_pk = store.get_draft(draft_pk).contact_pk
    store.set_stage(contact_pk, Stage.DEAD, "просил не писать")

    result = outreach.mark_sent_manually(store, draft_pk)
    assert result.ok, "сообщение уже ушло — запись обязана случиться"
    assert "закрыт" in result.reason, "владельцу говорим, что человек просил не писать"
    assert store.get_contact(contact_pk).stage is Stage.DEAD, "стадия не откатывается"
    assert store.get_draft(draft_pk).status == "sent"


# --- Наша беда и беда адресата — разные вещи -----------------------------------


@pytest.mark.parametrize(
    "error",
    [
        __import__("smtplib").SMTPAuthenticationError(535, b"bad password"),
        __import__("smtplib").SMTPConnectError(421, b"cannot connect"),
        __import__("smtplib").SMTPServerDisconnected("сервер оборвал связь"),
        ConnectionRefusedError("сеть недоступна"),
        TimeoutError("не дождались"),
    ],
)
def test_our_own_failure_does_not_burn_the_letter(store, prepared, smtp_ready, monkeypatch, error):
    """Опечатка в пароле или пропавшая сеть не должны хоронить очередь.

    Счётчик попыток меряет отказы АДРЕСАТА. Без разделения одна неверная
    строка в .env за три круга снимала с отправки каждое письмо навсегда,
    а причина оставалась только в логе.
    """
    def boom(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(outreach, "deliver_email", boom)
    draft, _contact = prepared

    for _ in range(outreach.MAX_ATTEMPTS + 2):
        store.approve_draft(draft.pk)
        outreach.send(store, draft.pk)

    after = store.get_draft(draft.pk)
    assert after.status == "draft"      # письмо ждёт, а не похоронено
    assert after.attempts == 0


def test_recipient_refusal_still_counts(store, prepared, smtp_ready, monkeypatch):
    """А отказ получателя считается — иначе мёртвый адрес долбится вечно."""
    import smtplib

    def refuse(*_args, **_kwargs):
        raise smtplib.SMTPRecipientsRefused({"hello@mediasfera.ru": (550, b"User unknown")})

    monkeypatch.setattr(outreach, "deliver_email", refuse)
    draft, _contact = prepared

    store.approve_draft(draft.pk)
    outreach.send(store, draft.pk)
    assert store.get_draft(draft.pk).attempts == 1


# --- Падение посреди отправки ---------------------------------------------------


def test_crash_mid_send_does_not_resend_the_letter(store, prepared, smtp_ready, monkeypatch):
    """Процесс умер между «сервер принял» и «мы записали» — второй раз не шлём.

    Письмо могло уже уйти. Повторная отправка того же холодного письма тому же
    человеку хуже неотправленного: это уже не рассылка, а навязчивость.
    """
    draft, _contact = prepared

    def die(*_args, **_kwargs):
        raise KeyboardInterrupt("процесс убили посреди разговора с почтой")

    monkeypatch.setattr(outreach, "deliver_email", die)
    store.approve_draft(draft.pk)
    with pytest.raises(KeyboardInterrupt):
        outreach.send(store, draft.pk)

    assert store.release_stale_sending()[0] == 0, "такое письмо в очередь возвращать нельзя"
    after = store.get_draft(draft.pk)
    assert after.status == "failed"
    assert "неизвестно" in (after.error or "")


def test_known_failure_returns_the_letter_to_the_queue(store, prepared, smtp_ready, monkeypatch):
    """А если почта честно отказала — исход известен, письмо ждёт следующего круга."""
    def boom(*_args, **_kwargs):
        raise ConnectionRefusedError("сеть недоступна")

    monkeypatch.setattr(outreach, "deliver_email", boom)
    draft, _contact = prepared
    store.approve_draft(draft.pk)
    outreach.send(store, draft.pk)

    assert store.get_draft(draft.pk).status == "draft"
    assert store.release_stale_sending()[0] == 0  # оно уже в очереди, а не занято


def test_unknown_smtp_error_is_counted_in_our_favour():
    """Незнакомая ошибка протокола — не улика против адреса."""
    import smtplib

    assert outreach._our_side(smtplib.SMTPException("что-то новое"))
    assert not outreach._our_side(
        smtplib.SMTPRecipientsRefused({"a@b.ru": (550, b"no such user")})
    )


# --- След в ящике и обязательные заголовки -------------------------------------


def _compose_via_smtp(monkeypatch):
    """Собирает письмо тем же кодом, что и боевая отправка, но никуда не шлёт.

    Читаем его обратно из байтов, ушедших в сокет, а не из объекта: заголовок,
    потерянный при сборке провода, из объекта всё равно был бы виден, а до
    получателя не дошёл бы.
    """
    import smtplib
    from email import message_from_bytes

    server = _FakeSMTP()
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: server)
    monkeypatch.setattr(outreach, "_keep_a_copy", lambda message: None)
    outreach.deliver_email("client@company.ru", "Тема", "Текст письма")
    assert server.body.endswith(b"\r\n.\r\n"), "письмо должно кончаться точкой на своей строке"
    return message_from_bytes(server.body[: -len(b"\r\n.\r\n")])


def test_letter_has_date_and_message_id(smtp_ready, monkeypatch):
    """Письмо без Date и Message-ID спам-фильтры считают самоделкой.

    По RFC 5322 оба заголовка обязательны, а у нас их не было вовсе — лишний
    минус ровно там, где холодной рассылке он не нужен.
    """
    message = _compose_via_smtp(monkeypatch)
    assert message["Date"]
    assert message["Message-ID"]
    assert "@" in message["Message-ID"]


def test_date_is_a_real_rfc_date(smtp_ready, monkeypatch):
    """Дата должна разбираться почтовым клиентом, а не быть просто строкой."""
    from email.utils import parsedate_to_datetime

    message = _compose_via_smtp(monkeypatch)
    when = parsedate_to_datetime(message["Date"])
    assert when.tzinfo is not None      # с часовым поясом, иначе разъедется


def test_sent_letter_is_copied_to_the_mailbox(smtp_ready, monkeypatch):
    """Прямая отправка через SMTP копию в «Отправленные» не кладёт.

    Из-за этого владелец открывал ящик и не находил там ни одного письма от
    бота: письма уходили по-настоящему, а следа не оставалось.
    """
    import smtplib

    copies = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: _FakeSMTP())
    monkeypatch.setattr(outreach, "_keep_a_copy", lambda message: copies.append(message))
    outreach.deliver_email("client@company.ru", "Тема", "Текст")
    assert len(copies) == 1


def test_failed_copy_does_not_break_the_send(smtp_ready, monkeypatch):
    """Письмо уже ушло — неудачная копия этого не отменяет."""
    import imaplib
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: _FakeSMTP())

    def no_mailbox(*a, **kw):
        raise OSError("ящик недоступен")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", no_mailbox)
    # Настоящий _keep_a_copy обязан проглотить это молча: письмо уже ушло,
    # и неудачная копия отменить отправку не может.
    outreach.deliver_email("client@company.ru", "Тема", "Текст")


# Русское имя папки приходит от сервера байтами — держим отдельно, чтобы
# строка теста не разъезжалась.
_RU_SENT = '(\\HasNoChildren) "." "Отправленные"'.encode()


@pytest.mark.parametrize(
    ("folders", "expected"),
    [
        ([b'(\\HasNoChildren \\Sent) "." INBOX.Sent'], "INBOX.Sent"),
        ([b'(\\HasNoChildren) "." Sent'], "Sent"),
        ([_RU_SENT], "Отправленные"),
        ([b'(\\HasNoChildren) "." INBOX'], None),
    ],
)
def test_sent_folder_is_found(folders, expected):
    """Папка зовётся по-разному у разных служб; у Beget это INBOX.Sent."""

    class Box:
        def list(self):
            return "OK", folders

    assert outreach._sent_folder(Box()) == expected


# --- Находки ревизии 23.08 --------------------------------------------------


def test_cyrillic_domain_is_punycoded_for_smtp():
    """«info@компания.рф» — легальный адрес, но SMTP без SMTPUTF8 его не примет.

    Раньше такой адрес зацикливал отправку навсегда: отказ сервера считался
    «нашей стороной», попытки не росли, и письмо каждые пару минут заново
    логинилось в SMTP — ровно тот перебор, за который хостер блокирует ящик.
    """
    assert outreach._ascii_address("info@компания.рф") == "info@xn--80aqeigdi5k.xn--p1ai"
    assert outreach._ascii_address("hello@mediasfera.ru") == "hello@mediasfera.ru"


def test_cyrillic_local_part_is_a_recipient_refusal():
    """Кириллица до собаки не лечится punycode — это честный отказ адресата."""
    import smtplib

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        outreach._ascii_address("иван@mail.ru")


def test_warmup_is_shared_between_tabs(store, smtp_ready):
    """Прогрев ограничивает ящик, а не вкладку: 3 письма в день — на двоих.

    Раньше каждая вкладка получала полный потолок, и с холодного ящика в
    первый день уходило 6 писем вместо 3 — вся кривая прогрева удваивалась.
    """
    store.set_state(outreach.WARMUP_KEY, __import__("orbit.store", fromlist=["today_msk"]).today_msk())
    day_cap = outreach.WARMUP_STEPS[0]

    total = 0
    for _ in range(day_cap * 2):
        for kind in (Kind.PARTNER, Kind.CLIENT):
            if outreach.letters_left_today(store, kind) > 0:
                store.add_send(kind)
                total += 1
    assert total == day_cap, f"с ящика ушло {total} писем при потолке {day_cap}"


def test_followup_pause_counts_from_the_real_send(store, agency):
    """Черновик неделю пролежал в очереди — следующее касание не наезжает.

    Пауза мерилась от подготовки письма: отправил владелец касание кнопкой
    через неделю — и следующее готовилось сразу, человек получал два письма
    подряд.
    """
    from orbit import followup
    from orbit.store import now_msk

    pk = store.upsert_contact(agency)
    contact = store.get_contact(pk)
    # Метка подготовки недельной давности, а настоящая отправка — только что.
    contact.meta[followup.TOUCH_AT] = "2026-08-10T12:00:00"
    store.set_meta(pk, contact.meta)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Т", body="Текст."
    ))
    store.mark_sent(draft_pk)
    store.conn.execute("UPDATE drafts SET sent_at = ? WHERE pk = ?", (now_msk(), draft_pk))
    store.conn.commit()

    waited = followup._waited_days(store, store.get_contact(pk))
    assert waited is not None and waited < 1, "отсчёт обязан идти от настоящей отправки"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'(\\HasNoChildren \\Sent) "." "Sent Items"', "Sent Items"),
        (b'(\\HasNoChildren) "." INBOX.Sent', "INBOX.Sent"),
    ],
)
def test_sent_folder_survives_spaces_in_names(line, expected):
    """«Sent Items» из кавычек не должен превращаться в мусорное «Items"»."""
    class Box:
        def list(self):
            return "OK", [line]

    assert outreach._sent_folder(Box()) == expected


# --- Прощание с почтовым сервером -------------------------------------------

class _FakeSMTP:
    """Почтовый сервер, который ломается там, где ему велено.

    Разговор теперь ведётся по шагам (MAIL → RCPT → DATA → текст), поэтому и
    двойник отвечает по шагам: только так видно, оборвалось ДО текста письма
    или ПОСЛЕ — а от этого зависит, отправим мы письмо второй раз или нет.
    """

    def __init__(self, *, fail_on: str = "", data_code: int = 354, final_code: int = 250) -> None:
        self.fail_on = fail_on
        self.data_code = data_code
        self.final_code = final_code
        self.accepted: list[str] = []
        self.steps: list[str] = []
        self.body = b""
        self.closed = False
        self._to = ""

    def _maybe_fail(self, step: str) -> None:
        self.steps.append(step)
        if self.fail_on == step:
            import smtplib

            raise smtplib.SMTPServerDisconnected(f"связь оборвалась на {step}")

    def login(self, user, password):
        self._maybe_fail("login")

    def ehlo_or_helo_if_needed(self):
        self._maybe_fail("ehlo")

    def mail(self, address, options=()):
        self._maybe_fail("mail")
        return 250, b"ok"

    def rcpt(self, address, options=()):
        self._maybe_fail("rcpt")
        self._to = address
        return 250, b"ok"

    def docmd(self, command, arg=""):
        self._maybe_fail("data")
        return self.data_code, b"go ahead"

    def send(self, payload):
        self._maybe_fail("payload")
        self.body = payload

    def getreply(self):
        self._maybe_fail("reply")
        if self.final_code == 250:
            self.accepted.append(self._to)
        return self.final_code, b"queued"

    def rset(self):
        self.steps.append("rset")

    def quit(self):
        # Настоящий smtplib закрывает сокет внутри quit(), поэтому и здесь.
        self._maybe_fail("quit")
        self.closed = True

    def close(self):
        self.closed = True


def test_broken_goodbye_does_not_undo_delivery():
    """Разрыв связи ПОСЛЕ приёма письма не должен выглядеть как несостоявшаяся отправка.

    Иначе вызывающий вернёт письмо в очередь, и получатель увидит его дважды —
    ровно то, ради чего отметка «отдано на почту» ставится заранее.
    """
    server = _FakeSMTP(fail_on="quit")
    outreach._close_quietly(server)
    assert server.closed, "сокет надо закрыть даже без вежливого прощания"


def test_failure_before_acceptance_is_reported(monkeypatch, smtp_ready):
    """Не вошли на сервер — письмо не ушло, и молчать об этом нельзя."""
    import smtplib
    from email.message import EmailMessage

    config.get_settings.cache_clear()
    server = _FakeSMTP(fail_on="login")
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: server)

    message = EmailMessage()
    message["To"] = "hello@mediasfera.ru"
    with pytest.raises(smtplib.SMTPServerDisconnected):
        outreach._hand_over(message)
    assert server.accepted == []
    assert server.closed, "сокет надо закрыть и после неудачи"


def test_delivery_survives_a_rude_disconnect(monkeypatch, smtp_ready):
    """Письмо принято, а сервер бросил трубку — это успешная отправка."""
    import smtplib
    from email.message import EmailMessage

    config.get_settings.cache_clear()
    server = _FakeSMTP(fail_on="quit")
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: server)

    message = EmailMessage()
    message["To"] = "hello@mediasfera.ru"
    outreach._hand_over(message)  # молча: доставка состоялась
    assert server.accepted == ["hello@mediasfera.ru"]


def test_telegram_by_hand_does_not_start_the_mailbox_warmup(store, agency, monkeypatch):
    """Прогрев считает возраст ЯЩИКА, а телеграм уходит с личного аккаунта.

    Раньше нажатие «Отправил» в телеграме запускало часы прогрева. Ящик
    «взрослел» на бумаге, суточный лимит рос, и первое настоящее письмо
    уходило с потолком, заслуженным чужим каналом, — ровно то, ради чего
    прогрев и придуман.
    """
    from orbit.models import Channel, Draft

    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Здравствуйте, по делу.",
    ))

    result = outreach.mark_sent_manually(store, draft_pk)

    assert result.ok
    assert not store.get_state(outreach.WARMUP_KEY), "часы прогрева ящика запускать нельзя"


def test_a_real_letter_does_start_the_warmup(store, agency, monkeypatch, smtp_ready):
    from orbit.models import Channel, Draft

    config.get_settings.cache_clear()
    monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: None)
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте, по делу.",
    ))

    assert outreach.send(store, draft_pk).ok
    assert store.get_state(outreach.WARMUP_KEY), "первое письмо запускает прогрев"


def test_manual_send_warning_reaches_the_owner(store, agency):
    """«Записал, но имейте в виду…» обязано дойти до владельца.

    Кнопка описывает свершившийся факт и записывает его всегда — но вместе с
    предупреждением: суточный лимит превышен, контакт просил не писать.
    Раньше при успехе подставлялась своя бодрая строка, и предупреждение
    исчезало: владелец узнавал о выходе за рамки ниоткуда.
    """
    from orbit.models import Channel, Draft
    from orbit.sections import base, partners

    agency.emails = []
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Привет.",
    ))
    for _ in range(outreach.tab_limit(store, Kind.PARTNER) + 1):
        store.add_send(Kind.PARTNER)

    screen = base.mark_manual(partners.TAB, store, str(draft_pk))

    assert "имейте в виду" in (screen.alert or ""), screen.alert
    assert "лимит" in (screen.alert or "")


def test_dropped_letter_is_reported_to_the_owner(store, prepared, smtp_ready, monkeypatch):
    """Письмо, снятое после трёх отказов, не должно исчезать молча.

    Его писала модель, оно стоило денег и висело в очереди. В автоматическом
    режиме отправкой занимается фоновый поток, и результат его работы не видит
    никто: единственным следом оставалась строка в логе.
    """
    from orbit import notify

    said: list[str] = []
    monkeypatch.setattr(notify, "tell", lambda text, **kw: said.append(text) or True)

    def refuse(*_args):
        import smtplib

        raise smtplib.SMTPRecipientsRefused({"hello@mediasfera.ru": (550, b"User unknown")})

    monkeypatch.setattr(outreach, "deliver_email", refuse)
    draft, contact = prepared

    for _ in range(outreach.MAX_ATTEMPTS):
        store.approve_draft(draft.pk)
        outreach.send(store, draft.pk)

    # Дёргать этим владельца в ту же секунду незачем — письмо всё равно снято,
    # и сделать с этим прямо сейчас нечего. Но и пропасть оно не должно:
    # строка ждёт в журнале дня и выйдет в вечерней сводке.
    assert not said, "по снятому письму владельца не дёргаем"
    notes = store.quiet_notes()
    assert notes, "событие обязано дойти до вечерней сводки"
    assert "снял с отправки" in notes[-1]
    assert contact.name in notes[-1]
    assert any(row["kind"] == "bounce" for row in store.events(contact.pk))


def test_interrupted_letters_are_reported_to_the_owner(store, agency, monkeypatch):
    """Судьбу прерванного письма обещали отдать владельцу — надо ему сказать.

    Письмо, которое успели отдать почтовому серверу перед падением процесса,
    в очередь не возвращается: повтор холодного письма хуже, чем его
    отсутствие. Но узнать об этом владелец мог только из лога, которого не
    читает, — письмо просто исчезало из очереди.
    """
    from orbit import notify
    from orbit.models import Channel, Draft

    said: list[str] = []
    monkeypatch.setattr(notify, "tell", lambda text, **kw: said.append(text) or True)

    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте.",
    ))
    store.approve_draft(draft_pk)
    store.claim_draft(draft_pk)
    store.hand_to_smtp(draft_pk)

    outreach._report_interrupted(store)

    assert not said, "среди ночи будить этим незачем: решать всё равно утром"
    notes = store.quiet_notes()
    assert notes, "событие обязано дойти до вечерней сводки"
    assert "неизвестной судьбой" in notes[0]
    assert agency.name in notes[0]


def test_a_clean_restart_says_nothing(store, agency):
    """Ничего не прерывалось — беспокоить владельца незачем."""
    from orbit import notify

    said: list[str] = []
    notify_tell = notify.tell
    try:
        notify.tell = lambda text, **kw: said.append(text) or True
        outreach._report_interrupted(store)
    finally:
        notify.tell = notify_tell

    assert said == []


# --- Обрыв связи посреди отправки ---------------------------------------------


def test_break_before_the_text_is_a_plain_failure(monkeypatch, smtp_ready):
    """Оборвалось на RCPT — письмо точно не ушло, повторить можно и нужно."""
    import smtplib
    from email.message import EmailMessage

    config.get_settings.cache_clear()
    server = _FakeSMTP(fail_on="rcpt")
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: server)

    message = EmailMessage()
    message["From"] = "manager@uniqore.ru"
    message["To"] = "hello@mediasfera.ru"
    with pytest.raises(smtplib.SMTPServerDisconnected):
        outreach._hand_over(message)
    assert server.accepted == [], "до текста письма ничего не принято"
    assert "rset" not in server.steps or server.steps[-1] != "payload"


def test_break_after_the_text_is_not_a_failure(monkeypatch, smtp_ready):
    """Оборвалось после текста письма — исход неизвестен, а не «не ушло».

    Сервер к этому моменту письмо мог уже принять. Раньше здесь летел обычный
    SMTPServerDisconnected, вызывающий возвращал письмо в очередь, и на
    следующем круге получатель видел то же холодное письмо второй раз.
    """
    import smtplib
    from email.message import EmailMessage

    config.get_settings.cache_clear()
    server = _FakeSMTP(fail_on="reply")
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: server)

    message = EmailMessage()
    message["From"] = "manager@uniqore.ru"
    message["To"] = "hello@mediasfera.ru"
    with pytest.raises(outreach.MaybeDelivered):
        outreach._hand_over(message)
    assert server.closed, "сокет надо закрыть и в непонятном случае"


def test_unclear_letter_is_not_sent_again(store, agency, monkeypatch, smtp_ready):
    """Письмо с неизвестной судьбой не возвращается в очередь и не уходит второй раз."""
    from orbit import mailcheck

    config.get_settings.cache_clear()
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте, по делу."
    ))
    store.approve_draft(draft_pk)

    monkeypatch.setattr(
        mailcheck, "pick_address",
        lambda st, c: ("hello@mediasfera.ru", mailcheck.Verdict(True, "")),
    )

    def broken(*a, **kw):
        raise outreach.MaybeDelivered("связь оборвалась после отправки текста")

    monkeypatch.setattr(outreach, "deliver_email", broken)
    said: list[str] = []
    monkeypatch.setattr(outreach, "_tell_letter_unclear", lambda s, c, e: said.append(c.name))

    result = outreach.send(store, draft_pk)
    assert not result.ok
    assert said == ["Медиасфера"], "владельцу надо сказать сразу, а не при перезапуске"

    draft = store.get_draft(draft_pk)
    assert draft.status == "failed", "в очереди такому письму не место"
    assert draft.error == store.UNCLEAR_ERROR
    assert store.unclear_drafts() == 1, "цифра должна дойти до блока «Требует вас»"
    assert store.draft_for(pk) is None, "второго захода по этому контакту быть не должно"


def test_unclear_letter_card_survives_an_ampersand_in_the_name(store, monkeypatch):
    """Имя компании со «&» не должно ломать разметку карточки владельцу."""
    from orbit import alerts

    said: list[str] = []
    import orbit.notify as notify

    monkeypatch.setattr(notify, "tell", lambda text, **kw: said.append(text) or True)
    # Обычно это сообщение ждёт вечера. Тумблером возвращаем его в телеграм —
    # заодно проверяем, что переключатель работает.
    alerts.set_instant(store, alerts.LETTER_UNCLEAR, True)
    contact = Contact(kind=Kind.CLIENT, key="ivanov.ru", name="Иванов & Ко <главный>")
    outreach._tell_letter_unclear(store, contact, RuntimeError("сокет <закрыт>"))
    assert said, "сообщение должно уйти"
    assert "&amp;" in said[0] and "&lt;главный&gt;" in said[0]
    assert "<главный>" not in said[0]


def test_dropped_letter_card_escapes_the_name_too(store, monkeypatch):
    """Та же карточка про снятое письмо — с теми же скобками в названии."""
    from orbit import alerts

    said: list[str] = []
    import orbit.notify as notify

    monkeypatch.setattr(notify, "tell", lambda text, **kw: said.append(text) or True)
    alerts.set_instant(store, alerts.LETTER_DROPPED, True)
    contact = Contact(kind=Kind.CLIENT, key="ivanov.ru", name="Иванов & Ко")
    outreach._tell_letter_dropped(store, contact, 3, RuntimeError("550 <no such user>"))
    assert said and "&amp;" in said[0] and "&lt;no such user&gt;" in said[0]


# --- Прогрев ограничивает ЯЩИК, а не владельца --------------------------------


def test_manual_telegram_does_not_eat_the_mailbox_warmup(store, agency, monkeypatch, smtp_ready):
    """Телеграм владелец шлёт руками со своего аккаунта — ящик при этом молчит.

    Прогревная квота меряет нагрузку на ЯЩИК. Ручные телеграм-отправки её
    съедали наравне с письмами: три сообщения в первый день закрывали ящику
    всю норму, писем не уходило ни одного, а отсчёт дней прогрева начинается
    от первого УШЕДШЕГО письма — то есть не начинался никогда.
    """
    config.get_settings.cache_clear()
    monkeypatch.setenv("DAILY_LIMIT_PARTNERS", "50")
    monkeypatch.setenv("DAILY_LIMIT_CLIENTS", "50")
    config.get_settings.cache_clear()

    assert outreach.warmup_limit(store, Kind.PARTNER) == outreach.WARMUP_STEPS[0]

    # Владелец отправил три сообщения в телеграм руками — по клиентам.
    for _ in range(3):
        store.add_send(Kind.CLIENT, by_mail=False)

    assert store.mailed_today() == 0, "из ящика не ушло ничего"
    assert outreach.letters_left_today(store, Kind.PARTNER) == outreach.WARMUP_STEPS[0], (
        "прогревная норма ящика телеграмом не тратится"
    )

    # А вот письмо — тратит, и именно у соседней вкладки.
    store.add_send(Kind.CLIENT, by_mail=True)
    assert store.mailed_today() == 1
    assert outreach.letters_left_today(store, Kind.PARTNER) == outreach.WARMUP_STEPS[0] - 1


def test_mailed_today_counts_by_tab(store):
    """Письма считаются и по вкладке отдельно, и по ящику в сумме."""
    store.add_send(Kind.CLIENT, by_mail=True)
    store.add_send(Kind.CLIENT, by_mail=False)
    store.add_send(Kind.PARTNER, by_mail=True)

    assert store.sent_today(Kind.CLIENT) == 2, "касаний по клиентам два"
    assert store.mailed_today(Kind.CLIENT) == 1, "а письмо из ящика одно"
    assert store.mailed_today() == 2


def test_a_lone_dot_in_the_letter_does_not_cut_it_short():
    """Строка из одной точки в письме обязана дойти целиком.

    По SMTP точка на своей строке означает «письмо кончилось». Модель вполне
    может написать абзац, который переносом окажется строкой «.»: без
    удвоения сервер обрубил бы письмо ровно там, и клиент получил бы половину.
    """
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "manager@uniqore.ru"
    message["To"] = "hello@mediasfera.ru"
    message["Subject"] = "Тема"
    message.set_content("Первый абзац.\n.\nВторой абзац.")

    wire = outreach._wire_bytes(message)
    assert wire.endswith(b"\r\n.\r\n"), "письмо кончается точкой на своей строке"
    body = wire[: -len(b"\r\n.\r\n")]
    assert b"\r\n..\r\n" in body, "точка внутри письма обязана удвоиться"
    assert b"\r\n.\r\n" not in body, "иначе сервер обрубит письмо здесь"


def test_the_letter_goes_out_with_crlf_line_endings():
    """Переводы строк на проводе — CRLF, как требует SMTP."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "manager@uniqore.ru"
    message["To"] = "hello@mediasfera.ru"
    message["Subject"] = "Тема"
    message.set_content("Строка раз\nСтрока два")

    wire = outreach._wire_bytes(message)
    assert b"\n" in wire
    assert wire.replace(b"\r\n", b"").count(b"\n") == 0, "голых переводов строк быть не должно"


# --- Временный отказ адресата — не отказ --------------------------------------


@pytest.mark.parametrize(
    ("name", "exc"),
    [
        ("грейлистинг на RCPT", smtplib.SMTPRecipientsRefused(
            {"hello@mediasfera.ru": (450, b"4.2.0 Greylisted, try again later")})),
        ("временный отказ на DATA", smtplib.SMTPDataError(451, b"4.3.0 try later")),
        ("сервис недоступен", smtplib.SMTPDataError(421, b"4.7.0 too many connections")),
    ],
)
def test_a_temporary_refusal_is_not_the_addresss_fault(name, exc):
    """4xx — «зайди позже», а не «такого ящика нет».

    Грейлистинг отклоняет ПЕРВОЕ письмо с незнакомого адреса и ждёт повтора:
    его получают все нормальные отправители. Три круга отправщика укладываются
    в те самые несколько минут — письмо снималось навсегда, а владельцу
    сообщалось, что адрес не принимает почту третий раз подряд.
    """
    assert outreach.wait_and_retry(exc), name


@pytest.mark.parametrize(
    ("name", "exc"),
    [
        ("нет такого ящика", smtplib.SMTPRecipientsRefused(
            {"hello@mediasfera.ru": (550, b"5.1.1 no such user")})),
        ("письмо отвергнуто", smtplib.SMTPDataError(554, b"5.7.1 rejected as spam")),
        ("оборвалась связь", smtplib.SMTPServerDisconnected("сокет закрыт")),
    ],
)
def test_a_real_refusal_still_counts(name, exc):
    """А настоящий отказ по-прежнему считается: тут ждать нечего."""
    assert not outreach.wait_and_retry(exc), name


def test_greylisting_does_not_burn_the_attempt_counter(store, agency, monkeypatch, smtp_ready):
    """Три грейлистинга подряд не должны снять письмо с отправки."""
    from orbit import mailcheck

    config.get_settings.cache_clear()
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте, по делу."
    ))
    monkeypatch.setattr(
        mailcheck, "pick_address",
        lambda st, c: ("hello@mediasfera.ru", mailcheck.Verdict(True, "")),
    )

    def greylisted(*a, **kw):
        raise smtplib.SMTPRecipientsRefused(
            {"hello@mediasfera.ru": (450, b"4.2.0 Greylisted, try again later")}
        )

    monkeypatch.setattr(outreach, "deliver_email", greylisted)
    dropped: list[str] = []
    monkeypatch.setattr(outreach, "_tell_letter_dropped", lambda s, c, a, e: dropped.append(c.name))

    for _ in range(outreach.MAX_ATTEMPTS + 2):
        store.approve_draft(draft_pk)
        result = outreach.send(store, draft_pk)
        assert not result.ok
        assert "подождать" in result.reason, result.reason

    draft = store.get_draft(draft_pk)
    assert draft.status == "draft", "письмо обязано остаться в очереди"
    assert draft.attempts == 0, "временный отказ попыткой адресату не считается"
    assert dropped == [], "снимать письмо и жаловаться владельцу тут не за что"


def test_manual_telegram_never_blocks_the_mailbox(store, prepared, smtp_ready):
    """Ручной телеграм тратит норму касаний, но не прогревную норму ящика.

    Три сообщения в телеграм в первый день закрывали ящику всю норму: писем
    не уходило ни одного, а отсчёт дней прогрева начинается от первого
    УШЕДШЕГО письма — то есть не начинался никогда.
    """
    draft, contact = prepared
    warmup = outreach.warmup_limit(store, Kind.PARTNER)

    # Владелец отправил столько же сообщений руками, сколько ящику разрешено
    # писем. По касаниям это ещё не потолок вкладки.
    for _ in range(warmup):
        store.add_send(Kind.PARTNER, by_mail=False)

    verdict = outreach.can_send(store, draft, contact)
    assert verdict.ok, verdict.reason
    assert store.mailed_today() == 0


def test_the_queue_approves_what_the_send_check_allows(store, agency, smtp_ready):
    """Очередь и проверка у двери обязаны считать одинаково.

    Считались порознь: `can_send` пропускал письмо, а очередь брала «потолок
    минус касания» — прогревный потолок ЯЩИКА против касаний ВСЕМИ каналами.
    Три сообщения в телеграм руками — и фоновая очередь молча одобряла ноль
    писем, хотя отправка была не против. Владельцу при этом сообщалось
    «Суточный лимит исчерпан».
    """
    config.get_settings.cache_clear()
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте, по делу."
    ))

    # Владелец отправил три сообщения в телеграм руками ПО ЭТОЙ ЖЕ вкладке.
    for _ in range(outreach.WARMUP_STEPS[0]):
        store.add_send(Kind.PARTNER, by_mail=False)

    verdict = outreach.can_send(store, store.get_draft(draft_pk), store.get_contact(pk))
    assert verdict.ok, verdict.reason
    assert outreach.approve_queue(store, Kind.PARTNER) == 1, (
        "очередь обязана одобрить то, что отправка пропускает"
    )
    assert outreach.letters_left_today(store, Kind.PARTNER) > 0


def test_letters_left_today_respects_both_norms(store, smtp_ready):
    """Остаток — минимум из двух норм: касаний вкладки и прогрева ящика."""
    config.get_settings.cache_clear()
    warmup = outreach.warmup_limit(store, Kind.PARTNER)
    assert outreach.letters_left_today(store, Kind.PARTNER) == warmup

    # Письма из ящика тратят прогрев (он общий на обе вкладки).
    store.add_send(Kind.CLIENT, by_mail=True)
    assert outreach.letters_left_today(store, Kind.PARTNER) == warmup - 1

    # А касания вкладки тратят её норму.
    for _ in range(outreach.tab_limit(store, Kind.PARTNER)):
        store.add_send(Kind.PARTNER, by_mail=False)
    assert outreach.letters_left_today(store, Kind.PARTNER) == 0


# --- Письмо, снятое приговором адресу -----------------------------------------


def _rejecting_address(monkeypatch, *, permanent: bool):
    from orbit import mailcheck

    monkeypatch.setattr(
        mailcheck, "pick_address",
        lambda st, c: (None, mailcheck.Verdict(False, "домена не существует", permanent)),
    )


def test_a_dead_address_does_not_strand_the_contact(store, monkeypatch, smtp_ready):
    """Письмо снято — контакт обязан вернуться в «новые», а не зависнуть.

    Компания оставалась в стадии «Готовы к отправке» без единого письма: в
    очереди её нет (письма-то нет), а пачка берёт только «новых» и проходит
    мимо. Выпадение молчаливое, заметить его нечем.
    """
    config.get_settings.cache_clear()
    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a.ru", name="Улыбка",
        emails=["info@a.ru"], telegram=["https://t.me/ulybka"],
    ))
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"
    ))
    store.set_stage(pk, Stage.QUEUED, "текст готов")
    store.approve_draft(draft_pk)
    _rejecting_address(monkeypatch, permanent=True)

    said: list[str] = []
    monkeypatch.setattr(outreach, "_tell_address_rejected", lambda s, c, r: said.append(c.name))

    result = outreach.send(store, draft_pk)
    assert not result.ok

    contact = store.get_contact(pk)
    assert store.draft_for(pk) is None, "письмо ушло из очереди"
    assert contact.stage is Stage.NEW, "иначе пачка больше никогда его не возьмёт"
    assert said == ["Улыбка"], "о снятом письме владельцу надо сказать"


def test_a_dead_address_without_telegram_closes_the_contact(store, monkeypatch, smtp_ready):
    """Достучаться нечем — контакт закрывается, и это не «новый»."""
    config.get_settings.cache_clear()
    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="b.ru", name="Улыбка", emails=["info@b.ru"]
    ))
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"
    ))
    store.set_stage(pk, Stage.QUEUED, "текст готов")
    _rejecting_address(monkeypatch, permanent=True)
    monkeypatch.setattr(outreach, "_tell_address_rejected", lambda s, c, r: None)

    outreach.send(store, draft_pk)
    assert store.get_contact(pk).stage is Stage.DEAD


def test_a_letter_dropped_after_three_failures_also_frees_the_contact(store, agency, monkeypatch,
                                                                      smtp_ready):
    """То же самое для письма, снятого после трёх отказов подряд."""
    from orbit import mailcheck

    config.get_settings.cache_clear()
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"
    ))
    store.set_stage(pk, Stage.QUEUED, "текст готов")
    monkeypatch.setattr(
        mailcheck, "pick_address",
        lambda st, c: ("hello@mediasfera.ru", mailcheck.Verdict(True, "")),
    )

    def refused(*a, **kw):
        raise smtplib.SMTPRecipientsRefused({"hello@mediasfera.ru": (550, b"5.1.1 no such user")})

    monkeypatch.setattr(outreach, "deliver_email", refused)
    monkeypatch.setattr(outreach, "_tell_letter_dropped", lambda s, c, a, e: None)

    for _ in range(outreach.MAX_ATTEMPTS):
        store.approve_draft(draft_pk)
        outreach.send(store, draft_pk)

    assert store.draft_for(pk) is None
    assert store.get_contact(pk).stage is Stage.NEW, "контакт не должен остаться без письма в очереди"


def test_three_refusals_blacklist_the_address_not_just_the_letter(store, agency, monkeypatch,
                                                                  smtp_ready):
    """Адрес, отказавший трижды, не должен получить новое письмо.

    Счётчик попыток живёт на ЧЕРНОВИКЕ, а не на адресе. Снятое письмо
    возвращает контакт в «новые» — и без записи о негодном адресе пачка
    написала бы новый платный текст на тот же мёртвый ящик. Круг: текст —
    три отказа — снова текст.
    """
    from orbit import mailcheck

    config.get_settings.cache_clear()
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"
    ))
    monkeypatch.setattr(
        mailcheck, "pick_address",
        lambda st, c: ("hello@mediasfera.ru", mailcheck.Verdict(True, "")),
    )

    def refused(*a, **kw):
        raise smtplib.SMTPRecipientsRefused({"hello@mediasfera.ru": (550, b"5.1.1 no such user")})

    monkeypatch.setattr(outreach, "deliver_email", refused)
    monkeypatch.setattr(outreach, "_tell_letter_dropped", lambda s, c, a, e: None)

    for _ in range(outreach.MAX_ATTEMPTS):
        store.approve_draft(draft_pk)
        outreach.send(store, draft_pk)

    row = store.email_check("hello@mediasfera.ru")
    assert row is not None, "адрес обязан попасть в чёрный список"
    assert not row["ok"] and row["permanent"], dict(row)
    assert store.get_contact(pk).stage is Stage.NEW


def test_a_spam_filter_rejection_does_not_blacklist_a_live_address(store, agency, monkeypatch,
                                                                   smtp_ready):
    """Отказ после текста письма — приговор ПИСЬМУ, а не ящику.

    Спам-фильтр не принял формулировку, ссылку или домен отправителя. Ящик
    там живой, и вычеркнуть его значит потерять компанию за то, в чём она не
    виновата.
    """
    from orbit import mailcheck

    config.get_settings.cache_clear()
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"
    ))
    monkeypatch.setattr(
        mailcheck, "pick_address",
        lambda st, c: ("hello@mediasfera.ru", mailcheck.Verdict(True, "")),
    )

    def rejected_content(*a, **kw):
        raise smtplib.SMTPDataError(554, b"5.7.1 message rejected as spam")

    monkeypatch.setattr(outreach, "deliver_email", rejected_content)
    monkeypatch.setattr(outreach, "_tell_letter_dropped", lambda s, c, a, e: None)

    for _ in range(outreach.MAX_ATTEMPTS):
        store.approve_draft(draft_pk)
        outreach.send(store, draft_pk)

    assert store.email_check("hello@mediasfera.ru") is None, (
        "адрес живой — вычёркивать его не за что"
    )
    assert store.draft_for(pk) is None, "а письмо всё равно снимаем"


def test_only_a_recipient_refusal_counts_as_a_dead_address():
    """Различаем отказ адресу и отказ письму по типу исключения."""
    assert outreach._address_was_refused(
        smtplib.SMTPRecipientsRefused({"a@b.ru": (550, b"5.1.1 no such user")})
    )
    assert not outreach._address_was_refused(smtplib.SMTPDataError(554, b"5.7.1 spam"))
    assert not outreach._address_was_refused(smtplib.SMTPServerDisconnected("оборвалось"))


# --- Как люди на самом деле просят перестать писать ---------------------------
#
# Пропуск здесь — письмо тому, кто попросил отстать: жалоба на спам, которую
# не отозвать. Ложное срабатывание — закрытый навсегда живой контакт. Оба
# списка проверяем вместе, поодиночке они врут.

STOP_ANSWERS = [
    "Стоп",
    "стоп, больше не пишите",
    "Не пишите нам больше",
    "отписка",
    "unsubscribe",
    "Прошу больше не писать на этот адрес",
    "Уберите нас из рассылки",
    "Отпишите меня, пожалуйста",
    "Пожалуйста, не пишите нам",
    "Здравствуйте! Просьба больше не присылать письма.",
    "Здравствуйте!\n\nСпасибо за предложение.\n\nПрошу больше не писать.",
    "Исключите нас из рассылки",
]

LIVE_ANSWERS = [
    "Здравствуйте, расскажите подробнее",
    "Стоимость какая?",
    "Не могли бы вы прислать презентацию",
    "Не сейчас, вернёмся в сентябре",
    "Стопроцентно интересно, давайте обсудим",
    "Пишите на почту директора",
    "Можете писать мне напрямую",
]


@pytest.mark.parametrize("text", STOP_ANSWERS)
def test_a_refusal_is_understood(text):
    assert outreach.looks_like_stop(text), f"отказ не распознан: {text!r}"


@pytest.mark.parametrize("text", LIVE_ANSWERS)
def test_a_live_answer_is_not_a_refusal(text):
    assert not outreach.looks_like_stop(text), f"живой ответ принят за отказ: {text!r}"


def test_stale_text_is_not_sent(store):
    """Письмо, написанное месяц назад, не уходит: текст написан про «сейчас».

    Касание вдогонку прямо говорит «писал вам недавно». Пролежав в очереди
    сорок шесть дней — так было, когда всё стояло из-за мёртвого прокси, — оно
    уходит враньём.
    """
    from datetime import datetime, timedelta

    from orbit import followup
    from orbit.store import MSK

    contact = Contact(
        kind=Kind.PARTNER, key="old.ru", name="Старое агентство", site="old.ru",
        emails=["hi@old.ru"], stage=Stage.NEW,
    )
    contact.pk = store.upsert_contact(contact)
    draft_pk = store.save_draft(Draft(
        contact_pk=contact.pk, channel=Channel.EMAIL, subject="Тема", body="Текст",
    ))
    when = (datetime.now(MSK) - timedelta(days=followup.STALE_DAYS + 1)).isoformat(
        timespec="seconds"
    )
    store.conn.execute("UPDATE drafts SET created_at = ? WHERE pk = ?", (when, draft_pk))
    store.conn.commit()

    verdict = outreach.can_send(store, store.get_draft(draft_pk), store.get_contact(contact.pk))
    assert not verdict.ok
    assert "перепишем" in verdict.reason


def test_fresh_text_passes_the_age_check(store):
    """А только что написанное письмо проверка возраста пропускает."""
    contact = Contact(
        kind=Kind.PARTNER, key="fresh.ru", name="Свежее агентство", site="fresh.ru",
        emails=["hi@fresh.ru"], stage=Stage.NEW,
    )
    contact.pk = store.upsert_contact(contact)
    draft_pk = store.save_draft(Draft(
        contact_pk=contact.pk, channel=Channel.EMAIL, subject="Тема", body="Текст",
    ))
    verdict = outreach.can_send(store, store.get_draft(draft_pk), store.get_contact(contact.pk))
    # Проверку возраста и только её: настройки почты в тестах пустые, и дальше
    # письмо всё равно упрётся в них — но не в свежесть текста.
    assert "перепишем" not in verdict.reason

"""Проверка адреса до отправки.

Главное, что проверяем здесь, — разница между «писать нельзя никогда» и
«сейчас не смогли проверить». Первое закрывает контакт, второе только
откладывает письмо. Спутать их значит либо вычистить половину базы из-за
одной упавшей сети, либо разослать письма на мёртвые домены.
"""
from __future__ import annotations

import pytest

from orbit import mailcheck, outreach
from orbit.mailcheck import DomainState
from orbit.models import Channel, Contact, Draft, Kind, Stage

# Общая обвязка тестов подменяет domain_state, чтобы никто случайно не пошёл
# в сеть. Тестам про повторы нужна настоящая функция — держим её отдельно.
domain_state = mailcheck.domain_state


@pytest.fixture()
def dns(monkeypatch):
    """Управляемый DNS: тест сам говорит, что ответил резолвер."""

    def answer(state: DomainState, detail: str = "тест"):
        monkeypatch.setattr(mailcheck, "domain_state", lambda domain: (state, detail))

    return answer


# --- Проверки без сети --------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["", "  ", "просто текст", "без-собаки.ru", "два@@собачки.ru", "нет@домена"],
)
def test_broken_address_is_rejected_forever(store, address):
    verdict = mailcheck.check(store, address)
    assert not verdict.ok
    assert verdict.permanent


@pytest.mark.parametrize(
    "address",
    [
        "postmaster@company.ru",
        "abuse@company.ru",
        "noreply@company.ru",
        "no-reply@company.ru",
        "mailer-daemon@company.ru",
        "spamtrap@company.ru",
        "unsubscribe@company.ru",
    ],
)
def test_service_mailbox_is_never_written_to(store, address):
    """Служебные ящики читают машины, а у почтовых служб это спам-ловушки."""
    verdict = mailcheck.check(store, address)
    assert not verdict.ok
    assert verdict.permanent
    assert "служебный" in verdict.reason


def test_service_mailbox_with_suffix_is_caught(store):
    """«noreply.msk@» — тот же автоответчик, просто с уточнением."""
    assert not mailcheck.check(store, "noreply.msk@company.ru").ok


def test_disposable_mail_is_rejected(store):
    verdict = mailcheck.check(store, "info@mailinator.com")
    assert not verdict.ok
    assert verdict.permanent


def test_personal_mailbox_is_rejected(store):
    """Личный ящик — это человек, а не компания: холодное письмо туда нельзя."""
    verdict = mailcheck.check(store, "ivanov@gmail.com")
    assert not verdict.ok
    assert verdict.permanent


def test_normal_corporate_address_passes(store, dns):
    dns(DomainState.ACCEPTS)
    assert mailcheck.check(store, "info@company.ru").ok


def test_offline_checks_do_not_touch_dns(store, monkeypatch):
    """Кривой адрес незачем искать в DNS — резолвер даже не зовём."""

    def explode(domain):
        raise AssertionError("DNS спрашивать не должны")

    monkeypatch.setattr(mailcheck, "domain_state", explode)
    assert not mailcheck.check(store, "postmaster@company.ru").ok


# --- Что сказал DNS -----------------------------------------------------------


def test_missing_domain_closes_the_address(store, dns):
    dns(DomainState.DEAD, "домена не существует")
    verdict = mailcheck.check(store, "info@ghost-company.ru")
    assert not verdict.ok
    assert verdict.permanent


def test_dns_silence_is_not_a_verdict(store, dns):
    """Молчащий DNS — не приговор адресу: письмо ждёт, контакт живой."""
    dns(DomainState.UNKNOWN, "DNS не ответил вовремя")
    verdict = mailcheck.check(store, "info@company.ru")
    assert not verdict.ok
    assert not verdict.permanent
    assert verdict.retry_later


def test_dns_silence_is_not_remembered(store, dns):
    """«Не знаю» в кэш не кладём: иначе минута без сети стоит месяца молчания."""
    dns(DomainState.UNKNOWN)
    mailcheck.check(store, "info@company.ru")
    assert store.email_check("info@company.ru") is None

    dns(DomainState.ACCEPTS)
    assert mailcheck.check(store, "info@company.ru").ok


# --- Повторы ------------------------------------------------------------------


@pytest.fixture()
def no_pause(monkeypatch):
    """Паузу между попытками в тестах не выдерживаем."""
    monkeypatch.setattr(mailcheck, "DNS_RETRY_PAUSE", 0)


def test_silence_is_asked_again(monkeypatch, no_pause):
    """Молчание переспрашиваем: живой домен так и отвечал со второй попытки."""
    answers = iter([
        (DomainState.UNKNOWN, "DNS не ответил вовремя"),
        (DomainState.ACCEPTS, "есть MX"),
    ])
    monkeypatch.setattr(mailcheck, "_ask_dns", lambda domain: next(answers))
    assert domain_state("company.ru")[0] is DomainState.ACCEPTS


def test_living_domain_is_not_asked_twice(monkeypatch, no_pause):
    """«Принимает почту» — ответ окончательный, тут ошибиться нечем."""
    asked = []

    def once(domain):
        asked.append(domain)
        return DomainState.ACCEPTS, "есть MX"

    monkeypatch.setattr(mailcheck, "_ask_dns", once)
    assert domain_state("company.ru")[0] is DomainState.ACCEPTS
    assert len(asked) == 1


def test_dead_domain_needs_a_second_opinion(monkeypatch, no_pause):
    """Приговор домену закрывает контакт навсегда — одного ответа мало.

    Подменённый DNS (гостиничный вайфай, сбой провайдера) отдаёт NXDOMAIN
    по живому домену, и без подтверждения это стоило бы компании.
    """
    asked = []

    def twice(domain):
        asked.append(domain)
        return DomainState.DEAD, "домена не существует"

    monkeypatch.setattr(mailcheck, "_ask_dns", twice)
    assert domain_state("ghost.ru")[0] is DomainState.DEAD
    assert len(asked) == 2


def test_unconfirmed_death_is_only_unknown(monkeypatch, no_pause):
    """Первый ответ «нет», второй молчание — хоронить домен не за что."""
    answers = iter([
        (DomainState.DEAD, "домена не существует"),
        (DomainState.UNKNOWN, "DNS не ответил вовремя"),
        (DomainState.UNKNOWN, "DNS не ответил вовремя"),
    ])
    monkeypatch.setattr(mailcheck, "_ask_dns", lambda domain: next(answers))
    assert domain_state("company.ru")[0] is DomainState.UNKNOWN


def test_death_taken_back_by_a_second_answer(monkeypatch, no_pause):
    """Повтор сказал «принимает» — верим ему, а не первому «нет»."""
    answers = iter([
        (DomainState.DEAD, "домена не существует"),
        (DomainState.ACCEPTS, "есть MX"),
    ])
    monkeypatch.setattr(mailcheck, "_ask_dns", lambda domain: next(answers))
    assert domain_state("company.ru")[0] is DomainState.ACCEPTS


def test_silence_all_the_way_stays_unknown(monkeypatch, no_pause):
    monkeypatch.setattr(
        mailcheck, "_ask_dns", lambda domain: (DomainState.UNKNOWN, "DNS не ответил вовремя")
    )
    assert domain_state("company.ru")[0] is DomainState.UNKNOWN


# --- Кэш ----------------------------------------------------------------------


def test_result_is_cached(store, dns, monkeypatch):
    dns(DomainState.ACCEPTS)
    assert mailcheck.check(store, "info@company.ru").ok

    def explode(domain):
        raise AssertionError("второй раз в DNS ходить не должны")

    monkeypatch.setattr(mailcheck, "domain_state", explode)
    assert mailcheck.check(store, "info@company.ru").ok


def test_stale_check_is_repeated(store, dns):
    """Удачная проверка живёт месяц: домен могли закрыть за это время."""
    dns(DomainState.ACCEPTS)
    mailcheck.check(store, "info@company.ru")
    store.conn.execute(
        "UPDATE email_checks SET checked_at = '2020-01-01' WHERE address = ?",
        ("info@company.ru",),
    )
    store.conn.commit()

    dns(DomainState.DEAD, "домена не существует")
    assert not mailcheck.check(store, "info@company.ru").ok


def test_permanent_verdict_never_expires(store, dns, monkeypatch):
    """Домена нет — он от старости кэша не появится, перепроверять нечего."""
    dns(DomainState.DEAD)
    mailcheck.check(store, "info@ghost.ru")
    store.conn.execute("UPDATE email_checks SET checked_at = '2020-01-01'")
    store.conn.commit()

    def explode(domain):
        raise AssertionError("перепроверять мёртвый домен не должны")

    monkeypatch.setattr(mailcheck, "domain_state", explode)
    assert not mailcheck.check(store, "info@ghost.ru").ok


def test_case_and_spaces_do_not_make_a_second_entry(store, dns):
    dns(DomainState.ACCEPTS)
    mailcheck.check(store, "info@company.ru")
    mailcheck.check(store, "  INFO@Company.RU ")
    rows = store.conn.execute("SELECT COUNT(*) AS n FROM email_checks").fetchone()
    assert rows["n"] == 1


# --- Выбор адреса из нескольких -----------------------------------------------


def _contact(*emails: str) -> Contact:
    return Contact(kind=Kind.CLIENT, key="k", name="Компания", emails=list(emails))


def test_dead_first_address_gives_way_to_live_second(store, monkeypatch):
    """У компании старый домен и новый — теряем адрес, а не компанию."""
    monkeypatch.setattr(
        mailcheck, "domain_state",
        lambda domain: (DomainState.DEAD, "домена не существует")
        if domain == "old.ru" else (DomainState.ACCEPTS, "есть MX"),
    )
    address, verdict = mailcheck.pick_address(store, _contact("info@old.ru", "sales@new.ru"))
    assert address == "sales@new.ru"
    assert verdict.ok


def test_all_addresses_dead_is_a_permanent_verdict(store, dns):
    dns(DomainState.DEAD)
    address, verdict = mailcheck.pick_address(store, _contact("info@a.ru", "sales@b.ru"))
    assert address is None
    assert verdict.permanent


def test_one_unchecked_address_holds_the_contact(store, monkeypatch):
    """Пока хоть один адрес не проверен, контакт не закрываем."""
    monkeypatch.setattr(
        mailcheck, "domain_state",
        lambda domain: (DomainState.DEAD, "домена не существует")
        if domain == "a.ru" else (DomainState.UNKNOWN, "DNS не ответил вовремя"),
    )
    address, verdict = mailcheck.pick_address(store, _contact("info@a.ru", "sales@b.ru"))
    assert address is None
    assert not verdict.permanent
    assert verdict.retry_later


def test_contact_without_address_is_closed(store):
    address, verdict = mailcheck.pick_address(store, _contact())
    assert address is None
    assert verdict.permanent


def test_role_address_is_checked_first(store, dns):
    """Порядок предпочтения не ломается проверкой: info@ по-прежнему первый."""
    dns(DomainState.ACCEPTS)
    address, _ = mailcheck.pick_address(store, _contact("ivanov@company.ru", "info@company.ru"))
    assert address == "info@company.ru"


# --- Отправка -----------------------------------------------------------------


@pytest.fixture()
def smtp_ready(monkeypatch):
    from orbit import config

    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "sales@uniqore.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _queued(store, contact: Contact) -> int:
    pk = store.upsert_contact(contact)
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
    store.approve_draft(store.draft_for(pk).pk)
    return store.draft_for(pk).pk


def test_send_stops_on_dead_domain(store, dns, smtp_ready, monkeypatch):
    """Письмо пролежало в очереди, домен успел закрыться — не отправляем."""
    dns(DomainState.ACCEPTS)
    draft_pk = _queued(store, _contact("info@company.ru"))

    sent: list[str] = []
    monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: sent.append(a[0]))
    dns(DomainState.DEAD, "домена не существует")

    result = outreach.send(store, draft_pk)
    assert not result.ok
    assert not sent
    assert store.get_draft(draft_pk).status == "failed"


def test_dead_domain_closes_the_contact(store, dns, smtp_ready, monkeypatch):
    dns(DomainState.ACCEPTS)
    draft_pk = _queued(store, _contact("info@company.ru"))
    monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: None)
    dns(DomainState.DEAD)

    outreach.send(store, draft_pk)
    contact = store.get_contact(store.get_draft(draft_pk).contact_pk)
    assert contact.stage is Stage.DEAD


def test_send_waits_when_dns_is_silent(store, dns, smtp_ready, monkeypatch):
    """DNS молчит — письмо остаётся в очереди, а контакт живым."""
    dns(DomainState.ACCEPTS)
    draft_pk = _queued(store, _contact("info@company.ru"))
    monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: None)
    dns(DomainState.UNKNOWN)

    result = outreach.send(store, draft_pk)
    assert not result.ok
    contact = store.get_contact(store.get_draft(draft_pk).contact_pk)
    assert contact.stage is not Stage.DEAD


def test_send_uses_the_checked_address(store, smtp_ready, monkeypatch):
    """В письмо идёт адрес, прошедший проверку, а не первый по списку."""
    monkeypatch.setattr(
        mailcheck, "domain_state",
        lambda domain: (DomainState.DEAD, "нет") if domain == "old.ru"
        else (DomainState.ACCEPTS, "есть MX"),
    )
    draft_pk = _queued(store, _contact("info@old.ru", "sales@new.ru"))

    sent: list[str] = []
    monkeypatch.setattr(outreach, "deliver_email", lambda to, *a, **kw: sent.append(to))

    assert outreach.send(store, draft_pk).ok
    assert sent == ["sales@new.ru"]


# --- Подготовка письма --------------------------------------------------------


def test_model_is_not_paid_for_a_dead_address(store, dns, monkeypatch):
    """Проверка стоит запроса в DNS, письмо — денег за генерацию."""
    from orbit import llm
    from orbit.sections import base

    def explode(*args, **kwargs):
        raise AssertionError("модель звать не должны")

    monkeypatch.setattr(llm, "write_letter", explode)
    dns(DomainState.DEAD)

    pk = store.upsert_contact(_contact("info@ghost.ru"))
    ok, reason = base.write_for(store, store.get_contact(pk))
    assert not ok
    assert "не годится" in reason
    assert store.get_contact(pk).stage is Stage.DEAD


def test_unchecked_address_does_not_close_the_contact(store, dns, monkeypatch):
    from orbit import llm
    from orbit.sections import base

    monkeypatch.setattr(llm, "write_letter", lambda *a, **kw: None)
    dns(DomainState.UNKNOWN)

    pk = store.upsert_contact(_contact("info@company.ru"))
    ok, reason = base.write_for(store, store.get_contact(pk))
    assert not ok
    assert "не проверить" in reason
    assert store.get_contact(pk).stage is Stage.NEW


# --- Регистр и не-латиница ----------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["POSTMASTER@company.ru", "NoReply@Company.RU", "  ABUSE@Company.ru  "],
)
def test_service_mailbox_is_caught_in_any_case(store, address):
    """Один `POSTMASTER@` мимо списка — это письмо в спам-ловушку."""
    verdict = mailcheck.check(store, address)
    assert not verdict.ok
    assert verdict.permanent
    assert "служебный" in verdict.reason


def test_disposable_domain_is_caught_in_any_case(store):
    assert not mailcheck.check(store, "Info@MAILINATOR.com").ok


@pytest.mark.parametrize(
    "address",
    ["info@компания.рф", "почта@фирма.рф", "info@xn--80adxhks.рф"],
)
def test_cyrillic_domain_is_not_garbage(store, dns, address):
    """«компания.рф» — рабочий домен, а не мусор: DNS его понимает.

    Латинская проверка объявляла такой адрес «это не почтовый адрес» навсегда
    и закрывала контакт — то есть теряла живого клиента из-за кириллицы.
    """
    dns(DomainState.ACCEPTS)
    assert mailcheck.check(store, address).ok


def test_cyrillic_service_mailbox_is_still_caught(store, dns):
    dns(DomainState.ACCEPTS)
    assert not mailcheck.check(store, "postmaster@компания.рф").ok


# --- Что показываем владельцу -------------------------------------------------


def test_shown_address_is_the_one_that_will_be_used(store, monkeypatch):
    """Карточка обязана показывать тот адрес, который получит адресат."""
    monkeypatch.setattr(
        mailcheck, "domain_state",
        lambda domain: (DomainState.DEAD, "нет") if domain == "old.ru"
        else (DomainState.ACCEPTS, "есть MX"),
    )
    contact = _contact("info@old.ru", "sales@new.ru")
    chosen, _ = mailcheck.pick_address(store, contact)
    assert mailcheck.known_address(store, contact) == chosen == "sales@new.ru"


def test_shown_address_does_not_ask_dns(store, monkeypatch):
    """Экран не должен ждать резолвер: показываем по тому, что уже знаем."""

    def explode(domain):
        raise AssertionError("на экране в DNS не ходим")

    monkeypatch.setattr(mailcheck, "domain_state", explode)
    assert mailcheck.known_address(store, _contact("info@a.ru")) == "info@a.ru"


def test_shown_address_is_none_when_all_are_dead(store, dns):
    dns(DomainState.DEAD)
    contact = _contact("info@a.ru")
    mailcheck.pick_address(store, contact)
    assert mailcheck.known_address(store, contact) is None


def test_card_shows_the_checked_address(store, monkeypatch):
    """Сквозь весь экран: в карточке письма — проверенный адрес."""
    from orbit.models import Draft
    from orbit.sections import base, partners

    monkeypatch.setattr(
        mailcheck, "domain_state",
        lambda domain: (DomainState.DEAD, "нет") if domain == "old.ru"
        else (DomainState.ACCEPTS, "есть MX"),
    )
    contact = Contact(kind=Kind.PARTNER, key="k", name="Компания",
                      emails=["info@old.ru", "sales@new.ru"])
    pk = store.upsert_contact(contact)
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
    mailcheck.pick_address(store, store.get_contact(pk))  # проверка прошла, как при подготовке

    screen = base.show_draft(partners.TAB, store, str(store.draft_for(pk).pk))
    assert "sales@new.ru" in screen.text
    assert "info@old.ru" not in screen.text


# --- Телеграм как запасной канал ----------------------------------------------


def test_dead_email_does_not_bury_a_telegram_contact(store, dns, monkeypatch):
    """Почта мёртвая, телеграм живой — контакт не теряем, меняем канал."""
    from orbit import llm
    from orbit.sections import base

    dns(DomainState.DEAD)
    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact, channel=None, **kw: llm.Letter("пишем", "", "", "Текст в телеграм"),
    )
    contact = Contact(kind=Kind.CLIENT, key="k", name="Компания",
                      emails=["info@ghost.ru"], telegram=["https://t.me/company_chat"])
    pk = store.upsert_contact(contact)

    ok, _ = base.write_for(store, store.get_contact(pk))
    assert ok
    assert store.get_contact(pk).stage is not Stage.DEAD
    assert store.draft_for(pk).channel is Channel.TELEGRAM


def test_dead_email_without_telegram_still_closes(store, dns, monkeypatch):
    from orbit import llm
    from orbit.sections import base

    dns(DomainState.DEAD)
    monkeypatch.setattr(llm, "write_letter", lambda *a, **kw: None)
    pk = store.upsert_contact(_contact("info@ghost.ru"))
    ok, reason = base.write_for(store, store.get_contact(pk))
    assert not ok
    assert "не годится" in reason
    assert store.get_contact(pk).stage is Stage.DEAD


def test_absurdly_long_address_does_not_reach_dns(store, monkeypatch):
    """Склеенная из разметки строка не должна тратить запросы в DNS."""

    def explode(domain):
        raise AssertionError("в DNS за таким не ходим")

    monkeypatch.setattr(mailcheck, "domain_state", explode)
    verdict = mailcheck.check(store, "info@" + "x" * 300 + ".ru")
    assert not verdict.ok
    assert verdict.permanent


@pytest.mark.parametrize(
    "address",
    ["a@b", "a@.ru", "a@b..ru", "два слова@a.ru", "a@b.ru лишнее", "a@b.c"],
)
def test_relaxed_regex_still_rejects_junk(store, address):
    """Разрешив кириллицу, мусор пропускать не начали."""
    assert not mailcheck.check(store, address).ok


@pytest.mark.parametrize(
    ("emails", "expected"),
    [
        (["webmaster@alpha.ru", "ivanov@alpha.ru"], "ivanov@alpha.ru"),
        (["info@mailinator.com", "sales@beta.ru"], "sales@beta.ru"),
        (["info@gamma.r", "sales@gamma.ru"], "sales@gamma.ru"),
        (["ivanov@gmail.com", "sales@delta.ru"], "sales@delta.ru"),
    ],
)
def test_shown_address_skips_what_sending_skips(store, dns, emails, expected):
    """Отсев без сети (служебный, одноразовый, кривой) в кэш не попадает.

    Из-за этого экран показывал `webmaster@`, а письмо уходило на следующий
    адрес: отправка отсеивала его сама, а карточка про этот отсев не знала.
    """
    dns(DomainState.ACCEPTS)
    contact = _contact(*emails)
    picked, _ = mailcheck.pick_address(store, contact)
    assert picked == expected
    assert mailcheck.known_address(store, contact) == expected


def test_contact_card_shows_the_checked_address(store, dns):
    """Карточка контакта — второй экран с адресом, и он тоже обязан совпадать."""
    from orbit.sections import base, partners

    dns(DomainState.ACCEPTS)
    contact = Contact(kind=Kind.PARTNER, key="k", name="Компания",
                      emails=["webmaster@alpha.ru", "ivanov@alpha.ru"])
    pk = store.upsert_contact(contact)

    screen = base.card(partners.TAB, store, str(pk))
    assert "ivanov@alpha.ru" in screen.text
    assert "webmaster@alpha.ru" not in screen.text


def test_contact_card_without_a_usable_address_says_so(store, dns):
    """Все адреса негодны — карточка не должна показывать ни одного."""
    from orbit.sections import base, partners

    dns(DomainState.DEAD)
    contact = Contact(kind=Kind.PARTNER, key="k", name="Компания",
                      emails=["info@ghost.ru"], phones=["+79990000000"])
    pk = store.upsert_contact(contact)
    mailcheck.pick_address(store, store.get_contact(pk))

    screen = base.card(partners.TAB, store, str(pk))
    assert "info@ghost.ru" not in screen.text


# --- Мёртвая почта не хоронит контакт ни на одном пути -------------------------


def _with_telegram(store, dns_state):
    contact = Contact(kind=Kind.CLIENT, key="k", name="Компания",
                      emails=["info@ghost.ru"], telegram=["https://t.me/company_chat"])
    return store.upsert_contact(contact)


def test_send_does_not_bury_a_telegram_contact(store, dns, smtp_ready, monkeypatch):
    """Третий путь к тому же решению: отправка тоже не должна закрывать контакт."""
    dns(DomainState.ACCEPTS)
    pk = _with_telegram(store, dns)
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
    draft_pk = store.draft_for(pk).pk
    store.approve_draft(draft_pk)

    monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: None)
    dns(DomainState.DEAD)
    result = outreach.send(store, draft_pk)

    assert not result.ok
    assert store.get_contact(pk).stage is not Stage.DEAD


def test_send_still_closes_when_there_is_nowhere_else(store, dns, smtp_ready, monkeypatch):
    dns(DomainState.ACCEPTS)
    contact = Contact(kind=Kind.CLIENT, key="k", name="Компания", emails=["info@ghost.ru"])
    pk = store.upsert_contact(contact)
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
    draft_pk = store.draft_for(pk).pk
    store.approve_draft(draft_pk)

    monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: None)
    dns(DomainState.DEAD)
    outreach.send(store, draft_pk)
    assert store.get_contact(pk).stage is Stage.DEAD


def test_followup_checks_the_address_too(store, dns, monkeypatch):
    """Цепочка касаний не должна обходить проверку стороной через три дня."""
    from orbit import followup, llm

    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact, channel=None, **kw: llm.Letter("пишем", "", "Тема", "Текст"),
    )
    dns(DomainState.DEAD)
    contact = Contact(kind=Kind.PARTNER, key="k", name="Компания",
                      emails=["info@ghost.ru"], stage=Stage.WRITTEN)
    pk = store.upsert_contact(contact)

    ok, reason = followup.write_one(store, store.get_contact(pk))
    assert not ok
    assert "не годится" in reason


def test_followup_switches_to_telegram_instead_of_closing(store, dns, monkeypatch):
    from orbit import followup, llm

    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact, channel=None, **kw: llm.Letter("пишем", "", "", "Текст"),
    )
    dns(DomainState.DEAD)
    contact = Contact(kind=Kind.PARTNER, key="k", name="Компания", emails=["info@ghost.ru"],
                      telegram=["https://t.me/company_chat"], stage=Stage.WRITTEN)
    pk = store.upsert_contact(contact)

    ok, _ = followup.write_one(store, store.get_contact(pk))
    assert ok
    assert store.get_contact(pk).stage is not Stage.DEAD
    assert store.draft_for(pk).channel is Channel.TELEGRAM


# --- Ветки, которые разбор отметил как непокрытые -------------------------------


def test_lowercasing_lives_inside_the_check(store):
    """Проверка не должна полагаться на то, что регистр привёл вызывающий."""
    assert mailcheck._check_offline("POSTMASTER@Company.RU") is not None
    assert mailcheck._check_offline("  Info@MAILINATOR.com  ") is not None
    assert mailcheck._check_offline("IVANOV@GMAIL.COM") is not None


def test_dns_exception_becomes_unknown_not_dead(monkeypatch, no_pause):
    """Сбой резолвера — это «не знаю», а не «домена нет».

    Раньше неожиданное исключение из ветки A/AAAA улетало наружу и роняло
    проход целиком.
    """
    import dns.resolver

    class Boom(dns.resolver.Resolver):
        def resolve(self, *args, **kwargs):
            raise RuntimeError("резолвер сломался неожиданным образом")

    monkeypatch.setattr(dns.resolver, "Resolver", Boom)
    state, _ = mailcheck._ask_dns("company.ru")
    assert state is DomainState.UNKNOWN

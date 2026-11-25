"""Контакт: чем его можно достать и куда он попадёт.

От выбора канала зависит, уйдёт ли письмо само или будет ждать руки владельца,
а от проверки адреса — не попадёт ли холодное письмо на личную почту человека.
"""
from __future__ import annotations

import pytest

from orbit.models import (
    Channel,
    Contact,
    Kind,
    is_corporate_email,
    normalize_telegram,
    same_company,
)


def _contact(**kwargs) -> Contact:
    data = {"kind": Kind.CLIENT, "key": "x.ru", "name": "Тест"}
    data.update(kwargs)
    return Contact(**data)


# --- Адреса -----------------------------------------------------------------

@pytest.mark.parametrize("address", ["hi@uniqore.ru", "info@agency.com", "a.b@sub.domain.pro"])
def test_corporate_emails_pass(address):
    assert is_corporate_email(address)


@pytest.mark.parametrize(
    "address",
    ["vasya@gmail.com", "ivan@mail.ru", "test@yandex.ru", "", "не почта", "@nodomain", "a@b"],
)
def test_personal_and_broken_emails_rejected(address):
    """Личный ящик это человек, а не компания. Холодные письма туда не шлём."""
    assert not is_corporate_email(address)


# --- Телеграм ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://t.me/uniqore", "@uniqore"),
        ("t.me/uniqore_bot", "@uniqore_bot"),
        ("@uniqore", "@uniqore"),
        ("uniqore", "@uniqore"),
        ("https://telegram.me/agency", "@agency"),
    ],
)
def test_telegram_normalized(raw, expected):
    assert normalize_telegram(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "https://vk.com/club1"])
def test_telegram_garbage_is_none(raw):
    """Короткий или чужой хэндл лучше потерять, чем написать не туда."""
    assert normalize_telegram(raw) is None


# --- Выбор канала -----------------------------------------------------------

def test_email_wins_over_telegram():
    """Почта уходит сама, телеграм требует ручного шага — почта первее."""
    contact = _contact(emails=["hi@agency.ru"], telegram=["@agency"])
    assert contact.channel is Channel.EMAIL


def test_telegram_used_when_no_corporate_email():
    contact = _contact(emails=["vasya@gmail.com"], telegram=["@agency"])
    assert contact.channel is Channel.TELEGRAM
    assert contact.best_email is None


def test_only_phone_is_not_reachable():
    """Такие контакты идут в обзвон, а не в рассылку."""
    contact = _contact(phones=["+79991234567"])
    assert contact.channel is Channel.PHONE
    assert contact.reachable is False


def test_nothing_at_all_is_not_reachable():
    assert _contact().reachable is False


def test_site_url_gets_scheme():
    """Без схемы ссылка в Telegram не кликается."""
    assert _contact(site="agency.ru").url == "https://agency.ru"
    assert _contact(site="https://agency.ru").url == "https://agency.ru"
    assert _contact().url is None


# --- Личные ящики: список должен покрывать то, чем реально пользуются ----------


@pytest.mark.parametrize(
    "address",
    [
        # Россия и СНГ
        "ivan@yandex.by", "ivan@yandex.com", "ivan@yandex.kz", "ivan@narod.ru",
        "ivan@pochta.ru", "ivan@tut.by", "ivan@ukr.net", "ivan@i.ua",
        # Мировые
        "ivan@yahoo.com", "ivan@googlemail.com", "ivan@proton.me",
        "ivan@protonmail.com", "ivan@live.com", "ivan@msn.com", "ivan@me.com",
        "ivan@aol.com", "ivan@gmx.net", "ivan@zoho.com", "ivan@outlook.ru",
    ],
)
def test_personal_mailbox_is_not_corporate(address):
    """Адрес на бесплатном домене — это человек, а не компания.

    Холодное письмо туда нельзя: по 152-ФЗ это персональные данные, а не
    контакт организации. Список доменов раньше знал только десяток самых
    частых, и на yandex.by, yahoo.com, proton.me письмо ушло бы спокойно.
    """
    assert not is_corporate_email(address)


@pytest.mark.parametrize(
    "address",
    ["info@uniqore.ru", "sales@mediasfera.ru", "hello@studio.agency", "info@компания.рф"],
)
def test_corporate_mailbox_still_passes(address):
    """Расширяя список, нельзя случайно отсечь настоящие рабочие адреса."""
    assert is_corporate_email(address)


# --- Чей это вообще адрес ------------------------------------------------------


def test_own_domain_beats_a_role_mailbox_elsewhere():
    """Обезличенный ящик чужого бренда — это чужая компания, а не наш адресат.

    На сайте института пластической хирургии лежало шесть адресов, и `info@`
    среди них принадлежал другому бренду, а собственные ящики института
    обезличенными не выглядели. Письмо ушло бы не туда.
    """
    contact = Contact(
        kind=Kind.CLIENT, key="k", name="Институт", site="iphk.ru",
        emails=["info@lancette.clinic", "iphk@iphk.ru", "u.center@iphk.ru"],
    )
    assert contact.best_email == "iphk@iphk.ru"
    assert contact.email_candidates[:2] == ["iphk@iphk.ru", "u.center@iphk.ru"]


def test_role_mailbox_wins_inside_the_same_domain():
    """Внутри своего домена по-прежнему предпочитаем обезличенный — это 152-ФЗ."""
    contact = Contact(
        kind=Kind.CLIENT, key="k", name="Компания", site="company.ru",
        emails=["ivanov@company.ru", "info@company.ru"],
    )
    assert contact.best_email == "info@company.ru"


def test_without_a_site_the_old_order_holds():
    """Сайт неизвестен — сравнивать не с чем, работает прежнее правило."""
    contact = Contact(
        kind=Kind.CLIENT, key="k", name="Компания",
        emails=["ivanov@a.ru", "info@b.ru"],
    )
    assert contact.best_email == "info@b.ru"


@pytest.mark.parametrize(
    "site",
    ["company.ru", "https://company.ru", "http://www.company.ru/", "www.company.ru"],
)
def test_site_is_recognised_in_any_form(site):
    """Сайт приходит из разных источников и записан по-разному."""
    contact = Contact(
        kind=Kind.CLIENT, key="k", name="Компания", site=site,
        emails=["info@other.ru", "sales@company.ru"],
    )
    assert contact.best_email == "sales@company.ru"


def test_foreign_domain_is_not_dropped_entirely():
    """Чужой адрес остаётся запасным: свой домен может оказаться мёртвым."""
    contact = Contact(
        kind=Kind.CLIENT, key="k", name="Компания", site="company.ru",
        emails=["info@other.ru", "sales@company.ru"],
    )
    assert contact.email_candidates == ["sales@company.ru", "info@other.ru"]


# --- Та ли это вообще компания -------------------------------------------------


@pytest.mark.parametrize(
    ("site", "address"),
    [
        ("company.ru", "info@company.ru"),          # ровно тот же домен
        ("blackline-pr.ru", "hello@blackline-pr.com"),  # тот же бренд, другая зона
        ("mygribs.com", "partner@mygribs.ru"),
        ("stomatologiya.doct.ru", "info@doct.ru"),  # сайт на поддомене
        ("new.astra.agency", "marketing@astra.agency"),
        (None, "info@anything.ru"),                 # сайта нет — сравнивать не с чем
        ("company.ru", None),
    ],
)
def test_same_company(site, address):
    assert same_company(site, address)


@pytest.mark.parametrize(
    ("site", "address"),
    [
        ("zavod.team", "klaus@altfm.ru"),
        ("hl2b.ru", "alexts@dadcan.ru"),
        ("digital-info.agency", "info@transinfo.mobi"),
    ],
)
def test_clearly_another_company(site, address):
    """Живые примеры из выгрузки: адрес принадлежит совсем другой фирме."""
    assert not same_company(site, address)


def test_foreign_email_is_flagged_not_blocked():
    """Пометка, а не запрет: письмо всё равно можно отправить, решает владелец."""
    contact = Contact(kind=Kind.PARTNER, key="k", name="Zavod",
                      site="zavod.team", emails=["klaus@altfm.ru"])
    assert contact.foreign_email
    assert contact.best_email == "klaus@altfm.ru"   # адрес никуда не делся
    assert contact.reachable


def test_own_domain_is_not_flagged():
    contact = Contact(kind=Kind.PARTNER, key="k", name="К",
                      site="company.ru", emails=["info@company.ru"])
    assert not contact.foreign_email


def test_no_site_no_suspicion():
    """Сайт неизвестен — это не повод подозревать адрес."""
    contact = Contact(kind=Kind.PARTNER, key="k", name="К", emails=["info@company.ru"])
    assert not contact.foreign_email


def test_best_email_prefers_the_companys_own_domain_over_a_role_box():
    """Строка документации обещала обратное — проверяем, что делает код.

    Именной адрес на домене компании обходит `info@` на чужом домене. Так и
    задумано: `info@` с чужого домена — чужая компания, письмо туда уйдёт не
    по адресу, и это заметит получатель.
    """
    contact = Contact(
        kind=Kind.CLIENT, key="k", name="Институт", site="institut.ru",
        emails=["info@chuzhoy.ru", "a.ivanov@institut.ru"],
    )
    assert contact.best_email == "a.ivanov@institut.ru"


def test_role_box_wins_inside_the_same_domain():
    """А внутри своего домена вперёд идёт обезличенный ящик — это про 152-ФЗ."""
    contact = Contact(
        kind=Kind.CLIENT, key="k", name="Институт", site="institut.ru",
        emails=["a.ivanov@institut.ru", "info@institut.ru"],
    )
    assert contact.best_email == "info@institut.ru"


@pytest.mark.parametrize(
    ("site", "address", "same"),
    [
        ("smmashing.media", "info@wunder-digital.media", False),
        ("a-studio.digital", "info@b-studio.digital", False),
        ("zavod.team", "info@altfm.ru", False),
        ("blackline-pr.ru", "info@blackline-pr.com", True),
        ("stomatologiya.doct.ru", "info@doct.ru", True),
        ("example.com.ru", "info@example.ru", True),
        ("shop.example.co.uk", "info@example.co.uk", True),
        ("medsi.ru", "info@medsi.ru", True),
    ],
)
def test_same_company_cuts_the_zone_instead_of_listing_it(site, address, same):
    """Зона — это последний кусок имени, а не то, что попало в наш список.

    Список зон перечислял «ru, com, team, pro…», и всё, чего в нём не
    оказалось, считалось именем компании: `smmashing.media` и
    `wunder-digital.media` давали одно ядро «media», то есть два разных
    агентства признавались одной компанией. Новых зон бесконечно много
    (.media, .digital, .studio, .art), угнаться за ними списком нельзя.
    """
    assert same_company(site, address) is same

"""Тесты правил площадок.

На Кворке владелец уже получил блокировку на месяц за переданный контакт.
Эти тесты — про то, чтобы такое не ушло автоматом ещё раз.
"""
from __future__ import annotations

import pytest

from fbid.models import Source
from fbid.policy import is_usable, policy_for, sanitize

CLEAN = (
    "Здравствуйте! Я из студии UniqOre. По вашей задаче: каталог с выгрузкой в 1С "
    "делали дважды, обычно укладываемся в 3-4 дня, оплата 50/50. Выгрузка нужна "
    "по расписанию или по кнопке из админки?"
)
WITH_CONTACTS = (
    "Здравствуйте! Наш сайт uniqore.ru, пишите в телеграм @uniqore или на "
    "+7 999 123-45-67, почта hi@uniqore.ru. Сделаем каталог за 3-4 дня."
)


# --- Где что разрешено -----------------------------------------------------

def test_fl_and_profi_allow_links():
    for source in (Source.FL, Source.PROFI):
        rules = policy_for(source)
        assert rules.allow_links and rules.allow_contacts


def test_youdo_forbids_links_and_contacts():
    rules = policy_for(Source.YOUDO)
    assert not rules.allow_links
    assert not rules.allow_contacts


# --- Вычистка --------------------------------------------------------------

def test_free_platform_keeps_all_contacts():
    """На FL и Profi контакты разрешены — вырезать нечего.

    Единственная правка, которая там происходит, — голый домен получает
    https://, иначе биржа не сделает его кликабельным.
    """
    out = sanitize(WITH_CONTACTS, Source.FL)
    for keep in ("@uniqore", "+7 999 123-45-67", "hi@uniqore.ru", "телеграм"):
        assert keep in out
    assert "https://uniqore.ru" in out


@pytest.mark.parametrize(
    "forbidden", ["uniqore.ru", "@uniqore", "+7 999 123-45-67", "hi@uniqore.ru", "телеграм"]
)
def test_strict_platform_loses_every_contact(forbidden):
    cleaned = sanitize(WITH_CONTACTS, Source.YOUDO)
    assert forbidden not in cleaned


def test_email_domain_does_not_survive_as_link():
    """Почту режем до ссылок, иначе от «hi@uniqore.ru» остаётся огрызок."""
    cleaned = sanitize("Пишите на hi@uniqore.ru", Source.YOUDO)
    assert "uniqore" not in cleaned


def test_clean_text_passes_strict_platform_unchanged():
    assert sanitize(CLEAN, Source.YOUDO) == CLEAN


# --- Можно ли это отправлять ----------------------------------------------

def test_clean_text_is_usable_everywhere():
    for source in (Source.FL, Source.PROFI, Source.YOUDO):
        assert is_usable(CLEAN, sanitize(CLEAN, source), source) is True


def test_text_that_needed_cleaning_is_not_sent_to_strict_platform():
    """Порезанная фраза читается как обрывок, а бан стоит месяца работы."""
    cleaned = sanitize(WITH_CONTACTS, Source.YOUDO)
    assert is_usable(WITH_CONTACTS, cleaned, Source.YOUDO) is False


def test_same_text_is_fine_where_links_are_allowed():
    cleaned = sanitize(WITH_CONTACTS, Source.FL)
    assert is_usable(WITH_CONTACTS, cleaned, Source.FL) is True


def test_empty_text_is_never_usable():
    assert is_usable(CLEAN, "", Source.FL) is False


def test_unknown_platform_is_treated_as_strict():
    """Новая площадка, правил которой мы не знаем, считается строгой."""

    class FakeSource:
        value = "kwork"

    rules = policy_for(FakeSource())
    assert not rules.allow_links and not rules.allow_contacts


# --- Ссылка должна кликаться ------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Подробнее о нас: uniqore.ru", "Подробнее о нас: https://uniqore.ru"),
        ("демо uniqore.ru/demo/klinika/", "демо https://uniqore.ru/demo/klinika/"),
        ("Мы студия UniqOre, uniqore.ru.", "Мы студия UniqOre, https://uniqore.ru."),
    ],
)
def test_bare_domain_becomes_clickable(text, expected):
    """На FL голый домен остаётся текстом — клиент по нему не кликнет."""
    from fbid.policy import ensure_clickable_links

    assert ensure_clickable_links(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Уже целиком: https://uniqore.ru",
        "И так: http://uniqore.ru/demo/",
        "Почта: hi@uniqore.ru",
        "Версия 3.11, цена 45.000 рублей",
        "Файл config.py и папка knowledge.md",
        "",
    ],
)
def test_what_should_not_be_touched(text):
    from fbid.policy import ensure_clickable_links

    assert ensure_clickable_links(text) == text


def test_sanitize_makes_links_clickable_where_allowed():
    from fbid.policy import sanitize

    out = sanitize("Наши работы: uniqore.ru", Source.FL)
    assert "https://uniqore.ru" in out


def test_strict_platform_still_has_no_links_at_all():
    """На Юду ссылок быть не должно вообще — дописывать там нечего."""
    from fbid.policy import sanitize

    out = sanitize("Наши работы: uniqore.ru", Source.YOUDO)
    assert "uniqore.ru" not in out
    assert "https://" not in out


def test_a_bare_number_is_a_phone_too():
    """«79161234567» — тот же номер, только без плюса.

    На строгих площадках номер в переписке стоит месяца блокировки аккаунта, а
    именно этот вид записи вычистка пропускала: так номер пишут в ссылках tel:
    и просто в тексте.
    """
    from fbid.models import Source
    from fbid.policy import is_usable, sanitize

    text = "Пишите мне 79161234567, обсудим"
    cleaned = sanitize(text, Source.YOUDO)
    assert "79161234567" not in cleaned
    assert not is_usable(text, cleaned, Source.YOUDO), "такой текст отправлять нельзя"


def test_an_inn_is_not_a_phone():
    """ИНН и ОГРН вычищать не за что: цифр столько же или больше."""
    from fbid.models import Source
    from fbid.policy import sanitize

    for text in ("ИНН 7707083893, работаем официально", "ОГРН 1027700132195"):
        assert sanitize(text, Source.YOUDO) == text, text

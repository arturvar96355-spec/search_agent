"""Защита от рассылки по филиалам одной сети.

В 2ГИС сеть стоит десятком карточек: у «Медси» их тринадцать, у «Авторуси»
шесть. Адреса и телефоны разные, а компания одна. Без этой проверки владелец
разослал бы тринадцать писем в один головной офис и получил бы жалобу на спам
вместо ответа.
"""
from __future__ import annotations

import pytest

from orbit import llm
from orbit.models import Contact, Kind, Stage
from orbit.sections import base
from orbit.store import network_name


@pytest.fixture()
def writes_ok(monkeypatch):
    """Модель всегда возвращает готовое письмо — проверяем не её, а защиту."""
    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact, channel=None: llm.Letter("пишем", "", "Тема", "Текст письма."),
    )


def _branch(key: str, name: str = "Медси, сеть медицинских центров") -> Contact:
    return Contact(
        kind=Kind.CLIENT, key=key, name=name, emails=["info@medsi.ru"], site="medsi.ru"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Медси, сеть медицинских центров", "медси"),
        ("«Медси», клиника", "медси"),
        ("МЕДСИ", "медси"),
        ("Авторусь, магазин автотоваров и технических жидкостей", "авторусь"),
    ],
)
def test_network_name_extracted(raw, expected):
    assert network_name(raw) == expected


def test_different_companies_are_not_siblings():
    assert network_name("Улыбка, стоматология") != network_name("Медси, клиника")


def test_second_branch_is_not_written_to(store, writes_ok):
    first = store.upsert_contact(_branch("https://2gis.ru/firm/1"))
    second = store.upsert_contact(_branch("https://2gis.ru/firm/2"))

    ok, _ = base.write_for(store, store.get_contact(first))
    assert ok

    ok, reason = base.write_for(store, store.get_contact(second))
    assert not ok
    assert "сети уже писали" in reason
    assert store.get_contact(second).stage is Stage.DEAD


def test_branch_reason_names_the_sibling(store, writes_ok):
    store.upsert_contact(_branch("https://2gis.ru/firm/1"))
    second = store.upsert_contact(_branch("https://2gis.ru/firm/2"))
    base.write_for(store, store.get_contact(store.find_contact(Kind.CLIENT, "https://2gis.ru/firm/1").pk))
    base.write_for(store, store.get_contact(second))
    events = store.events(second)
    assert any("Медси" in row["text"] for row in events)


def test_untouched_network_is_written_to(store, writes_ok):
    """Пока сети не писали, первому филиалу письмо готовится нормально."""
    pk = store.upsert_contact(_branch("https://2gis.ru/firm/1"))
    ok, _ = base.write_for(store, store.get_contact(pk))
    assert ok
    assert store.draft_for(pk) is not None


def test_dead_sibling_does_not_block(store, writes_ok):
    """Закрытый контакт это не «уже писали» — сети всё ещё можно написать."""
    first = store.upsert_contact(_branch("https://2gis.ru/firm/1"))
    store.set_stage(first, Stage.DEAD)
    second = store.upsert_contact(_branch("https://2gis.ru/firm/2"))
    ok, _ = base.write_for(store, store.get_contact(second))
    assert ok


def test_partner_networks_checked_separately(store, writes_ok):
    """Агентство и компания с похожим названием — не одна сеть."""
    store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="medsi.ru", name="Медси", emails=["hi@medsi.ru"]
    ))
    client = store.upsert_contact(_branch("https://2gis.ru/firm/1"))
    base.write_for(store, store.get_contact(store.find_contact(Kind.PARTNER, "medsi.ru").pk))
    ok, _ = base.write_for(store, store.get_contact(client))
    assert ok


# --- Одно имя — не всегда одна сеть --------------------------------------------


def _clinic(key: str, site: str) -> Contact:
    """Карточка 2ГИС, где до запятой стоит город, а не название сети."""
    return Contact(
        kind=Kind.CLIENT, key=key, name="Москва, стоматологическая клиника",
        site=site, emails=[f"info@{site}"],
    )


def test_same_city_name_is_not_the_same_network(store, writes_ok):
    """Две несвязанные клиники подписаны одинаково — второй тоже надо написать.

    В карточках 2ГИС до запятой попадает не только имя сети, но иногда и
    город. По одному имени вторая клиника закрывалась как филиал первой.
    """
    first = store.upsert_contact(_clinic("https://2gis.ru/firm/10", "mmccdzm.ru"))
    second = store.upsert_contact(_clinic("https://2gis.ru/firm/11", "dantistoff.ru"))

    ok, _ = base.write_for(store, store.get_contact(first))
    assert ok

    ok, reason = base.write_for(store, store.get_contact(second))
    assert ok, reason
    assert store.get_contact(second).stage is not Stage.DEAD


def test_real_network_is_still_caught(store, writes_ok):
    """У настоящей сети домен один на все филиалы — она по-прежнему ловится."""
    first = store.upsert_contact(_branch("https://2gis.ru/firm/1"))
    second = store.upsert_contact(_branch("https://2gis.ru/firm/2"))
    for pk in (first, second):
        contact = store.get_contact(pk)
        contact.site = "medsi.ru"
        store.upsert_contact(contact)

    assert base.write_for(store, store.get_contact(first))[0]
    ok, reason = base.write_for(store, store.get_contact(second))
    assert not ok
    assert "сети уже писали" in reason


def test_branch_without_a_site_is_still_a_branch(store, writes_ok):
    """Сайт неизвестен — сравнивать не с чем, работает прежнее правило по имени."""
    first = store.upsert_contact(_branch("https://2gis.ru/firm/1"))
    second = store.upsert_contact(_branch("https://2gis.ru/firm/2"))
    assert base.write_for(store, store.get_contact(first))[0]
    ok, _ = base.write_for(store, store.get_contact(second))
    assert not ok

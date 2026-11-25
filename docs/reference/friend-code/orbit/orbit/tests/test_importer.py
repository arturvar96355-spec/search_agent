"""Импорт выгрузок парсера.

Выгрузки приходят из Excel, и в них попадает всё: пустые ячейки в виде NaN,
списки, склеенные через запятую, адреса с типом в скобках. Ни одна такая
строка не должна ронять импорт целиком.
"""
from __future__ import annotations

import json

import pytest

from orbit import importer
from orbit.models import Kind

AGENCY_ROW = {
    "name": "Медиасфера",
    "website": "https://WWW.Mediasfera.RU/about/",
    "pitch": "сайты + CRM",
    "dev_level": "none",
    "best_email": "hello@mediasfera.ru",
    "emails": "hello@mediasfera.ru (general), sales@mediasfera.ru (sales)",
    "phones": "+7 495 123-45-67",
    "telegram": "https://t.me/mediasfera",
    "city": "Москва",
    "niche_tags": "медицина, авто",
    "services_raw": "SMM, таргет",
}

ORG_ROW = {
    "name": "Стоматология «Улыбка»",
    "address": "Москва, ул. Ленина, 5",
    "phone": "+7 495 765-43-21",
    "website": "smile-dent.ru",
    "rubric": "Стоматология",
    "rating": 4.2,
    "reviews_count": 87,
    "review_complaints": "не дозвониться | долго не отвечают",
    "source_url": "https://2gis.ru/firm/123",
}


# --- Агентства --------------------------------------------------------------

def test_agency_converted():
    contact = importer.agency_to_contact(AGENCY_ROW)
    assert contact.kind is Kind.PARTNER
    assert contact.key == "mediasfera.ru"      # ключ дедупа — голый домен
    assert contact.emails == ["hello@mediasfera.ru", "sales@mediasfera.ru"]
    assert contact.telegram == ["@mediasfera"]
    assert contact.meta["pitch"] == "сайты + CRM"


def test_agency_without_site_keyed_by_name():
    """Без домена ключом становится имя, иначе безымянные строки слипнутся."""
    contact = importer.agency_to_contact({**AGENCY_ROW, "website": ""})
    assert contact.key.startswith("name:")


def test_row_without_name_skipped():
    assert importer.agency_to_contact({"website": "a.ru"}) is None
    assert importer.org_to_contact({"phone": "+79991234567"}) is None


# --- Компании ---------------------------------------------------------------

def test_org_converted():
    contact = importer.org_to_contact(ORG_ROW)
    assert contact.kind is Kind.CLIENT
    assert contact.site == "smile-dent.ru"
    assert contact.phones == ["+7 495 765-43-21"]
    assert contact.meta["complaints"] == ["не дозвониться", "долго не отвечают"]
    assert contact.city == "Москва"


def test_org_keyed_by_card_url():
    """Ссылка на карточку уникальна и есть всегда — она надёжнее телефона."""
    assert importer.org_to_contact(ORG_ROW).key == "https://2gis.ru/firm/123"


def test_same_company_from_two_exports_is_one_contact(store):
    """2ГИС в демо-режиме не отдаёт телефон.

    Раньше выгрузка с номером и выгрузка без него давали два контакта, и
    компания получала два письма.
    """
    with_phone = importer.org_to_contact(ORG_ROW)
    without_phone = importer.org_to_contact({**ORG_ROW, "phone": [], "website": ""})
    store.upsert_contact(with_phone)
    store.upsert_contact(without_phone)
    assert store.count(Kind.CLIENT) == 1


def test_org_without_card_keyed_by_phone():
    contact = importer.org_to_contact({**ORG_ROW, "website": "", "source_url": ""})
    assert contact.key == "+7 495 765-43-21"


def test_org_with_nothing_keyed_by_name_and_address():
    """Две «Пятёрочки» на разных улицах должны остаться двумя записями."""
    first = importer.org_to_contact({"name": "Пятёрочка", "address": "Москва, Ленина 1"})
    second = importer.org_to_contact({"name": "Пятёрочка", "address": "Москва, Мира 7"})
    assert first.key != second.key


def test_phones_as_list_from_jsonl():
    """В .jsonl телефоны приходят списком: без развилки контакт терял номер."""
    contact = importer.org_to_contact({**ORG_ROW, "phone": ["+74951112233", "+74954445566"]})
    assert contact.phones == ["+74951112233", "+74954445566"]
    assert contact.primary_phone == "+74951112233"


def test_agency_lists_from_jsonl():
    contact = importer.agency_to_contact({
        **AGENCY_ROW, "phones": ["+74951112233"], "telegram": ["@agency"], "niche_tags": ["авто"]
    })
    assert contact.phones == ["+74951112233"]
    assert contact.telegram == ["@agency"]


def test_complaints_as_list_from_jsonl():
    """В .jsonl жалобы приходят списком, а не строкой через «|»."""
    contact = importer.org_to_contact({**ORG_ROW, "review_complaints": ["не берут трубку"]})
    assert contact.meta["complaints"] == ["не берут трубку"]


def test_empty_excel_cell_does_not_break(monkeypatch):
    """Пустая ячейка Excel приходит как NaN, а не как пустая строка."""
    nan = float("nan")
    contact = importer.org_to_contact({**ORG_ROW, "review_complaints": nan, "rating": nan})
    assert contact.meta.get("complaints") in (None, [])
    assert "rating" not in contact.meta


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Agency.RU/about", "agency.ru"),
        ("agency.ru", "agency.ru"),
        ("http://agency.ru:8080/x?y=1", "agency.ru"),
        ("", ""),
    ],
)
def test_domain_normalized(raw, expected):
    assert importer.domain_of(raw) == expected


# --- Файлы ------------------------------------------------------------------

def test_jsonl_read(tmp_path):
    path = tmp_path / "leads.jsonl"
    path.write_text(
        json.dumps({"query": {"source": "2gis"}, "org": ORG_ROW}, ensure_ascii=False) + "\n"
        + json.dumps({"query": {"source": "2gis"}, "done": True}, ensure_ascii=False) + "\n"
        + "{битая строка\n",
        encoding="utf-8",
    )
    rows = importer.read_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["name"] == ORG_ROW["name"]


def test_kind_detected_by_columns():
    assert importer.looks_like_agencies([AGENCY_ROW]) is True
    assert importer.looks_like_agencies([ORG_ROW]) is False
    assert importer.looks_like_agencies([]) is False


def test_import_file_is_idempotent(store, tmp_path):
    path = tmp_path / "leads.jsonl"
    path.write_text(
        json.dumps({"org": ORG_ROW}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    importer.import_file(store, path)
    importer.import_file(store, path)
    assert store.count(Kind.CLIENT) == 1


def test_missing_file_reports_zero(store, tmp_path):
    assert importer.import_file(store, tmp_path / "нет.xlsx") == (0, 0)
    assert importer.import_dir(store, tmp_path / "нет") == (0, 0, [])


def test_one_broken_file_does_not_stop_the_import(store, tmp_path, monkeypatch):
    """Битый файл в середине забирал с собой все остальные выгрузки.

    Владелец при этом видел только «команда упала» — и не знал, что часть
    базы просто не доехала.
    """
    from orbit import importer

    for name in ("a.csv", "b.csv", "c.csv"):
        (tmp_path / name).write_text("name,site\nФирма,firma.ru\n", encoding="utf-8")

    real = importer.import_file

    def flaky(store_, path, kind=None):
        if path.name == "b.csv":
            raise ValueError("файл побит")
        return real(store_, path, kind)

    monkeypatch.setattr(importer, "import_file", flaky)
    rows, written, broken = importer.import_dir(store, tmp_path)

    assert rows > 0          # a.csv и c.csv доехали
    assert written > 0
    assert broken == ["b.csv"], "имя непрочитанного файла должно дойти до владельца"


def test_all_files_broken_is_not_a_crash(store, tmp_path, monkeypatch):
    from orbit import importer

    (tmp_path / "a.csv").write_text("name\nФирма\n", encoding="utf-8")
    monkeypatch.setattr(
        importer, "import_file",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("побит")),
    )
    assert importer.import_dir(store, tmp_path) == (0, 0, ["a.csv"])


def test_broken_dumps_are_not_reported_as_an_empty_directory(store, tmp_path, monkeypatch):
    """Экран должен различать «нечего читать» и «ничего не прочиталось».

    Первое чинят путём к каталогу, второе — парсером. Раньше кнопка на обоих
    отвечала «выгрузок не нашлось», и владелец шёл искать каталог, который
    был на месте.
    """
    from orbit import importer
    from orbit.sections import base, partners

    (tmp_path / "a.csv").write_text("name\nФирма\n", encoding="utf-8")
    monkeypatch.setattr(
        importer, "import_file",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("побит")),
    )
    monkeypatch.setattr(
        base, "get_settings", lambda: type("S", (), {"leads_path": tmp_path.parent})()
    )
    (tmp_path.parent / "output").mkdir(exist_ok=True)
    (tmp_path.parent / "output" / "a.csv").write_text("name\nФирма\n", encoding="utf-8")

    screen = base.do_import(partners.TAB, store)
    assert "не прочиталась" in screen.alert.lower(), screen.alert
    assert "a.csv" in screen.alert


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("Москва, Остаповский проезд, 6 ст1", "Москва"),
        ("Вешки (Мытищи городской округ), Липкинское шоссе, 2", "Вешки"),
        ("Немчиновка (Одинцовский городской округ), улица Связи, 1", "Немчиновка"),
        ("", None),
    ],
)
def test_city_is_cut_at_the_bracket_too(address, expected):
    """Город уходит в промт письма как факт о компании.

    У подмосковных адресов 2ГИС пишет «Вешки (Мытищи городской округ, …», и
    в город попадал кусок с незакрытой скобкой — а оттуда прямо в текст
    письма клиенту.
    """
    from orbit.importer import _city_from

    assert _city_from(address) == expected

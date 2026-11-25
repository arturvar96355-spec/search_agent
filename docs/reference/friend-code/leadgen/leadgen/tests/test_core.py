"""Основные кейсы: нормализация телефонов, соцсети, дедуп, отзывы, экспорт, store."""
from __future__ import annotations

import json

import pytest

from parser.contacts import (
    classify_email,
    detect_dev_level,
    detect_niches,
    detect_red_flags,
    extract_emails,
    extract_phones,
    extract_telegram,
    looks_like_big_agency,
)
from parser.dedup import deduplicate, deduplicate_agencies
from parser.export import AGENCY_COLUMNS, COLUMNS, agencies_to_dataframe, to_dataframe
from parser.models import Email, Organization, extract_socials, normalize_domain, normalize_phone
from parser.providers.catalogs import parse_catalog
from parser.reviews import categories, find_complaints, scan_reviews
from parser.store import JsonlStore

from .conftest import CITY_SLUGS, ZWSP, _parse_search_card, resolve_source


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+7 (495) 123-45-67", "+74951234567"),
        ("8 800 555 35 35", "+78005553535"),
        ("495 123 45 67", "+74951234567"),  # без кода страны
        ("+7-999-000-11-22", "+79990001122"),
        ("8(495)1112233", "+74951112233"),
    ],
)
def test_normalize_phone_valid(raw, expected):
    assert normalize_phone(raw) == expected


def test_extract_socials_basic():
    text = "Пишите https://t.me/best_clinic или vk.com/clinicmsk, инста instagram.com/clinic.msk/"
    socials = extract_socials(text)
    assert socials["telegram"] == "https://t.me/best_clinic"
    assert socials["vk"] == "vk.com/clinicmsk"
    assert socials["instagram"] == "instagram.com/clinic.msk"


def test_organization_phone_normalized_and_deduped():
    org = Organization(
        name="Клиника",
        phone=["8 (495) 111-22-33", "+7 495 111 22 33", "мусор"],
        source="2gis",
    )
    # два формата одного номера схлопнулись, мусор отброшен
    assert org.phone == ["+74951112233"]
    assert org.primary_phone == "+74951112233"


def test_dedup_by_phone_merges_fields():
    orgs = [
        Organization(name="Клиника А", phone=["+7 495 111-22-33"], rating=4.5, source="2gis"),
        Organization(name="Клиника А (Я)", phone=["8(495)1112233"], website="https://a.ru", source="yandex"),
    ]
    result = deduplicate(orgs)
    assert len(result) == 1
    merged = result[0]
    assert merged.website == "https://a.ru"  # добрали из второго источника
    assert merged.rating == 4.5  # осталось из первого
    assert merged.source == "2gis"  # приоритет базовой записи


def test_dedup_by_name_address_when_no_phone():
    orgs = [
        Organization(name="Автосервис Б", address="Москва,  Тверская 5", source="2gis"),
        Organization(name="автосервис б", address="москва, тверская 5",
                     website="https://b.ru", source="yandex"),
    ]
    result = deduplicate(orgs)
    assert len(result) == 1
    assert result[0].website == "https://b.ru"


def test_dedup_keeps_distinct():
    orgs = [
        Organization(name="Кафе В", address="пр. Мира, 10", source="2gis"),
        Organization(name="Кафе Г", address="пр. Мира, 12", source="2gis"),
    ]
    assert len(deduplicate(orgs)) == 2


def test_reviews_find_complaints_detects_categories(sample_reviews):
    assert find_complaints(sample_reviews[0])[0][0] == "no_answer"
    assert find_complaints(sample_reviews[1])[0][0] == "slow_reply"
    assert find_complaints(sample_reviews[2]) == []  # хвалебный отзыв — чисто
    assert categories(sample_reviews) == {"no_answer", "slow_reply"}


def test_scan_reviews_unique_quotes(sample_reviews):
    quotes = scan_reviews(sample_reviews)
    assert len(quotes) == 3  # три отзыва с жалобами, четвёртый чистый
    assert len(quotes) == len(set(quotes))  # без дублей


def test_export_columns_and_values(make_org):
    org = make_org(name="Тест", phone=["+7 495 111-22-33"], review_complaints=["не дозвониться"])
    df = to_dataframe([org])
    assert list(df.columns) == COLUMNS
    assert df.loc[0, "phone"] == "+74951112233"
    assert df.loc[0, "review_complaints"] == "не дозвониться"


def test_store_roundtrip(tmp_path, make_org):
    store = JsonlStore(tmp_path / "run.jsonl")
    orgs = [make_org(name="А", phone=["+74951112233"]), make_org(name="Б")]
    store.append_query("2gis", "стоматология", "Москва", orgs)

    assert store.completed_queries() == {("2gis", "стоматология", "Москва")}
    loaded = store.load_orgs()
    assert [o.name for o in loaded] == ["А", "Б"]
    assert loaded[0].phone == ["+74951112233"]


# --- режим partners: модель Agency и дедуп по домену --------------------------

def test_normalize_domain_strips_scheme_www_and_path():
    assert normalize_domain("https://WWW.Agency.RU/about/") == "agency.ru"
    assert normalize_domain("agency.ru") == "agency.ru"
    assert normalize_domain("http://sub.agency.ru:8080/x?y=1") == "sub.agency.ru"


def test_agency_website_normalized_and_url_property(make_agency):
    agency = make_agency(website="https://WWW.Agency.RU/contacts")
    assert agency.website == "agency.ru"  # ключ дедупа — чистый домен
    assert agency.url == "https://agency.ru"  # для захода на сайт


def test_agency_best_email_priority(make_agency):
    agency = make_agency(
        emails=[
            Email(address="info@agency.ru", kind="general"),
            Email(address="sales@agency.ru", kind="sales"),
            Email(address="partner@agency.ru", kind="partner"),
        ]
    )
    assert agency.best_email == "partner@agency.ru"  # partner > personal > sales > general
    assert agency.email_found is True


def test_agency_without_email_marks_not_found(make_agency):
    agency = make_agency(phones=["+7 495 111-22-33"])
    assert agency.email_found is False  # ничего не выдумываем
    assert agency.best_email is None
    assert agency.phones == ["+74951112233"]  # телефон нормализован


def test_deduplicate_agencies_by_domain(make_agency):
    first = make_agency(name="Агентство", website="https://www.agency.ru/",
                        emails=[Email(address="info@agency.ru")])
    second = make_agency(
        name="Агентство (каталог 2)",
        website="agency.ru",
        emails=[Email(address="partner@agency.ru", kind="partner")],
        city="Москва",
        niche_tags=["стоматология"],
    )
    result = deduplicate_agencies([first, second])

    assert len(result) == 1  # схлопнули по домену
    merged = result[0]
    assert merged.name == "Агентство"  # приоритет у первой записи
    assert merged.city == "Москва"  # пустое поле добрали из второй
    assert {e.address for e in merged.emails} == {"info@agency.ru", "partner@agency.ru"}
    assert merged.best_email == "partner@agency.ru"
    assert merged.niche_tags == ["стоматология"]


# --- режим partners: контакты и метки с сайта ---------------------------------

def test_extract_emails_from_text_and_mailto():
    text = "Пишите на info@agency.ru или partner@agency.ru"
    hrefs = ["mailto:Sales@Agency.ru?subject=привет", "https://agency.ru/about"]
    emails = extract_emails(text, hrefs)
    addresses = {e.address for e in emails}
    assert addresses == {"info@agency.ru", "partner@agency.ru", "sales@agency.ru"}


def test_classify_email_kinds():
    assert classify_email("info@a.ru") == "general"
    assert classify_email("hello@a.ru") == "general"
    assert classify_email("sales@a.ru") == "sales"
    assert classify_email("zayavka@a.ru") == "sales"
    assert classify_email("partner@a.ru") == "partner"
    assert classify_email("ref@a.ru") == "partner"
    assert classify_email("ivan.petrov@a.ru") == "personal"


def test_extract_phones_and_telegram():
    text = "Тел: +7 (495) 123-45-67, 8 812 000 11 22. Телеграм: @agency_bot"
    assert extract_phones(text) == ["+74951234567", "+78120001122"]
    assert extract_telegram(text, ["https://t.me/agency_msk"]) == [
        "https://t.me/agency_msk",
        "https://t.me/agency_bot",
    ]


def test_detect_niches_and_red_flags():
    text = "Мы делаем маркетинг для стоматологий и автосервисов. Настраиваем Яндекс Директ и таргет."
    assert set(detect_niches(text)) == {"стоматология", "авто"}
    assert detect_red_flags(text) == []  # обычное трафиковое агентство — флагов нет

    competitor = "Разработка CRM, свои боты и AI-агенты под ключ, всё инхаус"
    assert "конкурент" in detect_red_flags(competitor)

    media = "ТВ-реклама, наружная реклама и медиабаинг"
    assert "медийный" in detect_red_flags(media)


# --- режим partners: источники-каталоги --------------------------------------

def test_parse_ruward_listing():
    html = """<html><body>
      <a class="companyratingname" href="https://dot.ru/?utm_source=ruward">DOT</a>
      <a class="companyratingname" href="https://double24.ru/">Double</a>
      <a class="companyratingname" href="https://ruward.ru/inner">Каталог (не агентство)</a>
      <a href="https://other.ru">Без класса</a>
    </body></html>"""
    agencies = parse_catalog("https://ruward.ru/smm/region-moscow/", html, city="Москва")
    assert [a.website for a in agencies] == ["dot.ru", "double24.ru"]  # ссылку каталога отбросили
    assert agencies[0].name == "DOT"
    assert agencies[0].source == "ruward"
    assert agencies[0].city == "Москва"


def test_parse_ratingruneta_from_initial_state():
    state = {
        "rating_page_new": {"page": {"rating": {"rating_tables": [{"rows": [
            {"name": "Агентство А", "link": "https://a-agency.ru/?erid=X", "place": "1",
             "segment_name": "Нижний ценовой сегмент", "cases_cnt": "3"},
            {"name": "Без сайта", "link": ""},
            {"name": None, "link": "https://nope.ru"},
        ]}]}}}
    }
    html = ("<html><body><script>window.__INITIAL_STATE__ = "
            + json.dumps(state, ensure_ascii=False) + ";</script></body></html>")
    agencies = parse_catalog("https://ratingruneta.ru/smm/moscow/", html, city="Москва")
    assert len(agencies) == 1  # запись без сайта и без имени пропущены
    assert agencies[0].website == "a-agency.ru"
    assert agencies[0].source == "ratingruneta"
    assert "место 1" in agencies[0].catalog_info
    assert "Нижний ценовой сегмент" in agencies[0].catalog_info


def test_agency_export_columns_and_not_found_mark(make_agency):
    with_email = make_agency(name="С почтой", emails=[Email(address="partner@a.ru", kind="partner")])
    without = make_agency(name="Без почты", website="b.ru", phones=["+7 495 111-22-33"])
    df = agencies_to_dataframe([with_email, without])
    assert list(df.columns) == AGENCY_COLUMNS
    assert df.loc[0, "best_email"] == "partner@a.ru"
    assert df.loc[0, "email_found"] == "да"
    assert df.loc[1, "email_found"] == "не найдено"  # явная метка, а не пустая ячейка
    assert df.loc[1, "phones"] == "+74951112233"


# --- фиксы аудита: классификация email и контакты ------------------------------

def test_classify_email_technical_addresses_are_general():
    # hr/zakupki/press — не personal: иначе они вытесняли настоящие контакты из best_email.
    for address in ("hr@a.ru", "zakupki@a.ru", "press@a.ru"):
        assert classify_email(address) == "general", address
    # noreply — отдельный класс: «не отвечайте» — не контакт вовсе. Как general
    # он побеждал живой info@ в best_email, и письмо уходило в никуда.
    for address in ("noreply@a.ru", "no-reply@a.ru", "donotreply@a.ru", "robot@a.ru"):
        assert classify_email(address) == "noreply", address
    assert classify_email("partnership@a.ru") == "partner"  # суффикс не мешает
    assert classify_email("info_msk@a.ru") == "general"
    assert classify_email("sales2@a.ru") == "sales"
    assert classify_email("ivan.petrov@a.ru") == "personal"


def test_best_email_not_hijacked_by_technical(make_agency):
    agency = make_agency(emails=[
        Email(address="noreply@a.ru", kind=classify_email("noreply@a.ru")),
        Email(address="sales@a.ru", kind=classify_email("sales@a.ru")),
    ])
    assert agency.best_email == "sales@a.ru"


def test_extract_phones_with_dots_and_dashes():
    assert extract_phones("+7.495.123.45.67") == ["+74951234567"]
    assert extract_phones("8–812–000–11–22") == ["+78120001122"]  # типографское тире


def test_extract_telegram_requires_context_for_at_handle():
    assert extract_telegram("мы в инстаграм @agency_insta") == []  # не Telegram — не выдумываем
    assert extract_telegram("телеграм: @agency_tg") == ["https://t.me/agency_tg"]


def test_extract_emails_percent_encoded_mailto():
    assert [e.address for e in extract_emails(None, ["mailto:%69nfo@agency.ru"])] == ["info@agency.ru"]


def test_red_flags_no_false_positives():
    assert detect_red_flags("AI-агентство полного цикла") == []  # агентство != «AI-агенты»
    assert detect_red_flags("продаём свои ботинки") == []
    assert "конкурент" in detect_red_flags("делаем AI-агентов под ключ")


def test_niches_genitive_and_word_boundary():
    assert detect_niches("маркетинг для клиник") == ["клиники"]  # род. мн. без суффикса
    assert detect_niches("лежит в шкафе") == []  # «шкафе» != «кафе»


# --- правила из брифа продаж: уровень разработки определяет питч ---------------

def test_dev_level_matches_sales_brief_examples():
    # Эталоны из брифа: Товарищ (чистый SMM), TrendFox (конструкторы), веб-студия (полный цикл).
    assert detect_dev_level("Ведение соцсетей, таргет, SMM-стратегия, SERM, лидогенерация") == "none"
    assert detect_dev_level(
        "PR и digital. Разработка сайтов на Tilda, WordPress, Bitrix, OpenCart"
    ) == "constructors"
    assert detect_dev_level("Аналитика, дизайн, веб-разработка, ИТ-поддержка") == "full"
    assert detect_dev_level("Собственный отдел разработки, backend на Laravel") == "full"


def test_recommended_pitch_by_dev_level(make_agency):
    assert make_agency(dev_level="none").recommended_pitch == "сайты + CRM"
    assert make_agency(dev_level="constructors").recommended_pitch == "только CRM"
    assert make_agency(dev_level="full").recommended_pitch == "пропустить: своя разработка"
    assert make_agency(big_agency=True, dev_level="none").recommended_pitch == "пропустить: крупный сетевой"


def test_is_target_excludes_big_and_full_dev(make_agency):
    assert make_agency(dev_level="none").is_target is True
    assert make_agency(dev_level="constructors").is_target is True
    assert make_agency(dev_level="full").is_target is False  # свой продакшн — не наш субподряд
    assert make_agency(big_agency=True).is_target is False


def test_big_agency_detection_from_brief_stoplist():
    # Бриф: «Топ рейтинга не трогать — Nectarin, SETTERS, Kokoc Group, Demis Group, Ingate».
    for name in ("Kokoc Group", "SETTERS", "Demis Group", "Ingate", "Nectarin"):
        assert looks_like_big_agency(name) is True, name
    assert looks_like_big_agency("Небольшое агентство") is False
    assert looks_like_big_agency("X", "место 3 · Нижний ценовой сегмент") is True  # верх рейтинга
    assert looks_like_big_agency("X", "место 25 · Нижний ценовой сегмент") is False


def test_sort_for_outreach_puts_targets_first(make_agency):
    from parser.partners import sort_for_outreach
    ranked = sort_for_outreach([
        make_agency(name="Крупняк", website="big.ru", big_agency=True),
        make_agency(name="Свой продакшн", website="dev.ru", dev_level="full"),
        make_agency(name="Конструкторы", website="tilda.ru", dev_level="constructors"),
        make_agency(name="Чистый SMM", website="smm.ru", dev_level="none",
                    emails=[Email(address="info@smm.ru")]),
    ])
    assert [a.name for a in ranked][:2] == ["Чистый SMM", "Конструкторы"]
    assert [a.name for a in ranked][-2:] == ["Крупняк", "Свой продакшн"]


# --- пул прокси ---------------------------------------------------------------

def test_proxy_pool_rotation_and_failures():
    from parser.providers.browser import ProxyPool
    pool = ProxyPool(["http://u:p@a.ru:1", "http://u:p@b.ru:2"])
    assert len(pool) == 2
    first = pool.current
    pool.mark_failure()
    second = pool.rotate()
    assert second != first  # переключились на другой канал
    assert pool.rotate() == first  # по кругу
    assert sum(pool.failures().values()) == 1


def test_proxy_pool_single_and_empty():
    from parser.providers.browser import ProxyPool
    single = ProxyPool(["http://u:p@a.ru:1"])
    assert single.rotate() == single.current  # некуда переключаться — остаёмся
    empty = ProxyPool([])
    assert len(empty) == 0 and empty.current is None and empty.rotate() is None


def test_settings_proxy_list_parses_multiple():
    from parser.config import Settings
    s = Settings(proxy_url="http://u:p@a.ru:1, http://u:p@b.ru:2\nhttp://u:p@c.ru:3")
    assert len(s.proxy_list()) == 3
    assert Settings(proxy_url=None).proxy_list() == []


# --- браузерный поиск в вебе 2ГИС (работает без API-ключа) --------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        # Карточка выдачи: блоки разделены zero-width space, рейтинг слипается с числом оценок.
        (f"Илатан Сеть клиник4.8139 оценок{ZWSP}Марксистский переулок, 3, Москва{ZWSP}3 филиала",
         (4.8, 139, "Марксистский переулок, 3, Москва")),
        # Номерная улица: «1-й» раньше обрезался до «й».
        (f"Клиника5.0376 оценок{ZWSP}1-й Колобовский переулок, 4, Москва",
         (5.0, 376, "1-й Колобовский переулок, 4, Москва")),
        # Тот же текст без разделителей — запасной путь через регэксп.
        ("Илатан Сеть клиник4.8139 оценок Марксистский переулок, 3, Москва 3 филиала",
         (4.8, 139, "Марксистский переулок, 3, Москва")),
        # Обратный порядок слов: тип улицы впереди названия.
        ("Дента улица Ленина, 12, Казань", (None, None, "улица Ленина, 12, Казань")),
        ("Стома4.5175 оценок Грайвороновская улица, 6 ст1, Москва",
         (4.5, 175, "Грайвороновская улица, 6 ст1, Москва")),
        ("Мед5.012 оценок Хорошёвское шоссе, 48, Москва", (5.0, 12, "Хорошёвское шоссе, 48, Москва")),
    ],
)
def test_parse_search_card(text, expected):
    assert _parse_search_card(text) == expected


def test_parse_search_card_invents_nothing():
    """Чего в карточке нет — остаётся None: пустых адресов и нулевых рейтингов не выдумываем."""
    assert _parse_search_card("Без адреса и рейтинга вообще") == (None, None, None)
    assert _parse_search_card("") == (None, None, None)
    # Станция метро — не адрес: нет ни типа улицы, ни номера дома.
    assert _parse_search_card(f"Салон4.212 оценок{ZWSP}метро Тверская")[2] is None


def test_city_slugs_cover_bot_cities():
    """Каждый город из меню бота должен иметь слаг, иначе браузерный поиск молча вернёт ноль."""
    from parser.niches import CITIES

    # CITIES — {код кнопки: название города}; слаги заведены по названиям.
    missing = [name for name in CITIES.values() if name.strip().lower() not in CITY_SLUGS]
    assert not missing, f"нет слага в browser.CITY_SLUGS: {missing}"


def test_resolve_source_falls_back_to_browser_without_key():
    from parser.config import Settings

    assert resolve_source("auto", Settings(dgis_api_key=None)) == "browser"
    assert resolve_source("auto", Settings(dgis_api_key="k")) == "2gis"
    # Явный выбор источника auto не переопределяет.
    assert resolve_source("2gis", Settings(dgis_api_key=None)) == "2gis"


def test_resolve_source_switches_when_key_blocked(monkeypatch):
    """Ключ мог выгореть посреди прогона — auto обязан уйти на браузер, а не отдавать ноль."""
    from parser import pipeline
    from parser.config import Settings

    monkeypatch.setattr(pipeline, "dgis_key_blocked", lambda: True)
    assert pipeline.resolve_source("auto", Settings(dgis_api_key="k")) == "browser"


# --- outreach: рассылка по партнёрам ------------------------------------------

def _target(**overrides):
    from parser.outreach import Target

    data = dict(name="Тест-Агентство", domain="agency.ru", email="info@agency.ru",
                pitch="сайты + CRM", niche_tags="", partner_page_url="")
    data.update(overrides)
    return Target(**data)


def test_outreach_letter_has_required_parts():
    """В каждом первом письме: имя, отписка, подпись — и НЕТ цены (правило брифа)."""
    from parser.outreach import render_letter

    letter = render_letter(_target())
    assert "Тест-Агентство" in letter.subject or "Тест-Агентство" in letter.body
    assert "стоп" in letter.body
    assert "uniqore.ru" in letter.body
    for forbidden in ("₽", "руб", "$", "абонплат"):
        assert forbidden not in letter.body.lower()


def test_outreach_templates_rotate():
    from parser.outreach import render_letter

    first = render_letter(_target(), template_index=0)
    second = render_letter(_target(), template_index=1)
    assert first.subject != second.subject  # соседние письма не под копирку


def test_outreach_personalization():
    from parser.outreach import render_letter

    letter = render_letter(_target(niche_tags="HoReCa, недвижимость", partner_page_url="https://a.ru/partners"))
    assert "HoReCa" in letter.body and "недвижимость" in letter.body
    assert "партнёров" in letter.body
    # «универсальное» — это отсутствие ниши, в письмо не попадает
    plain = render_letter(_target(niche_tags="универсальное"))
    assert "универсальное" not in plain.body


def test_outreach_journal_no_repeat_and_daily_limit(tmp_path):
    from datetime import datetime

    from parser.outreach import Journal, render_letter

    journal = Journal(tmp_path / "sent.json")
    letter = render_letter(_target())
    journal.mark_sent(letter, datetime(2026, 7, 30, 12, 0))
    assert journal.already_sent("agency.ru")
    assert not journal.already_sent("other.ru")
    assert journal.sent_today(datetime(2026, 7, 30).date()) == 1
    assert journal.sent_today(datetime(2026, 7, 31).date()) == 0
    # журнал переживает перезапуск процесса
    assert Journal(tmp_path / "sent.json").already_sent("agency.ru")


def test_outreach_business_days():
    from datetime import date

    from parser.outreach import business_days_between

    # чт 30.07.2026 -> вт 04.08: пт, пн, вт = 3 рабочих дня (сб/вс не считаются)
    assert business_days_between(date(2026, 7, 30), date(2026, 8, 4)) == 3
    assert business_days_between(date(2026, 7, 30), date(2026, 7, 31)) == 1
    assert business_days_between(date(2026, 7, 30), date(2026, 7, 30)) == 0


def test_outreach_followup_due_logic(tmp_path):
    from datetime import date, datetime

    from parser.outreach import Journal, render_letter

    journal = Journal(tmp_path / "sent.json")
    journal.mark_sent(render_letter(_target()), datetime(2026, 7, 27, 10, 0))  # пн
    # чт 30.07 — прошло 3 рабочих дня (вт, ср, чт) -> пора
    due = journal.followup_due(date(2026, 7, 30))
    assert [d["domain"] for d in due] == ["agency.ru"]
    # ответили — догон не шлём
    journal._data["agency.ru"]["replied"] = True
    assert journal.followup_due(date(2026, 7, 30)) == []


def test_outreach_run_dry_run_sends_nothing(tmp_path, monkeypatch):
    """Dry-run: письма готовятся, журнал не трогается, send_fn не вызывается."""
    import pandas as pd

    from parser import outreach as om
    from parser.config import Settings

    xlsx = tmp_path / "agents.xlsx"
    pd.DataFrame([
        {"name": "А", "website": "a.ru", "pitch": "сайты + CRM", "best_email": "info@a.ru",
         "niche_tags": "", "partner_page_url": ""},
        {"name": "Б", "website": "b.ru", "pitch": "только CRM", "best_email": "sales@b.ru",
         "niche_tags": "", "partner_page_url": ""},
        {"name": "В", "website": "c.ru", "pitch": "пропустить: своя разработка",
         "best_email": "info@c.ru", "niche_tags": "", "partner_page_url": ""},
        {"name": "Г", "website": "d.ru", "pitch": "сайты + CRM", "best_email": "vasya@gmail.com",
         "niche_tags": "", "partner_page_url": ""},
    ]).to_excel(xlsx, index=False)

    called = []
    result = om.run_outreach(
        xlsx, Settings(output_dir=tmp_path), send=False,
        journal_path=tmp_path / "sent.json",
        send_fn=lambda s, letter: called.append(letter),
    )
    # В (не тот питч) и Г (личная почта) отсеяны; ничего не отправлено и не записано
    assert [item.target.email for item in result.prepared] == ["info@a.ru", "sales@b.ru"]
    assert result.dry_run and result.sent == 0 and called == []
    assert not (tmp_path / "sent.json").exists()


def test_outreach_run_send_respects_journal_and_limit(tmp_path):
    from datetime import datetime

    import pandas as pd

    from parser import outreach as om
    from parser.config import Settings

    xlsx = tmp_path / "agents.xlsx"
    pd.DataFrame([
        {"name": f"А{i}", "website": f"a{i}.ru", "pitch": "сайты + CRM",
         "best_email": f"info@a{i}.ru", "niche_tags": "", "partner_page_url": ""}
        for i in range(5)
    ]).to_excel(xlsx, index=False)

    settings = Settings(output_dir=tmp_path, smtp_host="smtp.test", smtp_user="u@test",
                        smtp_password="x", outreach_daily_limit=3)
    sent = []
    result = om.run_outreach(
        xlsx, settings, send=True, journal_path=tmp_path / "sent.json",
        send_fn=lambda s, letter: sent.append(letter.target.domain),
        sleep_fn=lambda seconds: None, now=datetime(2026, 7, 30, 12, 0),
    )
    assert result.sent == 3 and result.limited == 2  # дневной лимит
    # повторный запуск в тот же день: лимит выбран, ничего не уходит
    result2 = om.run_outreach(
        xlsx, settings, send=True, journal_path=tmp_path / "sent.json",
        send_fn=lambda s, letter: sent.append(letter.target.domain),
        sleep_fn=lambda seconds: None, now=datetime(2026, 7, 30, 18, 0),
    )
    assert result2.sent == 0 and result2.skipped_already == 3
    # назавтра доезжают оставшиеся два — и только они (журнал помнит первых трёх)
    result3 = om.run_outreach(
        xlsx, settings, send=True, journal_path=tmp_path / "sent.json",
        send_fn=lambda s, letter: sent.append(letter.target.domain),
        sleep_fn=lambda seconds: None, now=datetime(2026, 7, 31, 12, 0),
    )
    assert result3.sent == 2 and sorted(sent) == [f"a{i}.ru" for i in range(5)]


def test_outreach_send_without_smtp_fails_loudly(tmp_path):
    import pandas as pd
    import pytest as _pytest

    from parser import outreach as om
    from parser.config import Settings

    xlsx = tmp_path / "agents.xlsx"
    pd.DataFrame([{"name": "А", "website": "a.ru", "pitch": "сайты + CRM",
                   "best_email": "info@a.ru", "niche_tags": "",
                   "partner_page_url": ""}]).to_excel(xlsx, index=False)
    with _pytest.raises(RuntimeError, match="SMTP"):
        om.run_outreach(xlsx, Settings(output_dir=tmp_path), send=True,
                        journal_path=tmp_path / "sent.json")


def test_placeholder_email_is_skipped():
    """«example@email.com» из подсказки формы — не контакт агентства.

    Такое письмо однажды ушло по-настоящему: адрес выглядел живым, а стоял
    в placeholder формы обратной связи.
    """
    from parser.contacts import extract_emails

    emails = extract_emails(
        "Пишите: info@agency.ru. Пример: example@email.com, your-email@site.ru"
    )
    assert [e.address for e in emails] == ["info@agency.ru"]


def test_unwritten_lead_is_not_silently_lost(tmp_path, monkeypatch):
    """Лид, не легший на диск, должен попасть в отдельный список, а не в тишину.

    Сбой записи проглатывался одной строкой в логе, а метод возвращал «лид
    добавлен». Снаружи это неотличимо от нормального сбора: бот честно
    рапортует «нашёл 40 компаний», а на диске их нет — после перезапуска
    работа за вечер пропадает целиком.
    """
    from parser.leadbase import LeadBase
    from parser.models import Organization

    base = LeadBase(tmp_path / "leads.json")
    org = Organization(name="Стоматология «Улыбка»", source="2gis",
                       source_url="https://2gis.ru/firm/1")

    def disk_is_full(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr("builtins.open", disk_is_full)
    assert base.add(org) is True          # в памяти он есть и до человека доедет
    assert base.unsaved == ["Стоматология «Улыбка»"]


def test_saved_lead_leaves_the_unsaved_list_empty(tmp_path):
    from parser.leadbase import LeadBase
    from parser.models import Organization

    base = LeadBase(tmp_path / "leads.json")
    base.add(Organization(name="Клиника", source="2gis", source_url="https://2gis.ru/firm/2"))
    assert base.unsaved == []
    assert (tmp_path / "leads.json").exists()


def test_noreply_never_beats_a_live_mailbox(make_agency):
    """noreply@ берётся, только когда другого адреса нет вовсе."""
    both = make_agency(emails=[
        Email(address="noreply@a.ru", kind=classify_email("noreply@a.ru")),
        Email(address="info@a.ru", kind=classify_email("info@a.ru")),
    ])
    assert both.best_email == "info@a.ru"

    only = make_agency(emails=[Email(address="noreply@a.ru", kind=classify_email("noreply@a.ru"))])
    assert only.best_email == "noreply@a.ru"


def test_merge_keeps_facts_regardless_of_order(make_agency):
    """big_agency и dev_level не должны теряться от порядка слияния дублей."""
    from parser.dedup import deduplicate_agencies

    knowing = make_agency(website="x.ru", big_agency=True, dev_level="full")
    catalog = make_agency(website="x.ru", phones=["+74951112233"])
    for pair in ([knowing, catalog], [catalog, knowing]):
        merged = deduplicate_agencies(pair)[0]
        assert merged.big_agency is True
        assert merged.dev_level == "full"


def test_share_php_is_not_a_social_account():
    """Кнопка «поделиться» — не аккаунт агентства."""
    from parser.models import extract_socials

    assert extract_socials("https://vk.com/share.php?url=x") == {}
    assert "vk" in extract_socials("https://vk.com/agency_hq")


def test_the_export_carries_what_the_owner_paid_for():
    """Форма юрлица, год регистрации и приоритет обязаны быть в таблице.

    Первые два приезжают из ДаData вместе с ИНН — за них уже заплачено, и по
    ним же считается платёжеспособность. Третий — порядок строк, который
    пропадает от первой же сортировки в Excel, хотя подпись к файлу обещает
    «по убыванию приоритета».
    """
    from parser.export import COLUMNS, to_dataframe
    from parser.models import Organization

    org = Organization(
        source="2gis", name="Стоматология", address="Москва, ул. Ленина, 1",
        phone=["+7 900 000-00-00"], inn="7700000000", entity_type="ООО",
        registered_year=2012, review_complaints=["не берут трубку", "не перезвонили"],
    )
    frame = to_dataframe([org])

    for column in ("entity_type", "registered_year", "priority"):
        assert column in COLUMNS, column
    row = frame.iloc[0]
    assert row["entity_type"] == "ООО"
    assert row["registered_year"] == 2012
    assert row["priority"] > 0


# --- Как контакты выглядят на живых сайтах ------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Работаем с 9:00 до 21:00, тел 79161234567", ["+79161234567"]),
        ("tel:79161234567", ["+79161234567"]),
        ("+7 (495) 123-45-67", ["+74951234567"]),
        ("8 800 555 35 35", ["+78005553535"]),
        # Не телефоны: у ИНН, ОГРН и номера заказа цифр столько же или больше,
        # но границы слова в конце не сойдутся.
        ("ИНН 7707083893 КПП 770701001", []),
        ("ИНН 770708389312", []),
        ("ОГРН 1027700132195", []),
        ("заказ №1234567890", []),
    ],
)
def test_phones_are_found_the_way_sites_write_them(text, expected):
    from parser.contacts import extract_phones

    assert extract_phones(text) == expected


def test_a_cyrillic_address_is_a_real_address():
    """«почта@компания.рф» — обычный рабочий адрес, а не мусор.

    Латинская проверка не находила его вовсе, и у такой компании контактов не
    оказывалось совсем. В Orbit это уже чинили — здесь оставалось.
    """
    from parser.contacts import extract_emails

    found = [item.address for item in extract_emails("Пишите: info@фирма.рф")]
    assert found == ["info@фирма.рф"]


def test_a_file_name_is_not_an_address():
    from parser.contacts import extract_emails

    assert extract_emails("logo@2x.png doc@v2.pdf") == []

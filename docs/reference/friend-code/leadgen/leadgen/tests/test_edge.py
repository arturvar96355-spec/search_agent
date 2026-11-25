"""Краевые случаи: мусорные телефоны, пустые входы, перекрытия отзывов, битый .jsonl."""
from __future__ import annotations

import pytest

from parser.contacts import detect_niches, detect_red_flags, extract_emails, extract_phones, extract_telegram
from parser.dedup import deduplicate, deduplicate_agencies
from parser.models import Agency, Email, Organization, extract_socials, normalize_domain, normalize_phone
from parser.providers.catalogs import parse_catalog
from parser.reviews import find_complaints, scan_reviews
from parser.store import JsonlStore


@pytest.mark.parametrize("raw", ["", "12-34", "abc", "+1 202 555 0143", "7999", "8 800 555 35 35 12"])
def test_normalize_phone_rejects_non_rf(raw):
    assert normalize_phone(raw) is None


def test_extract_socials_ignores_service_handles():
    # t.me/share — служебный путь, не аккаунт
    assert "telegram" not in extract_socials("ссылка https://t.me/share/url?u=...")


def test_extract_socials_empty_input():
    assert extract_socials(None) == {}
    assert extract_socials("") == {}


def test_organization_accepts_single_phone_string():
    org = Organization(name="X", phone="+7 (495) 000-00-00", source="2gis")
    assert org.phone == ["+74950000000"]


def test_organization_empty_phone_list_default():
    org = Organization(name="X", source="2gis")
    assert org.phone == []
    assert org.primary_phone is None


def test_dedup_empty_input():
    assert deduplicate([]) == []


def test_dedup_no_phone_no_address_collapses_by_name():
    orgs = [
        Organization(name="Ромашка", source="2gis"),
        Organization(name="ромашка", website="https://r.ru", source="yandex"),
    ]
    result = deduplicate(orgs)
    assert len(result) == 1
    assert result[0].website == "https://r.ru"


def test_reviews_overlapping_patterns_merge_to_one_quote():
    # два сигнала в одном фрагменте -> одна цитата, а не две почти одинаковых
    text = "не дозвониться, и трубку не берут совсем"
    complaints = find_complaints(text)
    assert len(complaints) == 1


def test_reviews_empty_and_none():
    assert find_complaints(None) == []
    assert find_complaints("") == []
    assert scan_reviews([]) == []


def test_store_skips_corrupted_line(tmp_path):
    path = tmp_path / "run.jsonl"
    good = ('{"query": {"source": "2gis", "rubric": "р", "city": "г"}, '
            '"org": {"name": "Ок", "source": "2gis"}}')
    path.write_text(good + "\n{битая строка не json\n", encoding="utf-8")
    store = JsonlStore(path)
    loaded = store.load_orgs()
    assert [o.name for o in loaded] == ["Ок"]  # битую строку пропустили, валидную взяли


def test_store_missing_file_is_empty(tmp_path):
    store = JsonlStore(tmp_path / "nope.jsonl")
    assert store.completed_queries() == set()
    assert store.load_orgs() == []


# --- режим partners: краевые случаи ------------------------------------------

def test_normalize_domain_on_garbage():
    assert normalize_domain(None) is None
    assert normalize_domain("") is None
    assert normalize_domain("   ") is None


def test_agency_empty_website_falls_back_to_name_key(make_agency):
    # Без домена ключ дедупа — имя: две записи с одинаковым именем схлопнутся.
    a = make_agency(name="Без сайта", website=None, phones=["+74951112233"])
    b = make_agency(name="без сайта", website=None, telegram=["https://t.me/agency"])
    result = deduplicate_agencies([a, b])
    assert len(result) == 1
    assert result[0].phones == ["+74951112233"]
    assert result[0].telegram == ["https://t.me/agency"]


def test_agency_different_domains_not_merged(make_agency):
    result = deduplicate_agencies([make_agency(website="a.ru"), make_agency(website="b.ru")])
    assert len(result) == 2  # разные домены — разные агентства


def test_agency_merge_keeps_partner_page_and_flags(make_agency):
    a = make_agency(website="agency.ru", red_flags=["конкурент"])
    b = make_agency(
        website="agency.ru",
        has_partner_page=True,
        partner_page_url="https://agency.ru/partners",
        red_flags=["конкурент", "медийный"],
    )
    merged = deduplicate_agencies([a, b])[0]
    assert merged.has_partner_page is True
    assert merged.partner_page_url == "https://agency.ru/partners"
    assert merged.red_flags == ["конкурент", "медийный"]  # без дублей, порядок сохранён


def test_agency_phone_dedup_and_invalid_dropped(make_agency):
    agency = make_agency(phones=["+7 495 111-22-33", "84951112233", "не телефон"])
    assert agency.phones == ["+74951112233"]  # дубли схлопнуты, мусор отброшен


def test_deduplicate_organizations_still_works(make_org):
    # Регресс: клиентский режим не сломан после параметризации дедупа.
    orgs = [make_org(name="А", phone=["+74951112233"]), make_org(name="А дубль", phone=["+7 495 111 22 33"])]
    assert len(deduplicate(orgs)) == 1


# --- режим partners: краевые случаи контактов --------------------------------

def test_extract_emails_ignores_image_files_and_empty():
    assert extract_emails(None) == []
    assert extract_emails("логотип: logo@2x.png, иконка: sprite@3x.svg") == []


def test_extract_telegram_does_not_catch_email_handles():
    # @ внутри email не должен попасть в Telegram.
    assert extract_telegram("напишите ivan@agency.ru") == []


def test_pr_flag_only_without_traffic_words():
    pr_only = "Управление репутацией, media relations и GR для брендов"
    assert "PR" in detect_red_flags(pr_only)

    pr_with_traffic = "Репутация, а также SEO и настройка Яндекс Директ"
    assert "PR" not in detect_red_flags(pr_with_traffic)  # трафик есть -> не чистый PR


def test_detect_niches_empty_text():
    assert detect_niches("") == []
    assert detect_niches(None) == []


def test_extract_phones_rejects_non_russian():
    assert extract_phones("+1 202 555 0143") == []
    assert extract_phones("") == []


def test_parse_catalog_unknown_domain_returns_empty():
    assert parse_catalog("https://example.com/list/", "<html></html>") == []


def test_parse_ratingruneta_without_state_is_empty():
    assert parse_catalog("https://ratingruneta.ru/smm/moscow/",
                         "<html><body>нет состояния</body></html>") == []


def test_parse_ratingruneta_broken_state_is_empty():
    html = "<html><script>window.__INITIAL_STATE__ = {это не json;</script></html>"
    assert parse_catalog("https://ratingruneta.ru/smm/moscow/", html) == []


def test_parse_ruward_skips_social_and_catalog_links():
    html = """<html><body>
      <a class="companyratingname" href="https://t.me/agency">TG</a>
      <a class="companyratingname" href="https://vk.com/agency">VK</a>
      <a class="companyratingname" href="/relative/path">Относительная</a>
    </body></html>"""
    assert parse_catalog("https://ruward.ru/smm/region-moscow/", html) == []


def test_agency_key_anonymous_records_not_merged():
    # Без домена и без имени схлопывать нечем — записи должны остаться раздельными.
    a = Agency(name=" ", website=None, phones=["+74951112233"])
    b = Agency(name="", website=None, phones=["+78120001122"])
    assert len(deduplicate_agencies([a, b])) == 2


def test_site_enrich_keeps_preexisting_emails(make_agency):
    # Email, пришедший из каталога, не должен потеряться при обходе сайта.
    agency = make_agency(emails=[Email(address="known@agency.ru", kind="general")])
    # Симулируем инициализацию словаря, как в enrich_agency.
    start = {e.address: e for e in agency.emails}
    assert "known@agency.ru" in start


def test_normalize_domain_idn_and_trailing_dot():
    assert normalize_domain("https://клиника.рф/") == normalize_domain("xn--80apagcdp.xn--p1ai")
    assert normalize_domain("agency.ru.") == "agency.ru"


def test_agency_merge_keeps_catalog_info(make_agency):
    a = make_agency(website="a.ru", catalog_info=None)
    b = make_agency(website="a.ru", catalog_info="место 1 · Нижний ценовой сегмент")
    merged = deduplicate_agencies([a, b])[0]
    assert merged.catalog_info == "место 1 · Нижний ценовой сегмент"  # метрики каталога не теряем


def test_is_agency_site_host_matching():
    from parser.providers.catalogs import _is_agency_site
    assert _is_agency_site("https://agency.ru/") is True
    assert _is_agency_site("https://t.me/agency") is False
    assert _is_agency_site("https://msk.workspace.ru/x") is False  # поддомен каталога
    assert _is_agency_site("https://linkedin.com/company/x") is False
    assert _is_agency_site("/relative") is False


def test_xlsx_export_survives_control_characters(tmp_path, make_agency):
    # Текст с чужого сайта бывает грязным — экспорт не должен падать целиком.
    from parser.export import agencies_to_xlsx
    agency = make_agency(services_raw="услуги\x0bи\x00мусор")
    path = agencies_to_xlsx([agency], tmp_path / "a.xlsx")
    assert path.exists()


def test_extract_emails_skips_platform_addresses():
    # «Made on Tilda» и виджеты подсовывают свои почты — это не контакты агентства.
    got = [e.address for e in extract_emails("Made on Tilda hello@madeontilda.com, наша: info@agency.ru")]
    assert got == ["info@agency.ru"]


def test_normalize_phone_rejects_template_placeholders():
    # На сайтах-шаблонах стоят «рыбы» вроде +7 999 999 99 99 — это не контакт.
    assert normalize_phone("+7 999 999 99 99") is None
    assert normalize_phone("+70000000000") is None
    assert normalize_phone("+71234567890") is None
    assert normalize_phone("+7 916 503-21-81") == "+79165032181"  # настоящий номер не задет


def test_dgis_page_size_fallback_constant():
    # На демо-ключе page_size максимум 10 (при 50 API отвечает 400) — константа зафиксирована,
    # а в search() есть автопонижение при такой ошибке.
    import inspect

    from parser.providers import dgis
    from parser.providers.dgis import _DEMO_PAGE_SIZE
    assert _DEMO_PAGE_SIZE == 10
    src = inspect.getsource(dgis.DgisProvider.search)
    assert "page_size" in src and "_DEMO_PAGE_SIZE" in src  # фоллбэк на месте


# --- браузерный поиск: мусор в выдаче -----------------------------------------

@pytest.mark.parametrize(
    "name", ["Казань", "3 филиала", "Показать на карте", "Позвонить", "Записаться", "", " "]
)
def test_search_junk_links_rejected(name):
    """Служебные ссылки на /firm/ не должны попадать в базу как организации."""
    from parser.providers.browser import _is_junk_name

    assert _is_junk_name(name, "Казань")


@pytest.mark.parametrize("name", ["Кан Авто", "SK-Auto", "Aвто-M", "Автосеть.РФ"])
def test_search_real_names_kept(name):
    from parser.providers.browser import _is_junk_name

    assert not _is_junk_name(name, "Казань")


def test_search_card_address_with_capitalized_type_and_nbsp():
    """Рекламные карточки идут без разделителей, с заглавным типом улицы и nbsp внутри."""
    from parser.providers.browser import _parse_search_card

    text = "Art-service\xa0Автосервис4.395 оценокУлица Патриса Лумумбы,\xa061, КазаньЛегковой автосервис"
    rating, reviews, address = _parse_search_card(text)
    assert (rating, reviews) == (4.3, 95)
    assert address == "Улица Патриса Лумумбы,\xa061, Казань"


def test_partner_niches_have_both_sources():
    """Ниша без поисковых запросов при --source all молча собирала только каталоги."""
    from parser.partners import CATALOG_URLS
    from parser.providers.search import SEARCH_QUERIES

    missing = sorted(set(CATALOG_URLS) - set(SEARCH_QUERIES))
    assert not missing, f"нет запросов в search.SEARCH_QUERIES для ниш: {missing}"


# --- фиксы финального аудита --------------------------------------------------

def test_browsable_accepts_browser_source(make_org):
    """source='browser' — те же карточки 2ГИС: добор обязан их брать, иначе 0 лидов без ключа."""
    from parser.providers.browser import browsable

    orgs = [
        make_org(source="2gis", source_url="https://2gis.ru/firm/1"),
        make_org(source="browser", source_url="https://2gis.ru/firm/2"),
        make_org(source="yandex", source_url="https://yandex.ru/x"),
        make_org(source="browser", source_url=None),
    ]
    assert [o.source_url for o in browsable(orgs)] == [
        "https://2gis.ru/firm/1", "https://2gis.ru/firm/2",
    ]


def test_outreach_limit_zero_sends_nothing(tmp_path):
    """--limit 0 = пауза кампании, а не подмена дефолтом с реальной отправкой."""
    import pandas as pd

    from parser import outreach as om
    from parser.config import Settings

    xlsx = tmp_path / "a.xlsx"
    pd.DataFrame([{"name": "А", "website": "a.ru", "pitch": "сайты + CRM",
                   "best_email": "info@a.ru", "niche_tags": "",
                   "partner_page_url": ""}]).to_excel(xlsx, index=False)
    settings = Settings(output_dir=tmp_path, smtp_host="h", smtp_user="u", smtp_password="p")
    sent = []
    result = om.run_outreach(xlsx, settings, limit=0, send=True,
                             journal_path=tmp_path / "j.json",
                             send_fn=lambda s, letter: sent.append(letter),
                             sleep_fn=lambda s: None)
    assert result.sent == 0 and sent == []


def test_outreach_one_domain_one_letter_within_run(tmp_path):
    """Дубли домена в файле (склейка выгрузок) — уходит только первое письмо."""
    from datetime import datetime

    import pandas as pd

    from parser import outreach as om
    from parser.config import Settings

    xlsx = tmp_path / "a.xlsx"
    pd.DataFrame([
        {"name": "А", "website": "same.ru", "pitch": "сайты + CRM",
         "best_email": "info@same.ru", "niche_tags": "", "partner_page_url": ""},
        {"name": "А2", "website": "www.same.ru", "pitch": "только CRM",
         "best_email": "sales@same.ru", "niche_tags": "", "partner_page_url": ""},
    ]).to_excel(xlsx, index=False)
    settings = Settings(output_dir=tmp_path, smtp_host="h", smtp_user="u", smtp_password="p")
    sent = []
    result = om.run_outreach(xlsx, settings, send=True, journal_path=tmp_path / "j.json",
                             send_fn=lambda s, letter: sent.append(letter.target.email),
                             sleep_fn=lambda s: None, now=datetime(2026, 7, 30, 12, 0))
    assert sent == ["info@same.ru"] and result.skipped_already == 1


def test_outreach_followup_keeps_agency_name(tmp_path):
    """Догон персонализирован названием агентства, а не доменом (журнал хранит name)."""
    from datetime import date, datetime

    from parser.outreach import Journal, Target, render_followup, render_letter

    journal = Journal(tmp_path / "j.json")
    target = Target(name="Крутое Агентство", domain="agency.ru",
                    email="info@agency.ru", pitch="сайты + CRM")
    journal.mark_sent(render_letter(target), datetime(2026, 7, 27, 10, 0))
    due = journal.followup_due(date(2026, 7, 30))
    assert due and due[0]["name"] == "Крутое Агентство"
    letter = render_followup(Target(name=due[0]["name"], domain=due[0]["domain"],
                                    email=due[0]["email"], pitch=due[0].get("pitch", "")))
    assert "Крутое Агентство" in letter.subject and "agency.ru" not in letter.subject


def test_dgis_key_blocked_rechecks_after_ttl(monkeypatch):
    """Блокировка ключа не вечна: спустя TTL даём API пробный шанс (лимит обнуляется 24.08)."""
    from parser.providers import dgis

    monkeypatch.setattr(dgis, "KEY_BLOCKED", False)
    monkeypatch.setattr(dgis, "_BLOCKED_AT", None)
    dgis._mark_blocked()
    assert dgis.key_blocked() is True
    # прошло больше TTL — флаг сбрасывается, разрешая пробный запрос
    monkeypatch.setattr(dgis, "_BLOCKED_AT", dgis._BLOCKED_AT - dgis._BLOCK_RECHECK_SECONDS - 1)
    assert dgis.key_blocked() is False
    assert dgis.KEY_BLOCKED is False


def test_search_marks_big_agencies():
    """Поисковый источник помечает крупняк тем же стоп-листом, что и каталоги."""
    from parser.contacts import BIG_AGENCY_NAMES, looks_like_big_agency

    known = next(iter(BIG_AGENCY_NAMES))
    assert looks_like_big_agency(known)


def test_address_fallback_hyphen_and_two_word_cities():
    from parser.providers.browser import _parse_search_card

    assert _parse_search_card("Сервис4.512 оценок Будённовский проспект, 1, Ростов-на-Дону")[2] \
        == "Будённовский проспект, 1, Ростов-на-Дону"
    assert _parse_search_card("Дент5.03 оценки Большая Покровская улица, 5, Нижний Новгород")[2] \
        == "Большая Покровская улица, 5, Нижний Новгород"


# --- фиксы аудита №2 ----------------------------------------------------------

def test_dedup_merges_same_firm_from_api_and_browser(make_org):
    """Одна фирма из API и из браузерного поиска: имена/адреса разные, /firm/id один."""
    url = "https://2gis.ru/firm/4504127908510464"
    orgs = [
        make_org(name="Илатан, сеть стоматологических клиник",
                 address="Марксистский переулок, 3, Москва", source="2gis", source_url=url),
        make_org(name="Илатан", address="Марксистский переулок, 3",
                 source="browser", source_url=url + "/"),
    ]
    result = deduplicate(orgs)
    assert len(result) == 1 and result[0].source == "2gis"  # приоритет базовой записи


def test_dedup_keeps_different_firms(make_org):
    orgs = [
        make_org(name="А", source="browser", source_url="https://2gis.ru/firm/1"),
        make_org(name="Б", source="browser", source_url="https://2gis.ru/firm/2"),
    ]
    assert len(deduplicate(orgs)) == 2


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://user:pass@host.ru:8080", "http://user:pass@host.ru:8080"),
        ("1.2.3.4:8080:user:pass", "http://user:pass@1.2.3.4:8080"),  # формат провайдера
        ("host.ru:3128", "http://host.ru:3128"),  # без схемы и кредов
    ],
)
def test_httpx_proxy_url_normalizes(raw, expected):
    """httpx не понимает провайдерский формат — без нормализации живой прокси «мёртв»."""
    from parser.providers.browser import httpx_proxy_url

    assert httpx_proxy_url(raw) == expected
    assert httpx_proxy_url(None) is None


def test_httpx_proxy_url_accepted_by_httpx():
    import httpx

    from parser.providers.browser import httpx_proxy_url

    # Раньше сырая строка бросала ValueError 'Unknown scheme' прямо в конструкторе.
    httpx.AsyncClient(proxy=httpx_proxy_url("1.2.3.4:8080:user:pass"))


def test_send_via_smtp_survives_error_on_quit(monkeypatch):
    """Письмо принято сервером, но QUIT ответил 421: это НЕ повод считать отправку неудачной."""
    import smtplib

    from parser.config import Settings
    from parser.outreach import Letter, Target, send_via_smtp

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg["To"])
        def quit(self): raise smtplib.SMTPResponseException(421, b"rate limit")

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    letter = Letter(target=Target(name="А", domain="a.ru", email="info@a.ru", pitch="сайты + CRM"),
                    subject="тема", body="текст")
    send_via_smtp(Settings(smtp_host="h", smtp_user="u@h", smtp_password="p"), letter)
    assert sent == ["info@a.ru"]  # исключение из quit не всплыло -> письмо попадёт в журнал


def test_send_via_smtp_propagates_real_failure(monkeypatch):
    """А вот отказ логина обязан всплыть — иначе адресат попал бы в журнал без письма."""
    import smtplib

    import pytest as _pytest

    from parser.config import Settings
    from parser.outreach import Letter, Target, send_via_smtp

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def login(self, *a): raise smtplib.SMTPAuthenticationError(535, b"bad password")
        def send_message(self, msg): raise AssertionError("не должно дойти до отправки")
        def quit(self): pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    letter = Letter(target=Target(name="А", domain="a.ru", email="i@a.ru", pitch="сайты + CRM"),
                    subject="т", body="б")
    with _pytest.raises(smtplib.SMTPAuthenticationError):
        send_via_smtp(Settings(smtp_host="h", smtp_user="u@h", smtp_password="p"), letter)


def test_collector_stops_on_persistent_failures(monkeypatch):
    """Систематические падения запроса гасят сбор, а не крутят цикл вечно."""
    import asyncio

    from parser import bot as bot_mod

    calls = {"n": 0}
    sent: list[str] = []

    async def always_fails(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("прокси умер")

    monkeypatch.setattr(bot_mod, "pipeline_run", always_fails)

    class FakeBot:
        settings = type("S", (), {"collect_target": 100, "collect_delay": 0,
                                  "collect_per_query": 5, "search_source": "auto"})()
        _collecting = True
        _collect_checked = 0
        _base = type("B", (), {
            "count": staticmethod(lambda: 0),
            "unsaved": [],
            "take_unsaved": staticmethod(list),
        })()
        _seen: set = set()

        def _collect_combos(self):
            return [("стоматология", "Москва"), ("автосервис", "Москва")]

        def _main_keyboard(self):
            return None

        async def _send(self, client, chat_id, text, kb=None):
            sent.append(text)

    asyncio.run(bot_mod.Bot._collector_loop(FakeBot(), None, 1))
    # остановились на пороге, а не после тысяч попыток
    assert calls["n"] == bot_mod._COLLECT_MAX_ERRORS
    assert any("Сбор остановлен" in t for t in sent)


def test_start_collect_refuses_while_previous_task_alive():
    """Гонка stop→start: пока старая задача жива, второй коллектор не поднимается."""
    import asyncio

    from parser import bot as bot_mod

    async def scenario():
        class FakeBot:
            _collecting = False
            _collect_task = None

            async def _collector_loop(self, client, chat_id):
                await asyncio.sleep(0.2)

        bot = FakeBot()
        assert bot_mod.Bot._start_collect(bot, None, 1) is True
        first = bot._collect_task
        bot_mod.Bot._stop_collect(bot)  # /stop только просит остановиться
        # старая задача ещё доигрывает — второй запуск обязан быть отклонён
        assert bot_mod.Bot._start_collect(bot, None, 1) is False
        assert bot._collect_task is first
        await first
        # доиграла — теперь можно
        assert bot_mod.Bot._start_collect(bot, None, 1) is True
        bot._collecting = False
        await bot._collect_task

    asyncio.run(scenario())


def test_systemd_unit_startlimit_in_unit_section():
    """StartLimit* работают только в [Unit]; в [Service] systemd их игнорирует."""
    from pathlib import Path

    text = Path(__file__).resolve().parent.parent.joinpath("deploy/leadgen-bot.service").read_text()

    # Разбираем как systemd: секция — строка вида [Name], комментарии игнорируем
    # (иначе упоминание «[Service]» внутри пояснения ломает разбор).
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)

    unit = sections["[Unit]"]
    service = sections["[Service]"]
    assert any(ln.startswith("StartLimitIntervalSec=") for ln in unit)
    assert any(ln.startswith("StartLimitBurst=") for ln in unit)
    assert not any(ln.startswith("StartLimit") for ln in service)


# --- фиксы аудита №4 ----------------------------------------------------------

def test_dedup_merges_chain_branches_by_shared_phone(make_org):
    """Филиалы сети: разные /firm/id, общий телефон (появляется после добора) — одна запись."""
    orgs = [
        make_org(name="Клиника, филиал 1", phone=["+74951112233"],
                 source_url="https://2gis.ru/firm/111"),
        make_org(name="Клиника, филиал 2", phone=["+7 495 111-22-33"],
                 source_url="https://2gis.ru/firm/222"),
    ]
    assert len(deduplicate(orgs)) == 1


def test_dedup_links_groups_through_shared_signal(make_org):
    """Запись, связавшая две ранее разные группы, объединяет их (порядок входа не важен)."""
    orgs = [
        make_org(name="А", source_url="https://2gis.ru/firm/111"),
        make_org(name="Б", phone=["+74951112233"], source_url="https://2gis.ru/firm/222"),
        # третья запись знает и карточку первой, и телефон второй
        make_org(name="В", phone=["+74951112233"], source_url="https://2gis.ru/firm/111"),
    ]
    assert len(deduplicate(orgs)) == 1


def test_dedup_merge_keeps_complaints_and_dadata_fields(make_org):
    """Жалобы и данные DaData не должны теряться вместе с отброшенной записью."""
    merged = deduplicate([
        make_org(name="К", source_url="https://2gis.ru/firm/1", review_complaints=["не дозвониться"]),
        make_org(name="К", source_url="https://2gis.ru/firm/1", review_complaints=["долго отвечают"],
                 director="Иванов И.И.", entity_type="ООО", registered_year=2015),
    ])[0]
    assert merged.review_complaints == ["не дозвониться", "долго отвечают"]
    assert merged.director == "Иванов И.И." and merged.entity_type == "ООО"
    assert merged.registered_year == 2015


def test_collector_refuses_when_target_already_reached():
    """База уже >= цели: честно объясняем, а не рапортуем «запущен» и молча выходим."""
    import asyncio

    from parser import bot as bot_mod

    sent: list[str] = []

    class FakeBot:
        settings = type("S", (), {"collect_target": 100, "collect_delay": 0,
                                  "collect_per_query": 5, "search_source": "auto"})()
        _collecting = True
        _collect_checked = 0
        _base = type("B", (), {"count": staticmethod(lambda: 100)})()
        _seen: set = set()

        def _collect_combos(self):
            return [("стоматология", "Москва")]

        def _main_keyboard(self):
            return None

        async def _send(self, client, chat_id, text, kb=None):
            sent.append(text)

    bot = FakeBot()
    asyncio.run(bot_mod.Bot._collector_loop(bot, None, 1))
    assert len(sent) == 1 and "COLLECT_TARGET" in sent[0]
    assert not any("Фоновый сбор запущен" in t for t in sent)
    assert bot._collecting is False


# --- фиксы аудита №5: enrich_file не должен терять уже найденные данные ---------

def _fake_dadata(monkeypatch, answers: dict):
    """Подменяем поход в DaData: возвращаем ответ по имени организации."""
    import sys
    import types

    from parser import enrich as enrich_mod

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def suggest(self, kind, query, count=1):
            return answers.get(query, [])
        def find_by_id(self, kind, query, count=1):
            return answers.get(query, [])

    module = types.ModuleType("dadata")
    module.Dadata = FakeClient
    monkeypatch.setitem(sys.modules, "dadata", module)
    monkeypatch.setattr(enrich_mod, "_require_keys", lambda settings: None)


def test_enrich_file_keeps_existing_director_and_inn(tmp_path, monkeypatch):
    """Повторный прогон не должен стирать ФИО у строк, где ИНН уже найден."""
    import pandas as pd

    from parser import enrich as enrich_mod
    from parser.config import Settings

    csv = tmp_path / "leads.csv"
    pd.DataFrame([
        {"name": "Клиника А", "address": "Москва", "inn": "7701234567", "director": "Иванов Иван"},
        {"name": "Клиника Б", "address": "Москва", "inn": "", "director": ""},
        {"name": "Клиника В", "address": "Москва", "inn": "0278012345", "director": "Петров Пётр"},
    ]).to_csv(csv, index=False)

    _fake_dadata(monkeypatch, {})  # ничего нового не находим
    enrich_mod.enrich_file(csv, settings=Settings(dadata_api_key="k", dadata_secret="s"))

    out = pd.read_csv(csv, dtype=str)
    assert list(out["director"].fillna("")) == ["Иванов Иван", "", "Петров Пётр"]
    # ИНН не поплыл в float: без «.0» и с сохранённым ведущим нулём
    assert list(out["inn"].fillna("")) == ["7701234567", "", "0278012345"]


def test_enrich_file_repairs_inn_broken_by_previous_version(tmp_path, monkeypatch):
    """ИНН, испорченный прошлой версией («7701234567.0»), чинится при чтении."""
    import pandas as pd

    from parser import enrich as enrich_mod
    from parser.config import Settings

    csv = tmp_path / "leads.csv"
    pd.DataFrame([
        {"name": "А", "address": "Москва", "inn": "7701234567.0", "director": "Иванов"}
    ]).to_csv(csv, index=False)

    _fake_dadata(monkeypatch, {})
    enrich_mod.enrich_file(csv, settings=Settings(dadata_api_key="k", dadata_secret="s"))

    assert pd.read_csv(csv, dtype=str).loc[0, "inn"] == "7701234567"


def test_dedup_keeps_earliest_record_as_base(make_org):
    """При слиянии групп базой остаётся самая ранняя запись, а не случайная."""
    orgs = [
        make_org(name="Первая", source_url="https://2gis.ru/firm/111"),
        make_org(name="Вторая", phone=["+74951112233"]),
        # связывает обе группы; в её ключах телефон идёт раньше ссылки
        make_org(name="Третья", phone=["+74951112233"], source_url="https://2gis.ru/firm/111"),
    ]
    result = deduplicate(orgs)
    assert len(result) == 1
    assert result[0].name == "Первая"


# --- Очистка базы лидов обязана быть честной ----------------------------------

def test_clear_reports_failure_instead_of_pretending(tmp_path, monkeypatch):
    """Файл не удалился — «база очищена» это неправда.

    Память к этому моменту уже пуста, и бот рапортует успех. Но после
    рестарта файл прочитается заново и все лиды вернутся, а владелец уверен,
    что начал с нуля, — и собирает поверх старого.
    """
    import pytest

    from parser.leadbase import LeadBase

    path = tmp_path / "leads_base.json"
    path.write_text("{}", encoding="utf-8")
    base = LeadBase(path)

    def refuse(self):
        raise OSError("нет прав на удаление")

    monkeypatch.setattr(type(path), "unlink", refuse, raising=False)

    base.leads["ключ"] = object()

    with pytest.raises(OSError):
        base.clear()

    assert base.leads, "не смогли удалить файл — значит и память не трогаем"


def test_enrich_limit_counts_only_unfilled_rows(monkeypatch, tmp_path):
    """--limit обязан считать те строки, которые ещё надо обогатить.

    Раньше лимит резал список ДО пропуска уже заполненных: на повторном
    прогоне того же файла первые N строк оказывались с ИНН, все они
    пропускались, и прогон не делал ничего. Сколько ни запускай — файл не
    двигался с места.
    """
    from parser import enrich
    from parser.models import Organization

    orgs = [
        Organization(name=f"Фирма {i}", source="2gis", inn="7700000000") for i in range(5)
    ]
    orgs.append(Organization(name="Новая фирма", source="2gis"))

    asked: list[str] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def suggest(self, _kind, query, count=5):
            asked.append(query)
            return [{"data": {"inn": "7712345678", "type": "LEGAL"}}]

    monkeypatch.setattr(enrich, "_require_keys", lambda _settings: None)
    monkeypatch.setattr("dadata.Dadata", lambda *a, **kw: FakeClient(), raising=False)

    enrich.enrich(orgs, limit=3)

    assert asked == ["Новая фирма"], asked
    assert orgs[-1].inn == "7712345678"


def test_a_dead_proxy_is_not_reported_as_a_clean_run(monkeypatch, caplog):
    """Ни одна карточка не открылась — это поломка, а не «жалоб нет».

    Флаг `ok` из обхода выбрасывался, и счётчик считал ВСЕ карточки
    обработанными: прогон с мёртвым прокси писал в отчёт «готово 40/40,
    с сигналами — 0» — то есть выглядел ровно как честный сбор по компаниям
    без жалоб.
    """
    import asyncio

    from parser.models import Organization
    from parser.providers import browser as browser_mod

    orgs = [
        Organization(name=f"Компания {n}", source="2gis",
                     source_url=f"https://2gis.ru/firm/{n}")
        for n in range(3)
    ]

    async def all_failed(orgs_, settings, limit):
        for org in orgs_:
            yield org, False

    monkeypatch.setattr(browser_mod, "iter_enrich_with_browser", all_failed)

    errors: list[str] = []
    monkeypatch.setattr(browser_mod.logger, "error", lambda msg, *a: errors.append(msg.format(*a)))

    asyncio.run(browser_mod.enrich_with_browser(orgs))
    assert errors, "о мёртвом прокси надо сказать вслух"
    assert "не открылось карточек — 3 из 3" in errors[0]


def test_a_healthy_run_says_nothing_extra(monkeypatch):
    """А удачный обход лишних тревог не поднимает."""
    import asyncio

    from parser.models import Organization
    from parser.providers import browser as browser_mod

    orgs = [Organization(name="Компания", source="2gis",
                         source_url="https://2gis.ru/firm/1")]

    async def all_fine(orgs_, settings, limit):
        for org in orgs_:
            yield org, True

    monkeypatch.setattr(browser_mod, "iter_enrich_with_browser", all_fine)
    errors: list[str] = []
    monkeypatch.setattr(browser_mod.logger, "error", lambda msg, *a: errors.append(msg))

    asyncio.run(browser_mod.enrich_with_browser(orgs))
    assert errors == []


def test_stopping_a_collection_that_is_not_running_says_so():
    """Кнопка «Остановить сбор» на старом сообщении не должна врать.

    Сообщение с кнопкой никуда не девается: нажать её можно и через сутки
    после того, как сбор кончился сам. Ответ «Останавливаю сбор…» заставлял
    владельца ждать остановки того, чего нет.
    """
    import asyncio

    from parser import bot as bot_mod

    answers: list[str] = []

    class FakeBot:
        _collecting = False

        def _state(self, chat_id):
            return type("S", (), {"niche": None, "city": None, "busy": False})()

        async def _answer_cb(self, client, callback_id, text=""):
            answers.append(text)

        def _stop_collect(self):
            raise AssertionError("останавливать нечего")

    cb = {"id": "1", "data": "collect:stop", "message": {"chat": {"id": 1}, "message_id": 5}}
    asyncio.run(bot_mod.Bot._handle_callback(FakeBot(), None, cb))
    assert answers == ["Сбор и так не идёт"]


def test_stopping_a_running_collection_stops_it():
    """А идущий сбор кнопка обязана остановить."""
    import asyncio

    from parser import bot as bot_mod

    answers: list[str] = []
    stopped: list[int] = []

    class FakeBot:
        _collecting = True

        def _state(self, chat_id):
            return type("S", (), {"niche": None, "city": None, "busy": False})()

        async def _answer_cb(self, client, callback_id, text=""):
            answers.append(text)

        def _stop_collect(self):
            stopped.append(1)

    cb = {"id": "1", "data": "collect:stop", "message": {"chat": {"id": 1}, "message_id": 5}}
    asyncio.run(bot_mod.Bot._handle_callback(FakeBot(), None, cb))
    assert answers == ["Останавливаю сбор…"]
    assert stopped == [1]


def test_a_dead_key_is_not_reported_as_no_places_left(monkeypatch):
    """Пустые ответы подряд — это мёртвый источник, а не конец справочника.

    Выгоревший ключ 2ГИС и мёртвый прокси исключений не бросают: провайдер
    пишет в лог «запрос пропущен» и возвращает пустой список. Защита по
    падениям на это не срабатывала никогда, зато рос счётчик «обошёл всё», и
    владельцу сообщали, что компании в стране кончились.
    """
    import asyncio

    from parser import bot as bot_mod

    sent: list[str] = []
    calls = {"n": 0}

    async def always_empty(*args, **kwargs):
        calls["n"] += 1
        return []

    monkeypatch.setattr(bot_mod, "pipeline_run", always_empty)

    class FakeBot:
        settings = type("S", (), {"collect_target": 100, "collect_delay": 0,
                                  "collect_per_query": 5, "search_source": "auto"})()
        _collecting = True
        _collect_checked = 0
        _base = type("B", (), {
            "count": staticmethod(lambda: 0),
            "unsaved": [],
            "take_unsaved": staticmethod(list),
        })()
        _seen: set = set()

        def _collect_combos(self):
            return [("стоматология", f"Город{n}") for n in range(20)]

        def _main_keyboard(self):
            return None

        async def _send(self, client, chat_id, text, kb=None):
            sent.append(text)

    asyncio.run(bot_mod.Bot._collector_loop(FakeBot(), None, 1))

    assert any("вернули пусто" in t for t in sent), sent
    assert not any("новых мест не осталось" in t for t in sent), "это не конец справочника"
    assert calls["n"] == bot_mod._COLLECT_MAX_ERRORS, "долбиться в мёртвый источник незачем"


# --- разовый поиск: лиды должны доживать до базы --------------------------------


def _search_bot(tmp_path, sent, eta_args):
    """Заготовка бота для разового поиска: живая база, всё остальное подделано."""
    from parser.bot import Bot, SeenStore
    from parser.leadbase import LeadBase

    class FakeBot(Bot):
        def __init__(self):
            self.settings = type("S", (), {
                "search_source": "2gis", "request_delay": 0, "output_dir": tmp_path,
            })()
            self.states = {}
            self._base = LeadBase(tmp_path / "base.jsonl")
            self._seen = SeenStore(tmp_path / "seen.json")

        def _build_pairs(self, niche, city):
            return [("стоматология", "Москва"), ("стоматология", "Казань")]

        def _niche_title(self, niche):
            return "Стоматологии"

        def _city_title(self, city):
            return "Москва"

        async def _edit(self, client, chat_id, message_id, text, kb=None):
            sent.append(text)

        async def _send(self, client, chat_id, text, kb=None):
            sent.append(text)

        async def _enrich_inn(self, org, city):
            return None

        async def _send_export(self, client, chat_id, orgs):
            return None

        @staticmethod
        def _eta_minutes(n_pairs, n_cards, source="2gis"):
            eta_args.append((n_pairs, n_cards, source))
            return 1

    return FakeBot()


def _hot_org(number: int, phone: str):
    from parser.models import Organization

    return Organization(
        source="2gis", name=f"Стоматология {number}",
        address=f"Москва, ул. Ленина, {number}",
        source_url=f"https://2gis.ru/firm/{number}", phone=[phone],
        review_complaints=["не берут трубку"],
    )


def _run_search_with(monkeypatch, bot, orgs, count=10):
    import asyncio

    from parser import bot as bot_mod

    async def fake_pipeline_run(pairs, **kwargs):
        return list(orgs)

    async def fake_iter(items, settings):
        for org in items:
            yield org, True

    monkeypatch.setattr(bot_mod, "pipeline_run", fake_pipeline_run)
    monkeypatch.setattr(bot_mod, "iter_enrich_with_browser", fake_iter)
    monkeypatch.setattr(bot_mod, "resolve_source", lambda *a, **kw: "2gis")
    asyncio.run(bot._run_search(None, 1, 1, count=count))


def test_a_one_off_search_saves_its_hot_leads(monkeypatch, tmp_path):
    """Разовый поиск помечал организацию проверенной, но лид никуда не клал.

    Карточка в переписке и строка в выгрузке — всё; в базу лид не попадал, а
    второй раз эта организация не показалась бы уже никогда. Потерять сообщение
    значило потерять лид насовсем.
    """
    sent: list[str] = []
    bot = _search_bot(tmp_path, sent, [])
    _run_search_with(monkeypatch, bot, [_hot_org(1, "+7 900 000-00-01")])

    assert bot._base.count() == 1, "горячий лид не доехал до базы"


def test_two_branches_with_one_phone_are_one_lead(monkeypatch, tmp_path):
    """Телефон появляется только после добора — филиалы сети надо схлопнуть тут.

    В командной строке после добора прогоняется второй дедуп, а бот отдаёт лид
    сразу, как нашёл: без проверки по телефону владельцу уходили два лида с
    одним номером, и он звонил в одну компанию дважды.
    """
    sent: list[str] = []
    bot = _search_bot(tmp_path, sent, [])
    same = "+7 900 000-00-02"
    _run_search_with(monkeypatch, bot, [_hot_org(1, same), _hot_org(2, same)])

    assert bot._base.count() == 1, "филиалы с общим телефоном ушли как два лида"


def test_the_eta_counts_what_we_actually_ask_for(monkeypatch, tmp_path):
    """Срок обещался по полному объёму, хотя у каждой пары просим долю от него."""
    sent: list[str] = []
    eta_args: list[tuple] = []
    bot = _search_bot(tmp_path, sent, eta_args)
    # Сто карточек на две пары: у каждой просим 75, а не все сто.
    _run_search_with(monkeypatch, bot, [_hot_org(1, "+7 900 000-00-03")], count=100)

    pairs, cards, _source = eta_args[0]
    assert pairs == 2
    assert cards < 100, f"обещали срок по полному объёму: {cards}"


def test_clearbase_does_not_clean_under_a_running_collector(tmp_path):
    """Очистка под работающим сбором врала: он дописывал лиды уже после неё.

    `_stop_collect` только просит остановиться; задача узнаёт об этом, доработав
    текущую карточку — до тридцати секунд браузера через прокси. Всё это время
    она кладёт лиды, и они ложились под сообщением «База очищена начисто».
    """
    import asyncio

    from parser import bot as bot_mod

    sent: list[str] = []

    async def scenario():
        class FakeBot:
            _collecting = True
            _base = type("B", (), {"clear": staticmethod(lambda: 0)})()

            def __init__(self):
                # Задача, которая «доигрывает» дольше, чем мы согласны ждать.
                self._collect_task = asyncio.ensure_future(asyncio.sleep(5))

            def _state(self, chat_id):
                from parser.bot import ChatState

                return ChatState()

            def _stop_collect(self):
                type(self)._collecting = False

            def _main_keyboard(self):
                return None

            def _niche_keyboard(self):
                return None

            async def _send(self, client, chat_id, text, kb=None):
                sent.append(text)

        bot = FakeBot()
        monkey = bot_mod._COLLECT_STOP_WAIT
        bot_mod._COLLECT_STOP_WAIT = 0.05
        try:
            await bot_mod.Bot._handle_command(bot, None, 1, "/clearbase")
        finally:
            bot_mod._COLLECT_STOP_WAIT = monkey
            bot._collect_task.cancel()

    asyncio.run(scenario())

    assert any("База не тронута" in text for text in sent), sent
    assert not any("очищена начисто" in text for text in sent), sent


def test_the_state_file_belongs_to_one_query(tmp_path):
    """Файл докачки назывался только по --out, а он у всех прогонов один.

    С --resume (он по умолчанию) второй прогон подхватывал из общего файла ВСЕ
    ранее собранные организации: выгрузка по автосервисам Казани приезжала
    вместе с московскими стоматологиями из прошлого прогона.
    """
    from parser.cli import _run_tag

    moscow = [("стоматология", "Москва")]
    kazan = [("автосервис", "Казань")]

    assert _run_tag("2gis", moscow) != _run_tag("2gis", kazan)
    assert _run_tag("2gis", moscow) != _run_tag("browser", moscow)
    # Та же команда, набранная в другом порядке, обязана продолжать свой файл.
    pair = [("стоматология", "Москва"), ("автосервис", "Казань")]
    assert _run_tag("2gis", pair) == _run_tag("2gis", list(reversed(pair)))


def test_a_search_says_when_a_lead_did_not_reach_the_disk(monkeypatch, tmp_path):
    """Лид в базе только до перезапуска — об этом надо сказать сразу.

    Раньше разовый поиск в базу вообще не писал; теперь пишет, и о неудачной
    записи должен говорить он сам. Молчание тут неотличимо от нормального
    поиска, а следующий фоновый сбор сообщит об этом уже про чужие лиды.
    """
    sent: list[str] = []
    bot = _search_bot(tmp_path, sent, [])

    def refuse(self, org):
        self.unsaved.append(org.name)
        return True

    monkeypatch.setattr(type(bot._base), "add", refuse)
    _run_search_with(monkeypatch, bot, [_hot_org(1, "+7 900 000-00-04")])

    assert any("Не записалось на диск" in text for text in sent), sent


def test_the_owner_hears_that_the_rules_changed(tmp_path):
    """Расширили правила поиска жалоб — прежние «чисто» стали сомнительными.

    Журнал проверенных не покажет эти компании второй раз, а перепроверять всё
    молча нельзя: это часы браузера через мобильный прокси. Значит, решает
    владелец — но узнать он должен от нас.
    """
    import asyncio

    from parser import bot as bot_mod
    from parser.bot import SeenStore

    sent: list[str] = []

    class FakeBot:
        settings = type("S", (), {"output_dir": tmp_path})()
        _allowed = {1}

        def __init__(self):
            self._seen = SeenStore(tmp_path / "seen.json")
            self._seen.keys = {"https://2gis.ru/firm/1", "https://2gis.ru/firm/2"}

        async def _send(self, client, chat_id, text, kb=None):
            sent.append(text)

    bot = FakeBot()
    # Первый запуск: отметки нет, сообщать не о чем — только запоминаем.
    asyncio.run(bot_mod.Bot._tell_if_rules_changed(bot, None))
    assert sent == []

    # Правила изменились — говорим ровно один раз.
    (tmp_path / "seen_rules.txt").write_text("другой-отпечаток", "utf-8")
    asyncio.run(bot_mod.Bot._tell_if_rules_changed(bot, None))
    assert len(sent) == 1 and "/reset" in sent[0], sent

    asyncio.run(bot_mod.Bot._tell_if_rules_changed(bot, None))
    assert len(sent) == 1, "повторять это на каждом запуске нельзя"

"""Поиск почты на сайте компании.

Сеть в тестах поддельная: настоящий обход зависит от того, что сегодня
отвечает чужой сайт, и такой тест ломается сам по себе. Через httpx.
MockTransport проверяется ровно наша логика — что берём, что отбрасываем
и куда ходим.
"""
from __future__ import annotations

import httpx
import pytest

from orbit import sitescan
from orbit.models import Contact, Kind


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    """Вежливая пауза между запросами тестам не нужна."""
    monkeypatch.setattr(sitescan.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def no_real_search(monkeypatch):
    """Поиск сайта по умолчанию заглушён.

    Без этого тест на компанию без сайта незаметно уходил в настоящий
    DuckDuckGo: результат зависел от того, что сегодня в выдаче, а сам
    прогон — от наличия интернета.
    """
    monkeypatch.setattr(sitescan, "find_site", lambda *a, **kw: None)


# Ссылку на настоящий поиск берём до всяких подмен: фикстура выше подменяет
# sitescan.find_site раньше, чем до него доберётся тест.
_REAL_FIND_SITE = sitescan.find_site


@pytest.fixture()
def real_find_site():
    """Настоящая функция поиска — для тестов, которые проверяют именно её."""
    return _REAL_FIND_SITE


def make_client(pages: dict[str, str], *, status: int = 200) -> httpx.Client:
    """Сайт из словаря «путь → HTML». Чего нет в словаре — то 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# --- Что считаем контактом -------------------------------------------------

def test_finds_email_in_text_and_mailto():
    html = '<a href="mailto:hello@firma.ru">написать</a> или zakaz@firma.ru'
    assert sitescan.extract_emails(html) == ["hello@firma.ru", "zakaz@firma.ru"]


def test_ignores_personal_mailboxes():
    """На личный ящик холодное письмо не шлём — это правило рассылки."""
    html = "Пишите: ivanov1985@mail.ru, ivan.petrov@gmail.com"
    assert sitescan.extract_emails(html) == []


def test_ignores_widget_and_builder_addresses():
    """«Сделано на Тильде» — это не контакт компании."""
    html = "support@tilda.cc help@jivosite.com info@firma.ru"
    assert sitescan.extract_emails(html) == ["info@firma.ru"]


def test_ignores_files_that_look_like_email():
    assert sitescan.extract_emails('<img src="logo@2x.png"> doc@v2.pdf') == []


def test_email_inside_script_is_ignored():
    """Внутри скриптов лежат ключи аналитики, а не адреса компании."""
    html = "<script>var t='track@roistat.com';</script><p>office@firma.ru</p>"
    assert sitescan.extract_emails(html) == ["office@firma.ru"]


def test_finds_telegram_links():
    html = '<a href="https://t.me/firma_msk">телеграм</a>'
    assert sitescan.extract_telegram(html) == ["@firma_msk"]


def test_ignores_share_links():
    """t.me/share/url — это кнопка «поделиться», а не контакт."""
    assert sitescan.extract_telegram('<a href="https://t.me/share/url?u=x">поделиться</a>') == []


# --- Обход сайта -----------------------------------------------------------

def test_scan_walks_to_contacts_page():
    client = make_client({
        "/": "<html>Главная без контактов</html>",
        "/contacts": "<html>Почта: info@firma.ru</html>",
    })
    with client:
        found = sitescan.scan("firma.ru", client=client)

    assert found.emails == ["info@firma.ru"]
    assert found.pages == 2
    assert found.error is None


def test_scan_follows_link_from_home_when_paths_do_not_fit():
    """Самописный сайт: контакты лежат по своему адресу, ссылка есть на главной."""
    client = make_client({
        "/": '<a href="/svyaz-s-nami">Контакты</a>',
        "/svyaz-s-nami": "office@firma.ru",
    })
    with client:
        found = sitescan.scan("firma.ru", client=client)

    assert found.emails == ["office@firma.ru"]


def test_scan_reports_dead_site():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет такого хоста")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = sitescan.scan("nosuchsite.ru", client=client)

    assert not found.anything
    assert found.error and "не открылся" in found.error


def test_scan_respects_robots():
    """robots.txt запрещает раздел — туда не ходим."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /contacts")
        if request.url.path == "/contacts":
            return httpx.Response(200, text="secret@firma.ru")
        return httpx.Response(200, text="<html>главная</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = sitescan.scan("firma.ru", client=client)

    assert found.emails == []


def test_wordpress_robots_does_not_close_the_whole_site():
    """«Disallow: /?» запрещает адреса с параметрами, а не весь сайт.

    Стандартный разборщик Python понимает эту строку как запрет корня, и
    живой сайт агентства из-за неё выглядел как «не открылся».
    """
    rules = sitescan.parse_robots(
        "User-agent: *\nDisallow: /wp-admin/\nDisallow: /?\nDisallow: *?s=\nDisallow: /search"
    )
    assert sitescan.robots_allows(rules, "https://firma.ru/") is True
    assert sitescan.robots_allows(rules, "https://firma.ru/contacts") is True
    assert sitescan.robots_allows(rules, "https://firma.ru/wp-admin/") is False
    assert sitescan.robots_allows(rules, "https://firma.ru/search") is False
    assert sitescan.robots_allows(rules, "https://firma.ru/?s=запрос") is False


def test_robots_allow_beats_disallow_of_same_length():
    rules = sitescan.parse_robots(
        "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php"
    )
    assert sitescan.robots_allows(rules, "https://firma.ru/wp-admin/admin-ajax.php") is True


def test_robots_for_other_bots_is_not_ours():
    """Запрет для чужого робота нас не касается, а разрешение — не оправдывает."""
    rules = sitescan.parse_robots("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /")
    assert sitescan.robots_allows(rules, "https://firma.ru/contacts") is True


def test_robots_end_anchor():
    rules = sitescan.parse_robots("User-agent: *\nDisallow: */embed$")
    assert sitescan.robots_allows(rules, "https://firma.ru/post/embed") is False
    assert sitescan.robots_allows(rules, "https://firma.ru/post/embedded") is True


def test_empty_robots_allows_everything():
    assert sitescan.robots_allows(sitescan.parse_robots(""), "https://firma.ru/") is True


def test_scan_ignores_foreign_domain_after_redirect():
    """Сайт переехал на чужой домен — контакты оттуда не наши."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "firma.ru":
            return httpx.Response(301, headers={"location": "https://gruppa.ru/"})
        return httpx.Response(200, text="info@gruppa.ru")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        found = sitescan.scan("firma.ru", client=client)

    assert found.emails == []


def test_site_url_normalizes():
    assert sitescan.site_url("firma.ru") == "https://firma.ru"
    assert sitescan.site_url("https://firma.ru/") == "https://firma.ru"
    assert sitescan.site_url("") == ""


# --- Запись в базу ---------------------------------------------------------

def _company(store, **kwargs) -> Contact:
    contact = Contact(
        kind=Kind.CLIENT,
        key=kwargs.pop("key", "firma.ru"),
        name=kwargs.pop("name", "Фирма"),
        site=kwargs.pop("site", "firma.ru"),
        phones=["+74951234567"],
        **kwargs,
    )
    contact.pk = store.upsert_contact(contact)
    return contact


def test_enrich_gives_company_a_channel(store, monkeypatch):
    """Была только трубка — стал адрес, и компании теперь можно написать."""
    company = _company(store)
    assert company.channel.value == "phone"

    monkeypatch.setattr(
        sitescan, "scan", lambda site, **kw: sitescan.Found(emails=["info@firma.ru"], pages=1)
    )
    scanned, gained = sitescan.enrich(store, Kind.CLIENT)

    assert (scanned, gained) == (1, 1)
    updated = store.find_contact(Kind.CLIENT, "firma.ru")
    assert updated.emails == ["info@firma.ru"]
    assert updated.reachable is True


def test_enrich_skips_companies_that_already_have_email(store, monkeypatch):
    _company(store, emails=["already@firma.ru"])
    monkeypatch.setattr(sitescan, "scan", lambda *a, **kw: pytest.fail("ходить незачем"))
    assert sitescan.enrich(store, Kind.CLIENT) == (0, 0)


def test_company_without_site_and_without_search_result_is_left_alone(store, monkeypatch):
    """Сайт не нашёлся — обходить нечего, но повторно дёргать компанию не будем."""
    _company(store, site=None, key="bez-sayta")
    monkeypatch.setattr(sitescan, "scan", lambda *a, **kw: pytest.fail("ходить некуда"))

    assert sitescan.enrich(store, Kind.CLIENT) == (1, 0)
    updated = store.find_contact(Kind.CLIENT, "bez-sayta")
    assert updated.meta["site_scan_error"] == "сайт не нашёлся"
    assert sitescan.enrich(store, Kind.CLIENT) == (0, 0)


def test_second_pass_does_not_walk_the_same_site_twice(store, monkeypatch):
    """Сходили и ничего не нашли — второй заход даст то же самое."""
    _company(store)
    monkeypatch.setattr(sitescan, "scan", lambda site, **kw: sitescan.Found(pages=1))

    assert sitescan.enrich(store, Kind.CLIENT) == (1, 0)
    monkeypatch.setattr(sitescan, "scan", lambda *a, **kw: pytest.fail("уже ходили"))
    assert sitescan.enrich(store, Kind.CLIENT) == (0, 0)


def test_failed_scan_remembers_why(store, monkeypatch):
    """Причина неудачи должна остаться в карточке, а не только в логе."""
    _company(store)
    monkeypatch.setattr(
        sitescan, "scan", lambda site, **kw: sitescan.Found(error="сайт не открылся")
    )
    sitescan.enrich(store, Kind.CLIENT)

    updated = store.find_contact(Kind.CLIENT, "firma.ru")
    assert updated.meta["site_scan_error"] == "сайт не открылся"


# --- Поиск сайта, когда его нет в выгрузке ---------------------------------

def test_domain_taken_from_company_name():
    """«ап-сервис.рф, автотехцентр» — искать нечего, домен в самом названии."""
    assert sitescan.domain_from_name("ап-сервис.рф, автотехцентр") == "ап-сервис.рф"
    assert sitescan.domain_from_name("Chery Кунцево, автоцентр") is None


def test_search_skips_maps_and_aggregators(real_find_site):
    """Первым в выдаче почти всегда 2ГИС — это не сайт компании."""
    html = """
    <a class="result__a" href="https://2gis.ru/moscow/firm/70000">ДС Авто в 2ГИС</a>
    <a class="result__a" href="https://zoon.ru/msk/ds-avto/">ДС Авто на Zoon</a>
    <a class="result__a" href="https://ds-avto.ru/">ДС Авто — официальный сайт</a>
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert real_find_site("ДС Авто", "Москва", client=client) == "ds-avto.ru"


def test_search_unwraps_duckduckgo_redirect(real_find_site):
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fds-avto.ru%2F">'
        "ДС Авто</a>"
    )
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))) as c:
        assert real_find_site("ДС Авто", client=c) == "ds-avto.ru"


def test_search_is_not_needed_when_name_is_a_domain(real_find_site):
    """Название и есть домен — в поиск не ходим вовсе."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("незачем искать")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert real_find_site("ап-сервис.рф, автотехцентр", client=client) == "ап-сервис.рф"


def test_search_challenge_is_not_mistaken_for_absence(real_find_site):
    """Поисковик подсунул заглушку — это «не посмотрели», а не «сайта нет»."""
    challenge = httpx.MockTransport(lambda r: httpx.Response(202, text="<html>"))
    with httpx.Client(transport=challenge) as client, pytest.raises(sitescan.SearchUnavailable):
        real_find_site("ДС Авто", client=client)


def test_company_is_retried_after_search_outage(store, monkeypatch):
    """Поиск лежал — компания должна вернуться в обход, а не выпасть навсегда."""
    _company(store, site=None, key="ds-avto", name="ДС Авто, автотехцентр")

    def broken(*a, **kw):
        raise sitescan.SearchUnavailable("поисковик ответил 202")

    monkeypatch.setattr(sitescan, "find_site", broken)
    assert sitescan.enrich(store, Kind.CLIENT) == (1, 0)

    updated = store.find_contact(Kind.CLIENT, "ds-avto")
    assert "поиск недоступен" in updated.meta["site_scan_error"]
    assert sitescan.SCANNED_KEY not in updated.meta

    # Поиск починился — компанию берём снова.
    monkeypatch.setattr(sitescan, "find_site", lambda *a, **kw: "ds-avto.ru")
    monkeypatch.setattr(
        sitescan, "scan",
        lambda site, **kw: sitescan.Found(emails=["info@ds-avto.ru"], pages=1, verified=True),
    )
    assert sitescan.enrich(store, Kind.CLIENT) == (1, 1)


def test_site_belongs_when_phone_matches(store):
    """Телефон из карт на странице — надёжнее любого совпадения по названию."""
    company = _company(store)
    assert sitescan.belongs_to("<p>Звоните: 8 (495) 123-45-67</p>", company) is True


def test_site_belongs_when_name_matches(store):
    company = _company(store, name="ДС Авто, автотехцентр", key="ds")
    assert sitescan.belongs_to("<h1>ДС Авто — ремонт машин</h1>", company) is True


def test_foreign_site_is_rejected(store):
    company = _company(store, name="ДС Авто, автотехцентр", key="ds")
    assert sitescan.belongs_to("<h1>Пекарня «Колосок»</h1> +7 999 000-11-22", company) is False


def test_enrich_finds_site_then_email(store, monkeypatch):
    """Полный путь: сайта нет → нашли поиском → сняли почту."""
    company = _company(store, site=None, key="ds-avto", name="ДС Авто, автотехцентр")

    monkeypatch.setattr(sitescan, "find_site", lambda *a, **kw: "ds-avto.ru")
    monkeypatch.setattr(
        sitescan, "scan",
        lambda site, **kw: sitescan.Found(emails=["info@ds-avto.ru"], pages=1, verified=True),
    )
    assert sitescan.enrich(store, Kind.CLIENT) == (1, 1)

    updated = store.find_contact(Kind.CLIENT, "ds-avto")
    assert updated.site == "ds-avto.ru"
    assert updated.emails == ["info@ds-avto.ru"]
    assert updated.meta["site_source"] == "поиск"
    assert company.pk == updated.pk


def test_unverified_site_contacts_are_not_saved(store, monkeypatch):
    """Поиск привёл к однофамильцу — чужую почту в карточку не пишем.

    Письмо не по адресу заметит получатель, и это хуже, чем пустая карточка.
    """
    _company(store, site=None, key="ds-avto", name="ДС Авто, автотехцентр")
    monkeypatch.setattr(sitescan, "find_site", lambda *a, **kw: "chuzhoy.ru")
    monkeypatch.setattr(
        sitescan, "scan",
        lambda site, **kw: sitescan.Found(
            emails=["info@chuzhoy.ru"], pages=1, verified=False, error="нашёлся чужой сайт"
        ),
    )
    assert sitescan.enrich(store, Kind.CLIENT) == (1, 0)

    updated = store.find_contact(Kind.CLIENT, "ds-avto")
    assert updated.emails == []
    assert updated.site is None
    assert updated.reachable is False


def test_scan_stops_on_foreign_site(store):
    """Главная не про эту компанию — дальше по её страницам не ходим."""
    company = _company(store, name="ДС Авто, автотехцентр", key="ds")
    client = make_client({
        "/": "<h1>Пекарня «Колосок»</h1>",
        "/contacts": "info@kolosok.ru",
    })
    with client:
        found = sitescan.scan("kolosok.ru", client=client, belongs_to_contact=company)

    assert found.verified is False
    assert found.emails == []
    assert found.pages == 1


def test_scan_confirms_own_site(store):
    company = _company(store, name="ДС Авто, автотехцентр", key="ds")
    client = make_client({
        "/": "<h1>ДС Авто</h1>",
        "/contacts": "info@ds-avto.ru",
    })
    with client:
        found = sitescan.scan("ds-avto.ru", client=client, belongs_to_contact=company)

    assert found.verified is True
    assert found.emails == ["info@ds-avto.ru"]


def test_enrich_respects_limit(store, monkeypatch):
    for index in range(5):
        _company(store, key=f"firma{index}.ru", site=f"firma{index}.ru")
    seen = []
    monkeypatch.setattr(
        sitescan, "scan", lambda site, **kw: seen.append(site) or sitescan.Found(pages=1)
    )
    sitescan.enrich(store, Kind.CLIENT, limit=2)
    assert len(seen) == 2


def test_placeholder_email_is_not_a_contact():
    """«example@email.com» — образец из формы, а не адрес компании.

    Такое письмо мы однажды отправили по-настоящему: адрес выглядел живым,
    а стоял в подсказке формы обратной связи.
    """
    html = """
      <p>Напишите нам: <a href="mailto:info@studio.ru">info@studio.ru</a></p>
      <input placeholder="example@email.com">
      <span>your-email@company.ru</span>
    """
    assert sitescan.extract_emails(html) == ["info@studio.ru"]


# --- Поисковик отказывает: не жечь проход --------------------------------------


def test_scan_stops_when_search_keeps_refusing(store, monkeypatch):
    """Треть базы теряется не из-за отсутствия сайта, а из-за антибота.

    Поисковик отдаёт заглушку — дальше он отдаст её и по всем остальным.
    Продолжать значит сжечь проход впустую и выглядеть как перебор запросов.
    """
    from orbit.models import Contact, Kind

    for number in range(10):
        store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"firm{number}", name=f"Фирма {number}",
            phones=["+79990000000"],
        ))

    tried = []

    def refuse(name, city=None, *, client=None):
        tried.append(name)
        raise sitescan.SearchUnavailable("поисковик ответил 202")

    monkeypatch.setattr(sitescan, "find_site", refuse)
    scanned, gained = sitescan.enrich(store, Kind.CLIENT, limit=10)

    assert gained == 0
    assert scanned == sitescan.SEARCH_GIVE_UP_AFTER
    assert len(tried) == sitescan.SEARCH_GIVE_UP_AFTER


def test_refused_companies_stay_in_the_queue(store, monkeypatch):
    """«Не смогли посмотреть» — не «сайта нет»: метку «уже ходили» не ставим."""
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="firm", name="Фирма", phones=["+79990000000"],
    ))
    monkeypatch.setattr(
        sitescan, "find_site",
        lambda *a, **kw: (_ for _ in ()).throw(sitescan.SearchUnavailable("202")),
    )
    sitescan.enrich(store, Kind.CLIENT, limit=1)

    contact = store.get_contact(pk)
    assert not sitescan._scanned(contact)
    assert "поиск недоступен" in contact.meta.get("site_scan_error", "")


def test_single_refusal_does_not_stop_the_pass(store, monkeypatch):
    """Одна осечка — не повод бросать: считаем только отказы подряд."""
    from orbit.models import Contact, Kind

    for number in range(4):
        store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"f{number}", name=f"Фирма {number}",
            phones=["+79990000000"],
        ))

    calls = {"n": 0}

    def flaky(name, city=None, *, client=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sitescan.SearchUnavailable("202")
        return None  # сайт честно не нашёлся

    monkeypatch.setattr(sitescan, "find_site", flaky)
    scanned, _ = sitescan.enrich(store, Kind.CLIENT, limit=4)
    assert scanned == 4


def test_search_pause_is_bigger_than_page_pause():
    """Поиском мы пользуемся у того, кто нас терпит, — темп должен быть другой."""
    assert min(sitescan.SEARCH_PAUSE) > sitescan.PAUSE_SECONDS * 2


# --- «Не открылся» и «чужой» — разные вещи --------------------------------------


def test_unreachable_site_is_not_called_foreign(monkeypatch):
    """Сайт не отдался ни одной страницей — мы ничего не увидели.

    Раньше это трактовалось как «сайт чужой»: ссылка на него стиралась,
    компания помечалась обойдённой, и живая фирма с прилёгшим на минуту
    сайтом теряла сайт навсегда. Ровно так вышло с «ап-сервис.рф».
    """
    from orbit.models import Contact, Kind

    class Dead:
        def get(self, url, **kwargs):
            raise httpx.ConnectError("сайт не отвечает")

    contact = Contact(kind=Kind.CLIENT, key="k", name="Фирма", site="firma.ru")
    found = sitescan.scan("firma.ru", client=Dead(), robots=_AllowAll(),
                          belongs_to_contact=contact)

    assert found.pages == 0
    assert found.retry is True          # вернёмся позже
    assert found.verified is True       # чужим не считаем
    assert "не открылся" in (found.error or "")


def test_unreachable_site_keeps_its_link(store, monkeypatch):
    """Метку «уже ходили» не ставим и ссылку на сайт не стираем."""
    from orbit.models import Contact, Kind

    contact = Contact(kind=Kind.CLIENT, key="k", name="Фирма", site="firma.ru",
                      phones=["+79990000000"])
    pk = store.upsert_contact(contact)
    contact = store.get_contact(pk)

    found = sitescan.Found(retry=True, error="сайт не открылся", verified=True)
    sitescan._apply(store, contact, found)

    fresh = store.get_contact(pk)
    assert fresh.site == "firma.ru"
    assert not sitescan._scanned(fresh)


def test_foreign_site_still_loses_its_link(store):
    """А вот подтверждённо чужой сайт по-прежнему отвязывается от компании."""
    from orbit.models import Contact, Kind

    contact = Contact(kind=Kind.CLIENT, key="k", name="Фирма", site="chuzhoy.ru",
                      phones=["+79990000000"])
    pk = store.upsert_contact(contact)
    contact = store.get_contact(pk)

    found = sitescan.Found(pages=1, verified=False, error="нашёлся чужой сайт")
    sitescan._apply(store, contact, found)

    fresh = store.get_contact(pk)
    assert fresh.site is None
    assert sitescan._scanned(fresh)


class _AllowAll:
    def allowed(self, url, client=None):
        return True

    def allows(self, *a, **kw):
        return True


# --- Пометка «есть ли сайт» не должна врать ------------------------------------


def test_found_site_updates_the_flag(store):
    """Модель не должна писать «у вас нет сайта» компании, у которой он есть.

    Пометка приезжает из выгрузки парсера и с тех пор не менялась. Обход
    находил сайт, а в письмо уходило «Есть ли сайт: нет, только карточка».
    """
    from orbit import llm
    from orbit.models import Channel, Contact, Kind

    contact = Contact(kind=Kind.CLIENT, key="k", name="Фирма", phones=["+79990000000"],
                      meta={"has_site": "нет, только карточка"})
    contact.site = "firma.ru"

    brief = llm.contact_brief(contact, Channel.EMAIL)
    assert "Есть ли сайт: есть" in brief
    assert "только карточка" not in brief


def test_flag_survives_when_there_is_really_no_site():
    from orbit import llm
    from orbit.models import Channel, Contact, Kind

    contact = Contact(kind=Kind.CLIENT, key="k", name="Фирма",
                      meta={"has_site": "нет, только карточка"})
    assert "нет, только карточка" in llm.contact_brief(contact, Channel.EMAIL)


def test_offline_tip_does_not_claim_a_missing_site(store):
    """Зацепка для владельца тоже не должна утверждать несуществующее."""
    from orbit.models import Contact, Kind
    from orbit.sections import clients

    contact = Contact(kind=Kind.CLIENT, key="k", name="Фирма", site="firma.ru",
                      meta={"has_site": "нет, только карточка"})
    assert "Сайта нет" not in clients._offline_tip(contact)


# --- Что остаётся в карточке после неудачного обхода ---------------------------


def _client_contact(store, **meta):
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="https://2gis.ru/firm/1", name="Улыбка, стоматология",
        city="Москва", meta={"has_site": "нет", **meta},
    ))
    return store.get_contact(pk)


def test_foreign_site_does_not_leave_the_has_site_mark(store, monkeypatch):
    """Сайт признан чужим и стёрт — пометка «сайт есть» обязана уйти вместе с ним.

    Пометку читает модель. С ней она пишет компании письмо, исходя из того,
    что сайт у той есть, — а мы этот сайт только что стёрли как чужой.
    """
    contact = _client_contact(store)
    monkeypatch.setattr(sitescan, "find_site", lambda *a, **kw: "chuzhoy.ru")
    monkeypatch.setattr(
        sitescan, "scan",
        lambda *a, **kw: sitescan.Found(pages=1, verified=False, error="нашёлся чужой сайт"),
    )

    sitescan._apply(store, contact, sitescan._one(store, contact, client=None, robots=None))

    fresh = store.get_contact(contact.pk)
    assert fresh.site is None
    assert fresh.meta.get("has_site") == "нет", fresh.meta
    assert sitescan.CANDIDATE_KEY not in fresh.meta, "на тот же чужой сайт возвращаться незачем"


def test_a_site_that_was_down_is_remembered_and_not_searched_again(store, monkeypatch):
    """Сайт нашёлся, но в тот момент лёг — адрес запоминаем, поиск не повторяем.

    Раньше найденный адрес пропадал вместе с проходом, а пометка «сайт есть»
    оставалась. Следующий проход платил поисковику за тот же ответ заново.
    """
    contact = _client_contact(store)
    searches: list[str] = []

    def search(name, city, client=None):
        searches.append(name)
        return "ulybka.ru"

    monkeypatch.setattr(sitescan, "find_site", search)
    monkeypatch.setattr(
        sitescan, "scan",
        lambda *a, **kw: sitescan.Found(retry=True, error="сайт не открылся"),
    )
    sitescan._apply(store, contact, sitescan._one(store, contact, client=None, robots=None))

    fresh = store.get_contact(contact.pk)
    assert fresh.meta.get(sitescan.CANDIDATE_KEY) == "ulybka.ru"
    assert fresh.meta.get("has_site") == "нет", "пока сайт не прочитан, обещать его нельзя"
    assert fresh.site is None, "непроверенный адрес в карточку не кладём"

    # Второй проход берёт запомненный адрес и поисковика не трогает.
    sitescan._one(store, fresh, client=None, robots=None)
    assert searches == ["Улыбка, стоматология"], "поиск должен был случиться ровно один раз"


def test_a_confirmed_site_sets_the_mark(store, monkeypatch):
    """Сайт нашёлся, открылся и подтвердился — вот теперь «сайт есть»."""
    contact = _client_contact(store)
    monkeypatch.setattr(sitescan, "find_site", lambda *a, **kw: "ulybka.ru")
    monkeypatch.setattr(
        sitescan, "scan",
        lambda *a, **kw: sitescan.Found(emails=["info@ulybka.ru"], pages=3, verified=True),
    )
    sitescan._apply(store, contact, sitescan._one(store, contact, client=None, robots=None))

    fresh = store.get_contact(contact.pk)
    assert fresh.meta.get("has_site") == "есть"
    assert fresh.emails == ["info@ulybka.ru"]
    assert sitescan.CANDIDATE_KEY not in fresh.meta


def test_dead_sites_do_not_stop_the_whole_pass(store, monkeypatch):
    """Три подряд неоткрывшихся сайта — не повод обрывать проход.

    Обрыв задуман против отказа ПОИСКОВИКА: он повторится на каждой следующей
    компании. Прилёгший сайт — беда одной компании, и три таких подряд на
    обычной базе встречаются постоянно. Раньше они останавливали весь проход,
    а в логе значилось, что виноват поиск.
    """
    from orbit.models import Contact, Kind

    for n in range(6):
        store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"https://2gis.ru/firm/{n}", name=f"Компания {n}",
            site=f"c{n}.ru",
        ))

    seen: list[str] = []

    def dead(store_, contact, client, robots):
        seen.append(contact.name)
        return sitescan.Found(retry=True, error="сайт не открылся")

    monkeypatch.setattr(sitescan, "_one", dead)
    walked, _gained = sitescan.enrich(store, Kind.CLIENT, limit=6)
    assert walked == 6, f"обход должен был дойти до конца, а прошёл {len(seen)}"


def test_a_refusing_search_still_stops_the_pass(store, monkeypatch):
    """А отказ поисковика проход по-прежнему останавливает."""
    from orbit.models import Contact, Kind

    for n in range(6):
        store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"https://2gis.ru/firm/{n}", name=f"Компания {n}",
        ))

    monkeypatch.setattr(
        sitescan, "_one",
        lambda *a, **kw: sitescan.Found(error="поиск недоступен", retry=True, search_failed=True),
    )
    walked, _gained = sitescan.enrich(store, Kind.CLIENT, limit=6)
    assert walked == sitescan.SEARCH_GIVE_UP_AFTER


def test_dead_sites_do_not_hog_the_head_of_the_crawl_queue(store, monkeypatch):
    """Двадцать мёртвых сайтов не должны занимать всю выборку каждый проход.

    Список кандидатов идёт по номеру в базе, берёт первые двадцать и отсеивает
    только тех, к кому уже ходили. Компании с намертво лежащими сайтами метки
    не получали — и следующие несколько тысяч не проверялись никогда.
    """
    from orbit.models import Contact, Kind

    for n in range(6):
        store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"https://2gis.ru/firm/{n}", name=f"Компания {n}",
            site=f"c{n}.ru",
        ))

    seen: list[str] = []

    def dead(store_, contact, client, robots):
        seen.append(contact.name)
        return sitescan.Found(retry=True, error="сайт не открылся")

    monkeypatch.setattr(sitescan, "_one", dead)

    sitescan.enrich(store, Kind.CLIENT, limit=3)
    first_pass = list(seen)
    seen.clear()
    sitescan.enrich(store, Kind.CLIENT, limit=3)

    assert first_pass, "первый проход должен кого-то взять"
    assert seen, "второй проход не должен остаться без кандидатов"
    assert set(first_pass) & set(seen) == set(), (
        f"второй проход снова взял тех же: {sorted(set(first_pass) & set(seen))}"
    )


def test_a_search_outage_does_not_send_the_company_to_rest(store, monkeypatch):
    """Отказ поисковика — беда общая, наказывать за неё компанию не за что."""
    from orbit.models import Contact, Kind

    store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="https://2gis.ru/firm/1", name="ДС Авто",
    ))
    monkeypatch.setattr(
        sitescan, "_one",
        lambda *a, **kw: sitescan.Found(error="поиск недоступен", retry=True, search_failed=True),
    )
    sitescan.enrich(store, Kind.CLIENT, limit=3)

    contact = store.find_contact(Kind.CLIENT, "https://2gis.ru/firm/1")
    assert sitescan.RETRY_KEY not in contact.meta


def test_a_confirmed_scan_clears_the_rest_mark(store, monkeypatch):
    """Сайт открылся — отметка отдыха снимается вместе с приговором."""
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="https://2gis.ru/firm/9", name="Улыбка", site="ulybka.ru",
        meta={sitescan.RETRY_KEY: "2020-01-01 00:00:00"},
    ))
    contact = store.get_contact(pk)
    sitescan._apply(store, contact, sitescan.Found(emails=["a@ulybka.ru"], pages=2, verified=True))

    fresh = store.get_contact(pk)
    assert sitescan.RETRY_KEY not in fresh.meta
    assert sitescan.SCANNED_KEY in fresh.meta


def test_the_scan_does_not_wipe_an_edit_made_while_it_walked(store, monkeypatch):
    """Между чтением контакта и записью проходят минуты обхода чужого сайта.

    Владелец за это время нажимает «С чего начать» или «Перезвонить». Запись
    всего словаря пометок целиком стирала его правку снимком минутной
    давности.
    """
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="https://2gis.ru/firm/1", name="Улыбка", site="ulybka.ru",
        meta={"rubric": "стоматология"},
    ))
    contact = store.get_contact(pk)  # снимок «до обхода»

    # Пока обход шёл, владелец отложил звонок из бота.
    store.update_meta(pk, {"remind_after": "2026-09-01", "remind_reason": "перезвонить"})

    sitescan._apply(store, contact, sitescan.Found(emails=["a@ulybka.ru"], pages=2, verified=True))

    fresh = store.get_contact(pk)
    assert fresh.meta.get("remind_after") == "2026-09-01", "правку владельца затёрли"
    assert fresh.meta.get("rubric") == "стоматология"
    assert sitescan.SCANNED_KEY in fresh.meta, "а свои пометки обход всё равно поставил"


def test_update_meta_touches_only_what_it_was_told(store):
    """update_meta меняет названное и не трогает остальное."""
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a.ru", name="А", meta={"rubric": "автосервис", "старый": "ключ"},
    ))
    result = store.update_meta(pk, {"новый": "ключ"}, drop=("старый",))

    assert result == {"rubric": "автосервис", "новый": "ключ"}
    assert store.get_contact(pk).meta == result


def test_a_cyrillic_address_is_taken_from_the_site():
    """«почта@компания.рф» — рабочий адрес российской компании, а не мусор.

    Проверка адреса кириллицу понимает и специально это оговаривает, а сборщик
    с сайта её не находил вовсе: с такого сайта не приносилось ни одного
    контакта, и компания оставалась без связи.
    """
    from orbit.sitescan import _EMAIL_RE

    assert _EMAIL_RE.findall("Пишите: почта@компания.рф") == ["почта@компания.рф"]
    assert _EMAIL_RE.findall("info@firma.ru") == ["info@firma.ru"]

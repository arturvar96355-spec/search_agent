"""Формы обратной связи.

Канал пока выключен, и главная проверка здесь именно про это: ничего не
должно уходить, пока владелец не разрешит. Остальное — разбор разметки:
что считаем формой обратной связи, а что поиском, подпиской или ловушкой
для роботов.
"""
from __future__ import annotations

import httpx
import pytest

from orbit import webform
from orbit.models import Contact, Kind

FEEDBACK_HTML = """
<form action="/send.php" method="post">
  <input type="hidden" name="csrf" value="abc123">
  <input type="text" name="name" placeholder="Ваше имя" required>
  <input type="tel" name="phone" placeholder="Телефон" required>
  <textarea name="message" placeholder="Сообщение"></textarea>
  <input type="submit" value="Отправить">
</form>
"""

SEARCH_HTML = """
<form action="/search" method="get" id="search-form">
  <input type="text" name="q">
</form>
"""


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(webform, "PAUSE_SECONDS", 0)


def company() -> Contact:
    return Contact(kind=Kind.CLIENT, key="firma.ru", name="Фирма", site="firma.ru")


# --- Отправка выключена ----------------------------------------------------

def test_submit_refuses_while_disabled():
    """Пока владелец не включил канал, заявка не уходит ни при каких условиях."""
    form = webform.parse_forms(FEEDBACK_HTML, "https://firma.ru/contacts")[0]
    with pytest.raises(webform.Disabled):
        webform.submit(form, company(), "Здравствуйте!")


def test_scout_does_not_send_anything(store, monkeypatch):
    """Разведка только смотрит: вызов отправки — это ошибка."""
    contact = company()
    contact.phones = ["+74951234567"]
    contact.pk = store.upsert_contact(contact)

    monkeypatch.setattr(webform, "submit", lambda *a, **kw: pytest.fail("отправлять нельзя"))
    monkeypatch.setattr(
        webform, "find",
        lambda site, **kw: webform.parse_forms(FEEDBACK_HTML, "https://firma.ru/contacts")[0],
    )
    looked, found = webform.scout(store, Kind.CLIENT)

    assert (looked, found) == (1, 1)
    assert "send.php" in store.get_contact(contact.pk).meta[webform.FORM_KEY]


# --- Что считаем формой обратной связи -------------------------------------

def test_feedback_form_is_recognized():
    form = webform.parse_forms(FEEDBACK_HTML, "https://firma.ru/contacts")[0]
    assert form.usable is True
    assert form.roles == {"name", "phone", "message"}
    assert form.action == "https://firma.ru/send.php"
    assert form.method == "post"


def test_search_form_is_not_feedback():
    assert webform.parse_forms(SEARCH_HTML, "https://firma.ru/") == []


def test_subscription_form_is_not_feedback():
    html = '<form action="/newsletter" method="post"><input name="email"></form>'
    assert webform.parse_forms(html, "https://firma.ru/") == []


def test_login_form_is_skipped():
    html = (
        '<form action="/enter" method="post">'
        '<input name="email"><input type="password" name="pass">'
        '<textarea name="message"></textarea></form>'
    )
    assert webform.parse_forms(html, "https://firma.ru/") == []


def test_form_without_message_field_is_not_usable():
    html = '<form action="/call"><input name="phone" type="tel"></form>'
    form = webform.parse_forms(html, "https://firma.ru/")[0]
    assert form.usable is False


def test_honeypot_field_is_not_filled():
    """Скрытое поле-ловушка: заполнил — заявка уехала в спам."""
    html = (
        '<form action="/send"><input name="winnie" style="display:none">'
        '<input name="phone" type="tel"><textarea name="message"></textarea></form>'
    )
    form = webform.parse_forms(html, "https://firma.ru/")[0]
    assert [f.name for f in form.fields] == ["phone", "message"]


# --- Что подставляем -------------------------------------------------------

def test_fill_puts_our_contacts_and_text(monkeypatch):
    settings = webform.get_settings()
    monkeypatch.setattr(settings, "smtp_user", "partnership@uniqore.ru")
    monkeypatch.setattr(settings, "smtp_from_name", "UniqOre")
    monkeypatch.setattr(settings, "contact_phone", "+79990001122")

    form = webform.parse_forms(FEEDBACK_HTML, "https://firma.ru/contacts")[0]
    values = webform.fill(form, company(), "Здравствуйте! Сделали бы вам сайт.")

    assert values["name"] == "UniqOre"
    assert values["phone"] == "+79990001122"
    assert values["message"].startswith("Здравствуйте")
    # Служебное поле переносим как есть, иначе форма не примет заявку.
    assert values["csrf"] == "abc123"


def test_fill_never_invents_data(monkeypatch):
    """Нет телефона в настройках — подставляем пустое, а не выдуманный номер."""
    monkeypatch.setattr(webform.get_settings(), "contact_phone", None)
    form = webform.parse_forms(FEEDBACK_HTML, "https://firma.ru/contacts")[0]
    assert webform.fill(form, company(), "текст")["phone"] == ""


# --- Поиск формы на сайте --------------------------------------------------

def test_find_walks_to_contacts_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/contacts":
            return httpx.Response(200, text=FEEDBACK_HTML)
        return httpx.Response(200, text=SEARCH_HTML)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        form = webform.find("firma.ru", client=client)

    assert form is not None
    assert form.url.endswith("/contacts")


def test_find_returns_none_when_there_is_no_form():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<html>просто страница</html>")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        assert webform.find("firma.ru", client=client) is None


def test_thank_you_page_is_recognized():
    assert webform.looks_accepted("<h1>Спасибо!</h1> Мы свяжемся с вами.") is True
    assert webform.looks_accepted("<h1>Ошибка 500</h1>") is False


def test_submit_works_when_enabled(monkeypatch):
    """Включённый канал реально отправляет — проверяем на поддельном сервере."""
    monkeypatch.setattr(webform.get_settings(), "webform_enabled", True)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, text="Спасибо!")

    form = webform.parse_forms(FEEDBACK_HTML, "https://firma.ru/contacts")[0]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert webform.submit(form, company(), "Здравствуйте!", client=client) is True

    assert seen["url"] == "https://firma.ru/send.php"
    assert "message=" in seen["body"]


# --- «Не смогли посмотреть» ≠ «посмотрели, формы нет» --------------------------


def test_a_site_that_will_not_open_is_not_a_site_without_a_form():
    """Ни одна страница не открылась — это не ответ на вопрос «есть ли форма»."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("сайт лежит")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, follow_redirects=True) as client, \
            pytest.raises(webform.SiteUnreachable):
        webform.find("firma.ru", client=client)


def test_scout_does_not_write_the_verdict_for_an_unreachable_site(store, monkeypatch):
    """Пометка «формы нет» ставится навсегда — по молчащему сайту её ставить нельзя.

    С ней компания выпадает из обхода насовсем: фильтр берёт только тех, у
    кого пометки ещё нет. Сайт, прилёгший на минуту, терял форму обратной
    связи вместе со всеми будущими проходами.
    """
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="https://2gis.ru/firm/1", name="Улыбка", site="ulybka.ru",
    ))

    def down(site, *, client=None, robots=None):
        raise webform.SiteUnreachable("сайт лежит")

    monkeypatch.setattr(webform, "find", down)
    walked, found = webform.scout(store, Kind.CLIENT, limit=5)

    assert (walked, found) == (1, 0)
    assert webform.FORM_KEY not in store.get_contact(pk).meta, (
        "приговор по несмотренному сайту — это враньё в карточке"
    )


def test_scout_writes_the_verdict_when_the_site_did_open(store, monkeypatch):
    """А если сайт открылся и формы там нет — пометка ставится, и это правда."""
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="https://2gis.ru/firm/2", name="Улыбка", site="ulybka.ru",
    ))
    monkeypatch.setattr(webform, "find", lambda site, **kw: None)
    webform.scout(store, Kind.CLIENT, limit=5)

    assert store.get_contact(pk).meta.get(webform.FORM_KEY) == "нет"

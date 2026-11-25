"""Дверь мини-аппа. Адрес туннеля публичный — за ней вся база и переписка.

Здесь проверяется ровно одно: чужого не пускают. Каждый тест — это способ,
которым чужой мог бы войти.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from orbit.webapp import auth

TOKEN = "8267888669:TESTTOKENTESTTOKEN"
OWNER = "1251473751"


@pytest.fixture(autouse=True)
def bot_settings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    from orbit import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def init_data(*, chat_id: str = OWNER, when: float | None = None, token: str = TOKEN) -> str:
    fields = {
        "auth_date": str(int(when if when is not None else time.time())),
        "query_id": "AAxxx",
        "user": json.dumps({"id": int(chat_id), "first_name": "Сергей"}),
    }
    checked = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_the_owner_gets_in():
    visitor = auth.check(init_data())
    assert visitor.chat_id == OWNER
    assert not visitor.dev


def test_a_stranger_with_a_valid_signature_is_refused():
    """Подпись настоящая, но человек чужой: токен бота знает и сам Telegram.

    Любой, кто откроет бота, получит от Telegram законную подпись со СВОИМ
    id. Без проверки владельца этого хватило бы, чтобы увидеть всю базу.
    """
    with pytest.raises(auth.NotAllowed):
        auth.check(init_data(chat_id="999000111"))


def test_a_forged_signature_is_refused():
    with pytest.raises(auth.NotAllowed):
        auth.check(init_data(token="777:ЧУЖОЙТОКЕН"))


def test_a_tampered_field_breaks_the_signature():
    """Подменили id прямо в строке — подпись обязана разойтись."""
    raw = init_data()
    spoiled = raw.replace("%D0%A1%D0%B5%D1%80%D0%B3%D0%B5%D0%B9", "%D0%92%D0%BE%D1%80")
    if spoiled == raw:  # имя закодировалось иначе — подменяем дату
        spoiled = raw.replace("auth_date=", "auth_date=1")
    with pytest.raises(auth.NotAllowed):
        auth.check(spoiled)


def test_an_old_link_stops_working():
    """Подсмотренная однажды ссылка не должна работать вечно."""
    old = time.time() - auth.MAX_AGE_SECONDS - 60
    with pytest.raises(auth.NotAllowed):
        auth.check(init_data(when=old))


def test_without_init_data_nobody_gets_in():
    with pytest.raises(auth.NotAllowed):
        auth.check("", client_host="127.0.0.1")


def test_dev_door_opens_only_on_localhost(monkeypatch):
    monkeypatch.setenv(auth.DEV_ENV, "1")
    assert auth.check("", client_host="127.0.0.1").dev
    with pytest.raises(auth.NotAllowed):
        auth.check("", client_host="203.0.113.7")


def test_dev_door_is_closed_unless_switched_on(monkeypatch):
    monkeypatch.delenv(auth.DEV_ENV, raising=False)
    with pytest.raises(auth.NotAllowed):
        auth.check("", client_host="127.0.0.1")


@pytest.mark.parametrize("hash_value", ["подделка", "", "zz", "hash с пробелом", "✓" * 64])
def test_a_junk_signature_is_a_refusal_not_a_crash(hash_value):
    """Мусор в подписи — это отказ, а не пятисотка.

    compare_digest на строках бросает TypeError, если в них есть не-ASCII, и
    подпись кириллицей роняла проверку. Снаружи, на публичном адресе туннеля,
    это выглядит как сломанный сервер и подсказывает, чем ломать дальше.
    """
    raw = init_data()
    spoiled = raw.split("&hash=")[0] + "&hash=" + hash_value
    with pytest.raises(auth.NotAllowed):
        auth.check(spoiled)


# --- Страница целиком ---------------------------------------------------------

def test_the_page_arrives_in_one_piece():
    """Стили, скрипт и знак — внутри страницы.

    Когда они шли отдельными запросами, на рваном туннеле терялся хоть один,
    и владелец видел вёрстку без скрипта: всё на месте, ничего не нажимается.
    """
    from orbit.webapp.api import _page

    page = _page()
    assert '<link rel="stylesheet" href="/app.css">' not in page
    assert '<script src="/app.js"></script>' not in page
    assert "<style>" in page and "window.ORBIT_LOGO=" in page
    assert "loaders.home" in page, "скрипт должен быть внутри"


def test_the_page_is_never_cached():
    """Telegram держит мини-апп в кэше и после правки показывает старую версию."""
    from fastapi.testclient import TestClient

    from orbit.webapp.api import app

    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")


def test_ping_needs_no_signature_and_shows_no_data():
    from fastapi.testclient import TestClient

    from orbit.webapp.api import app

    response = TestClient(app).get("/api/ping")
    assert response.status_code == 200
    assert set(response.json()) == {"ok", "build"}


def test_data_still_needs_a_signature():
    from fastapi.testclient import TestClient

    from orbit.webapp.api import app

    assert TestClient(app).get("/api/overview").status_code == 403

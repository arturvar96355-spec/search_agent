"""Адрес мини-аппа: где он живёт и когда считается мёртвым.

Туннель поднимается заново при каждом запуске, и адрес каждый раз новый.
Владелец не должен ни копировать его, ни лезть в настройки бота — кнопка
появляется в боте сама. Ровно поэтому важно, чтобы она НЕ появлялась, когда
туннеля нет: нажатие на мёртвый адрес открывает белый экран, и человек идёт
искать поломку там, где её нет.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from orbit.store import MSK
from orbit.ui import WEBAPP_PREFIX, Screen
from orbit.webapp import link

LIVE = "https://greensboro-cave.trycloudflare.com"


def test_a_fresh_address_is_offered(store):
    link.remember(store, LIVE)
    assert link.current(store) == LIVE


def test_no_tunnel_means_no_button(store):
    assert link.current(store) is None


def test_a_yesterday_address_is_not_offered(store):
    """Туннель живёт, пока открыто окно терминала. Вчерашний адрес мёртв."""
    link.remember(store, LIVE)
    old = (datetime.now(MSK) - timedelta(hours=link.STALE_HOURS + 1)).isoformat()
    store.set_state(link.AT_KEY, old)
    assert link.current(store) is None


def test_a_plain_http_address_is_refused(store):
    """Telegram открывает мини-апп только по https.

    Подсунув ему http, мы получили бы молчаливо неработающую кнопку — хуже,
    чем её отсутствие: непонятно, сломан пульт, бот или телефон.
    """
    link.remember(store, "http://127.0.0.1:8137")
    assert link.current(store) is None


def test_the_button_carries_the_signature(store):
    """Мини-апп открывается кнопкой web_app, а не обычной ссылкой.

    Разница не косметическая: только такая кнопка передаёт приложению
    подпись Telegram, по которой оно узнаёт владельца. Открытая ссылкой, та
    же страница увидит гостя без подписи и никого не пустит.
    """
    screen = Screen("текст", [[("🖥 Открыть пульт", f"{WEBAPP_PREFIX}{LIVE}")]])
    button = screen.markup["inline_keyboard"][0][0]
    assert button["web_app"] == {"url": LIVE}
    assert "url" not in button and "callback_data" not in button


def test_the_bot_explains_what_to_do_when_the_tunnel_is_down(store):
    from orbit.bot import app_screen

    screen = app_screen(store)
    assert "не запущен" in screen.text
    assert "miniapp.sh" in screen.text, "владельцу нужна команда, а не диагноз"


def test_the_bot_offers_the_button_when_the_tunnel_is_up(store):
    from orbit.bot import app_screen

    link.remember(store, LIVE)
    screen = app_screen(store)
    assert any(
        data == f"{WEBAPP_PREFIX}{LIVE}" for row in screen.rows for _label, data in row
    ), screen.rows

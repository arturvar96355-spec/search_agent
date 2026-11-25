"""Тесты разбора прокси. Реальные доступы в тестах не участвуют."""
from __future__ import annotations

import pytest

from fbid.proxy import parse_proxy


def test_parse_cabinet_format():
    """Строка из кабинета LTE Space: host:port:логин:пароль."""
    proxy = parse_proxy("p10101.example.net:10101:userxx:passyy")
    assert proxy is not None
    assert (proxy.host, proxy.port) == ("p10101.example.net", 10101)
    assert proxy.user == "userxx"
    assert proxy.playwright() == {
        "server": "http://p10101.example.net:10101",
        "username": "userxx",
        "password": "passyy",
    }


def test_parse_url_format():
    proxy = parse_proxy("http://userxx:passyy@1.2.3.4:8080")
    assert proxy is not None
    assert proxy.url == "http://userxx:passyy@1.2.3.4:8080"


def test_password_never_leaks_into_safe_view():
    """safe идёт в логи — пароля там быть не должно."""
    proxy = parse_proxy("p10101.example.net:10101:userxx:s3cr3t")
    assert "s3cr3t" not in proxy.safe
    assert "userxx" not in proxy.safe


def test_empty_means_no_proxy():
    assert parse_proxy(None) is None
    assert parse_proxy("") is None
    assert parse_proxy("   ") is None


def test_garbage_raises():
    with pytest.raises(ValueError):
        parse_proxy("просто_текст")
    with pytest.raises(ValueError):
        parse_proxy("socks5://user:pass@1.2.3.4:1080")  # Chromium не потянет

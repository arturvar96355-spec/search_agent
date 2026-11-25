"""Прокси: разбор строки доступа и приведение к формату Chromium/httpx.

Мобильные (LTE) прокси — правильный выбор под авторизованные сессии бирж: такой IP
неотличим от обычного мобильного абонента. Но у них есть автозамена IP, и под живой
сессией её надо держать выключенной — иначе биржа увидит прыжки IP и разлогинит.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from loguru import logger

# Формат из кабинета LTE Space: host:port:user:pass
_RAW_RE = re.compile(r"^(?P<host>[^:\s]+):(?P<port>\d+):(?P<user>[^:\s]+):(?P<password>[^:\s]+)$")
# Обычный URL: http://user:pass@host:port
_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?:(?P<user>[^:@/]+):(?P<password>[^@/]+)@)?(?P<host>[^:/@]+):(?P<port>\d+)/?$"
)


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    user: str | None = None
    password: str | None = None
    scheme: str = "http"

    @property
    def url(self) -> str:
        """Полный URL с логином — для httpx. НЕ логировать."""
        auth = f"{self.user}:{self.password}@" if self.user else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def safe(self) -> str:
        """Как показывать в логах: без пароля."""
        return f"{self.scheme}://{self.host}:{self.port}"

    def playwright(self) -> dict:
        """Chromium принимает логин отдельным полем, а не внутри URL."""
        config: dict = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.user:
            config["username"] = self.user
            config["password"] = self.password
        return config


def parse_proxy(raw: str | None) -> Proxy | None:
    """Понимает оба формата: host:port:user:pass и http://user:pass@host:port."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    match = _RAW_RE.match(raw)
    if match:
        return Proxy(
            host=match["host"],
            port=int(match["port"]),
            user=match["user"],
            password=match["password"],
        )

    match = _URL_RE.match(raw)
    if match:
        return Proxy(
            host=match["host"],
            port=int(match["port"]),
            user=match["user"],
            password=match["password"],
            # Chromium не умеет socks5 с авторизацией, поэтому только http/https.
            scheme=match["scheme"],
        )

    raise ValueError(
        f"Не разобрал прокси: {raw!r}. Ожидаю host:port:user:pass "
        f"или http://user:pass@host:port"
    )


IP_CHECK_URL = "https://api.ipify.org?format=json"


def check(proxy: Proxy, *, timeout: float = 20.0) -> str | None:
    """Проверяет, что через прокси есть интернет. Возвращает внешний IP или None."""
    try:
        resp = httpx.get(IP_CHECK_URL, proxy=proxy.url, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("ip")
    except Exception as exc:  # noqa: BLE001 — любая сетевая ошибка здесь значит «прокси не работает»
        logger.error("Прокси {} не отвечает: {}", proxy.safe, exc)
        return None


def direct_ip(*, timeout: float = 20.0) -> str | None:
    """Внешний IP без прокси — чтобы сравнить и убедиться, что прокси реально применяется."""
    try:
        return httpx.get(IP_CHECK_URL, timeout=timeout).json().get("ip")
    except Exception:  # noqa: BLE001 — не определили свой IP — не повод падать
        return None

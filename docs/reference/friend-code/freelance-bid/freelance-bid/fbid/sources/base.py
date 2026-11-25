"""Общее для всех площадок: HTTP-клиент и интерфейс источника."""
from __future__ import annotations

import re
from html import unescape
from typing import Protocol

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..models import Order

# Обычный десктопный UA: биржи отдают ботам урезанную вёрстку или 403.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError,)),
    reraise=True,
)
def http_get(url: str, *, timeout: float = 25.0) -> httpx.Response:
    """GET с ретраями. Сетевые сбои у бирж — норма, а не повод падать."""
    resp = httpx.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        follow_redirects=True,
        proxy=_feed_proxy_url(),
    )
    resp.raise_for_status()
    return resp


def _feed_proxy_url() -> str | None:
    """Прокси для открытых лент — только если явно включён PROXY_FOR_FEED."""
    from ..config import get_settings
    from ..proxy import parse_proxy

    settings = get_settings()
    if not settings.proxy_for_feed:
        return None
    proxy = parse_proxy(settings.proxy_url)
    return proxy.url if proxy else None


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")


def html_to_text(fragment: str) -> str:
    """Грубо, но надёжно: HTML-кусок → читаемый текст."""
    text = _BR_RE.sub("\n", fragment)
    text = text.replace("</p>", "\n").replace("</div>", "\n")
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


class SourceAdapter(Protocol):
    """Что должна уметь площадка. Отклик добавится на шаге 4."""

    name: str

    def fetch_feed(self) -> list[Order]:
        """Свежие заказы из ленты. Описание может быть обрезанным."""

    def fetch_details(self, order: Order) -> Order:
        """Догружает полное ТЗ и всё, чего нет в ленте."""

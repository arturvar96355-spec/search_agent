"""Источник «каталоги агентств» (режим partners): листинг -> название + сайт агентства.

Поддержаны (разметка сверена на живых страницах 2026-07-30):
  • ratingruneta.ru — данные листинга лежат в SSR-состоянии `window.__INITIAL_STATE__`
    (в самой разметке классы обфусцированы, внешних ссылок нет). Оттуда же берём полезные
    метрики: место в рейтинге и ценовой сегмент — сигнал размера агентства.
  • ruward.ru — обычный HTML: ссылки `a.companyratingname` (текст = название, href = сайт).

С листинга берём ТОЛЬКО название + ссылку на сайт (как в ТЗ). Контакты собирает providers/site.py.
Пагинацию `?page=` у ratingruneta не трогаем — она закрыта в их robots.txt.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from loguru import logger

from ..config import Settings, get_settings
from ..contacts import looks_like_big_agency
from ..models import Agency
from .site import _USER_AGENT, RobotsCache

# Ссылки каталога/сервисные — не сайты агентств.
# Домены каталогов/соцсетей/агрегаторов — это не сайты агентств.
# Сравниваем ПО ХОСТУ (а не подстрокой): подстрочное сравнение выбрасывало валидные
# домены вида «best-t.me-agency.ru» и пропускало соцсети, которых нет в списке.
_SKIP_HOSTS = frozenset({
    "ratingruneta.ru", "ruward.ru", "workspace.ru", "cmsmagazine.ru", "tagline.ru",
    "vk.com", "t.me", "telegram.me", "telegram.org", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "vc.ru", "habr.com", "linkedin.com", "ok.ru", "dzen.ru",
    "wa.me", "whatsapp.com", "wixsite.com", "tilda.ws", "taplink.cc", "clutch.co",
    "google.com", "yandex.ru", "ya.ru", "avito.ru", "hh.ru", "profi.ru", "youla.ru",
})


def _is_agency_site(url: str) -> bool:
    """Внешняя ссылка ведёт на сайт агентства (а не на каталог/соцсеть/агрегатор)?"""
    if not url or not url.startswith("http"):
        return False
    host = (urlparse(url).netloc or "").lower().removeprefix("www.").rstrip(".")
    if not host:
        return False
    # Отсекаем и сам домен из списка, и его поддомены (msk.workspace.ru).
    parts = host.split(".")
    return all(".".join(parts[i:]) not in _SKIP_HOSTS for i in range(len(parts) - 1))


def _clean_js_escapes(raw: str) -> str:
    """Убираем из JS-литерала escape-последовательности, недопустимые в строгом JSON.

    Идём по строке и сохраняем валидные пары (\\\\, \\", \\n, \\uXXXX…) как есть — наивная
    замена по regex ломала экранированный бэкслеш и вместе с ним весь JSON.
    """
    valid = set('"\\/bfnrtu')
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in valid:
                out.append(ch)
                out.append(nxt)
            else:
                out.append(nxt)  # выкидываем только сам недопустимый слэш
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _initial_state(html: str) -> dict | None:
    """Достаём window.__INITIAL_STATE__ (JS-литерал -> dict). None, если состояния нет.

    Ищем внутри конкретного <script>, а не по всему документу: иначе «последняя }» уедет
    в чужой скрипт дальше по странице и JSON не соберётся.
    """
    try:
        from selectolax.parser import HTMLParser  # опциональная зависимость
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("selectolax не установлен. Поставь: pip install -e '.[partners]'") from exc

    script = ""
    for node in HTMLParser(html).css("script"):
        text = node.text() or ""
        if "__INITIAL_STATE__" in text:
            script = text
            break
    if not script or "{" not in script:
        return None
    raw = script[script.index("{"):]
    end = raw.rfind("}")
    if end < 0:
        return None
    raw = raw[: end + 1]
    # В JS-литерале бывают escape-последовательности, недопустимые в строгом JSON
    # (\\x41, \\u2028, \\'). Валидные пары (в т.ч. \\\\) не трогаем — иначе портим данные.
    cleaned = _clean_js_escapes(raw)
    for candidate in (cleaned, cleaned[: cleaned.rfind("}") + 1]):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    logger.warning("Каталог: не разобрал __INITIAL_STATE__ — разметка могла измениться.")
    return None


def parse_ratingruneta(html: str, city: str | None = None) -> list[Agency]:
    """Листинг «Рейтинга Рунета» -> агентства. Данные в SSR-состоянии, не в разметке."""
    state = _initial_state(html)
    if not state:
        return []
    try:
        tables = state["rating_page_new"]["page"]["rating"]["rating_tables"]
    except (KeyError, TypeError):
        logger.warning("Каталог ratingruneta: не нашёл rating_tables — структура изменилась.")
        return []

    agencies: list[Agency] = []
    for table in tables or []:
        if not isinstance(table, dict):
            continue  # структура могла поменяться — не роняем прогон
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            link = str(row.get("link") or "").strip()
            if not name or not _is_agency_site(link):
                continue  # без имени/внешнего сайта запись бесполезна — ничего не выдумываем
            note_parts = [p for p in (
                f"место {row.get('place')}" if row.get("place") else "",
                row.get("segment_name") or "",
                f"кейсов {row.get('cases_cnt')}" if row.get("cases_cnt") else "",
            ) if p]
            catalog_info = " · ".join(note_parts) or None
            agencies.append(
                Agency(
                    name=name,
                    website=link,  # валидатор модели сам приведёт к домену
                    city=city,
                    source="ratingruneta",
                    catalog_info=catalog_info,
                    # Крупный сетевой игрок (тендеры, свой продакшн) — по брифу не наша цель.
                    big_agency=looks_like_big_agency(name, catalog_info),
                )
            )
    return agencies


def parse_ruward(html: str, city: str | None = None) -> list[Agency]:
    """Листинг Ruward -> агентства (обычный HTML: a.companyratingname)."""
    try:
        from selectolax.parser import HTMLParser  # опциональная зависимость
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("selectolax не установлен. Поставь: pip install -e '.[partners]'") from exc

    agencies: list[Agency] = []
    for node in HTMLParser(html).css("a.companyratingname"):
        name = node.text(strip=True)
        link = (node.attributes.get("href") or "").strip()
        if not name or not _is_agency_site(link):
            continue
        agencies.append(Agency(
            name=name, website=link, city=city, source="ruward",
            big_agency=looks_like_big_agency(name),
        ))
    return agencies


# Парсер листинга по домену каталога.
CATALOG_PARSERS: dict[str, callable] = {
    "ratingruneta.ru": parse_ratingruneta,
    "ruward.ru": parse_ruward,
}


def parse_catalog(url: str, html: str, city: str | None = None) -> list[Agency]:
    """Разбираем листинг подходящим парсером (по домену URL)."""
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    for domain, parser in CATALOG_PARSERS.items():
        if host.endswith(domain):
            return parser(html, city)
    logger.warning("Каталог {}: парсер не найден (поддержаны: {}).", host, ", ".join(CATALOG_PARSERS))
    return []


async def fetch_catalog(
    url: str,
    client: httpx.AsyncClient,
    settings: Settings | None = None,
    robots: RobotsCache | None = None,
    city: str | None = None,
) -> list[Agency]:
    """Скачиваем страницу листинга (с проверкой robots.txt) и разбираем её в список агентств."""
    settings = settings or get_settings()
    robots = robots or RobotsCache()
    if not await robots.allowed(client, url):
        logger.warning("Каталог: robots.txt запрещает {} — пропускаю.", url)
        return []
    try:
        response = await client.get(url, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — недоступный каталог пропускаем
        logger.warning("Каталог {} не открылся: {}", url, exc)
        return []
    if response.status_code != 200:
        logger.warning("Каталог {}: HTTP {}", url, response.status_code)
        return []

    agencies = parse_catalog(url, response.text, city)
    logger.info("Каталог {}: {} агентств с сайтом.", url, len(agencies))
    return agencies


async def fetch_catalogs(
    urls: list[str],
    settings: Settings | None = None,
    city: str | None = None,
) -> list[Agency]:
    """Несколько листингов подряд, аккуратно (пауза между запросами из настроек)."""
    import asyncio

    settings = settings or get_settings()
    robots = RobotsCache()
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    collected: list[Agency] = []
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers=headers) as client:
        for url in urls:
            collected.extend(await fetch_catalog(url, client, settings, robots, city))
            await asyncio.sleep(settings.request_delay)
    return collected

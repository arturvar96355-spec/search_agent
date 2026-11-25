"""Источник «поисковая выдача» (режим partners): запросы -> домены агентств.

Почему DuckDuckGo, а не Google/Яндекс: их robots.txt ПРЯМО ЗАПРЕЩАЕТ парсинг /search
(проверено 2026-07-30), а html.duckduckgo.com/html/ — разрешает. Работаем только там,
где нам разрешено; обходить запреты не пытаемся.

С выдачи берём только домен + заголовок. Контакты собирает providers/site.py, как и для каталогов.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from loguru import logger

from ..config import Settings, get_settings
from ..contacts import looks_like_big_agency
from ..models import Agency
from .catalogs import _is_agency_site
from .site import _USER_AGENT, RobotsCache

_SEARCH_URL = "https://html.duckduckgo.com/html/"

# Медиа, обзоры и агрегаторы: в выдаче их много, но это не сайты агентств.
_NOT_AGENCY_HOSTS = frozenset({
    "sostav.ru", "adindex.ru", "cossa.ru", "rb.ru", "forbes.ru", "kommersant.ru", "rbc.ru",
    "sberbank.ru", "tinkoff.ru", "vtb.ru", "wikipedia.org", "zoon.ru", "otzovik.com",
    "irecommend.ru", "spark.ru", "pikabu.ru", "livejournal.com", "medium.com",
    "prodoctorov.ru", "napopravku.ru", "docdoc.ru", "sberhealth.ru",
})

# Готовые запросы под наши ниши: ищем агентства, которые уже работают с нашими клиентами.
SEARCH_QUERIES: dict[str, list[str]] = {
    # Ключи обязаны совпадать с нишами каталогов (partners.CATALOG_URLS) — иначе
    # --source all по такой нише молча соберёт только каталоги. Проверяется тестом.
    "smm": [
        "smm агентство продвижение в соцсетях услуги",
        "агентство социальных сетей ведение аккаунтов бизнес",
        "smm агентство для малого бизнеса заявки",
    ],
    "medicine": [
        "агентство маркетинга для стоматологий",
        "медицинский маркетинг агентство клиники",
        "продвижение стоматологии агентство",
        "маркетинговое агентство для медицинских центров",
    ],
    "auto": [
        "продвижение автосервисов агентство",
        "маркетинговое агентство автобизнес реклама",
        "агентство маркетинга для автосалонов",
    ],
    "horeca": [
        "маркетинг для ресторанов агентство",
        "продвижение доставки еды агентство",
        "smm агентство для кафе и ресторанов",
    ],
    "beauty": [
        "продвижение салонов красоты агентство",
        "маркетинг для косметологии агентство",
    ],
    "realty": [
        "маркетинговое агентство недвижимость лидогенерация",
        "продвижение агентства недвижимости реклама",
    ],
    "general": [
        "агентство лидогенерации для малого бизнеса",
        "агентство performance маркетинга заявки",
    ],
}


def search_queries_for(niche: str) -> list[str]:
    """Запросы под нишу. 'all' -> все запросы (без дублей, в порядке объявления)."""
    if niche == "all":
        seen: list[str] = []
        for queries in SEARCH_QUERIES.values():
            for query in queries:
                if query not in seen:
                    seen.append(query)
        return seen
    return SEARCH_QUERIES.get(niche, [])


# Заголовки, которые не называют компанию вовсе.
_JUNK_TITLES = frozenset({
    "главная", "главная страница", "home", "index", "домашняя страница",
    "официальный сайт", "о компании", "контакты",
})


def _clean_title(title: str, host: str) -> str:
    """Заголовок выдачи → название агентства (или домен, если заголовок мусорный).

    Из «Маркетинг для клиник — Агентство X» нужно «Агентство X», а не первая
    половина. Раньше бралась именно она, то есть в название попадал СЛОГАН:
    он уходил в холодное письмо («Здравствуйте, Маркетинг для клиник!») и мимо
    него проходил стоп-лист крупных агентств, который сверяется по имени.

    Как выбираем. Заголовки сайтов бывают и «Слоган — Бренд», и «Бренд —
    Слоган», так что по месту не угадать. Зато бренд почти всегда перекликается
    с доменом — по нему и узнаём. Не перекликается ни один кусок — берём самый
    короткий: слоган длиннее названия практически всегда.
    """
    parts = [
        re.sub(r"\s+", " ", piece).strip()
        for piece in re.split(r"\s[—–|:•]\s|\s[-]\s", title.strip())
    ]
    parts = [piece for piece in parts if 3 <= len(piece) <= 70]
    if not parts:
        return host

    core = _host_core(host)
    if core:
        for piece in parts:
            if core in _letters(piece):
                return piece

    shortest = min(parts, key=len)
    # Ничего похожего на домен не нашлось. Отдаём домен, если и оставшийся
    # кусок названием не выглядит: «Главная» и «Комплексный интернет-маркетинг
    # для среднего бизнеса» — это заголовок страницы, а не имя компании, и
    # обращение к нему в письме читается как ошибка робота.
    if shortest.strip().lower() in _JUNK_TITLES or len(shortest.split()) > 4:
        return host
    return shortest


def _letters(text: str) -> str:
    """Только буквы и цифры в нижнем регистре — для сравнения с доменом."""
    return re.sub(r"[^0-9a-zа-яё]", "", (text or "").lower())


def _host_core(host: str) -> str:
    """Значимая часть домена: без www и без зоны."""
    parts = [piece for piece in (host or "").lower().split(".") if piece and piece != "www"]
    if len(parts) <= 1:
        return _letters(parts[0]) if parts else ""
    return _letters(parts[-2])


def _result_url(href: str) -> str:
    """DDG иногда оборачивает ссылку в /l/?uddg=… — достаём реальный URL."""
    if not href:
        return ""
    if "uddg=" in href:
        target = parse_qs(urlparse(href).query).get("uddg")
        if target:
            return unquote(target[0])
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_search_results(html: str, city: str | None = None) -> list[Agency]:
    """HTML выдачи DuckDuckGo -> агентства (по одному на домен)."""
    try:
        from selectolax.parser import HTMLParser  # опциональная зависимость
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("selectolax не установлен. Поставь: pip install -e '.[partners]'") from exc

    agencies: list[Agency] = []
    seen_hosts: set[str] = set()
    for node in HTMLParser(html).css("a.result__a"):
        url = _result_url(node.attributes.get("href") or "")
        if not _is_agency_site(url):
            continue
        host = urlparse(url).netloc.lower().removeprefix("www.")
        # Медиа/агрегаторы/справочники — не агентства.
        if any(host == bad or host.endswith("." + bad) for bad in _NOT_AGENCY_HOSTS):
            continue
        if host in seen_hosts:
            continue  # одна запись на домен: у выдачи бывает несколько страниц одного сайта
        seen_hosts.add(host)
        name = _clean_title(node.text(strip=True), host)
        agencies.append(
            Agency(
                name=name,
                website=url,
                city=city,
                source="search",
                # Тот же стоп-лист крупняка, что у каталогов: иначе сетевые агентства
                # проходили в выгрузку через поисковый источник.
                big_agency=looks_like_big_agency(name),
            )
        )
    return agencies


async def fetch_search(
    query: str,
    client: httpx.AsyncClient,
    settings: Settings | None = None,
    robots: RobotsCache | None = None,
    city: str | None = None,
) -> list[Agency]:
    """Один поисковый запрос -> агентства. Проверяем robots (нам разрешён только html-эндпоинт)."""
    settings = settings or get_settings()
    robots = robots or RobotsCache()
    if not await robots.allowed(client, _SEARCH_URL):
        logger.warning("Поиск: robots.txt запрещает {} — источник пропущен.", _SEARCH_URL)
        return []
    try:
        # POST — так html-эндпоинт DDG отдаёт выдачу без редиректов и капчи.
        response = await client.post(_SEARCH_URL, data={"q": query})
    except Exception as exc:  # noqa: BLE001 — сбой поиска — пропуск одного запроса
        logger.warning("Поиск {!r}: {} — пропускаю.", query, type(exc).__name__)
        return []
    if response.status_code != 200:
        logger.warning("Поиск {!r}: HTTP {}", query, response.status_code)
        return []

    agencies = parse_search_results(response.text, city)
    logger.info("Поиск {!r}: {} доменов агентств.", query, len(agencies))
    return agencies


async def fetch_searches(
    queries: list[str],
    settings: Settings | None = None,
    city: str | None = None,
) -> list[Agency]:
    """Несколько запросов подряд, с паузами (поисковик лимитирует частые обращения)."""
    settings = settings or get_settings()
    robots = RobotsCache()
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    collected: list[Agency] = []
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers=headers,
                                 follow_redirects=True) as client:
        for query in queries:
            collected.extend(await fetch_search(query, client, settings, robots, city))
            # Пауза больше обычной: у поисковика жёсткие лимиты на частоту.
            await asyncio.sleep(max(settings.request_delay * 3, 3.0))
    return collected

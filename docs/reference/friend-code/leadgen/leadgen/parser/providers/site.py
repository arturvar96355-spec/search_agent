"""Обход сайта агентства: собираем email/телефоны/Telegram/услуги и метки (режим partners).

Работаем аккуратно: проверяем robots.txt (и уважаем Disallow), паузы между запросами берём
из настроек, один заход = несколько типовых страниц (главная, контакты, партнёрам, о компании).

Ничего не выдумываем: чего на сайте нет — того не будет в модели (см. Agency.email_found).
"""
from __future__ import annotations

import asyncio
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from ..config import Settings, get_settings
from ..contacts import (
    detect_dev_level,
    detect_niches,
    detect_red_flags,
    extract_emails,
    extract_phones,
    extract_telegram,
)
from ..models import Agency, normalize_domain
from ..robots import Rules
from ..robots import parse as parse_robots

# Слова, подтверждающие, что открывшаяся страница действительно про партнёрство
# (иначе soft-404 или редирект на главную давали ложный has_partner_page).
_PARTNER_WORDS = ("партнёр", "партнер", "partner", "реселл", "комисси", "сотрудничеств")


def _looks_like_partner_page(text: str | None) -> bool:
    """Похоже ли содержимое на страницу для партнёров (а не на главную/заглушку)."""
    if not text:
        return False
    low = text.lower()
    return any(word in low for word in _PARTNER_WORDS)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Типовые страницы, где лежат контакты и услуги. Пустая строка — главная.
PAGE_PATHS: tuple[str, ...] = (
    "",
    "/contacts",
    "/kontakty",
    "/контакты",
    "/partners",
    "/partnery",
    "/about",
    "/o-kompanii",
    "/services",
    "/uslugi",
)

# Пути, по наличию которых считаем, что у агентства есть страница для партнёров.
_PARTNER_HINTS = ("/partners", "/partnery", "/партнер")

_SERVICES_LIMIT = 1500  # сколько символов текста услуг сохраняем в services_raw


def _text_and_hrefs(html: str, base_url: str) -> tuple[str, list[str]]:
    """HTML -> (видимый текст, список абсолютных ссылок). Требует selectolax (extras partners)."""
    try:
        from selectolax.parser import HTMLParser  # опциональная зависимость
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "selectolax не установлен. Поставь: pip install -e '.[partners]'"
        ) from exc

    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript"):
        tag.decompose()
    text = tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True)

    hrefs: list[str] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href")
        if not href:
            continue
        # mailto/tel оставляем как есть, остальное приводим к абсолютному URL.
        hrefs.append(href if href.startswith(("mailto:", "tel:")) else urljoin(base_url, href))
    return text, hrefs


class RobotsCache:
    """Кэш robots.txt по хосту: не дёргаем один и тот же файл на каждую страницу.

    Политика (RFC 9309): 404/нет файла -> можно; 5xx/429/таймаут -> НЕЛЬЗЯ (консервативно,
    чтобы сбой сети не превращался в «разрешено всё»). Редиректы robots.txt следуем.
    """

    _ALLOW_ALL = "allow-all"  # robots отсутствует (404) — ограничений нет
    _DENY_ALL = "deny-all"  # robots недоступен по ошибке сервера — не рискуем

    def __init__(self) -> None:
        self._parsers: dict[str, Rules | str] = {}

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        """Разрешает ли robots.txt заходить по этому URL (по хосту самого URL)."""
        parsed = urlparse(url)
        host = parsed.netloc
        if not host:
            return False
        if host not in self._parsers:
            scheme = parsed.scheme or "https"
            self._parsers[host] = await self._load(client, f"{scheme}://{host}/robots.txt")
        parser = self._parsers[host]
        if parser == self._ALLOW_ALL:
            return True
        if parser == self._DENY_ALL:
            return False
        return parser.allows(url)

    def crawl_delay(self, url: str) -> float | None:
        """Crawl-delay из robots.txt для хоста URL (если задан) — уважаем темп сайта."""
        parser = self._parsers.get(urlparse(url).netloc)
        return parser.delay if isinstance(parser, Rules) else None

    @classmethod
    async def _load(cls, client: httpx.AsyncClient, robots_url: str) -> Rules | str:
        try:
            # follow_redirects обязателен: у www-канонических сайтов apex отдаёт 301 на www,
            # и без этого robots.txt «не находится» -> обход шёл бы без ограничений.
            response = await client.get(robots_url, timeout=10.0, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001 — robots не прочитан — не ходим вовсе
            logger.warning("robots.txt {} недоступен ({}) — на всякий случай НЕ хожу.",
                           robots_url, type(exc).__name__)
            return cls._DENY_ALL

        if response.status_code == 404 or (response.status_code == 200 and not response.text.strip()):
            return cls._ALLOW_ALL  # файла нет / пустой — ограничений нет
        if response.status_code >= 500 or response.status_code == 429:
            logger.warning("robots.txt {}: HTTP {} — консервативно НЕ хожу.",
                           robots_url, response.status_code)
            return cls._DENY_ALL
        if response.status_code != 200:
            logger.info("robots.txt {}: HTTP {} — считаю, что ограничений нет.",
                        robots_url, response.status_code)
            return cls._ALLOW_ALL

        # Редирект на другой хост — правила чужого сайта к нам не относятся.
        final_host = urlparse(str(response.url)).netloc
        asked_host = urlparse(robots_url).netloc
        if final_host and final_host.removeprefix("www.") != asked_host.removeprefix("www."):
            logger.info("robots.txt {} увёл на {} — правила чужого хоста не применяю.",
                        asked_host, final_host)
            return cls._ALLOW_ALL

        return parse_robots(response.text)


def _decoded_text(response: httpx.Response) -> str:
    """Текст ответа с учётом кодировки: у части РФ-сайтов windows-1251 без charset в заголовке."""
    if "charset" not in response.headers.get("content-type", "").lower():
        # httpx угадывает по заголовку; при его отсутствии смотрим мету/эвристику самого HTML.
        raw = response.content
        for encoding in ("utf-8", "windows-1251"):
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            # Признак верной кодировки: нет типичных «кракозябр» от неверного декода.
            if "�" not in text[:4000]:
                return text
    return response.text


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[str, str] | None:
    """Одна страница -> (HTML, финальный URL после редиректов). None, если не открылась/не HTML."""
    try:
        response = await client.get(url, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — сайт не открылся — пропуск
        logger.debug("Сайт: не открылась {} ({})", url, type(exc).__name__)
        return None
    if response.status_code != 200:
        return None
    if "html" not in response.headers.get("content-type", "").lower():
        return None
    return _decoded_text(response), str(response.url)


async def enrich_agency(
    agency: Agency,
    client: httpx.AsyncClient,
    settings: Settings | None = None,
    robots: RobotsCache | None = None,
) -> Agency:
    """Заходим на сайт агентства и заполняем контакты/услуги/метки. Мутирует и возвращает agency.

    Обходим типовые страницы, пока не наберём email (и всегда смотрим главную + контакты).
    Robots-check: страницы, запрещённые в robots.txt, пропускаем.
    """
    settings = settings or get_settings()
    robots = robots or RobotsCache()
    if not agency.url:
        logger.debug("Сайт: у {} нет домена — пропускаю.", agency.name)
        return agency

    base = agency.url
    all_text: list[str] = []
    services_text: list[str] = []
    # Стартуем с уже известных email (например, пришедших из каталога) — иначе перезапишем их ниже.
    emails_by_address: dict[str, object] = {email.address: email for email in agency.emails}
    phones: list[str] = list(agency.phones)
    telegram: list[str] = list(agency.telegram)
    visited = 0

    own_domain = agency.website  # чтобы не приписать агентству контакты чужого сайта

    def _save_progress() -> None:
        """Складываем собранное в модель сразу: исключение на следующей странице не обнулит прогресс."""
        agency.emails = list(emails_by_address.values())  # type: ignore[assignment]
        agency.phones = phones
        agency.telegram = telegram
        if all_text:
            joined_now = " ".join(all_text)
            agency.services_raw = (" ".join(services_text) or joined_now)[:_SERVICES_LIMIT] or None
            agency.niche_tags = detect_niches(joined_now) or ["универсальное"]
            agency.red_flags = detect_red_flags(joined_now)
            # Уровень своей разработки определяет питч (бриф: none -> сайты+CRM,
            # constructors -> только CRM, full -> пропускаем).
            agency.dev_level = detect_dev_level(joined_now)

    for path in PAGE_PATHS:
        url = base + path
        if not await robots.allowed(client, url):
            logger.info("Сайт: robots.txt запрещает {} — пропускаю.", url)
            continue

        fetched = await _fetch(client, url)
        # Уважаем Crawl-delay сайта, если он задан, иначе — свою вежливую паузу.
        await asyncio.sleep(max(settings.request_delay, robots.crawl_delay(url) or 0))
        if not fetched:
            continue
        html, final_url = fetched

        # Редирект мог увести на другой домен: правила и контент там чужие.
        final_domain = normalize_domain(final_url)
        if own_domain and final_domain and final_domain != own_domain:
            if not await robots.allowed(client, final_url):
                logger.info("Сайт: {} увёл на {}, robots чужого хоста запрещает — пропускаю.",
                            url, final_domain)
                continue
            logger.info("Сайт {}: редирект на другой домен {} — контакты оттуда не беру.",
                        own_domain, final_domain)
            continue

        visited += 1
        text, hrefs = _text_and_hrefs(html, final_url)
        all_text.append(text)
        if path in ("/services", "/uslugi"):
            services_text.append(text)

        for email in extract_emails(text, hrefs):
            emails_by_address.setdefault(email.address, email)
        for phone in extract_phones(text):
            if phone not in phones:
                phones.append(phone)
        for handle in extract_telegram(text, hrefs):
            if handle not in telegram:
                telegram.append(handle)

        # Страница для партнёров: открылась сама (и текст это подтверждает) либо есть ссылка
        # на неё в разметке. Ссылку берём только со своего домена — чужие партнёрки не наши.
        if any(hint in path.lower() for hint in _PARTNER_HINTS) and _looks_like_partner_page(text):
            agency.has_partner_page = True
            agency.partner_page_url = final_url
        elif not agency.has_partner_page:
            for href in hrefs:
                low = href.lower()
                if any(hint in low for hint in _PARTNER_HINTS) and normalize_domain(href) == own_domain:
                    agency.has_partner_page = True
                    agency.partner_page_url = href
                    break

        _save_progress()
        # Хватит ходить, если контакты собраны И партнёрскую страницу уже посмотрели.
        if emails_by_address and visited >= 3 and agency.has_partner_page:
            break

    if not visited:
        logger.info("Сайт {} не открылся ни на одной странице.", agency.website)
        return agency

    _save_progress()
    logger.info(
        "Сайт {}: email {}, тел {}, tg {}, ниши {}, разработка {}, питч «{}»",
        agency.website, len(agency.emails), len(agency.phones), len(agency.telegram),
        agency.niche_tags, agency.dev_level, agency.recommended_pitch,
    )
    return agency


async def enrich_agencies(
    agencies: list[Agency],
    settings: Settings | None = None,
) -> list[Agency]:
    """Последовательный обход сайтов агентств (аккуратно, с паузами и общим кэшем robots)."""
    settings = settings or get_settings()
    robots = RobotsCache()
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers=headers) as client:
        for agency in agencies:
            try:
                await enrich_agency(agency, client, settings, robots)
            except Exception as exc:  # noqa: BLE001 — один сайт не должен ронять весь прогон
                logger.warning("Сайт {} не обработан: {}", agency.website, exc)
    with_email = sum(1 for a in agencies if a.email_found)
    logger.info("Обход сайтов готов: {} агентств, с email — {}.", len(agencies), with_email)
    return agencies

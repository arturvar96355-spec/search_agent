"""Запасной браузерный модуль (playwright): добор телефонов и текстов отзывов.

ВНИМАНИЕ: браузерный режим может нарушать ToS площадок — использовать на свой риск
и умеренно (см. README, «Ограничения»). Работает как ДОБОР к результатам API:
берёт организации 2ГИС с известным source_url, открывает публичную карточку,
раскрывает телефон и прогоняет текст отзывов через матчер жалоб (parser.reviews).

Требует опциональную зависимость:
    pip install -e '.[browser]'   &&   playwright install chromium
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from urllib.parse import quote, urlparse

from loguru import logger

from ..config import Settings, get_settings
from ..models import Organization, extract_socials, normalize_phone
from ..reviews import scan_reviews
from .base import Provider

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Аргументы запуска Chromium под маленький VPS (2 ГБ):
#   --disable-dev-shm-usage — критично: /dev/shm мал, иначе Chromium падает;
#   --no-sandbox — нужно при запуске под root/в контейнере;
#   остальное — срезать лишнюю память/процессы.
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--mute-audio",
]

# ГЛОБАЛЬНЫЙ лимит: не больше ОДНОГО Chromium на процесс. На 2 ГБ RAM два headless-Chromium
# на тяжёлом SPA 2ГИС (по ~400-600 МБ) выходят за память → OOM. busy в боте — пер-чат и от
# этого не спасает; семафор здесь защищает всех вызывающих (бот и CLI) разом.
_BROWSER_SLOT = asyncio.Semaphore(1)


def _proxy_config(proxy_url: str | None) -> dict | None:
    """URL прокси -> dict для playwright (server + отдельно username/password).

    Chromium требует креды ОТДЕЛЬНО от server (в самом server их быть не должно).
    Возвращаем None, если прокси не задан. socks5 с авторизацией Chromium не поддерживает —
    предупреждаем в лог.
    """
    if not proxy_url:
        return None
    from urllib.parse import unquote, urlsplit

    raw = proxy_url.strip()
    # Многие провайдеры дают строку «host:port:user:pass» (без схемы и @) — нормализуем.
    if "://" not in raw and "@" not in raw and raw.count(":") == 3:
        host, port, user, pw = raw.split(":")
        raw = f"http://{user}:{pw}@{host}:{port}"
    if "://" not in raw:
        raw = f"http://{raw}"

    try:
        parts = urlsplit(raw)
        host, port = parts.hostname, parts.port  # port бросает ValueError на кривом значении
    except ValueError as exc:
        raise RuntimeError(
            f"Некорректный PROXY_URL {proxy_url!r}. Ожидаю http://user:pass@host:port "
            f"или host:port:user:pass. ({exc})"
        ) from exc
    if not host:
        raise RuntimeError(f"Некорректный PROXY_URL {proxy_url!r}: не разобрал host.")

    if parts.scheme.startswith("socks") and (parts.username or parts.password):
        logger.warning(
            "PROXY_URL = socks с логином/паролем — Chromium это НЕ поддерживает. "
            "Возьми HTTP-прокси (http://user:pass@host:port)."
        )
    server = f"{parts.scheme}://{host}"
    if port:
        server += f":{port}"
    cfg: dict = {"server": server}
    # Percent-decode: пароль может содержать спецсимволы (%40 -> @) — playwright сам не декодит.
    if parts.username:
        cfg["username"] = unquote(parts.username)
    if parts.password:
        cfg["password"] = unquote(parts.password)
    return cfg


def httpx_proxy_url(proxy_url: str | None) -> str | None:
    """Одна строка прокси -> URL для httpx (со схемой и кредами внутри).

    httpx, в отличие от playwright, ждёт креды прямо в URL и не понимает провайдерский
    формат host:port:user:pass — без нормализации рабочий прокси выглядел «мёртвым».
    """
    cfg = _proxy_config(proxy_url)
    if not cfg:
        return None
    scheme, _, hostport = cfg["server"].partition("://")
    user, password = cfg.get("username"), cfg.get("password")
    if user:
        creds = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
        return f"{scheme}://{creds}@{hostport}"
    return cfg["server"]


# Сколько карточек подряд должны упасть, чтобы счесть прокси мёртвым и переключиться.
# Одиночные сбои бывают всегда (медленный сайт), серия — почти всегда прокси.
_PROXY_SWITCH_AFTER = 3


class ProxyPool:
    """Пул прокси с ротацией: при сбоях переключаемся на следующий.

    Зачем: один мобильный прокси — узкое место (таймауты, бан, нет параллельности).
    Пул позволяет пережить падение одного канала, не роняя весь прогон.
    Пустой список = работаем без прокси (локально это нормально, на VPS 2ГИС забанит).
    """

    def __init__(self, proxies: list[str]) -> None:
        self._proxies = list(proxies)
        self._index = 0
        self._failures: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._proxies)

    @property
    def current(self) -> str | None:
        """Текущий прокси (или None, если пул пуст)."""
        return self._proxies[self._index % len(self._proxies)] if self._proxies else None

    def rotate(self) -> str | None:
        """Переключиться на следующий прокси. Возвращает новый текущий."""
        if len(self._proxies) > 1:
            self._index = (self._index + 1) % len(self._proxies)
            logger.info("Прокси: переключаюсь на #{} из {}.", self._index + 1, len(self._proxies))
        return self.current

    def mark_failure(self) -> None:
        """Отметить сбой текущего прокси (для лога/диагностики)."""
        proxy = self.current
        if proxy:
            self._failures[proxy] = self._failures.get(proxy, 0) + 1

    def failures(self) -> dict[str, int]:
        """Сколько сбоев у каждого прокси (для сводки; ключи маскируем при выводе)."""
        return dict(self._failures)


_SHOW_PHONE_RE = re.compile(r"показать\s+телефон", re.IGNORECASE)
_SKIP_GATE_RE = re.compile(r"перейти в 2ГИС", re.IGNORECASE)

# Дата отзыва 2ГИС: «2 апреля 2026». Захватываем год для фильтра свежести.
_REVIEW_DATE_RE = re.compile(
    r"\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\s+(20\d{2})",
    re.IGNORECASE,
)


def _recent_reviews_text(text: str, min_year: int) -> str:
    """Отзывы у 2ГИС идут НОВЫМИ СВЕРХУ. Обрезаем текст на первой полной дате старше min_year —
    всё, что ниже, это старьё (неактуальные жалобы). Нет старых дат — возвращаем всё как есть.

    Ошибается в КОНСЕРВАТИВНУЮ сторону: если в свежем отзыве есть полная старая дата в самом
    тексте («звонил ещё 5 марта 2021»), обрежем чуть раньше и потеряем часть свежих жалоб — но
    НЕ покажем старьё. Регэксп требует полную дату (день+месяц+год), поэтому случайные упоминания
    года («с 2015 года») не триггерят. Надёжный вариант (поэлементный парсинг дат отзывов из DOM)
    упирается в обфусцированные классы 2ГИС — вынесен в бэклог улучшений.
    """
    for match in _REVIEW_DATE_RE.finditer(text):
        if int(match.group(1)) < min_year:
            return text[: match.start()]
    return text


# Города -> слаг в URL 2ГИС (для браузерного поиска). Совпадает с niches.CITIES.
CITY_SLUGS: dict[str, str] = {
    "москва": "moscow",
    "санкт-петербург": "spb",
    "екатеринбург": "ekaterinburg",
    "новосибирск": "novosibirsk",
    "казань": "kazan",
    "краснодар": "krasnodar",
    "нижний новгород": "n_novgorod",
    "самара": "samara",
    "челябинск": "chelyabinsk",
    "омск": "omsk",
    "ростов-на-дону": "rostov",
    "уфа": "ufa",
    "пермь": "perm",
    "воронеж": "voronezh",
    "волгоград": "volgograd",
}

# Карточка в выдаче: «Илатан Сеть клиник4.8139 оценок​Марксистский переулок, 3, Москва​3 филиала».
# Рейтинг (1–5, иногда дробный) слипается с числом оценок; адрес обрамлён ​.
# Без \b: в «клиник4.8139 оценок» между буквой и цифрой границы слова нет.
_SEARCH_RATING_RE = re.compile(r"([1-5](?:[.,]\d)?)(\d{1,6})\s*оцен", re.IGNORECASE)
_FIRM_ID_RE = re.compile(r"/firm/(\d+)")

# Блоки внутри карточки 2ГИС разделяет zero-width space — по нему адрес отделён от
# названия и от «3 филиала». Это надёжнее любого регэкспа, поэтому основной путь — split.
_ZWSP = "​"

# Тип улицы: нужен и для выбора нужного куска после split, и для регэкспа-запасного пути.
_STREET_TYPES = (
    r"улица|ул\.|переулок|пер\.|проспект|просп\.|пр-?кт|пр-?т|шоссе|бульвар|б-?р|"
    r"набережная|наб\.|площадь|пл\.|проезд|аллея|тракт|линия|квартал|микрорайон|мкр"
)
_STREET_TYPE_RE = re.compile(rf"(?:^|[\s,]|\d)(?:{_STREET_TYPES})(?:$|[\s,])", re.IGNORECASE)
_HOUSE_RE = re.compile(r",\s*\d")

# Запасной путь, когда разделителей в тексте нет: «Марксистский переулок, 3, Москва».
# IGNORECASE НЕ ставим: с ним [А-ЯЁ] начинает матчить строчные и адрес обрезается с середины
# («1-й Колобовский» превращалось в «й Колобовский»).
# Название улицы — 1–3 слова БЕЗ цифр: иначе «Илатан Сеть клиник4.8139 оценок Марксистский
# переулок» цепляется целиком с названием фирмы и числом отзывов.
_STREET_NAME = r"[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁа-яё][а-яё\-]+){0,2}"
# Хвост с городом: «, Москва», «, Ростов-на-Дону» (заглавная в середине — только после
# дефиса, иначе слипшиеся блоки «КазаньЛегковой» захватывались целиком), «, Нижний Новгород».
_CITY = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё][а-яё]*)*(?:\s[А-ЯЁ][а-яё]+)?"
_HOUSE_TAIL = rf"[^,​]{{0,20}},\s*\d+[^,​]{{0,18}}(?:,\s*{_CITY})?"

# Тип улицы матчим без учёта регистра — в рекламных карточках он с заглавной («Улица
# Энгельса, 15»), — но (?i:) НЕ распространяем на [А-ЯЁ] в названии: иначе адрес снова
# начнёт обрезаться с середины слова.
_TYPE = rf"(?i:{_STREET_TYPES})"

# Два порядка слов, и порядок ПРОВЕРКИ важен: «Дента улица Ленина, 12» без приоритета
# type-first разобралось бы как название «Дента» + тип «улица».
_ADDRESS_TYPE_FIRST_RE = re.compile(rf"{_TYPE}\s{_STREET_NAME}{_HOUSE_TAIL}")
_ADDRESS_NAME_FIRST_RE = re.compile(rf"(?:\d+-[а-яё]{{1,2}}\s)?{_STREET_NAME}\s{_TYPE}{_HOUSE_TAIL}")


# Служебные ссылки на /firm/ в выдаче: «3 филиала», «Показать на карте», хлебные крошки
# с названием города. Без фильтра они попадали в базу как организация с именем «Казань».
_JUNK_NAME_RE = re.compile(
    r"^(?:\d+\s*филиал|ещё\b|показать\b|на карте\b|подробнее\b|отзыв|маршрут|позвонить\b|"
    r"написать\b|сайт\b|записаться\b)",
    re.IGNORECASE,
)


def _is_junk_name(name: str, city: str) -> bool:
    """Текст ссылки — не название организации, а элемент интерфейса выдачи?"""
    name = name.strip()
    if len(name) < 2:
        return True
    if name.casefold() == city.strip().casefold():
        return True
    return bool(_JUNK_NAME_RE.match(name))


def _extract_address(text: str) -> str | None:
    """Адрес из текста карточки выдачи или None, если его там нет (не выдумываем)."""
    # Только когда разделители реально есть: без них «кусок» — это весь текст карточки,
    # и проверка «есть тип улицы и номер дома» вернула бы название с рейтингом впридачу.
    if _ZWSP in text:
        for chunk in text.split(_ZWSP):
            chunk = chunk.strip(" ,\xa0")
            if len(chunk) <= 120 and _STREET_TYPE_RE.search(chunk) and _HOUSE_RE.search(chunk):
                return chunk
    for pattern in (_ADDRESS_TYPE_FIRST_RE, _ADDRESS_NAME_FIRST_RE):
        match = pattern.search(text)
        if match:
            return match.group(0).strip(" ,")
    return None


def _parse_search_card(text: str) -> tuple[float | None, int | None, str | None]:
    """Текст карточки выдачи -> (рейтинг, число отзывов, адрес). Чего нет — None (не выдумываем)."""
    text = text or ""
    rating = reviews = None
    match = _SEARCH_RATING_RE.search(text)
    if match:
        try:
            rating = float(match.group(1).replace(",", "."))
            reviews = int(match.group(2))
        except ValueError:
            rating = reviews = None
    return rating, reviews, _extract_address(text)


class BrowserProvider(Provider):
    """Поиск организаций прямо в вебе 2ГИС (без Places API и его лимитов).

    Нужен, когда ключ API исчерпан/не куплен: открываем страницу поиска 2ГИС через прокси
    и снимаем карточки выдачи. robots.txt 2ГИС это разрешает (закрыты только /_/ и личный
    кабинет), но это ВСЁ РАВНО скрейпинг: медленнее API, чувствителен к смене вёрстки.
    Подробности и ограничения — в BROWSER_SEARCH.md.
    """

    name = "browser"

    async def search(self, query: str, city: str, max_results: int | None = None) -> list[Organization]:
        try:
            from playwright.async_api import async_playwright  # опциональная зависимость
        except ImportError as exc:
            raise RuntimeError(
                "playwright не установлен. Поставь: "
                "pip install -e '.[browser]' && playwright install chromium"
            ) from exc

        max_results = max_results or self.settings.max_results
        slug = CITY_SLUGS.get(city.strip().lower())
        if not slug:
            logger.warning(
                "Браузерный поиск: не знаю слаг города {!r}. Добавь его в browser.CITY_SLUGS.", city
            )
            return []

        base = f"https://2gis.ru/{slug}/search/{quote(query)}"
        collected: dict[str, Organization] = {}
        pool = ProxyPool(self.settings.proxy_list())
        async with _BROWSER_SLOT, async_playwright() as pw:  # лимит: один Chromium на процесс
                browser = await pw.chromium.launch(
                    headless=True, args=_CHROMIUM_ARGS, proxy=_proxy_config(pool.current)
                )
                try:
                    page = await (
                        await browser.new_context(user_agent=_USER_AGENT, locale="ru-RU")
                    ).new_page()
                    timeout_ms = int(self.settings.browser_timeout * 1000)
                    page_no = 1
                    proxy_switches = 0
                    while page_no <= _SEARCH_MAX_PAGES:
                        url = base if page_no == 1 else f"{base}/page/{page_no}"
                        # Мобильный прокси регулярно роняет одну загрузку из нескольких.
                        # Без повтора первый же таймаут обрывал выдачу на текущей странице.
                        opened = False
                        for attempt in range(1, _SEARCH_PAGE_ATTEMPTS + 1):
                            try:
                                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                                opened = True
                                break
                            except Exception as exc:  # noqa: BLE001 — None
                                logger.warning(
                                    "Браузерный поиск: страница {} не открылась ({}), попытка {}/{}.",
                                    page_no, type(exc).__name__, attempt, _SEARCH_PAGE_ATTEMPTS,
                                )
                                pool.mark_failure()
                                await asyncio.sleep(3)
                        if not opened:
                            # Прокси может быть мёртв целиком, а в пуле ждёт живой — без
                            # переключения весь поиск возвращал пустоту при рабочем втором канале.
                            if proxy_switches < len(pool) - 1:
                                proxy_switches += 1
                                proxy = pool.rotate()
                                logger.warning("Браузерный поиск: переключаюсь на прокси #{} и пробую снова.",
                                               proxy_switches + 1)
                                await browser.close()
                                browser = await pw.chromium.launch(
                                    headless=True, args=_CHROMIUM_ARGS, proxy=_proxy_config(proxy)
                                )
                                page = await (await browser.new_context(
                                    user_agent=_USER_AGENT, locale="ru-RU")).new_page()
                                continue  # та же страница, новый канал
                            break
                        await _pass_gate(page)
                        await asyncio.sleep(3)
                        await page.mouse.wheel(0, 2500)
                        await asyncio.sleep(1.5)

                        cards = await page.evaluate(_SEARCH_CARDS_JS)
                        if not cards:
                            logger.info("Браузерный поиск: на странице {} карточек нет — остановка.", page_no)
                            break

                        before = len(collected)
                        for card in cards:
                            firm = _FIRM_ID_RE.search(card.get("href") or "")
                            name = (card.get("name") or "").strip()
                            if not firm or firm.group(1) in collected or _is_junk_name(name, city):
                                continue
                            rating, reviews, address = _parse_search_card(card.get("text") or "")
                            collected[firm.group(1)] = Organization(
                                name=name,
                                address=address,
                                rating=rating,
                                reviews_count=reviews,
                                rubric=query,
                                source="browser",
                                source_url=FIRM_URL_TEMPLATE.format(id=firm.group(1)),
                            )
                            if len(collected) >= max_results:
                                break
                        logger.info(
                            "Браузерный поиск {!r}/{!r}: страница {} — +{} (всего {}).",
                            query, city, page_no, len(collected) - before, len(collected),
                        )
                        if len(collected) >= max_results or len(collected) == before:
                            break  # набрали сколько нужно / новых не появилось
                        page_no += 1
                        await asyncio.sleep(self.settings.request_delay)
                finally:
                    await browser.close()

        result = list(collected.values())[:max_results]
        logger.info("Браузерный поиск: собрано {} организаций по {!r}/{!r}.", len(result), query, city)
        return result


# Сколько страниц выдачи максимум листаем (12 карточек на страницу).
_SEARCH_MAX_PAGES = 6
# Попыток открыть одну страницу выдачи: мобильный прокси часто отваливается по таймауту.
_SEARCH_PAGE_ATTEMPTS = 3

# Ссылка на карточку + текст её блока: классы у 2ГИС обфусцированы, поэтому поднимаемся
# от ссылки к ближайшему контейнеру с осмысленным объёмом текста.
_SEARCH_CARDS_JS = """() => {
    const out = [];
    document.querySelectorAll('a[href*="/firm/"]').forEach(a => {
        let box = a.closest('div');
        for (let i = 0; i < 4 && box && box.textContent.length < 60; i++) box = box.parentElement;
        out.push({
            href: a.getAttribute('href') || '',
            name: (a.textContent || '').trim(),
            text: box ? box.textContent.slice(0, 400) : ''
        });
    });
    return out;
}"""

FIRM_URL_TEMPLATE = "https://2gis.ru/firm/{id}"


async def _reveal_phones(page) -> None:
    """Кликаем кнопки «Показать телефон», чтобы раскрыть tel:-ссылки."""
    try:
        buttons = page.get_by_text(_SHOW_PHONE_RE)
        for i in range(min(await buttons.count(), 10)):
            try:
                await buttons.nth(i).click(timeout=2500)
            except Exception:  # noqa: BLE001 — кнопка могла исчезнуть — следующая
                continue
    except Exception:  # noqa: BLE001 — раскрытие отзывов необязательно
        pass


async def _collect_phones(page) -> list[str]:
    hrefs = await page.eval_on_selector_all(
        'a[href^="tel:"]', "els => els.map(e => e.getAttribute('href'))"
    )
    return [h.replace("tel:", "") for h in hrefs if h]


# Домены, которые НЕ являются сайтом организации (сам 2ГИС, редиректы, магазины приложений).
_NOT_A_SITE = (
    "2gis.", "api.", "go.2gis", "link.2gis",
    # Сервисы экосистемы 2ГИС и промо-блоки на карточке (otello — их бронирование отелей):
    # именно otello.ru раньше приписывался ВСЕМ лидам подряд.
    "otello.ru", "edadeal.ru", "dostavista.",
    "apps.apple.com", "play.google.com", "itunes.apple.com",
    "onelink.me", "adjust.com", "/redirect", "yandex.ru/maps",
)
_SOCIAL_HOSTS = ("t.me", "telegram.", "vk.com", "instagram.", "facebook.",
                 "youtube.", "wa.me", "whatsapp.", "ok.ru")


def _is_2gis_owned(url: str) -> bool:
    """Ссылка принадлежит самому 2ГИС (его соцсети/промо в футере), а не организации."""
    low = url.lower()
    return "2gis" in low or "govoritdgis" in low


def _link_text_is_domain(href: str, text: str) -> bool:
    """Текст ссылки — это её же домен? Так 2ГИС показывает САЙТ ОРГАНИЗАЦИИ («klinika.ru»).

    Промо-блоки и виджеты (otello.ru, магазины приложений, сервисы записи) подписаны словами
    («Забронировать», «Записаться»), а не доменом — поэтому этот признак их отсекает.
    """
    host = urlparse(href).netloc.lower().removeprefix("www.")
    if not host:
        return False
    shown = re.sub(r"^https?://|^www\.|/+$", "", (text or "").strip().lower())
    return bool(shown) and (shown == host or shown.rstrip("/") == host)


async def _collect_site_and_socials(page) -> tuple[str | None, dict[str, str]]:
    """Внешние ссылки с карточки -> (сайт компании, соцсети).

    Сайт берём ТОЛЬКО из ссылки, чей текст равен её домену — именно так 2ГИС выводит сайт
    организации. Раньше брали «первую подходящую внешнюю ссылку», и всем лидам подряд
    приписывался промо-домен 2ГИС (otello.ru): у него пустой путь, поэтому он выигрывал
    любую сортировку. Не нашли такую ссылку — оставляем website пустым (не выдумываем).
    """
    links = await page.eval_on_selector_all(
        'a[href^="http"]',
        "els => els.map(e => ({href: e.getAttribute('href'), text: (e.textContent || '').trim()}))",
    )
    links = [link for link in links if link.get("href") and not _is_2gis_owned(link["href"])]
    socials = extract_socials(" ".join(link["href"] for link in links))

    website: str | None = None
    for link in links:
        href, text = link["href"], link.get("text", "")
        low = href.lower()
        if any(bad in low for bad in _NOT_A_SITE) or any(s in low for s in _SOCIAL_HOSTS):
            continue
        if _link_text_is_domain(href, text):  # единственный надёжный признак «это их сайт»
            website = href
            break
    return website, socials


async def _scroll(page, times: int = 6) -> None:
    for _ in range(times):
        await page.mouse.wheel(0, 4000)
        await asyncio.sleep(0.6)


async def _select_negative(page) -> None:
    """Фильтруем отзывы на «Только отрицательные» — там сидят жалобы на связь.

    Опция скрыта в дропдауне; JS-клик по элементу срабатывает и без раскрытия меню.
    """
    try:
        clicked = await page.eval_on_selector_all(
            '[title="Только отрицательные"]',
            "els => { if (els[0]) { els[0].click(); return true } return false }",
        )
        if clicked:
            await asyncio.sleep(3)  # список перезагружается на негативные
    except Exception:  # noqa: BLE001 — фильтр «негативные» необязателен
        pass


async def _pass_gate(page) -> None:
    """2ГИС в headless показывает заглушку «обновите браузер» — пробиваем её и ждём SPA.

    networkidle у 2ГИС не наступает (постоянные соединения), поэтому фиксированная пауза.
    """
    try:
        skip = page.get_by_text(_SKIP_GATE_RE)
        if await skip.count():
            await skip.first.click(timeout=5000)
    except Exception:  # noqa: BLE001 — плашки онбординга может не быть
        pass
    await asyncio.sleep(4)


async def _load(page, url: str, timeout_ms: int) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await _pass_gate(page)


async def _enrich_one(page, org: Organization, settings: Settings) -> None:
    # Отдельный (больший) таймаут для браузера: 2ГИС тяжёлый + мобильный прокси медленный.
    timeout_ms = int(settings.browser_timeout * 1000)

    # --- телефоны с карточки ---
    await _load(page, org.source_url, timeout_ms)
    await _reveal_phones(page)
    await asyncio.sleep(1)
    phones = list(org.phone)
    for raw in await _collect_phones(page):
        normalized = normalize_phone(raw)
        if normalized and normalized not in phones:
            phones.append(normalized)
    org.phone = phones

    # --- сайт и соцсети с карточки (публичные данные) ---
    website, socials = await _collect_site_and_socials(page)
    if website and not org.website:
        org.website = website
    for net, url in socials.items():
        org.socials.setdefault(net, url)

    # --- тексты отзывов (только отрицательные) -> сигналы жалоб ---
    reviews_url = page.url.split("?")[0].rstrip("/") + "/tab/reviews"
    await _load(page, reviews_url, timeout_ms)
    await _select_negative(page)
    await _scroll(page, times=12)
    body_text = await page.inner_text("body")
    # Только свежие отзывы: отбрасываем всё старше settings.reviews_min_year.
    body_text = _recent_reviews_text(body_text, settings.reviews_min_year)
    for complaint in scan_reviews([body_text]):
        if complaint not in org.review_complaints:
            org.review_complaints.append(complaint)

    # --- второй источник: отзывы из Яндекс.Карт (по флагу, экспериментально) ---
    if settings.use_yandex_reviews:
        from .yandex import enrich_reviews_yandex

        for complaint in await enrich_reviews_yandex(page, org, settings):
            if complaint not in org.review_complaints:
                org.review_complaints.append(complaint)


def browsable(orgs: list[Organization]) -> list[Organization]:
    """Карточки, которые умеет открыть браузерный добор (карточка 2ГИС с source_url).

    source="browser" — это ТЕ ЖЕ карточки 2ГИС (найдены поиском в вебе, source_url ведёт
    на /firm/…): без него в режиме без API-ключа добор отсекал бы всё и бот не находил
    ни одного лида.
    """
    return [org for org in orgs if org.source in ("2gis", "browser") and org.source_url]


async def iter_enrich_with_browser(
    orgs: list[Organization],
    settings: Settings | None = None,
    limit: int | None = None,
) -> AsyncIterator[tuple[Organization, bool]]:
    """То же, что enrich_with_browser, но отдаёт карточки по одной, сразу после обработки.

    Отдаёт пары (org, ok): ok=False, если добор карточки упал (таймаут прокси, разрыв и т.п.).
    Вызывающий по ok решает, помечать ли карточку «проверенной» — иначе транзиентный таймаут
    навсегда выкинул бы потенциально горячий лид (карточку просто не удалось прочитать).

    Нужно для Telegram-бота: он шлёт найденный «горячий» лид немедленно и может
    оборвать добор, как только наберёт нужное количество, — не тратя минуты на хвост.
    Обрыв делается обычным `break` по итератору: браузер закроется через `finally`.
    """
    try:
        from playwright.async_api import async_playwright  # опциональная зависимость
    except ImportError as exc:
        raise RuntimeError(
            "playwright не установлен. Поставь: pip install -e '.[browser]' && playwright install chromium"
        ) from exc

    settings = settings or get_settings()
    targets = browsable(orgs)
    if limit:
        targets = targets[:limit]
    if not targets:
        logger.warning("Браузерный добор: нет карточек 2ГИС с source_url — пропускаю.")
        return

    logger.info("Браузерный добор: обрабатываю {} карточек…", len(targets))
    # Один Chromium на процесс (см. _BROWSER_SLOT). Если слот занят другим поиском — ждём тут.
    if _BROWSER_SLOT.locked():
        logger.info("Браузер занят другим поиском — жду освобождения слота…")
    async with _BROWSER_SLOT:
        pool = ProxyPool(settings.proxy_list())
        if len(pool) > 1:
            logger.info("Прокси-пул: {} каналов, ротация при сбоях.", len(pool))
        proxy_cfg = _proxy_config(pool.current)
        if proxy_cfg:
            logger.info("Браузер идёт через прокси {}.", proxy_cfg["server"])
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS, proxy=proxy_cfg)
            except Exception as exc:  # чаще всего — не установлен бинарь/системные .so на VPS
                raise RuntimeError(
                    "Chromium не запустился. На VPS выполни: "
                    "pip install -e '.[browser]' && playwright install --with-deps chromium. "
                    f"Причина: {exc}"
                ) from exc
            try:
                context = await browser.new_context(user_agent=_USER_AGENT, locale="ru-RU")
                page = await context.new_page()
                consecutive_failures = 0
                for org in targets:
                    ok = True
                    try:
                        await _enrich_one(page, org, settings)
                    except Exception as exc:  # noqa: BLE001 — через прокси бывает разовый лаг — один повтор
                        logger.info("Карточка {} — повтор после ошибки: {}", org.source_url, exc)
                        try:
                            await asyncio.sleep(1.5)
                            await _enrich_one(page, org, settings)
                        except Exception as exc2:  # noqa: BLE001 — одна карточка не должна ронять весь прогон
                            ok = False
                            logger.warning("Карточка {} не обработана (2 попытки): {}", org.source_url, exc2)
                    yield org, ok

                    # Серия сбоев подряд обычно значит «прокси сдох/забанен» — меняем канал
                    # и перезапускаем браузер на новом прокси (если в пуле есть куда).
                    consecutive_failures = 0 if ok else consecutive_failures + 1
                    if consecutive_failures >= _PROXY_SWITCH_AFTER and len(pool) > 1:
                        pool.mark_failure()
                        pool.rotate()
                        consecutive_failures = 0
                        new_cfg = _proxy_config(pool.current)
                        try:
                            await browser.close()
                            browser = await pw.chromium.launch(
                                headless=True, args=_CHROMIUM_ARGS, proxy=new_cfg
                            )
                            context = await browser.new_context(user_agent=_USER_AGENT, locale="ru-RU")
                            page = await context.new_page()
                            logger.info("Прокси сменён на {} — продолжаю добор.",
                                        (new_cfg or {}).get("server", "без прокси"))
                        except Exception as exc3:  # noqa: BLE001 — браузер не поднялся — добор останавливаем
                            logger.error("Не удалось перезапустить браузер на новом прокси: {}", exc3)
                            break

                    await asyncio.sleep(settings.request_delay)  # вежливый rate limiting
            finally:
                await browser.close()
                if pool.failures():
                    logger.info("Прокси: сбоев по каналам — {}", len(pool.failures()))


async def enrich_with_browser(
    orgs: list[Organization],
    settings: Settings | None = None,
    limit: int | None = None,
) -> list[Organization]:
    """Добираем телефоны и сигналы из отзывов для карточек 2ГИС (по source_url).

    Флаг `ok` из обхода не выбрасываем. Он был `_ok`, и счётчик считал ВСЕ
    карточки обработанными: прогон с мёртвым прокси, где не открылась ни одна,
    писал в отчёт «готово 40/40, с сигналами — 0» — то есть выглядел ровно как
    честный сбор по компаниям без жалоб.
    """
    processed = 0
    failed = 0
    async for _org, ok in iter_enrich_with_browser(orgs, settings, limit):
        processed += 1
        failed += 0 if ok else 1

    targets = browsable(orgs)[: limit or None]
    with_phones = sum(1 for o in targets if o.phone)
    with_flags = sum(1 for o in targets if o.review_complaints)
    logger.info(
        "Браузерный добор готов: {}/{} карточек, с телефоном — {}, с сигналами в отзывах — {}.",
        processed - failed,
        len(targets),
        with_phones,
        with_flags,
    )
    if failed:
        # Отдельной строкой и погромче: «не открылось» и «жалоб нет» — разные
        # новости, и вторая без первой читается как результат.
        logger.error(
            "Браузерный добор: не открылось карточек — {} из {}. "
            "Похоже на мёртвый прокси или слетевший профиль, а не на отсутствие жалоб.",
            failed,
            processed,
        )
    return orgs

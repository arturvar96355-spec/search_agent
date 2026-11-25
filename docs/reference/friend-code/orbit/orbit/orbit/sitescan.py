"""Поиск почты и телеграма на сайте компании.

Зачем: из карт приходит только телефон. Из 70 собранных компаний ни у одной
не было адреса — вкладка «Клиенты» при этом превращается в список для
обзвона и ничего больше. Почта чаще всего есть, просто лежит на сайте.

Почему свой обход, а не готовый из парсера: тот живёт в соседнем проекте со
своим окружением (pandas, playwright, selectolax) и тянуть его сюда ради
десятка регулярных выражений дороже, чем написать эти выражения здесь.
Общее у них только одно — правила приличия, и они соблюдаются одинаково:
читаем robots.txt, держим паузу между запросами, чужой домен не трогаем.

Ничего не выдумываем: не нашли адрес — значит его нет, а не «наверное,
info@».
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from .models import Contact, is_corporate_email, normalize_telegram

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Типовые страницы, где лежат контакты. Пустая строка — главная.
PAGES: tuple[str, ...] = ("", "/contacts", "/kontakty", "/contact", "/about", "/o-nas")
# Дальше шестой страницы ходить незачем: если контактов нет на главной и в
# «контактах», их на сайте нет вообще.
MAX_PAGES = 6
PAUSE_SECONDS = 1.0
# Пауза между запросами к поисковику. Отдельная и намного больше, чем между
# страницами одного сайта: страницы мы читаем у того, кто нас ждёт, а поиском
# пользуемся у того, кто нас терпит.
SEARCH_PAUSE = (4.0, 9.0)
# Сколько отказов поисковика подряд считаем сигналом «хватит на сегодня».
# Дальше он всё равно отдаёт заглушку, а мы тратим обход впустую и выглядим
# как перебор запросов.
SEARCH_GIVE_UP_AFTER = 3
TIMEOUT = 15.0
# Больше мегабайта — это не страница с контактами, а выгрузка каталога.
MAX_BYTES = 1_500_000

# Буквы не ограничены латиницей: «почта@компания.рф» — обычный рабочий адрес
# российской компании. Проверка адреса (mailcheck) кириллицу понимает и
# специально это оговаривает, а сборщик с сайта её не находил вовсе — и с
# такого сайта не приносил ни одного контакта. Домен верхнего уровня — только
# буквы: цифры в нём не бывают. Имена файлов («logo@2x.png») отсеиваются не
# здесь, а списком _STOP_TLD ниже — там расширения перечислены поимённо.
_EMAIL_RE = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}", re.UNICODE)
_MAILTO_RE = re.compile(r"mailto:([^\"'>?\s]+)", re.IGNORECASE)
_TG_RE = re.compile(r"(?:https?://)?t(?:elegram)?\.me/([A-Za-z0-9_]{4,32})", re.IGNORECASE)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
# По этим словам узнаём ссылку на страницу с контактами — хоть в адресе, хоть в подписи.
_CONTACT_WORDS = ("contact", "kontakt", "контакт", "about", "o-nas", "о нас", "связ", "svyaz")

# Куски адресов, которые выглядят как почта, но ею не являются.
_STOP_TLD = frozenset({
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "ico",
    "woff", "woff2", "pdf", "doc", "docx", "xls", "xlsx", "zip", "mp4", "json", "xml",
})
# Почты конструкторов, виджетов и аналитики: они попадают в разметку чужим
# текстом («Сделано на Тильде») и к компании отношения не имеют.
_STOP_DOMAINS = frozenset({
    "madeontilda.com", "tilda.cc", "tilda.ws", "wix.com", "wixsite.com", "readymag.com",
    "squarespace.com", "shopify.com", "bitrix24.ru", "bitrix.ru", "1c-bitrix.ru",
    "sentry.io", "jivosite.com", "carrotquest.io", "envybox.io", "calltouch.ru",
    "roistat.com", "amocrm.ru", "example.com", "example.ru", "domain.ru", "sitename.ru",
    # Заглушки из форм обратной связи: «введите почту, например…». Письмо на
    # такой адрес мы однажды честно отправили — оно ушло в никуда от имени студии.
    "email.com", "mail.com", "yourmail.ru", "yourdomain.ru", "site.ru", "company.ru",
    "test.ru", "test.com", "email.ru", "mydomain.ru", "name.ru",
})
# Имя ящика, по которому видно образец, а не живой адрес.
_STOP_LOCAL_PARTS = frozenset({
    "example", "sample", "your-email", "youremail", "your_mail", "yourmail",
    "email", "e-mail", "mail@mail", "test", "noreply", "no-reply", "donotreply",
    "do-not-reply", "username", "user", "name", "ваша", "почта",
})
# Телеграм-ники самих площадок и виджетов — это не контакт компании.
_STOP_TELEGRAM = frozenset({
    "telegram", "share", "joinchat", "durov", "tilda", "instagram", "whatsapp",
})


class SearchUnavailable(RuntimeError):
    """Поисковик не отдал выдачу (антибот, блокировка, смена вёрстки).

    Это не «у компании нет сайта», а «мы не смогли посмотреть». Разница
    важная: в первом случае к компании возвращаться незачем, во втором —
    обязательно нужно, когда поиск снова заработает.
    """


@dataclass
class Found:
    """Что нашлось на сайте."""

    emails: list[str] = field(default_factory=list)
    telegram: list[str] = field(default_factory=list)
    # Где именно нашли адрес. Это доказательство, что он лежал в открытом
    # доступе: в споре о холодном письме первым спрашивают, откуда адрес.
    source_page: str = ""
    pages: int = 0
    error: str | None = None
    # Убедились ли, что сайт принадлежит именно этой компании. Важно только
    # для сайтов, найденных поиском: там легко попасть на однофамильца.
    verified: bool = True
    # Помеха временная — вернуться к этой компании позже.
    retry: bool = False
    # Помеха была со стороны ПОИСКОВИКА, а не сайта компании. Разница важна
    # для всего прохода: отказ поисковика повторится на каждой следующей
    # компании, а прилёгший сайт — беда одной. Считать их вместе значит
    # обрывать проход из-за трёх подряд неудачных сайтов и списывать это на
    # поиск, который ни при чём.
    search_failed: bool = False

    @property
    def anything(self) -> bool:
        return bool(self.emails or self.telegram)


def site_url(site: str) -> str:
    """«company.ru» → «https://company.ru». Уже полный адрес оставляем как есть."""
    site = (site or "").strip().rstrip("/")
    if not site:
        return ""
    if site.startswith(("http://", "https://")):
        return site
    return f"https://{site}"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def visible_text(html: str) -> str:
    """HTML → текст. Грубо, но для поиска адресов этого хватает."""
    without_code = _TAG_RE.sub(" ", html or "")
    return _HTML_TAG_RE.sub(" ", without_code)


def extract_emails(html: str) -> list[str]:
    """Адреса из текста и из mailto-ссылок.

    Личные ящики на gmail/mail.ru отсеиваются: писать холодное письмо на
    личную почту нельзя (это правило рассылки, а не техническое ограничение).
    """
    found: list[str] = []
    candidates = [m.group(1) for m in _MAILTO_RE.finditer(html or "")]
    candidates += [m.group(0) for m in _EMAIL_RE.finditer(visible_text(html))]

    for raw in candidates:
        address = raw.strip().strip(".,;:").lower()
        # mailto может нести имя получателя и параметры: чистим.
        address = address.split("?", 1)[0]
        if not _EMAIL_RE.fullmatch(address):
            continue
        domain = address.rsplit("@", 1)[-1]
        if domain.rsplit(".", 1)[-1] in _STOP_TLD:
            continue
        if domain in _STOP_DOMAINS:
            continue
        if address.split("@", 1)[0] in _STOP_LOCAL_PARTS:
            continue
        if not is_corporate_email(address):
            continue
        if address not in found:
            found.append(address)
    return found


def extract_telegram(html: str) -> list[str]:
    handles: list[str] = []
    for match in _TG_RE.finditer(html or ""):
        name = match.group(1)
        if name.lower() in _STOP_TELEGRAM:
            continue
        handle = normalize_telegram(name)
        if handle and handle not in handles:
            handles.append(handle)
    return handles


# --- Поиск сайта компании ---------------------------------------------------
# Из карт сайт не приходит: 2ГИС отдаёт его только на платном тарифе, а у
# Яндекса лицензия запрещает складывать выдачу в свою базу. Поэтому сайт
# ищем сами — по названию.
#
# Ищем в DuckDuckGo и только там: у Google и Яндекса robots.txt прямо
# запрещает разбор страницы выдачи, а html.duckduckgo.com — разрешает.

SEARCH_URL = "https://html.duckduckgo.com/html/"
_DDG_LINK_RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', re.IGNORECASE)
_UDDG_RE = re.compile(r"uddg=([^&]+)")

# Карты, отзовики, соцсети и агрегаторы. Сайт компании — это её собственный
# домен, а не страница на чужой площадке.
_NOT_A_SITE = frozenset({
    "2gis.ru", "yandex.ru", "maps.yandex.ru", "google.com", "zoon.ru", "flamp.ru",
    "yell.ru", "orgpage.ru", "blizko.ru", "spr.ru", "rusprofile.ru", "list-org.com",
    "avito.ru", "youla.ru", "ozon.ru", "wildberries.ru", "vk.com", "ok.ru",
    "instagram.com", "facebook.com", "t.me", "youtube.com", "dzen.ru", "otzovik.com",
    "irecommend.ru", "prodoctorov.ru", "napopravku.ru", "docdoc.ru", "hh.ru",
    "wikipedia.org", "tripadvisor.ru", "restoclub.ru", "afisha.ru", "delivery-club.ru",
})

_DOMAIN_IN_NAME_RE = re.compile(r"\b([a-zа-я0-9-]{2,}\.(?:рф|ru|com|su|moscow))\b", re.IGNORECASE)


def domain_from_name(name: str) -> str | None:
    """Иногда компания названа своим же доменом («ап-сервис.рф, автотехцентр»).

    Тогда искать нечего — сайт уже перед нами.
    """
    match = _DOMAIN_IN_NAME_RE.search(name or "")
    return match.group(1).lower() if match else None


def _search_result_urls(html: str) -> list[str]:
    """Ссылки из выдачи DuckDuckGo. Обёртку /l/?uddg=… разворачиваем."""
    from urllib.parse import unquote

    urls = []
    for match in _DDG_LINK_RE.finditer(html or ""):
        href = match.group(1)
        wrapped = _UDDG_RE.search(href)
        url = unquote(wrapped.group(1)) if wrapped else href
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http") and url not in urls:
            urls.append(url)
    return urls


def find_site(name: str, city: str | None = None, *, client: httpx.Client | None = None) -> str | None:
    """Ищет сайт компании по названию. None — не нашли ничего похожего."""
    own = domain_from_name(name)
    if own:
        return own

    query = " ".join(part for part in (name, city, "официальный сайт") if part)
    close_after = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    )
    try:
        response = client.post(SEARCH_URL, data={"q": query})
        # Пауза после поиска намного больше, чем между страницами сайта.
        # С секундой между запросами поисковик переставал отвечать выдачей
        # примерно на сорок пятой компании и до конца прохода отдавал
        # заглушку: треть базы оставалась без сайта не потому, что сайта нет,
        # а потому, что мы себя так вели. Разброс — чтобы ровный интервал не
        # выдавал машину.
        time.sleep(random.uniform(*SEARCH_PAUSE))
    except httpx.HTTPError as exc:
        raise SearchUnavailable(f"поисковик не ответил: {type(exc).__name__}") from exc
    finally:
        if close_after:
            client.close()

    if response.status_code != 200:
        # 202 приходит, когда поисковик подставляет страницу-заглушку вместо
        # выдачи. Пустой список результатов отсюда означал бы «сайта нет», а
        # это неправда — мы просто ничего не увидели.
        raise SearchUnavailable(f"поисковик ответил {response.status_code}")

    results = _search_result_urls(response.text)
    if not results:
        raise SearchUnavailable("в выдаче нет ссылок — вёрстка поменялась или это заглушка")

    for url in results:
        host = _host(url)
        if not host or host in _NOT_A_SITE:
            continue
        if any(host.endswith("." + stop) for stop in _NOT_A_SITE):
            continue
        return host
    return None


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def belongs_to(html: str, contact: Contact) -> bool:
    """Тот ли это сайт. Поиск может привести куда угодно, и чужие контакты
    в карточке компании хуже, чем пустая карточка.

    Достаточно одного надёжного совпадения: телефон из карт на странице или
    заметный кусок названия компании.
    """
    text = visible_text(html).lower()
    page_digits = _digits(text)
    for phone in contact.phones:
        tail = _digits(phone)[-10:]
        if len(tail) == 10 and tail in page_digits:
            return True

    # Название вида «ап-сервис.рф, автотехцентр»: рубрика после запятой нам
    # не нужна, сравниваем по самому имени.
    head = (contact.name or "").split(",")[0].strip().lower()
    return len(head) >= 4 and head in text


def parse_robots(text: str) -> list[tuple[bool, str]]:
    """robots.txt → список правил (разрешено, шаблон) для «User-agent: *».

    Свой разбор, а не robotparser из стандартной библиотеки. Тот приводит
    правило к пути через urlparse и на строке «Disallow: /?» (запретить
    адреса с параметрами — она есть в каждом втором робots WordPress)
    получает просто «/», то есть запрет всего сайта. На живом сайте
    агентства это выглядело как «сайт не открылся», хотя обход разрешён.
    """
    rules: list[tuple[bool, str]] = []
    for_us = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()

        if field == "user-agent":
            # Нас интересует только общая группа: собственного имени у нас нет.
            for_us = value == "*"
        elif for_us and field in ("allow", "disallow") and value:
            rules.append((field == "allow", value))
    return rules


def _rule_matches(pattern: str, path: str) -> bool:
    """Сопоставление по правилам robots: * — что угодно, $ — конец адреса."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    parts = [re.escape(piece) for piece in body.split("*")]
    regex = ".*".join(parts)
    return bool(re.match(regex + ("$" if anchored else ""), path))


def robots_allows(rules: list[tuple[bool, str]], url: str) -> bool:
    """Пускают ли нас. При споре побеждает более длинное правило."""
    parts = urlparse(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    best: tuple[int, bool] | None = None
    for allowed, pattern in rules:
        if not _rule_matches(pattern, path):
            continue
        weight = len(pattern)
        # Одинаково длинные правила: разрешение сильнее запрета.
        if best is None or weight > best[0] or (weight == best[0] and allowed):
            best = (weight, allowed)
    return best[1] if best else True


class Robots:
    """robots.txt по хостам. Не пускают — не идём."""

    def __init__(self) -> None:
        self._cache: dict[str, list[tuple[bool, str]]] = {}

    def allowed(self, client: httpx.Client, url: str) -> bool:
        host = _host(url)
        if host not in self._cache:
            self._cache[host] = self._load(client, url)
        return robots_allows(self._cache[host], url)

    @staticmethod
    def _load(client: httpx.Client, url: str) -> list[tuple[bool, str]]:
        parts = urlparse(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            response = client.get(robots_url, timeout=8.0)
        except httpx.HTTPError:
            return []  # robots не прочитался — правил нет
        if response.status_code != 200 or not response.text.strip():
            return []
        # Некоторые хосты отдают на robots.txt обычную HTML-страницу или
        # заглушку. Правил там нет, а разбирать разметку как robots — верный
        # способ выдумать себе запрет.
        if "<html" in response.text[:200].lower():
            return []
        return parse_robots(response.text)


def scan(
    site: str,
    *,
    client: httpx.Client | None = None,
    robots: Robots | None = None,
    belongs_to_contact: Contact | None = None,
) -> Found:
    """Обходит сайт и собирает почту с телеграмом.

    belongs_to_contact — проверить, что сайт правда принадлежит этой компании.
    Нужно, когда адрес сайта не пришёл из выгрузки, а найден поиском.
    """
    base = site_url(site)
    if not base:
        return Found(error="нет сайта")

    own = _host(base)
    robots = robots or Robots()
    close_after = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    )

    result = Found()
    home_html = ""

    def visit(url: str) -> str:
        """Открывает страницу и забирает с неё контакты. Возвращает её HTML."""
        if not robots.allowed(client, url):
            logger.debug("robots запрещает {}", url)
            return ""
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            if not result.pages:
                result.error = f"сайт не открылся: {type(exc).__name__}"
            return ""
        time.sleep(PAUSE_SECONDS)

        if response.status_code != 200:
            return ""
        # Редирект увёл на чужой домен — контакты оттуда не наши.
        if _host(str(response.url)) != own:
            logger.debug("{} увёл на чужой домен {}", url, _host(str(response.url)))
            return ""

        html = response.text[:MAX_BYTES]
        result.pages += 1
        result.error = None
        for address in extract_emails(html):
            if address not in result.emails:
                result.emails.append(address)
                if not result.source_page:
                    result.source_page = url
        for handle in extract_telegram(html):
            if handle not in result.telegram:
                result.telegram.append(handle)
        return html

    if belongs_to_contact is not None:
        result.verified = False

    try:
        for path in PAGES[:MAX_PAGES]:
            url = urljoin(base + "/", path.lstrip("/")) if path else base
            html = visit(url)
            if not path:
                home_html = html
            if belongs_to_contact is not None and html and not result.verified:
                result.verified = belongs_to(html, belongs_to_contact)
                if not result.verified and path == "":
                    # Главная не про эту компанию — дальше идти незачем.
                    # Именно незачем, а не «почти незачем»: `return` вместо
                    # `break`, потому что после цикла стоит обход по ссылкам с
                    # главной, и он продолжал ходить по ЧУЖОМУ сайту, собирая
                    # чужие адреса. В карточку они не попадали, но десяток
                    # лишних запросов на каждую такую компанию проход платил.
                    result.error = "нашёлся чужой сайт"
                    return result
            # Нашли и почту, и телеграм — дальше ходить незачем.
            if result.emails and result.telegram:
                break

        # Типовые адреса подошли не всем: на самописных сайтах контакты живут
        # где угодно. Тогда идём по ссылкам с главной — там путь и найдётся.
        if not result.emails and home_html:
            for url in _links_to_more_pages(home_html, base):
                visit(url)
                if result.emails:
                    break

        if not result.pages:
            # Ни одна страница не открылась. Это НЕ «сайт чужой»: мы вообще
            # ничего не увидели. Раньше непроверенный сайт трактовался как
            # чужой, ссылка на него стиралась, компания помечалась обойдённой —
            # и живая фирма с прилёгшим на минуту сайтом теряла его навсегда.
            result.verified = True
            result.retry = True
            result.error = result.error or "сайт не открылся"
        return result
    finally:
        if close_after:
            client.close()


def _links_to_more_pages(html: str, base: str) -> list[str]:
    """Ссылки на страницы контактов, если типовые пути не подошли.

    Смотрим и адрес, и подпись ссылки: у самописных сайтов путь бывает какой
    угодно («/svyaz-s-nami»), а вот подписана ссылка почти всегда «Контакты».
    """
    own = _host(base)
    out: list[str] = []
    for match in _LINK_RE.finditer(html or ""):
        href, label = match.group(1), _HTML_TAG_RE.sub(" ", match.group(2))
        haystack = f"{href} {label}".lower()
        if not any(word in haystack for word in _CONTACT_WORDS):
            continue
        full = urljoin(base, href)
        if _host(full) == own and full not in out:
            out.append(full)
    return out[:3]


def forget_failures(store, kind) -> int:
    """Снимает метку «уже ходили» с тех, у кого обход закончился ничем.

    Нужно после починки самого обхода: иначе компании, забракованные старой
    версией, останутся без контактов навсегда.
    """
    count = 0
    for contact in store.contacts(kind, limit=2000):
        if not _scanned(contact) or contact.reachable:
            continue
        contact.meta.pop(SCANNED_KEY, None)
        contact.meta.pop("site_scan_error", None)
        store.set_meta(contact.pk, contact.meta)
        count += 1
    return count


def enrich(store, kind, *, limit: int = 20) -> tuple[int, int]:
    """Ищет почту у контактов, которым некуда написать.

    Возвращает (сколько компаний обработали, у скольких появился канал связи).
    Берём тех, у кого нет ни почты, ни телеграма и к кому мы ещё не ходили.
    Нет и сайта — сначала ищем сайт: из карт он приходит редко, а без него
    компания годится только на обзвон.
    """
    candidates = [
        contact
        for contact in store.contacts(kind, limit=1000)
        if not contact.reachable and not _scanned(contact) and not _resting(contact)
    ][:limit]

    if not candidates:
        return 0, 0

    robots = Robots()
    gained = 0
    with httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    ) as client:
        refusals = 0
        for number, contact in enumerate(candidates, start=1):
            try:
                found = _one(store, contact, client, robots)
            except Exception as exc:  # noqa: BLE001 — один сайт не роняет проход
                logger.warning("{}: обход не удался — {}", contact.name, exc)
                continue
            if _apply(store, contact, found):
                gained += 1

            # Поисковик отказал несколько раз подряд — дальше он будет
            # отдавать заглушку по всем оставшимся, и мы просто сожжём проход,
            # пометив живые компании как «сайт не нашёлся». Останавливаемся:
            # метку «уже ходили» им никто не поставил, вернёмся следующим разом.
            #
            # Считаем только отказы ПОИСКА. Прилёгший сайт компании — беда
            # одной компании, и три таких подряд встречаются на обычной базе
            # сплошь и рядом: раньше они обрывали весь проход, а в логе
            # значилось, что виноват поисковик.
            refusals = refusals + 1 if found.search_failed else 0
            if refusals >= SEARCH_GIVE_UP_AFTER:
                logger.warning(
                    "Поиск отказывает {} раз подряд — останавливаю обход на {} из {}",
                    refusals, number, len(candidates),
                )
                return number, gained

    logger.info("Обход сайтов: {} компаний, каналов добавилось {}", len(candidates), gained)
    return len(candidates), gained


# Куда кладём адрес, найденный поиском, пока он не подтверждён обходом.
# В `contact.site` его класть нельзя: оттуда он читается как «сайт из выгрузки
# парсера», то есть заведомо принадлежащий компании, и проверять его больше
# никто не станет. А совсем не запоминать — значит платить поисковику заново
# каждый проход за компанию, чей сайт в тот раз просто не открылся.
CANDIDATE_KEY = "site_candidate"


def _one(store, contact: Contact, client: httpx.Client, robots: Robots) -> Found:
    """Один контакт: при необходимости найти сайт, потом обойти его."""
    known_site = bool(contact.site)
    site = contact.site
    if not site:
        site = str(contact.meta.get(CANDIDATE_KEY) or "")
        if not site:
            try:
                site = find_site(contact.name, contact.city, client=client)
            except SearchUnavailable as exc:
                # Вернёмся к этой компании позже: сейчас мы не искали, а
                # получили от поисковика отказ.
                return Found(error=f"поиск недоступен: {exc}", retry=True, search_failed=True)
            if not site:
                return Found(error="сайт не нашёлся")
        contact.site = site
        contact.meta["site_source"] = "поиск"
        contact.meta[CANDIDATE_KEY] = site

    return scan(
        site,
        client=client,
        robots=robots,
        # Сайт из выгрузки принадлежит компании по определению, а найденный
        # поиском — ещё надо доказать.
        belongs_to_contact=None if known_site else contact,
    )


SCANNED_KEY = "site_scanned"
# Когда мы в последний раз наткнулись на временную помеху по этому контакту.
RETRY_KEY = "site_retry_at"
# Сколько ждать перед следующим заходом на контакт с временной помехой.
#
# Без ожидания проход не двигался с места: список кандидатов идёт по номеру в
# базе, берёт первые двадцать и отсеивает только тех, к кому уже ходили. Двум
# десяткам компаний с намертво лежащими сайтами хватало, чтобы занять всю
# двадцатку на каждом проходе — остальные несколько тысяч не проверялись
# никогда, и заметить это было нечем.
RETRY_AFTER_HOURS = 12


def _resting(contact: Contact) -> bool:
    """Ждём ли мы по этому контакту паузы после временной помехи."""
    from datetime import datetime, timedelta

    from .store import MSK

    raw = str(contact.meta.get(RETRY_KEY) or "")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=MSK)
    return datetime.now(MSK) - when < timedelta(hours=RETRY_AFTER_HOURS)


def _scanned(contact: Contact) -> bool:
    """Уже ходили на этот сайт. Второй заход даст то же самое."""
    return bool(contact.meta.get(SCANNED_KEY))


def _apply(store, contact: Contact, found: Found) -> bool:
    """Записывает найденное. True — контакту теперь есть куда написать.

    Пометки кладём через update_meta, а не set_meta. Разница не косметическая:
    между чтением контакта и этим моментом прошли минуты обхода чужого сайта,
    и владелец за это время мог нажать «С чего начать» или «Перезвонить».
    Запись всего словаря целиком стирала его правку снимком минутной давности.
    """
    from .store import now_msk

    changes: dict = {}
    drop: list[str] = []
    if found.error:
        changes["site_scan_error"] = found.error
    else:
        drop.append("site_scan_error")

    if found.retry:
        # Помеха временная — метку «уже ходили» не ставим, иначе компания
        # выпадет из обхода навсегда из-за одной чужой заглушки. Найденный
        # адрес при этом остаётся в CANDIDATE_KEY: следующий проход возьмёт
        # его оттуда и не станет платить поисковику за тот же ответ.
        #
        # Но и возвращаться к ней сразу же незачем: сайт, не открывшийся
        # минуту назад, не откроется и через минуту. Отметка времени двигает
        # контакт в конец очереди обхода — без неё двадцать компаний с
        # мёртвыми сайтами занимали всю выборку на каждом проходе, и остальные
        # несколько тысяч не проверялись никогда.
        # Отдых назначаем только за СВОЙ сайт. Отказ поисковика — беда общая,
        # и проход из-за неё останавливается целиком (см. SEARCH_GIVE_UP_AFTER):
        # наказывать за неё отдельную компанию не за что, к ней вернутся, как
        # только поиск оживёт.
        if not found.search_failed:
            changes[RETRY_KEY] = now_msk()
        if CANDIDATE_KEY in contact.meta:
            changes[CANDIDATE_KEY] = contact.meta[CANDIDATE_KEY]
        contact.meta = store.update_meta(contact.pk, changes, drop=tuple(drop))
        return False

    changes[SCANNED_KEY] = now_msk()
    drop.append(RETRY_KEY)  # приговор вынесен, ждать больше нечего

    if not found.verified:
        # Поиск привёл на чужой сайт. Чужие контакты в карточке компании хуже
        # пустой карточки: письмо уйдёт не туда, и это заметит получатель.
        logger.info("{}: сайт {} не подтвердился — контакты не беру", contact.name, contact.site)
        drop.append("site_source")
        # И кандидата, и пометку «сайт есть» снимаем вместе с самим сайтом.
        # Пометку читает модель: с ней она пишет компании письмо, исходя из
        # того, что сайт у той есть, — а мы этот сайт только что стёрли как
        # чужой. Оставленный кандидат вернул бы нас на тот же чужой сайт
        # следующим проходом.
        drop.append(CANDIDATE_KEY)
        contact.meta = store.update_meta(contact.pk, changes, drop=tuple(drop))
        store.set_site(contact.pk, None)
        return False

    if contact.meta.get(CANDIDATE_KEY):
        drop.append(CANDIDATE_KEY)
        # Сайт нашёлся поиском и подтвердился обходом — теперь он настоящий.
        #
        # Пометка «есть ли сайт» приехала из выгрузки парсера и с тех пор не
        # менялась. Без обновления модель получит «сайта нет» и напишет
        # компании письмо про то, что у неё нет сайта, — а он есть, мы его
        # только что нашли и прочитали.
        changes["has_site"] = "есть"

    contact.emails = list(dict.fromkeys([*contact.emails, *found.emails]))
    contact.telegram = list(dict.fromkeys([*contact.telegram, *found.telegram]))
    if found.source_page:
        # Откуда взяли адрес и когда. Пригодится, если однажды спросят.
        changes["email_source"] = found.source_page
        changes["email_found_at"] = now_msk()
    store.upsert_contact(contact)
    # Пометки пишем отдельно: upsert умеет только дополнять, а нам иногда
    # нужно снять ключ, а не добавить.
    contact.meta = store.update_meta(contact.pk, changes, drop=tuple(drop))

    if not found.anything:
        return False
    store.log_event(
        contact.pk,
        "site_scan",
        f"с сайта: {', '.join(found.emails + found.telegram)}",
    )
    logger.info("{}: нашёл {}", contact.name, found.emails + found.telegram)
    return True

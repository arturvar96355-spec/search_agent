"""Извлечение контактов и меток с сайта агентства (режим partners).

Источник-агностично: на входе — текст страницы и ссылки (href), на выходе — email/телефоны/
Telegram и подсказки для ручной оценки (red_flags, niche_tags). Сеть живёт в providers/site.py.

Разбор совпадений — через re.finditer (по требованию проекта), не findall.
Ничего не выдумываем: не нашли контакт — возвращаем пусто, а не догадку.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import unquote

from .models import Email, normalize_phone

# --- email --------------------------------------------------------------------

# Достаточно строгий шаблон, чтобы не хватать мусор вида «слово@слово» без TLD.
# Буквы намеренно не ограничены латиницей: «почта@компания.рф» — обычный
# рабочий адрес российской компании, а не мусор. Латинская проверка не
# находила его вовсе, и у такой компании контактов не оказывалось совсем.
# Домен верхнего уровня — только буквы: цифры в нём не бывают. Имена файлов
# («logo@2x.png») отсеивает список _EMAIL_STOP_TLD ниже — там расширения
# перечислены поимённо.
_EMAIL_RE = re.compile(
    r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}",
    re.UNICODE,
)

# Файлы, которые иногда выглядят как email в разметке (logo@2x.png, doc@v2.pdf).
_EMAIL_STOP_TLD = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "ico", "woff", "woff2",
    "pdf", "doc", "docx", "xls", "xlsx", "zip", "rar", "mp4", "webm", "json", "xml",
}

# Почты платформ/сервисов, которые попадают в разметку чужим текстом («Made on Tilda»,
# виджеты, шаблоны). Это НЕ контакты агентства — их брать нельзя (ничего не выдумываем).
_EMAIL_STOP_DOMAINS = frozenset({
    "madeontilda.com", "tilda.cc", "tilda.ws", "wix.com", "wixsite.com", "readymag.com",
    "squarespace.com", "shopify.com", "bitrix24.ru", "bitrix.ru", "1c-bitrix.ru",
    "sentry.io", "google.com", "googlemail.com", "example.com", "example.ru", "domain.ru",
    "jivosite.com", "carrotquest.io", "envybox.io", "calltouch.ru",
    "roistat.com", "amocrm.ru",
    # Заглушки из форм обратной связи («введите почту, например…»). Такой адрес
    # выглядит как настоящий, а письмо по нему уходит в никуда.
    "email.com", "mail.com", "yourmail.ru", "yourdomain.ru", "site.ru", "company.ru",
    "test.ru", "test.com", "email.ru", "mydomain.ru", "name.ru",
})

# Локальные части-образцы: «example@», «your-email@», «username@».
_EMAIL_STOP_LOCAL = frozenset({
    "example", "sample", "your-email", "youremail", "your_mail", "yourmail",
    "email", "e-mail", "test", "username", "user", "name",
})

# Локальные части -> тип email. Порядок проверки важен: partner > sales > general.
# Без завершающего \b: нужно ловить partnership@, info_msk@, sales2@, salesdept@.
_EMAIL_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # noreply — раньше остальных: адрес «не отвечайте» — это не контакт, а
    # табличка на двери. Он классифицировался как general и, стоя первым в
    # списке, побеждал живой info@ в best_email — письмо уходило в никуда,
    # притом что настоящий ящик был известен.
    ("noreply", re.compile(
        r"^(?:no-?reply|donotreply|do-not-reply|noreply|robot|notif|mailer-daemon|postmaster|bounce)",
        re.IGNORECASE,
    )),
    ("partner", re.compile(r"^(?:partner|ref|referral|agent|affiliate|franch)", re.IGNORECASE)),
    ("sales", re.compile(r"^(?:sales|sale|lead|zayav|zakaz|order|client|crm|shop|market)", re.IGNORECASE)),
    ("general", re.compile(
        r"^(?:info|hello|hi|mail|e?-?mail|office|admin|support|help|contact|team|welcome|reply|"
        r"hr|job|vacanc|career|resume|"
        r"press|pr|media|billing|account|invoice|finance|buh|legal|secretar|reception|feedback|"
        r"director|ceo|boss|manager|sekretar|zakupki|tender)", re.IGNORECASE)),
]

# Похоже на имя человека: ivan@, a.petrov@, i_ivanov@, ivan.petrov2@.
_PERSONAL_RE = re.compile(r"^[a-z]{2,}(?:[._-][a-z]{2,})?\d{0,3}$", re.IGNORECASE)


def classify_email(address: str) -> str:
    """Тип email по локальной части: partner | sales | general | personal.

    personal ставим только если локальная часть ПОХОЖА на имя человека (ivan@, a.petrov@) —
    иначе неизвестные технические адреса (noreply@, zakupki@) попадали в personal и, так как
    personal стоит вторым по приоритету, вытесняли настоящие контакты из best_email.
    """
    local = address.split("@")[0].strip().lower()
    for kind, pattern in _EMAIL_KIND_PATTERNS:
        if pattern.match(local):
            return kind
    return "personal" if _PERSONAL_RE.match(local) else "general"


def extract_emails(text: str | None, hrefs: Iterable[str] = ()) -> list[Email]:
    """Email'ы из текста страницы + отдельно из mailto:-ссылок. Дедуп по адресу.

    mailto собираем отдельно: там адрес лежит явно, даже если в тексте он картинкой/скриптом.
    """
    found: dict[str, Email] = {}

    def _add(raw: str) -> None:
        address = raw.strip().strip(".,;:()<>[]\"'").lower()
        if not address or "@" not in address:
            return
        tld = address.rsplit(".", 1)[-1]
        if tld in _EMAIL_STOP_TLD:
            return  # это файл, а не адрес
        domain = address.split("@")[-1]
        if domain in _EMAIL_STOP_DOMAINS:
            return  # почта конструктора/виджета, а не агентства
        if address.split("@")[0] in _EMAIL_STOP_LOCAL:
            return  # образец из формы, а не живой адрес
        if address not in found:
            found[address] = Email(address=address, kind=classify_email(address))

    for href in hrefs:  # mailto:info@agency.ru?subject=… , mailto:%69nfo@a.ru , "mailto: info@a.ru"
        if href and href.strip().lower().startswith("mailto:"):
            candidate = href.strip()[7:].split("?")[0].strip()
            candidate = unquote(candidate)  # percent-кодирование в href встречается часто
            for match in _EMAIL_RE.finditer(candidate):  # re.finditer, не findall
                _add(match.group())

    if text:
        for match in _EMAIL_RE.finditer(text):  # re.finditer, не findall
            _add(match.group())

    return list(found.values())


# --- телефоны и Telegram ------------------------------------------------------

# Российский номер в произвольном тексте: +7/8 и 10 цифр с любыми разделителями.
# Разделители: пробелы, дефис/тире (в т.ч. типографские), скобки, точки, неразрывный пробел.
_PHONE_SEP = r"[\s\-\u2010-\u2015\u00a0().]*"
# «7» без плюса — тоже начало номера: так пишут в ссылках tel:79161234567 и
# просто в тексте. Раньше такой номер не находился вовсе, и у карточки не
# оказывалось телефона — то есть лид без единственного, ради чего он нужен.
# Спутать с ИНН или ОГРН нельзя: там цифр больше десяти после первой, и
# граница слова в конце не сойдётся.
_PHONE_RE = re.compile(
    r"(?:\+7|\b8|\b7)" + _PHONE_SEP + r"\d{3}" + _PHONE_SEP + r"\d{3}"
    + _PHONE_SEP + r"\d{2}" + _PHONE_SEP + r"\d{2}\b"
)

# t.me/handle и @handle. Для @ требуем, чтобы слева не было символа слова — иначе поймаем email.
_TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?P<handle>[A-Za-z][\w\d_]{3,31})", re.IGNORECASE
)
# @handle берём ТОЛЬКО рядом со словом про Telegram — иначе в TG попадали хэндлы
# инстаграма/VK («мы в инстаграм @agency»), т.е. выдуманный контакт.
_TELEGRAM_AT_RE = re.compile(
    r"(?:telegram|телеграм|телега|tg|тг)\W{0,20}@(?P<handle>[A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.IGNORECASE,
)

# Служебные пути t.me, которые не являются аккаунтами.
_TELEGRAM_STOP = {"share", "joinchat", "addstickers", "iv", "proxy", "socks"}


def extract_phones(text: str | None) -> list[str]:
    """Телефоны РФ из текста, нормализованные в +7XXXXXXXXXX (через существующий normalize_phone)."""
    if not text:
        return []
    phones: list[str] = []
    for match in _PHONE_RE.finditer(text):  # re.finditer, не findall
        phone = normalize_phone(match.group())
        if phone and phone not in phones:
            phones.append(phone)
    return phones


def extract_telegram(text: str | None, hrefs: Iterable[str] = ()) -> list[str]:
    """Telegram-аккаунты: из ссылок t.me и из упоминаний @handle в тексте."""
    handles: list[str] = []

    def _add(handle: str) -> None:
        handle = handle.strip().lower()
        if handle and handle not in _TELEGRAM_STOP:
            link = f"https://t.me/{handle}"
            if link not in handles:
                handles.append(link)

    for source in (" ".join(h for h in hrefs if h), text or ""):
        for match in _TELEGRAM_LINK_RE.finditer(source):  # re.finditer, не findall
            _add(match.group("handle"))

    if text:
        for match in _TELEGRAM_AT_RE.finditer(text):
            _add(match.group("handle"))

    return handles


# --- метки для ручной оценки --------------------------------------------------

# red_flags: подсказка владельцу, НЕ приговор — помеченное агентство остаётся в выгрузке.
RED_FLAG_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "конкурент": [
        re.compile(r"разработка\s+CRM", re.IGNORECASE),
        re.compile(r"сво[ийе]\w*\s+(?:чат-?)?бот(?:ы|а|ов|)\b", re.IGNORECASE),  # не «ботинки»
        re.compile(r"(?:IT|ИТ)[-\s]решени\w+", re.IGNORECASE),
        re.compile(r"интегратор\w*\s+битрикс", re.IGNORECASE),
        re.compile(r"собственн\w+\s+платформ\w+", re.IGNORECASE),
        re.compile(r"\b(?:AI|ИИ)[-\s]?агент(?!ств)\w*", re.IGNORECASE),  # не «AI-агентство»
        re.compile(r"вс[её]\s+инхаус", re.IGNORECASE),
        re.compile(r"чат[-\s]бот\w*\s+под\s+ключ", re.IGNORECASE),
    ],
    "медийный": [
        re.compile(r"ТВ[-\s]реклам\w+", re.IGNORECASE),
        re.compile(r"наружн\w+\s+реклам\w+", re.IGNORECASE),
        re.compile(r"медиабаинг", re.IGNORECASE),
        re.compile(r"\bOOH\b", re.IGNORECASE),
        re.compile(r"реклама\s+на\s+радио", re.IGNORECASE),
    ],
    "не наш сегмент": [
        re.compile(r"\bFMCG\b", re.IGNORECASE),
        re.compile(r"\bфарм(?:а|ацевт\w+|индустри\w+|компани\w+|рынк\w+|\b)", re.IGNORECASE),
        re.compile(r"\b(?:банк\w*|финанс\w+|страхов\w+|микрозайм\w*|МФО|финтех\w*)\b", re.IGNORECASE),
        re.compile(r"федеральн\w+\s+ритейл\w*", re.IGNORECASE),
        re.compile(r"крупн\w+\s+ритейл\w*", re.IGNORECASE),
    ],
}

# PR-метку ставим только при отсутствии слов про трафик/рекламу (см. detect_red_flags).
_PR_PATTERNS = [
    re.compile(r"управлени\w+\s+репутаци\w+", re.IGNORECASE),
    re.compile(r"\bрепутаци\w+", re.IGNORECASE),
    re.compile(r"media\s+relations", re.IGNORECASE),
    re.compile(r"\bGR\b"),
    re.compile(r"PR[-\s]агентств\w+", re.IGNORECASE),
]

# Признаки того, что агентство всё-таки про трафик (тогда PR-метку не ставим).
_TRAFFIC_PATTERNS = [
    re.compile(r"яндекс[\s.]*директ", re.IGNORECASE),
    re.compile(r"контекстн\w+\s+реклам\w+", re.IGNORECASE),
    re.compile(r"таргет\w*", re.IGNORECASE),
    re.compile(r"\bSEO\b", re.IGNORECASE),
    re.compile(r"\bSMM\b", re.IGNORECASE),
    re.compile(r"лидоген\w+", re.IGNORECASE),
    re.compile(r"performance", re.IGNORECASE),
    re.compile(r"трафик\w*", re.IGNORECASE),
]

# niche_tags: ниши, на которых агентство специализируется (по упоминаниям на сайте).
NICHE_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "стоматология": [
        re.compile(r"стоматолог\w+", re.IGNORECASE),
        re.compile(r"зубн\w+\s+клиник\w+", re.IGNORECASE),
    ],
    "клиники": [
        re.compile(r"медицинск\w+\s+(?:центр\w*|клиник\w+|маркетинг\w*)", re.IGNORECASE),
        re.compile(r"\bклиник\w*", re.IGNORECASE),
        re.compile(r"медцентр\w*", re.IGNORECASE),
    ],
    "авто": [
        re.compile(r"автосервис\w*", re.IGNORECASE),
        re.compile(r"автотехцентр\w*", re.IGNORECASE),
        re.compile(r"автодилер\w*", re.IGNORECASE),
        re.compile(r"шиномонтаж\w*", re.IGNORECASE),
    ],
    "HoReCa": [
        re.compile(r"\bHoReCa\b", re.IGNORECASE),
        re.compile(r"ресторан\w*", re.IGNORECASE),
        re.compile(r"\bкафе\b", re.IGNORECASE),
        re.compile(r"доставк\w+\s+ед\w+", re.IGNORECASE),
    ],
    "бьюти": [
        re.compile(r"салон\w*\s+красот\w+", re.IGNORECASE),
        re.compile(r"бьюти", re.IGNORECASE),
        re.compile(r"косметолог\w+", re.IGNORECASE),
        re.compile(r"барбершоп\w*", re.IGNORECASE),
    ],
    "недвижимость": [
        re.compile(r"недвижимост\w+", re.IGNORECASE),
        re.compile(r"застройщик\w*", re.IGNORECASE),
        re.compile(r"посуточн\w+\s+аренд\w+", re.IGNORECASE),
        re.compile(r"агентств\w+\s+недвижимост\w+", re.IGNORECASE),
    ],
}


def _any_match(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    """Есть ли хоть одно совпадение (через finditer — без findall)."""
    for pattern in patterns:
        for _match in pattern.finditer(text):
            return True
    return False


def detect_red_flags(text: str | None) -> list[str]:
    """Метки-предупреждения по тексту сайта. Пусто — значит явных стоп-сигналов нет."""
    if not text:
        return []
    flags: list[str] = []
    for flag, patterns in RED_FLAG_PATTERNS.items():
        if _any_match(patterns, text):
            flags.append(flag)
    # PR — только если про трафик/рекламу на сайте ничего нет (иначе это обычное агентство).
    if _any_match(_PR_PATTERNS, text) and not _any_match(_TRAFFIC_PATTERNS, text):
        flags.append("PR")
    return flags


def detect_niches(text: str | None) -> list[str]:
    """Ниши агентства по упоминаниям на сайте. Пусто -> универсальное (метку ставит вызывающий)."""
    if not text:
        return []
    return [niche for niche, patterns in NICHE_PATTERNS.items() if _any_match(patterns, text)]


# --- уровень собственной разработки (определяет ПИТЧ, см. бриф продаж) ---------

# Сайты на конструкторах: разработка есть, но кастомной CRM нет -> питч только про CRM.
_CONSTRUCTOR_PATTERNS = [
    re.compile(r"\bTilda\b|тильд\w*", re.IGNORECASE),
    re.compile(r"\bWordPress\b|вордпресс", re.IGNORECASE),
    # Латинская C, а не кириллическая: здесь стояла «С» из русской раскладки,
    # и слово Creatium, написанное как его пишут, не находилось никогда.
    re.compile(r"\bWix\b|\buKit\b|\bCreatium\b|креатиум", re.IGNORECASE),
    re.compile(r"\bOpenCart\b|\bInSales\b|\bShopify\b", re.IGNORECASE),
    re.compile(r"сайт\w*\s+на\s+(?:тильде|битриксе|вордпресс\w*|конструктор\w+)", re.IGNORECASE),
    re.compile(r"лендинг\w*\s+на\s+\w+", re.IGNORECASE),
]

# СИЛЬНЫЕ признаки своей разработки: перебивают даже упоминание конструкторов.
_STRONG_FULL_DEV_PATTERNS = [
    re.compile(r"собственн\w+\s+(?:отдел\w*\s+)?разработк\w+", re.IGNORECASE),
    re.compile(r"сво[йя]\w*\s+(?:отдел|команд\w+|штат)\w*\s+разработ\w+", re.IGNORECASE),
    re.compile(r"разработк\w+\s+полн\w+\s+цикл\w+", re.IGNORECASE),
    re.compile(r"\bбэкенд\b|\bbackend\b|\bfullstack\b|фулстек", re.IGNORECASE),
    re.compile(r"\b(?:React|Vue|Angular|Laravel|Django|Node\.js|Symfony|Kotlin|Swift)\b"),
    re.compile(r"веб[-\s]студи\w+|студи\w+\s+разработк\w+", re.IGNORECASE),
    re.compile(r"мобильн\w+\s+приложени\w+\s+(?:под\s+ключ|разработ\w+)", re.IGNORECASE),
    re.compile(r"аккредитац\w+\s+ИТ|реестр\w*\s+росс\w+\s+ПО|росПО", re.IGNORECASE),
]

# Разработка ЗАЯВЛЕНА как услуга. Если рядом нет конструкторов — считаем полным циклом.
# Именно этого не хватало: Текарт пишет «веб-разработка», но не пишет «собственный отдел».
_DEV_SERVICE_PATTERNS = [
    re.compile(r"веб[-\s]?разработ\w+|web[-\s]?разработ\w+", re.IGNORECASE),
    re.compile(
        r"(?:разработк\w+|создани\w+)\s+(?:сайт\w+|порталов|интернет[-\s]магазин\w+|сервис\w+)",
        re.IGNORECASE,
    ),
    re.compile(r"разработк\w+\s+(?:мобильн\w+\s+)?приложени\w+", re.IGNORECASE),
    re.compile(r"интернет[-\s]магазин\w*\s+под\s+ключ", re.IGNORECASE),
    re.compile(r"(?:ИТ|IT)[-\s]поддержк\w+|техподдержк\w+\s+сайт\w+", re.IGNORECASE),
    re.compile(r"(?:высоконагруж\w+|корпоративн\w+)\s+(?:систем\w+|портал\w+|решени\w+)", re.IGNORECASE),
]

# Признаки того, что разработки/сайтов в услугах нет вообще (чистый SMM/таргет/PR).
_NO_DEV_HINT = [
    re.compile(r"\b(?:SMM|таргет\w*|соцсет\w+|контент[-\s]маркетинг\w*)\b", re.IGNORECASE),
]


def detect_dev_level(text: str | None) -> str:
    """Уровень своей разработки: 'full' | 'constructors' | 'none' | 'unknown'.

    Прямо определяет питч (по брифу продаж):
      none          -> предлагаем сайты И CRM (самая тёплая цель);
      constructors  -> предлагаем ТОЛЬКО CRM (сайты они и так делают на конструкторах);
      full          -> пропускаем: у них свой продакшн, мы им не нужны.
    """
    if not text:
        return "unknown"
    # 1) Сильные признаки своего продакшна — сразу full (даже если где-то мелькает Tilda).
    if _any_match(_STRONG_FULL_DEV_PATTERNS, text):
        return "full"
    has_constructors = _any_match(_CONSTRUCTOR_PATTERNS, text)
    has_dev_service = _any_match(_DEV_SERVICE_PATTERNS, text)
    # 2) Разработка заявлена и рядом конструкторы -> сайты делают на них (питч: только CRM).
    if has_dev_service and has_constructors:
        return "constructors"
    # 3) Разработка заявлена без конструкторов -> свой продакшн, нам такие не нужны.
    if has_dev_service:
        return "full"
    if has_constructors:
        return "constructors"
    if _any_match(_NO_DEV_HINT, text):
        return "none"
    return "unknown"


# Крупные сетевые агентства: свой продакшн и тендерные процедуры — по брифу НЕ наша цель.
BIG_AGENCY_NAMES = (
    "nectarin", "setters", "kokoc", "demis", "ingate", "ashmanov", "articul",
    "isee marketing", "molinos", "agima", "redkeds", "possible", "dentsu", "publicis",
    "havas", "mgcom", "adventum", "arrow media", "artics", "i-media", "profitator",
)


def looks_like_big_agency(name: str | None, catalog_info: str | None = None) -> bool:
    """Похоже ли на крупного сетевого игрока (тендеры, свой продакшн) — таких пропускаем."""
    low = (name or "").strip().lower()
    if any(big in low for big in BIG_AGENCY_NAMES):
        return True
    # Верхние места рейтинга — обычно крупняк; берём как сигнал, а не приговор.
    if catalog_info:
        match = re.search(r"место\s+(\d+)", catalog_info)
        if match and int(match.group(1)) <= 10:
            return True
    return False

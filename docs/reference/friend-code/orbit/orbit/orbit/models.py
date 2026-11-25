"""Общие сущности Orbit: контакт, канал связи и стадия воронки.

Одна воронка на все три системы. Партнёр, компания с карты и заказчик с биржи
приходят по-разному, но дальше живут одинаково: написали — ответил — созвон —
работа. Общие стадии позволяют держать один экран статусов, а не три разных.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    """Откуда контакт и как с ним разговаривать."""

    PARTNER = "partner"    # агентство: заходим с субподряда
    CLIENT = "client"      # компания с карт: заходим с её боли
    ORDER = "order"        # заказчик с биржи: ведёт freelance-bid


class Channel(StrEnum):
    """Чем можем достать контакт. Порядок = приоритет выбора."""

    EMAIL = "email"
    TELEGRAM = "telegram"
    PHONE = "phone"        # писать некуда, только звонить руками


class Stage(StrEnum):
    """Стадия воронки. Значения общие для всех трёх систем."""

    NEW = "new"            # в базе, ещё не трогали
    QUEUED = "queued"      # текст готов, ждёт отправки
    WRITTEN = "written"    # написали (на бирже — отправили отклик)
    REPLIED = "replied"    # ответил, пошёл разговор
    CALL = "call"          # готов к созвону
    DEAL = "deal"          # взяли в работу
    DEAD = "dead"          # не подходит, отказ или «стоп»


# Как стадия называется на экране. Держим здесь, чтобы подписи не разъехались
# между вкладками.
STAGE_TITLES: dict[Stage, str] = {
    Stage.NEW: "Новые",
    Stage.QUEUED: "Готовы к отправке",
    Stage.WRITTEN: "Написали",
    Stage.REPLIED: "Начали общение",
    Stage.CALL: "Готовы к созвону",
    Stage.DEAL: "В работе",
    Stage.DEAD: "Не подошли",
}

STAGE_ICONS: dict[Stage, str] = {
    Stage.NEW: "🆕",
    Stage.QUEUED: "✉️",
    Stage.WRITTEN: "📤",
    Stage.REPLIED: "💬",
    Stage.CALL: "📞",
    Stage.DEAL: "🤝",
    Stage.DEAD: "🚫",
}

# Порядок для экрана: как контакт движется по воронке.
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.NEW,
    Stage.QUEUED,
    Stage.WRITTEN,
    Stage.REPLIED,
    Stage.CALL,
    Stage.DEAL,
    Stage.DEAD,
)

# На бирже те же стадии называются иначе — там мы откликаемся, а не пишем письмо.
FREELANCE_TITLES: dict[Stage, str] = {
    **STAGE_TITLES,
    Stage.WRITTEN: "Откликнулись",
    Stage.DEAD: "Отказались",
}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
# Ящики на бесплатных доменах — это личная почта человека, а не адрес компании.
# Писать туда холодные письма нельзя (см. регламент рассылки).
FREE_MAIL_DOMAINS = frozenset({
    # Россия и СНГ
    "mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru", "pochta.ru",
    "newmail.ru", "land.ru", "nm.ru", "front.ru", "qip.ru", "narod.ru",
    "yandex.ru", "ya.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.ua",
    "rambler.ru", "lenta.ru", "autorambler.ru", "myrambler.ru", "ro.ru",
    "tut.by", "ukr.net", "i.ua", "meta.ua", "bigmir.net", "mail.ua",
    # Мировые
    "gmail.com", "googlemail.com", "outlook.com", "outlook.ru", "hotmail.com",
    "hotmail.ru", "live.com", "live.ru", "msn.com", "icloud.com", "me.com",
    "mac.com", "yahoo.com", "ymail.com", "rocketmail.com", "aol.com",
    "gmx.com", "gmx.net", "gmx.de", "web.de", "zoho.com", "fastmail.com",
    "proton.me", "protonmail.com", "pm.me", "tutanota.com", "tuta.io",
    # mail.com — такой же бесплатный ящик, как gmail, и в список не попал
    # только потому, что похож на корпоративный домен. На письмо оттуда
    # система смотрела как на рабочую почту компании.
    "mail.com", "email.com", "usa.com", "consultant.com", "seznam.cz",
})


def is_corporate_email(address: str) -> bool:
    """Корпоративный ли ящик. Пустой или личный адрес не годится для рассылки."""
    address = (address or "").strip().lower()
    if not _EMAIL_RE.match(address):
        return False
    return address.rsplit("@", 1)[-1] not in FREE_MAIL_DOMAINS


# Обезличенные ящики: указывают на отдел или функцию, а не на человека.
# Разница не косметическая. Именной адрес (ivanov@company.ru) по 152-ФЗ —
# персональные данные со всеми обязанностями оператора; обезличенный —
# просто контакт организации. Поэтому холодное письмо шлём на обезличенный,
# даже если именной нашёлся первым.
ROLE_MAILBOXES = frozenset({
    "info", "mail", "office", "sales", "zakaz", "order", "orders", "shop",
    "hello", "hi", "contact", "contacts", "support", "help", "admin",
    "partner", "partners", "partnership", "pr", "marketing", "reklama",
    "secretary", "reception", "welcome", "team", "corp", "company", "post",
})


def is_role_email(address: str) -> bool:
    """Обезличенный ли ящик (info@, sales@, office@…)."""
    address = (address or "").strip().lower()
    if not is_corporate_email(address):
        return False
    local = address.split("@", 1)[0]
    # «info.msk» и «sales-spb» — те же отделы, просто с уточнением.
    head = re.split(r"[.\-_+]", local)[0]
    return head in ROLE_MAILBOXES


def same_company(site: str | None, address: str | None) -> bool:
    """Похоже ли, что адрес принадлежит той же компании, что и сайт.

    Не строгое равенство: у одной компании бывает `.ru` и `.com`, поддомен
    (`stomatologiya.doct.ru` при почте на `doct.ru`) или отдельный домен под
    бренд. Сравниваем по значимому куску имени.

    Пусто с любой стороны — считаем, что сравнивать не с чем, и не спорим:
    отсутствие сайта не улика.
    """
    own = _domain_of(site)
    mail = (address or "").strip().lower().rsplit("@", 1)[-1]
    if not own or not mail or "@" not in (address or ""):
        return True
    if own == mail or own.endswith("." + mail) or mail.endswith("." + own):
        return True
    # «blackline-pr.ru» и «blackline-pr.com» — одна компания; «zavod.team» и
    # «altfm.ru» — разные. Сравниваем имя без зоны.
    return _core_label(own) == _core_label(mail)


# Второй уровень доменной зоны: «com.ru», «co.uk», «org.ua». Только он и
# бывает зоной в середине имени — всё остальное принадлежит компании.
_SECOND_LEVEL_ZONES = frozenset({"com", "net", "org", "co", "edu", "gov", "ac", "pp"})


def looks_foreign(site: str | None, address: str | None) -> bool:
    """Адрес выглядит принадлежащим другой компании, чем сайт.

    Отдельной функцией, а не только свойством контакта, потому что спрашивать
    надо про ТОТ адрес, который получит письмо. Свойство судит по первому
    кандидату, а отправка берёт первый ПРОШЕДШИЙ проверку: у компании с двумя
    ящиками первый бывает забракован, и предупреждение «домен не связан с
    сайтом» относилось не к тому адресу — то есть пропадало ровно в том
    случае, ради которого заведено.
    """
    return bool(site) and bool(address) and not same_company(site, address)


def _core_label(domain: str) -> str:
    """Значимая часть домена: без зоны и без www.

    Зона — это ПОСЛЕДНИЙ кусок имени, какой бы он ни был. Раньше зоны
    перечислялись списком, и всё, чего в списке не оказалось, считалось частью
    имени компании: `smmashing.media` и `wunder-digital.media` давали одно и то
    же ядро «media», то есть два разных агентства признавались одной компанией.
    Список новых зон бесконечен (.media, .digital, .studio, .agency, .art), и
    угнаться за ним нельзя — поэтому не перечисляем, а отрезаем.
    """
    parts = [p for p in domain.split(".") if p and p != "www"]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    parts = parts[:-1]  # последний кусок — зона всегда
    if len(parts) > 1 and parts[-1] in _SECOND_LEVEL_ZONES:
        parts = parts[:-1]  # «example.com.ru» → «example»
    return parts[-1]


def _domain_of(site: str | None) -> str:
    """Домен сайта без схемы и www. Пусто — сайт неизвестен."""
    value = (site or "").strip().lower()
    if not value:
        return ""
    value = re.sub(r"^https?://", "", value).split("/", 1)[0]
    return value.removeprefix("www.")


def normalize_telegram(raw: str) -> str | None:
    """Ссылка или ник телеграма → «@handle». Мусор → None."""
    value = (raw or "").strip()
    if not value:
        return None
    match = re.search(r"(?:t\.me/|telegram\.me/|@)([A-Za-z0-9_]{4,32})", value)
    if match:
        return "@" + match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_]{4,32}", value):
        return "@" + value
    return None


@dataclass
class Contact:
    """Один контакт в базе Orbit.

    pk = None у ещё не сохранённого. `key` — ключ дедупликации: домен сайта
    для компаний, ссылка на заказ для бирж. По нему же ловим повторную запись
    из другой выгрузки.
    """

    kind: Kind
    key: str
    name: str
    stage: Stage = Stage.NEW
    city: str | None = None
    site: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    telegram: list[str] = field(default_factory=list)
    source_url: str | None = None
    # Всё, что нужно для текста письма и для карточки: рубрика, рейтинг,
    # питч, жалобы из отзывов. Хранится как есть, разные источники дают разное.
    meta: dict = field(default_factory=dict)
    note: str | None = None
    pk: int | None = None

    @property
    def email_candidates(self) -> list[str]:
        """Все рабочие адреса по порядку предпочтения.

        Сначала свой домен, потом обезличенный ящик — именно в таком порядке.
        Обе части важны, но первая важнее. Обезличенность — про 152-ФЗ: письмо
        на info@ обращено к организации, на ivanov@ — к человеку. Свой домен —
        про то, той ли компании мы вообще пишем: на сайте института пластической
        хирургии лежало шесть адресов, и `info@` среди них принадлежал чужому
        бренду, а собственные ящики института обезличенными не выглядели. Без
        приоритета домена письмо ушло бы не туда.

        Список, а не один адрес, нужен проверке перед отправкой: если у
        первого адреса домен мёртвый, у компании может найтись второй живой.
        """
        corporate = [address for address in self.emails if is_corporate_email(address)]
        own = _domain_of(self.site)

        def rank(address: str) -> tuple[int, int]:
            same_domain = bool(own) and address.strip().lower().endswith("@" + own)
            return (0 if same_domain else 1, 0 if is_role_email(address) else 1)

        return sorted(corporate, key=rank)

    @property
    def best_email(self) -> str | None:
        """Куда писать: первый из `email_candidates`.

        Порядок задаёт он же — сначала свой домен, внутри домена сначала
        обезличенный ящик. Здесь стояло «сначала обезличенные ящики», и это
        было неправдой: именной адрес на домене компании обходит `info@` на
        чужом. Так и задумано (см. email_candidates), но читавший эту строку
        считал наоборот.
        """
        candidates = self.email_candidates
        return candidates[0] if candidates else None

    @property
    def foreign_email(self) -> bool:
        """Почта выглядит принадлежащей другой компании, чем сайт.

        Не запрет, а повод показать владельцу. Из выгрузки парсера так пришли
        живые примеры: у «Zavod» (zavod.team) единственный адрес — на
        `altfm.ru`, у «HEIGHT LINE» (hl2b.ru) — на `dadcan.ru`. Письмо туда
        уходит с рассказом о чужом бизнесе, и это заметит получатель.

        Блокировать нельзя: у «Чудо Доктор» сайт на поддомене `doct.ru`, а
        почта на самом `doct.ru` — та же компания. Поэтому только пометка.
        """
        best = self.best_email
        return bool(best) and bool(self.site) and not same_company(self.site, best)

    @property
    def personal_only(self) -> bool:
        """Обезличенного ящика нет — есть только именной."""
        best = self.best_email
        return bool(best) and not is_role_email(best)

    @property
    def best_telegram(self) -> str | None:
        for handle in self.telegram:
            normalized = normalize_telegram(handle)
            if normalized:
                return normalized
        return None

    @property
    def channel(self) -> Channel:
        """Каким каналом идём. Почта первой: письмо не требует ручного шага.

        Телефон — не канал рассылки, а признак «писать некуда». Такие контакты
        уходят в отдельный список на обзвон.
        """
        if self.best_email:
            return Channel.EMAIL
        if self.best_telegram:
            return Channel.TELEGRAM
        return Channel.PHONE

    @property
    def reachable(self) -> bool:
        """Есть ли куда написать (не считая телефона)."""
        return self.channel is not Channel.PHONE

    @property
    def primary_phone(self) -> str | None:
        return self.phones[0] if self.phones else None

    @property
    def url(self) -> str | None:
        """Ссылка на сайт целиком — в карточке она должна кликаться."""
        if not self.site:
            return None
        if self.site.startswith(("http://", "https://")):
            return self.site
        return f"https://{self.site}"


@dataclass
class Draft:
    """Готовый текст для одного контакта в одном канале."""

    contact_pk: int
    channel: Channel
    subject: str
    body: str
    pk: int | None = None
    status: str = "draft"     # draft | sent | failed
    error: str | None = None
    # Когда владелец разрешил отправку. Пока пусто — письмо просто лежит в очереди.
    approved_at: str | None = None
    # Сколько раз пытались отправить и не вышло.
    attempts: int = 0
    # Когда текст написали. Нужен, чтобы не отправить протухшее письмо:
    # касание вдогонку говорит «писал вам недавно», и через месяц это враньё.
    created_at: str | None = None

    @property
    def approved(self) -> bool:
        return bool(self.approved_at)

    @property
    def preview(self) -> str:
        """Первые строки для списка очереди."""
        head = " ".join(self.body.split())
        return head[:160] + ("…" if len(head) > 160 else "")

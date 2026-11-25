"""Модели данных Organization (клиенты) и Agency (партнёры) + помощники нормализации.

Один формат для всех источников (2ГИС, Яндекс, browser). Нормализация телефонов
и извлечение соцсетей живут здесь же, чтобы провайдеры возвращали уже готовый формат.
"""
from __future__ import annotations

import re
from contextlib import suppress
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Source = Literal["2gis", "yandex", "browser"]

# Тип email по локальной части — задаёт приоритет «лучшего» контакта для аутрича.
EmailKind = Literal["partner", "personal", "sales", "general", "noreply"]

# Приоритет выбора best_email: партнёрский > личный > продажи > общий.
# noreply в хвосте намеренно: «не отвечайте» — не контакт. Берётся только
# когда другого адреса нет вовсе — и то как «хоть что-то показать человеку».
EMAIL_PRIORITY: dict[str, int] = {"partner": 0, "personal": 1, "sales": 2, "general": 3, "noreply": 98}

# Только цифры телефона (для нормализации в +7XXXXXXXXXX).
_DIGITS_RE = re.compile(r"\d+")

# Ссылки на соцсети в произвольном тексте (website/описание карточки).
# Разбор совпадений делаем через re.finditer (по требованию проекта), а не findall.
_SOCIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "telegram": re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?P<handle>\w[\w\d_]{2,})", re.IGNORECASE),
    "vk": re.compile(r"(?:https?://)?(?:m\.)?vk\.com/(?P<handle>[\w\d_.]{2,})", re.IGNORECASE),
    "instagram": re.compile(
        r"(?:https?://)?(?:www\.)?instagram\.com/(?P<handle>[\w\d_.]{2,})", re.IGNORECASE
    ),
}

# «Хэндлы», которые на самом деле служебные пути соцсетей, а не аккаунты.
_SOCIAL_STOP_HANDLES = {"share", "sharer", "p", "reel", "reels", "explore", "share_url"}


def normalize_domain(raw: str | None) -> str | None:
    """URL/домен -> нормализованный домен: без схемы, www, пути, порта и слэшей, в нижнем регистре.

    Ключ дедупликации агентств («https://WWW.Agency.RU/about/» -> «agency.ru»).
    Возвращаем None, если домена в строке нет.
    """
    if not raw:
        return None
    value = raw.strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)  # схема
    value = value.split("/")[0].split("?")[0].split("#")[0]  # путь/query/anchor
    value = value.split("@")[-1]  # на случай mailto-подобной строки
    value = value.split(":")[0]  # порт
    if value.startswith("www."):
        value = value[4:]
    value = value.rstrip(".")  # завершающая точка в FQDN («agency.ru.»)
    if not value:
        return None
    # IDN к единой форме (punycode): «клиника.рф» и «xn--80ak6aa92e.рф» — один домен.
    with suppress(UnicodeError, UnicodeDecodeError):
        value = value.encode("idna").decode("ascii")
    return value or None


# Явные заглушки-«рыбы», которые встречаются в шаблонах сайтов: 9999999999, 0000000000,
# 1234567890 и т.п. Это не контакты — брать их нельзя (ничего не выдумываем).
_FAKE_PHONE_TAILS = frozenset({
    "0000000000", "1111111111", "2222222222", "3333333333", "4444444444", "5555555555",
    "6666666666", "7777777777", "8888888888", "9999999999", "1234567890", "9876543210",
    "0123456789", "1234567891",
})


def _looks_fake(digits10: str) -> bool:
    """Похоже на номер-заглушку из шаблона сайта (все цифры одинаковые / прямая последовательность)."""
    if digits10 in _FAKE_PHONE_TAILS:
        return True
    return len(set(digits10)) == 1  # 9999999999 и подобные


def normalize_phone(raw: str) -> str | None:
    """Приводим телефон к формату +7XXXXXXXXXX. Возвращаем None, если это не РФ-номер.

    Разбор цифр через re.finditer, чтобы собрать номер из кусков вроде '+7 (495) 123-45-67'.
    Явные заглушки из шаблонов сайтов (+79999999999) отбрасываем — это не контакт.
    """
    digits = "".join(match.group() for match in _DIGITS_RE.finditer(raw))
    if not digits:
        return None
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:  # номер без кода страны
        digits = "7" + digits
    else:
        return None  # не похоже на РФ-номер — на шаге 5 логику расширим
    if _looks_fake(digits[1:]):
        return None  # номер-заглушка из шаблона сайта
    return "+" + digits


def extract_socials(text: str | None) -> dict[str, str]:
    """Достаём ссылки на telegram/vk/instagram из произвольного текста."""
    if not text:
        return {}
    socials: dict[str, str] = {}
    for network, pattern in _SOCIAL_PATTERNS.items():
        for match in pattern.finditer(text):  # re.finditer, не findall
            handle = match.group("handle")
            # Стоп-список сравниваем по первому сегменту: «share.php» — это
            # тот же служебный «share», но с точкой он проскакивал целиком,
            # и кнопка «поделиться» становилась выдуманным аккаунтом агентства.
            head = handle.split(".")[0].lower() if handle else ""
            if handle and head not in _SOCIAL_STOP_HANDLES:
                socials[network] = match.group(0)
                break  # берём первое валидное совпадение на сеть
    return socials


class Organization(BaseModel):
    """Организация-лид в едином формате для всех источников."""

    name: str
    address: str | None = None
    phone: list[str] = Field(default_factory=list, description="Телефоны, нормализованные в +7XXXXXXXXXX.")
    website: str | None = None
    socials: dict[str, str] = Field(default_factory=dict, description="telegram/vk/instagram → ссылка.")
    rubric: str | None = None
    lat: float | None = None
    lon: float | None = None
    rating: float | None = None
    reviews_count: int | None = None
    hours: str | None = None
    review_complaints: list[str] = Field(
        default_factory=list,
        description="Цитаты из отзывов про недозвон/долгий ответ (заполняет браузерный модуль).",
    )
    source: Source
    source_url: str | None = None  # ссылка на карточку для ручного добора
    inn: str | None = None  # None на старте — заполняется на шаге обогащения (enrich)
    director: str | None = None  # ФИО руководителя — заполняется на шаге обогащения (enrich)
    # "ИП" или "юрлицо" — из DaData (enrich). Для ИП телефон = телефон человека.
    entity_type: str | None = None
    registered_year: int | None = None  # год регистрации из DaData — для оценки платёжеспособности

    @field_validator("phone", mode="before")
    @classmethod
    def _normalize_phones(cls, value: object) -> list[str]:
        """Нормализуем и дедуплицируем список телефонов на входе."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        normalized: list[str] = []
        for item in value:  # type: ignore[assignment]
            phone = normalize_phone(str(item))
            if phone and phone not in normalized:
                normalized.append(phone)
        return normalized

    @property
    def primary_phone(self) -> str | None:
        """Первый телефон — часто используется как ключ дедупликации."""
        return self.phone[0] if self.phone else None


class Email(BaseModel):
    """Email агентства с типом по локальной части (см. EmailKind)."""

    address: str
    kind: EmailKind = "general"

    @field_validator("address", mode="before")
    @classmethod
    def _normalize_address(cls, value: object) -> str:
        return str(value).strip().lower()


class Agency(BaseModel):
    """Агентство-партнёр (режим partners). Ключ дедупликации — нормализованный домен website.

    Ничего не выдумываем: не нашли контакт — оставляем пусто (см. email_found).
    red_flags/niche_tags — подсказки для ручной оценки, а не приговор: помеченное
    агентство остаётся в выгрузке.
    """

    name: str
    website: str | None = None  # нормализованный домен (agency.ru) — он же ключ дедупа
    emails: list[Email] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list, description="Телефоны в формате +7XXXXXXXXXX.")
    telegram: list[str] = Field(default_factory=list, description="Ссылки/хэндлы Telegram.")
    city: str | None = None
    services_raw: str | None = None  # текст блока услуг — для ручной оценки
    niche_tags: list[str] = Field(default_factory=list, description="Ниши агентства (стоматология, авто…).")
    has_partner_page: bool = False
    partner_page_url: str | None = None
    red_flags: list[str] = Field(
        default_factory=list, description="Метки: конкурент/медийный/PR/не наш сегмент."
    )
    source: str | None = None  # откуда взяли (ratingruneta / workspace / search …)
    # Метрики из каталога (место в рейтинге, ценовой сегмент, число кейсов) — сигнал размера
    # агентства для ручной оценки «малое/среднее».
    catalog_info: str | None = None
    # Уровень своей разработки: none | constructors | full | unknown (см. contacts.detect_dev_level).
    # Определяет питч и пригодность агентства под субподряд.
    dev_level: str = "unknown"
    big_agency: bool = False  # крупный сетевой игрок (тендеры, свой продакшн) — не наша цель

    @field_validator("website", mode="before")
    @classmethod
    def _normalize_website(cls, value: object) -> str | None:
        """Website приводим к домену — чтобы дедуп по нему был честным."""
        return normalize_domain(str(value)) if value else None

    @field_validator("phones", mode="before")
    @classmethod
    def _normalize_phones(cls, value: object) -> list[str]:
        """Нормализуем и дедуплицируем телефоны (та же логика, что у Organization)."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        normalized: list[str] = []
        for item in value:  # type: ignore[assignment]
            phone = normalize_phone(str(item))
            if phone and phone not in normalized:
                normalized.append(phone)
        return normalized

    @property
    def email_found(self) -> bool:
        """Нашли ли хоть один email (в выгрузке — колонка email_found)."""
        return bool(self.emails)

    @property
    def best_email(self) -> str | None:
        """«Лучший» email по приоритету partner > personal > sales > general."""
        if not self.emails:
            return None
        best = min(self.emails, key=lambda e: EMAIL_PRIORITY.get(e.kind, 99))
        return best.address

    @property
    def url(self) -> str | None:
        """Полный URL для захода на сайт (website хранится доменом)."""
        return f"https://{self.website}" if self.website else None

    @property
    def recommended_pitch(self) -> str:
        """С чем идти к агентству — по уровню его разработки (правило из брифа продаж).

        none         -> «сайты + CRM» (самая тёплая цель: лиды льют, складывать некуда);
        constructors -> «только CRM» (сайты делают на Tilda/WP, кастомной системы нет);
        full         -> «пропустить» (свой продакшн полного цикла);
        unknown      -> «уточнить вручную» (сайт не прочитался/услуги неясны).
        """
        if self.big_agency:
            return "пропустить: крупный сетевой"
        return {
            "none": "сайты + CRM",
            "constructors": "только CRM",
            "full": "пропустить: своя разработка",
            "unknown": "уточнить вручную",
        }.get(self.dev_level, "уточнить вручную")

    @property
    def is_target(self) -> bool:
        """Подходит ли под субподряд: не крупняк и нет своей разработки полного цикла."""
        return not self.big_agency and self.dev_level != "full"

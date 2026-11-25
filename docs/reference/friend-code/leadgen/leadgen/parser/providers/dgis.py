"""Провайдер 2ГИС через официальный Places API (Catalog API).

Эндпоинт и поля сверены с документацией и живым ответом API:
  GET https://catalog.api.2gis.com/3.0/items
  параметры: q, fields, page, page_size, locale, key

ВАЖНО: поле items.contact_groups (телефоны/сайт) у 2ГИС платное («по запросу за
отдельную плату») и на демо-ключе приходит пустым. Маппинг контактов написан
forward-compatible: заработает сразу, как подключишь платный доступ. Остальные поля
(адрес, координаты, рубрика, рейтинг, часы, ссылка на карточку) доступны на демо.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from ..models import Organization, extract_socials
from .base import Provider

BASE_URL = "https://catalog.api.2gis.com/3.0/items"
FIRM_URL = "https://2gis.ru/firm/{id}"  # публичная карточка для ручного добора

# Запрашиваемые доп. поля (без них 2ГИС отдаёт только id/name/type).
_FIELDS = ",".join(
    [
        "items.point",
        "items.address",
        "items.full_address_name",
        "items.contact_groups",  # платное поле: на демо пусто
        "items.rubrics",
        "items.reviews",
        "items.schedule",
    ]
)

_DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_RU = {"Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт", "Fri": "Пт", "Sat": "Сб", "Sun": "Вс"}

# Типы контактов в contact_groups[].contacts[].type -> куда кладём.
_PHONE_TYPES = {"phone"}
_SITE_TYPES = {"website", "url", "site"}
_SOCIAL_TYPE_MAP = {"vkontakte": "vk", "vk": "vk", "telegram": "telegram", "instagram": "instagram"}


def _is_retryable(exc: BaseException) -> bool:
    """Ретраим только 429/5xx и сетевые сбои — остальное сразу наверх."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


# Сколько запросов к Places API сделано за жизнь процесса. Демо-ключ = 1000 запросов/мес,
# поэтому расход важно видеть: иначе ключ «внезапно» блокируется на 100%.
REQUESTS_MADE = 0
DEMO_MONTHLY_LIMIT = 1000
_DEMO_PAGE_SIZE = 10  # предел page_size на демо-ключе (проверено: 50 -> HTTP 400)

# Ключ заблокирован/лимит исчерпан — узнаём это только по ответу API. Дальше долбить
# бессмысленно: прогон «все ниши × все города» — это сотни запросов, каждый вернёт ту же
# ошибку. Флаг даёт вызывающему (pipeline) сразу уйти на браузерный поиск.
# НО не навсегда: демо-лимит обнуляется раз в месяц, а бот живёт неделями без рестарта.
# Раз в _BLOCK_RECHECK_SECONDS даём API один пробный шанс — если ключ ожил, вернёмся на него.
KEY_BLOCKED = False
_BLOCKED_AT: float | None = None  # time.monotonic() момента блокировки
_BLOCK_RECHECK_SECONDS = 6 * 3600  # перепроверка раз в 6 часов: 4 лишних запроса в сутки


def _mark_blocked() -> None:
    global KEY_BLOCKED, _BLOCKED_AT
    KEY_BLOCKED = True
    _BLOCKED_AT = time.monotonic()


def key_blocked() -> bool:
    """Ключ 2ГИС отклонён API (лимит/бан) — API как источник сейчас мёртв.

    Через _BLOCK_RECHECK_SECONDS возвращает False, разрешая один пробный запрос:
    не ожил — search() снова взведёт флаг, ожил — источник auto вернётся на API.
    """
    global KEY_BLOCKED, _BLOCKED_AT
    if not KEY_BLOCKED:
        return False
    if _BLOCKED_AT is not None and time.monotonic() - _BLOCKED_AT >= _BLOCK_RECHECK_SECONDS:
        logger.info("2ГИС: прошло {} ч с блокировки ключа — даю API пробный шанс.",
                    _BLOCK_RECHECK_SECONDS // 3600)
        KEY_BLOCKED = False
        _BLOCKED_AT = None
    return KEY_BLOCKED


class DgisProvider(Provider):
    name = "2gis"

    def available(self) -> bool:
        return bool(self.settings.dgis_api_key)

    async def search(
        self,
        query: str,
        city: str,
        max_results: int | None = None,
    ) -> list[Organization]:
        global REQUESTS_MADE

        if not self.available():
            logger.warning("2ГИС: не задан DGIS_API_KEY — источник пропущен (пустой результат).")
            return []
        if key_blocked():  # функция, не флаг: раз в 6 часов даёт API пробный шанс
            # Уже получали отказ в этом процессе — не жжём время на заведомо мёртвые запросы.
            logger.debug("2ГИС: ключ заблокирован ранее — запрос {!r} пропущен.", query)
            return []

        max_results = max_results or self.settings.max_results
        # Верхний предел page_size зависит от тарифа: на демо — 10, на платном больше.
        # Начинаем с настройки и при 400 «page_size should be from 1 to 10» падаем на демо-предел.
        page_size = min(self.settings.page_size, 50)
        q = f"{query} {city}".strip()  # город задаём текстом в запросе (простой надёжный скоуп)

        orgs: list[Organization] = []
        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:
            page = 1
            while len(orgs) < max_results:
                params = {
                    "q": q,
                    "fields": _FIELDS,
                    "page": page,
                    "page_size": page_size,
                    "locale": "ru_RU",
                    "key": self.settings.dgis_api_key,
                }
                REQUESTS_MADE += 1
                if REQUESTS_MADE % 50 == 0:
                    logger.info(
                        "2ГИС: израсходовано ~{} запросов за прогон (демо-лимит {} в месяц).",
                        REQUESTS_MADE, DEMO_MONTHLY_LIMIT,
                    )
                try:
                    data = await self._fetch_page(client, params)
                except httpx.HTTPStatusError as exc:
                    logger.warning("2ГИС: HTTP {} на странице {} — останавливаюсь.",
                                   exc.response.status_code, page)
                    break

                result = data.get("result")
                if not result:  # у 2ГИС ошибки/конец демо-лимита приходят без result
                    meta = data.get("meta", {})
                    error = meta.get("error") or {}
                    message = error.get("message")
                    # Ошибка (лимит/бан демо-ключа) — это НЕ «ничего не нашлось». Логируем громко.
                    # Тариф не даёт такой page_size (на демо максимум 10) — снижаем и повторяем,
                    # иначе прогон молча вернёт ноль организаций.
                    if "page_size" in str(message) and page_size > _DEMO_PAGE_SIZE:
                        logger.warning(
                            "2ГИС: page_size={} не поддержан тарифом — снижаю до {} и повторяю.",
                            page_size, _DEMO_PAGE_SIZE,
                        )
                        page_size = _DEMO_PAGE_SIZE
                        continue
                    if error or (meta.get("code") and meta["code"] != 200):
                        blocked = "block" in str(message).lower() or meta.get("code") == 403
                        if blocked:
                            _mark_blocked()
                        logger.warning(
                            "2ГИС: запрос {!r} отклонён (code={}, {}).{}",
                            q, meta.get("code"), message,
                            " КЛЮЧ ЗАБЛОКИРОВАН/ЛИМИТ ИСЧЕРПАН: демо даёт 1000 запросов Places API "
                            "в месяц. Проверь расход в Platform Manager (platform.2gis.ru) — нужен "
                            "новый период или платный ключ." if blocked else
                            " Возможно, исчерпан лимит демо-ключа — результат неотличим от пустого.",
                        )
                    else:
                        logger.info("2ГИС: выдача закончилась (code={}).", meta.get("code"))
                    break

                items = result.get("items") or []
                if not items:
                    break

                for item in items:
                    orgs.append(self._to_org(item, query))
                    if len(orgs) >= max_results:
                        break

                if len(items) < page_size:  # неполная страница — данных больше нет
                    break
                page += 1
                await asyncio.sleep(self.settings.request_delay)  # rate limiting между страницами

        logger.info("2ГИС: собрано {} организаций по запросу {!r}.", len(orgs), q)
        return orgs

    async def _fetch_page(self, client: httpx.AsyncClient, params: dict) -> dict:
        """Один GET к API с ретраями (tenacity, экспоненциальный бэкофф на 429/5xx)."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.settings.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(BASE_URL, params=params)
                resp.raise_for_status()
                return resp.json()
        return {}  # недостижимо (reraise=True), нужно для типизации

    # --- маппинг ответа 2ГИС -> Organization ------------------------------------

    def _to_org(self, item: dict[str, Any], rubric_query: str) -> Organization:
        point = item.get("point") or {}
        reviews = item.get("reviews") or {}
        phones, website, socials = self._parse_contacts(item.get("contact_groups"))
        if website:  # соцсети иногда прячутся в самом сайте/ссылке
            for network, link in extract_socials(website).items():
                socials.setdefault(network, link)

        org_id = item.get("id")
        return Organization(
            name=item.get("name") or item.get("full_name") or "",
            address=item.get("full_address_name") or item.get("address_name"),
            phone=phones,
            website=website,
            socials=socials,
            rubric=self._primary_rubric(item.get("rubrics")) or rubric_query,
            lat=point.get("lat"),
            lon=point.get("lon"),
            rating=reviews.get("general_rating") or reviews.get("org_rating"),
            reviews_count=reviews.get("general_review_count") or reviews.get("org_review_count"),
            hours=self._format_schedule(item.get("schedule")),
            source="2gis",
            source_url=FIRM_URL.format(id=org_id) if org_id else None,
        )

    @staticmethod
    def _parse_contacts(contact_groups: Any) -> tuple[list[str], str | None, dict[str, str]]:
        """Разбираем contact_groups (телефоны/сайт/соцсети). Устойчиво к пустому/None."""
        phones: list[str] = []
        website: str | None = None
        socials: dict[str, str] = {}
        for group in contact_groups or []:
            for contact in group.get("contacts") or []:
                ctype = (contact.get("type") or "").lower()
                value = contact.get("value") or contact.get("url") or contact.get("text")
                if not value:
                    continue
                if ctype in _PHONE_TYPES:
                    phones.append(value)  # нормализация в +7 — в валидаторе Organization
                elif ctype in _SITE_TYPES:
                    website = website or (contact.get("url") or value)
                elif ctype in _SOCIAL_TYPE_MAP:
                    socials.setdefault(_SOCIAL_TYPE_MAP[ctype], contact.get("url") or value)
        return phones, website, socials

    @staticmethod
    def _primary_rubric(rubrics: Any) -> str | None:
        if not rubrics:
            return None
        for rubric in rubrics:
            if rubric.get("kind") == "primary":
                return rubric.get("name")
        return rubrics[0].get("name")

    @staticmethod
    def _format_schedule(schedule: Any) -> str | None:
        """schedule -> строка вида 'Пн 08:00–21:00; Вт 08:00–21:00; ...'."""
        if not isinstance(schedule, dict):
            return None
        if schedule.get("is_24x7") or schedule.get("round_the_clock"):
            return "круглосуточно"
        parts: list[str] = []
        for day in _DAY_ORDER:
            entry = schedule.get(day)
            if not isinstance(entry, dict):
                continue
            spans = ", ".join(
                f"{w.get('from')}–{w.get('to')}"
                for w in (entry.get("working_hours") or [])
                if w.get("from")
            )
            if spans:
                parts.append(f"{_DAY_RU[day]} {spans}")
        return "; ".join(parts) or None

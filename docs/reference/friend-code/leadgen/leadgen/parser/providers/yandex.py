"""Яндекс.Карты как ВТОРОЙ источник отзывов (добор к 2ГИС).

Зачем: многие клиенты пишут отзывы в Яндексе, а не в 2ГИС. Для того же места ищем его
в Яндекс.Картах по названию+городу, открываем отзывы и прогоняем через тот же матчер жалоб —
получаем больше горячих лидов на то же место.

⚠️ ЭКСПЕРИМЕНТАЛЬНО. Яндекс.Карты — тяжёлый SPA с антиботом и обфусцированной вёрсткой;
надёжность сильно зависит от качества прокси (на слабом/флакающем прокси часто ERR_PROXY_*).
Поэтому:
  • по умолчанию ВЫКЛЮЧЕНО (флаг USE_YANDEX_REVIEWS в .env; включать, когда прокси хороший);
  • работает как ДОБОР: любое падение НЕ ломает основной поток (2ГИС) — жалобы просто не добавятся;
  • селекторы (a[href*="/org/"]) могут поменяться на стороне Яндекса — тогда чинить здесь.

Заготовка `YandexProvider` (поиск организаций через официальный API) оставлена как есть —
её импортит pipeline; полноценный API-поиск — отдельная задача (нужен YANDEX_API_KEY).
"""
from __future__ import annotations

from urllib.parse import quote

from loguru import logger

from ..config import Settings
from ..models import Organization
from ..reviews import scan_reviews
from .base import Provider


class YandexProvider(Provider):
    """Заглушка провайдера-источника (Яндекс «Поиск по организациям»). Требует YANDEX_API_KEY."""

    name = "yandex"

    def available(self) -> bool:
        return bool(self.settings.yandex_api_key)

    async def search(self, query: str, city: str, max_results: int | None = None) -> list[Organization]:
        if not self.available():
            logger.warning("Яндекс: не задан YANDEX_API_KEY — источник пропущен.")
            return []
        logger.warning("Яндекс-провайдер (API-поиск) не реализован. Запрос: {!r}/{!r}", query, city)
        return []


def _base_name(name: str) -> str:
    """Название без хвоста-рубрики 2ГИС («ДС Авто, автотехцентр» -> «ДС Авто»)."""
    head, sep, tail = name.rpartition(",")
    return head.strip() if (sep and tail.strip() and tail.strip()[0].islower()) else name.strip()


def _city_of(org: Organization) -> str:
    if not org.address:
        return ""
    return org.address.split("(")[0].split(",")[0].strip()


def _key(text: str) -> str:
    """Название под сравнение: только буквы и цифры, в нижнем регистре.

    Кавычки, дефисы и лишние пробелы у Яндекса и 2ГИС расставлены по-разному
    («ДС-Авто» против «ДС Авто»), а речь об одной и той же фирме.
    """
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _same_place(cards: list[dict], name: str) -> str | None:
    """Ссылка на ТУ ЖЕ организацию, а не на первую в выдаче.

    Яндекс на любой запрос отвечает списком: рядом с нужной фирмой стоят
    соседи по улице и тёзки из другого района. Раньше бралась первая ссылка
    подряд — и чужие жалобы попадали в карточку нашего лида, а оттуда прямо
    в холодное письмо: «вижу, у вас жалуются на…» о том, чего у компании нет.
    Такое письмо хуже, чем отсутствие письма.

    Не узнали организацию — возвращаем None: без жалоб лид останется просто
    холодным, и это честно.
    """
    wanted = _key(name)
    if not wanted or len(wanted) < 3:
        # Слишком короткое имя («АС», «БМ») совпадёт с чем угодно.
        return None
    for card in cards:
        if wanted not in _key(card.get("text", "")):
            continue
        href = (card.get("href") or "").split("?")[0].rstrip("/")
        if href:
            return href
    return None


async def _try_goto(page, url: str, timeout_ms: int) -> bool:
    """Одна попытка перехода (без ретрай-шторма — на флакающем прокси он только вредит)."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return True
    except Exception as exc:  # noqa: BLE001 — не загрузилось — честный False
        logger.info("Яндекс: не загрузилось {} ({})", url[:60], type(exc).__name__)
        return False


async def enrich_reviews_yandex(page, org: Organization, settings: Settings) -> list[str]:
    """Найти место в Яндекс.Картах и вернуть цитаты-жалобы из его СВЕЖИХ отзывов.

    Работает на том же page (последовательно, после 2ГИС-добора). Любая ошибка -> [] (добор
    не обязателен). Возвращает список цитат для слияния в org.review_complaints.
    """
    import asyncio

    from .browser import _recent_reviews_text  # ленивый импорт: избегаем циклической зависимости

    name = _base_name(org.name)
    city = _city_of(org)
    query = f"{name} {city}".strip()
    if not name:
        return []
    timeout_ms = int(settings.browser_timeout * 1000)
    try:
        # 1) поиск места
        if not await _try_goto(page, f"https://yandex.ru/maps/?text={quote(query)}", timeout_ms):
            return []
        await asyncio.sleep(5)
        cards = await page.eval_on_selector_all(
            'a[href*="/org/"]',
            "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))",
        )
        org_url = _same_place(cards, name)
        if not org_url:
            logger.info("Яндекс: в выдаче по «{}» этой же организации не видно", query)
            return []

        # 2) отзывы места
        if not await _try_goto(page, org_url + "/reviews/", timeout_ms):
            return []
        await asyncio.sleep(5)
        for _ in range(8):  # подгружаем ленту отзывов
            await page.mouse.wheel(0, 3000)
            await asyncio.sleep(0.6)
        body = await page.inner_text("body")

        # 3) только свежие отзывы -> матчер жалоб (тот же, что для 2ГИС)
        body = _recent_reviews_text(body, settings.reviews_min_year)
        quotes = scan_reviews([body])
        if quotes:
            logger.info("Яндекс: +{} жалоб для {}", len(quotes), org.name)
        return quotes
    except Exception as exc:  # noqa: BLE001 — добор Яндекса не обязателен — не роняем основной поток
        logger.info("Яндекс-добор {} пропущен: {}", org.name, exc)
        return []

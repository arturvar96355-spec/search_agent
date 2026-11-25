"""Оркестрация: запуск провайдеров по списку запросов, сбор Organization.

Строит провайдеров по выбранному источнику, гоняет по ним пары «рубрика + город»,
схлопывает дубли и отдаёт список организаций. Здесь же живут докачка (состояние
в .jsonl, см. JsonlStore), пауза между запросами и общий потолок объёма: без него
прогон по шести парам забирал у 2ГИС до шестисот карточек ради первой сотни.

Переключение источника на лету — тоже отсюда: ключ 2ГИС может выгореть посреди
прогона, и при source="auto" остаток запросов уходит в браузерный поиск, а не
возвращает молча ноль.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable

from loguru import logger

from .config import Settings, get_settings
from .dedup import deduplicate
from .models import Organization
from .providers.base import Provider
from .providers.browser import BrowserProvider
from .providers.dgis import DgisProvider
from .providers.dgis import key_blocked as dgis_key_blocked
from .providers.yandex import YandexProvider
from .store import JsonlStore

# Реестр провайдеров по имени источника.
PROVIDERS: dict[str, type[Provider]] = {
    "2gis": DgisProvider,
    "yandex": YandexProvider,
    "browser": BrowserProvider,
}


def resolve_source(source: str, settings: Settings) -> str:
    """'auto' -> реальный источник: API 2ГИС, пока ключ жив, иначе браузерный поиск.

    Ключ может быть задан, но исчерпан — это видно только по ответу API, поэтому
    учитываем и флаг блокировки, выставленный провайдером за время процесса.
    """
    if source != "auto":
        return source
    if settings.dgis_api_key and not dgis_key_blocked():
        return "2gis"
    reason = "ключ 2ГИС исчерпан/заблокирован" if settings.dgis_api_key else "DGIS_API_KEY не задан"
    logger.info("Источник auto -> browser ({}): ищу прямо в вебе 2ГИС, ключ не нужен.", reason)
    return "browser"


def build_providers(source: str, settings: Settings) -> list[Provider]:
    """Собираем список провайдеров по значению --source (auto|2gis|yandex|browser|all)."""
    source = resolve_source(source, settings)
    if source == "all":
        keys = ["2gis", "yandex"]
    else:
        keys = [source]
    providers: list[Provider] = []
    for key in keys:
        provider_cls = PROVIDERS.get(key)
        if provider_cls is None:
            raise ValueError(
                f"Неизвестный источник: {key!r}. Доступно: 2gis, yandex, browser, all."
            )
        providers.append(provider_cls(settings))
    return providers


def _refresh(
    source: str,
    active: str,
    providers: list[Provider],
    settings: Settings,
) -> tuple[str, list[Provider]]:
    """Пересобрать провайдеров, если при auto действующий источник сменился."""
    if source != "auto":
        return active, providers
    current = resolve_source(source, settings)
    if current == active:
        return active, providers
    logger.warning("Источник переключён {} -> {}.", active, current)
    return current, build_providers(current, settings)


async def run_query(
    providers: list[Provider],
    rubric: str,
    city: str,
    max_results: int,
) -> list[Organization]:
    """Один запрос (рубрика+город) прогоняем через все выбранные провайдеры."""
    collected: list[Organization] = []
    for provider in providers:
        try:
            found = await provider.search(rubric, city, max_results)
        except NotImplementedError:
            logger.warning("Провайдер {} — заглушка, пропускаю.", provider.name)
            found = []
        collected.extend(found)
    return collected


async def run(
    queries: Iterable[tuple[str, str]],
    source: str = "2gis",
    max_results: int | None = None,
    settings: Settings | None = None,
    state_path: str | None = None,
    resume: bool = True,
    total_limit: int | None = None,
) -> list[Organization]:
    """Главный вход пайплайна: по каждому (рубрика, город) собираем организации.

    state_path — файл .jsonl для промежуточного состояния (resumability). Если задан
    и resume=True, уже собранные запросы пропускаются, ранее собранное подхватывается.

    max_results — потолок НА ОДИН запрос (рубрика+город).
    total_limit — сколько организаций нужно всего. Дойдя до этого числа, обход
    останавливается. Без него прогон по шести парам с потолком 100 забирал у
    2ГИС до шестисот карточек, а вызывающий оставлял себе первую сотню:
    пятьсот запросов демо-ключа (их всего тысяча в месяц) сгорали впустую.
    """
    settings = settings or get_settings()
    max_results = max_results or settings.max_results
    active = resolve_source(source, settings)
    providers = build_providers(active, settings)

    store = JsonlStore(state_path) if state_path else None
    done = store.completed_queries() if (store and resume) else set()
    all_orgs: list[Organization] = store.load_orgs() if (store and resume) else []
    if all_orgs:
        logger.info("Resume: подхватил {} организаций из {}", len(all_orgs), store.path)

    for rubric, city in queries:
        if (source, rubric, city) in done:
            logger.info("Пропускаю уже собранный запрос: рубрика={!r} город={!r}", rubric, city)
            continue
        # Ключ мог выгореть уже посреди прогона (лимит на месяц). При auto пересобираем
        # провайдеров на лету, иначе остаток запросов молча вернёт ноль.
        active, providers = _refresh(source, active, providers, settings)
        logger.info("Запрос: рубрика={!r} город={!r} источник={}", rubric, city, active)
        orgs = await run_query(providers, rubric, city, max_results)
        if not orgs and source == "auto":
            # Ключ мог выгореть ровно на ЭТОМ запросе — тогда переключение «со следующего»
            # опоздало бы: при одном-единственном запросе прогон вернул бы ноль.
            new_active, providers = _refresh(source, active, providers, settings)
            if new_active != active:
                active = new_active
                orgs = await run_query(providers, rubric, city, max_results)
        logger.info("  найдено: {}", len(orgs))
        # Пустой результат НЕ помечаем выполненным: 0 организаций неотличим от сбоя
        # (упавший прокси, выгоревший ключ), а resume пропускал бы такую пару навсегда.
        # Цена — редкий повторный запрос по действительно пустой паре при докачке.
        if store is not None and orgs:
            store.append_query(source, rubric, city, orgs)  # промежуточно сохраняем сразу
        all_orgs.extend(orgs)
        if total_limit is not None and len(deduplicate(all_orgs)) >= total_limit:
            logger.info(
                "Набрали {} организаций при нужных {} — дальше не спрашиваю",
                len(all_orgs), total_limit,
            )
            break
        await asyncio.sleep(settings.request_delay)  # rate limiting между запросами

    before = len(all_orgs)
    all_orgs = deduplicate(all_orgs)
    if before != len(all_orgs):
        logger.info("Дедуп: {} -> {} (схлопнуто {})", before, len(all_orgs), before - len(all_orgs))
    return all_orgs

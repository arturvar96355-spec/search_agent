"""Оркестрация режима partners: каталоги -> домены -> дедуп -> обход сайтов -> XLSX.

Тонкий слой над готовыми частями: источники в providers/catalogs.py, сбор контактов
в providers/site.py, дедуп по домену в dedup.py, выгрузка в export.py.
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from . import export
from .config import Settings, get_settings
from .dedup import deduplicate_agencies
from .models import Agency
from .providers.catalogs import fetch_catalogs
from .providers.site import enrich_agencies

# Готовые срезы каталогов по нишам. Ключ 'all' — прогон по всем срезам.
# Все URL проверены живьём 2026-07-30: robots разрешает, парсер отдаёт агентства (в скобках —
# сколько нашлось). Пагинацию ?page= у ratingruneta не используем — закрыта в их robots.txt.
# Порядок важен: по брифу продаж самая тёплая цель — SMM/таргет-агентства (у них нет разработки).
CATALOG_URLS: dict[str, list[str]] = {
    "smm": [
        "https://ratingruneta.ru/smm/moscow/",  # 51
        "https://ruward.ru/smm/region-moscow/",  # 14
    ],
    "medicine": [
        "https://ruward.ru/digital/industry-medicine-pharmacies/",  # 13
        "https://ruward.ru/context/industry-health-beauty/",  # 2
    ],
    "auto": [
        "https://ratingruneta.ru/seo+context/auto/",  # 60
        "https://ratingruneta.ru/digital-agencies/auto/",  # 39
    ],
    "horeca": [
        "https://ratingruneta.ru/digital-agencies/restaurants/",  # 39
    ],
    "beauty": [
        "https://ratingruneta.ru/digital-agencies/beauty/",  # 39
    ],
    "realty": [
        "https://ratingruneta.ru/seo+context/realty/",  # 60
    ],
}


def catalog_urls_for(niche: str) -> list[str]:
    """Срезы каталогов под нишу. 'all' -> все известные срезы (без дублей)."""
    if niche == "all":
        seen: list[str] = []
        for urls in CATALOG_URLS.values():
            for url in urls:
                if url not in seen:
                    seen.append(url)
        return seen
    return CATALOG_URLS.get(niche, [])


def sort_for_outreach(agencies: list[Agency]) -> list[Agency]:
    """Порядок для аутрича (правила из брифа продаж): сначала те, кому реально есть что предложить.

    1) подходящие под субподряд (не крупняк, нет своей разработки полного цикла);
    2) внутри — сначала «нет разработки» (сайты + CRM), потом «конструкторы» (только CRM);
    3) с найденным email — выше (есть куда писать);
    4) со страницей «Партнёрам» — выше (самый конверсионный вход).
    """
    dev_rank = {"none": 0, "constructors": 1, "unknown": 2, "full": 3}
    return sorted(
        agencies,
        key=lambda a: (
            not a.is_target,  # целевые первыми
            dev_rank.get(a.dev_level, 2),
            not a.email_found,
            not a.has_partner_page,
            a.name.lower(),
        ),
    )


async def collect_agencies(
    urls: list[str],
    settings: Settings | None = None,
    city: str | None = None,
    max_results: int | None = None,
    enrich: bool = True,
    queries: list[str] | None = None,
) -> list[Agency]:
    """Полный проход: листинги (+поиск) -> дедуп по домену -> обход сайтов -> порядок для аутрича."""
    settings = settings or get_settings()
    agencies: list[Agency] = []
    if urls:
        agencies.extend(await fetch_catalogs(urls, settings, city))
    if queries:
        from .providers.search import fetch_searches

        agencies.extend(await fetch_searches(queries, settings, city))
    if not agencies:
        logger.warning("Партнёры: из источников ничего не собрано.")
        return []

    before = len(agencies)
    agencies = deduplicate_agencies(agencies)
    if before != len(agencies):
        logger.info("Партнёры: дедуп по домену {} -> {}", before, len(agencies))

    # Крупный сетевой игрок (тендеры, свой продакшн) нам не подходит — не тратим на него обход.
    big = [a for a in agencies if a.big_agency]
    if big:
        agencies = [a for a in agencies if not a.big_agency]
        logger.info("Партнёры: отложил {} крупных сетевых агентств (не наш размер).", len(big))

    if max_results:
        agencies = agencies[:max_results]

    if enrich:
        await enrich_agencies(agencies, settings)
    return sort_for_outreach(agencies)


def export_agencies(agencies: list[Agency], out: Path, settings: Settings | None = None) -> Path:
    """Выгрузка агентств в XLSX (или CSV, если путь с .csv)."""
    settings = settings or get_settings()
    out = Path(out)
    if out.suffix.lower() == ".csv":
        # Чистка та же, что в XLSX-ветке: CSV открывают тем же Excel.
        df = export._sanitize_for_xlsx(export.agencies_to_dataframe(agencies))
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, encoding="utf-8-sig")
        logger.info("CSV сохранён: {} ({} строк)", out, len(df))
        return out
    return export.agencies_to_xlsx(agencies, out)


def summary(agencies: list[Agency]) -> str:
    """Короткая сводка по прогону — что реально удалось собрать и с чем к кому идти."""
    with_email = sum(1 for a in agencies if a.email_found)
    with_phone = sum(1 for a in agencies if a.phones)
    with_tg = sum(1 for a in agencies if a.telegram)
    with_partner_page = sum(1 for a in agencies if a.has_partner_page)
    flagged = sum(1 for a in agencies if a.red_flags)
    sites_and_crm = sum(1 for a in agencies if a.dev_level == "none")
    crm_only = sum(1 for a in agencies if a.dev_level == "constructors")
    skip = sum(1 for a in agencies if not a.is_target)
    lines = [
        f"агентств: {len(agencies)} | с email: {with_email} | тел: {with_phone} | TG: {with_tg} | "
        f"страница «Партнёрам»: {with_partner_page} | с флагами: {flagged}",
        f"по питчу: «сайты + CRM» — {sites_and_crm} | «только CRM» — {crm_only} | "
        f"пропустить (своя разработка/крупняк) — {skip} | остальным нужна ручная проверка",
    ]
    target = sites_and_crm + crm_only
    if target:
        lines.append("темп аутрича — 10 писем в день: "
                     f"целевых {target} хватит примерно на {max(1, target // 10)} дн.")
    return "\n".join(lines)

"""Точка входа CLI (typer).

Команды (полный список — `parser --help`):
  parser doctor      — диагностика окружения: ключи, зависимости, прокси, источник поиска.
  parser search      — сбор организаций по (рубрика, город) из выбранных источников.
  parser enrich      — обогащение файла лидов через DaData (ИНН, руководитель, тип, год).
  parser partners    — поиск агентств-партнёров: каталоги + поиск -> контакты -> XLSX.
  parser outreach    — email-рассылка по партнёрам (по умолчанию dry-run).
  parser bot         — Telegram-бот (меню, разовый поиск, фоновый сбор, база).
  parser proxy-check — проверка PROXY_URL: подключение, внешний IP, доступность 2ГИС.
  parser niches      — список ниш и городов.
  parser tg-chat-id  — узнать свой chat_id для ALLOWED_CHAT_IDS.
"""
from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import typer
from loguru import logger

from . import __version__, export, pipeline
from .config import get_settings

app = typer.Typer(
    add_completion=False,
    help="CLI для сбора базы организаций (лидов) с карт 2ГИС и Яндекс — для ручного B2B-аутрича.",
)


def _run_tag(source: str, pairs: list[tuple[str, str]]) -> str:
    """Короткая метка запроса для имени файла состояния.

    Докачивать имеет смысл ровно тот же самый прогон: тот же источник и тот же
    список пар «рубрика + город». Порядок пар не важен — сортируем, чтобы одна
    и та же команда, набранная иначе, продолжала свой файл, а не заводила новый.
    """
    import hashlib

    payload = "|".join(sorted(f"{rubric}~{city}" for rubric, city in pairs))
    digest = hashlib.sha1(f"{source}::{payload}".encode()).hexdigest()
    return digest[:8]


def _load_queries(
    query: str | None,
    city: str | None,
    queries_file: Path | None,
) -> list[tuple[str, str]]:
    """Собираем список (рубрика, город): либо из файла, либо из одной пары --query/--city."""
    pairs: list[tuple[str, str]] = []
    if queries_file:
        with open(queries_file, encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if not row or len(row) < 2:
                    continue
                rubric, town = row[0].strip(), row[1].strip()
                if not rubric or rubric.lower() in {"rubric", "рубрика"}:
                    continue  # пропускаем заголовок/пустые строки
                pairs.append((rubric, town))
    elif query and city:
        pairs.append((query, city))
    return pairs


@app.command()
def search(
    query: str | None = typer.Option(None, "--query", "-q", help="Рубрика, например 'стоматология'."),
    city: str | None = typer.Option(None, "--city", "-c", help="Город, например 'Москва'."),
    niche: str | None = typer.Option(
        None, "--niche", "-n",
        help="Ниша целиком (пачка рубрик): auto, clinic, dental… (список: parser niches).",
    ),
    depth: int | None = typer.Option(
        None, "--depth", help="Сколько рубрик ниши взять (напр. --depth 2 → первые 2 из ниши)."
    ),
    problems_only: bool = typer.Option(
        False, "--problems-only",
        help="Оставить только организации с жалобами на связь в отзывах (нужен --browser-enrich).",
    ),
    queries: Path | None = typer.Option(
        None, "--queries", help="CSV-файл с парами 'рубрика,город' (по строке на запрос)."
    ),
    source: str = typer.Option(
        "auto", "--source", "-s",
        help="Источник: auto (API, пока ключ жив, иначе браузер) | 2gis | yandex | all | browser.",
    ),
    max_results: int | None = typer.Option(None, "--max", help="Максимум организаций на запрос."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Файл результата (.csv или .xlsx)."),
    sheet: str | None = typer.Option(
        None, "--sheet", help="Также выгрузить в Google Sheets (URL/ключ/имя таблицы)."
    ),
    telegram: bool = typer.Option(
        False, "--telegram", help="Отправить лиды карточками в Telegram (бот+чат из .env)."
    ),
    tg_chat: str | None = typer.Option(
        None, "--tg-chat", help="chat_id/@канал получателя (по умолчанию из .env)."
    ),
    tg_hot_only: bool = typer.Option(
        False, "--tg-hot-only", help="В Telegram слать только организации с сигналами в отзывах."
    ),
    resume: bool = typer.Option(
        True, "--resume/--no-resume", help="Докачивать из .jsonl и пропускать уже собранные запросы."
    ),
    state: Path | None = typer.Option(
        None, "--state", help="Файл состояния .jsonl (по умолчанию рядом с --out)."
    ),
    browser_enrich: bool = typer.Option(
        False, "--browser-enrich",
        help="После API добрать телефоны и тексты отзывов с публичных карточек (playwright, grey-zone).",
    ),
    with_inn: bool = typer.Option(
        False, "--enrich", help="Дотянуть ИНН и ФИО руководителя через DaData (нужны ключи в .env)."
    ),
) -> None:
    """Сбор организаций по рубрике/нише и городу из выбранного источника."""
    settings = get_settings()
    if niche:
        from . import niches as niches_mod

        found = niches_mod.resolve(niche)
        if found is None:
            raise typer.BadParameter(
                f"Ниша {niche!r} не найдена. Список ниш: parser niches"
            )
        if not city:
            raise typer.BadParameter("Для --niche укажи ещё и --city (город).")
        pairs = found.queries(city, depth=depth)
        typer.echo(f"Ниша «{found.title}»: {len(pairs)} рубрик × город {city}")
    else:
        pairs = _load_queries(query, city, queries)
    if not pairs:
        raise typer.BadParameter("Укажи --query и --city, либо --niche и --city, либо --queries файл.csv")

    out = out or (settings.output_dir / "leads.csv")
    # Имя файла состояния зависит не только от --out, но и от самого запроса.
    # Раньше все прогоны с одинаковым --out (а он по умолчанию один на всех)
    # делили один leads.jsonl, и --resume, включённый по умолчанию, подхватывал
    # оттуда ВСЕ ранее собранные организации: выгрузка по автосервисам Казани
    # приезжала вместе с московскими стоматологиями из прошлого прогона.
    state_path = state or out.with_name(f"{out.stem}.{_run_tag(source, pairs)}.jsonl")

    orgs = asyncio.run(
        pipeline.run(
            pairs,
            source=source,
            max_results=max_results,
            settings=settings,
            state_path=str(state_path),
            resume=resume,
        )
    )
    logger.info("Итого собрано организаций: {}", len(orgs))

    if browser_enrich:
        from .dedup import deduplicate
        from .providers import browser

        typer.echo("Браузерный добор (playwright) — телефоны и тексты отзывов…")
        orgs = asyncio.run(browser.enrich_with_browser(orgs, settings))
        # Телефоны появились только сейчас — повторный дедуп схлопывает филиалы с общим номером.
        before = len(orgs)
        orgs = deduplicate(orgs)
        if before != len(orgs):
            logger.info("Дедуп после добора: {} -> {}", before, len(orgs))

    if with_inn:
        from . import enrich as enrich_module

        try:
            typer.echo("Обогащение через DaData — ИНН и руководитель…")
            orgs = enrich_module.enrich(orgs, settings)
        except Exception as exc:  # noqa: BLE001 — нет ключей/лимит — не роняем весь прогон
            logger.error("Обогащение не выполнено: {}", exc)
            typer.echo(f"Обогащение не выполнено: {exc}")

    if problems_only:
        before = len(orgs)
        orgs = [org for org in orgs if org.review_complaints]
        typer.echo(f"Фильтр --problems-only: {before} → {len(orgs)} (только с жалобами в отзывах)")
        if not browser_enrich:
            typer.echo(
                "⚠️  Жалобы находит только браузерный добор — без --browser-enrich список будет пустым."
            )

    if out.suffix.lower() == ".xlsx":
        export.to_xlsx(orgs, out)
    else:
        export.to_csv(orgs, out)
    typer.echo(f"Готово: {len(orgs)} организаций → {out}")

    if sheet:
        try:
            url = export.to_google_sheets(orgs, sheet, settings)
            typer.echo(f"Google Sheets обновлён: {url}")
        except Exception as exc:  # noqa: BLE001 — не роняем весь прогон из-за проблем с доступом к Sheets
            logger.error("Google Sheets не обновлён: {}", exc)
            typer.echo(f"Google Sheets не обновлён: {exc}")

    if telegram:
        from . import telegram as tg

        try:
            count = asyncio.run(tg.send_orgs(orgs, settings, chat_id=tg_chat, hot_only=tg_hot_only))
            typer.echo(f"Telegram: отправлено {count} сообщений")
        except Exception as exc:  # noqa: BLE001 — телеграм не должен ронять весь прогон
            logger.error("Telegram не отправлен: {}", exc)
            typer.echo(f"Telegram не отправлен: {exc}")


@app.command()
def enrich(
    infile: Path = typer.Argument(..., help="Файл с лидами (.csv/.xlsx) для обогащения ИНН/директором."),
    limit: int | None = typer.Option(
        None, "--limit", help="Обработать не больше N строк (экономия лимита DaData)."
    ),
) -> None:
    """Обогащение лидов через официальный API DaData: ИНН и ФИО директора."""
    from . import enrich as enrich_module

    settings = get_settings()
    try:
        enrich_module.enrich_file(infile, settings=settings, limit=limit)
        typer.echo(f"Готово: обогащено (ИНН/директор) → {infile}")
    except Exception as exc:  # noqa: BLE001 — последний рубеж команды: код выхода 1
        logger.error("enrich: {}", exc)
        raise typer.Exit(code=1) from None


@app.command()
def partners(
    niche: str = typer.Option(
        "all", "--niche", "-n",
        help="Ниша: smm | medicine | auto | horeca | beauty | realty | all; "
             "general — только поиском (каталожного среза нет).",
    ),
    source: str = typer.Option("catalogs", "--source", "-s", help="Источник: catalogs | search | all."),
    max_results: int | None = typer.Option(None, "--max", help="Максимум агентств за прогон."),
    city: str | None = typer.Option("Москва", "--city", "-c", help="Город (метка в выгрузке)."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Файл результата (.xlsx или .csv)."),
    urls: str | None = typer.Option(
        None, "--urls", help="Свои URL листингов через запятую (вместо --niche)."
    ),
    no_enrich: bool = typer.Option(
        False, "--no-enrich", help="Не заходить на сайты (только имена и домены)."
    ),
) -> None:
    """Поиск агентств-партнёров: каталоги -> домены -> контакты с сайта -> XLSX."""
    from . import partners as partners_module
    from .providers.search import search_queries_for

    settings = get_settings()
    if source not in {"catalogs", "search", "all"}:
        raise typer.BadParameter("--source должен быть catalogs | search | all.")

    catalog_urls: list[str] = []
    if source in {"catalogs", "all"}:
        catalog_urls = (
            [u.strip() for u in urls.split(",") if u.strip()] if urls
            else partners_module.catalog_urls_for(niche)
        )
    queries = search_queries_for(niche) if source in {"search", "all"} else []
    # Половина источников может молча отвалиться, если у ниши нет каталогов или запросов.
    # Тихий недобор выглядит как «просто мало нашлось» — говорим об этом вслух.
    if source == "all" and not queries:
        typer.echo(f"⚠️  Для ниши {niche!r} нет поисковых запросов — собираю только по каталогам.")
    if source == "all" and not catalog_urls:
        typer.echo(f"⚠️  Для ниши {niche!r} нет каталогов — собираю только поиском.")

    if not catalog_urls and not queries:
        known = ", ".join(partners_module.CATALOG_URLS) + ", all"
        raise typer.BadParameter(f"Нет источников для ниши {niche!r}. Доступно: {known}. Либо задай --urls.")

    out = out or (settings.output_dir / "agentstva.xlsx")
    typer.echo(f"Источники: каталогов {len(catalog_urls)}, поисковых запросов {len(queries)}. Собираю…")
    agencies = asyncio.run(
        partners_module.collect_agencies(
            catalog_urls,
            settings=settings,
            city=city,
            max_results=max_results,
            enrich=not no_enrich,
            queries=queries,
        )
    )
    if not agencies:
        typer.echo("Ничего не собрано — проверь URL листингов.")
        raise typer.Exit(code=1)

    path = partners_module.export_agencies(agencies, out, settings)
    typer.echo(partners_module.summary(agencies))
    typer.echo(f"Готово: {len(agencies)} агентств → {path}")


@app.command()
def outreach(
    file: Path | None = typer.Option(
        None, "--file", "-f", help="Выгрузка parser partners (.xlsx/.csv). Не нужна при --followup."
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=0,
        help=(
            "Суточная норма писем, а не потолок одного запуска: из неё "
            "вычитается уже отправленное сегодня (по умолчанию "
            "OUTREACH_DAILY_LIMIT=10; 0 = ничего не слать)."
        ),
    ),
    send: bool = typer.Option(
        False, "--send", help="РЕАЛЬНО отправить. Без флага — dry-run: показать письма и ничего не слать."
    ),
    followup: bool = typer.Option(
        False, "--followup", help="Слать догоны тем, кому писали ≥3 рабочих дней назад без ответа."
    ),
) -> None:
    """Email-рассылка по партнёрам: dry-run по умолчанию, отправка только с --send.

    Правила (бриф + EMAIL_OUTREACH.md): 10 писем/день, один домен = одно письмо,
    пауза 60–120с, шаблон по колонке pitch, цен в письмах нет, отписка в каждом.
    """
    from . import outreach as outreach_module

    settings = get_settings()
    if not followup and file is None:
        file = settings.output_dir / "agentstva.xlsx"
        typer.echo(f"--file не задан — беру {file}")

    import smtplib

    try:
        result = outreach_module.run_outreach(
            file, settings, limit=limit, send=send, followup=followup
        )
    # OSError покрывает и smtplib (SMTPException — наследник OSError), и сеть
    # (gaierror/timeout при опечатке в SMTP_HOST) — иначе падали бы голым traceback.
    except smtplib.SMTPAuthenticationError:
        typer.echo("❌ SMTP не принял логин/пароль. Нужен ПАРОЛЬ ПРИЛОЖЕНИЯ, не основной "
                   "пароль аккаунта — как получить, см. EMAIL_OUTREACH.md раздел 3.2.")
        raise typer.Exit(code=1) from None
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        typer.echo(f"❌ {exc}")
        typer.echo("Уже отправленные письма записаны в журнал — при повторном запуске "
                   "дублей не будет.")
        raise typer.Exit(code=1) from None

    if result.dry_run:
        for letter in result.prepared:
            typer.echo("\n" + "=" * 72)
            typer.echo(f"→ {letter.target.email}  [{letter.kind}, питч: {letter.target.pitch}]")
            typer.echo(f"Тема: {letter.subject}\n")
            typer.echo(letter.body)
        typer.echo("\n" + "=" * 72)
        typer.echo(
            f"DRY-RUN: подготовлено {len(result.prepared)} писем, НИЧЕГО не отправлено. "
            f"Повторы пропущены: {result.skipped_already}. Срезано лимитом: {result.limited}."
        )
        typer.echo("Вычитай письма глазами и запусти с --send для реальной отправки.")
    else:
        typer.echo(
            f"Отправлено: {result.sent}. Повторы пропущены: {result.skipped_already}. "
            f"Срезано дневным лимитом: {result.limited}."
        )
        if result.limited:
            typer.echo("Остаток уйдёт завтра — журнал помнит, кому уже писали.")


@app.command()
def doctor() -> None:
    """Проверить окружение: ключи, зависимости, прокси, доступность источников.

    Первая команда для нового разработчика: показывает, что настроено, а что сломано.
    """
    import importlib

    settings = get_settings()
    ok, warn, bad = "✅", "⚠️ ", "❌"
    lines: list[str] = ["", "=== Ключи (.env) ==="]

    def mark(value: object) -> str:
        return ok if value else bad

    lines.append(f"{mark(settings.dgis_api_key)} DGIS_API_KEY — 2ГИС Places API (клиентский режим)")
    lines.append(f"{mark(settings.dadata_api_key and settings.dadata_secret)} "
                 f"DADATA_API_KEY/SECRET — ИНН и руководитель")
    lines.append(f"{mark(settings.telegram_bot_token)} TELEGRAM_BOT_TOKEN — Telegram-бот")
    lines.append(f"{mark(settings.telegram_chat_id)} TELEGRAM_CHAT_ID — получатель лидов")
    proxies = settings.proxy_list()
    lines.append(f"{mark(proxies)} PROXY_URL — прокси для браузера: {len(proxies)} шт."
                 + (" (для VPS нужен пул из 3–5)" if len(proxies) < 2 else ""))
    from .outreach import smtp_ready

    ready, missing = smtp_ready(settings)
    lines.append(
        f"{ok} SMTP — email-рассылка по партнёрам настроена ({settings.smtp_user})" if ready
        else f"{warn}SMTP не настроен ({missing}) — parser outreach работает только в dry-run. "
             f"Как подключить почту: EMAIL_OUTREACH.md"
    )

    lines.append("")
    lines.append("=== Зависимости ===")
    for module, extra, why in (
        ("playwright", "browser", "браузерный добор телефонов и отзывов"),
        ("selectolax", "partners", "разбор HTML каталогов и сайтов агентств"),
        ("dadata", "enrich", "ИНН и руководитель"),
    ):
        try:
            importlib.import_module(module)
            lines.append(f"{ok} {module} — {why}")
        except ImportError:
            lines.append(f"{bad} {module} нет → pip install -e '.[{extra}]' ({why})")

    lines.append("")
    lines.append("=== Источник клиентского поиска ===")
    from .pipeline import resolve_source

    active = resolve_source(settings.search_source, settings)
    lines.append(f"{ok} SEARCH_SOURCE={settings.search_source} → сейчас работает: {active}")
    if active == "browser":
        lines.append("   Ключа 2ГИС нет/исчерпан → ищем прямо в вебе 2ГИС через прокси. Лимитов нет,")
        lines.append("   но медленнее: 40–50с на страницу выдачи (12 организаций), "
                     "и нет телефонов на этапе поиска.")
        lines.append("   Подробности и ограничения — BROWSER_SEARCH.md.")

    lines.append("")
    lines.append("=== Настройки, влияющие на расход лимита ===")
    lines.append(f"{ok} PAGE_SIZE={settings.page_size} — на демо-ключе предел 10 (при 50 API даёт 400); "
                 f"на платном тарифе можно поднять и тратить лимит в разы медленнее")
    lines.append("   Демо-ключ 2ГИС = 1000 запросов Places API в месяц; при page_size=10 и MAX_RESULTS=50")
    lines.append("   один (рубрика, город) стоит до 5 запросов. Расход видно в логах прогона.")
    lines.append(f"   Темп фонового сбора: пачка {settings.collect_per_query}, "
                 f"кулдаун {settings.collect_delay}с "
                 f"({'бережно' if settings.collect_delay >= 10 else 'быстро'})")
    lines.append("   Яндекс как 2-й источник отзывов: "
                 f"{'включён' if settings.use_yandex_reviews else 'выключен'}")

    typer.echo("\n".join(lines))

    # Живые проверки: прокси и 2ГИС (по одному запросу — лимит не жжём).
    async def _live() -> None:
        import httpx

        typer.echo("\n=== Живые проверки ===")
        if proxies:
            from .providers.browser import _proxy_config, httpx_proxy_url

            for index, proxy in enumerate(proxies, 1):
                try:
                    cfg = _proxy_config(proxy)
                    # Сырую строку httpx не принимает (нет схемы / формат host:port:user:pass)
                    # и рабочий прокси помечался бы «не отвечает: ValueError».
                    async with httpx.AsyncClient(proxy=httpx_proxy_url(proxy), timeout=15) as client:
                        ip = (await client.get("https://api.ipify.org?format=json")).json().get("ip")
                    typer.echo(f"{ok} прокси #{index} ({cfg['server']}) работает, IP {ip}")
                except Exception as exc:  # noqa: BLE001 — диагностика: любая ошибка = «не работает»
                    typer.echo(f"{bad} прокси #{index} не отвечает: {type(exc).__name__}")
        else:
            typer.echo(f"{warn} прокси не задан — на VPS 2ГИС забанит датацентр-IP")

        if settings.dgis_api_key:
            from .providers.dgis import DgisProvider

            found = await DgisProvider(settings).search("стоматология", "Москва", 1)
            if found:
                typer.echo(f"{ok} 2ГИС Places API отвечает (пробный запрос вернул данные)")
            else:
                typer.echo(f"{bad} 2ГИС Places API не отдал данные — см. предупреждение выше в логе "
                           f"(вероятно, исчерпан лимит 1000 запросов/мес или ключ заблокирован)")
                # Источник выше считался ДО живой проверки: тогда ключ ещё выглядел рабочим.
                # Пересчитываем — иначе вывод врёт, что поиск пойдёт через API.
                after = resolve_source(settings.search_source, settings)
                if after == "browser":
                    typer.echo(f"{ok} → клиентский поиск уйдёт на браузерный (SEARCH_SOURCE=auto), "
                               f"ключ не нужен. Медленнее: 40–50с на страницу выдачи "
                               f"(12 организаций). См. BROWSER_SEARCH.md")
                else:
                    typer.echo(f"{warn}→ SEARCH_SOURCE={settings.search_source}: "
                               f"поиск вернёт НОЛЬ, пока ключ мёртв. "
                               f"Поставь SEARCH_SOURCE=auto, чтобы автоматически уходить на браузер.")

    asyncio.run(_live())
    typer.echo("\nПодробности и что делать дальше — README.md и HANDOFF.md.")


@app.command("proxy-check")
def proxy_check() -> None:
    """Проверить PROXY_URL: подключается ли, какой внешний IP и не заблокирован ли 2ГИС."""
    import httpx

    settings = get_settings()
    if not settings.proxy_url:
        typer.echo("PROXY_URL не задан в .env — сначала впиши его.")
        raise typer.Exit(code=1)

    async def _check() -> None:
        from .providers.browser import httpx_proxy_url

        # PROXY_URL может быть пулом через запятую — httpx принимает только ОДИН прокси,
        # и только со схемой. Проверяем первый; остальные покажет parser doctor.
        pool = settings.proxy_list()
        if len(pool) > 1:
            typer.echo(f"В PROXY_URL {len(pool)} прокси — проверяю первый. Все сразу: parser doctor")
        proxies = httpx_proxy_url(pool[0])
        # 1) внешний IP через прокси
        async with httpx.AsyncClient(proxy=proxies, timeout=20) as c:
            try:
                r = await c.get("https://api.ipify.org?format=json")
                ip = r.json().get("ip")
                typer.echo(f"✅ Прокси работает. Внешний IP: {ip}")
            except Exception as exc:  # noqa: BLE001 — диагностика: любая ошибка = «не работает»
                typer.echo(f"❌ Прокси НЕ подключается: {exc}")
                raise typer.Exit(code=1) from None
            # 2) геолокация IP (RU?)
            try:
                geo = (await c.get(f"http://ip-api.com/json/{ip}?lang=ru")).json()
                typer.echo(f"   Гео: {geo.get('country')} / {geo.get('city')} · {geo.get('isp')}")
                if geo.get("countryCode") != "RU":
                    typer.echo("   ⚠️ IP НЕ российский — 2ГИС может отдавать другой регион.")
            except Exception:  # noqa: BLE001 — гео-подсказка необязательна
                pass
            # 3) отвечает ли 2ГИС
            try:
                r = await c.get("https://2gis.ru/", timeout=20)
                typer.echo(f"   2ГИС ответил HTTP {r.status_code} "
                           f"({'ок' if r.status_code < 400 else 'подозрительно'}).")
            except Exception as exc:  # noqa: BLE001 — диагностика: любая ошибка = «недоступен»
                typer.echo(f"   ⚠️ 2ГИС недоступен через прокси: {exc}")

    asyncio.run(_check())


@app.command("bot")
def bot_cmd() -> None:
    """Запустить интерактивного Telegram-бота (кнопки: ниша → город → объём → лиды)."""
    from . import bot as bot_module

    settings = get_settings()
    try:
        asyncio.run(bot_module.run_bot(settings))
    except KeyboardInterrupt:
        typer.echo("\nБот остановлен.")


@app.command("niches")
def niches_cmd() -> None:
    """Показать доступные ниши (наборы рубрик) и их коды для --niche."""
    from . import niches as niches_mod

    typer.echo("Доступные ниши (--niche <код>):\n")
    for n in niches_mod.all_niches():
        typer.echo(f"  {n.code:<11} {n.title}")
        typer.echo(f"              рубрики: {', '.join(n.rubrics)}")
    typer.echo("\nПример: parser search --niche clinic --city Москва "
               "--browser-enrich --problems-only --telegram")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"leadgen-parser {__version__}")
        raise typer.Exit()


@app.command("tg-chat-id")
def tg_chat_id() -> None:
    """Показать chat_id недавних чатов бота (сначала напиши боту любое сообщение)."""
    from . import telegram as tg

    settings = get_settings()
    try:
        chats = asyncio.run(tg.recent_chats(settings))
    except Exception as exc:  # noqa: BLE001 — последний рубеж команды: код выхода 1
        logger.error("{}", exc)
        raise typer.Exit(code=1) from None
    if not chats:
        typer.echo("Пусто. Напиши боту любое сообщение и запусти команду снова.")
        return
    typer.echo("chat_id\tназвание/имя:")
    for chat_id, title in chats.items():
        typer.echo(f"{chat_id}\t{title}")


@app.callback()
def _main(
    version: bool | None = typer.Option(
        None,
        "--version",
        help="Показать версию и выйти.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Общие опции CLI."""


if __name__ == "__main__":
    app()

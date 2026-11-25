"""CLI: fbid <команда>.

    fbid scan          — один проход сбора
    fbid watch         — бесконечный цикл (основной режим)
    fbid login fl      — ручной вход в аккаунт, сессия сохраняется в профиль
    fbid status        — что в базе, траты и лимиты за сегодня
    fbid test-telegram — проверить доставку в бота
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import typer
from loguru import logger

from . import limits, notify
from .config import get_settings
from .models import Source
from .pipeline import scan_once
from .store import Store

app = typer.Typer(add_completion=False, help="Парсер заказов с фриланс-бирж с автооткликом.")

# Каждый N-й проход цикла проверяем переписки. При интервале 60 сек это ~5 минут.
REPLY_CHECK_EVERY = 5

LOGIN_URLS = {
    "fl": "https://www.fl.ru/",
    "profi": "https://profi.ru/backoffice/",
    "youdo": "https://youdo.com/",
}


def _setup_logging(verbose: bool = False) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
    )


def _store() -> Store:
    return Store(get_settings().db_path)


@app.command()
def scan(
    notify: bool = typer.Option(
        False, "--notify", help="Присылать карточки новых заказов (обычно не нужно)."
    ),
    no_details: bool = typer.Option(False, "--no-details", help="Не догружать полное ТЗ."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Один проход: прочитать ленты, записать новое, отправить в Telegram."""
    _setup_logging(verbose)
    store = _store()
    try:
        stats = scan_once(store, notify_new=notify, with_details=not no_details)
        logger.info("Проход завершён: {}", stats)
    finally:
        store.close()


@app.command()
def watch(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Основной режим: сбор, автоотклик, досыл текстов и проверка ответов.

    Цикл тикает по быстрому интервалу Profi.ru (PROFI_SCAN_INTERVAL): там заказы
    разбирают за минуты. Лента FL.ru пополняется медленно, её смотрим реже —
    раз в SCAN_INTERVAL.
    """
    _setup_logging(verbose)
    from .browser import single_instance

    settings = get_settings()

    with single_instance("watch") as first:
        if not first:
            logger.error(
                "Основной цикл уже запущен. Второй экземпляр отправлял бы вторые "
                "отклики на те же заказы — выхожу."
            )
            raise typer.Exit(code=1)
        _watch_loop(settings)


def _watch_loop(settings) -> None:
    store = _store()

    tick = max(10.0, settings.profi_scan_interval)
    fl_every = max(1, round(settings.scan_interval / tick))
    logger.info(
        "Profi.ru каждые ~{:.0f} сек, FL.ru каждые ~{:.0f} сек. Автоотклик: {}",
        tick,
        tick * fl_every,
        "ВЫКЛ (DRY_RUN)" if settings.dry_run else "ВКЛ",
    )

    cycle = 0
    try:
        while True:
            cycle += 1
            scope = "all" if cycle % fl_every == 1 or fl_every == 1 else "profi"
            try:
                # Уведомления о новых заказах по умолчанию выключены: в чат
                # уходит результат разбора (отклик, ответ клиента), а не сырая лента.
                stats = scan_once(store, sources=scope, notify_new=settings.notify_new_orders)
                logger.info("Проход ({}): {}", scope, stats)
            except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
                logger.exception("Проход упал: {}", exc)

            # Автоотклик по свежим заказам.
            try:
                from .pipeline import autobid_all

                sent = autobid_all(store)
                if sent:
                    logger.info("Отправлено откликов: {}", sent)
            except Exception as exc:  # noqa: BLE001 — упавший отклик не гасит цикл
                logger.exception("Автоотклик упал: {}", exc)

            # Уборка за прежней двухфазной отправкой: снимаем строки, которые
            # старая версия пометила к досылу. Ничего никому не отправляется.
            try:
                from .pipeline import flush_pending_all

                flush_pending_all(store)
            except Exception as exc:  # noqa: BLE001 — уборка не гасит цикл
                logger.exception("Уборка прежней очереди упала: {}", exc)

            # Ответы клиентов проверяем реже: открывать браузер по каждому
            # заказу дорого, а клиент отвечает не за секунды.
            if cycle % REPLY_CHECK_EVERY == 0:
                try:
                    from .pipeline import check_replies_all

                    check_replies_all(store)
                except Exception as exc:  # noqa: BLE001 — проверка ответов не гасит цикл
                    logger.exception("Проверка ответов упала: {}", exc)

            time.sleep(tick + random.uniform(0, settings.scan_jitter))
    except KeyboardInterrupt:
        logger.info("Остановлено вручную")
    finally:
        store.close()


@app.command()
def login(source: str = typer.Argument(..., help="fl, profi или youdo")) -> None:
    """Ручной вход в аккаунт биржи. Сессия сохранится и будет переиспользоваться."""
    _setup_logging()
    key = source.lower()
    if key not in LOGIN_URLS:
        raise typer.BadParameter(
            f"Неизвестная площадка: {source}. Доступны: " + ", ".join(LOGIN_URLS)
        )
    from .browser import BrowserUnavailable, interactive_login

    try:
        interactive_login(key, LOGIN_URLS[key])
    except BrowserUnavailable as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc


@app.command()
def probe(source: str = typer.Argument(..., help="пока только profi")) -> None:
    """Снимает слепок твоего личного кабинета (HTML + скриншоты) для настройки парсера.

    Только чтение: ничего не кликает, не отправляет и никуда не выгружает.
    Файлы кладутся в data/probe/.
    """
    _setup_logging()
    key = source.lower()
    if key != "profi":
        raise typer.BadParameter("probe пока нужен только для profi (FL.ru идёт по RSS)")

    from .browser import BrowserUnavailable, open_context
    from .sources.profi import ProfiSource
    from .sources.profi import probe as probe_profi

    out_dir = get_settings().db_path.parent / "probe"
    try:
        with open_context(key, headless=False) as context:
            saved = probe_profi(context, out_dir)
    except BrowserUnavailable as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    if not saved:
        logger.error(
            "Ни одна страница не открылась — похоже, сессия слетела. "
            "Запусти: fbid login profi"
        )
        raise typer.Exit(code=1)

    first = Path(saved[0]).read_text(encoding="utf-8")
    if not ProfiSource.is_logged_in(first):
        logger.warning("В слепке видна гостевая страница — сначала войди: fbid login profi")
    logger.info("Готово. Слепок: {}", out_dir)


@app.command("filter-check")
def filter_check(
    limit: int = typer.Option(200, help="сколько последних заказов из базы прогнать"),
    show: str = typer.Option("passed", help="passed | rejected | all"),
) -> None:
    """Прогоняет filters.yaml по уже собранным заказам. Ничего не отправляет.

    Главный инструмент настройки: правишь filters.yaml — сразу видишь, что пройдёт.
    """
    _setup_logging()
    from .filters import FilterSet

    try:
        filters = FilterSet.load()
    except ValueError as exc:
        typer.echo(f"❌ {exc}")
        raise typer.Exit(code=1) from exc

    store = _store()
    try:
        orders = store.iter_orders(limit=limit)
    finally:
        store.close()

    if not orders:
        typer.echo("В базе пусто — сначала: fbid scan")
        return

    passed, rejected = [], []
    for order in orders:
        decision = filters.decide(order)
        (passed if decision.passed else rejected).append((order, decision))

    if show in ("passed", "all"):
        typer.echo(f"\n✅ ПРОШЛИ ({len(passed)}):")
        for order, decision in passed:
            budget = f"{order.budget_value:.0f} ₽" if order.budget_value else "договорная"
            typer.echo(f"  [{order.source.value}] {order.title[:55]}")
            typer.echo(f"       {order.category[:45]} · {budget} · {decision.reason}")

    if show in ("rejected", "all"):
        typer.echo(f"\n❌ ОТСЕЯНЫ ({len(rejected)}):")
        for order, decision in rejected:
            typer.echo(f"  [{order.source.value}] {order.title[:55]} — {decision.reason}")

    total = len(orders)
    share = len(passed) / total * 100 if total else 0
    typer.echo(f"\nИтого: {len(passed)} из {total} ({share:.0f}%) прошли фильтры.")

    from collections import Counter

    reasons = Counter(d.reason.split("«")[0].strip() for _, d in rejected)
    if reasons:
        typer.echo("Причины отсева:")
        for reason, count in reasons.most_common(8):
            typer.echo(f"  {count:3}  {reason}")


@app.command()
def bot(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Телеграм-бот с кнопками: отчёт, статус, стоп-кран.

    Запускается отдельно от `watch` — управление должно работать, даже если
    основной цикл упал или перезапускается.
    """
    _setup_logging(verbose)
    from .tgbot import run

    run()


@app.command()
def stats(
    days: int = typer.Option(7, help="за сколько суток считать"),
    to_telegram: bool = typer.Option(False, "--telegram", help="прислать отчёт в бота"),
) -> None:
    """Воронка и деньги: сколько просмотрено, отсеяно, отправлено и во что обошлось."""
    _setup_logging()
    from .stats import collect, render

    store = _store()
    try:
        report = render(collect(store, days=days), days)
    finally:
        store.close()

    typer.echo("\n" + report + "\n")
    if to_telegram:
        notify.send(report.replace("📊", "📊 <b>Сводка</b>\n"))


@app.command()
def doctor() -> None:
    """Показывает эффективные настройки и состояние: что реально применяется.

    Настройки собираются из .env, переменных окружения и умолчаний — понять,
    что именно сейчас действует, иначе негде.
    """
    _setup_logging()
    from .filters import FilterSet
    from .llm import build_system_prompt
    from .proxy import parse_proxy

    s = get_settings()
    ok, warn = "✅", "⚠️ "

    typer.echo("\n=== ДОСТУПЫ ===")
    typer.echo(f"  {ok if s.telegram_bot_token else warn} Telegram-бот")
    typer.echo(f"  {ok if s.anthropic_api_key else warn} Ключ Anthropic (без него откликов нет)")
    try:
        proxy = parse_proxy(s.proxy_url)
        typer.echo(f"  {ok if proxy else warn} Прокси: {proxy.safe if proxy else 'не задан'}")
    except ValueError as exc:
        typer.echo(f"  {warn} Прокси: {exc}")

    typer.echo("\n=== ПЛОЩАДКИ ===")
    for title, enabled in (
        ("FL.ru", s.fl_enabled),
        ("Profi.ru", s.profi_enabled),
        ("Юду (сбор без отклика)", s.youdo_enabled),
    ):
        typer.echo(f"  {title}: {'вкл' if enabled else 'выкл'}")

    typer.echo("\n=== ЛИМИТЫ (правятся кнопками в боте) ===")
    store = _store()
    try:
        for key, (value, is_override) in limits.snapshot(store).items():
            spec = limits.SPECS[key]
            mark = " ← изменён из бота" if is_override else ""
            typer.echo(f"  {spec.title}: {spec.format(value)}{mark}")

        typer.echo("\n=== РЕЖИМ ===")
        typer.echo(f"  Автоотклик: {'ВЫКЛ (DRY_RUN=true)' if s.dry_run else 'ВКЛЮЧЁН'}")
        from .bidder import Bidder

        typer.echo(f"  Стоп-кран: {'включён' if Bidder(store).is_paused() else 'снят'}")
        typer.echo(f"  Заказов в базе: {store.orders_count()}")
        for src in Source:
            bids, amount = store.spend_today(src)
            typer.echo(f"    {src.value}: сегодня откликов {bids}, потрачено {amount:.0f} ₽")
    finally:
        store.close()

    typer.echo("\n=== ПРАВИЛА ===")
    prompt = build_system_prompt()
    typer.echo(f"  {ok if prompt else warn} База знаний: {len(prompt) if prompt else 0} символов")
    try:
        f = FilterSet.load()
        typer.echo(
            f"  {ok} Фильтры: минимум {f.common.min_budget or 'нет'} ₽, "
            f"свежесть {f.common.max_age_hours} ч, "
            f"стоп-слов {len(f.common.stop_words)}, ключевых {len(f.common.keywords_any)}"
        )
    except ValueError as exc:
        typer.echo(f"  {warn} Фильтры: {exc}")
    typer.echo("")


@app.command()
def autobid(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Один проход автоотклика по уже собранным заказам.

    Соблюдает все предохранители: DRY_RUN, стоп-кран, лимиты и потолки трат.
    """
    _setup_logging(verbose)
    from .pipeline import autobid_all

    settings = get_settings()
    if settings.dry_run:
        typer.echo("DRY_RUN=true — отклики не отправляются, только проверка.")
    store = _store()
    try:
        typer.echo(f"Отправлено откликов: {autobid_all(store)}")
    finally:
        store.close()


@app.command("check-replies")
def check_replies_cmd() -> None:
    """Проверяет, не написал ли клиент по заказам с нашими откликами."""
    _setup_logging()
    from .pipeline import check_replies_all

    store = _store()
    try:
        found = check_replies_all(store).new_replies
        typer.echo(f"Новых ответов: {found}")
    finally:
        store.close()


@app.command("bid-preview")
def bid_preview(
    url: str = typer.Argument(..., help="ссылка на заказ FL.ru или Profi.ru"),
) -> None:
    """Показывает, какой заход сгенерируется на этот заказ. НИЧЕГО не отправляет.

    Главный инструмент настройки регламента: правишь knowledge/*.md — сразу видишь,
    как изменился текст.
    """
    _setup_logging()
    from .llm import generate_draft
    from .sources.fl import FlSource

    store = _store()
    try:
        found = _lookup_order(store, url)
        order = found[0] if found else None
    finally:
        store.close()

    if order is None:
        if "fl.ru/projects/" not in url:
            typer.echo("Заказа нет в базе. Для Profi.ru сначала собери доску: fbid scan")
            raise typer.Exit(code=1)
        import re as _re

        from .models import Order, Source

        match = _re.search(r"/projects/(\d+)/", url)
        if not match:
            raise typer.BadParameter("не разобрал ссылку на заказ FL.ru")
        order = FlSource().fetch_details(
            Order(source=Source.FL, ext_id=match.group(1), url=url, title="(заголовок с сайта)")
        )

    typer.echo(f"\nЗаказ: {order.title}")
    typer.echo(f"Категория: {order.category or '—'}")
    typer.echo(f"Бюджет: {order.budget_value or 'договорная'}\n")

    draft = generate_draft(order)
    if draft is None:
        typer.echo("❌ Заход не сгенерировался — проверь ANTHROPIC_API_KEY и knowledge/")
        raise typer.Exit(code=1)

    typer.echo(f"Вердикт: {draft.verdict}" + (f" ({draft.reason})" if draft.reason else ""))
    typer.echo(f"Метка: {draft.label}")
    typer.echo(f"Оценка (внутреннее): {draft.estimate or '—'}")
    typer.echo("\n--- ЗАХОД КЛИЕНТУ ---")
    typer.echo(draft.reply or "(пусто)")



def _lookup_order(store: Store, url: str):
    """Находит заказ по ссылке. Возвращает (заказ, pk) или None."""
    from .models import order_ref

    ref = order_ref(url)
    if ref is None:
        return None
    return store.find_order(*ref)


@app.command("bid")
def bid_one(
    url: str = typer.Argument(..., help="ссылка на заказ"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="действительно отправить и списать деньги"
    ),
) -> None:
    """Отклик на один заказ, выбранный руками.

    Порог рейтинга здесь не действует: заказ выбрал человек, и «рейтинг ниже 7»
    больше ничего не решает. Всё остальное действует полностью — фильтры,
    суточные лимиты, потолок цены, баланс и перечитывание цены на кнопке.

    Без --yes ничего не отправляется: команда только показывает решение и текст.
    """
    _setup_logging()
    from .bidder import Bidder
    from .browser import BrowserUnavailable, open_context
    from .models import BidStatus
    from .pipeline import bidding_sources, load_details

    store = _store()
    try:
        found = _lookup_order(store, url)
        if found is None:
            typer.echo("Заказа нет в базе. Сначала собери ленту: fbid scan")
            raise typer.Exit(code=1)
        order, order_pk = found

        adapter = next(
            (a for a, src in bidding_sources() if src is order.source), None
        )
        if adapter is None:
            typer.echo(f"Площадка {order.source.value} выключена или не умеет отклики.")
            raise typer.Exit(code=1)

        if yes:
            # Ручная команда с явным согласием: DRY_RUN снимается только в этом
            # процессе и только для одного заказа. Файл .env не трогаем — общий
            # автоотклик остаётся выключенным.
            get_settings().dry_run = False
            typer.echo("⚠️  DRY_RUN снят для этой одной отправки.")
        else:
            # А без согласия — ставим его, а не «оставляем как есть». Строка
            # документации обещает «без --yes ничего не отправляется», но флаг
            # умел только снимать защиту. В бою DRY_RUN выключен, и команда
            # `fbid bid <ссылка>` без --yes отправляла отклик и тратила деньги
            # по-настоящему — а в конце сообщала «чтобы отправить по-настоящему,
            # добавь --yes». То есть выдавала списание за репетицию.
            get_settings().dry_run = True

        bidder = Bidder(store)

        try:
            with open_context(adapter.name) as context:
                balance = None
                if order.source is Source.PROFI:
                    balance = adapter.read_balance(context)
                    typer.echo(f"Баланс: {balance if balance is not None else 'не прочитался'} ₽")

                # Цену и состояние заказа перечитываем со страницы: в базе они
                # могли устареть, а платим по тому, что на странице сейчас.
                order = load_details(adapter, order, context)
                store.upsert_order(order)
                typer.echo(f"Заказ: {order.title}")
                typer.echo(f"Бюджет: {order.budget_value or 'по договорённости'}")
                typer.echo(f"Отклик стоит: {order.bid_price or 'не прочиталось'} ₽")

                outcome = bidder.decide(order, order_pk, balance=balance, min_score=0)
                if outcome.score is not None:
                    typer.echo(f"Рейтинг: {outcome.score.explain()}")

                if outcome.status is BidStatus.PLANNED:
                    outcome = bidder.execute(adapter, context, order, outcome, order_pk)
                bidder.record(order, order_pk, outcome)
                bidder.announce(order, outcome)
        except BrowserUnavailable as exc:
            typer.echo(f"❌ {exc}")
            raise typer.Exit(code=1) from exc

        typer.echo(f"\nРешение: {outcome.status.value} — {outcome.reason}")
        if outcome.text:
            typer.echo("\n--- ТЕКСТ ОТКЛИКА ---")
            typer.echo(outcome.text)
        if outcome.status is BidStatus.SENT:
            typer.echo(f"\n✅ Отправлено. Списано {outcome.cost:.0f} ₽")
        elif not yes:
            typer.echo("\nЧтобы отправить по-настоящему, добавь --yes")
    finally:
        store.close()


@app.command("go-live")
def go_live(
    yes: bool = typer.Option(False, "--yes", "-y", help="подтвердить: софт начнёт тратить деньги"),
) -> None:
    """Снимает режим проверки: софт начинает отправлять отклики по-настоящему.

    Делает две вещи, и вторая не менее важная первой. Пока стоял DRY_RUN, каждый
    подходящий заказ помечался как «отработан в режиме проверки», и по-настоящему
    отклик на него больше бы не ушёл. Такие отметки снимаются, а готовые тексты
    остаются в кэше — заново у модели они не заказываются.
    """
    _setup_logging()
    from .models import BidStatus

    settings = get_settings()
    store = _store()
    try:
        blocked = store.conn.execute(
            "SELECT COUNT(*) AS n FROM bids WHERE status = ?", (BidStatus.DRY_RUN.value,)
        ).fetchone()["n"]

        typer.echo("\n=== ЧТО ВКЛЮЧАЕТСЯ ===")
        for title, on in (
            ("FL.ru", settings.fl_enabled),
            ("Profi.ru", settings.profi_enabled),
            ("Юду", settings.youdo_enabled),
        ):
            typer.echo(f"  {title}: {'да' if on else 'нет'}")
        typer.echo("\n=== ПОТОЛКИ ТРАТ ===")
        for key in ("profi_daily_budget", "profi_max_bid_price", "profi_max_bid_pct",
                    "fl_daily_bids", "profi_daily_bids", "hot_score_min"):
            spec = limits.SPECS[key]
            typer.echo(f"  {spec.title}: {spec.format(limits.get(key, store))}")
        typer.echo(f"\nЗаказов ждут своей очереди после режима проверки: {blocked}")

        if not yes:
            typer.echo("\nЭто включает настоящие траты. Повтори с --yes, если согласен.")
            return

        if not settings.anthropic_api_key:
            typer.echo("❌ Нет ANTHROPIC_API_KEY — тексты откликов генерировать нечем.")
            raise typer.Exit(code=1)

        # Строку НЕ удаляем, а переводим в «отложен». Удаление уносило вместе
        # со строкой и колонку draft_json — тот самый кэш готовых заходов,
        # про который соседняя строка документации обещает «заново у модели не
        # заказываются». Каждый освобождённый заказ шёл к модели по второму
        # разу, и включение боевого режима стоило ровно столько, сколько
        # текстов накопил режим проверки.
        #
        # SKIPPED повторную попытку не блокирует (см. has_bid), так что заказ
        # освобождается ровно так же, как при удалении.
        released = store.conn.execute(
            "UPDATE bids SET status = ?, reason = ? WHERE status = ?",
            (BidStatus.SKIPPED.value, "освобождён при выходе из режима проверки",
             BidStatus.DRY_RUN.value),
        ).rowcount
        store.conn.commit()

        changed = _set_env_value("DRY_RUN", "false")
        get_settings.cache_clear()

        typer.echo(f"\n✅ Режим проверки снят{' (.env обновлён)' if changed else ''}.")
        typer.echo(f"   Разблокировано заказов: {released}")
        typer.echo("   Перезапусти основной цикл, чтобы он подхватил настройку: fbid watch")
        notify.send_alert(
            "Режим проверки снят — софт начал отправлять отклики по-настоящему. "
            "Остановить в любой момент: кнопка «Остановить отклики» в боте."
        )
    finally:
        store.close()


def _set_env_value(key: str, value: str) -> bool:
    """Правит одну строку в .env. Возвращает True, если файл изменился.

    Ровно одна строка: .env — это боевые доступы владельца, переписывать его
    целиком ради одного флага нельзя.
    """
    from .config import PROJECT_ROOT

    path = PROJECT_ROOT / ".env"
    if not path.exists():
        logger.warning("Файла .env нет — правьте настройку через окружение")
        return False

    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            comment = line.partition("#")[2]
            lines[index] = f"{key}={value}" + (f"  #{comment}" if comment else "")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


@app.command()
def pause() -> None:
    """Стоп-кран: автоотклик останавливается, сбор и уведомления продолжаются."""
    _setup_logging()
    from .bidder import Bidder

    store = _store()
    try:
        Bidder(store).pause()
        typer.echo("⏸  Автоотклик остановлен. Снять: fbid resume")
        notify.send_alert("Автоотклик остановлен вручную.")
    finally:
        store.close()


@app.command()
def resume() -> None:
    """Снимает стоп-кран."""
    _setup_logging()
    from .bidder import Bidder

    store = _store()
    try:
        Bidder(store).resume()
        typer.echo("▶️  Автоотклик снова разрешён (если DRY_RUN=false).")
    finally:
        store.close()


@app.command("check-proxy")
def check_proxy() -> None:
    """Проверяет прокси из .env: есть ли через него интернет и меняется ли внешний IP."""
    _setup_logging()
    from .proxy import check, direct_ip, parse_proxy

    settings = get_settings()
    try:
        proxy = parse_proxy(settings.proxy_url)
    except ValueError as exc:
        typer.echo(f"❌ {exc}")
        raise typer.Exit(code=1) from exc

    if not proxy:
        typer.echo("PROXY_URL в .env не задан — работаем с прямого IP.")
        typer.echo(f"Текущий внешний IP: {direct_ip() or 'не определился'}")
        return

    typer.echo(f"Прокси: {proxy.safe}")
    own = direct_ip()
    through = check(proxy)
    if not through:
        typer.echo("❌ Через прокси интернета нет — проверь доступы и срок действия.")
        raise typer.Exit(code=1)

    typer.echo(f"IP без прокси:  {own or 'не определился'}")
    typer.echo(f"IP через прокси: {through}")
    if own and own == through:
        typer.echo("⚠️  IP одинаковый — прокси не применяется, трафик идёт напрямую.")
        raise typer.Exit(code=1)
    typer.echo("✅ Прокси работает.")


@app.command("check-session")
def check_session(source: str = typer.Argument(..., help="fl, profi или youdo")) -> None:
    """Проверяет, жива ли сохранённая сессия биржи."""
    _setup_logging()
    key = source.lower()
    if key not in LOGIN_URLS:
        raise typer.BadParameter(
            f"Неизвестная площадка: {source}. Доступны: " + ", ".join(LOGIN_URLS)
        )

    from .browser import BrowserUnavailable, open_context
    from .sources.fl import FlSource
    from .sources.profi import BACKOFFICE_URL, ProfiSource
    from .sources.youdo import FEED_URL as YOUDO_FEED_URL

    checks = {
        "fl": (LOGIN_URLS["fl"], FlSource.is_logged_in),
        "profi": (BACKOFFICE_URL, ProfiSource.is_logged_in),
        # На Юду отклик ещё не настроен, поэтому «жива ли сессия» здесь значит
        # только «пускает ли нас площадка на доску», а не «мы авторизованы».
        "youdo": (YOUDO_FEED_URL, lambda html: "youdo" in html.lower() and "403" not in html),
    }
    url, is_logged_in = checks[key]
    try:
        with open_context(key) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            alive = is_logged_in(page.content())
            final_url = page.url
    except BrowserUnavailable as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    if alive:
        typer.echo(f"✅ {key}: сессия жива ({final_url})")
    else:
        typer.echo(f"❌ {key}: сессия слетела. Войди заново: fbid login {key}")
        notify.send_alert(f"Слетела сессия {key}. Нужен повторный вход: fbid login {key}")
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Сколько заказов в базе, сколько потрачено сегодня, где лимиты."""
    _setup_logging()
    settings = get_settings()
    store = _store()
    try:
        typer.echo(f"База: {settings.db_path}")
        typer.echo(f"Всего заказов: {store.orders_count()}")
        for src, limit_key, budget_key in (
            (Source.FL, "fl_daily_bids", None),
            (Source.PROFI, "profi_daily_bids", "profi_daily_budget"),
            (Source.YOUDO, None, None),
        ):
            bids, amount = store.spend_today(src)
            line = f"  {src.value}: заказов {store.orders_count(src)}, откликов сегодня {bids}"
            if limit_key:
                line += f"/{limits.get(limit_key, store):.0f}"
            if budget_key:
                line += f", потрачено {amount:.0f}/{limits.get(budget_key, store):.0f} ₽"
            typer.echo(line)
        typer.echo(f"Автоотклик: {'ВЫКЛ (dry_run)' if settings.dry_run else 'ВКЛ'}")
        typer.echo(
            f"Потолок цены отклика на Profi: {limits.get('profi_max_bid_price', store):.0f} ₽"
        )
        typer.echo(f"Мгновенный отклик от: {limits.get('hot_score_min', store):.1f} из 10")

        recent = store.recent_orders(5)
        if recent:
            typer.echo("\nПоследние заказы:")
            for row in recent:
                budget = f"{row['budget_value']:.0f} ₽" if row["budget_value"] else "договорная"
                typer.echo(f"  [{row['source']}] {row['title'][:60]} — {budget}")
    finally:
        store.close()


@app.command("test-telegram")
def test_telegram() -> None:
    """Проверка, что бот настроен и пишет в нужный чат."""
    _setup_logging()
    ok = notify.send("✅ <b>freelance-bid</b> на связи. Доставка работает.")
    if ok:
        typer.echo("Отправлено.")
    else:
        typer.echo("Не отправлено — проверь TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

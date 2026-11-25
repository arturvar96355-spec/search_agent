"""Один проход сбора: лента → дедуп → полное ТЗ → Telegram.

Фильтры и автоотклик встраиваются сюда же на шагах 3-4 — между дедупом и уведомлением.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from . import notify
from .config import get_settings
from .filters import FilterSet
from .models import Order, Source
from .scoring import score_order
from .sources.fl import PACKAGE_EMPTY, FlSource
from .store import MSK, Store, as_utc


@dataclass
class ScanStats:
    fetched: int = 0
    new: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        line = f"получено {self.fetched}, новых {self.new}, отправлено {self.notified}"
        if self.errors:
            line += f", ошибок {len(self.errors)}"
        return line


def _worth_notifying(filters, order: Order) -> bool:
    """Стоит ли вообще показывать заказ владельцу.

    Раньше в Telegram уходил КАЖДЫЙ новый заказ, включая вакансии и заказы
    за 500 ₽ — владелец справедливо назвал это мусором. Теперь уведомление
    уходит только если заказ прошёл фильтры, то есть по нему реально
    возможен отклик.
    """
    decision = filters.decide(order)
    if not decision.passed:
        logger.debug("[{}] не уведомляю: {}", order.key, decision.reason)
    return decision.passed


def enabled_sources() -> list[object]:
    """Площадки, которые читаются без браузера. Profi.ru и Юду идут отдельно."""
    settings = get_settings()
    sources: list[object] = []
    if settings.fl_enabled:
        sources.append(FlSource())
    return sources


def browser_sources() -> list[object]:
    """Площадки, доску которых видно только через браузер.

    Profi.ru — потому что доска под сессией. Юду — потому что перед сайтом
    стоит защита, которая отдаёт обычному HTTP-клиенту 403.
    """
    settings = get_settings()
    sources: list[object] = []
    if settings.profi_enabled:
        from .sources.profi import ProfiSource

        sources.append(ProfiSource())
    if settings.youdo_enabled:
        from .sources.youdo import YoudoSource

        sources.append(YoudoSource())
    return sources


def bidding_sources() -> list[tuple[object, Source]]:
    """Площадки, куда мы реально умеем отправлять отклик.

    Одно место на весь модуль: раньше этот список был скопирован в трёх
    функциях, и включённая площадка легко забывалась в одной из них.
    Адаптер с can_bid = False сюда не попадает — так неоткалиброванная форма
    отклика физически не может потратить деньги.
    """
    settings = get_settings()
    plan: list[tuple[object, Source]] = []
    if settings.fl_enabled:
        plan.append((FlSource(), Source.FL))
    for adapter in browser_sources():
        if getattr(adapter, "can_bid", True):
            plan.append((adapter, adapter.source_enum))
    return plan


def scan_once(
    store: Store,
    *,
    notify_new: bool = True,
    with_details: bool = True,
    sources: str = "all",
) -> ScanStats:
    """Проход сбора. sources: all | fl | profi | youdo.

    Раздельный запуск нужен циклу: заказы на Profi.ru разбирают за минуты,
    поэтому доска смотрится чаще, чем лента FL.ru.
    """
    stats = ScanStats()

    if sources in ("all", "fl"):
        stats = _scan_http_sources(store, notify_new=notify_new, with_details=with_details)

    for source in browser_sources():
        if sources not in ("all", source.name):
            continue
        part = scan_browser_source(
            store, source, notify_new=notify_new, with_details=with_details
        )
        stats.fetched += part.fetched
        stats.new += part.new
        stats.notified += part.notified
        stats.errors.extend(part.errors)
    return stats


# Как часто перечитывать остаток на счёте площадки во время обычного прохода.
# Реже — цифра на экране стареет и вводит в заблуждение; чаще — лишняя загрузка
# страницы каждую минуту, а это и время прохода, и повод для антибота.
BALANCE_EVERY_MINUTES = 60


def _refresh_balance(store: Store, source, context) -> None:
    """Перечитывает остаток, но не чаще раза в час. Молча: это не главное дело.

    Раньше остаток читался только перед отправкой откликов, то есть когда
    заказы вообще есть. В тихий день (или когда встала модель) он не читался
    ни разу, и экран честно писал «ещё не читали» — при работающем софте.
    """
    from datetime import datetime, timedelta, timezone

    if not hasattr(source, "read_balance"):
        return
    known = known_balance(store, _source_of(source))
    if known is not None:
        _value, when = known
        moment = as_utc(when)
        if moment and datetime.now(timezone.utc) - moment < timedelta(
            minutes=BALANCE_EVERY_MINUTES
        ):
            return
    try:
        remember_balance(store, _source_of(source), source.read_balance(context))
    except Exception as exc:  # noqa: BLE001 — остаток не главное дело прохода
        logger.debug("[{}] остаток не прочитался: {}", source.name, exc)


def scan_browser_source(
    store: Store, source, *, notify_new: bool = True, with_details: bool = True
) -> ScanStats:
    """Проход по площадке, доску которой видно только браузером.

    Один браузер на весь проход: поднимать Chromium под каждый заказ — это и память,
    и лишний повод для антибота.
    """
    from .browser import BrowserUnavailable, open_context
    from .sources.profi import NotAuthorized as ProfiNotAuthorized
    from .sources.youdo import NotAuthorized as YoudoNotAuthorized

    # Обе площадки кидают свой NotAuthorized: ловим оба одним кортежем, чтобы
    # «слетела сессия» не тонуло в общем обработчике исключений.
    not_authorized = (ProfiNotAuthorized, YoudoNotAuthorized)

    stats = ScanStats()
    filters = FilterSet.load()

    try:
        with open_context(source.name) as context:
            orders = source.fetch_feed(context)
            stats.fetched = len(orders)
            # Доска ответила — значит вход жив. Снимаем отметку сразу: экран
            # обязан позеленеть, как только владелец вошёл руками.
            session_is_alive(store, _source_of(source))
            _source_is_alive(store, _source_of(source))
            _refresh_balance(store, source, context)

            bootstrap_key = f"bootstrapped:{source.name}"
            is_bootstrap = store.get_state(bootstrap_key) is None

            for order in orders:
                order_pk, is_new = store.upsert_order(order)
                # Недоставленная карточка — не приговор. Раньше отсекали по
                # is_new, и заказ, чьё уведомление не дошло (Телеграм моргнул),
                # на следующем проходе был уже «не новым» и не доходил до
                # отправки никогда — владелец не видел заказ вовсе.
                if not is_new and store.is_notified(order_pk):
                    continue
                if is_new:
                    stats.new += 1

                if is_bootstrap:
                    store.mark_notified(order_pk)
                    continue

                details_ok = True
                if with_details:
                    try:
                        order = source.fetch_details(context, order)
                        store.upsert_order(order)
                    except not_authorized:
                        raise
                    except Exception as exc:  # noqa: BLE001 — недогруженное ТЗ не гасит проход
                        details_ok = False
                        logger.warning("[{}] не догрузил {}: {}", source.name, order.ext_id, exc)

                if not notify_new:
                    # Уведомления выключены — но отметку ставить всё равно
                    # надо. Она значит «заказ обработан», а не «карточка
                    # ушла»: без неё каждый следующий проход заново открывал
                    # страницу КАЖДОГО заказа с доски ради того же ТЗ. На
                    # площадке с антиботом это лишние десятки заходов в час
                    # ровно за тем, что уже лежит в базе.
                    #
                    # Но только если ТЗ действительно доехало. Пометить
                    # обработанным заказ, чью страницу мы не открыли, значит
                    # оставить его с одной строкой описания навсегда — и
                    # отдать модели решать по огрызку.
                    if details_ok:
                        store.mark_notified(order_pk)
                    continue

                if not store.is_notified(order_pk):
                    if not _worth_notifying(filters, order):
                        store.mark_notified(order_pk)
                        continue
                    if notify.send_order(order):
                        store.mark_notified(order_pk)
                        stats.notified += 1

            if is_bootstrap:
                store.set_state(bootstrap_key, "done")
                logger.info(
                    "[{}] первый запуск: {} заказов записаны без уведомлений",
                    source.name,
                    len(orders),
                )
    except not_authorized as exc:
        logger.error("[{}] {}", source.name, exc)
        # Через глушитель, а не напрямую. Profi обходится каждые полминуты, и
        # прямая отправка означала бы примерно сто двадцать одинаковых тревог
        # в час: читать их перестают на третьей, а вместе с ними тонет всё
        # остальное, что бот пишет владельцу.
        _alert_session_lost(store, _source_of(source), exc)
        stats.errors.append(f"{source.name}: {exc}")
    except BrowserUnavailable as exc:
        logger.error("[{}] {}", source.name, exc)
        _alert_source_down(store, _source_of(source), exc)
        stats.errors.append(f"{source.name}: {exc}")
    except Exception as exc:  # noqa: BLE001 — упавшая площадка не гасит остальные
        logger.exception("[{}] проход упал: {}", source.name, exc)
        _alert_source_down(store, _source_of(source), exc)
        stats.errors.append(f"{source.name}: {exc}")

    return stats


def _scan_http_sources(
    store: Store, *, notify_new: bool = True, with_details: bool = True
) -> ScanStats:
    stats = ScanStats()
    filters = FilterSet.load()

    for source in enabled_sources():
        try:
            orders = source.fetch_feed()
        except Exception as exc:  # noqa: BLE001 — одна упавшая биржа не гасит остальные
            logger.error("[{}] лента не прочиталась: {}", source.name, exc)
            stats.errors.append(f"{source.name}: {exc}")
            continue

        stats.fetched += len(orders)
        # Первый запуск: в ленте уже полсотни заказов, и все они «новые».
        # Заполняем базу молча, иначе тебе прилетит полсотни сообщений подряд.
        bootstrap_key = f"bootstrapped:{source.name}"
        is_bootstrap = store.get_state(bootstrap_key) is None

        for order in orders:
            order_pk, is_new = store.upsert_order(order)
            # Та же логика, что в браузерном проходе: недоставленная карточка
            # не должна прятать заказ навсегда.
            if not is_new and store.is_notified(order_pk):
                continue
            if is_new:
                stats.new += 1

            if is_bootstrap:
                store.mark_notified(order_pk)
                continue

            details_ok = True
            if with_details:
                order, details_ok = _with_details(source, order)
                store.upsert_order(order)

            if not notify_new:
                # Та же причина, что и в браузерном проходе: отметка значит
                # «заказ обработан». Без неё лента перекачивала описание
                # каждого известного заказа на каждом проходе. И так же не
                # помечаем заказ, чьё ТЗ не догрузилось.
                if details_ok:
                    store.mark_notified(order_pk)
                continue

            if not store.is_notified(order_pk):
                # Молчим по заказам, которые всё равно не пройдут в отклик.
                if not _worth_notifying(filters, order):
                    store.mark_notified(order_pk)
                    continue
                if notify.send_order(order):
                    store.mark_notified(order_pk)
                    stats.notified += 1

        if is_bootstrap:
            store.set_state(bootstrap_key, "done")
            logger.info(
                "[{}] первый запуск: {} заказов записаны в базу без уведомлений",
                source.name,
                len(orders),
            )

    return stats


def load_details(adapter, order: Order, context=None) -> Order:
    """Догружает полное ТЗ независимо от того, нужен площадке браузер или нет.

    У адаптеров разные подписи не случайно: FL.ru читается обычным HTTP, а
    Profi и Юду — только браузером. Вызывающему коду эта разница не нужна,
    и без общей точки он ошибался в аргументах.
    """
    if getattr(adapter, "needs_browser", False):
        return adapter.fetch_details(context, order)
    return adapter.fetch_details(order)


def _with_details(source: object, order: Order) -> tuple[Order, bool]:
    """Заказ с догруженным ТЗ и признак «получилось».

    Признак нужен вызывающему: заказ, чью страницу мы не открыли, нельзя
    помечать обработанным — иначе он навсегда останется с одной строкой
    описания, и решать по нему модель будет по огрызку.
    """
    try:
        return source.fetch_details(order), True
    except Exception as exc:  # noqa: BLE001 — заказ с коротким ТЗ лучше упавшего прохода
        logger.warning("[{}] не догрузил ТЗ для {}: {}", source.name, order.url, exc)
        return order, False



@dataclass
class ReplySweep:
    """Что вышло из обхода переписок.

    Одного числа мало. Ноль новых ответов и НЕСОСТОЯВШИЙСЯ обход выглядели
    одинаково: кнопка «Проверить ответы» рапортовала «новых ответов нет», хотя
    браузер не открылся или слетела сессия. Владелец читал это как «клиенты
    молчат» и не шёл проверять руками.
    """

    new_replies: int = 0
    checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def happened(self) -> bool:
        """Обход состоялся хоть на одной площадке."""
        return self.checked > 0 or not self.errors


def check_replies_all(store: Store) -> ReplySweep:
    """Обходит обе площадки и уведомляет о новых сообщениях клиентов.

    Возвращает не только число новых ответов, но и то, состоялся ли обход:
    «проверил, тихо» и «проверить не смог» — разные новости.
    """
    from . import dialog
    from .browser import BrowserUnavailable, open_context
    from .replies import check_replies, orders_awaiting_reply

    # Сначала выбрасываем протухшие разрешения — по всем площадкам, включая
    # выключенные: там их некому подобрать, и они висели бы вечно.
    dialog.drop_expired(store)

    sweep = ReplySweep()
    for adapter, source in bidding_sources():
        pending = orders_awaiting_reply(store, source)
        approved = dialog.approved_orders(store)
        if not pending and not approved:
            continue
        try:
            with open_context(adapter.name) as context:
                # Сначала отправляем то, что владелец уже одобрил: браузер
                # открыт, а ответ клиенту тем ценнее, чем быстрее ушёл.
                if approved:
                    dialog.flush_approved(store, adapter, context)
                if not pending:
                    continue
                stats = check_replies(store, context, pending)
                # Клиенты, которые молчат почти сутки, получают одно
                # напоминание. Делаем это здесь же: браузер открыт, а
                # отдельный проход ради этого — лишний повод для антибота.
                from .replies import nudge_silent_clients

                nudged = nudge_silent_clients(store, context, pending)
                if nudged:
                    logger.info("[{}] напомнил о себе по {} заказам", adapter.name, nudged)
                # Отправляем всё, что накопилось за этот проход: и ответы на
                # вопросы клиентов, и напоминания молчунам. Порядок важен —
                # отправка идёт ПОСЛЕ обоих шагов, иначе подготовленное на
                # этом проходе пролежало бы до следующего. Клиент при этом
                # ждёт, а браузер уже открыт и на нужной площадке.
                dialog.flush_approved(store, adapter, context)
                sweep.new_replies += stats.new_replies
                sweep.checked += stats.checked
                sweep.errors.extend(stats.errors)
                logger.info(
                    "[{}] проверено переписок: {}, новых ответов: {}",
                    adapter.name,
                    stats.checked,
                    stats.new_replies,
                )
        except BrowserUnavailable as exc:
            logger.error("[{}] {}", adapter.name, exc)
            sweep.errors.append(f"{adapter.name}: {exc}")
        except Exception as exc:  # noqa: BLE001 — проверка ответов не гасит сбор
            logger.exception("[{}] проверка ответов упала: {}", adapter.name, exc)
            sweep.errors.append(f"{adapter.name}: {exc}")
    return sweep


def flush_pending_all(store: Store) -> int:
    """Разбирает остатки прежней двухфазной очереди. Ничего не досылает.

    Второй фазы больше нет ни на одной площадке (почему — в Bidder.two_phase),
    и досылать текст клиенту эта функция не должна. Прежний вариант обещал в
    заголовке обратное, а на деле не делал вообще ничего: тело цикла стояло
    за проверкой two_phase(), которая всегда False. Заодно не делалась и
    уборка — строки, помеченные к досылу старой версией, оставались в базе
    навсегда, и каждый проход снова видел их как незавершённые.

    Браузер для уборки не нужен: она вся в базе. Раньше здесь на каждую
    площадку открывался контекст — ради цикла, который ни разу не начался.
    """
    from .bidder import Bidder

    return Bidder(store).flush_pending_messages()


def evening_sweep_now(now: datetime | None = None) -> bool:
    """Пора ли добирать отложенные заказы.

    Днём отклик уходит только на заказы с высоким рейтингом — суточный лимит
    и деньги конечны, и тратить их на середнячок в 11 утра глупо. К вечеру
    видно, сколько осталось, и всё непотраченное идёт в дело.
    """
    settings = get_settings()
    now = now or datetime.now(MSK)
    return now.hour >= settings.evening_sweep_hour


def _refresh_price(adapter, context, store: Store, order, order_pk: int) -> None:
    """Перечитывает цену отклика, если её нет.

    Страница заказа читалась только при первом появлении заказа, и если тогда
    цену не удалось разобрать, она оставалась пустой навсегда: фильтр отсеивал
    заказ «цена не прочиталась», а перечитать её было некому. В базе так
    накопилось больше сотни заказов, по которым мы просто не могли решить.

    Заодно это выравнивает учёт: цена, по которой считается решение, снята
    в том же проходе, что и списание.
    """
    if order.source is not Source.PROFI or order.bid_price is not None:
        return
    if not hasattr(adapter, "fetch_details"):
        return
    try:
        adapter.fetch_details(context, order)
    except Exception as exc:  # noqa: BLE001 — не смогли перечитать, решит фильтр
        logger.debug("[{}] цену перечитать не вышло: {}", order.key, exc)
        return
    if order.bid_price is not None:
        store.upsert_order(order)
        logger.info("[{}] цена отклика перечитана: {} ₽", order.key, order.bid_price)


def autobid_all(store: Store, *, min_score: float | None = None) -> int:
    """Автоотклик по собранным заказам. Возвращает число отправленных.

    Вынесен из scan_once намеренно: сбор должен работать даже когда браузер
    недоступен или сессия слетела, а отклик требует и того, и другого.

    min_score: порог рейтинга. None — обычный режим (порог из лимитов, днём
    берём только горячие). Вечером сюда приходит 0 — добираем всё, что прошло
    фильтры, пока не упрёмся в деньги или суточный лимит.
    """
    from .bidder import Bidder
    from .browser import BrowserUnavailable, open_context
    from .models import BidStatus

    bidder = Bidder(store)
    if bidder.is_paused():
        logger.info("Автоотклик на стоп-кране — пропускаю")
        return 0

    if min_score is None and evening_sweep_now():
        min_score = 0.0
        logger.info("Вечерний добор: беру и отложенные заказы, если лимиты позволяют")

    sent_total = 0
    # Причины, по которым отклик не ушёл при живом заказе: деньги и лимиты.
    # По ним владельцу уходит сигнал, что очередь встала не просто так.
    blocked: list[str] = []

    for adapter, source in bidding_sources():
        candidates = _bid_candidates(store, source, filters=bidder.filters)
        if not candidates:
            continue

        try:
            with open_context(adapter.name) as context:
                balance = None
                package = None
                if source is Source.PROFI:
                    balance = adapter.read_balance(context)
                    remember_balance(store, source, balance)
                elif source is Source.FL and hasattr(adapter, "read_package"):
                    # На FL отклик стоит не рублей, а единицы из купленного
                    # пакета. Остаток до сих пор не читался вовсе: о конце
                    # пакета узнавали задним числом — по тому, что отклик не
                    # появился на странице заказа, уже потратив попытку.
                    try:
                        package = adapter.read_package(context)
                    except Exception as exc:  # noqa: BLE001 — сессия слетела
                        # Отдельная ветка: «войди руками» и «биржа моргнула» —
                        # разные новости. Первая без владельца не чинится, и
                        # молчать о ней значит остановить отклики на сутки.
                        #
                        # И тревога своя, а не общая «отклики встали». У той
                        # свой таймер молчания, заведённый под денежные лимиты:
                        # он ждёт часы, потому что «кончился бюджет» само
                        # пройдёт к утру. Слетевший вход не пройдёт никогда, и
                        # прятать его за чужим таймером — значит оставить
                        # площадку мёртвой до случайной проверки.
                        logger.error("[fl] {}", exc)
                        _alert_session_lost(store, source, exc)
                        continue
                    session_is_alive(store, source)
                    if package is None:
                        logger.warning("[fl] остаток пакета не прочитался — откликов не шлю")
                        blocked.append("FL: остаток пакета откликов не прочитался")
                        continue
                    if package <= 0:
                        logger.warning("[fl] пакет откликов пуст — откликов не шлю")
                        remember_balance(store, source, 0)
                        blocked.append(f"FL: {PACKAGE_EMPTY}")
                        continue
                    logger.info("[fl] в пакете осталось откликов: {}", package)
                    remember_balance(store, source, package)

                for order, order_pk in candidates:
                    if package is not None and package <= 0:
                        # Пакет кончился прямо в этом проходе. Продолжать нельзя:
                        # на каждый следующий заказ уйдёт платный вызов модели за
                        # заход, который отправить всё равно нечем — форма отклика
                        # на странице просто не отрисуется.
                        logger.warning("[fl] пакет кончился в этом проходе — останавливаюсь")
                        blocked.append("FL: пакет откликов кончился, купи новый на fl.ru/payed/")
                        break

                    _refresh_price(adapter, context, store, order, order_pk)
                    outcome = bidder.decide(
                        order, order_pk, balance=balance, min_score=min_score
                    )
                    if outcome.status is BidStatus.PLANNED:
                        outcome = bidder.execute(adapter, context, order, outcome, order_pk)
                        if outcome.status is BidStatus.SENT:
                            sent_total += 1
                            if balance is not None:
                                balance -= outcome.cost
                            if package is not None:
                                # Отклик занял единицу из пакета. Раньше остаток
                                # читался один раз до цикла и больше не менялся.
                                package -= 1
                    if _is_money_block(outcome):
                        blocked.append(outcome.reason)
                    bidder.record(order, order_pk, outcome)
                    bidder.announce(order, outcome)
                    if package is not None and PACKAGE_EMPTY in (outcome.reason or ""):
                        # Биржа сама сказала, что пакет пуст: формы отклика на
                        # странице заказа нет, а на платной странице ноль.
                        # Остаток, прочитанный до цикла, устарел — верим бирже,
                        # а не своему счётчику, иначе каждый следующий заказ
                        # съест платный вызов модели ради захода, который
                        # отправить нечем.
                        logger.warning("[fl] биржа сообщила о пустом пакете — останавливаюсь")
                        package = 0
        except BrowserUnavailable as exc:
            logger.error("[{}] {}", adapter.name, exc)
        except Exception as exc:  # noqa: BLE001 — упавший отклик не гасит сбор
            logger.exception("[{}] автоотклик упал: {}", adapter.name, exc)

    _warn_if_queue_stuck(store, blocked)
    return sent_total


# Раз в сколько часов повторять тревогу о слетевшей сессии. Чаще — шум: пока
# владелец не войдёт руками, каждый проход будет упираться в то же самое.
SESSION_ALERT_HOURS = 4


def _source_of(source) -> Source:
    """Площадка адаптера как значение Source. Не узнали — считаем Profi.

    Имя адаптера и значение перечисления совпадают («profi», «fl», «youdo»),
    но полагаться на это молча нельзя: разъедутся — тревога уйдёт под чужим
    ключом глушителя и начнёт повторяться на каждом проходе.
    """
    try:
        return Source(getattr(source, "name", ""))
    except ValueError:
        logger.warning("Неизвестная площадка «{}» — тревогу считаю профи", source)
        return Source.PROFI


# Две отметки на одну беду, и разведены они намеренно.
#
# `sessionlost:<площадка>` — ПРАВДА: сессия лежит прямо сейчас. Её читают
# экраны Orbit, и пишется она всегда. Пока она была одна на двоих с «уже
# сказали», вкладка «Фриланс» показывала бодрое «работает» при мёртвой
# площадке: снаружи это неотличимо от «сегодня нет подходящих заказов».
#
# `sessionlost_told:<площадка>` — «владельцу сказали», и вот у неё свой
# таймер молчания: пока человек не войдёт руками, каждый проход упирается в
# то же самое, и повторять тревогу каждые полминуты бессмысленно.
# Остаток на площадке: рубли на Profi, штуки откликов в пакете FL. Читаются
# они браузером во время прохода и до сих пор жили только в логе — а это две
# цифры, от которых зависит, может ли софт сегодня работать вообще. Владелец
# узнавал их, только открыв лог; на экранах их не было ни одной.
def balance_key(source: Source) -> str:
    return f"balance:{source.value}"


def remember_balance(store: Store, source: Source, value: float | None) -> None:
    """Запоминает остаток и время, когда он прочитан. None — не прочитался."""
    if value is None:
        return
    store.set_state(balance_key(source), f"{value:g}|{_now_utc()}")


def known_balance(store: Store, source: Source) -> tuple[float, str] | None:
    """Последний прочитанный остаток и когда. None — ни разу не читали."""
    raw = store.get_state(balance_key(source)) or ""
    number, _, when = raw.partition("|")
    try:
        return float(number), when
    except ValueError:
        return None


def _now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_lost_key(source: Source) -> str:
    return f"sessionlost:{source.value}"


def session_told_key(source: Source) -> str:
    return f"sessionlost_told:{source.value}"


def lost_sessions(store: Store) -> list[str]:
    """Площадки, вход на которые слетел. Для экранов Orbit."""
    return [
        source.value
        for source in Source
        if store.get_state(session_lost_key(source))
    ]


def session_is_alive(store: Store, source: Source) -> None:
    """Площадка ответила — снимаем обе отметки.

    Правду снимаем сразу: экран обязан позеленеть немедленно. Отметку «уже
    сказали» тоже, иначе следующая беда через месяц промолчала бы.
    """
    store.delete_state(session_lost_key(source))
    store.delete_state(session_told_key(source))


def _alert_session_lost(store: Store, source: Source, exc: Exception) -> None:
    """Зовёт владельца войти на площадку. Отметку «сказали» ставит после доставки.

    Отметка до отправки — это «сказали» при несказанном: сбой Телеграма
    заглушил бы тревогу на четыре часа, а площадка всё это время стояла бы
    мёртвой.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    store.set_state(session_lost_key(source), now.isoformat(timespec="seconds"))

    told = store.get_state(session_told_key(source))
    if told:
        try:
            when = datetime.fromisoformat(told)
        except ValueError:
            when = None
        if when and now - when < timedelta(hours=SESSION_ALERT_HOURS):
            return

    if notify.send_alert(
        f"Сессия {source.value} слетела — отклики и ответы клиентам встали.\n"
        f"Войди руками: fbid login {source.value}\n\n{exc}"
    ):
        store.set_state(session_told_key(source), now.isoformat(timespec="seconds"))


# --- Площадка молчит ----------------------------------------------------------
#
# Сессия слетела и площадка не отвечает — разные беды с разным лечением, и
# вторая до сих пор не доходила до владельца вовсе. 5 сентября умер мобильный
# прокси: проход по Profi.ru падал каждые полминуты восемь суток подряд, в
# логе накопилось три тысячи одинаковых падений, заказы с площадки перестали
# приходить совсем — а в Telegram не ушло ни одного слова. Снаружи это
# выглядело как «на Profi сегодня тихо».

# Сколько проходов подряд должны упасть, чтобы звать человека. Один — это
# моргнувшая сеть, и тревожить по нему нельзя: проход идёт каждые полминуты.
SOURCE_FAILS_BEFORE_ALARM = 5
# Как часто повторять тревогу, пока площадка лежит.
SOURCE_ALERT_HOURS = 12


def source_down_key(source: Source) -> str:
    return f"sourcedown:{source.value}"


def source_fails_key(source: Source) -> str:
    return f"sourcefails:{source.value}"


def source_told_key(source: Source) -> str:
    return f"sourcedown_told:{source.value}"


def down_sources(store: Store) -> list[str]:
    """Площадки, которые сейчас не отвечают. Для экранов готовности."""
    return [
        source.value for source in Source if store.get_state(source_down_key(source))
    ]


def _looks_like_proxy(exc: Exception) -> bool:
    """Похоже ли, что дело в прокси, а не в самой площадке.

    Называть причину важнее, чем назвать площадку: при мёртвом прокси лежат
    сразу все браузерные проходы, и владелец должен чинить одно, а не три.
    """
    text = str(exc).lower()
    return any(
        mark in text
        for mark in ("proxy", "прокси", "tunnel", "туннел", "err_proxy", "proxy_connection")
    )


def _alert_source_down(store: Store, source: Source, exc: Exception) -> None:
    """Говорит владельцу, что площадка не отвечает. Один раз на беду.

    Правда о площадке пишется сразу (её читают экраны), а отметка «сказали» —
    только после доставленного сообщения: сбой Телеграма не должен глушить
    тревогу на полсуток.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fails = 0
    try:
        fails = int(store.get_state(source_fails_key(source)) or 0)
    except ValueError:
        fails = 0
    fails += 1
    store.set_state(source_fails_key(source), str(fails))
    if fails < SOURCE_FAILS_BEFORE_ALARM:
        return

    reason = str(exc)[:200]
    store.set_state(source_down_key(source), f"{now.isoformat(timespec='seconds')}|{reason}")

    told = as_utc((store.get_state(source_told_key(source)) or "").strip())
    if told and now - told < timedelta(hours=SOURCE_ALERT_HOURS):
        return

    if _looks_like_proxy(exc):
        what = (
            "Похоже, дело в прокси: браузер не может через него выйти. "
            "Пока он лежит, встают ВСЕ браузерные проходы — сбор с Profi.ru, "
            "остаток пакета FL и проверка ответов клиентов.\n"
            "Проверить: fbid check-proxy"
        )
    else:
        what = "Проход по площадке падает подряд — сбор и отклики оттуда встали."

    if notify.send_alert(
        f"Площадка {source.value} не отвечает: {fails} проходов подряд упали.\n\n"
        f"{what}\n\n{reason}"
    ):
        store.set_state(source_told_key(source), now.isoformat(timespec="seconds"))


def _source_is_alive(store: Store, source: Source) -> None:
    """Площадка ответила — снимаем отметки и, если жаловались, говорим об этом."""
    if store.get_state(source_down_key(source)):
        store.delete_state(source_down_key(source))
        if store.get_state(source_told_key(source)):
            notify.send_alert(f"Площадка {source.value} снова отвечает — сбор пошёл дальше.")
            store.delete_state(source_told_key(source))
    store.delete_state(source_fails_key(source))


# Слова из причин отказа, которые означают «упёрлись в деньги или лимит»,
# а не «заказ не наш». Только по ним имеет смысл дёргать владельца.
_MONEY_BLOCKS = ("бюджет исчерпан", "лимит откликов", "баланс", "минимум", "пакет откликов")


def _is_money_block(outcome) -> bool:
    """Упёрлись в деньги — или заказ просто не наш?

    Статусов здесь два, и оба нужны. SKIPPED — это наши собственные запреты:
    бюджет, лимит, минимальная цена. FAILED с пустым пакетом — приговор самой
    биржи: формы отклика на странице нет, потому что платить нечем. Пока
    считался только SKIPPED, конец пакета на FL не попадал в «очередь встала»
    вовсе — отклики молча замирали до случайной проверки.
    """
    from .models import BidStatus

    if outcome.status not in (BidStatus.SKIPPED, BidStatus.FAILED):
        return False
    reason = (outcome.reason or "").lower()
    return any(mark in reason for mark in _MONEY_BLOCKS)


# Ключ и период жалобы о вставшей очереди.
QUEUE_ALERT_KEY = "queue:stuck_alert"
QUEUE_ALERT_HOURS = 6


def _warn_if_queue_stuck(store: Store, blocked: list[str]) -> None:
    """Сообщает, что отклики упёрлись в деньги или лимит.

    Без этого «тишина в чате» означает одновременно и «нет подходящих заказов»,
    и «всё есть, но платить нечем» — а это ровно противоположные ситуации.
    """
    if not blocked:
        return

    from datetime import datetime, timedelta, timezone

    last = store.get_state(QUEUE_ALERT_KEY)
    if last:
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            when = None
        if when and datetime.now(timezone.utc) - when < timedelta(hours=QUEUE_ALERT_HOURS):
            return

    queue = waiting_queue(store, limit=3)
    if not queue:
        return

    lines = [f"Отклики встали: {blocked[0]}", "", "Ждут своей очереди:"]
    for item in queue:
        order = item.order
        budget = f"{order.budget_value:,.0f} ₽".replace(",", " ") if order.budget_value else "—"
        lines.append(f"  ⭐{item.score:.1f} {order.title[:45]} ({budget})")
    lines.append("")
    lines.append("Как появятся деньги и лимит — уйдут сами, лучшие первыми.")
    # Отметку «уже говорили» ставим после ДОСТАВЛЕННОЙ тревоги: иначе один
    # сбой Телеграма глушил «отклики встали» на шесть часов — а это ровно то
    # сообщение, без которого тишина в чате неотличима от «нет заказов».
    if notify.send_alert("\n".join(lines)):
        store.set_state(QUEUE_ALERT_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class QueueItem:
    """Заказ, ждущий отклика: что это, насколько интересно и почему ждёт."""

    order: Order
    order_pk: int
    score: float
    reason: str


# Сколько последних заказов смотрит экран очереди. На биржах решает скорость:
# заказ, пролежавший дольше, в отклик всё равно не пойдёт, а перебирать всю
# базу ради пяти строк на экране незачем.
FRESH_WINDOW = 500

# Сколько часов заказ ещё считается свежим. Одно число на очередь и на выбор
# кандидатов: разъедутся — экран снова начнёт обещать отклики по заказам,
# которые проход уже не возьмёт.
CANDIDATE_HOURS = 48


def waiting_queue(
    store: Store, *, limit: int | None = 5, offset: int = 0
) -> list[QueueItem]:
    """Заказы, которые ждут своей очереди на отклик. Лучшие первыми.

    Копятся, когда кончились деньги или суточный лимит: заказ не выброшен,
    а отложен. Как только лимит сбросился или счёт пополнили, ближайший проход
    берёт эту очередь сверху — сначала те, где больше денег и лучше совпадение.

    limit=None отдаёт всю очередь целиком — и целиком по-настоящему.

    Для экрана смотрим только свежий хвост базы (FRESH_WINDOW заказов): на
    биржах решает скорость, и заказ месячной давности в отклик всё равно не
    пойдёт. А вот когда спрашивают ВСЮ очередь — считают её размер или чистят
    её кнопкой, — окно не годится: `LIMIT 500` стоял в запросе всегда, и на
    базе в три тысячи заказов счётчик врал, а «Очистить очередь» вычищала
    ровно пятьсот штук за нажатие. Владелец жал её раз за разом и не понимал,
    почему очередь не кончается.
    """
    from .filters import FilterSet

    filters = FilterSet.load()
    floor = filters.common.min_budget
    queue: list[QueueItem] = []

    # Тем же сроком, что и у автоотклика. Очередь показывала всё подряд, а
    # брать из неё проход мог только заказы моложе CANDIDATE_HOURS: на живой
    # базе полторы сотни строк из двух сотен не ушли бы никогда, и «в очереди
    # 209» читалось как «209 отправим», а отправлялось пятьдесят.
    edge = (
        datetime.now(timezone.utc) - timedelta(hours=CANDIDATE_HOURS)
    ).isoformat(timespec="seconds")
    window = "" if limit is None else f" LIMIT {FRESH_WINDOW}"
    rows = store.conn.execute(
        f"""SELECT o.*, o.pk AS order_pk, b.status, b.reason FROM orders o
           LEFT JOIN bids b ON b.order_pk = o.pk
           WHERE (b.status IS NULL OR b.status = 'skipped')
             AND o.first_seen >= ?
           ORDER BY o.pk DESC{window}""",
        (edge,),
    ).fetchall()

    for row in rows:
        order = store._row_to_order(row)
        if not filters.decide(order).passed:
            continue
        queue.append(
            QueueItem(
                order=order,
                order_pk=int(row["order_pk"]),
                score=score_order(order, budget_floor=floor).total,
                reason=row["reason"] or "ждёт ближайшего прохода",
            )
        )

    queue.sort(key=lambda item: item.score, reverse=True)
    queue = queue[offset:]
    return queue if limit is None else queue[:limit]


def queue_size(store: Store) -> int:
    """Сколько ВСЕГО заказов ждёт отклика. По нему считает и «Очистить»."""
    return len(waiting_queue(store, limit=None))


def listed_queue_size(store: Store) -> int:
    """Сколько заказов очереди реально можно пролистать на экране.

    Экран смотрит свежий хвост базы (FRESH_WINDOW), а `queue_size` считает всю
    очередь целиком. Листалка, считавшая страницы по полному размеру, рисовала
    «Дальше →» там, где дальше пусто: владелец жал и попадал на чистый экран.
    """
    return len(waiting_queue(store, limit=FRESH_WINDOW, offset=0))


def clear_queue(store: Store) -> int:
    """Убирает из очереди всё. Возвращает, сколько заказов убрано.

    Заказы не удаляются из базы: они помечаются отработанными, чтобы софт
    к ним не возвращался. История и отчёты остаются целыми.
    """
    from .models import BidStatus

    removed = 0
    for item in waiting_queue(store, limit=None):
        store.record_bid(item.order_pk, BidStatus.REJECTED, reason="очередь очищена вручную")
        removed += 1
    if removed:
        logger.info("Владелец очистил очередь: убрано {} заказов", removed)
    return removed


def drop_from_queue(store: Store, order_pk: int) -> str | None:
    """Убирает заказ из очереди руками. Возвращает его заголовок.

    Заказ не удаляется из базы, а помечается отработанным: софт к нему больше
    не вернётся, а следующие в очереди поднимаются на его место.
    """
    from .models import BidStatus

    row = store.conn.execute("SELECT title FROM orders WHERE pk = ?", (order_pk,)).fetchone()
    if row is None:
        return None
    store.record_bid(order_pk, BidStatus.REJECTED, reason="убран из очереди вручную")
    logger.info("Заказ {} убран из очереди владельцем", row["title"][:60])
    return row["title"]


# Сколько нерешённых заказов берём на скоринг за проход. Срок свежести —
# CANDIDATE_HOURS, он объявлен рядом с очередью: число одно на обоих.
CANDIDATE_POOL = 300


def _bid_candidates(store: Store, source: Source, *, limit: int = 10, filters=None):
    """Свежие заказы без решения, лучшие первыми.

    Суточный лимит откликов конечен, поэтому важно, на что он потратится.
    Сортировка по рейтингу: заказ с деньгами и по нашей витрине уходит вперёд
    очереди, даже если появился позже.

    Берём именно «без решения», а не «последние по номеру». Прежнее окно
    «120 последних» голодало: за день простоя (кончились деньги на модель)
    новые заказы выталкивали нерешённые за край окна, и те не попадали в
    кандидаты уже никогда — на живой базе так молча лежали две сотни свежих
    заказов. Свежесть отсекаем отдельно: старше двух суток заказ на бирже
    уже разобран.
    """
    floor = filters.common.min_budget if filters else None
    # Границу считаем в UTC, потому что в UTC лежит и first_seen. Сравнение
    # идёт строками, и московская метка «…T19:00:00+03:00» сопоставлялась с
    # «…T16:00:00+00:00» посимвольно: окно молча укорачивалось на три часа —
    # 45 вместо заявленных 48. Заказы этих трёх часов выпадали из кандидатов
    # навсегда.
    edge = (
        datetime.now(timezone.utc) - timedelta(hours=CANDIDATE_HOURS)
    ).isoformat(timespec="seconds")
    rows = store.conn.execute(
        """SELECT * FROM orders o
           WHERE o.source = ? AND o.first_seen >= ?
             AND NOT EXISTS (
               SELECT 1 FROM bids b WHERE b.order_pk = o.pk
                 AND b.status IN ('sent', 'dry_run', 'unavailable', 'rejected'))
           ORDER BY o.pk DESC LIMIT ?""",
        (source.value, edge, CANDIDATE_POOL),
    ).fetchall()
    candidates = []
    for row in rows:
        order = store._row_to_order(row)
        candidates.append((order, int(row["pk"])))

    candidates.sort(key=lambda pair: score_order(pair[0], budget_floor=floor).total, reverse=True)
    return candidates[:limit]


def attractiveness(order: Order) -> float:
    """Рейтинг заказа числом. Тонкая обёртка над scoring.score_order."""
    return score_order(order).total

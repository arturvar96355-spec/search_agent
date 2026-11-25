"""Вкладка «Фриланс» — заказы с бирж.

Систему freelance-bid Orbit не переписывает: она собирает ленты, считает деньги
и отправляет отклики сама. Здесь только два дела.

Первое — воронка. Биржа знает лишь «отклик отправлен», а дальше сделка живёт
в переписке: клиент ответил, дошли до созвона, взяли в работу. Эти стадии
Orbit держит у себя и подтягивает из биржи то, что она умеет замечать сама:
появление ответа в чате.

Второе — готовые экраны. Отчёт, очередь, лимиты у freelance-bid уже есть, и
они здесь переиспользуются как есть, с подменой кодов кнопок. Так правка в
самой системе сразу видна в Orbit, и одну логику не надо держать в двух местах.
"""
from __future__ import annotations

import time
from contextlib import suppress
from datetime import UTC

from loguru import logger

from ..config import get_settings
from ..models import FREELANCE_TITLES, STAGE_ICONS, Contact, Kind, Stage
from ..registry import Health, Section
from ..store import Store
from ..ui import PAGE_SIZE, Screen, clip, counted, esc, pager

CODE = "f"
# Кнопки самой биржи приходят со своими кодами («stats:1»), Orbit дописывает
# к ним префикс: «f:fb:stats:1» → раздел f, экран биржи stats:1.
BRIDGE = "fb"

# Отметки, которые ставит биржа в СВОЕЙ базе, а читаем мы. Имена перечислены
# здесь один раз: ниже они уходят в SQL, и опечатка в LIKE не вызывает ошибки —
# просто ничего не находится, и вкладка «Начали общение» тихо перестаёт
# наполняться. Что имена всё ещё те самые, проверяет отдельный тест: он
# спрашивает их у самой биржи, а не сверяет две строки в одном файле.
REPLIED_MARK = "clientreplied"
READYCALL_MARK = "readycall"

# Стадии вкладки в том порядке, в котором заказ по ним движется.
STAGES = (Stage.WRITTEN, Stage.REPLIED, Stage.CALL, Stage.DEAL)


class BidsNotInstalled(RuntimeError):
    """Проект биржи лежит на диске, но не установлен в окружение Orbit.

    Это разные вещи, и путать их дорого: «биржи нет» владелец читает как
    «сегодня нет заказов», а на самом деле нужно одно действие —
    `pip install -e ../freelance-bid` в .venv Orbit. Зависимость сквозная и
    ставится вручную, потому что это соседний проект, а не пакет с PyPI.
    """


def _fbid():
    """Модули биржи. Импорт внутри функции: без неё Orbit должен работать."""
    try:
        from fbid import tgbot
        from fbid.config import get_settings as fbid_settings
        from fbid.store import Store as FbidStore
    except ImportError as exc:
        raise BidsNotInstalled(
            "проект биржи не установлен в окружение Orbit: "
            ".venv/bin/pip install -e ../freelance-bid"
        ) from exc

    return tgbot, FbidStore, fbid_settings


def _open_fbid_store():
    _, FbidStore, fbid_settings = _fbid()
    return FbidStore(fbid_settings().db_path)


# --- Синхронизация с биржей -------------------------------------------------

def sync(store: Store) -> int:
    """Подтягивает отправленные отклики и отметки об ответах клиентов.

    Стадию только повышаем. Если владелец уже отметил «готов к созвону», а
    биржа по-прежнему видит один отклик, откатывать назад нельзя: биржа знает
    меньше, чем человек, который с клиентом разговаривал.
    """
    fbid_store = _open_fbid_store()
    try:
        rows = fbid_store.conn.execute(
            # Время отклика берём тем же выражением, что и вся биржа: bid_at
            # заполнялся только в двухфазном режиме, которого больше нет, и в
            # боевой базе он пуст у всех откликов. Строка «Откликнулись» в
            # карточке из-за этого не появлялась НИ РАЗУ.
            """SELECT o.source, o.ext_id, o.url, o.title, o.budget_value, b.text,
                      COALESCE(b.message_sent_at, b.bid_at, b.created_at) AS bid_at
               FROM bids b JOIN orders o ON o.pk = b.order_pk
               WHERE b.status = 'sent' ORDER BY b.pk"""
        ).fetchall()
        # Считать сообщения нельзя: Profi.ru дописывает под нашим откликом
        # свой блок с ценой, да и владелец мог написать клиенту сам — и то и
        # другое увеличивает счётчик, не будучи ответом. Поэтому берём отметку
        # «clientreplied», которую ставит модель, разобрав переписку.
        # Значение отметки — дата первого ответа; у старых записей там «1».
        # Сравнивать с «1» нельзя: после перехода на дату вкладка «Начали
        # общение» переставала наполняться, и ответы клиентов пропадали из
        # воронки молча. Значима сама непустота.
        replied = {
            str(row["key"]).split(":", 1)[1]
            for row in fbid_store.conn.execute(
                "SELECT key, value FROM state WHERE key LIKE ? AND value != ''",
                (f"{REPLIED_MARK}:%",),
            ).fetchall()
        }
        # Клиент согласился созвониться — модель ставит эту отметку сама,
        # и заказ переезжает в «Готовы к созвону» без нажатий владельца.
        ready_call = {
            str(row["key"]).split(":", 1)[1]
            for row in fbid_store.conn.execute(
                "SELECT key, value FROM state WHERE key LIKE ? AND value = '1'",
                (f"{READYCALL_MARK}:%",),
            ).fetchall()
        }
    finally:
        fbid_store.close()

    touched = 0
    for row in rows:
        key = f"{row['source']}:{row['ext_id']}"
        has_answer = key in replied

        existing = store.find_contact(Kind.ORDER, key)
        stage = existing.stage if existing else Stage.WRITTEN
        if has_answer and _rank(stage) < _rank(Stage.REPLIED):
            stage = Stage.REPLIED
        if key in ready_call and _rank(stage) < _rank(Stage.CALL):
            stage = Stage.CALL

        contact = Contact(
            kind=Kind.ORDER,
            key=key,
            name=row["title"] or "Заказ",
            stage=stage,
            source_url=row["url"],
            meta={
                "platform": row["source"],
                "budget": row["budget_value"] or "",
                "bid_at": row["bid_at"] or "",
                "reply": (row["text"] or "")[:900],
            },
        )
        pk = store.upsert_contact(contact)
        # upsert стадию не трогает намеренно (повторный импорт не должен сбивать
        # воронку), поэтому повышение отмечаем отдельно.
        if existing is None or stage is not existing.stage:
            store.set_stage(pk, stage, "с биржи")
            touched += 1
    return touched


def _rank(stage: Stage) -> int:
    """Место стадии в воронке. Неизвестная — выше всех, её не понижаем.

    DEAD в STAGES нет, и раньше он получал -1: заказ, помеченный «отказались»,
    синхронизация поднимала обратно в «откликнулись» на ближайшем проходе,
    вопреки обещанию «стадию только повышаем». Владелец закрывал заказ, а тот
    возвращался в работу.
    """
    order = {s: i for i, s in enumerate(STAGES)}
    if stage in order:
        return order[stage]
    return len(STAGES) + 1  # DEAD и прочее вне воронки: трогать не даём


def _to_int(raw) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _clamp_page(page: int, total: int) -> int:
    """Не даёт открыть страницу, которой больше нет.

    «Назад» помнит, на какой странице был владелец, — и это правильно. Но
    список за это время укоротился ровно потому, что владелец нажал «Взяли в
    работу» или «Отказались»: заказ ушёл в другую стадию. Возврат на вторую
    страницу однострочного списка показывал «Пусто.» — на непустом списке, без
    листалки (листать нечего) и без дороги к первой странице.
    """
    last = max(0, (max(0, total) - 1) // PAGE_SIZE)
    return min(max(0, page), last)


# --- Экраны -----------------------------------------------------------------

# Как часто освежать копию биржи ради ЧУЖИХ экранов. Внутри вкладки sync
# зовётся всегда: там владелец пришёл именно за свежими цифрами.
QUIET_SYNC_SECONDS = 60
# Время последнего тихого обновления. Список, а не число, чтобы менять его
# изнутри функции без global — и чтобы тесты могли обнулить.
_synced_at: list[float] = [0.0]


def platform_title(code: str) -> str:
    """Как площадка называется на экранах: «FL.ru», а не «fl» и не «FL».

    Имя спрашиваем у самой биржи — там оно уже есть и уже уходит владельцу в
    карточках откликов. Пока здесь стоял .upper(), одна и та же площадка
    называлась на трёх экранах по-разному: «fl» в сводке, «FL» в карточке
    заказа и «FL.ru» в уведомлении об отклике.
    """
    code = (code or "").strip()
    try:
        from fbid.notify import SOURCE_TITLES

        return SOURCE_TITLES.get(code.lower(), code.upper())
    except Exception:  # noqa: BLE001 — биржа не подключена, обойдёмся кодом
        return code.upper()


def lost_sessions_now() -> list[str]:
    """Площадки, вход на которые слетел. Биржи может не быть подключено вовсе.

    Спрашивают об этом двое — главный экран и вечерняя сводка, — и ответ у них
    обязан быть один. Пока функция жила приватной в bot.py, сводка про
    слетевший вход молчала: владелец весь день не получал ни одного отклика на
    площадке и узнавал об этом, только открыв бота.
    """
    try:
        fbid_store = _open_fbid_store()
    except Exception:  # noqa: BLE001 — биржа не подключена, спрашивать нечего
        return []
    try:
        return _lost_sessions(fbid_store)
    except Exception:  # noqa: BLE001 — биржа не отвечает: это отдельная новость
        return []
    finally:
        with suppress(Exception):
            fbid_store.close()


def down_sources_now() -> list[str]:
    """Площадки, которые не отвечают вовсе. Биржи может не быть подключено.

    Спрашивают об этом трое — главный экран, экран готовности и вечерняя
    сводка, — и ответ у них обязан быть один.
    """
    try:
        fbid_store = _open_fbid_store()
    except Exception:  # noqa: BLE001 — биржа не подключена, спрашивать нечего
        return []
    try:
        from fbid.pipeline import down_sources

        return down_sources(fbid_store)
    except Exception:  # noqa: BLE001 — биржа отвечает не так, как мы ждём
        return []
    finally:
        with suppress(Exception):
            fbid_store.close()


def quiet_notes_now(day: str) -> list[str]:
    """Строки с бирж, отложенные до вечерней сводки, за московский день.

    Отложенное живёт в базе бирж, а сводку собирает Orbit. Границы дня
    считаем здесь: у fbid отметки в UTC, а день у нас московский — по
    календарной дате строки, записанные после полуночи по Москве, терялись бы.
    """
    from datetime import UTC, datetime, time

    from ..store import MSK

    try:
        start = datetime.combine(datetime.fromisoformat(day).date(), time.min, tzinfo=MSK)
    except ValueError:
        logger.warning("Фриланс: не разобрал день «{}» — отложенное не покажу", day)
        return []
    end = start.replace(hour=23, minute=59, second=59)

    def _utc(moment: datetime) -> str:
        return moment.astimezone(UTC).isoformat(timespec="seconds")

    try:
        fbid_store = _open_fbid_store()
    except Exception:  # noqa: BLE001 — биржа не подключена, сводка уходит без неё
        return []
    try:
        return fbid_store.quiet_between(_utc(start), _utc(end))
    except Exception as exc:  # noqa: BLE001 — старая база без таблицы
        logger.warning("Фриланс: отложенные строки не прочитались: {}", exc)
        return []
    finally:
        fbid_store.close()


def refresh_quietly(store: Store, *, every: float = QUIET_SYNC_SECONDS) -> None:
    """Освежает копию биржи для экранов вне вкладки. Тихо и не чаще раза в минуту.

    Главный экран считал заказы по копии, а копию обновлял только вход во
    вкладку «Фриланс». Пока владелец туда не заходил, в блоке «требует вас»
    не было ни «готовы к созвону», ни «ответили» — то есть ровно то, ради
    чего этот блок и сделан, на бирже не показывалось вовсе. Заглянуть в
    биржу дважды за минуту дешевле, чем пропустить готового к созвону.

    Ошибки глотаем: биржи может не быть подключено вовсе, а главный экран
    обязан открыться в любом случае.
    """
    now = time.monotonic()
    if now - _synced_at[0] < every:
        return
    _synced_at[0] = now
    try:
        sync(store)
    except Exception as exc:  # noqa: BLE001 — биржа лежит, экран всё равно рисуем
        logger.debug("Тихое обновление биржи не прошло: {}", exc)


def home(store: Store) -> Screen:
    try:
        moved = sync(store)
        if moved:
            logger.info("Фриланс: обновил {} заказов", moved)
        broken = None
    except Exception as exc:  # noqa: BLE001 — биржа лежит, вкладка всё равно открывается
        logger.warning("Фриланс: синхронизация не прошла: {}", exc)
        broken = str(exc)

    counts = store.stage_counts(Kind.ORDER)
    lines = ["<b>💼 Фриланс</b>", "FL.ru, Profi.ru и Юду: сбор заказов и автоотклик.", ""]
    if broken:
        lines.append(f"⚠️ Биржа не отвечает: {esc(broken)}")
        lines.append("")
    lines.append(_today_line())
    lines.append("")
    for stage in STAGES:
        lines.append(f"{STAGE_ICONS[stage]} {FREELANCE_TITLES[stage]} — <b>{counts[stage]}</b>")

    rows = [
        [("📨 Отклики за сегодня", f"{CODE}:bids:today:0"),
         ("🗂 Все отклики", f"{CODE}:bids:all:0")],
        [(f"📤 Откликнулись · {counts[Stage.WRITTEN]}", f"{CODE}:list:written:0"),
         (f"💬 Общение · {counts[Stage.REPLIED]}", f"{CODE}:list:replied:0")],
        [(f"📞 На созвон · {counts[Stage.CALL]}", f"{CODE}:list:call:0"),
         (f"🤝 В работе · {counts[Stage.DEAL]}", f"{CODE}:list:deal:0")],
        [("📊 Отчёт за сутки", f"{CODE}:{BRIDGE}:stats:1"), ("⏳ Очередь", f"{CODE}:{BRIDGE}:queue")],
        [("⚙️ Лимиты трат", f"{CODE}:{BRIDGE}:limits"), ("🔧 Состояние", f"{CODE}:{BRIDGE}:status")],
        [(f"🤖 Клиент ответил: {_mode_label()}", f"{CODE}:auto")],
        [("⬅️ В главное меню", "home")],
    ]
    return Screen("\n".join(lines), rows)


def _mode_label() -> str:
    """Подпись кнопки: что софт делает, когда клиент ответил."""
    try:
        from fbid import dialog

        fbid_store = _open_fbid_store()
        try:
            return dialog.MODE_TITLES[dialog.mode(fbid_store)]
        finally:
            fbid_store.close()
    except Exception:  # noqa: BLE001 — подпись кнопки не должна ронять экран
        return "недоступно"


def cycle_mode(store: Store) -> Screen:
    """Переключает переписку на биржах: молчим или отвечаем дешёвым сами.

    Положения всего два, и это важно. Прежний тумблер на три положения
    включал автоответ сразу всем, включая заказы на сотни тысяч; теперь
    «отвечать» ограничено порогом суммы, а сам порог живёт в лимитах и
    случайным нажатием не сдвигается.
    """
    from fbid import dialog, limits

    fbid_store = _open_fbid_store()
    try:
        following = (
            dialog.MODE_AUTO
            if dialog.mode(fbid_store) == dialog.MODE_NOTIFY
            else dialog.MODE_NOTIFY
        )
        dialog.set_mode(fbid_store, following)
        ceiling = limits.get("auto_reply_max_price", fbid_store)
    finally:
        fbid_store.close()

    screen = home(store)
    if following == dialog.MODE_AUTO:
        screen.alert = (
            f"Отвечаю сам по заказам до {ceiling:.0f} ₽ и веду до созвона. "
            "Дороже и договорные — только скажу вам"
        )
    else:
        screen.alert = "Молчу на всех биржах: клиент ответил — просто скажу вам"
    return screen


def _today_line() -> str:
    """Сколько откликов и денег ушло сегодня — цифры берём у самой биржи."""
    try:
        from fbid.models import Source

        fbid_store = _open_fbid_store()
        try:
            profi_sent, spent = fbid_store.spend_today(Source.PROFI)
            fl_sent, _ = fbid_store.spend_today(Source.FL)
        finally:
            fbid_store.close()
        today = counted(profi_sent + fl_sent, "отклик", "отклика", "откликов")
        return f"Сегодня: <b>{today}</b> · потрачено {spent:.0f} ₽"
    except Exception as exc:  # noqa: BLE001 — счётчик не должен ронять экран
        logger.warning("Фриланс: счётчик за сутки не собрался: {}", exc)
        return "Сегодня: счётчик недоступен"


# --- Список отправленных откликов -------------------------------------------

def _day_start_utc() -> str:
    """Начало сегодняшних суток по Москве — в том же виде, в каком лежит в базе."""
    from datetime import datetime

    from fbid.store import MSK

    midnight = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


def _when(raw: str, *, with_date: bool) -> str:
    """Время отклика по Москве. В базе оно лежит в UTC."""
    from datetime import datetime

    from fbid.store import MSK

    try:
        moment = datetime.fromisoformat(raw).astimezone(MSK)
    except (TypeError, ValueError):
        return raw[:16]
    return moment.strftime("%d.%m %H:%M") if with_date else moment.strftime("%H:%M")


def bids(store: Store, arg: str) -> Screen:
    """Отклики, которые мы отправили: за сегодня или за всё время.

    Список идёт из базы биржи, а не из воронки Orbit: воронка показывает, где
    заказ сейчас, а здесь нужен сам факт отправки — с ценой, временем и ссылкой,
    чтобы можно было открыть заказ и проверить, что отклик на месте.
    """
    scope, _, page_raw = arg.partition(":")
    scope = "today" if scope != "all" else "all"
    page = _to_int(page_raw)
    since = _day_start_utc() if scope == "today" else None

    fbid_store = _open_fbid_store()
    try:
        total = fbid_store.sent_bids_count(since=since)
        # Список откликов за сегодня укорачивается сам собой в полночь, а
        # ссылка на вторую страницу живёт в старом сообщении сколько угодно.
        page = _clamp_page(page, total)
        rows_db = fbid_store.sent_bids(since=since, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    finally:
        fbid_store.close()

    title = "📨 Отклики за сегодня" if scope == "today" else "🗂 Все отклики"
    lines = [f"<b>{title}</b> · {counted(total, 'отклик', 'отклика', 'откликов')}", ""]
    if not rows_db:
        lines.append("Пусто." if scope == "all" else "Сегодня ещё ни одного.")

    spent = 0.0
    for row in rows_db:
        spent += float(row["cost_rub"] or 0)
        platform = platform_title(str(row["source"]))
        head = esc(str(row["title"] or "Заказ")[:70])
        link = f'<a href="{esc(str(row["url"]))}">{head}</a>' if row["url"] else head
        tail = [platform, _when(str(row["at"]), with_date=scope == "all")]
        if row["budget_value"]:
            tail.append(f"бюджет {float(row['budget_value']):.0f} ₽")
        if row["cost_rub"]:
            tail.append(f"списано {float(row['cost_rub']):.0f} ₽")
        lines.append(f"{link}\n{esc(' · '.join(tail))}")
        lines.append("")
    if spent:
        lines.append(f"На этой странице списано: <b>{spent:.0f} ₽</b>")

    other = "all" if scope == "today" else "today"
    other_label = "🗂 За всё время" if scope == "today" else "📨 Только сегодня"
    rows = pager(f"{CODE}:bids:{scope}", page, total)
    rows.append([(other_label, f"{CODE}:bids:{other}:0")])
    rows.append([("⬅️ Назад", f"{CODE}:home")])
    return Screen(clip("\n".join(lines)), rows)


def show_list(store: Store, arg: str) -> Screen:
    stage_code, _, page_raw = arg.partition(":")
    page = _to_int(page_raw)
    try:
        stage = Stage(stage_code)
    except ValueError:
        stage = Stage.WRITTEN

    total = store.count(Kind.ORDER, stage)
    page = _clamp_page(page, total)
    items = store.contacts(Kind.ORDER, stage, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    lines = [f"<b>{STAGE_ICONS[stage]} {FREELANCE_TITLES[stage]}</b> · {total}", ""]
    if not items:
        lines.append("Пусто.")

    rows: list[list[tuple[str, str]]] = []
    for contact in items:
        platform = platform_title(str(contact.meta.get("platform", "")))
        budget = contact.meta.get("budget")
        head = f"<b>{esc(contact.name[:60])}</b>"
        tail = " · ".join(filter(None, [platform, f"{budget} ₽" if budget else ""]))
        lines.append(f"{head}\n{esc(tail)}")
        lines.append("")
        rows.append([
            (contact.name[:28] or "Заказ", f"{CODE}:card:{contact.pk}:{stage_code}.{page}")
        ])

    rows += pager(f"{CODE}:list:{stage_code}", page, total)
    rows.append([("⬅️ Назад", f"{CODE}:home")])
    return Screen(clip("\n".join(lines)), rows)


def _split_origin(arg: str, parts: int = 1) -> tuple[list[str], str]:
    """Делит аргумент кнопки на «свои» части и хвост «откуда пришли».

    Хвост нужен, чтобы «назад» вернула в тот же список, из которого заказ
    открыли. Без него отметка «отказались» уводила владельца в список
    отказов — экран, куда из меню вообще не попасть.
    """
    pieces = (arg or "").split(":")
    own = pieces[:parts]
    while len(own) < parts:
        own.append("")
    rest = pieces[parts:]
    return own, rest[0] if rest else ""


def _back_to_list(origin: str, stage: Stage) -> str:
    """Список, в который ведёт «назад». Пусто — список текущей стадии заказа."""
    stage_code, _, page = origin.partition(".")
    if not stage_code:
        return f"{CODE}:list:{stage.value}:0"
    return f"{CODE}:list:{stage_code}:{page or '0'}"


def card(store: Store, arg: str) -> Screen:
    (pk_raw,), origin = _split_origin(arg)
    contact = store.get_contact(_to_int(pk_raw))
    if contact is None:
        return Screen("Заказ не найден.", [[("⬅️ Назад", f"{CODE}:home")]])

    platform = platform_title(str(contact.meta.get("platform", "")))
    lines = [f"<b>{esc(contact.name)}</b>", platform]
    if contact.meta.get("budget"):
        lines.append(f"Бюджет: {esc(contact.meta['budget'])} ₽")
    if contact.meta.get("bid_at"):
        # Время в базе биржи лежит в UTC — показываем по Москве, как и всё
        # остальное в этом разделе.
        lines.append(f"Откликнулись: {esc(_when(str(contact.meta['bid_at']), with_date=True))}")
    lines.append(f"\n{STAGE_ICONS[contact.stage]} {FREELANCE_TITLES[contact.stage]}")

    if contact.meta.get("reply"):
        lines += ["", "<b>Что мы написали:</b>", esc(str(contact.meta["reply"]))]

    tail = f":{origin}" if origin else ""
    rows: list[list[tuple[str, str]]] = []
    if contact.source_url:
        rows.append([("🔗 Открыть чат на бирже", f"url:{contact.source_url}")])
    rows.append([("💬 Начали общение", f"{CODE}:mark:replied:{contact.pk}{tail}"),
                 ("📞 Готов к созвону", f"{CODE}:mark:call:{contact.pk}{tail}")])
    rows.append([("🤝 Взяли в работу", f"{CODE}:mark:deal:{contact.pk}{tail}"),
                 ("🚫 Отказались", f"{CODE}:mark:dead:{contact.pk}{tail}")])
    rows.append([("⬅️ Назад", _back_to_list(origin, contact.stage))])
    return Screen(clip("\n".join(lines)), rows)


def mark(store: Store, arg: str) -> Screen:
    (stage_code, pk_raw), origin = _split_origin(arg, parts=2)
    pk = _to_int(pk_raw)
    back = f"{pk}:{origin}" if origin else str(pk)
    try:
        stage = Stage(stage_code)
    except ValueError:
        return card(store, back)
    store.set_stage(pk, stage, "вручную")
    screen = card(store, back)
    screen.alert = FREELANCE_TITLES[stage]
    return screen


# --- Мост к экранам биржи ---------------------------------------------------

# Действия биржи, которые лезут в браузер. Orbit их не выполняет: браузером
# занят сборщик (`fbid watch`), у него открыт профиль площадки и стоит прокси.
# Если пустить сюда, в лучшем случае будет ждать блокировку профиля, в худшем —
# тихо вернёт «ответов нет», и владелец решит, что клиенты молчат.
BROWSER_ACTIONS = {"replies"}


def understands(action_code: str) -> bool:
    """Наш ли это код кнопки биржи.

    Спрашивает сама биржа: список кодов живёт у неё. Без этого вопроса любое
    незнакомое нажатие уходило туда, а она отвечала «Не понял» и подменяла
    собой сообщение — вместе с вопросом клиента и готовым ответом, если они
    были в той же карточке.
    """
    if action_code.split(":")[0] in BROWSER_ACTIONS:
        return True
    try:
        tgbot, _, _ = _fbid()
    except Exception:  # noqa: BLE001 — биржи может не быть вовсе
        return False
    knows = getattr(tgbot, "knows", None)
    if knows is None:
        # Старая версия биржи без списка кодов. Отказываться нельзя: живые
        # кнопки её карточек перестанут работать. Пропускаем как раньше.
        return True
    return bool(knows(action_code))


def bridge(action_code: str) -> Screen:
    """Открывает экран freelance-bid, подменив коды кнопок на свои."""
    if action_code.split(":")[0] in BROWSER_ACTIONS:
        return Screen(
            "🔍 <b>Проверка переписок</b>\n\n"
            "Ей занимается сборщик: он сам обходит заказы с нашими откликами "
            "и, как только клиент что-то написал, присылает сюда его вопрос "
            "вместе с готовым ответом.\n\n"
            "Отсюда запускать её не нужно: браузер площадки занят сборщиком, "
            "и два процесса в один профиль не помещаются.",
            [[("⬅️ К фрилансу", f"{CODE}:home")]],
        )

    tgbot, _, _ = _fbid()
    fbid_store = _open_fbid_store()
    try:
        text, markup = tgbot.handle(tgbot.parse_action(action_code), fbid_store)
    finally:
        fbid_store.close()

    return screen_from_markup(text, markup)


def screen_from_markup(text: str, markup: dict) -> Screen:
    """Ответ биржи — в кнопки Orbit.

    Отдельной функцией, потому что мест два: экраны биржи открываются здесь,
    а ответ на введённый лимит приходит в bot.py — диалог начинает биржа, но
    сообщение владельца попадает в общего бота. Там этот же перевод был
    написан заново и без разбора кнопки «домой»: после ввода лимита на экране
    оказывались две «назад» подряд, и одна уводила в чужое главное меню.
    """
    rows: list[list[tuple[str, str]]] = []
    has_home = False
    for row in markup.get("inline_keyboard", []):
        converted = []
        for button in row:
            data = button.get("callback_data", "")
            # Кнопка «домой» самой биржи должна вести в её раздел Orbit,
            # иначе владелец проваливается в чужое главное меню.
            if data == "menu":
                has_home = True
                converted.append(("⬅️ К фрилансу", f"{CODE}:home"))
                continue
            converted.append((button["text"], f"{CODE}:{BRIDGE}:{data}"))
        rows.append(converted)
    # Своя кнопка «назад» — только если у экрана биржи её не было. Две подряд
    # на один и тот же адрес выглядят как разные и заставляют выбирать.
    if not has_home:
        rows.append([("⬅️ К фрилансу", f"{CODE}:home")])
    return Screen(text, rows)


# --- Точка входа ------------------------------------------------------------

def handle(store: Store, action: str, arg: str) -> Screen:
    if action == BRIDGE:
        return bridge(arg)
    if action == "bids":
        return bids(store, arg)
    if action == "list":
        return show_list(store, arg)
    if action == "card":
        return card(store, arg)
    if action == "mark":
        return mark(store, arg)
    if action == "auto":
        return cycle_mode(store)
    return home(store)


def health(store: Store) -> Health:
    path = get_settings().bids_path
    if not path.exists():
        return Health(False, "проект не найден", [f"ожидался в {path}"])

    try:
        _fbid()
    except BidsNotInstalled as exc:
        # Каталог есть, а пакета нет. Говорим ровно это и как чинить, иначе
        # владелец читает «биржа молчит» как «сегодня нет заказов».
        return Health(False, "проект есть, но не установлен", [str(exc)])

    from fbid.bidder import Bidder
    from fbid.config import get_settings as fbid_settings
    from fbid.models import Source

    settings = fbid_settings()
    if not settings.db_path.exists():
        # Открывать базу нельзя: sqlite создаст пустую, Store накатит схему, и
        # вкладка бодро отрапортует «работает · 0 откликов» — то есть соврёт,
        # что биржа работает и просто ничего не нашла. Настоящая база при этом
        # лежит рядом с другим .env или ещё не создана.
        return Health(
            False,
            "база биржи не найдена",
            [f"ожидалась в {settings.db_path}", "проверь .env проекта freelance-bid"],
        )

    fbid_store = _open_fbid_store()
    try:
        orders = fbid_store.orders_count()
        profi_sent, spent = fbid_store.spend_today(Source.PROFI)
        fl_sent, _ = fbid_store.spend_today(Source.FL)
        paused = Bidder(fbid_store).is_paused()
        lost = _lost_sessions(fbid_store)
        # Площадка может не отвечать вовсе — чаще всего из-за мёртвого прокси.
        # Это не «вход слетел» и лечится не входом, но вкладка обязана быть
        # красной: 5 сентября Profi замолчал на восемь суток, а здесь всё это
        # время горело «работает · сегодня 0 откликов».
        from fbid.pipeline import down_sources

        down = down_sources(fbid_store)
    finally:
        fbid_store.close()

    details = [f"заказов в базе: {orders}", f"потрачено сегодня: {spent:.0f} ₽"]
    if settings.dry_run:
        return Health(True, "режим проверки: отклики не отправляются", details)
    if paused:
        return Health(False, "новые отклики остановлены кнопкой", details)
    if lost:
        # Слетевший вход — это стоящая площадка, и вкладка обязана быть
        # красной. Раньше она показывала бодрое «работает»: снаружи мёртвая
        # биржа неотличима от «сегодня нет подходящих заказов», а чинится это
        # только руками владельца.
        where = ", ".join(platform_title(name) for name in lost)
        return Health(
            False,
            f"вход слетел: {where} — отклики и ответы клиентам встали",
            [*details, f"войти руками: fbid login {lost[0]}"],
        )
    if down:
        where = ", ".join(platform_title(name) for name in down)
        return Health(
            False,
            f"{where} не отвечает — сбор и отклики оттуда встали",
            [*details, "чаще всего это прокси: fbid check-proxy"],
        )
    today = counted(profi_sent + fl_sent, "отклик", "отклика", "откликов")
    return Health(True, f"работает · сегодня {today}", details)


def _lost_sessions(fbid_store) -> list[str]:
    """Площадки, вход на которые слетел. Старая версия биржи их не знает."""
    try:
        from fbid.pipeline import lost_sessions
    except ImportError:
        return []
    try:
        return list(lost_sessions(fbid_store))
    except Exception as exc:  # noqa: BLE001 — состояние биржи не должно ронять вкладку
        logger.warning("Не смог спросить биржу про сессии: {}", exc)
        return []


def load() -> Section:
    _fbid()  # проверяем импорт сразу, а не при первом нажатии
    return Section(
        code=CODE,
        title="Фриланс",
        icon="💼",
        handle=handle,
        health=health,
        knows=understands,
        commands=[
            ("freelance", "Фриланс: заказы с бирж", "f:home"),
            ("report", "Отчёт по откликам за сутки", "f:fb:stats:1"),
            ("limits", "Лимиты трат на биржах", "f:fb:limits"),
        ],
    )

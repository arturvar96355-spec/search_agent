"""Хранилище на SQLite: заказы, отклики, траты, служебное состояние.

Зачем БД, а не файл: нужен надёжный дедуп (не откликнуться дважды на один заказ)
и счётчик суточных трат, переживающий рестарт — на нём висит защита баланса Profi.ru.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import BidStatus, Order, Source

# Сутки считаем по Москве: биржи и лимиты живут в МСК, а сервер может быть в UTC.
MSK = ZoneInfo("Europe/Moscow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    pk              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    ext_id          TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    category        TEXT DEFAULT '',
    budget_raw      TEXT DEFAULT '',
    budget_value    REAL,
    bid_price       REAL,
    published_at    TEXT,
    first_seen      TEXT NOT NULL,
    notified_at     TEXT,
    raw             TEXT DEFAULT '{}',
    -- Кто разместил заказ. Пусто, если площадка заказчика не раскрывает.
    client_key      TEXT,
    UNIQUE (source, ext_id)
);

CREATE TABLE IF NOT EXISTS bids (
    pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_pk    INTEGER NOT NULL REFERENCES orders(pk),
    status      TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    text        TEXT DEFAULT '',
    cost_rub    REAL DEFAULT 0,
    created_at  TEXT NOT NULL,
    -- Сгенерированный заход целиком. Отложенный заказ берёт его отсюда,
    -- а не заказывает у модели заново.
    draft_json  TEXT,
    UNIQUE (order_pk)
);

CREATE TABLE IF NOT EXISTS spend (
    day         TEXT NOT NULL,
    source      TEXT NOT NULL,
    bids        INTEGER NOT NULL DEFAULT 0,
    amount_rub  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
);

CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Строки, о которых решено не дёргать владельца в ту же секунду. Их забирает
-- вечерняя сводка Orbit. Отдельная таблица, а не state: state — это «текущее
-- значение по ключу», а здесь нужен именно список событий за день.
CREATE TABLE IF NOT EXISTS quiet (
    pk   INTEGER PRIMARY KEY AUTOINCREMENT,
    at   TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quiet_at ON quiet(at);

CREATE INDEX IF NOT EXISTS idx_orders_seen ON orders(first_seen);
"""
# Индекс по заказчику создаётся не здесь, а в _migrate: на уже существующей базе
# колонки client_key ещё нет, и CREATE INDEX по ней упал бы на старте.


# Статусы, после которых заказ больше не трогаем. Отправлено и списано; биржа
# отказала по причине, которая сама не изменится; заказ отсеян окончательно.
BLOCKING_STATUSES = (
    BidStatus.SENT.value,
    BidStatus.DRY_RUN.value,
    BidStatus.UNAVAILABLE.value,
    BidStatus.REJECTED.value,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Когда отклик ушёл на самом деле — одним выражением на все запросы. Порядок
# важен: `message_sent_at` ставится в момент отправки и в проде заполнен
# всегда; `bid_at` — только в двухфазном режиме, которого больше нет;
# `created_at` — время генерации захода, а не отправки. Разъедутся по файлу —
# один экран покажет отклик сегодняшним, другой вчерашним.
SENT_AT = "COALESCE(b.message_sent_at, b.bid_at, b.created_at)"


def as_utc(raw: str | None) -> datetime | None:
    """Отметка времени из базы — в момент с часовым поясом. None — не разобрали.

    Один разбор на весь проект. Без него в каждом месте писалось своё, и в
    двух из них забыли про отметки БЕЗ пояса: вычитание такой из
    `datetime.now(timezone.utc)` не «даёт неверный ответ», а бросает TypeError.
    В одном случае это гасило проверку ответов по всей площадке, в другом —
    весь проход по заказам. Старые отметки без пояса писала прежняя версия,
    и в боевой базе они переживут нас обоих.
    """
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _column(row: sqlite3.Row, name: str):
    """Значение колонки, если она есть в выборке. Иначе None.

    Нужно для запросов с JOIN и для баз, где колонку добавила миграция:
    обращение к отсутствующему имени у sqlite3.Row кидает IndexError.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def today_msk() -> str:
    return datetime.now(MSK).date().isoformat()


def order_fingerprint(title: str | None, category: str | None, description: str | None) -> str:
    """Текст, по которому заказ отличают от других.

    Обычно это заголовок. Но на Profi.ru своего названия у заказа нет — в
    карточке стоит имя категории, одинаковое у десятков заказов. Тогда берём
    начало описания: его пишет сам заказчик, и одинаковые описания означают,
    что одно задание развешено несколько раз.
    """
    head = (title or "").strip()
    if head and head.casefold() != (category or "").strip().casefold():
        return head
    return " ".join((description or "").split())[:120]


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # В базу пишут два процесса разом: основной цикл (`fbid watch`) и бот
        # (`fbid bot`, правка лимитов кнопками). В режиме по умолчанию запись
        # блокирует читателей, и цикл мог бы упасть на «database is locked»
        # ровно в момент, когда владелец меняет лимит.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Добавляет недостающие колонки в уже существующую базу."""
        bid_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(bids)")}
        # Двухфазный отклик: отметка времени, когда ушёл текст вторым шагом.
        if "message_sent_at" not in bid_columns:
            self.conn.execute("ALTER TABLE bids ADD COLUMN message_sent_at TEXT")
        if "bid_at" not in bid_columns:
            self.conn.execute("ALTER TABLE bids ADD COLUMN bid_at TEXT")
        # Готовый заход от модели. Хранится, чтобы отложенный до вечера заказ
        # не генерировался заново — каждая генерация стоит денег.
        if "draft_json" not in bid_columns:
            self.conn.execute("ALTER TABLE bids ADD COLUMN draft_json TEXT")

        order_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(orders)")}
        # Заказчик: нужен, чтобы не откликнуться дважды одному человеку.
        if "client_key" not in order_columns:
            self.conn.execute("ALTER TABLE orders ADD COLUMN client_key TEXT")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(source, client_key)"
        )

    # --- Отложенное до вечерней сводки ------------------------------------

    def add_quiet(self, text: str) -> None:
        """Кладёт строку в журнал дня, ничего не отправляя."""
        self.conn.execute(
            "INSERT INTO quiet (at, text) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), text[:1000]),
        )
        self.conn.commit()

    def quiet_notes(self, day: str) -> list[str]:
        """Отложенные строки за день (YYYY-MM-DD по UTC). Порядок — как случилось."""
        rows = self.conn.execute(
            "SELECT text FROM quiet WHERE at LIKE ? ORDER BY pk", (f"{day}%",)
        ).fetchall()
        return [row["text"] for row in rows if row["text"]]

    def quiet_between(self, since: str, until: str) -> list[str]:
        """Отложенные строки за промежуток времени. Границы — время UTC.

        Нужен потому, что сводку читает Orbit, а день у него московский:
        строка, записанная в час ночи по Москве, лежит здесь под вчерашней
        датой UTC. По календарной дате такие строки терялись бы каждую ночь.
        """
        rows = self.conn.execute(
            "SELECT text FROM quiet WHERE at >= ? AND at < ? ORDER BY pk", (since, until)
        ).fetchall()
        return [row["text"] for row in rows if row["text"]]

    def close(self) -> None:
        self.conn.close()

    # --- Заказы -----------------------------------------------------------

    def _raw_of(self, order_pk: int) -> str:
        """Сырые данные заказа как они лежат в базе. Для слияния при обновлении."""
        row = self.conn.execute("SELECT raw FROM orders WHERE pk = ?", (order_pk,)).fetchone()
        return (row["raw"] if row else "") or "{}"

    def upsert_order(self, order: Order) -> tuple[int, bool]:
        """Сохраняет заказ. Возвращает (pk, is_new).

        Повторная встреча заказа обновляет описание/цену отклика, но НЕ трогает
        notified_at — иначе один заказ прилетит в TG десять раз.
        """
        cur = self.conn.execute(
            "SELECT pk FROM orders WHERE source = ? AND ext_id = ?",
            (order.source.value, order.ext_id),
        )
        row = cur.fetchone()
        published = order.published_at.isoformat() if order.published_at else None

        if row:
            # raw дополняем, а не заменяем. Лента про заказчика не знает ничего,
            # и её пустой raw затирал бы то, что сняли со страницы заказа:
            # признак «заказчик на бирже меньше месяца» жил ровно до следующего
            # прохода ленты, то есть около минуты, и до решения не доживал.
            merged = json.loads(self._raw_of(row["pk"]) or "{}")
            merged.update(order.raw or {})
            order.raw = merged
            self.conn.execute(
                """UPDATE orders SET
                       -- Полное ТЗ со страницы заказа не затираем обрезком из
                       -- ленты. Лента отдаёт первые ~170 знаков с многоточием,
                       -- и следующий проход сборщика раньше клал этот обрезок
                       -- поверх подробного описания. По нему потом считался
                       -- рейтинг заказа и писался платный заход.
                       description = CASE
                           WHEN length(?) >= length(COALESCE(description, '')) THEN ?
                           ELSE description END,
                       budget_raw = COALESCE(NULLIF(?, ''), budget_raw),
                       budget_value = COALESCE(?, budget_value),
                       bid_price = COALESCE(?, bid_price),
                       raw = ?,
                       client_key = COALESCE(NULLIF(?, ''), client_key)
                   WHERE pk = ?""",
                (
                    order.description or "",
                    order.description or "",
                    order.budget_raw or "",
                    order.budget_value,
                    # Цену тоже не сбрасываем в NULL: лента её не знает, а на
                    # странице заказа она уже могла прочитаться.
                    order.bid_price,
                    json.dumps(order.raw, ensure_ascii=False),
                    # Заказчик определяется на странице заказа, а лента его не знает:
                    # пустым значением затирать уже найденное нельзя.
                    order.client_key or "",
                    row["pk"],
                ),
            )
            self.conn.commit()
            return int(row["pk"]), False

        cur = self.conn.execute(
            """INSERT INTO orders (source, ext_id, url, title, description, category,
                   budget_raw, budget_value, bid_price, published_at, first_seen, raw,
                   client_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.source.value,
                order.ext_id,
                order.url,
                order.title,
                order.description or "",
                order.category,
                order.budget_raw,
                order.budget_value,
                order.bid_price,
                published,
                _now(),
                json.dumps(order.raw, ensure_ascii=False),
                order.client_key or "",
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid), True

    def mark_notified(self, order_pk: int) -> None:
        """Заказ обработан проходом сбора: ТЗ догружено, карточка решена.

        Имя историческое — «уведомлён». На деле отметка шире: когда
        уведомления о новых заказах выключены (а в проде они выключены),
        она значит просто «этот заказ проход уже разобрал, второй раз
        открывать его страницу незачем». Отклики от неё не зависят вовсе:
        кандидатов на отклик выбирают по таблице `bids`, а не по ней.
        """
        self.conn.execute("UPDATE orders SET notified_at = ? WHERE pk = ?", (_now(), order_pk))
        self.conn.commit()

    def is_notified(self, order_pk: int) -> bool:
        """Разбирал ли проход этот заказ. См. mark_notified про имя."""
        row = self.conn.execute(
            "SELECT notified_at FROM orders WHERE pk = ?", (order_pk,)
        ).fetchone()
        return bool(row and row["notified_at"])

    def orders_count(self, source: Source | None = None) -> int:
        if source:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE source = ?", (source.value,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
        return int(row["n"])

    def iter_orders(self, source: Source | None = None, limit: int | None = None) -> list[Order]:
        """Заказы из базы обратно в модели — для прогона фильтров без обращения к биржам."""
        sql = "SELECT * FROM orders"
        params: list = []
        if source:
            sql += " WHERE source = ?"
            params.append(source.value)
        sql += " ORDER BY pk DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        return [self._row_to_order(row) for row in self.conn.execute(sql, params)]

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> Order:
        return Order(
            source=Source(row["source"]),
            ext_id=row["ext_id"],
            url=row["url"],
            title=row["title"],
            description=row["description"] or "",
            category=row["category"] or "",
            budget_raw=row["budget_raw"] or "",
            budget_value=row["budget_value"],
            bid_price=row["bid_price"],
            client_key=_column(row, "client_key") or "",
            published_at=(
                datetime.fromisoformat(row["published_at"]) if row["published_at"] else None
            ),
            raw=json.loads(row["raw"] or "{}"),
        )

    def find_order(self, source: Source, ext_id: str) -> tuple[Order, int] | None:
        """Заказ по площадке и её внутреннему номеру. None — такого нет."""
        row = self.conn.execute(
            "SELECT * FROM orders WHERE source = ? AND ext_id = ?", (source.value, ext_id)
        ).fetchone()
        return (self._row_to_order(row), int(row["pk"])) if row else None

    def recent_orders(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM orders ORDER BY pk DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    # --- Отклики ----------------------------------------------------------

    def record_bid(
        self,
        order_pk: int,
        status: BidStatus,
        *,
        reason: str = "",
        text: str = "",
        cost_rub: float = 0.0,
    ) -> None:
        """Пишет решение по заказу. UNIQUE(order_pk) не даёт откликнуться дважды.

        Запись с окончательным статусом (отправлено, биржа отказала, отсеяно)
        НЕ затирается временной. Иначе так: команда `fbid bid` по уже
        отработанному заказу получает «уже откликались» — то есть SKIPPED — и
        этим самым SKIPPED стирает отметку «отправлено». Заказ снова становится
        свободным, и на него уходит второй платный отклик.

        Отдельно защищаем SENT: его не перекрывает НИКАКОЙ ДРУГОЙ статус.
        Раньше окончательный статус мог сменить другой окончательный, и нажатие
        «убрать из очереди» по уже отправленному отклику записывало REJECTED
        поверх — вместе с обнулением суммы. Деньги списаны, а в истории их нет.

        Сам себя SENT дописывать может, и это нужно: факт списания кладётся в
        базу сразу после оплаты, одной короткой строкой, а текст отклика и
        причина приезжают следующим шагом. Запрети мы и это — в истории
        осталась бы отметка «заплачено» без единого слова о том, за что.
        """
        blocking = ",".join("?" * len(BLOCKING_STATUSES))
        self.conn.execute(
            f"""INSERT INTO bids (order_pk, status, reason, text, cost_rub, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_pk) DO UPDATE SET
                   status = excluded.status, reason = excluded.reason,
                   text = excluded.text,
                   -- Сумму не обнуляем: деньги уже списаны, и запись о них
                   -- важнее любого последующего уточнения.
                   cost_rub = MAX(excluded.cost_rub, bids.cost_rub)
               WHERE (bids.status <> ? OR excluded.status = ?)
                 AND (bids.status NOT IN ({blocking})
                      OR excluded.status IN ({blocking}))""",
            (order_pk, status.value, reason, text, cost_rub, _now(),
             BidStatus.SENT.value, BidStatus.SENT.value, *BLOCKING_STATUSES, *BLOCKING_STATUSES),
        )
        self.conn.commit()

    def sent_bids(
        self, *, since: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[sqlite3.Row]:
        """Отправленные отклики, новые сверху. since — граница по времени (UTC ISO).

        Время берём по факту ОТПРАВКИ, тем же порядком, что и bid_time_iso:
        `message_sent_at` в проде заполнен всегда, `bid_at` пуст (второй фазы
        больше нет ни на одной площадке), `created_at` — момент, когда модель
        написала заход. Заказ мог пролежать в очереди до вечера: по created_at
        отправленный сегодня отклик попадал во «вчера» и исчезал из экрана
        «Отклики за сегодня» — а деньги за него в сводке были сегодняшние.
        """
        where = "b.status = ?"
        args: list = [BidStatus.SENT.value]
        if since:
            where += f" AND {SENT_AT} >= ?"
            args.append(since)
        return self.conn.execute(
            f"""SELECT o.source, o.title, o.url, o.budget_value, b.cost_rub, b.text,
                       {SENT_AT} AS at
                FROM bids b JOIN orders o ON o.pk = b.order_pk
                WHERE {where} ORDER BY at DESC LIMIT ? OFFSET ?""",
            (*args, limit, offset),
        ).fetchall()

    def sent_bids_count(self, *, since: str | None = None) -> int:
        where = "status = ?"
        args: list = [BidStatus.SENT.value]
        if since:
            where += " AND COALESCE(message_sent_at, bid_at, created_at) >= ?"
            args.append(since)
        row = self.conn.execute(f"SELECT COUNT(*) FROM bids WHERE {where}", args).fetchone()
        return int(row[0])

    def mark_bid_placed(self, order_pk: int) -> None:
        """Фиксирует момент, когда отклик занял очередь (фаза 1)."""
        self.conn.execute("UPDATE bids SET bid_at = ? WHERE pk IS NOT NULL AND order_pk = ?",
                          (_now(), order_pk))
        self.conn.commit()

    def mark_message_sent(self, order_pk: int) -> None:
        self.conn.execute(
            "UPDATE bids SET message_sent_at = ? WHERE order_pk = ?", (_now(), order_pk)
        )
        self.conn.commit()

    def order_by_pk(self, order_pk: int) -> Order | None:
        """Заказ по номеру в базе. Нужен кнопкам бота: в них влезает только число."""
        row = self.conn.execute("SELECT * FROM orders WHERE pk = ?", (order_pk,)).fetchone()
        return self._row_to_order(row) if row else None

    def pending_messages(self, older_than_seconds: float) -> list[tuple[Order, int, str]]:
        """Отклики фазы 1, которым пора отправить текст.

        Возвращает (заказ, pk, текст). Пустой текст сюда не попадает — отправлять
        нечего, и такой отклик надо чинить руками, а не слать пустоту.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        ).isoformat(timespec="seconds")

        rows = self.conn.execute(
            """SELECT b.order_pk, b.text, o.* FROM bids b JOIN orders o ON o.pk = b.order_pk
               WHERE b.status = ? AND b.message_sent_at IS NULL
                 AND b.bid_at IS NOT NULL AND b.bid_at <= ?
                 AND TRIM(COALESCE(b.text, '')) != ''
               ORDER BY b.bid_at""",
            (BidStatus.SENT.value, cutoff),
        ).fetchall()

        result = []
        for row in rows:
            result.append((self._row_to_order(row), int(row["order_pk"]), row["text"]))
        return result

    def bid_time_iso(self, order_pk: int) -> str:
        """Когда ушёл наш отклик, машинной строкой. Пусто — отклика нет.

        Порядок источников важен. `message_sent_at` ставится в момент отправки
        и в проде заполнен всегда; `bid_at` — только в двухфазном режиме,
        которого больше нет, поэтому он всюду пуст; `created_at` — время
        ПЕРВОГО прохода по заказу, то есть генерации захода, а не отправки.
        Отложенный до вечера заказ по created_at выглядит отвеченным утром,
        расхождение доходит до девятнадцати часов. Раньше COALESCE начинался
        с bid_at и молча сваливался на created_at — то есть всегда врал.

        Отдельно от bid_time: та форматирует для человека («22.08 в 19:40»),
        а сравнивать даты надо в исходном виде.
        """
        row = self.conn.execute(
            """SELECT COALESCE(message_sent_at, bid_at, created_at) AS at
               FROM bids WHERE order_pk = ?""",
            (order_pk,),
        ).fetchone()
        return str(row["at"]) if row and row["at"] else ""

    def bid_time(self, order_pk: int) -> str:
        """Когда ушёл наш отклик, по Москве. Пусто — отклика нет.

        Момент берём тот же, что и bid_time_iso, а не свой. Здесь стоял
        отдельный COALESCE, начинавшийся с пустого в проде `bid_at` и
        сваливавшийся на `created_at` — время генерации захода. В карточке
        «клиент молчит» владелец читал «Откликнулись 22.08 в 00:40» по
        отклику, ушедшему в семь вечера, и считал дни молчания не оттуда.
        """
        at = self.bid_time_iso(order_pk)
        if not at:
            return ""
        try:
            return datetime.fromisoformat(at).astimezone(MSK).strftime("%d.%m в %H:%M")
        except ValueError:
            return ""

    def bid_text(self, order_pk: int) -> str:
        """Текст нашего отклика. По нему в переписке узнаём свои сообщения."""
        row = self.conn.execute(
            "SELECT text FROM bids WHERE order_pk = ?", (order_pk,)
        ).fetchone()
        return (row["text"] or "") if row else ""

    def has_bid(self, order_pk: int) -> bool:
        """Стоит ли ещё пробовать этот заказ.

        SENT/DRY_RUN — уже отработали. UNAVAILABLE — биржа отказала по причине,
        которая сама не изменится (мы уже откликались, пакет пуст, заказ закрыт):
        без этого софт долбился бы в один и тот же заказ каждый проход и слал
        владельцу одну и ту же тревогу. SKIPPED/FAILED повтор не блокируют —
        там причина временная.
        """
        row = self.conn.execute(
            "SELECT status FROM bids WHERE order_pk = ?", (order_pk,)
        ).fetchone()
        # Окончательный отказ тоже блокирует: без этого заказ каждый цикл
        # заново уходил бы в Claude API за новым заходом — деньги в никуда.
        return bool(row and row["status"] in BLOCKING_STATUSES)

    def bid_to_client(
        self, source: Source, client_key: str, *, within_days: int = 14, exclude_pk: int = 0
    ) -> str | None:
        """Заголовок заказа, по которому мы уже писали этому же заказчику.

        None — заказчику ещё не писали (или площадка его не раскрывает).
        Три отклика одному человеку за неделю читаются как рассылка, а не как
        студия, которая внимательно прочитала ТЗ.
        """
        if not client_key:
            return None
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=within_days)
        ).isoformat(timespec="seconds")

        row = self.conn.execute(
            """SELECT o.title FROM orders o JOIN bids b ON b.order_pk = o.pk
               WHERE o.source = ? AND o.client_key = ? AND o.pk != ?
                 AND b.status IN (?, ?) AND b.created_at >= ?
               ORDER BY b.created_at DESC LIMIT 1""",
            (
                source.value,
                client_key,
                exclude_pk,
                BidStatus.SENT.value,
                BidStatus.DRY_RUN.value,
                cutoff,
            ),
        ).fetchone()
        return row["title"] if row else None

    def recent_bid_titles(
        self, source: Source, *, within_days: int = 14, limit: int = 200
    ) -> list[str]:
        """Тексты заказов, по которым мы уже писали. Для поиска близнецов.

        Площадки не всегда раскрывают заказчика (FL.ru прячет его от гостей),
        поэтому одинаковые заявки ловим по тексту: три почти одинаковых задания
        — это почти всегда один человек, и три наших отклика читаются как
        рассылка.

        Отдаём заголовок, а где его нет — начало описания. Своего названия у
        заказа на Profi.ru не бывает вовсе: в карточке стоит имя категории, и
        оно одинаково у десятков заказов. По такому «заголовку» близнецов не
        найти, и защита на этой площадке не работала ни одним из двух способов
        — при том, что заказчика Profi тоже не раскрывает.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=within_days)
        ).isoformat(timespec="seconds")

        rows = self.conn.execute(
            """SELECT o.title, o.category, o.description FROM orders o
               JOIN bids b ON b.order_pk = o.pk
               WHERE o.source = ? AND b.status IN (?, ?) AND b.created_at >= ?
               ORDER BY b.created_at DESC LIMIT ?""",
            (source.value, BidStatus.SENT.value, BidStatus.DRY_RUN.value, cutoff, limit),
        ).fetchall()
        return [order_fingerprint(row["title"], row["category"], row["description"])
                for row in rows]

    # --- Кэш заходов ------------------------------------------------------

    def save_draft(self, order_pk: int, draft_json: str) -> None:
        """Запоминает сгенерированный заход.

        Заказ, отложенный до вечера, иначе уходил бы к модели заново на каждом
        проходе — это прямые деньги за один и тот же текст.
        """
        self.conn.execute(
            """INSERT INTO bids (order_pk, status, reason, draft_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(order_pk) DO UPDATE SET draft_json = excluded.draft_json""",
            (
                order_pk,
                BidStatus.SKIPPED.value,
                # Причина не пустая намеренно: строка сразу попадает в отчёт,
                # и «—» без объяснения выглядело бы как потерянный заказ.
                "заход сгенерирован, решение ещё не принято",
                draft_json,
                _now(),
            ),
        )
        self.conn.commit()

    def load_draft(self, order_pk: int) -> str | None:
        row = self.conn.execute(
            "SELECT draft_json FROM bids WHERE order_pk = ?", (order_pk,)
        ).fetchone()
        return _column(row, "draft_json") if row else None

    # --- Траты и лимиты ---------------------------------------------------

    def add_spend(self, source: Source, amount_rub: float, day: str | None = None) -> None:
        day = day or today_msk()
        self.conn.execute(
            """INSERT INTO spend (day, source, bids, amount_rub) VALUES (?, ?, 1, ?)
               ON CONFLICT(day, source) DO UPDATE SET
                   bids = bids + 1, amount_rub = amount_rub + excluded.amount_rub""",
            (day, source.value, amount_rub),
        )
        self.conn.commit()

    def average_bid_cost(self, source: Source, *, last: int = 10) -> float | None:
        """Во что обходится один отклик на площадке. None — ещё не платили.

        По последним отправкам, а не по потолку из настроек: потолок говорит,
        сколько мы согласны заплатить, а не сколько площадка берёт на самом
        деле. Разница на Profi двукратная, и по потолку остаток на счёте
        выглядел бы вдвое меньшим, чем есть.
        """
        rows = self.conn.execute(
            """SELECT b.cost_rub FROM bids b JOIN orders o ON o.pk = b.order_pk
               WHERE b.status = 'sent' AND o.source = ? AND b.cost_rub > 0
               ORDER BY b.pk DESC LIMIT ?""",
            (source.value, last),
        ).fetchall()
        costs = [float(row["cost_rub"]) for row in rows]
        return sum(costs) / len(costs) if costs else None

    def spend_today(self, source: Source) -> tuple[int, float]:
        """Возвращает (сколько откликов, сколько рублей) за сегодня по МСК."""
        row = self.conn.execute(
            "SELECT bids, amount_rub FROM spend WHERE day = ? AND source = ?",
            (today_msk(), source.value),
        ).fetchone()
        if not row:
            return 0, 0.0
        return int(row["bids"]), float(row["amount_rub"])

    # --- Служебное состояние ---------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def delete_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM state WHERE key = ?", (key,))
        self.conn.commit()

"""База Orbit: контакты, тексты писем, стадии и журнал.

Почему отдельная база, а не общая с системами: у freelance-bid своя база про
заказы и деньги, у парсера вообще нет базы — только выгрузки в файлы. Orbit
не лезет ни в ту, ни в другую, а держит своё: кто в работе, на какой стадии,
что ему написали и когда.

Записи в базу идут из двух мест (бот и фоновая отправка), поэтому WAL и
таймаут ожидания — без них второй процесс ловит «database is locked».
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from .models import FREE_MAIL_DOMAINS, Channel, Contact, Draft, Kind, Stage

# Москва: время в отчётах и суточные лимиты считаем по нему, а не по UTC.
MSK = timezone(timedelta(hours=3))

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    name       TEXT NOT NULL,
    stage      TEXT NOT NULL DEFAULT 'new',
    city       TEXT,
    site       TEXT,
    emails     TEXT NOT NULL DEFAULT '[]',
    phones     TEXT NOT NULL DEFAULT '[]',
    telegram   TEXT NOT NULL DEFAULT '[]',
    source_url TEXT,
    meta       TEXT NOT NULL DEFAULT '{}',
    note       TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, key)
);
CREATE INDEX IF NOT EXISTS idx_contacts_stage ON contacts(kind, stage);

CREATE TABLE IF NOT EXISTS drafts (
    pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_pk INTEGER NOT NULL REFERENCES contacts(pk) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft',
    error      TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    -- Когда владелец разрешил отправку. Письмо ждёт своей очереди у фонового
    -- отправщика: пачка из двадцати писем за одну секунду — первый признак
    -- рассылки и для получателя, и для спам-фильтра.
    approved_at TEXT,
    -- Сколько раз пытались отправить. Мёртвый адрес иначе долбится в почтовый
    -- сервер бесконечно, а за перебор подключений блокируют весь ящик.
    attempts INTEGER NOT NULL DEFAULT 0,
    -- Момент, когда письмо отдали почтовому серверу. Ставится ДО разговора с
    -- ним: если процесс умрёт посреди отправки, только по этой отметке и
    -- видно, что письмо могло уже уйти.
    handed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
-- Один живой черновик на контакт: перегенерация заменяет старый, а не плодит
-- очередь из десяти писем одному агентству.
CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_one_per_contact
    ON drafts(contact_pk) WHERE status = 'draft';

CREATE TABLE IF NOT EXISTS events (
    pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_pk INTEGER REFERENCES contacts(pk) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_contact ON events(contact_pk, pk DESC);

-- Сколько отправок сделано за день. `count` — все каналы разом: суточный
-- лимит меряет касания компании, а получателю всё равно, письмом ему написали
-- или в телеграм руками. `by_mail` — только то, что ушло ИЗ ЯЩИКА: прогрев
-- ограничивает ящик, и ручная отправка в телеграм его нагрузкой не была.
CREATE TABLE IF NOT EXISTS sends (
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    by_mail INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Что мы выяснили про адрес до отправки. Держим отдельно от контакта: один
-- и тот же домен встречается у десятка компаний, а спрашивать DNS про него
-- десять раз подряд незачем.
CREATE TABLE IF NOT EXISTS email_checks (
    address    TEXT PRIMARY KEY,
    ok         INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    -- 1 = приговор окончательный (домена нет, ящик служебный). Такое знание
    -- не протухает, в отличие от удачной проверки.
    permanent  INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL
);
"""


def now_msk() -> str:
    return datetime.now(MSK).isoformat(timespec="seconds")


def today_msk() -> str:
    return datetime.now(MSK).date().isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Досоздаёт то, чего нет в уже существующей базе.

        CREATE TABLE IF NOT EXISTS не трогает таблицу, которая уже есть, —
        новая колонка в SCHEMA до неё просто не доедет, и запросы начнут
        падать на живой базе. Поэтому колонки добавляем отдельно.
        """
        added = {
            "drafts": {
                "approved_at": "TEXT",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "handed_at": "TEXT",
            },
            "sends": {
                # На старых базах почтовых отправок отдельно не считали. Ноль
                # здесь безопасен: прогрев решит, что ящик сегодня молчал, и
                # пропустит письма — а не наоборот.
                "by_mail": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in added.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                    logger.info("База: добавил колонку {}.{}", table, name)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- Контакты ---------------------------------------------------------

    def upsert_contact(self, contact: Contact) -> int:
        """Добавляет контакт или дополняет уже известный.

        Повторный импорт той же выгрузки не должен ни плодить дубли, ни
        затирать то, что мы уже знаем: стадию, заметку, найденные вручную
        контакты. Поэтому списки объединяем, а стадию не трогаем совсем.
        """
        existing = self.find_contact(contact.kind, contact.key)
        now = now_msk()
        if existing is None:
            cursor = self.conn.execute(
                """INSERT INTO contacts
                   (kind, key, name, stage, city, site, emails, phones, telegram,
                    source_url, meta, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contact.kind.value, contact.key, contact.name, contact.stage.value,
                    contact.city, contact.site,
                    _dump(contact.emails), _dump(contact.phones), _dump(contact.telegram),
                    contact.source_url, _dump(contact.meta), contact.note, now, now,
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

        merged_meta = {**existing.meta, **contact.meta}
        # Вычеркнутый адрес обратно не берём, как бы настойчиво его ни
        # приносила выгрузка: о возврате письма знает только база.
        dead = set(merged_meta.get(DEAD_EMAILS) or [])
        emails = [
            address for address in _merge(existing.emails, contact.emails)
            if address not in dead
        ]
        self.conn.execute(
            """UPDATE contacts SET name = ?, city = ?, site = ?, emails = ?, phones = ?,
                   telegram = ?, source_url = ?, meta = ?, updated_at = ?
               WHERE pk = ?""",
            (
                contact.name or existing.name,
                contact.city or existing.city,
                contact.site or existing.site,
                _dump(emails),
                _dump(_merge(existing.phones, contact.phones)),
                _dump(_merge(existing.telegram, contact.telegram)),
                contact.source_url or existing.source_url,
                _dump(merged_meta),
                now,
                existing.pk,
            ),
        )
        self.conn.commit()
        return int(existing.pk)  # type: ignore[arg-type]

    def set_emails(self, pk: int, emails: list[str]) -> None:
        """Переписывает список адресов контакта.

        Нужно там, где адрес умер, а компания жива: вычеркнуть один ящик и
        оставить остальные каналы — не то же самое, что закрыть контакт.

        Вычеркнутые адреса запоминаем навсегда. Иначе следующий импорт той же
        выгрузки парсера возвращал их обратно: в файле-то адрес по-прежнему
        есть, а о том, что письмо с него вернулось, знает только база. Письмо
        уходило второй раз на мёртвый ящик — и это не просто холостой ход, а
        ещё один возврат в репутацию нашего домена.
        """
        current = self.get_contact(pk)
        gone = [
            address for address in (current.emails if current else [])
            if address not in emails
        ]
        meta = dict(current.meta) if current else {}
        if gone:
            dead = list(dict.fromkeys([*meta.get(DEAD_EMAILS, []), *gone]))
            meta[DEAD_EMAILS] = dead
            self.conn.execute(
                "UPDATE contacts SET emails = ?, meta = ?, updated_at = ? WHERE pk = ?",
                (_dump(emails), _dump(meta), now_msk(), pk),
            )
        else:
            self.conn.execute(
                "UPDATE contacts SET emails = ?, updated_at = ? WHERE pk = ?",
                (_dump(emails), now_msk(), pk),
            )
        self.conn.commit()

    def set_meta(self, pk: int, meta: dict) -> None:
        """Записывает служебные пометки как есть, вместе с удалёнными ключами.

        upsert_contact намеренно только дополняет: повторный импорт выгрузки
        не должен ничего терять. Но иногда пометку нужно именно убрать —
        например, вернуть компанию в обход сайтов после починки обхода.
        Через upsert это не сделать, ключ приезжает обратно из старой записи.
        """
        self.conn.execute(
            "UPDATE contacts SET meta = ?, updated_at = ? WHERE pk = ?",
            (_dump(meta), now_msk(), pk),
        )
        self.conn.commit()

    def update_meta(self, pk: int, changes: dict, *, drop: tuple[str, ...] = ()) -> dict:
        """Меняет ТОЛЬКО названные пометки, остальные оставляет как есть.

        Отличие от set_meta принципиальное. Обход сайтов читает контакт, потом
        несколько минут ходит по чужому серверу, а в конце пишет весь meta
        целиком — тот, что прочитал в начале. За эти минуты владелец успевает
        нажать «С чего начать» или «Перезвонить», и его правка стирается
        снимком минутной давности. Здесь перечитываем свежую запись и кладём
        поверх неё только своё.

        Возвращает получившиеся пометки — чтобы вызывающий не работал дальше
        с устаревшим словарём.
        """
        row = self.conn.execute("SELECT meta FROM contacts WHERE pk = ?", (pk,)).fetchone()
        fresh = _load(row["meta"], {}) if row else {}
        if not isinstance(fresh, dict):
            fresh = {}
        for key in drop:
            fresh.pop(key, None)
        fresh.update(changes)
        self.conn.execute(
            "UPDATE contacts SET meta = ?, updated_at = ? WHERE pk = ?",
            (_dump(fresh), now_msk(), pk),
        )
        self.conn.commit()
        return fresh

    def set_site(self, pk: int, site: str | None) -> None:
        """Меняет сайт, в том числе на пустой.

        Пустой нужен, когда сайт оказался чужим: upsert его не затрёт
        (`contact.site or existing.site`), и в карточке остался бы адрес
        другой компании.
        """
        self.conn.execute(
            "UPDATE contacts SET site = ?, updated_at = ? WHERE pk = ?",
            (site, now_msk(), pk),
        )
        self.conn.commit()

    def find_contact(self, kind: Kind, key: str) -> Contact | None:
        row = self.conn.execute(
            "SELECT * FROM contacts WHERE kind = ? AND key = ?", (kind.value, key)
        ).fetchone()
        return _to_contact(row) if row else None

    def get_contact(self, pk: int) -> Contact | None:
        row = self.conn.execute("SELECT * FROM contacts WHERE pk = ?", (pk,)).fetchone()
        return _to_contact(row) if row else None

    def contacts(
        self,
        kind: Kind,
        stage: Stage | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
        only_reachable: bool | None = None,
    ) -> list[Contact]:
        """Контакты вкладки. only_reachable=False — те, кому писать некуда (обзвон)."""
        sql = "SELECT * FROM contacts WHERE kind = ?"
        params: list = [kind.value]
        if stage is not None:
            sql += " AND stage = ?"
            params.append(stage.value)
        sql += " ORDER BY pk"
        rows = self.conn.execute(sql, params).fetchall()
        found = [_to_contact(row) for row in rows]
        if only_reachable is not None:
            found = [c for c in found if c.reachable is only_reachable]
        return found[offset : offset + limit]

    def count(
        self,
        kind: Kind,
        stage: Stage | None = None,
        *,
        only_reachable: bool | None = None,
    ) -> int:
        if only_reachable is None:
            sql = "SELECT count(*) FROM contacts WHERE kind = ?"
            params: list = [kind.value]
            if stage is not None:
                sql += " AND stage = ?"
                params.append(stage.value)
            return int(self.conn.execute(sql, params).fetchone()[0])
        # «Куда писать» считается по содержимому контактов, поэтому в SQL это
        # не выразить — берём весь срез и фильтруем в памяти. Объёмы тут
        # тысячи строк, не миллионы.
        return len(
            self.contacts(kind, stage, limit=10**9, only_reachable=only_reachable)
        )

    def stage_counts(self, kind: Kind) -> dict[Stage, int]:
        rows = self.conn.execute(
            "SELECT stage, count(*) AS n FROM contacts WHERE kind = ? GROUP BY stage",
            (kind.value,),
        ).fetchall()
        counts = {stage: 0 for stage in Stage}
        for row in rows:
            try:
                counts[Stage(row["stage"])] = int(row["n"])
            except ValueError:
                logger.warning("Незнакомая стадия в базе: {}", row["stage"])
        return counts

    # Что пишем в письмо, снятое вместе с закрытием контакта.
    CLOSED_ERROR = "контакт закрыт — письмо снято с отправки"

    def set_stage(self, pk: int, stage: Stage, note: str | None = None) -> None:
        self.conn.execute(
            "UPDATE contacts SET stage = ?, updated_at = ? WHERE pk = ?",
            (stage.value, now_msk(), pk),
        )
        self.conn.commit()
        self.log_event(pk, "stage", note or stage.value)

        if stage is Stage.DEAD:
            # Закрытый контакт не может получить письмо: `can_send` его не
            # пропустит. Но письмо оставалось лежать в очереди — попадало в
            # счётчик «Готовы к отправке», в цифру на кнопке «Отправить все» и
            # каждый заход отправщика получало отметку об отказе. Кнопка
            # обещала работу, которой нет, а владелец видел в очереди компанию,
            # которую сам же закрыл. Снимаем письмо тем же способом, что и
            # безнадёжное: строкой в базе оно остаётся, из очереди уходит.
            # Только почту. Телеграм-черновик — это рабочий инструмент
            # владельца: бот туда написать не может, текст копируется руками, а
            # кнопка «Отправил» записывает уже случившийся факт. Снять такой
            # черновик значит потерять запись об отправке, которая произошла,
            # — и вместе с ней суточный счётчик касаний.
            live = self.conn.execute(
                "SELECT pk FROM drafts WHERE contact_pk = ? AND status = 'draft' AND channel = ?",
                (pk, Channel.EMAIL.value),
            ).fetchone()
            if live:
                self.conn.execute(
                    "UPDATE drafts SET status = 'failed', error = ?, approved_at = NULL "
                    "WHERE pk = ?",
                    (self.CLOSED_ERROR, int(live["pk"])),
                )
                self.conn.commit()

    def written_siblings(
        self, kind: Kind, name: str, skip_pk: int | None = None, site: str | None = None
    ) -> list[Contact]:
        """Однофамильцы, которым уже написали или собираются.

        Сеть вроде «Медси» стоит в базе тринадцатью карточками филиалов: у
        каждой свой адрес и свой телефон, но почта и владелец общие. Без этой
        проверки сеть получила бы тринадцать писем подряд, и это выглядело бы
        ровно как спам, которым по сути и было бы.

        Совпадения имени мало. В карточках 2ГИС до запятой стоит не только имя
        сети, но иногда и город: две несвязанные клиники подписаны «Москва,
        стоматологическая клиника», и по одному имени вторая была бы закрыта
        как филиал первой. Поэтому разные сайты разводят компании обратно:
        у настоящей сети домен один на все филиалы.

        Одной проверки по сайту мало: на данных 2ГИС сайта нет у большинства
        карточек (в живой базе — у пятидесяти двух клиентов из семидесяти), и
        развести компании ей просто нечем. Поэтому отдельно отсекаем случай,
        ради которого проверка и заводилась: имя, совпавшее с городом, сетью
        не считаем вовсе. «Москва» — это не сеть, и закрывать по ней живую
        компанию как чужой филиал нельзя.
        """
        network = network_name(name)
        if not network:
            return []
        if network in _city_names(self, kind):
            logger.info("«{}»: имя совпадает с городом — сетью не считаю", name)
            return []
        own = _site_domain(site)
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE kind = ? AND stage IN (?, ?, ?, ?, ?)",
            (kind.value, Stage.QUEUED.value, Stage.WRITTEN.value, Stage.REPLIED.value,
             Stage.CALL.value, Stage.DEAL.value),
        ).fetchall()
        found = []
        for row in rows:
            if skip_pk is not None and int(row["pk"]) == skip_pk:
                continue
            if network_name(row["name"]) != network:
                continue
            other = _site_domain(row["site"])
            if own and other and own != other:
                continue  # разные сайты — разные компании, просто имена похожи
            found.append(_to_contact(row))
        return found

    def set_note(self, pk: int, note: str) -> None:
        self.conn.execute(
            "UPDATE contacts SET note = ?, updated_at = ? WHERE pk = ?", (note, now_msk(), pk)
        )
        self.conn.commit()

    # --- Черновики --------------------------------------------------------

    def save_draft(self, draft: Draft) -> int:
        """Кладёт готовый текст в очередь, заменяя прежний черновик контакта."""
        self.conn.execute(
            "DELETE FROM drafts WHERE contact_pk = ? AND status = 'draft'", (draft.contact_pk,)
        )
        cursor = self.conn.execute(
            """INSERT INTO drafts (contact_pk, channel, subject, body, status, created_at)
               VALUES (?, ?, ?, ?, 'draft', ?)""",
            (draft.contact_pk, draft.channel.value, draft.subject, draft.body, now_msk()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_draft(self, pk: int) -> Draft | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE pk = ?", (pk,)).fetchone()
        return _to_draft(row) if row else None

    def draft_for(self, contact_pk: int) -> Draft | None:
        row = self.conn.execute(
            "SELECT * FROM drafts WHERE contact_pk = ? AND status = 'draft'", (contact_pk,)
        ).fetchone()
        return _to_draft(row) if row else None

    def sent_letters(self, contact_pk: int, *, limit: int = 3) -> list[str]:
        """Тексты писем, которые мы уже отправили этому контакту.

        Нужны цепочке касаний: модель не помнит, что писала неделю назад, и
        без этих текстов на третьем письме уверенно повторяет довод из первого.

        Отдаём ПОСЛЕДНИЕ письма, а не первые, и в порядке отправки. Раньше
        выборка шла от самого старого и обрезалась лимитом: с четвёртого
        письма список замирал на первых трёх. Разбор ответа берёт из него
        последний элемент как «наше последнее письмо» — и модель разбирала
        ответ клиента, глядя на текст, отправленный три касания назад.
        """
        rows = self.conn.execute(
            """SELECT body FROM drafts
               WHERE contact_pk = ? AND status = 'sent'
               ORDER BY sent_at DESC, pk DESC LIMIT ?""",
            (contact_pk, limit),
        ).fetchall()
        return [row["body"] for row in reversed(rows)]

    def queue(
        self,
        kind: Kind,
        *,
        limit: int = 20,
        offset: int = 0,
        channel: Channel | None = None,
    ) -> list[tuple[Draft, Contact]]:
        """Очередь на отправку: тексты готовы, ждут кнопки или автоотправки.

        channel отбирает в самом запросе, а не после. Разница видна, когда в
        голове очереди скопились сообщения в телеграм: их бот отправить не
        может, и при отборе «взять пачку и отфильтровать» почта не доходила
        до отправки вовсе — окно целиком уходило на телеграм.
        """
        where = "d.status = 'draft' AND c.kind = ?"
        params: list = [kind.value]
        if channel is not None:
            where += " AND d.channel = ?"
            params.append(channel.value)
        rows = self.conn.execute(
            f"""SELECT d.*, c.pk AS c_pk FROM drafts d
                JOIN contacts c ON c.pk = d.contact_pk
                WHERE {where}
                ORDER BY d.pk LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            contact = self.get_contact(int(row["c_pk"]))
            if contact:
                result.append((_to_draft(row), contact))
        return result

    def queue_size(
        self, kind: Kind, channel: Channel | None = None, *, waiting_only: bool = False
    ) -> int:
        """Сколько писем ждёт отправки. channel — считать только этот канал.

        Разделение нужно кнопке «Отправить все»: фоновый отправщик умеет
        только почту, и общий счётчик обещал больше, чем уходило.

        waiting_only — считать ТОЛЬКО те, которые нажатие сдвинет с места.
        Одобренное письмо остаётся в статусе 'draft', пока его не заберёт
        отправщик, и попадало в общий счёт: кнопка обещала «Отправить все (7)»
        при семи уже стоящих в отправке, нажатие не делало ничего, а всплывашка
        отвечала «Отправлять нечего». Цифра на кнопке должна совпадать с тем,
        что произойдёт по нажатию.
        """
        sql = """SELECT count(*) FROM drafts d JOIN contacts c ON c.pk = d.contact_pk
                 WHERE d.status = 'draft' AND c.kind = ?"""
        params: list = [kind.value]
        if channel is not None:
            sql += " AND d.channel = ?"
            params.append(channel.value)
        if waiting_only:
            sql += " AND d.approved_at IS NULL"
        return int(self.conn.execute(sql, params).fetchone()[0])

    def claim_draft(self, pk: int) -> bool:
        """Занимает письмо на отправку. False — его уже взял кто-то другой.

        Отправляют двое: фоновый отправщик и владелец кнопкой в боте. Между
        проверками и `mark_sent` окно в несколько секунд — там живут запрос к
        DNS и разговор с почтовым сервером. Оба успевали войти в это окно и
        отправить одно письмо дважды. Захват атомарный: строку меняет тот, кто
        успел первым, второму UPDATE не находит ничего.
        """
        cursor = self.conn.execute(
            "UPDATE drafts SET status = 'sending' WHERE pk = ? AND status = 'draft'", (pk,)
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def hand_to_smtp(self, pk: int) -> None:
        """Отмечает, что письмо сейчас уйдёт: дальше — разговор с почтой.

        Отметка ставится ДО отправки и намеренно: после неё мы уже не знаем
        исхода. Умри процесс на середине разговора — без этой записи письмо
        вернулось бы в очередь и ушло получателю второй раз.
        """
        self.conn.execute("UPDATE drafts SET handed_at = ? WHERE pk = ?", (now_msk(), pk))
        self.conn.commit()

    def release_draft(self, pk: int) -> None:
        """Возвращает занятое письмо в очередь: отправка не состоялась.

        Пока письмо было занято, владелец мог нажать «Переписать» — тогда у
        контакта уже лежит новый черновик. Возвращать поверх него старый нельзя
        (на живой черновик стоит уникальный индекс — один на контакт), да и не
        нужно: новый текст свежее. Такое письмо закрываем как устаревшее.
        """
        row = self.conn.execute(
            "SELECT contact_pk FROM drafts WHERE pk = ? AND status = 'sending'", (pk,)
        ).fetchone()
        if row is None:
            return
        # Отправка не состоялась и мы это ЗНАЕМ — отметку о передаче снимаем,
        # иначе письмо навсегда осталось бы «с неизвестной судьбой».
        #
        # «Знаем» держится на одном условии, и его надо беречь: отправка
        # поднимает исключение только ДО того, как почтовый сервер принял
        # письмо (см. outreach._hand_over). Всё, что случается после приёма —
        # оборванное прощание, разрыв связи, — наружу не выходит. Стоит этому
        # правилу сломаться, и сюда придёт уже отправленное письмо: мы снимем
        # захват, вернём его в очередь, и получатель увидит его второй раз.
        self.conn.execute("UPDATE drafts SET handed_at = NULL WHERE pk = ?", (pk,))

        busy = self.conn.execute(
            "SELECT 1 FROM drafts WHERE contact_pk = ? AND status = 'draft' AND pk <> ?",
            (row["contact_pk"], pk),
        ).fetchone()
        if busy:
            self.conn.execute(
                "UPDATE drafts SET status = 'failed', error = ? WHERE pk = ?",
                ("письмо переписали, пока это уходило", pk),
            )
            logger.info("Черновик {} устарел: за время отправки его переписали", pk)
        else:
            self.conn.execute("UPDATE drafts SET status = 'draft' WHERE pk = ?", (pk,))
        self.conn.commit()

    def release_stale_sending(self) -> tuple[int, list[str]]:
        """Возвращает в очередь письма, занятые упавшим процессом.

        Захват живёт только внутри одной отправки. Если процесс умер посреди
        неё, письмо осталось бы «отправляемым» навсегда и не ушло бы никогда —
        поэтому при старте отправщика все такие возвращаются в очередь.

        Отдаёт (сколько вернули, имена тех, чья судьба неизвестна). Второе
        нужно вызывающему, чтобы сказать владельцу: соседний комментарий
        обещает, что судьбу таких писем решит он, а до сих пор об этом никто
        ему не говорил — письма молча уходили в «неудачные».
        """
        rows = self.conn.execute(
            "SELECT pk, handed_at FROM drafts WHERE status = 'sending'"
        ).fetchall()

        # Письмо, которое успели отдать почтовому серверу, в очередь не
        # возвращаем. Ответа от сервера мы не дождались, но письмо могло уйти —
        # и вторая отправка того же холодного письма тому же человеку хуже, чем
        # неотправленная: это уже не рассылка, а навязчивость. Решает владелец.
        unclear = [int(row["pk"]) for row in rows if row["handed_at"]]
        unclear_names: list[str] = []
        for pk in unclear:
            name = self.conn.execute(
                """SELECT c.name FROM drafts d JOIN contacts c ON c.pk = d.contact_pk
                   WHERE d.pk = ?""",
                (pk,),
            ).fetchone()
            if name and name["name"]:
                unclear_names.append(str(name["name"]))
            self.conn.execute(
                "UPDATE drafts SET status = 'failed', error = ? WHERE pk = ?",
                (self.UNCLEAR_ERROR, pk),
            )
        if unclear:
            self.conn.commit()
            logger.warning(
                "Писем с неизвестной судьбой после перезапуска: {} — второй раз не отправляю",
                len(unclear),
            )

        stuck = [int(row["pk"]) for row in rows if not row["handed_at"]]
        for pk in stuck:
            # По одному и через ту же дверь: среди застрявших может оказаться
            # письмо, которое успели переписать, и массовый UPDATE на нём падал бы.
            self.release_draft(pk)
        if stuck:
            logger.info("Вернул в очередь писем после перезапуска: {}", len(stuck))
        return len(stuck), unclear_names

    def approve_draft(self, pk: int) -> bool:
        """Ставит письмо в очередь на отправку с паузами."""
        cursor = self.conn.execute(
            "UPDATE drafts SET approved_at = ? WHERE pk = ? AND status = 'draft'",
            (now_msk(), pk),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def unapprove_draft(self, pk: int) -> None:
        """Снимает разрешение на отправку: письмо возвращается в очередь.

        Нужно после неудачи. Иначе отправщик каждые полминуты пытался бы
        отправить одно и то же письмо и забил бы лог одинаковыми ошибками.
        """
        self.conn.execute("UPDATE drafts SET approved_at = NULL WHERE pk = ?", (pk,))
        self.conn.commit()

    def approved_drafts(
        self, kind: Kind, limit: int = 100, channel: Channel | None = None
    ) -> list[tuple[Draft, Contact]]:
        """Письма, которые владелец разрешил отправить, в порядке одобрения.

        channel — брать только этот канал. Фоновой отправке нужна именно
        почта: сообщение в телеграм бот отправить не может (Telegram не даёт
        писать первым), такой черновик остаётся одобренным навсегда и, стоя
        первым в очереди, останавливал всю рассылку.
        """
        where = "d.status = 'draft' AND d.approved_at IS NOT NULL AND c.kind = ?"
        params: list = [kind.value]
        if channel is not None:
            where += " AND d.channel = ?"
            params.append(channel.value)
        params.append(limit)

        rows = self.conn.execute(
            f"""SELECT d.*, c.pk AS c_pk FROM drafts d JOIN contacts c ON c.pk = d.contact_pk
               WHERE {where}
               ORDER BY d.approved_at, d.pk LIMIT ?""",
            params,
        ).fetchall()
        result = []
        for row in rows:
            contact = self.get_contact(int(row["c_pk"]))
            if contact:
                result.append((_to_draft(row), contact))
        return result

    def approved_count(self, kind: Kind) -> int:
        return len(self.approved_drafts(kind, limit=10**6))

    def drop_draft(self, pk: int) -> bool:
        """Убирает готовое письмо из очереди и возвращает контакт в «новые»."""
        row = self.conn.execute(
            "SELECT contact_pk FROM drafts WHERE pk = ? AND status = 'draft'", (pk,)
        ).fetchone()
        cursor = self.conn.execute("DELETE FROM drafts WHERE pk = ? AND status = 'draft'", (pk,))
        self.conn.commit()
        if cursor.rowcount and row:
            self._back_to_new(int(row["contact_pk"]))
        return cursor.rowcount > 0

    def forget_draft(self, pk: int) -> bool:
        """Сносит письмо из очереди, НЕ трогая стадию контакта.

        Отличие от drop_draft: тот возвращает контакт в «новые», потому что
        владелец отказался от письма ещё до первого касания. Здесь случай
        другой — письмо уже уходило, а сносим мы протухшее касание вдогонку.
        Вернуть такой контакт в «новые» значило бы написать ему холодное
        первое письмо второй раз, как будто мы про него забыли.
        """
        cursor = self.conn.execute("DELETE FROM drafts WHERE pk = ? AND status = 'draft'", (pk,))
        self.conn.commit()
        return cursor.rowcount > 0

    def clear_queue(self, kind: Kind) -> int:
        """Сносит всю очередь вкладки и возвращает контакты в «новые»."""
        rows = self.conn.execute(
            """SELECT contact_pk FROM drafts WHERE status = 'draft' AND contact_pk IN
               (SELECT pk FROM contacts WHERE kind = ?)""",
            (kind.value,),
        ).fetchall()
        cursor = self.conn.execute(
            """DELETE FROM drafts WHERE status = 'draft' AND contact_pk IN
               (SELECT pk FROM contacts WHERE kind = ?)""",
            (kind.value,),
        )
        self.conn.commit()
        for row in rows:
            self._back_to_new(int(row["contact_pk"]))
        return cursor.rowcount

    def _back_to_new(self, contact_pk: int) -> None:
        """Возвращает контакт в «новые», если готовых писем у него не осталось.

        Стадия «Готовы к отправке» ставится вместе с письмом. Убрали письмо —
        а стадия оставалась, и контакт застревал в ней навсегда: в очереди его
        нет (письма-то нет), а пачка берёт только «новых» и мимо него проходит.
        Компания молча выпадала из рассылки, и заметить это было нечем.
        """
        left = self.conn.execute(
            "SELECT 1 FROM drafts WHERE contact_pk = ? AND status = 'draft'", (contact_pk,)
        ).fetchone()
        if left:
            return
        current = self.get_contact(contact_pk)
        if current is not None and current.stage is Stage.QUEUED:
            self.set_stage(contact_pk, Stage.NEW, "письмо убрано из очереди")

    def mark_sent(self, draft_pk: int) -> None:
        self.conn.execute(
            "UPDATE drafts SET status = 'sent', sent_at = ? WHERE pk = ?", (now_msk(), draft_pk)
        )
        self.conn.commit()

    def mark_failed(self, draft_pk: int, error: str, *, counts: bool = True) -> int:
        """Не отправилось — черновик остаётся в очереди, но с пометкой.

        Статус не меняем: письмо всё ещё должно уйти, когда причина уйдёт
        (почта не настроена, сеть отвалилась). Иначе контакт молча потеряется.

        Считаем попытки и возвращаем их число. Без счётчика мёртвый адрес
        превращался в бесконечный поток подключений к почтовому серверу:
        письмо снова одобрялось, снова падало, и так по кругу — а за перебор
        авторизаций хостер блокирует ящик целиком.

        counts=False — записать причину, но попытку не засчитывать. Счётчик
        меряет ровно одно: сколько раз почтовый сервер отказался принять
        письмо. Отказ до отправки (лимит исчерпан, DNS не ответил, почта не
        настроена) попыткой доставки не был, и три таких подряд не должны
        снимать письмо с очереди навсегда.
        """
        cursor = self.conn.execute(
            f"UPDATE drafts SET error = ?{', attempts = attempts + 1' if counts else ''}"
            " WHERE pk = ?",
            (error[:500], draft_pk),
        )
        self.conn.commit()
        if not cursor.rowcount:
            return 0
        row = self.conn.execute("SELECT attempts FROM drafts WHERE pk = ?", (draft_pk,)).fetchone()
        return int(row["attempts"]) if row else 0

    # Как помечается письмо, о судьбе которого мы не знаем. Строка одна на
    # запись и на поиск: разъедутся — счётчик на экране молча обнулится.
    UNCLEAR_ERROR = "отправка прервалась — неизвестно, ушло ли письмо"

    def mark_unclear(self, draft_pk: int) -> None:
        """Судьба письма неизвестна: связь оборвалась после отправки текста.

        В очередь такое письмо не возвращается — сервер мог его принять, и
        повтор дал бы получателю вторую копию. Помечаем той же строкой, что и
        письма, брошенные упавшим процессом: и счётчик в «Требует вас», и
        объяснение владельцу у них одни и те же.
        """
        row = self.conn.execute(
            "SELECT contact_pk FROM drafts WHERE pk = ?", (draft_pk,)
        ).fetchone()
        self.conn.execute(
            "UPDATE drafts SET status = 'failed', error = ?, approved_at = NULL WHERE pk = ?",
            (self.UNCLEAR_ERROR, draft_pk),
        )
        self.conn.commit()
        if row:
            # Контакт возвращаем в «новые» — то же правило, что у drop_draft и
            # give_up_draft. Без этого компания застревала в «Готовы к
            # отправке» без единого письма: в очереди её нет, а пачка берёт
            # только «новых» — она выпадала из работы совсем и молча.
            self._back_to_new(int(row["contact_pk"]))

    def unclear_drafts(self) -> int:
        """Сколько писем осталось с неизвестной судьбой.

        Сообщение о них уходит один раз, при перезапуске отправщика. Если в
        эту минуту Телеграм не отвечал, факт пропадал совсем: письмо помечено
        неудачным, из очереди исчезло, а на экранах его нет нигде. Поэтому
        цифра живёт в базе и попадает в блок «Требует вас».
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE status = 'failed' AND error = ?",
            (self.UNCLEAR_ERROR,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def give_up_draft(self, draft_pk: int, error: str) -> None:
        """Больше не пытаемся: письмо уходит из очереди с пометкой почему.

        И контакт возвращается в «новые», если готовых писем у него больше не
        осталось, — то же правило, что у `drop_draft`. Без него компания
        застревала в стадии «Готовы к отправке» без единого письма: в очереди
        её нет (письма-то нет), а пачка берёт только «новых» и проходит мимо.
        Выпадение молчаливое, и заметить его нечем.

        Стадию после этого может переставить вызывающий: адрес, забракованный
        навсегда, закрывает контакт целиком — но решает это он, а не мы.
        """
        row = self.conn.execute(
            "SELECT contact_pk FROM drafts WHERE pk = ?", (draft_pk,)
        ).fetchone()
        self.conn.execute(
            "UPDATE drafts SET status = 'failed', error = ?, approved_at = NULL WHERE pk = ?",
            (error[:500], draft_pk),
        )
        self.conn.commit()
        if row:
            self._back_to_new(int(row["contact_pk"]))

    # --- Суточные лимиты --------------------------------------------------

    def contact_by_email(self, address: str) -> Contact | None:
        """Кому принадлежит адрес. Нужно, чтобы узнать автора ответа на письмо.

        Адреса лежат в JSON-списке, поэтому ищем подстрокой, а совпадение
        проверяем уже по разобранному списку: LIKE поймает и «info@firma.ru»
        внутри «old-info@firma.ru».
        """
        address = (address or "").strip().lower()
        if not address:
            return None
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE lower(emails) LIKE ?", (f"%{address}%",)
        ).fetchall()
        for row in rows:
            contact = _to_contact(row)
            if any(known.strip().lower() == address for known in contact.emails):
                return contact
        return None

    def search_contacts(self, query: str, *, limit: int = 10) -> list[Contact]:
        """Ищет контакты по чему угодно: адресу, телефону, сайту, названию.

        Нужно для случая «скинул почту в чат — покажи, кто это». Телефон
        сравниваем по цифрам: в базе он лежит как +74951234567, а владелец
        пришлёт его как 8 (495) 123-45-67.
        """
        text = (query or "").strip().lower()
        if len(text) < 3:
            return []

        # Фильтруем в Python, а не в SQL. Причина: lower() в SQLite умеет
        # только латиницу, поэтому «Медиа» не нашло бы «Медиасферу» — для
        # русской базы это половина запросов. Контактов тут тысячи, а не
        # миллионы, так что перебор дешевле, чем поиск обходных путей.
        digits = re.sub(r"\D", "", text)
        phone_tail = digits[-10:] if len(digits) >= 7 else ""

        found: list[Contact] = []
        for row in self.conn.execute("SELECT * FROM contacts ORDER BY updated_at DESC"):
            contact = _to_contact(row)
            haystack = " ".join(
                [contact.name, contact.key, contact.site or "", *contact.emails]
            ).casefold()
            by_phone = phone_tail and any(
                phone_tail in re.sub(r"\D", "", phone) for phone in contact.phones
            )
            if text.casefold() in haystack or by_phone:
                found.append(contact)
            if len(found) >= limit:
                break
        return found

    def contact_by_domain(self, address: str) -> Contact | None:
        """Контакт по домену адреса — когда точного совпадения нет.

        Отвечают не всегда с того ящика, куда мы писали: письмо ушло на
        info@agency.ru, а ответил директор с d.petrov@agency.ru. Для нас это
        та же компания, и терять такой ответ нельзя.

        Берём того, кому недавно писали: если на домене несколько контактов,
        разговор идёт с тем, кто получил письмо.

        По бесплатным доменам не сопоставляем вовсе. gmail.com — не компания,
        а полмира: письмо с любого личного ящика находило бы «ту же компанию»
        наугад, и разбор ответа двигал бы стадию случайному контакту, а то и
        закрывал его отказом.
        """
        domain = (address or "").strip().lower().rsplit("@", 1)[-1]
        if not domain or "." not in domain:
            return None
        if domain in FREE_MAIL_DOMAINS:
            return None
        # Порядок — по времени ОТПРАВКИ письма, а не по updated_at. Последний
        # трогает что угодно: импорт выгрузки, обход сайтов, правка стадии,
        # даже пометка о возврате. На домене с двумя контактами наверх
        # всплывал тот, чью карточку последней тронул фон, — и ответ директора
        # уходил соседнему филиалу, двигая ЕГО стадию и его переписку.
        #
        # Событие «sent» пишет `_after_send`, и другого источника у него нет.
        rows = self.conn.execute(
            """SELECT c.*, (SELECT MAX(e.at) FROM events e
                            WHERE e.contact_pk = c.pk AND e.kind = 'sent') AS wrote_at
               FROM contacts c WHERE lower(c.emails) LIKE ?
               ORDER BY wrote_at IS NULL, wrote_at DESC, c.updated_at DESC""",
            (f"%@{domain}%",),
        ).fetchall()
        for row in rows:
            contact = _to_contact(row)
            if any(known.strip().lower().endswith("@" + domain) for known in contact.emails):
                return contact
        return None

    def close_same_email(self, address: str, *, skip_pk: int | None = None, note: str = "") -> int:
        """Убирает мёртвый адрес у всех, у кого он есть. Возвращает, скольких закрыл.

        Один адрес часто достаётся нескольким контактам: у филиалов сети на
        сайте одна общая почта. Если письмо оттуда вернулось, мёртвым адрес
        стал для всех сразу — но мёртвым стал именно АДРЕС, а не компания.
        Поэтому адрес вычёркиваем у всех, а закрываем только тех, у кого
        другого пути не осталось: второй ящик или телеграм — это живой канал.

        Контакты, с которыми уже идёт разговор, не закрываем вовсе: там
        возврат по общему ящику ничего не говорит о самом разговоре.
        """
        address = (address or "").strip().lower()
        if not address:
            return 0
        closed = 0
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE lower(emails) LIKE ?", (f"%{address}%",)
        ).fetchall()
        for row in rows:
            contact = _to_contact(row)
            if contact.pk == skip_pk or contact.stage is Stage.DEAD:
                continue
            if not any(known.strip().lower() == address for known in contact.emails):
                continue

            left = [a for a in contact.emails if a.strip().lower() != address]
            # Через set_emails, а не своим UPDATE: только он запоминает
            # вычеркнутый адрес. Свой UPDATE выглядел одинаково, но проходил
            # мимо этой памяти — и следующий импорт выгрузки возвращал мёртвый
            # ящик всем соседям по сети разом. Письмо уходило на него второй
            # раз, добавляя нам ещё один возврат в репутацию домена.
            self.set_emails(contact.pk or 0, left)
            self.log_event(contact.pk or 0, "bounce", f"адрес не принимает письма: {address}")

            contact.emails = left
            if contact.reachable:
                if not contact.best_email:
                    # Почты не осталось, но телеграм жив. Готовое ПИСЬМО при
                    # этом отправлять некуда: адрес, на который оно собрано,
                    # только что вычеркнут. Снимаем его — иначе компания
                    # висит в «Готовы к отправке» с письмом в никуда, и пачка
                    # не возьмёт её, чтобы подготовить телеграм.
                    live = self.conn.execute(
                        """SELECT pk FROM drafts
                           WHERE contact_pk = ? AND status = 'draft' AND channel = ?""",
                        (contact.pk, Channel.EMAIL.value),
                    ).fetchone()
                    if live:
                        self.give_up_draft(
                            int(live["pk"]), "адрес вычеркнут — письмо отправлять некуда"
                        )
                continue  # остался второй ящик или телеграм — компания достижима
            if contact.stage not in (Stage.NEW, Stage.QUEUED, Stage.WRITTEN):
                continue  # разговор уже идёт — решает владелец
            self.set_stage(contact.pk or 0, Stage.DEAD, note or "адрес не принимает письма")
            closed += 1
        return closed

    def last_sent_at(self, contact_pk: int) -> str | None:
        """Когда мы последний раз что-то отправили этому контакту."""
        row = self.conn.execute(
            """SELECT sent_at FROM drafts
               WHERE contact_pk = ? AND status = 'sent' AND sent_at IS NOT NULL
               ORDER BY sent_at DESC LIMIT 1""",
            (contact_pk,),
        ).fetchone()
        return row["sent_at"] if row else None

    def sent_today(self, kind: Kind) -> int:
        """Сколько касаний было сегодня по этой вкладке — всеми каналами."""
        row = self.conn.execute(
            "SELECT count FROM sends WHERE day = ? AND kind = ?", (today_msk(), kind.value)
        ).fetchone()
        return int(row["count"]) if row else 0

    def mail_days(self) -> list[str]:
        """Дни, в которые из ящика реально уходили письма. По возрастанию.

        Прогрев считается по НИМ, а не по календарю: ящик, отправивший восемь
        писем в один день и замолчавший на три недели, прогретым не стал.
        Календарный отсчёт объявлял его прогретым на пятый день — и первый же
        настоящий заход шёл с потолка в двести писем с холодного ящика.
        """
        rows = self.conn.execute(
            "SELECT day FROM sends WHERE by_mail > 0 GROUP BY day ORDER BY day"
        ).fetchall()
        return [str(row["day"]) for row in rows]

    def mailed_today(self, kind: Kind | None = None) -> int:
        """Сколько писем ушло сегодня ИЗ ЯЩИКА. Без kind — по обеим вкладкам.

        Прогрев ограничивает ящик, а не компанию. Ручная отправка в телеграм
        уходит с личного аккаунта владельца, нагрузкой на ящик не была, а
        прогревную квоту съедала: в первый день три сообщения в телеграм
        закрывали ящику всю норму, писем не уходило ни одного, а отсчёт
        прогрева стартует от первого УШЕДШЕГО письма — то есть не стартовал
        вовсе. Прогрев не кончался никогда.
        """
        sql = "SELECT COALESCE(SUM(by_mail), 0) AS n FROM sends WHERE day = ?"
        params: list = [today_msk()]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def add_send(self, kind: Kind, *, by_mail: bool = True) -> None:
        """Записывает отправку. by_mail — ушла ли она из нашего ящика."""
        self.conn.execute(
            """INSERT INTO sends (day, kind, count, by_mail) VALUES (?, ?, 1, ?)
               ON CONFLICT(day, kind) DO UPDATE SET
                   count = count + 1, by_mail = by_mail + ?""",
            (today_msk(), kind.value, int(by_mail), int(by_mail)),
        )
        self.conn.commit()

    # --- Журнал -----------------------------------------------------------

    def log_event(self, contact_pk: int | None, kind: str, text: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (contact_pk, kind, text, at) VALUES (?, ?, ?, ?)",
            (contact_pk, kind, text[:1000], now_msk()),
        )
        self.conn.commit()

    def events(self, contact_pk: int, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE contact_pk = ? ORDER BY pk DESC LIMIT ?",
            (contact_pk, limit),
        ).fetchall()

    # --- Настройки, меняемые из бота --------------------------------------

    def quiet_notes(self, day: str | None = None) -> list[str]:
        """Строки, отложенные до вечерней сводки. Порядок — как случилось.

        Сюда попадает всё, о чём решено не дёргать владельца в ту же секунду:
        ушедшие письма, снятые адреса, сделанный бэкап. Событие не пропадает,
        а ждёт вечера — молчание здесь про время доставки, а не про сокрытие.
        """
        from .alerts import QUIET_KIND

        when = day or today_msk()
        rows = self.conn.execute(
            "SELECT text FROM events WHERE kind = ? AND at LIKE ? ORDER BY pk",
            (QUIET_KIND, f"{when}%"),
        ).fetchall()
        return [row["text"] for row in rows if row["text"]]

    def event_counts(self, day: str | None = None) -> dict[str, int]:
        """Сколько чего случилось за день: письма, ответы, звонки, возвраты.

        Нужно для вечерней сводки: стадии показывают, где контакт сейчас, а
        сводке важно, что произошло именно сегодня.
        """
        when = day or today_msk()
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM events WHERE at LIKE ? GROUP BY kind",
            (f"{when}%",),
        ).fetchall()
        return {row["kind"]: int(row["n"]) for row in rows}

    def day_event_contacts(self, kind: str, day: str | None = None) -> list[tuple[str, str]]:
        """Кто попал в события этого вида за день: (имя компании, заметка).

        Для вечерней сводки с именами: «писем ушло: 6» не говорит, кому именно,
        а владелец наутро продолжает разговор с конкретными людьми, не с цифрой.
        """
        when = day or today_msk()
        rows = self.conn.execute(
            """SELECT c.name AS name, e.text AS note FROM events e
               JOIN contacts c ON c.pk = e.contact_pk
               WHERE e.kind = ? AND e.at LIKE ?
               ORDER BY e.pk""",
            (kind, f"{when}%"),
        ).fetchall()
        seen: dict[str, str] = {}
        for row in rows:
            seen.setdefault(row["name"], row["note"] or "")
        return list(seen.items())

    def waiting_contacts(self) -> list[tuple[str, str]]:
        """Кому написано письмом и кто молчит: (имя, с какого дня ждём).

        Только партнёры и клиенты. Биржевые заказы сюда не берём: их
        «молчание» отслеживает сборщик бирж со своими правилами, и в сводке
        они топили собой агентства, ради которых список и заведён.

        Сортировка от самых давних: если напоминать, то сначала тем, у кого
        письмо лежит дольше всех.
        """
        # Считаем от даты ПИСЬМА, а не от updated_at. Последнее трогает любая
        # правка карточки: импорт выгрузки, запись проверки адреса, заметка.
        # После нажатия «Загрузить из парсера» все контакты «обновлялись»
        # сегодняшним числом, и список молчунов опустевал целиком — ровно в
        # тот момент, когда по нему и надо работать. Ту же ошибку уже ловили
        # в карточке контакта, см. _since_line в sections/base.py.
        #
        # Сегодняшних не считаем молчунами: письмо ушло час назад, человек
        # мог его ещё не открыть. В списке «Молчат» такая строка сбивает —
        # владелец видит имя и думает, что пора напоминать.
        rows = self.conn.execute(
            """SELECT c.name AS name, substr(MAX(e.at), 1, 10) AS since
               FROM contacts c
               JOIN events e ON e.contact_pk = c.pk AND e.kind = 'sent'
               WHERE c.stage = ? AND c.kind IN (?, ?)
               GROUP BY c.pk
               HAVING since < ?
               ORDER BY since""",
            (Stage.WRITTEN.value, Kind.PARTNER.value, Kind.CLIENT.value, today_msk()),
        ).fetchall()
        return [(row["name"], row["since"]) for row in rows]

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        self.conn.commit()

    def delete_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM state WHERE key = ?", (key,))
        self.conn.commit()

    def due_reminders(self, day: str | None = None, limit: int = 50) -> list[Contact]:
        """Кому просили написать «позже» и этот «позже» настал.

        Срок ставится разбором ответа: человек сказал «вернитесь в сентябре» —
        значит до сентября мы молчим, а первого сентября про него напоминаем.
        Без такого списка «позже» равносильно «никогда»: контакт уходит в
        «Начали общение» и там теряется.

        Сначала свежие сроки, а не самые старые. Напоминание висит, пока
        владелец по нему не сработал (написал письмо, поставил созвон, закрыл
        контакт), поэтому просроченные накапливаются — и при обратном порядке
        десяток весенних вытеснял бы из сводки тот единственный срок, который
        наступил сегодня.
        """
        when = day or today_msk()
        # Срок может стоять и у контакта дальше по воронке: «созвонимся после
        # отпуска» ставит remind_after контакту на стадии «Созвон». Раньше
        # выборка смотрела только REPLIED, и такой срок не всплывал никогда —
        # «позже» молча превращалось в «никогда» ровно там, где сделка ближе
        # всего.
        # NEW здесь тоже: срок ставит не только разбор ответа, но и кнопка
        # «Перезвонить» в обзвоне, а там компания стоит в «новых». Без неё
        # назначенный владельцем звонок не всплывал бы нигде — то есть кнопка
        # была бы способом забыть про компанию, а не вспомнить о ней.
        stages = (Stage.NEW.value, Stage.REPLIED.value, Stage.CALL.value, Stage.DEAL.value)
        rows = self.conn.execute(
            f"""SELECT * FROM contacts
               WHERE stage IN ({",".join("?" * len(stages))})
                 AND json_extract(meta, '$.remind_after') IS NOT NULL
                 AND json_extract(meta, '$.remind_after') != ''
                 AND json_extract(meta, '$.remind_after') <= ?
               ORDER BY json_extract(meta, '$.remind_after') DESC
               LIMIT ?""",
            (*stages, when, limit),
        ).fetchall()
        return [_to_contact(row) for row in rows]

    # --- Проверка адресов -------------------------------------------------

    def email_check(self, address: str) -> sqlite3.Row | None:
        """Что мы уже выяснили про адрес. None — ещё не проверяли."""
        return self.conn.execute(
            "SELECT * FROM email_checks WHERE address = ?", ((address or "").strip().lower(),)
        ).fetchone()

    def save_email_check(self, address: str, ok: bool, reason: str, permanent: bool) -> None:
        self.conn.execute(
            """INSERT INTO email_checks (address, ok, reason, permanent, checked_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                   ok = excluded.ok, reason = excluded.reason,
                   permanent = excluded.permanent, checked_at = excluded.checked_at""",
            ((address or "").strip().lower(), int(ok), reason, int(permanent), today_msk()),
        )
        self.conn.commit()

    def bad_addresses(self, limit: int = 100) -> list[sqlite3.Row]:
        """Адреса, забракованные проверкой. Для экрана «почему не пишем»."""
        return self.conn.execute(
            "SELECT * FROM email_checks WHERE ok = 0 ORDER BY checked_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


# --- Преобразования ---------------------------------------------------------

def _site_domain(site) -> str:
    """Домен сайта без схемы и www. Пусто — сайта нет или он не разобрался."""
    from .models import _domain_of

    return _domain_of(site if isinstance(site, str) else None)


# Ключ в meta контакта: адреса, с которых вернулось письмо. Держим их именно
# в контакте, а не отдельной таблицей, чтобы они переживали любой импорт: файл
# выгрузки про возврат ничего не знает и приносит мёртвый адрес снова и снова.
DEAD_EMAILS = "dead_emails"


def _city_names(store, kind: Kind) -> set[str]:
    """Города, встречающиеся в этой вкладке, в том же виде, что и имя сети.

    Нужен один раз на вызов и стоит один запрос: список городов короткий,
    держать его кэшем значило бы разойтись с базой после импорта.
    """
    rows = store.conn.execute(
        "SELECT DISTINCT city FROM contacts WHERE kind = ? AND city != ''", (kind.value,)
    ).fetchall()
    return {network_name(row["city"]) for row in rows if row["city"]}


def network_name(raw: str) -> str:
    """Название сети из названия карточки.

    В 2ГИС филиал подписан как «Медси, сеть медицинских центров» или
    «Авторусь, магазин автотоваров»: до запятой имя сети, после — что это
    такое. Берём часть до запятой и чистим кавычки, чтобы «Медси» и
    «„Медси“» считались одним.
    """
    head = (raw or "").split(",")[0].strip().lower()
    return "".join(ch for ch in head if ch.isalnum() or ch.isspace()).strip()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(raw: str, fallback):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _merge(old: list[str], new: list[str]) -> list[str]:
    """Объединяет списки контактов, сохраняя порядок и убирая повторы."""
    result = list(old)
    for item in new:
        if item and item not in result:
            result.append(item)
    return result


def _to_contact(row: sqlite3.Row) -> Contact:
    return Contact(
        pk=int(row["pk"]),
        kind=Kind(row["kind"]),
        key=row["key"],
        name=row["name"],
        stage=_safe_stage(row["stage"]),
        city=row["city"],
        site=row["site"],
        emails=_load(row["emails"], []),
        phones=_load(row["phones"], []),
        telegram=_load(row["telegram"], []),
        source_url=row["source_url"],
        meta=_load(row["meta"], {}),
        note=row["note"],
    )


def _safe_stage(raw: str) -> Stage:
    """Незнакомая стадия не должна ронять экран — считаем такой контакт новым."""
    try:
        return Stage(raw)
    except ValueError:
        logger.warning("Незнакомая стадия «{}» — показываю как новую", raw)
        return Stage.NEW


def _to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        pk=int(row["pk"]),
        contact_pk=int(row["contact_pk"]),
        channel=Channel(row["channel"]),
        subject=row["subject"],
        body=row["body"],
        status=row["status"],
        error=row["error"],
        approved_at=_column(row, "approved_at"),
        attempts=int(_column(row, "attempts") or 0),
        created_at=_column(row, "created_at"),
    )


def _column(row: sqlite3.Row, name: str):
    """Значение колонки, которой может не быть в старой базе."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None

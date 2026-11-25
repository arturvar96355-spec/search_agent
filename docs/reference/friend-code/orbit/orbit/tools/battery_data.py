"""Проверка боевых данных на инварианты, которые обещает код.

Тесты проверяют логику на выдуманных случаях, обходчик — кнопки, а эта
батарея смотрит на то, что реально накопилось в базах за месяцы работы.
Разница важна: почти каждая ошибка из тех, что чинились на этой неделе,
оставляла после себя след в данных — контакт без письма, письмо у закрытого
контакта, отправку без записи в журнале. По самим данным такие следы видно
сразу, а по коду — только если знать, куда смотреть.

Инвариант — это обещание кода. Нарушенный инвариант значит одно из двух:
либо код сломан прямо сейчас, либо был сломан раньше и оставил мусор, из-за
которого экраны показывают неправду.

Запуск:  .venv/bin/python -m tools.battery_data

Пишем только в КОПИИ баз, и то ничего не пишем — но копии делаем всё равно.
Соседняя батарея тоже «только читала», а внутри вызывала функцию, которая
кладёт в базу сгенерированный текст: пять живых заказчиков получили тогда
поддельные отклики за 1210 ₽. Копия стоит сотую долю секунды.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]
BIDS = ORBIT.parent / "freelance-bid"
sys.path.insert(0, str(ORBIT))
sys.path.insert(0, str(BIDS))

_SANDBOX = Path(tempfile.mkdtemp(prefix="battery-data-"))

problems: list[str] = []


def sandbox_db(source: Path) -> Path:
    """Копия базы во временном каталоге. Отказ вместо тихого продолжения."""
    target = _SANDBOX / source.name
    if not source.exists():
        raise SystemExit(f"ОТКАЗ: базы {source} нет — проверять нечего")
    shutil.copy2(source, target)
    for tail in ("-wal", "-shm"):
        extra = source.with_name(source.name + tail)
        if extra.exists():
            shutil.copy2(extra, target.with_name(target.name + tail))
    if not target.exists() or target.resolve() == source.resolve():
        raise SystemExit(f"ОТКАЗ: не смог сделать копию базы {source} — по живой не работаю")
    return target


def head(title: str) -> None:
    print("\n" + "─" * 74)
    print(" " + title)
    print("─" * 74)


def flag(text: str) -> None:
    problems.append(text)
    print("  ⚠️ ", text)


def ok(text: str) -> None:
    print(f"     {text}")


# --- Orbit: контакты, письма, журнал ----------------------------------------

def check_orbit() -> None:
    from orbit import bot
    from orbit.models import Kind
    from orbit.store import Store

    head("ORBIT · контакты, письма, журнал")
    store = Store(sandbox_db(bot.get_settings().db_path))
    try:
        _drafts_and_stages(store)
        _sent_letters_are_logged(store)
        _dead_addresses_stay_dead(store)
        _counters_match_the_journal(store, Kind.PARTNER)
        _counters_match_the_journal(store, Kind.CLIENT)
    finally:
        store.close()


def _drafts_and_stages(store) -> None:
    """Письмо и стадия контакта обязаны быть согласованы.

    Обещание кода: живое письмо есть ровно у контакта в «Готовы к отправке»,
    и наоборот. Разъезд означает, что компания либо висит в очереди, будучи
    закрытой, либо выпала из работы совсем: пачка берёт только «новых», и
    контакт в «готовых» без письма не увидит больше никто.
    """
    live = store.conn.execute(
        """SELECT d.pk, c.stage, c.name, c.meta FROM drafts d JOIN contacts c ON c.pk = d.contact_pk
           WHERE d.status = 'draft'"""
    ).fetchall()
    ok(f"живых писем: {len(live)}")
    for row in live:
        if row["stage"] in ("queued", "new"):
            continue
        # Письмо вдогонку — законное исключение: цепочка касаний готовит его
        # контакту, которому УЖЕ написали, и стадию «написали» не трогает.
        # Проверка этого не знала и восемь честных касаний объявила дефектом.
        # Ложная тревога в проверяющем средстве хуже отсутствующей: после неё
        # перестают читать и настоящие.
        if row["stage"] == "written" and _touched(row["meta"]):
            continue
        flag(f"письмо {row['pk']} у «{row['name']}» в стадии «{row['stage']}»")

    stuck = store.conn.execute(
        """SELECT pk, name FROM contacts WHERE stage = 'queued'
           AND pk NOT IN (SELECT contact_pk FROM drafts WHERE status = 'draft')"""
    ).fetchall()
    ok(f"контактов в «готовы к отправке»: {len(live) + len(stuck)}")
    for row in stuck:
        flag(f"«{row['name']}» в «готовы к отправке», а письма нет — выпал из работы")


def _touched(raw) -> bool:
    """Есть ли в карточке отметка о касании вдогонку (followup.TOUCH_KEY)."""
    import json

    try:
        meta = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return False
    return bool(meta.get("touch_step") or meta.get("followup_at"))


def _sent_letters_are_logged(store) -> None:
    """У каждой отправки есть запись в журнале.

    По журналу считаются и вечерняя сводка, и «кому недавно писали» при
    разборе ответа с другого адреса. Отправка без записи — это ответ клиента,
    который уедет соседнему филиалу.
    """
    orphans = store.conn.execute(
        """SELECT d.pk, c.name FROM drafts d JOIN contacts c ON c.pk = d.contact_pk
           WHERE d.status = 'sent' AND NOT EXISTS (
               SELECT 1 FROM events e WHERE e.contact_pk = d.contact_pk AND e.kind = 'sent')"""
    ).fetchall()
    total = store.conn.execute("SELECT COUNT(*) AS n FROM drafts WHERE status = 'sent'").fetchone()
    ok(f"отправленных писем: {total['n']}, без записи в журнале: {len(orphans)}")
    for row in orphans[:10]:
        flag(f"письмо {row['pk']} («{row['name']}») отправлено, а события «sent» нет")


def _dead_addresses_stay_dead(store) -> None:
    """Адрес, забракованный навсегда, не должен вернуться в карточку.

    Возвращает его импорт выгрузки: в файле парсера он лежит по-прежнему.
    Ради этого set_emails и помнит вычеркнутые адреса — проверяем, что память
    работает, а не что она просто есть.
    """
    from orbit.models import Kind

    dead = {
        str(row["address"]).strip().lower()
        for row in store.conn.execute(
            "SELECT address FROM email_checks WHERE ok = 0 AND permanent = 1"
        )
    }
    ok(f"адресов забраковано навсегда: {len(dead)}")
    if not dead:
        return
    returned = 0
    for kind in (Kind.PARTNER, Kind.CLIENT):
        for contact in store.contacts(kind, limit=10**9, only_reachable=False):
            for address in contact.emails:
                if address.strip().lower() in dead:
                    returned += 1
                    if returned <= 10:
                        flag(f"забракованный {address} снова в карточке «{contact.name}»")
    if not returned:
        ok("ни один не вернулся")


def _counters_match_the_journal(store, kind) -> None:
    """Суточный счётчик отправок не должен расходиться с журналом.

    Слева — таблица `sends`, по ней считаются лимит и прогрев. Справа —
    события «sent». Разъезд означает, что лимит меряет не то, что произошло.
    """
    from orbit.store import today_msk

    counted = store.sent_today(kind)
    logged = store.conn.execute(
        """SELECT COUNT(*) AS n FROM events e JOIN contacts c ON c.pk = e.contact_pk
           WHERE e.kind = 'sent' AND c.kind = ? AND substr(e.at, 1, 10) = ?""",
        (kind.value, today_msk()),
    ).fetchone()["n"]
    ok(f"{kind.value}: счётчик за сегодня {counted}, событий в журнале {logged}")
    if counted != logged:
        flag(
            f"{kind.value}: счётчик отправок {counted}, а событий «sent» за сегодня {logged}"
        )


# --- Биржа: заказы, отклики, деньги -----------------------------------------

def check_exchange() -> None:
    from fbid.config import get_settings
    from fbid.store import Store

    head("БИРЖА · заказы, отклики, деньги")
    store = Store(sandbox_db(get_settings().db_path))
    try:
        _one_bid_per_order(store)
        _sent_bids_have_a_time(store)
        _money_matches_the_bids(store)
        _replied_orders_are_not_nudged(store)
    finally:
        store.close()


def _one_bid_per_order(store) -> None:
    """На заказ приходится не больше одного отклика. Второй — это второй платёж."""
    dupes = store.conn.execute(
        "SELECT order_pk, COUNT(*) AS n FROM bids GROUP BY order_pk HAVING n > 1"
    ).fetchall()
    total = store.conn.execute("SELECT COUNT(*) AS n FROM bids").fetchone()["n"]
    ok(f"строк в bids: {total}, заказов с двумя и больше: {len(dupes)}")
    for row in dupes[:10]:
        flag(f"по заказу {row['order_pk']} строк отклика: {row['n']}")


def _sent_bids_have_a_time(store) -> None:
    """У отправленного отклика есть время отправки.

    По нему считается и экран «Отклики за сегодня», и скорость отклика, и
    строка «Откликнулись» в карточке. Пусто — значит все три соврут.
    """
    rows = store.conn.execute(
        """SELECT COUNT(*) AS n FROM bids
           WHERE status = 'sent' AND COALESCE(message_sent_at, bid_at, created_at) IS NULL"""
    ).fetchone()
    sent = store.conn.execute(
        "SELECT COUNT(*) AS n FROM bids WHERE status = 'sent'"
    ).fetchone()["n"]
    ok(f"отправленных откликов: {sent}, без времени отправки: {rows['n']}")
    if rows["n"]:
        flag(f"откликов без времени отправки: {rows['n']}")


def _money_matches_the_bids(store) -> None:
    """Книга расхода и стоимость откликов должны сходиться по порядку величины.

    Книг две, и это уже однажды разошлось: в стоимость одного отклика попала
    цена нашей работы (28 000 ₽ вместо нуля), и сводка сообщала «потрачено
    28 491 ₽» там, где площадки списали 491 ₽.
    """
    spend = store.conn.execute(
        "SELECT COALESCE(SUM(amount_rub), 0) AS total FROM spend"
    ).fetchone()["total"]
    booked = store.conn.execute(
        "SELECT COALESCE(SUM(cost_rub), 0) AS total FROM bids WHERE status = 'sent'"
    ).fetchone()["total"]
    ok(f"по книге расхода: {spend:.0f} ₽, по стоимости откликов: {booked:.0f} ₽")
    if spend and booked > spend * 3:
        flag(f"стоимость откликов ({booked:.0f} ₽) втрое выше книги расхода ({spend:.0f} ₽)")

    absurd = store.conn.execute(
        "SELECT COUNT(*) AS n FROM bids WHERE cost_rub > 5000"
    ).fetchone()["n"]
    if absurd:
        flag(f"откликов дороже 5000 ₽: {absurd} — столько отклик не стоит нигде")


def _replied_orders_are_not_nudged(store) -> None:
    """Клиенту, который ответил, напоминания не шлём.

    Сравниваем ВРЕМЕНА, а не сам факт. Напоминание и ответ по одному заказу
    сплошь и рядом стоят рядом законно: второе касание как раз и будило
    молчуна, а ответ пришёл назавтра. Проверка на «есть и то, и другое»
    ловила такие пары и объявляла их дефектом — на боевой базе первый же
    удачно сработавший нудж выглядел как ошибка. Дефект — это напоминание
    ПОСЛЕ ответа: тогда мы написали вдогонку человеку, который уже написал сам.

    В отметке напоминания лежит «сколько|когда последнее», в отметке ответа —
    дата первого ответа.
    """
    replied = {
        str(row["key"]).split(":", 1)[1]: str(row["value"]).strip()
        for row in store.conn.execute(
            "SELECT key, value FROM state WHERE key LIKE 'clientreplied:%' AND value != ''"
        )
    }
    nudged = {
        str(row["key"]).split(":", 1)[1]: str(row["value"])
        for row in store.conn.execute("SELECT key, value FROM state WHERE key LIKE 'nudged:%'")
    }
    ok(f"заказов с ответом клиента: {len(replied)}, с напоминаниями: {len(nudged)}")
    late = 0
    for key, raw in nudged.items():
        answered = replied.get(key, "")
        # У старых отметок вместо даты стоит «1» — сравнивать не с чем, и
        # объявлять по ним дефект нельзя: «не смогли проверить» это не «плохо».
        if not answered or answered == "1":
            continue
        parts = raw.split("|")
        when = parts[1].strip() if len(parts) > 1 else ""
        if not when:
            continue
        if when > answered:
            late += 1
            if late <= 10:
                flag(f"по заказу {key} напоминание {when} — после ответа клиента {answered}")
    if not late:
        ok("вдогонку ответившим не писали")


def main() -> int:
    print("═" * 74)
    print(" ПРОВЕРКА БОЕВЫХ ДАННЫХ · инварианты, которые обещает код")
    print("═" * 74)
    check_orbit()
    check_exchange()

    print("\n" + "═" * 74)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        print("═" * 74)
        for text in problems:
            print(f"  · {text}")
        return 1
    print(" ПРОБЛЕМ НЕ НАЙДЕНО")
    print("═" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

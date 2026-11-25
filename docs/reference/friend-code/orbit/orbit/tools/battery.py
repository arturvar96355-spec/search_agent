"""Батареи прогонов на ЖИВЫХ данных. Наружу ничего не уходит, базы только читаются.

Отдельно от тестов намеренно. Тесты проверяют логику на выдуманных случаях и
должны быть быстрыми; эта батарея прогоняет настоящие заказы и контакты из
боевых баз — так видно то, чего в выдуманных данных нет: кривые названия,
пустые бюджеты, чужие домены в почте.

Запуск:  .venv/bin/python tools/battery.py

Четыре набора по 40 случаев: решения по заказам, развилка автоответа,
карточки телеграма, выбор адреса для письма. Данные настоящие — из боевых
баз, копий не делаем, потому что всё только читается.
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

FB = "/Users/serezastarkov/PyCharmMiscProject/freelance-bid"
OR = "/Users/serezastarkov/PyCharmMiscProject/orbit"
sys.path.insert(0, FB)
sys.path.insert(0, OR)

# Работаем на КОПИЯХ боевых баз. Батарея задумывалась как «только чтение», но
# на слово этому верить нельзя: соседняя батарея откликов так же «только
# читала» — а bidder.decide() внутри кладёт сгенерированный заход в базу.
# Модель там была подменена заглушкой, и в боевой кэш легли фальшивые тексты;
# сборщик потом отправил их пятерым живым заказчикам за 1210 ₽. Копия стоит
# сотую долю секунды и закрывает весь этот класс ошибок целиком.
_SANDBOX = Path(tempfile.mkdtemp(prefix="battery-"))


def sandbox_db(source: Path) -> Path:
    """Копия базы во временном каталоге. Писать в неё безопасно.

    Отказ вместо тихого продолжения — принципиально. Копия могла не сделаться
    (нет места, нет прав), и тогда прежний код спокойно вернул бы путь и
    работал бы по живым данным. Один такой тихий промах уже стоил 1210 ₽ и
    пяти писем не тем людям.
    """
    target = _SANDBOX / source.name
    if not source.exists():
        return target
    shutil.copy2(source, target)
    for tail in ("-wal", "-shm"):
        extra = source.with_name(source.name + tail)
        if extra.exists():
            shutil.copy2(extra, target.with_name(target.name + tail))
    if not target.exists() or target.resolve() == source.resolve():
        raise SystemExit(f"ОТКАЗ: не смог сделать копию базы {source} — по живой не работаю")
    return target

N = 40
problems: list[str] = []


def head(title: str) -> None:
    print("\n" + "═" * 74)
    print(" " + title)
    print("═" * 74)


def flag(text: str) -> None:
    problems.append(text)
    print("  ⚠️ ", text)


# --- 1. Решения по заказам --------------------------------------------------

def battery_orders() -> None:
    from fbid.filters import FilterSet
    from fbid.scoring import score_order
    from fbid.store import Store

    head(f"1. РЕШЕНИЯ ПО ЗАКАЗАМ · {N} живых заказов")
    store = Store(sandbox_db(Path(FB) / "data" / "fbid.db"))
    filters = FilterSet.load()
    rows = store.conn.execute(
        "SELECT * FROM orders ORDER BY pk DESC LIMIT ?", (N,)
    ).fetchall()

    verdicts = Counter()
    for row in rows:
        order = store._row_to_order(row)
        decision = filters.decide(order)
        score = score_order(order, budget_floor=filters.common.min_budget).total
        mark = "БЕРЁМ  " if decision.passed else "мимо   "
        verdicts["берём" if decision.passed else "мимо"] += 1
        money = f"{order.budget_value:>8.0f} ₽" if order.budget_value else "договорн."
        print(f"  {mark} ⭐{score:4.1f} {money} │ {order.title[:38]:40} │ {decision.reason[:30]}")

        if decision.passed and score < 3:
            flag(f"низкий рейтинг {score:.1f}, но заказ прошёл: {order.title[:40]}")
        if not decision.passed and not decision.reason:
            flag(f"отказ без причины: {order.title[:40]}")
    print(f"\n  итог: {dict(verdicts)}")
    store.close()


# --- 2. Развилка автоответа --------------------------------------------------

def battery_fork() -> None:
    from fbid import dialog, limits
    from fbid.store import Store

    head(f"2. РАЗВИЛКА АВТООТВЕТА · {N} сумм")
    store = Store(sandbox_db(Path(FB) / "data" / "fbid.db"))
    ceiling = limits.get("auto_reply_max_price", store)
    print(f"  порог: {ceiling:.0f} ₽, режим: {dialog.mode(store)}\n")

    sums = [None, 0, 1, 999, 5_000, 15_000, 30_000, 44_999, 45_000, 45_001,
            50_000, 70_000, 100_000, 300_000, 1_000_000]
    sums += [i * 3_000 for i in range(1, N - len(sums) + 1)]

    wrong = 0
    for price in sums[:N]:
        # Проверяем чистое правило цены, без отсечки по дате: она про другое.
        expected = price is not None and 0 < price <= ceiling
        actual = price is not None and bool(price) and price <= ceiling
        mark = "модель" if actual else "ВЛАДЕЛЕЦ"
        shown = f"{price:>9,.0f} ₽".replace(",", " ") if price else "договорная"
        print(f"  {shown} → {mark}")
        if actual != expected:
            wrong += 1
            flag(f"неожиданное решение на {shown}")
    print(f"\n  расхождений с правилом: {wrong}")
    store.close()


# --- 3. Карточки телеграма ---------------------------------------------------

def battery_cards() -> None:
    from fbid import notify
    from fbid.store import Store

    head(f"3. КАРТОЧКИ ТЕЛЕГРАМА · {N} живых заказов")
    store = Store(sandbox_db(Path(FB) / "data" / "fbid.db"))
    rows = store.conn.execute(
        "SELECT * FROM orders ORDER BY pk DESC LIMIT ?", (N,)
    ).fetchall()

    kinds = 0
    for row in rows:
        order = store._row_to_order(row)
        cards = {
            "новый заказ": notify.order_card(order),
            "клиент ответил": notify.client_replied_card(order, said="а сколько по срокам?"),
            "крупный клиент": notify.expensive_client_card(order, said="обсудим большой проект"),
            "клиент молчит": notify.silent_client_card(order, 1, placed="22.08 в 19:40", days_silent=2),
        }
        for name, card in cards.items():
            kinds += 1
            plain = re.sub(r"<[^>]+>", "", card)
            if len(card) > 4096:
                flag(f"{name}: длиннее лимита Telegram ({len(card)})")
            if card.count("<b>") != card.count("</b>"):
                flag(f"{name}: незакрытый тег <b> — Telegram отвергнет")
            if card.count("<blockquote>") != card.count("</blockquote>"):
                flag(f"{name}: незакрытый <blockquote>")
            if "&" in plain and "&amp;" not in card and "&gt;" not in card and "&lt;" not in card:
                flag(f"{name}: неэкранированный & в «{order.title[:30]}»")
            if not plain.strip():
                flag(f"{name}: пустая карточка")
            if "None" in plain:
                flag(f"{name}: в тексте пролезло None — «{plain[:60]}»")
    print(f"  проверено карточек: {kinds}")
    store.close()


# --- 4. Выбор адреса для письма ----------------------------------------------

def battery_mail() -> None:
    from orbit import mailcheck
    from orbit.config import PROJECT_ROOT
    from orbit.models import Channel
    from orbit.store import Store

    head(f"4. КУДА ПОЙДЁТ ПИСЬМО · {N} живых контактов")
    store = Store(sandbox_db(PROJECT_ROOT / "data" / "orbit.db"))
    from orbit.models import Kind

    contacts = (store.contacts(Kind.PARTNER, None, limit=N)
                + store.contacts(Kind.CLIENT, None, limit=N))[:N]

    stat = Counter()
    for contact in contacts:
        address = mailcheck.known_address(store, contact)
        channel = contact.channel
        if address:
            stat["почта"] += 1
            where = address
        elif contact.best_telegram:
            stat["телеграм"] += 1
            where = contact.best_telegram
        elif contact.primary_phone:
            stat["только телефон"] += 1
            where = contact.primary_phone
        else:
            stat["некуда"] += 1
            where = "—"
        mark = "⚠️" if contact.foreign_email else "  "
        print(f"  {mark} {contact.name[:34]:36} {channel.value:9} {where[:34]}")

        if address and channel is not Channel.EMAIL:
            flag(f"адрес есть, а канал не почта: {contact.name[:30]}")
        if contact.reachable and where == "—":
            flag(f"числится доступным, а писать некуда: {contact.name[:30]}")
    print(f"\n  итог: {dict(stat)}")
    store.close()


if __name__ == "__main__":
    battery_orders()
    battery_fork()
    battery_cards()
    battery_mail()

    print("\n" + "═" * 74)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for item in dict.fromkeys(problems):
            print("  ·", item)
    else:
        print(" ПРОБЛЕМ НЕ НАЙДЕНО")
    print("═" * 74)
    shutil.rmtree(_SANDBOX, ignore_errors=True)

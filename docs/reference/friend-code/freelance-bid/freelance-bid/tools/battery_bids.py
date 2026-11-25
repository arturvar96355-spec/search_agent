"""Батарея откликов: 40 живых заказов через весь путь решения.

Модель подменена — шлюз сейчас не тянет наши промты. Проверяется всё
остальное: фильтры, рейтинг, деньги, близнецы, лимиты, правила текста и то,
что именно ушло бы клиенту.

Запуск:  .venv/bin/python tools/battery_bids.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbid import dialog
from fbid.bidder import Bidder
from fbid.filters import FilterSet
from fbid.llm import VERDICT_TAKE, BidDraft
from fbid.models import BidStatus
from fbid.store import Store

N = 40
problems: list[str] = []


def flag(text: str) -> None:
    problems.append(text)
    print("  ⚠️ ", text)


def fake_draft(order, strict: bool = False):
    """Что вернула бы модель. Проверяем не её, а путь решения вокруг.

    strict — переспрос после шаблонного захода. Настоящая generate_draft его
    принимает, и подделка обязана принимать тоже: иначе батарея падает на
    первом же заказе, где нечего процитировать.

    Текст собираем ИЗ САМОЙ ЗАЯВКИ, и это не косметика. Прежний вариант был
    набором общих фраз («делаем такое регулярно», «вижу два пути»), одинаковых
    для любого заказа. Он дважды сыграл злую шутку: однажды такие тексты
    утекли живым заказчикам (см. заголовок файла), а теперь их ещё и
    отбраковывает проверка на шаблон — и батарея мерила бы не решения по
    заказам, а один и тот же отказ «заход шаблонный».
    """
    import re

    price = int(order.budget_value or 30_000)
    # Слово берём из заявки КАК ОНО ТАМ НАПИСАНО. specific_words отдаёт слова
    # приведёнными для сравнения — в них кириллица подменена латинскими
    # двойниками, и в тексте такое слово выглядит опечаткой.
    title = set(re.findall(r"\w{5,}", (order.title or "").lower()))
    detail = [
        word for word in re.findall(r"\w{5,}", order.description or "")
        if word.lower() not in title
    ][:1]
    about = f"по «{detail[0]}»" if detail else f"по задаче «{order.title[:40]}»"
    return BidDraft(
        verdict=VERDICT_TAKE,
        reason="наш профиль",
        label=order.title[:40],
        reply=(
            f"Здравствуйте. Мы студия UniqOre, https://uniqore.ru. Вопрос {about}: "
            f"что уже готово, а что собираем с нуля? Ориентир {price} ₽, оплата 50/50, "
            "старт от недели."
        ),
        estimate=f"{price} ₽",
        price_from=price,
        price_to=price,
        days=7,
    )


def main() -> int:
    # РАБОТАЕМ НА КОПИИ БАЗЫ. Это не перестраховка, а исправление ошибки,
    # которая стоила денег и репутации: batтерея вызывает bidder.decide(), а тот
    # внутри КЛАДЁТ сгенерированный заход в базу (_draft → save_draft), чтобы
    # отложенный заказ не генерировался заново. Модель здесь подменена
    # заглушкой — и в боевой кэш легли двадцать пять фальшивых заготовок вида
    # «делаем такое регулярно». Сборщик потом честно взял их из кэша и отправил
    # пятерым живым заказчикам, заплатив 1210 ₽ за отклики на чужие задачи
    # (игры на Unreal, 3D-миниатюры). Читать боевую базу можно, писать в неё —
    # нельзя ни при каких условиях.
    live = Path(__file__).resolve().parent.parent / "data" / "fbid.db"
    sandbox = Path(tempfile.mkdtemp(prefix="battery-bids-")) / "fbid.db"
    shutil.copy2(live, sandbox)
    for tail in ("-wal", "-shm"):
        extra = live.with_name(live.name + tail)
        if extra.exists():
            shutil.copy2(extra, sandbox.with_name(sandbox.name + tail))

    if not sandbox.exists() or sandbox.resolve() == live.resolve():
        raise SystemExit(f"ОТКАЗ: не смог сделать копию базы {live} — по живой не работаю")

    store = Store(sandbox)
    filters = FilterSet.load()
    bidder = Bidder(store, filters)

    import fbid.bidder as bidder_mod

    bidder_mod.generate_draft = fake_draft

    print("═" * 78)
    print(f" ОТКЛИКИ · {N} живых заказов через весь путь решения")
    print("═" * 78)

    rows = store.conn.execute(
        "SELECT * FROM orders ORDER BY pk DESC LIMIT ?", (N,)
    ).fetchall()

    verdicts = Counter()
    texts: list[str] = []
    for row in rows:
        order = store._row_to_order(row)
        order_pk = int(row["pk"])
        outcome = bidder.decide(order, order_pk, balance=5_000, min_score=0)
        verdicts[outcome.status.value] += 1
        money = f"{order.budget_value:>8.0f}₽" if order.budget_value else "договорн."
        print(f"  {outcome.status.value:11} {money} │ {order.title[:34]:36} │ "
              f"{outcome.reason[:28]}")

        if outcome.status is BidStatus.PLANNED:
            if not outcome.text or len(outcome.text) < 100:
                flag(f"заход короче ста знаков: {order.title[:35]}")
            texts.append(outcome.text)
            problem = dialog.check_reply_rules(outcome.text, order)
            if problem:
                flag(f"текст не прошёл бы проверку правил ({problem}): {order.title[:30]}")
        if outcome.status is BidStatus.SENT:
            flag(f"decide() отправил отклик — он не должен ничего отправлять: {order.title[:30]}")
        # На PLANNED cost — это ПЛАНИРУЕМАЯ цена отклика, а не списанные
        # деньги: проверять её здесь нечего, деньги считает execute().

    print(f"\n  решения: {dict(verdicts)}")
    print(f"  сгенерировано заходов: {len(texts)}")

    if texts:
        print("\n" + "─" * 78)
        print(" ЧТО УШЛО БЫ КЛИЕНТУ (первый заход целиком)")
        print("─" * 78)
        for line in texts[0].splitlines():
            print("  ", line[:74])

    print("\n" + "─" * 78)
    print(" РАЗВИЛКА ПО ДЕНЬГАМ НА ЭТИХ ЖЕ ЗАКАЗАХ")
    print("─" * 78)
    fork = Counter()
    for row in rows:
        order = store._row_to_order(row)
        who = "модель" if dialog.answers_itself(store, order) else "владелец"
        fork[who] += 1
    print(f"  {dict(fork)} (режим: {dialog.mode(store)})")

    print("\n" + "═" * 78)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for item in dict.fromkeys(problems):
            print("  ·", item)
    else:
        print(" ПРОБЛЕМ НЕ НАЙДЕНО")
    print("═" * 78)
    store.close()
    shutil.rmtree(sandbox.parent, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

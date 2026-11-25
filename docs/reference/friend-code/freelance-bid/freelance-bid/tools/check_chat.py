"""Проверка пути «ответить клиенту» на живых страницах бирж. Ничего не отправляет.

Зачем отдельный инструмент. Отправка ответа — единственное место, где софт
пишет живому человеку от имени владельца, и ошибиться в нём дороже всего.
Один раз уже ошиблись: на FL текст уходил в поле личной заметки по заказу,
клиент его не видел, а система рапортовала «отправлено». Ошибка держалась
недели, потому что проверить её было нечем — до первого живого ответа.

Что делает проверка, по шагам, тем же кодом, что и боевая отправка:
  1. открывает страницу заказа, где у нас уже стоит отклик;
  2. находит поле переписки (на FL — открыв окно чата кнопкой);
  3. берёт НАШ УЖЕ ОТПРАВЛЕННЫЙ отклик и проверяет, что подтверждение
     отправки его находит;
  4. проверяет, что выдуманный текст подтверждением НЕ считается.

Шаги 3 и 4 — главное. Они доказывают, что «отправлено» будет значить
«клиент увидит», а не «мы нажали кнопку». Само сообщение не печатается и не
отправляется: писать живому заказчику ради проверки нельзя.

Запуск:  .venv/bin/python tools/check_chat.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbid.browser import BrowserUnavailable, open_context
from fbid.config import get_settings
from fbid.models import Source
from fbid.store import Store

# Выдумка, которой в переписке быть не может: ею проверяем, что подтверждение
# не соглашается на что попало.
NONSENSE = "Этого сообщения в переписке нет и быть не может, это проверка связи"

problems: list[str] = []


def flag(text: str) -> None:
    problems.append(text)
    print("  ⚠️ ", text)


# Сколько заказов пробуем, прежде чем сказать «чата нет». Один заказ ничего не
# доказывает: он мог уйти в архив, и кнопки чата на нём не будет по делу, а не
# из-за поломки. Отличать эти случаи — вся ценность проверки.
ORDERS_TO_TRY = 5


def _recent_bids(store: Store, source: Source):
    """Заказы этой площадки с нашим откликом, от свежих к старым."""
    rows = store.conn.execute(
        """SELECT o.pk, b.text FROM orders o JOIN bids b ON b.order_pk = o.pk
           WHERE b.status = 'sent' AND o.source = ? AND b.text != ''
           ORDER BY b.pk DESC LIMIT ?""",
        (source.value, ORDERS_TO_TRY),
    ).fetchall()
    return [(store.order_by_pk(int(row["pk"])), row["text"]) for row in rows]


def check(source: Source, adapter) -> None:
    store = Store(get_settings().db_path)
    try:
        candidates = _recent_bids(store, source)
    finally:
        store.close()

    print(f"\n{'─' * 70}\n {source.value.upper()}\n{'─' * 70}")
    if not candidates:
        print("  пропускаю: нет ни одного отправленного отклика")
        return

    try:
        with open_context(adapter.name, headless=False) as context:
            page = context.pages[0] if context.pages else context.new_page()
            skipped = 0
            for order, our_text in candidates:
                page.goto(order.url, wait_until="domcontentloaded", timeout=120_000)
                page.wait_for_timeout(9000)

                # Слетевшая сессия выглядит как «чата нет», хотя это совсем
                # другая беда: она чинится входом руками за минуту, а «чата
                # нет» отправил бы разбираться в код. Проверяем отдельно.
                if not adapter.is_logged_in(page.content()):
                    flag(
                        f"{source.value}: СЕССИЯ СЛЕТЕЛА — страница отдаётся как гостю. "
                        f"Войди руками: fbid login {adapter.name}"
                    )
                    return

                field = _find_field(source, adapter, page, order)
                if field is None:
                    # Заказ мог уйти в архив — это про заказ, а не про код.
                    # Пробуем следующий, и только исчерпав все, зовём это поломкой.
                    skipped += 1
                    print(f"  чата нет: {order.title[:50]} (архив или отклик снят)")
                    continue

                print(f"  заказ: {order.title[:60]}")
                print(f"  {order.url}")
                print("  поле переписки: нашлось")
                _verify(source, adapter, page, order, our_text)
                return

            flag(
                f"{source.value}: чата нет ни на одном из {skipped} последних заказов — "
                f"отвечать клиенту нечем"
            )
    except BrowserUnavailable as exc:
        flag(f"{source.value}: браузер недоступен ({exc})")
    except Exception as exc:  # noqa: BLE001 — одна площадка не рушит проверку второй
        flag(f"{source.value}: проверка не прошла ({type(exc).__name__}: {exc})")


def _verify(source: Source, adapter, page, order, our_text: str) -> None:
    """Главное: подтверждение отправки говорит правду в обе стороны."""
    found = adapter._message_is_in_the_thread(page, order, our_text)
    print("  свой отправленный отклик в переписке:", "вижу" if found else "НЕ ВИЖУ")
    if not found:
        flag(
            f"{source.value}: подтверждение не находит наш же отправленный отклик — "
            f"любая настоящая отправка будет считаться неудачной"
        )

    bogus = adapter._message_is_in_the_thread(page, order, NONSENSE)
    print("  выдумку принял за отправленную:", "ДА" if bogus else "нет")
    if bogus:
        flag(
            f"{source.value}: подтверждение соглашается на любой текст — "
            f"значит оно ничего не подтверждает"
        )


def _find_field(source: Source, adapter, page, order):
    """Поле переписки тем же путём, каким его ищет отправка."""
    if source is Source.FL:
        return adapter._open_chat(page, order)
    return adapter._chat_field(page)


def main() -> int:
    print("═" * 70)
    print(" ПУТЬ «ОТВЕТИТЬ КЛИЕНТУ» · живые страницы, отправки НЕ будет")
    print("═" * 70)

    from fbid.sources.fl import FlSource
    from fbid.sources.profi import ProfiSource

    settings = get_settings()
    if settings.fl_enabled:
        check(Source.FL, FlSource())
    if settings.profi_enabled:
        check(Source.PROFI, ProfiSource())

    print("\n" + "═" * 70)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for item in dict.fromkeys(problems):
            print("  ·", item)
    else:
        print(" ПРОБЛЕМ НЕ НАЙДЕНО: ответ клиенту дойдёт, и мы это заметим")
    print("═" * 70)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

import pytest


def test_a_bid_sent_today_counts_today_even_if_written_yesterday(tmp_path):
    """Отклик попадает в сводку по времени ОТПРАВКИ, а не заведения строки.

    Строка в `bids` заводится, когда модель написала заход. Заказ мог
    пролежать в очереди до вечера, а деньги списываются в момент отправки и
    считаются по календарному дню списания. Из-за расхождения сводка за сутки
    выдавала «откликов 0 · потрачено 491 ₽»: деньги за сегодня, отклики — за
    вчера.
    """
    from datetime import datetime, timedelta, timezone

    from fbid.models import BidStatus, Order, Source
    from fbid.stats import collect
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        just_now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        order = Order(source=Source.FL, ext_id="1", title="Лендинг",
                      url="https://fl.ru/1", description="")
        pk, _ = store.upsert_order(order)
        store.conn.execute(
            """INSERT INTO bids (order_pk, status, reason, text, cost_rub,
                                 created_at, message_sent_at)
               VALUES (?, ?, '', 'текст', 60.5, ?, ?)""",
            (pk, BidStatus.SENT.value, long_ago, just_now),
        )
        store.conn.commit()

        funnel = collect(store, days=1)
        assert funnel.sent == 1, "отклик ушёл сегодня — значит он сегодняшний"
    finally:
        store.close()


def test_a_bid_written_today_but_not_yet_sent_is_not_counted_as_sent(tmp_path):
    """Обратная сторона: заход написан сегодня, а ушёл давно — он не сегодняшний."""
    from datetime import datetime, timedelta, timezone

    from fbid.models import BidStatus, Order, Source
    from fbid.stats import collect
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        just_now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        pk, _ = store.upsert_order(Order(source=Source.FL, ext_id="2", title="Сайт",
                                         url="https://fl.ru/2", description=""))
        store.conn.execute(
            """INSERT INTO bids (order_pk, status, reason, text, cost_rub,
                                 created_at, message_sent_at)
               VALUES (?, ?, '', 'текст', 60.5, ?, ?)""",
            (pk, BidStatus.SENT.value, just_now, long_ago),
        )
        store.conn.commit()

        assert collect(store, days=1).sent == 0
    finally:
        store.close()


# --- Причина отказа должна называться правдой ----------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("бюджет 5000 ₽ ниже минимума 20000 ₽", "низкий бюджет"),
        (
            "бюджет 900000 ₽ выше потолка 300000 ₽ — это тендер, отклик уйдёт в никуда",
            "бюджет выше потолка — тендер",
        ),
        (
            "бюджет не указан, наша оценка 900000 ₽ выше потолка 300000 ₽ — это тендер",
            "бюджет выше потолка — тендер",
        ),
        ("бюджет не указан, наша оценка 3000 ₽ ниже минимума 20000 ₽", "низкий бюджет"),
        ("бюджет не указан и оценить работу не вышло", "оценить работу не вышло"),
    ],
)
def test_a_budget_refusal_is_named_correctly(reason, expected):
    """«Выше потолка» и «ниже минимума» начинаются одинаково и значат разное.

    Второй попадал в строку «низкий бюджет» — причина называлась ровно
    наоборот, и владелец, читая отчёт, подкручивал не тот порог.
    """
    from fbid.stats import _short_reason

    assert _short_reason(reason) == expected


def test_the_queue_is_not_reported_as_filtered_out(tmp_path):
    """Заказы, до которых проход не дошёл, — это не «сразу мимо по правилам».

    Строка считалась как «просмотрено минус решения», а называлась так, будто
    все они отсеяны фильтром. Очередь, застрявшая на кончившемся лимите,
    выглядела в отчёте как нормальная работа фильтра — то есть единственный
    случай, где надо что-то делать, прятался в самой безобидной строке.
    """
    from fbid import stats
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        for number in range(3):
            store.upsert_order(Order(
                source=Source.FL, ext_id=str(number), url=f"https://fl.ru/{number}",
                title="Нужен сайт-каталог на Тильде",
                description="Каталог товаров, оплата, интеграция с 1С.",
                category="Сайты", budget_value=60000.0,
            ))

        funnel = stats.collect(store, days=1)
        assert funnel.dropped_early == 3
        assert funnel.waiting == 3, "все три ещё стоят в очереди"

        text = stats.render(funnel, 1)
        assert "Сразу мимо по правилам" not in text
        assert "ещё ждут своей очереди: 3" in text
    finally:
        store.close()

"""Тесты хранилища: дедуп заказов, защита от повторного отклика, счётчик трат."""
from __future__ import annotations

import pytest

from fbid.models import BidStatus, Order, Source
from fbid.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_order(ext_id: str = "1", **kwargs) -> Order:
    data = {
        "source": Source.FL,
        "ext_id": ext_id,
        "url": f"https://www.fl.ru/projects/{ext_id}/x.html",
        "title": "Тестовый заказ",
    }
    data.update(kwargs)
    return Order(**data)


def test_upsert_dedups_by_source_and_ext_id(store):
    pk1, new1 = store.upsert_order(make_order("100"))
    pk2, new2 = store.upsert_order(make_order("100"))
    assert new1 is True
    assert new2 is False
    assert pk1 == pk2
    assert store.orders_count() == 1


def test_repeat_does_not_reset_notified(store):
    """Заказ висит в ленте часами — повторная встреча не должна слать его в TG заново."""
    pk, _ = store.upsert_order(make_order("101"))
    store.mark_notified(pk)
    store.upsert_order(make_order("101", description="описание догрузилось"))
    assert store.is_notified(pk) is True


def test_same_ext_id_on_different_sources_are_distinct(store):
    store.upsert_order(make_order("500"))
    store.upsert_order(make_order("500", source=Source.PROFI, url="https://profi.ru/x/500"))
    assert store.orders_count() == 2
    assert store.orders_count(Source.FL) == 1


def test_has_bid_only_blocks_after_send(store):
    pk, _ = store.upsert_order(make_order("102"))
    store.record_bid(pk, BidStatus.SKIPPED, reason="не прошёл фильтр")
    assert store.has_bid(pk) is False  # пропуск не мешает откликнуться позже

    store.record_bid(pk, BidStatus.SENT, text="отклик")
    assert store.has_bid(pk) is True


def test_spend_accumulates_per_source(store):
    store.add_spend(Source.PROFI, 150.0)
    store.add_spend(Source.PROFI, 200.0)
    store.add_spend(Source.FL, 0.0)

    bids, amount = store.spend_today(Source.PROFI)
    assert bids == 2
    assert amount == 350.0

    bids_fl, amount_fl = store.spend_today(Source.FL)
    assert (bids_fl, amount_fl) == (1, 0.0)


def test_spend_today_ignores_other_days(store):
    store.add_spend(Source.PROFI, 900.0, day="2020-01-01")
    assert store.spend_today(Source.PROFI) == (0, 0.0)


def test_state_roundtrip(store):
    assert store.get_state("bootstrapped:fl") is None
    store.set_state("bootstrapped:fl", "done")
    assert store.get_state("bootstrapped:fl") == "done"


def test_skip_never_erases_a_sent_bid(tmp_path):
    """Отметку «отправлено» нельзя затирать временным отказом.

    Иначе `fbid bid` по уже отработанному заказу получает «уже откликались»
    (SKIPPED), этим стирает отметку, и на заказ уходит второй платный отклик.
    """
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    order = Order(source=Source.PROFI, ext_id="1", url="u", title="Заказ")
    pk, _ = store.upsert_order(order)

    store.record_bid(pk, BidStatus.SENT, reason="ушёл", cost_rub=650.0)
    store.record_bid(pk, BidStatus.SKIPPED, reason="по этому заказу уже откликались")

    row = store.conn.execute("SELECT status, cost_rub FROM bids WHERE order_pk=?", (pk,)).fetchone()
    assert row["status"] == BidStatus.SENT.value
    assert row["cost_rub"] == 650.0
    assert store.has_bid(pk) is True
    store.close()


def test_final_status_can_still_be_upgraded(tmp_path):
    """Ручная отправка после окончательного отказа записаться должна."""
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    pk, _ = store.upsert_order(Order(source=Source.FL, ext_id="2", url="u", title="Заказ"))

    store.record_bid(pk, BidStatus.REJECTED, reason="рейтинг низкий")
    store.record_bid(pk, BidStatus.SENT, reason="отправил руками", cost_rub=0.0)

    row = store.conn.execute("SELECT status FROM bids WHERE order_pk=?", (pk,)).fetchone()
    assert row["status"] == BidStatus.SENT.value
    store.close()


def test_sent_bid_cannot_be_overwritten_by_rejection(tmp_path):
    """Отправленный отклик не затирается отказом — деньги списаны навсегда.

    Кнопка «убрать из очереди» в боте пишет по заказу REJECTED, и старая
    кнопка в чате остаётся кликабельной. Раньше такое нажатие по уже
    отправленному отклику стирало и статус, и сумму: в отчётах деньги
    исчезали, хотя биржа их списала.
    """
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    pk, _ = store.upsert_order(Order(source=Source.PROFI, ext_id="3", url="u", title="Заказ"))

    store.record_bid(pk, BidStatus.SENT, reason="ушёл", cost_rub=420.0)
    store.record_bid(pk, BidStatus.REJECTED, reason="убрал из очереди вручную")

    row = store.conn.execute(
        "SELECT status, cost_rub, reason FROM bids WHERE order_pk=?", (pk,)
    ).fetchone()
    assert row["status"] == BidStatus.SENT.value
    assert row["cost_rub"] == 420.0
    assert row["reason"] == "ушёл"
    store.close()


def test_feed_does_not_overwrite_full_description(tmp_path):
    """Полное ТЗ со страницы заказа не затирается обрезком из ленты.

    Лента отдаёт первые ~170 знаков с многоточием. Раньше следующий проход
    сборщика клал этот обрезок поверх подробного описания, и по нему потом
    считался рейтинг заказа и писался платный заход.
    """
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    short = "Нужен сайт на реакте, бюджет обсудим..."
    full = "Нужен сайт на реакте. " + "Подробное описание задачи. " * 12

    store.upsert_order(Order(source=Source.FL, ext_id="7", url="u", title="Сайт", description=short))
    store.upsert_order(Order(source=Source.FL, ext_id="7", url="u", title="Сайт", description=full))
    # Следующий проход ленты снова приносит обрезок.
    pk, is_new = store.upsert_order(
        Order(source=Source.FL, ext_id="7", url="u", title="Сайт", description=short)
    )

    assert is_new is False
    kept = store.conn.execute("SELECT description FROM orders WHERE pk=?", (pk,)).fetchone()
    assert kept["description"] == full
    store.close()


def test_feed_does_not_erase_known_bid_price(tmp_path):
    """Цена отклика известна только со страницы заказа — лента её не сбрасывает."""
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    store.upsert_order(Order(source=Source.PROFI, ext_id="8", url="u", title="Заказ"))
    store.upsert_order(
        Order(source=Source.PROFI, ext_id="8", url="u", title="Заказ", bid_price=120.0)
    )
    pk, _ = store.upsert_order(Order(source=Source.PROFI, ext_id="8", url="u", title="Заказ"))

    row = store.conn.execute("SELECT bid_price FROM orders WHERE pk=?", (pk,)).fetchone()
    assert row["bid_price"] == 120.0
    store.close()


def test_bid_time_shows_the_send_moment_not_the_draft_moment(tmp_path):
    """Владельцу показываем момент ОТПРАВКИ отклика, а не генерации захода.

    Строка в bids заводится на первом проходе по заказу, а отклик по холодному
    заказу уходит вечером — расхождение доходит до девятнадцати часов. В
    карточке «клиент молчит» это меняет и дату, и счёт дней молчания.
    """
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
        pk, _ = store.upsert_order(order)
        store.record_bid(pk, BidStatus.SENT, reason="ok", text="Здравствуйте!")
        store.conn.execute(
            "UPDATE bids SET created_at = ?, bid_at = NULL, message_sent_at = ? "
            "WHERE order_pk = ?",
            ("2026-08-22T00:40:00+00:00", "2026-08-22T16:40:00+00:00", pk),
        )
        store.conn.commit()

        assert store.bid_time(pk) == "22.08 в 19:40", store.bid_time(pk)
    finally:
        store.close()


def test_sent_bids_use_the_real_send_time(tmp_path):
    """«Отклики за сегодня» считаются по отправке, а не по заведению строки.

    Строка в `bids` заводится, когда модель написала заход; заказ мог
    пролежать в очереди до вечера. По created_at отправленный сегодня отклик
    попадал во «вчера» и исчезал с экрана — а деньги за него в сводке были
    сегодняшние.
    """
    from datetime import datetime, timedelta, timezone

    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        just_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        today = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")

        pk, _ = store.upsert_order(Order(source=Source.FL, ext_id="1", title="Лендинг",
                                         url="https://fl.ru/1", description=""))
        store.conn.execute(
            """INSERT INTO bids (order_pk, status, reason, text, cost_rub,
                                 created_at, message_sent_at)
               VALUES (?, ?, '', 'текст', 60.5, ?, ?)""",
            (pk, BidStatus.SENT.value, long_ago, just_now),
        )
        store.conn.commit()

        assert store.sent_bids_count(since=today) == 1
        rows = store.sent_bids(since=today, limit=10, offset=0)
        assert len(rows) == 1
        assert rows[0]["at"] == just_now, "в списке должно стоять время отправки"
    finally:
        store.close()

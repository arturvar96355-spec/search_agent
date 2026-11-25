"""Полный проход автоотклика с выключенным DRY_RUN.

Это единственное место, где проверяется вся цепочка целиком: очередь заказов →
решение → отправка → учёт денег → уведомление. Здесь ловятся ошибки стыковки,
которых не видно в тестах отдельных кусков.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fbid import bidder as bidder_module
from fbid import config, notify, pipeline
from fbid.llm import BidDraft
from fbid.models import BidStatus, Order, Source
from fbid.store import Store

REPLY = (
    "Здравствуйте! Я из студии UniqOre. Соберём каталог с выгрузкой в 1С, обычно "
    "укладываемся в 3-4 дня, оплата 50/50. Выгрузку делаем по расписанию или по "
    "кнопке из админки?"
)
DRAFT = BidDraft("берём", "", "каталог", REPLY, "от 45к", 45000, 70000, 5)


def _quiet_today(store) -> list[str]:
    """Строки, отложенные до вечерней сводки, за сегодня."""
    return store.quiet_notes(datetime.now(timezone.utc).date().isoformat())

@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture()
def live(monkeypatch):
    """DRY_RUN выключен: софт в этом режиме тратит настоящие деньги."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PROFI_ENABLED", "true")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or True)
    monkeypatch.setattr(notify, "send_alert", lambda text: sent.append(f"ALERT {text}") or True)
    monkeypatch.setattr(bidder_module.notify, "send", lambda text, **kw: sent.append(text) or True)
    monkeypatch.setattr(
        bidder_module.notify, "send_alert", lambda text: sent.append(f"ALERT {text}") or True
    )
    return sent


def profi_order(ext_id="900", **kwargs) -> Order:
    data = {
        "source": Source.PROFI,
        "ext_id": ext_id,
        "url": f"https://profi.ru/backoffice/n.php?o={ext_id}",
        "title": "Нужен сайт-каталог с выгрузкой",
        "category": "Программист",
        "description": "Каталог с корзиной и выгрузкой в 1С",
        "budget_value": 60_000.0,
        "bid_price": 400.0,
        "published_at": datetime.now(timezone.utc),
    }
    data.update(kwargs)
    return Order(**data)


class FakeProfi:
    """Адаптер Profi, который «отправляет» отклик и запоминает как."""

    name = "profi"
    source_enum = Source.PROFI
    can_bid = True

    def __init__(self, balance=5000.0, accepts=True, charged=False, price=None):
        self.balance = balance
        self.accepts = accepts
        # Деньги списаны, а подтверждения нет — самый дорогой случай.
        self.charged = charged
        # Цена, которую площадка увидела на кнопке в момент клика. Именно она
        # должна попасть в учёт, а не та, что лежала в базе.
        self.price = price
        self.submitted = []

    def read_balance(self, _context):
        return self.balance

    def fetch_details(self, _context, order):
        return order

    def submit_bid(self, _context, order, text, **kwargs):
        from fbid.models import SubmitResult

        self.submitted.append({"order": order.ext_id, "text": text, **kwargs})
        return SubmitResult(
            sent=self.accepts,
            price=self.price if self.price is not None else order.bid_price,
            charged=self.accepts or self.charged,
        )


@pytest.fixture()
def run_autobid(monkeypatch):
    """Запускает autobid_all с поддельным браузером и площадкой."""
    from contextlib import contextmanager

    def runner(store, adapter, *, evening: bool = False, **kwargs):
        @contextmanager
        def fake_context(_name, **_kw):
            yield object()

        # Время суток задаём явно. Без этого тест «днём холодный заказ ждёт»
        # проходил до вечера и падал после: вечерний добор включается по
        # реальным часам, и результат зависел от того, когда гоняют тесты.
        monkeypatch.setattr(pipeline, "evening_sweep_now", lambda *a, **kw: evening)
        monkeypatch.setattr("fbid.browser.open_context", fake_context)
        monkeypatch.setattr(
            pipeline, "bidding_sources", lambda: [(adapter, adapter.source_enum)]
        )
        monkeypatch.setattr(bidder_module, "generate_draft", lambda order: DRAFT)
        return pipeline.autobid_all(store, **kwargs)

    return runner


# --- Счастливый путь -------------------------------------------------------

def test_bid_is_sent_and_money_is_counted(store, live, outbox, run_autobid):
    order = profi_order()
    store.upsert_order(order)
    adapter = FakeProfi()

    sent = run_autobid(store, adapter)

    assert sent == 1
    assert len(adapter.submitted) == 1
    assert adapter.submitted[0]["text"].startswith("Здравствуйте")
    # Цена ушла в обязательные поля формы Profi.
    assert adapter.submitted[0]["price_from"] == 45000

    bids, amount = store.spend_today(Source.PROFI)
    assert bids == 1
    assert amount == 400.0
    # Карточка отклика больше не дёргает владельца: она ждёт вечерней сводки.
    # Пропасть при этом не должна — иначе «не беспокоим» стало бы «не сказали».
    assert not any("Отклик отправлен" in text for text in outbox)
    assert any("Отклик отправлен" in note for note in _quiet_today(store))


def test_second_pass_does_not_bid_again(store, live, outbox, run_autobid):
    store.upsert_order(profi_order())
    adapter = FakeProfi()

    run_autobid(store, adapter)
    run_autobid(store, adapter)
    run_autobid(store, adapter)

    assert len(adapter.submitted) == 1, "повторный отклик — это повторное списание"
    assert store.spend_today(Source.PROFI)[1] == 400.0


def test_failed_send_is_not_charged(store, live, outbox, run_autobid):
    store.upsert_order(profi_order())
    adapter = FakeProfi(accepts=False)

    sent = run_autobid(store, adapter)

    assert sent == 0
    assert store.spend_today(Source.PROFI) == (0, 0.0)
    # Один несостоявшийся отклик — не поломка: заказ остаётся в очереди и
    # попадёт в следующий проход. Владелец увидит это вечером, а о настоящей
    # поломке — когда не выходит раз за разом — скажет тревога про площадку.
    assert not any("ALERT" in text for text in outbox)
    assert any("Не смог откликнуться" in note for note in _quiet_today(store))


def test_charged_but_unconfirmed_send_is_not_repeated(store, live, outbox, run_autobid):
    """Деньги списались, а подтверждения нет — второй раз платить не будем.

    Форма на Profi.ru закрывается не всегда: медленный прокси, оборванный
    туннель. Раньше это записывалось как FAILED, заказ возвращался в очередь
    и на следующем проходе оплачивался заново.
    """
    store.upsert_order(profi_order())
    adapter = FakeProfi(accepts=False, charged=True, price=400.0)

    run_autobid(store, adapter)
    first = len(adapter.submitted)
    run_autobid(store, adapter)

    assert first == 1
    assert len(adapter.submitted) == 1, "по оплаченному заказу второй попытки быть не должно"
    assert store.spend_today(Source.PROFI) == (1, 400.0), "списанное должно попасть в учёт"


def test_order_without_price_gets_a_second_look(store, live, run_autobid):
    """Цена не прочиталась при первой встрече — перечитываем перед откликом.

    Раньше страница заказа читалась только один раз, при появлении. Если тогда
    цену разобрать не вышло, заказ навсегда застревал в «цена не прочиталась»:
    отсеивался каждый проход, а перечитать её было некому.
    """
    order = profi_order()
    order.bid_price = None
    store.upsert_order(order)

    class Refreshing(FakeProfi):
        def fetch_details(self, _context, order):
            order.bid_price = 120.0  # на странице цена есть
            return order

    adapter = Refreshing(price=120.0)
    run_autobid(store, adapter)

    assert len(adapter.submitted) == 1, "заказ с перечитанной ценой должен уйти в отклик"
    assert store.spend_today(Source.PROFI) == (1, 120.0)


def test_spend_uses_price_from_the_button_not_from_the_base(store, live, run_autobid):
    """В бюджет идёт та цена, по которой списали, а не та, что лежала в базе.

    Цена в базе может быть суточной давности: страница заказа перечитывается
    только при первом появлении. Из-за этого суточный лимит считался по
    заниженным числам и фактически не работал.
    """
    order = profi_order()
    order.bid_price = 1.0  # столько было вчера
    store.upsert_order(order)
    adapter = FakeProfi(price=350.0)  # столько на кнопке сейчас

    run_autobid(store, adapter)

    assert store.spend_today(Source.PROFI) == (1, 350.0)


# --- Деньги кончились ------------------------------------------------------

def test_low_balance_stops_the_send(store, live, outbox, run_autobid):
    store.upsert_order(profi_order())
    adapter = FakeProfi(balance=100.0)

    assert run_autobid(store, adapter) == 0
    assert adapter.submitted == []


# Заказы должны быть РАЗНЫМИ по смыслу, иначе их склеит защита от близнецов,
# и тест про лимиты пройдёт вхолостую: до лимита дело просто не дойдёт.
DIFFERENT_TITLES = [
    "Нужен интернет-магазин автозапчастей",
    "Телеграм-бот для записи в барбершоп",
    "Интеграция amoCRM с телефонией",
    "Парсер цен конкурентов на python",
    "Личный кабинет для страховой компании",
]


def test_daily_budget_stops_further_sends(store, live, outbox, run_autobid):
    for i, title in enumerate(DIFFERENT_TITLES[:4]):
        store.upsert_order(profi_order(str(900 + i), title=title))
    adapter = FakeProfi()

    run_autobid(store, adapter)

    bids, amount = store.spend_today(Source.PROFI)
    budget = config.get_settings().profi_daily_budget
    assert bids >= 2, "проверять лимит имеет смысл, только если ушло несколько откликов"
    assert amount <= budget, "суточный бюджет пробит"
    assert len(adapter.submitted) < 4, "лимит вообще не сработал"


def test_balance_drops_within_one_pass(store, live, outbox, run_autobid):
    """Баланс уменьшается по ходу прохода, а не читается один раз на старте."""
    for i, title in enumerate(DIFFERENT_TITLES):
        store.upsert_order(profi_order(str(800 + i), title=title, bid_price=300.0))
    adapter = FakeProfi(balance=700.0)

    run_autobid(store, adapter)

    bids, spent = store.spend_today(Source.PROFI)
    assert bids == 2, "на 700 ₽ помещается ровно два отклика по 300 ₽"
    assert spent <= 700.0, "потратили больше, чем было на счету"


# --- Стоп-кран и пороги ----------------------------------------------------

def test_pause_blocks_the_whole_pass(store, live, outbox, run_autobid):
    from fbid.bidder import Bidder

    store.upsert_order(profi_order())
    Bidder(store).pause()
    adapter = FakeProfi()

    assert run_autobid(store, adapter) == 0
    assert adapter.submitted == []


def test_cold_order_waits_but_evening_sweep_takes_it(store, live, outbox, run_autobid):
    cold = profi_order(budget_value=20_000.0, bid_price=100.0)
    store.upsert_order(cold)
    adapter = FakeProfi()

    assert run_autobid(store, adapter, evening=False) == 0, "днём холодный заказ ждёт"
    assert run_autobid(store, adapter, evening=True) == 1, "вечером он получает свой шанс"


def test_same_client_gets_only_one_bid(store, live, outbox, run_autobid):
    store.upsert_order(profi_order("1", client_key="u42"))
    store.upsert_order(profi_order("2", title="Другой сайт-каталог", client_key="u42"))
    adapter = FakeProfi()

    run_autobid(store, adapter)
    assert len(adapter.submitted) == 1


# --- Учёт --------------------------------------------------------------------

def test_sent_bid_is_recorded_with_text(store, live, outbox, run_autobid):
    order = profi_order()
    pk, _ = store.upsert_order(order)
    run_autobid(store, FakeProfi())

    row = store.conn.execute(
        "SELECT status, text, cost_rub FROM bids WHERE order_pk = ?", (pk,)
    ).fetchone()
    assert row["status"] == BidStatus.SENT.value
    assert row["cost_rub"] == 400.0
    assert "UniqOre" in row["text"]


# --- За заказ нельзя заплатить дважды ------------------------------------------


def test_charge_is_recorded_immediately_after_payment(tmp_path, monkeypatch):
    """Между отправкой и record() процесс может умереть — деньги уже ушли.

    Раньше заказ оставался неотработанным, и на следующем проходе за него
    платили второй раз. Отметка «заплачено» должна лечь сразу.
    """
    from fbid.bidder import Bidder, BidOutcome
    from fbid.models import BidStatus, Order, Source, SubmitResult
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="700",
                      url="https://profi.ru/backoffice/n.php?o=700",
                      title="Программист", description="сайт", category="Программист",
                      bid_price=40.0)
        pk, _ = store.upsert_order(order)

        class Adapter:
            name = "profi"

            def submit_bid(self, *a, **kw):
                return SubmitResult(True, price=40.0, charged=True)

        bidder = Bidder(store)
        planned = BidOutcome(BidStatus.PLANNED, "готово", cost=40.0, text="Заход " * 30)
        bidder.execute(Adapter(), None, order, planned, pk)

        # Сюда мы попадаем ДО record(): именно здесь раньше и терялась отметка.
        assert store.has_bid(pk), "заказ должен быть закрыт от повторной оплаты"
    finally:
        store.close()


def test_spent_money_reaches_the_daily_counter_at_once(tmp_path):
    """Суточный счётчик обязан узнать о тратах в момент списания.

    По нему софт решает, можно ли тратить дальше. Раньше он двигался позже,
    в record(), и всё, что случалось между ними, оставляло деньги
    потраченными, а счётчик — нетронутым: бюджет считал, что рублей больше.
    """
    from fbid.bidder import Bidder, BidOutcome
    from fbid.models import BidStatus, Order, Source, SubmitResult
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="702",
                      url="https://profi.ru/backoffice/n.php?o=702",
                      title="Программист", category="Программист", bid_price=40.0)
        pk, _ = store.upsert_order(order)

        class Adapter:
            name = "profi"

            def submit_bid(self, *a, **kw):
                return SubmitResult(True, price=40.0, charged=True)

        planned = BidOutcome(BidStatus.PLANNED, "готово", cost=40.0, text="Заход " * 30)
        outcome = Bidder(store).execute(Adapter(), None, order, planned, pk)

        assert store.spend_today(Source.PROFI) == (1, 40.0)
        assert outcome.spend_booked, "record() не должен записать эти деньги ещё раз"
    finally:
        store.close()


def test_money_is_counted_once_when_everything_goes_well(tmp_path):
    """Обычный путь: execute() и следом record(). Деньги списаны один раз."""
    from fbid.bidder import Bidder, BidOutcome
    from fbid.models import BidStatus, Order, Source, SubmitResult
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="703",
                      url="https://profi.ru/backoffice/n.php?o=703",
                      title="Программист", category="Программист", bid_price=40.0)
        pk, _ = store.upsert_order(order)

        class Adapter:
            name = "profi"

            def submit_bid(self, *a, **kw):
                return SubmitResult(True, price=40.0, charged=True)

        bidder = Bidder(store)
        planned = BidOutcome(BidStatus.PLANNED, "готово", cost=40.0, text="Заход " * 30)
        outcome = bidder.execute(Adapter(), None, order, planned, pk)
        bidder.record(order, pk, outcome)

        assert store.spend_today(Source.PROFI) == (1, 40.0)
    finally:
        store.close()


def test_money_is_counted_when_the_platform_did_not_charge(tmp_path):
    """FL не списывает за отклик поштучно — счётчик двигает record()."""
    from fbid.bidder import Bidder, BidOutcome
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.FL, ext_id="704",
                      url="https://www.fl.ru/projects/704/", title="Сайт")
        pk, _ = store.upsert_order(order)
        sent = BidOutcome(BidStatus.SENT, "отправлено", cost=0.0, text="Заход")
        Bidder(store).record(order, pk, sent)

        assert store.spend_today(Source.FL) == (1, 0.0)
    finally:
        store.close()


def test_details_are_filled_in_after_the_charge(tmp_path):
    """Ранняя отметка не должна мешать дописать текст отклика."""
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="701",
                      url="https://profi.ru/backoffice/n.php?o=701", title="Программист")
        pk, _ = store.upsert_order(order)

        store.record_bid(pk, BidStatus.SENT, reason="списание зафиксировано", cost_rub=40.0)
        store.record_bid(pk, BidStatus.SENT, reason="подходит", text="Полный текст захода",
                         cost_rub=40.0)

        row = store.conn.execute("SELECT text, cost_rub FROM bids WHERE order_pk=?", (pk,)).fetchone()
        assert row["text"] == "Полный текст захода"
        assert row["cost_rub"] == 40.0
    finally:
        store.close()


def test_charge_amount_is_never_zeroed(tmp_path):
    """Деньги списаны — последующее уточнение не должно обнулить сумму."""
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="702",
                      url="https://profi.ru/backoffice/n.php?o=702", title="Программист")
        pk, _ = store.upsert_order(order)

        store.record_bid(pk, BidStatus.SENT, reason="списание", cost_rub=380.0)
        store.record_bid(pk, BidStatus.SENT, reason="уточнение", text="текст", cost_rub=0.0)

        row = store.conn.execute("SELECT cost_rub FROM bids WHERE order_pk=?", (pk,)).fetchone()
        assert row["cost_rub"] == 380.0
    finally:
        store.close()


def test_other_status_still_cannot_overwrite_sent(tmp_path):
    """Защита от REJECTED поверх SENT никуда не делась."""
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="703",
                      url="https://profi.ru/backoffice/n.php?o=703", title="Программист")
        pk, _ = store.upsert_order(order)

        store.record_bid(pk, BidStatus.SENT, reason="отправлено", cost_rub=380.0)
        store.record_bid(pk, BidStatus.REJECTED, reason="убрал из очереди")

        row = store.conn.execute("SELECT status, cost_rub FROM bids WHERE order_pk=?", (pk,)).fetchone()
        assert row["status"] == BidStatus.SENT.value
        assert row["cost_rub"] == 380.0
    finally:
        store.close()

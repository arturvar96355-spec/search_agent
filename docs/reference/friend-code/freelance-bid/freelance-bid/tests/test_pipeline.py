"""Тесты прохода сбора: что происходит, когда площадка ломается.

Проход крутится круглосуточно без присмотра, поэтому проверяем не «собрал ли»,
а «что будет, если не собрал»: упавшая биржа не должна гасить остальные, а
первый запуск не должен завалить владельца полусотней сообщений.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fbid import notify, pipeline
from fbid.models import Order, Source
from fbid.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture()
def quiet(monkeypatch):
    """Ловим уведомления вместо отправки в Telegram."""
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or True)
    monkeypatch.setattr(notify, "send_order", lambda order: sent.append(order.title) or True)
    monkeypatch.setattr(notify, "send_alert", lambda text: sent.append(f"ALERT {text}") or True)
    return sent


def fl_order(ext_id: str, title: str = "Нужен сайт-каталог", **kwargs) -> Order:
    data = {
        "source": Source.FL,
        "ext_id": ext_id,
        "url": f"https://www.fl.ru/projects/{ext_id}/x.html",
        "title": title,
        "category": "Сайты",
        "description": "Нужен интернет-магазин с каталогом",
        "budget_value": 45_000.0,
        "published_at": datetime.now(timezone.utc),
    }
    data.update(kwargs)
    return Order(**data)


class FakeSource:
    """Площадка, которая отдаёт что скажут (или падает, если так задумано)."""

    name = "fl"
    source_enum = Source.FL

    def __init__(self, orders=None, error: Exception | None = None):
        self._orders = orders or []
        self._error = error
        self.details_calls = 0

    def fetch_feed(self):
        if self._error:
            raise self._error
        return list(self._orders)

    def fetch_details(self, order):
        self.details_calls += 1
        return order


# --- Первый запуск ---------------------------------------------------------

def test_first_run_fills_base_without_spamming(store, quiet, monkeypatch):
    """В ленте уже полсотни заказов — на старте о них молчим."""
    source = FakeSource([fl_order(str(i)) for i in range(20)])
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [source])
    monkeypatch.setattr(pipeline, "browser_sources", list)

    stats = pipeline.scan_once(store, sources="fl")

    assert stats.new == 20
    assert stats.notified == 0
    assert quiet == [], "на первом запуске уведомлений быть не должно"
    assert store.orders_count() == 20


def test_second_run_notifies_about_new_orders(store, quiet, monkeypatch):
    first = FakeSource([fl_order("1")])
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [first])
    monkeypatch.setattr(pipeline, "browser_sources", list)
    pipeline.scan_once(store, sources="fl")

    second = FakeSource([fl_order("1"), fl_order("2", "Нужен телеграм-бот для записи")])
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [second])
    stats = pipeline.scan_once(store, sources="fl")

    assert stats.new == 1
    assert stats.notified == 1
    assert len(quiet) == 1


def test_same_order_is_never_notified_twice(store, quiet, monkeypatch):
    source = FakeSource([fl_order("1")])
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [source])
    monkeypatch.setattr(pipeline, "browser_sources", list)
    pipeline.scan_once(store, sources="fl")  # первый запуск, молчим

    new = FakeSource([fl_order("1"), fl_order("2", "Разработка CRM под ключ")])
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [new])
    pipeline.scan_once(store, sources="fl")
    pipeline.scan_once(store, sources="fl")
    pipeline.scan_once(store, sources="fl")

    assert len(quiet) == 1, "один заказ — одно уведомление, сколько бы проходов ни было"


# --- Мусор не доходит до владельца ----------------------------------------

def test_junk_orders_are_not_notified(store, quiet, monkeypatch):
    """Владелец просил: не подходит — даже не присылай."""
    first = FakeSource([fl_order("0")])
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [first])
    monkeypatch.setattr(pipeline, "browser_sources", list)
    pipeline.scan_once(store, sources="fl")

    junk = FakeSource(
        [
            fl_order("1", "Требуется контент-менеджер", budget_value=50000),
            fl_order("2", "Нужен дизайнер сайта", budget_value=80000),
            fl_order("3", "Вакансия: разработчик в штат", budget_value=200000),
            fl_order("4", "Нужен сайт", budget_value=3000),
        ]
    )
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [junk])
    stats = pipeline.scan_once(store, sources="fl")

    assert stats.new == 4
    assert stats.notified == 0
    assert quiet == []


# --- Сбои площадок ---------------------------------------------------------

def test_broken_feed_does_not_kill_the_pass(store, quiet, monkeypatch):
    broken = FakeSource(error=RuntimeError("лента не открылась"))
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [broken])
    monkeypatch.setattr(pipeline, "browser_sources", list)

    stats = pipeline.scan_once(store, sources="fl")
    assert stats.errors, "ошибку надо запомнить, а не проглотить"
    assert stats.fetched == 0


def test_one_broken_platform_does_not_stop_another(store, quiet, monkeypatch):
    broken = FakeSource(error=RuntimeError("FL лёг"))
    working = FakeSource([fl_order("7")])
    working.name = "profi"
    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [broken, working])
    monkeypatch.setattr(pipeline, "browser_sources", list)

    stats = pipeline.scan_once(store, sources="fl")
    assert stats.errors
    assert stats.new == 1


def test_details_failure_keeps_the_order(store, quiet, monkeypatch):
    """Не догрузили ТЗ — заказ с коротким описанием лучше, чем потерянный."""

    class NoDetails(FakeSource):
        def fetch_details(self, order):
            raise RuntimeError("страница не открылась")

    monkeypatch.setattr(pipeline, "enabled_sources", lambda: [NoDetails([fl_order("1")])])
    monkeypatch.setattr(pipeline, "browser_sources", list)

    stats = pipeline.scan_once(store, sources="fl")
    assert stats.new == 1
    assert store.orders_count() == 1


# --- Вечерний добор --------------------------------------------------------

def test_evening_sweep_switches_on_by_the_clock(monkeypatch):
    from datetime import datetime as dt

    from fbid import config
    from fbid.store import MSK

    monkeypatch.setenv("EVENING_SWEEP_HOUR", "21")
    config.get_settings.cache_clear()
    try:
        assert pipeline.evening_sweep_now(dt(2026, 8, 6, 9, 0, tzinfo=MSK)) is False
        assert pipeline.evening_sweep_now(dt(2026, 8, 6, 20, 59, tzinfo=MSK)) is False
        assert pipeline.evening_sweep_now(dt(2026, 8, 6, 21, 0, tzinfo=MSK)) is True
        assert pipeline.evening_sweep_now(dt(2026, 8, 6, 23, 59, tzinfo=MSK)) is True
    finally:
        config.get_settings.cache_clear()


# --- Очередь кандидатов ----------------------------------------------------

def test_best_orders_go_first(store, monkeypatch):
    """Суточный лимит конечен: сначала берём то, где больше денег."""
    from fbid.filters import FilterSet

    cheap = fl_order("1", "Нужен сайт подешевле", budget_value=21_000)
    rich = fl_order("2", "Нужен интернет-магазин", budget_value=300_000)
    for order in (cheap, rich):
        store.upsert_order(order)

    candidates = pipeline._bid_candidates(store, Source.FL, filters=FilterSet.load())
    assert next(o.ext_id for o, _ in candidates) == "2"


def test_already_answered_orders_leave_the_queue(store):
    from fbid.models import BidStatus

    order = fl_order("1")
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BidStatus.SENT, reason="ушёл")

    assert pipeline._bid_candidates(store, Source.FL) == []


# --- Догрузка ТЗ: у площадок разные подписи ---------------------------------

def test_load_details_handles_both_kinds_of_adapters():
    """FL читается HTTP-клиентом, Profi и Юду — браузером.

    Повод: команда ручного отклика звала их одинаково и падала на FL с
    TypeError прямо в момент отправки.
    """
    from fbid.pipeline import load_details

    calls = []

    class HttpAdapter:
        needs_browser = False

        def fetch_details(self, order):
            calls.append(("http", order))
            return order

    class BrowserAdapter:
        needs_browser = True

        def fetch_details(self, context, order):
            calls.append(("browser", context, order))
            return order

    order = fl_order("1")
    load_details(HttpAdapter(), order, context="ctx")
    load_details(BrowserAdapter(), order, context="ctx")

    assert calls[0] == ("http", order)
    assert calls[1] == ("browser", "ctx", order)


def test_real_adapters_declare_what_they_need():
    from fbid.sources.fl import FlSource
    from fbid.sources.profi import ProfiSource
    from fbid.sources.youdo import YoudoSource

    assert FlSource.needs_browser is False
    assert ProfiSource.needs_browser is True
    assert YoudoSource.needs_browser is True


# --- Остаток пакета откликов на FL ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Отклики на заказы У вас 4 отклика. Можно использовать без PRO", 4),
        ("У вас 1 отклик", 1),
        ("У вас 10 откликов", 10),
        ("У вас 0 откликов", 0),
        # Пакет кончился: страница (снято вживую 22.08.2026) не показывает
        # «У вас…» вовсе — только раздел с кнопкой покупки. Это ноль, а не
        # «не прочитали»: иначе владельцу неделями писалось бы «страница не
        # читается», хотя на деле пора покупать пакет.
        ("Аккаунт PRO … Отклики приобретаются отдельно Купить размещение "
         "Отклики на заказы Можно использовать без PRO Купить отклики", 0),
        # А без маркера раздела судить не по чему — страница не прогрузилась.
        ("Купить отклики", None),
        ("страница про что-то другое", None),
        ("", None),
    ],
)
def test_parse_package(text, expected):
    """Формулировка снята с живой страницы fl.ru/payed/ 09.08.2026."""
    from fbid.sources.fl import parse_package

    assert parse_package(text) == expected


def test_empty_package_stops_bidding(monkeypatch, tmp_path):
    """Пакет пуст — до решения по заказу дело не доходит.

    Раньше о конце пакета узнавали задним числом: отклик не появлялся на
    странице заказа, а попытка уже была потрачена.
    """
    from fbid import pipeline
    from fbid.bidder import Bidder
    from fbid.models import Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    decided = []

    class Adapter:
        name = "fl"

        def __init__(self, package):
            self.package = package

        def read_package(self, context):
            return self.package

    class Ctx:
        def __init__(self):
            self.pages = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_decide(self, order, order_pk, **kwargs):
        decided.append(order_pk)
        raise AssertionError("решать по заказу не должны: пакет пуст")

    monkeypatch.setattr(pipeline, "open_context", lambda name: Ctx(), raising=False)
    monkeypatch.setattr(Bidder, "decide", fake_decide)
    monkeypatch.setattr(pipeline, "_bid_candidates", lambda *a, **kw: [("заказ", 1)])
    try:
        for package in (0, None):          # пусто и «не прочитали» — одинаково
            monkeypatch.setattr(pipeline, "bidding_sources",
                                lambda p=package: [(Adapter(p), Source.FL)])
            assert pipeline.autobid_all(store) == 0
            assert decided == []
    finally:
        store.close()


def test_unreadable_package_stops_bidding_too():
    """«Не прочитали» — то же правило, что с балансом Profi: не откликаемся."""
    from fbid.sources.fl import parse_package

    assert parse_package("страница не отдалась") is None


def test_package_runs_out_mid_pass(monkeypatch, tmp_path):
    """Пакет кончился посреди прохода — дальше не идём.

    Остаток читался один раз до цикла и больше не менялся, хотя каждый
    отправленный отклик его тратит. С двумя откликами в пакете и десятком
    заказов проход продолжался: на каждый следующий уходил платный вызов
    модели за заход, отправить который всё равно нечем — форма отклика на
    странице просто не отрисуется.
    """
    from fbid import pipeline
    from fbid.bidder import Bidder
    from fbid.models import BidStatus, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    decided: list[int] = []

    class Adapter:
        name = "fl"

        def read_package(self, context):
            return 2

    class Ctx:
        def __init__(self):
            self.pages = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Outcome:
        status = BidStatus.PLANNED
        reason = ""
        cost = 0.0

    class Sent(Outcome):
        status = BidStatus.SENT

    def fake_decide(self, order, order_pk, **kwargs):
        decided.append(order_pk)
        return Outcome()

    monkeypatch.setattr(pipeline, "open_context", lambda name: Ctx(), raising=False)
    monkeypatch.setattr(pipeline, "bidding_sources", lambda: [(Adapter(), Source.FL)])
    monkeypatch.setattr(pipeline, "_bid_candidates",
                        lambda *a, **kw: [("з", n) for n in range(1, 8)])
    monkeypatch.setattr(pipeline, "_refresh_price", lambda *a, **kw: None)
    monkeypatch.setattr(Bidder, "decide", fake_decide)
    monkeypatch.setattr(Bidder, "execute", lambda self, *a, **kw: Sent())
    monkeypatch.setattr(Bidder, "record", lambda self, *a, **kw: None)
    monkeypatch.setattr(Bidder, "announce", lambda self, *a, **kw: None)

    try:
        assert pipeline.autobid_all(store) == 2, "отправить можно ровно столько, сколько в пакете"
        assert decided == [1, 2], "после второго отклика проход обязан остановиться"
    finally:
        store.close()


# --- Выход из режима проверки не должен стоить денег заново --------------------

def test_go_live_keeps_the_texts_it_already_paid_for(tmp_path):
    """Заходы, написанные в режиме проверки, обязаны пережить включение.

    Кэш готовых текстов лежит в той же строке bids, что и решение. Удаление
    строки уносило его с собой, и каждый освобождённый заказ шёл к модели по
    второму разу — то есть включение боевого режима стоило ровно столько,
    сколько текстов накопил режим проверки.
    """
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.FL, ext_id="777", url="https://fl.ru/777", title="Сайт")
        pk, _ = store.upsert_order(order)
        store.save_draft(pk, '{"reply": "готовый заход"}')
        store.record_bid(pk, BidStatus.DRY_RUN, reason="режим проверки")

        released = store.conn.execute(
            "UPDATE bids SET status = ?, reason = ? WHERE status = ?",
            (BidStatus.SKIPPED.value, "освобождён при выходе из режима проверки",
             BidStatus.DRY_RUN.value),
        ).rowcount
        store.conn.commit()

        assert released == 1
        assert store.load_draft(pk) == '{"reply": "готовый заход"}', "текст оплачен, терять нельзя"
        assert not store.has_bid(pk), "заказ обязан освободиться для настоящего отклика"
    finally:
        store.close()


def test_candidate_window_is_measured_in_the_same_timezone_as_the_base(tmp_path):
    """Окно свежести обязано быть ровно тем, что заявлено, — 48 часов.

    Граница считалась по Москве, а first_seen лежит в UTC. Сравнение идёт
    строками, и «…T19:00:00+03:00» сопоставлялось с «…T16:00:00+00:00»
    посимвольно: окно молча укорачивалось до 45 часов, и заказы этих трёх
    часов выпадали из кандидатов навсегда.
    """
    from datetime import datetime, timedelta, timezone

    from fbid import pipeline
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        # Заказ на границе: 47 часов назад — обязан попасть в кандидаты.
        seen = datetime.now(timezone.utc) - timedelta(hours=47)
        order = Order(
            source=Source.FL, ext_id="47", url="https://fl.ru/47",
            title="Нужен сайт-каталог", description="Каталог с корзиной",
            category="Сайты", budget_value=60_000.0,
        )
        pk, _ = store.upsert_order(order)
        store.conn.execute(
            "UPDATE orders SET first_seen = ? WHERE pk = ?",
            (seen.isoformat(timespec="seconds"), pk),
        )
        store.conn.commit()

        found = pipeline._bid_candidates(store, Source.FL, limit=10, filters=None)
        assert pk in [order_pk for _order, order_pk in found], (
            "заказ 47-часовой давности обязан быть кандидатом при окне в 48 часов"
        )
    finally:
        store.close()


# --- FL: временное и окончательное нельзя путать ------------------------------

def test_empty_package_does_not_bury_the_order_forever(monkeypatch):
    """Пакет откликов кончился — заказ обязан вернуться в очередь.

    Раньше отсутствие формы всегда означало BidUnavailable — статус, который
    закрывает заказ навсегда. Пакет кончался посреди прохода, и десяток живых
    заказов хоронился вместе с ним.
    """
    from fbid.models import Order, Source
    from fbid.sources.fl import FlSource

    class Page:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html><body>Мои проекты" + "." * 3000 + "</body></html>"

        def query_selector(self, _selector):
            return None

    class Context:
        def __init__(self):
            self.pages = [Page()]

    source = FlSource()
    monkeypatch.setattr(source, "read_package", lambda _context: 0)
    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")

    result = source.submit_bid(
        Context(), order, "Здравствуйте, по задаче", days=5, client_price=30000,
    )

    assert result.sent is False
    assert "пакет" in result.reason


def test_missing_form_with_a_full_package_is_final(monkeypatch):
    """Пакет есть, а формы нет — значит уже откликались или заказ закрыт."""
    import pytest

    from fbid.models import Order, Source
    from fbid.sources.fl import BidUnavailable, FlSource

    class Page:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html><body>Мои проекты" + "." * 3000 + "</body></html>"

        def query_selector(self, _selector):
            return None

    class Context:
        def __init__(self):
            self.pages = [Page()]

    source = FlSource()
    monkeypatch.setattr(source, "read_package", lambda _context: 7)
    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")

    with pytest.raises(BidUnavailable):
        source.submit_bid(Context(), order, "Здравствуйте", days=5, client_price=30000)


# --- Слетевшая сессия зовёт владельца сама, не дожидаясь чужого таймера --------

def test_lost_session_alerts_the_owner(tmp_path, monkeypatch):
    """«Войди руками» не должно глушиться таймером денежных тревог.

    У тревоги «отклики встали» свой срок молчания, заведённый под лимиты:
    он ждёт часы, потому что «кончился бюджет» само пройдёт к утру. Слетевший
    вход не пройдёт никогда, и за чужим таймером площадка стояла бы мёртвой
    до случайной проверки.
    """
    from fbid import notify, pipeline
    from fbid.models import Source
    from fbid.store import Store

    said: list[str] = []
    monkeypatch.setattr(notify, "send_alert", lambda text: said.append(text) or True)

    store = Store(tmp_path / "t.db")
    try:
        pipeline._alert_session_lost(store, Source.FL, RuntimeError("Сессия FL.ru слетела"))
        assert said, "владельцу должны сказать сразу"
        assert "fbid login fl" in said[0]

        # Второй раз подряд — молчим: пока не войдут, беда та же самая.
        pipeline._alert_session_lost(store, Source.FL, RuntimeError("Сессия FL.ru слетела"))
        assert len(said) == 1
    finally:
        store.close()


def test_undelivered_session_alert_is_repeated(tmp_path, monkeypatch):
    """Телеграм не принял тревогу — отметку «сказали» не ставим, повторим позже.

    А вот сам ФАКТ слетевшей сессии записываем всегда: его читают экраны
    Orbit. Пока отметка была одна на двоих, вкладка «Фриланс» показывала
    бодрое «работает» при мёртвой площадке.
    """
    from fbid import notify, pipeline
    from fbid.models import Source
    from fbid.store import Store

    monkeypatch.setattr(notify, "send_alert", lambda text: False)
    store = Store(tmp_path / "t.db")
    try:
        pipeline._alert_session_lost(store, Source.FL, RuntimeError("слетела"))
        assert store.get_state(pipeline.session_told_key(Source.FL)) is None, (
            "недоставленное не считается сказанным"
        )
        assert store.get_state(pipeline.session_lost_key(Source.FL)), (
            "правду о площадке пишем всегда"
        )
        assert pipeline.lost_sessions(store) == ["fl"]
    finally:
        store.close()


def test_a_living_session_clears_the_mark(tmp_path, monkeypatch):
    """Владелец вошёл руками — экран обязан позеленеть сразу."""
    from fbid import notify, pipeline
    from fbid.models import Source
    from fbid.store import Store

    monkeypatch.setattr(notify, "send_alert", lambda text: True)
    store = Store(tmp_path / "t.db")
    try:
        pipeline._alert_session_lost(store, Source.FL, RuntimeError("слетела"))
        assert pipeline.lost_sessions(store) == ["fl"]

        pipeline.session_is_alive(store, Source.FL)
        assert pipeline.lost_sessions(store) == []
        assert store.get_state(pipeline.session_told_key(Source.FL)) is None, (
            "иначе следующая беда через месяц промолчала бы"
        )
    finally:
        store.close()


def test_exchange_says_the_package_is_empty_and_the_pass_stops(monkeypatch, tmp_path):
    """Биржа сказала «пакет пуст» — верим ей, а не своему счётчику.

    Остаток читается один раз до цикла. Владелец мог откликнуться руками с
    телефона, пакет мог кончиться у биржи по своим причинам — и тогда на
    странице заказа просто нет формы отклика. Адаптер это распознаёт и
    возвращает отказ «кончился пакет», но проход шёл дальше по списку: на
    каждый следующий заказ уходил платный вызов модели ради захода, отправить
    который нечем. Плюс о вставшей очереди владельцу не говорили вовсе:
    «кончился пакет» не считался денежной причиной.
    """
    from fbid import pipeline
    from fbid.bidder import Bidder
    from fbid.models import BidStatus, Source
    from fbid.sources.fl import PACKAGE_EMPTY
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    decided: list[int] = []

    class Adapter:
        name = "fl"

        def read_package(self, context):
            return 9  # по нашим данным пакет полон

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Planned:
        status = BidStatus.PLANNED
        reason = ""
        cost = 0.0

    class Empty:
        status = BidStatus.FAILED
        reason = PACKAGE_EMPTY
        cost = 0.0

    def fake_decide(self, order, order_pk, **kwargs):
        decided.append(order_pk)
        return Planned()

    told: list[str] = []
    monkeypatch.setattr(pipeline, "open_context", lambda name: Ctx(), raising=False)
    monkeypatch.setattr(pipeline, "bidding_sources", lambda: [(Adapter(), Source.FL)])
    monkeypatch.setattr(pipeline, "_bid_candidates",
                        lambda *a, **kw: [("з", n) for n in range(1, 8)])
    monkeypatch.setattr(pipeline, "_refresh_price", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "_warn_if_queue_stuck",
                        lambda st, blocked: told.extend(blocked))
    monkeypatch.setattr(Bidder, "decide", fake_decide)
    monkeypatch.setattr(Bidder, "execute", lambda self, *a, **kw: Empty())
    monkeypatch.setattr(Bidder, "record", lambda self, *a, **kw: None)
    monkeypatch.setattr(Bidder, "announce", lambda self, *a, **kw: None)

    try:
        assert pipeline.autobid_all(store) == 0
        assert decided == [1], "после ответа биржи проход обязан остановиться на первом же заказе"
        assert any(PACKAGE_EMPTY in line for line in told), "владельцу надо сказать, почему встало"
    finally:
        store.close()


def test_empty_package_counts_as_a_money_block():
    """«Кончился пакет» — денежная причина, а не «заказ не наш»."""
    from fbid.models import BidStatus
    from fbid.pipeline import _is_money_block
    from fbid.sources.fl import PACKAGE_EMPTY

    class Outcome:
        status = BidStatus.FAILED
        reason = PACKAGE_EMPTY

    class NotOurs:
        status = BidStatus.FAILED
        reason = "форма отклика недоступна: уже откликались по этому заказу"

    assert _is_money_block(Outcome())
    assert not _is_money_block(NotOurs())


def test_a_lost_profi_session_alerts_once_not_every_pass(monkeypatch, tmp_path):
    """Слетевшая сессия — одна тревога, а не сто двадцать в час.

    Profi обходится каждые полминуты, и тревога уходила прямо из обработчика
    без всякого глушителя. Одинаковую тревогу перестают читать на третьей, а
    вместе с ней тонет всё остальное, что бот пишет владельцу.
    """
    from fbid import notify, pipeline
    from fbid.sources.profi import NotAuthorized
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    alerts: list[str] = []
    monkeypatch.setattr(notify, "send_alert", lambda text: alerts.append(text) or True)

    class Adapter:
        name = "profi"

        def fetch_feed(self, context):
            raise NotAuthorized("вошли как гость")

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("fbid.browser.open_context", lambda name: Ctx())
    try:
        for _ in range(10):
            stats = pipeline.scan_browser_source(store, Adapter(), notify_new=False)
            assert stats.errors, "об ошибке всё равно надо знать из отчёта прохода"
        assert len(alerts) == 1, f"тревог ушло {len(alerts)}"
    finally:
        store.close()


def test_orders_are_marked_handled_even_with_notifications_off(monkeypatch, tmp_path):
    """Уведомления выключены — отметка «обработан» всё равно ставится.

    Отметка значит «заказ обработан», а не «карточка ушла». Без неё каждый
    следующий проход заново открывал страницу КАЖДОГО заказа с доски ради
    того же ТЗ: на площадке с антиботом это десятки лишних заходов в час.
    """
    from fbid import pipeline
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    details_for: list[str] = []

    order = Order(source=Source.PROFI, ext_id="777", title="Лендинг",
                  url="https://profi.ru/777", description="")

    class Adapter:
        name = "profi"

        def fetch_feed(self, context):
            return [order]

        def fetch_details(self, context, o):
            details_for.append(o.ext_id)
            return o

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("fbid.browser.open_context", lambda name: Ctx())
    try:
        store.set_state("bootstrapped:profi", "done")
        for _ in range(4):
            pipeline.scan_browser_source(store, Adapter(), notify_new=False)
        assert details_for == ["777"], f"ТЗ догружали {len(details_for)} раз"
    finally:
        store.close()


def test_an_unreadable_package_does_not_bury_the_order(monkeypatch):
    """Остаток пакета не прочитался — приговор заказу выносить нельзя.

    None означает «страница платных услуг не открылась», то есть мы НЕ ЗНАЕМ,
    почему нет формы. Раньше это шло по ветке «уже откликались» и хоронило
    заказ навсегда: живой заказ терялся из-за не открывшейся чужой страницы.
    """
    from fbid.models import Order, Source
    from fbid.sources.fl import BidUnavailable, FlSource

    class Page:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html><body>Мои проекты" + "." * 3000 + "</body></html>"

        def query_selector(self, _selector):
            return None

    class Context:
        def __init__(self):
            self.pages = [Page()]

    source = FlSource()
    monkeypatch.setattr(source, "read_package", lambda _context: None)
    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")

    try:
        result = source.submit_bid(
            Context(), order, "Здравствуйте", days=5, client_price=30000,
        )
    except BidUnavailable as exc:  # pragma: no cover — это и есть проверяемый дефект
        raise AssertionError(f"заказ похоронен по неизвестной причине: {exc}") from exc

    assert result.sent is False
    assert result.charged is False
    assert "не прочитался" in result.reason


def test_the_queue_counter_sees_more_than_five_hundred(tmp_path):
    """«Очистить очередь» обязана чистить всю очередь, а не первые пятьсот.

    `LIMIT 500` стоял в запросе всегда, включая случай «отдай всю очередь».
    На базе в три тысячи заказов счётчик врал, а кнопка вычищала ровно
    пятьсот штук за нажатие — владелец жал её раз за разом и не понимал,
    почему очередь не кончается.
    """
    from fbid import pipeline
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        total = pipeline.FRESH_WINDOW + 40
        for n in range(total):
            store.upsert_order(Order(
                source=Source.FL, ext_id=str(n), url=f"https://fl.ru/{n}",
                title="Нужен сайт-каталог на Тильде",
                description="Каталог товаров, оплата, интеграция с 1С.",
                category="Сайты", budget_value=60000.0,
            ))

        size = pipeline.queue_size(store)
        assert size > pipeline.FRESH_WINDOW, f"счётчик показал {size} из {total}"

        removed = pipeline.clear_queue(store)
        assert removed == size, "убрать надо ровно столько, сколько насчитали"
        assert pipeline.queue_size(store) == 0, "после очистки очередь обязана быть пустой"
    finally:
        store.close()


def test_the_queue_screen_still_looks_at_the_fresh_tail(tmp_path):
    """А экран очереди смотрит свежий хвост: на биржах решает скорость."""
    from fbid import pipeline
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        for n in range(3):
            store.upsert_order(Order(
                source=Source.FL, ext_id=str(n), url=f"https://fl.ru/{n}",
                title="Нужен сайт-каталог на Тильде",
                description="Каталог товаров, оплата, интеграция с 1С.",
                category="Сайты", budget_value=60000.0,
            ))
        assert len(pipeline.waiting_queue(store, limit=2)) == 2
    finally:
        store.close()


def test_an_order_with_an_unloaded_spec_is_retried(monkeypatch, tmp_path):
    """Страница ТЗ не открылась — заказ обработанным не считается.

    Отметка «обработан» ставится, чтобы не перекачивать описание каждый
    проход. Но заказ, чью страницу мы не открыли, с ней остался бы с одной
    строкой описания навсегда — и решать по нему модель стала бы по огрызку.
    """
    from fbid import pipeline
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    tries: list[str] = []

    order = Order(source=Source.PROFI, ext_id="55", title="Лендинг",
                  url="https://profi.ru/55", description="")

    class Adapter:
        name = "profi"

        def fetch_feed(self, context):
            return [order]

        def fetch_details(self, context, o):
            tries.append(o.ext_id)
            raise RuntimeError("страница не открылась")

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("fbid.browser.open_context", lambda name: Ctx())
    try:
        store.set_state("bootstrapped:profi", "done")
        for _ in range(3):
            pipeline.scan_browser_source(store, Adapter(), notify_new=False)
        assert len(tries) == 3, "к недогруженному заказу надо вернуться"
    finally:
        store.close()


def test_a_loaded_spec_is_fetched_only_once(monkeypatch, tmp_path):
    """А догруженное ТЗ второй раз не качаем."""
    from fbid import pipeline
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    tries: list[str] = []

    order = Order(source=Source.PROFI, ext_id="56", title="Лендинг",
                  url="https://profi.ru/56", description="")

    class Adapter:
        name = "profi"

        def fetch_feed(self, context):
            return [order]

        def fetch_details(self, context, o):
            tries.append(o.ext_id)
            return o

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("fbid.browser.open_context", lambda name: Ctx())
    try:
        store.set_state("bootstrapped:profi", "done")
        for _ in range(3):
            pipeline.scan_browser_source(store, Adapter(), notify_new=False)
        assert tries == ["56"]
    finally:
        store.close()


def test_the_queue_only_shows_orders_the_pass_can_still_take(tmp_path):
    """Очередь и выбор кандидатов обязаны мерить одним сроком.

    Очередь показывала всё подряд, а брать из неё проход мог только заказы
    моложе CANDIDATE_HOURS: на живой базе полторы сотни строк из двух сотен не
    ушли бы никогда, и «в очереди 209» читалось как «209 отправим».
    """
    from datetime import datetime, timedelta, timezone

    from fbid import pipeline
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        fresh = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1",
                      title="Нужен сайт-каталог на Тильде",
                      description="Каталог товаров, оплата, интеграция с 1С.",
                      category="Сайты", budget_value=60000.0)
        stale = Order(source=Source.FL, ext_id="2", url="https://fl.ru/2",
                      title="Нужен сайт-каталог на Тильде",
                      description="Каталог товаров, оплата, интеграция с 1С.",
                      category="Сайты", budget_value=60000.0)
        fresh_pk, _ = store.upsert_order(fresh)
        stale_pk, _ = store.upsert_order(stale)

        old = (
            datetime.now(timezone.utc) - timedelta(hours=pipeline.CANDIDATE_HOURS + 5)
        ).isoformat(timespec="seconds")
        store.conn.execute("UPDATE orders SET first_seen = ? WHERE pk = ?", (old, stale_pk))
        store.conn.commit()

        queued = {item.order_pk for item in pipeline.waiting_queue(store, limit=None)}
        assert fresh_pk in queued
        assert stale_pk not in queued, "этот заказ проход уже не возьмёт — и обещать его нельзя"
    finally:
        store.close()


def test_the_leftovers_of_the_old_two_phase_queue_are_cleared(tmp_path):
    """Уборка за прежней двухфазной отправкой должна происходить на самом деле.

    Тело функции стояло за проверкой two_phase(), которая всегда False: она не
    досылала (и правильно), но и не убирала. Строки, помеченные к досылу старой
    версией, оставались в базе навсегда, и каждый проход видел их как
    незавершённые. Браузер для уборки не нужен — она вся в базе.
    """
    from datetime import datetime, timezone

    from fbid import pipeline
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(
            source=Source.FL, ext_id="77", url="https://fl.ru/77",
            title="Нужен сайт-каталог", category="Сайты", budget_value=60000.0,
        )
        pk, _ = store.upsert_order(order)
        store.conn.execute(
            """INSERT INTO bids (order_pk, status, text, bid_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (pk, BidStatus.SENT.value, "Здравствуйте, готовы взяться.",
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        store.conn.commit()
        assert store.pending_messages(0), "заготовили строку из прежней очереди"

        assert pipeline.flush_pending_all(store) == 0, "досылать никому ничего нельзя"
        assert not store.pending_messages(0), "строка так и осталась висеть в очереди"
    finally:
        store.close()


def test_the_balance_is_read_even_on_a_quiet_day(tmp_path, monkeypatch):
    """Остаток на счёте читался только перед отправкой откликов.

    В тихий день (или когда встала модель) заказов до отправки не доходило, и
    цифра не читалась ни разу: экран честно писал «ещё не читали» при
    работающем софте. А это как раз то число, по которому владелец решает,
    пополнять ли счёт.
    """
    from fbid import pipeline
    from fbid.models import Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        reads: list[int] = []

        class Adapter:
            name = "profi"

            def read_balance(self, _context):
                reads.append(1)
                return 170.0

        pipeline._refresh_balance(store, Adapter(), None)
        value, when = pipeline.known_balance(store, Source.PROFI)
        assert value == 170.0
        assert when, "без времени цифра стареет незаметно"
        assert len(reads) == 1

        # Второй раз в тот же час в браузер не лезем: лишняя загрузка страницы
        # каждую минуту — это и время прохода, и повод для антибота.
        pipeline._refresh_balance(store, Adapter(), None)
        assert len(reads) == 1
    finally:
        store.close()


def test_an_old_balance_is_read_again(tmp_path):
    from datetime import datetime, timedelta, timezone

    from fbid import pipeline
    from fbid.models import Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
        store.set_state(pipeline.balance_key(Source.PROFI), f"170|{long_ago}")

        reads: list[int] = []

        class Adapter:
            name = "profi"

            def read_balance(self, _context):
                reads.append(1)
                return 90.0

        pipeline._refresh_balance(store, Adapter(), None)
        assert len(reads) == 1
        value, _when = pipeline.known_balance(store, Source.PROFI)
        assert value == 90.0
    finally:
        store.close()


# --- Площадка молчит — это новость -------------------------------------------


def _boom(text="Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://profi.ru/"):
    return RuntimeError(text)


def test_a_dead_platform_calls_the_owner_once(tmp_path, monkeypatch):
    """Пять сентября умер прокси: проход падал каждые полминуты восемь суток.

    В логе накопилось три тысячи одинаковых падений, заказы с Profi перестали
    приходить совсем — а владельцу не ушло ни одного слова. Снаружи это
    выглядело как «сегодня на площадке тихо».
    """
    from fbid import notify, pipeline
    from fbid.models import Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        said: list[str] = []
        monkeypatch.setattr(notify, "send_alert", lambda text: said.append(text) or True)

        # Одно падение — это моргнувшая сеть, тревожить нельзя.
        for _ in range(pipeline.SOURCE_FAILS_BEFORE_ALARM - 1):
            pipeline._alert_source_down(store, Source.PROFI, _boom())
        assert said == [], "рано подняли тревогу"

        pipeline._alert_source_down(store, Source.PROFI, _boom())
        assert len(said) == 1, said
        assert "прокси" in said[0].lower(), "надо назвать причину, а не только площадку"
        assert "check-proxy" in said[0], "команда должна быть настоящей"

        # Дальше молчим: проход идёт каждые полминуты.
        for _ in range(20):
            pipeline._alert_source_down(store, Source.PROFI, _boom())
        assert len(said) == 1

        assert pipeline.down_sources(store) == ["profi"]
    finally:
        store.close()


def test_a_recovered_platform_says_so(tmp_path, monkeypatch):
    from fbid import notify, pipeline
    from fbid.models import Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        said: list[str] = []
        monkeypatch.setattr(notify, "send_alert", lambda text: said.append(text) or True)
        for _ in range(pipeline.SOURCE_FAILS_BEFORE_ALARM):
            pipeline._alert_source_down(store, Source.PROFI, _boom())
        assert len(said) == 1

        pipeline._source_is_alive(store, Source.PROFI)
        assert len(said) == 2 and "снова отвечает" in said[1]
        assert pipeline.down_sources(store) == []

        # А тому, кому не жаловались, о починке не сообщаем.
        pipeline._source_is_alive(store, Source.FL)
        assert len(said) == 2
    finally:
        store.close()


def test_the_owner_is_not_told_twice_when_telegram_failed(tmp_path, monkeypatch):
    """Отметка «сказали» ставится только после доставленного сообщения."""
    from fbid import notify, pipeline
    from fbid.models import Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        monkeypatch.setattr(notify, "send_alert", lambda text: False)
        for _ in range(pipeline.SOURCE_FAILS_BEFORE_ALARM):
            pipeline._alert_source_down(store, Source.PROFI, _boom())
        assert not store.get_state(pipeline.source_told_key(Source.PROFI))

        said: list[str] = []
        monkeypatch.setattr(notify, "send_alert", lambda text: said.append(text) or True)
        pipeline._alert_source_down(store, Source.PROFI, _boom())
        assert len(said) == 1, "после сбоя Телеграма тревогу надо повторить"
    finally:
        store.close()

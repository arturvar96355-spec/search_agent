"""Заказчик, а не только заказ.

Рейтинг всегда считался по заявке: сколько денег, наш ли профиль, свежая ли.
Про того, кто её выложил, не спрашивали вовсе — и мгновенный отклик уходил
человеку, который зарегистрировался вчера и никого ещё не нанимал. Отклики
конечны и стоят денег, поэтому такому заказчику мгновенный отклик не
покупаем: заказ ждёт вечера и разбирается вместе с остальными.

Главное правило здесь — не путать «заказчик новичок» с «биржа промолчала».
Вывод о человеке делаем только по прочитанному.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fbid import bidder as bidder_module
from fbid.bidder import Bidder
from fbid.filters import CommonRules, FilterSet, SourceRules
from fbid.llm import BidDraft
from fbid.models import BidStatus, Order, Source
from fbid.scoring import newcomer
from fbid.sources.fl import parse_client
from fbid.store import Store

REPLY = (
    "Здравствуйте! Я из студии UniqOre. По вашей задаче: соберём лендинг с формой "
    "заявки, обычно укладываемся в 3-4 дня, оплата 50/50. Заявки принимаем на почту "
    "или сразу в бот — как удобнее?"
)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture()
def filters():
    return FilterSet(
        CommonRules(keywords_any=["сайт", "бот"], max_age_hours=None),
        {"fl": SourceRules(categories_allow=["Сайты"])},
    )


@pytest.fixture()
def draft_ok(monkeypatch):
    monkeypatch.setattr(
        bidder_module, "generate_draft",
        lambda order: BidDraft(verdict="берём", reason="", label="Тест", reply=REPLY, estimate="8-15к"),
    )


def fl_order(**kwargs) -> Order:
    data = {
        "source": Source.FL,
        "ext_id": "1",
        "url": "https://www.fl.ru/projects/1/x.html",
        "title": "Нужен сайт",
        "category": "Сайты / Лендинги",
        "description": "Нужен лендинг для студии",
        "budget_value": 45_000.0,
        "published_at": datetime.now(timezone.utc),
    }
    data.update(kwargs)
    return Order(**data)


def _decide(store, filters, order, **kwargs):
    pk, _ = store.upsert_order(order)
    return Bidder(store, filters).decide(order, pk, **kwargs)


# --- Кого считаем новичком ----------------------------------------------------


@pytest.mark.parametrize(
    "registered",
    ["меньше недели", "меньше месяца", "менее недели", "Меньше месяца"],
)
def test_fresh_registration_is_noticed(registered):
    assert newcomer(fl_order(raw={"client_registered": registered}))


@pytest.mark.parametrize(
    "registered",
    ["больше года", "2 года", "полгода", "3 месяца", "больше месяца"],
)
def test_seasoned_client_is_not_a_newcomer(registered):
    assert newcomer(fl_order(raw={"client_registered": registered})) == ""


def test_silence_is_not_a_verdict_about_the_person():
    """Биржа не сказала — значит мы не знаем, а не «он новичок»."""
    assert newcomer(fl_order()) == ""
    assert newcomer(fl_order(raw={"client_registered": ""})) == ""
    assert newcomer(fl_order(raw={"client_registered": None})) == ""


# --- Что это меняет в решении -------------------------------------------------


def test_newcomer_waits_for_the_evening(store, filters, draft_ok):
    """Хороший свежий заказ, но заказчик вчерашний — мгновенный отклик не покупаем."""
    outcome = _decide(store, filters, fl_order(raw={"client_registered": "меньше месяца"}))
    assert outcome.status is BidStatus.SKIPPED
    assert "меньше месяца" in outcome.reason


def test_newcomer_is_not_buried(store, filters, draft_ok):
    """Вечером порог опущен до нуля — заказ разбирается наравне с остальными.

    «Никогда никого не нанимал» и «не успел никого нанять» снаружи выглядят
    одинаково, и хоронить заказ насовсем из-за этого нельзя.
    """
    outcome = _decide(
        store, filters, fl_order(raw={"client_registered": "меньше месяца"}), min_score=0
    )
    assert outcome.status is not BidStatus.SKIPPED


def test_seasoned_client_goes_straight_through(store, filters, draft_ok):
    outcome = _decide(store, filters, fl_order(raw={"client_registered": "больше года"}))
    assert outcome.status is not BidStatus.SKIPPED


def test_unknown_client_does_not_block_anything(store, filters, draft_ok):
    """Пока про заказчика ничего не известно, работаем как раньше."""
    assert _decide(store, filters, fl_order()).status is not BidStatus.SKIPPED


def test_model_is_not_paid_for_a_newcomer(store, filters, monkeypatch):
    """Отсев идёт до генерации захода: заход стоит денег, отсев — нет."""

    def explode(order):
        raise AssertionError("модель звать не должны")

    monkeypatch.setattr(bidder_module, "generate_draft", explode)
    outcome = _decide(store, filters, fl_order(raw={"client_registered": "меньше недели"}))
    assert outcome.status is BidStatus.SKIPPED


def test_negotiable_order_from_a_newcomer_also_waits(store, filters, draft_ok):
    """Договорной заказ судится после оценки — проверка заказчика и там работает."""
    outcome = _decide(
        store, filters,
        fl_order(budget_value=None, raw={"client_registered": "меньше месяца"}),
    )
    assert outcome.status is BidStatus.SKIPPED
    assert "меньше месяца" in outcome.reason


# --- Откуда берётся срок ------------------------------------------------------


PAGE = """
<section>
    <span class="text-6">Зарегистрирован:</span>
    <span class="text-5 text-nowrap">меньше месяца</span>
</section>
"""


def test_registration_is_read_from_the_order_page():
    """Разметка снята с живой страницы FL.ru 08.08.2026."""
    assert parse_client(PAGE) == {"client_registered": "меньше месяца"}


def test_changed_markup_gives_nothing_rather_than_nonsense():
    """FL.ru переименует блок — получим пусто, а не выдуманный срок."""
    assert parse_client("<html><body>Заказчик</body></html>") == {}
    assert parse_client("") == {}


def test_empty_value_is_not_a_registration():
    assert parse_client(
        '<span>Зарегистрирован:</span> <span class="x">  </span>'
    ) == {}


# --- Признак должен пережить следующий проход ленты ---------------------------


def test_client_data_survives_the_next_feed_pass(store):
    """Лента про заказчика не знает — и не должна затирать то, что мы узнали.

    Раньше UPDATE клал raw из ленты поверх сохранённого, и признак «заказчик
    на бирже меньше месяца» жил около минуты: до следующего прохода RSS.
    Отсев новичка при этом просто не срабатывал никогда.
    """
    from fbid import pipeline

    store.upsert_order(fl_order())                                  # лента
    store.upsert_order(fl_order(raw={"client_registered": "меньше месяца"}))  # страница заказа
    for _ in range(3):
        store.upsert_order(fl_order())                              # лента снова

    order = pipeline._bid_candidates(store, Source.FL)[0][0]
    assert newcomer(order) == "меньше месяца"


def test_new_client_data_wins_over_the_old(store):
    """Заказчик перестал быть новичком — обновление проходит."""
    store.upsert_order(fl_order(raw={"client_registered": "меньше месяца"}))
    store.upsert_order(fl_order(raw={"client_registered": "больше года"}))
    from fbid import pipeline

    assert newcomer(pipeline._bid_candidates(store, Source.FL)[0][0]) == ""


def test_other_raw_keys_are_kept(store):
    """Слияние не должно терять чужие ключи: там цена отклика и данные Profi."""
    store.upsert_order(fl_order(raw={"reviews": 3}))
    store.upsert_order(fl_order(raw={"client_registered": "меньше месяца"}))
    row = store.conn.execute("SELECT raw FROM orders WHERE ext_id = '1'").fetchone()
    import json

    saved = json.loads(row["raw"])
    assert saved["reviews"] == 3
    assert saved["client_registered"] == "меньше месяца"

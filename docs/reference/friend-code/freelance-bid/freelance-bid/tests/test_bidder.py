"""Тесты предохранителей автоотклика.

Проверяем не «работает ли отправка», а «не отправится ли лишнего»: каждый
пропущенный тест здесь — это списанные с баланса Profi.ru деньги.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fbid import bidder as bidder_module
from fbid.bidder import Bidder
from fbid.filters import CommonRules, FilterSet, SourceRules
from fbid.llm import BidDraft
from fbid.models import BidStatus, Order, Source
from fbid.store import Store

# Заход длиной с настоящий: в bidder есть порог MIN_REPLY_CHARS, и обрубок
# в 20 символов там честно отсеивается как «на такой отклик не тратимся».
REPLY = (
    "Здравствуйте! Я из студии UniqOre. По вашей задаче: соберём каталог с выгрузкой "
    "в 1С, обычно укладываемся в 3-4 дня, оплата 50/50. Заявки принимаем на почту "
    "или сразу в бот — как удобнее?"
)

GOOD_DRAFT = BidDraft(
    verdict="берём",
    reason="",
    label="Тест, лендинг",
    reply=REPLY,
    estimate="8-15к",
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
        {
            "fl": SourceRules(categories_allow=["Сайты"]),
            "profi": SourceRules(categories_allow=["Программист"], max_bid_price=300),
        },
    )


@pytest.fixture()
def draft_ok(monkeypatch):
    monkeypatch.setattr(bidder_module, "generate_draft", lambda order, strict=False: GOOD_DRAFT)


# Бюджет по умолчанию выбран так, чтобы свежий заказ по нашему профилю проходил
# порог мгновенного отклика (7 из 10). Тесты про рейтинг задают сумму явно.
DEFAULT_BUDGET = 45_000.0


def fl_order(**kwargs) -> Order:
    data = {
        "source": Source.FL,
        "ext_id": "1",
        "url": "https://www.fl.ru/projects/1/x.html",
        "title": "Нужен сайт",
        "category": "Сайты / Лендинги",
        "description": "Нужен лендинг для студии",
        "budget_value": DEFAULT_BUDGET,
        "published_at": datetime.now(timezone.utc),
    }
    data.update(kwargs)
    return Order(**data)


def profi_order(**kwargs) -> Order:
    data = {
        "source": Source.PROFI,
        "ext_id": "900",
        "url": "https://profi.ru/backoffice/n.php?o=900",
        "title": "Программист",
        "category": "Программист",
        "description": "Нужен сайт-каталог",
        "budget_value": DEFAULT_BUDGET,
        "bid_price": 42.0,
        "published_at": datetime.now(timezone.utc),
    }
    data.update(kwargs)
    return Order(**data)


def _decide(store, filters, order, **kwargs):
    pk, _ = store.upsert_order(order)
    return Bidder(store, filters).decide(order, pk, **kwargs), pk


# --- Базовый путь ----------------------------------------------------------

def test_dry_run_never_plans_a_send(store, filters, draft_ok, monkeypatch):
    """DRY_RUN=true по умолчанию — отправки не планируется никогда."""
    outcome, _ = _decide(store, filters, fl_order())
    assert outcome.status is BidStatus.DRY_RUN
    assert outcome.text == GOOD_DRAFT.reply


def test_plans_send_when_dry_run_off(store, filters, draft_ok, monkeypatch):
    from fbid import config

    monkeypatch.setenv("DRY_RUN", "false")
    config.get_settings.cache_clear()
    try:
        outcome, _ = _decide(store, filters, fl_order())
        assert outcome.status is BidStatus.PLANNED
    finally:
        config.get_settings.cache_clear()


# --- Стоп-кран и дедуп -----------------------------------------------------

def test_pause_blocks_everything(store, filters, draft_ok):
    bidder = Bidder(store, filters)
    bidder.pause()
    pk, _ = store.upsert_order(fl_order())
    outcome = bidder.decide(fl_order(), pk)
    assert outcome.status is BidStatus.SKIPPED
    assert "стоп-кран" in outcome.reason

    bidder.resume()
    assert bidder.decide(fl_order(), pk).status is BidStatus.DRY_RUN


def test_never_bids_twice_on_same_order(store, filters, draft_ok):
    order = fl_order()
    outcome, pk = _decide(store, filters, order)
    Bidder(store, filters).record(order, pk, outcome)

    second = Bidder(store, filters).decide(order, pk)
    assert second.status is BidStatus.SKIPPED
    assert "уже откликались" in second.reason


# --- Вердикт модели --------------------------------------------------------

def test_verdict_ask_never_auto_sends(store, filters, monkeypatch):
    """«Уточнить» — решает человек, автоотклик по нему не уходит.

    Отказ окончательный: заход уже показан владельцу, а повторная генерация
    того же заказа — деньги в Claude API впустую.
    """
    monkeypatch.setattr(
        bidder_module,
        "generate_draft",
        lambda order, strict=False: BidDraft("уточнить", "мало деталей", "", "Что именно нужно?", ""),
    )
    outcome, _ = _decide(store, filters, fl_order())
    assert outcome.status is BidStatus.REJECTED
    assert "уточнить" in outcome.reason


def test_verdict_reject_skips(store, filters, monkeypatch):
    monkeypatch.setattr(
        bidder_module,
        "generate_draft",
        lambda order, strict=False: BidDraft("не берём", "видеомонтаж", "", "", ""),
    )
    outcome, _ = _decide(store, filters, fl_order())
    assert outcome.status is BidStatus.REJECTED
    assert "не берём" in outcome.reason


def test_empty_reply_never_sent(store, filters, monkeypatch):
    """Пустой заход на Profi.ru = списанные деньги за ничто."""
    monkeypatch.setattr(
        bidder_module, "generate_draft", lambda order, strict=False: BidDraft("берём", "", "x", "   ", "")
    )
    outcome, _ = _decide(store, filters, fl_order())
    assert outcome.status is BidStatus.REJECTED


def test_generation_failure_skips(store, filters, monkeypatch):
    monkeypatch.setattr(bidder_module, "generate_draft", lambda order, strict=False: None)
    outcome, _ = _decide(store, filters, fl_order())
    assert outcome.status is BidStatus.SKIPPED
    assert "сгенерировать" in outcome.reason


# --- Деньги на Profi.ru ----------------------------------------------------

def test_unknown_balance_blocks_paid_bid(store, filters, draft_ok):
    """Баланс не прочитали — не тратим. Fail-closed."""
    outcome, _ = _decide(store, filters, profi_order(), balance=None)
    assert outcome.status is BidStatus.SKIPPED
    assert "баланс" in outcome.reason


def test_price_above_cap_blocked(store, filters, draft_ok):
    outcome, _ = _decide(store, filters, profi_order(bid_price=350.0), balance=5000)
    assert outcome.status is BidStatus.REJECTED
    assert "потолок" in outcome.reason


def test_unknown_price_blocked_but_retriable(store, filters, draft_ok):
    """Цена не прочиталась из-за сбоя связи: не откликаемся, но и не хороним заказ."""
    outcome, pk = _decide(store, filters, profi_order(bid_price=None), balance=5000)
    assert outcome.status is BidStatus.SKIPPED
    Bidder(store, filters).record(profi_order(bid_price=None), pk, outcome)
    assert store.has_bid(pk) is False


def test_daily_budget_exhausted(store, filters, draft_ok):
    for _ in range(5):
        store.add_spend(Source.PROFI, 200.0)  # 1000 ₽ = весь суточный бюджет
    outcome, _ = _decide(store, filters, profi_order(), balance=5000)
    assert outcome.status is BidStatus.SKIPPED
    assert "бюджет" in outcome.reason


def test_min_balance_guard(store, filters, draft_ok):
    """Баланс не должен уйти ниже страховочного минимума.

    По умолчанию остаток равен нулю (решение владельца: тратим до конца),
    поэтому здесь он задаётся явно — тест проверяет сам предохранитель,
    а не текущую настройку.
    """
    from fbid import limits

    limits.set_value(store, "profi_min_balance", 500)
    outcome, _ = _decide(store, filters, profi_order(bid_price=200.0), balance=600.0)
    assert outcome.status is BidStatus.SKIPPED
    assert "минимум" in outcome.reason


def test_zero_min_balance_allows_spending_to_the_last_ruble(store, filters, draft_ok):
    """Настройка владельца: остаток 0 — тратим до нуля."""
    outcome, _ = _decide(store, filters, profi_order(bid_price=200.0), balance=200.0)
    assert outcome.status is BidStatus.DRY_RUN


def test_paid_bid_passes_when_all_clear(store, filters, draft_ok):
    outcome, _ = _decide(store, filters, profi_order(), balance=5000)
    assert outcome.status is BidStatus.DRY_RUN


def test_daily_count_limit(store, filters, draft_ok):
    for _ in range(15):
        store.add_spend(Source.PROFI, 10.0)
    outcome, _ = _decide(store, filters, profi_order(), balance=5000)
    assert outcome.status is BidStatus.SKIPPED
    assert "лимит" in outcome.reason


# --- Учёт ------------------------------------------------------------------

def test_spend_recorded_only_on_send(store, filters, draft_ok):
    order = profi_order()
    outcome, pk = _decide(store, filters, order, balance=5000)
    Bidder(store, filters).record(order, pk, outcome)

    # DRY_RUN не тратит денег, но занимает слот дедупа.
    assert store.spend_today(Source.PROFI) == (0, 0.0)
    assert store.has_bid(pk) is True


# --- Двухфазный отклик -----------------------------------------------------

def test_opener_is_cut_on_sentence_boundary():
    """Первая фаза на FL видна клиенту в списке — обрыв на полуслове недопустим."""
    from fbid.sources.fl import opening_part

    full = "Здравствуйте. Делали похожее. Вопрос по приёму заявок: форма или бот? Покажу примеры."
    opener = opening_part(full, 40)
    assert opener.endswith(".")
    assert len(opener) <= 40
    assert full.startswith(opener)


def test_short_text_is_not_cut():
    from fbid.sources.fl import opening_part

    assert opening_part("Короткий заход.", 220) == "Короткий заход."


def test_two_phase_cannot_be_switched_on(store, filters, draft_ok, monkeypatch):
    """Досыл текста в чат не включается даже строкой в .env.

    Две причины сразу. На бирже софт отправляет один отклик и замолкает —
    досыл это второе сообщение клиенту. И сам досыл был сломан: на FL он
    целился в `#noteTextarea`, то есть в личную заметку по заказу, а наружу
    рапортовал «текст ушёл клиенту».
    """
    from fbid import config
    from fbid.models import BidStatus as BS

    monkeypatch.setenv("FL_TWO_PHASE", "true")
    monkeypatch.setenv("PROFI_TWO_PHASE", "true")
    config.get_settings.cache_clear()

    order = fl_order()
    pk, _ = store.upsert_order(order)
    bidder = Bidder(store, filters)
    bidder.record(order, pk, _sent_outcome(BS))

    assert bidder.two_phase(Source.FL) is False
    assert bidder.two_phase(Source.PROFI) is False
    assert store.pending_messages(0) == [], "в очередь досыла ничего попадать не должно"


def test_stale_follow_up_queue_is_cleared_not_sent(store, filters, draft_ok):
    """Отклики, помеченные к досылу прежней версией, снимаем, а не отправляем."""
    from fbid.models import BidStatus as BS

    order = fl_order(ext_id="960", url="https://www.fl.ru/projects/960/x.html")
    pk, _ = store.upsert_order(order)
    bidder = Bidder(store, filters)
    bidder.record(order, pk, _sent_outcome(BS))
    # Так выглядела запись, оставленная прежней версией: отклик занял очередь,
    # а текст ему ещё не досылали.
    store.mark_bid_placed(pk)
    store.conn.execute("UPDATE bids SET message_sent_at = NULL WHERE order_pk = ?", (pk,))
    store.conn.commit()
    assert len(store.pending_messages(0)) == 1

    sent: list = []

    class Adapter:
        source_enum = Source.FL

        def send_message(self, *_a, **_kw):
            sent.append(1)
            return True

    assert bidder.flush_pending_messages(Adapter(), context=None) == 0
    assert sent == [], "клиенту при этом ничего не пишем"
    assert store.pending_messages(0) == []


def _sent_outcome(BS):
    from fbid.bidder import BidOutcome

    return BidOutcome(BS.SENT, "ок", cost=0.0, text=GOOD_DRAFT.reply, draft=GOOD_DRAFT)


def test_pending_skips_orders_without_text(store, filters, draft_ok):
    """Отклик без текста в очередь на досыл не попадает — слать нечего."""
    from fbid.bidder import BidOutcome
    from fbid.models import BidStatus as BS

    order = fl_order(ext_id="77", url="https://www.fl.ru/projects/77/x.html")
    pk, _ = store.upsert_order(order)
    Bidder(store, filters).record(order, pk, BidOutcome(BS.SENT, "ок", text="   "))
    assert store.pending_messages(0) == []


# --- Слежение за ответами клиентов ----------------------------------------

def test_conversation_region_excludes_page_noise():
    """Читаем только переписку: число откликов других фрилансеров меняется само."""
    from fbid.models import Source
    from fbid.replies import CONVERSATION_END, CONVERSATION_START

    assert "Ваш отклик" in CONVERSATION_START[Source.FL]
    assert "Похожие заказы" in CONVERSATION_END


# --- Счётчик сообщений вместо хэша страницы -------------------------------

def test_count_messages_counts_timestamps():
    from fbid.replies import count_messages

    convo = ("Ваш отклик\n05.08.2026 в 21:11\nЗдравствуйте, мы студия.\n"
             "05.08.2026 в 22:30\nАнна: да, интересно.")
    assert count_messages(convo) == 2


def test_count_messages_stable_against_page_noise():
    """Реклама, счётчики и «был в сети» не должны менять число сообщений."""
    from fbid.replies import count_messages

    base = "Ваш отклик\n05.08.2026 в 21:11\nТекст отклика."
    noisy = base + "\nВ сети 3 минуты назад\n120 просмотров\nРеклама"
    assert count_messages(base) == count_messages(noisy) == 1


def test_count_messages_empty():
    from fbid.replies import count_messages

    assert count_messages("") == 0


def test_unknown_verdict_goes_to_human_not_silently_dropped():
    """Опечатка модели в вердикте не должна тихо съедать нормальный заказ."""
    from fbid.llm import VERDICT_ASK, normalize_verdict

    assert normalize_verdict("Берём") == "берём"
    assert normalize_verdict("возможно") == VERDICT_ASK
    assert normalize_verdict(None) == VERDICT_ASK
    assert normalize_verdict("") == VERDICT_ASK


# --- Ограничение длины сообщения Telegram ---------------------------------

def test_telegram_message_is_capped():
    """Сообщение длиннее 4096 символов Telegram не принимает."""
    from fbid.notify import TELEGRAM_LIMIT, fit_to_limit

    out = fit_to_limit("а" * 6000)
    assert len(out) <= TELEGRAM_LIMIT
    assert "обрезано" in out


def test_telegram_short_message_untouched():
    from fbid.notify import fit_to_limit

    assert fit_to_limit("короткое") == "короткое"


def test_telegram_cap_closes_open_tags():
    """Обрезка не должна оставлять незакрытый HTML — Telegram отвергнет такое."""
    from fbid.notify import fit_to_limit

    out = fit_to_limit("<blockquote>" + "я" * 5000)
    assert out.count("</blockquote>") == 1


def test_permanent_rejection_is_not_retried(store, filters, monkeypatch):
    """Отказ по фильтрам окончательный: повторно в Claude API заказ не уходит."""
    calls = []
    monkeypatch.setattr(bidder_module, "generate_draft",
                        lambda order, strict=False: calls.append(order) or GOOD_DRAFT)
    order = fl_order(category="Дизайн / Логотипы", title="Логотип", description="Нарисовать лого")
    outcome, pk = _decide(store, filters, order)
    assert outcome.status is BidStatus.REJECTED
    Bidder(store, filters).record(order, pk, outcome)
    assert store.has_bid(pk) is True
    assert calls == []  # до генерации дело не дошло


def test_transient_skip_is_retried(store, filters, draft_ok):
    """Стоп-кран это временно: после снятия заказ снова доступен."""
    bidder = Bidder(store, filters)
    bidder.pause()
    order = fl_order()
    pk, _ = store.upsert_order(order)
    outcome = bidder.decide(order, pk)
    assert outcome.status is BidStatus.SKIPPED
    bidder.record(order, pk, outcome)
    assert store.has_bid(pk) is False  # повтор разрешён

    bidder.resume()
    assert bidder.decide(order, pk).status is BidStatus.DRY_RUN


# --- Процентный потолок цены отклика --------------------------------------

def test_bid_cap_scales_with_budget():
    """Дорогой заказ оправдывает дорогой отклик, дешёвый — нет."""
    from fbid.filters import FilterSet, SourceRules

    rules = SourceRules()
    # 2% от 50 000 ₽ = 1000 ₽, но абсолютный потолок 800 ₽ ниже — он и побеждает.
    assert FilterSet.effective_max_bid_price(rules, 50_000) == 800
    # 2% от 20 000 ₽ = 400 ₽
    assert FilterSet.effective_max_bid_price(rules, 20_000) == 400
    # 2% от 2 000 ₽ = 40 ₽ — на такой заказ отклик за 380 ₽ не пройдёт
    assert FilterSet.effective_max_bid_price(rules, 2_000) == 40


def test_bid_cap_without_budget_uses_absolute_limit():
    from fbid.filters import FilterSet, SourceRules

    assert FilterSet.effective_max_bid_price(SourceRules(), None) == 800


def test_yaml_can_only_tighten_percentage_cap():
    from fbid.filters import FilterSet, SourceRules

    assert FilterSet.effective_max_bid_price(SourceRules(max_bid_price=300), 50_000) == 300


def test_expensive_order_now_passes_where_flat_cap_blocked_it():
    """Заказ за 50к с откликом 650 ₽: плоский потолок 250 ₽ его резал, процентный пропускает."""
    from fbid.filters import CommonRules, FilterSet, SourceRules

    filters = FilterSet(
        CommonRules(keywords_any=["сайт"], max_age_hours=None, min_budget=20000),
        {"profi": SourceRules(categories_allow=["Программист"])},
    )
    order = profi_order(bid_price=650.0, budget_value=50_000, description="Редизайн сайта")
    assert filters.decide(order).passed is True


def test_cheap_order_with_expensive_bid_is_blocked():
    from fbid.filters import CommonRules, FilterSet, SourceRules

    filters = FilterSet(
        CommonRules(keywords_any=["сайт"], max_age_hours=None),
        {"profi": SourceRules(categories_allow=["Программист"])},
    )
    order = profi_order(bid_price=380.0, budget_value=2_000, description="Правка сайта")
    decision = filters.decide(order)
    assert decision.passed is False
    assert "% от бюджета" in decision.reason


# --- Оценка бюджета для договорных ----------------------------------------

def test_negotiable_order_judged_by_our_estimate(store, filters, monkeypatch):
    """Договорной заказ, который мы оцениваем ниже порога, не проходит."""
    from fbid.llm import BidDraft

    filters.common.min_budget = 20000
    monkeypatch.setattr(
        bidder_module,
        "generate_draft",
        lambda order, strict=False: BidDraft("берём", "", "лендинг", REPLY, "от 8к",
                               price_from=8000, price_to=12000),
    )
    outcome, _ = _decide(store, filters, fl_order(budget_value=None))
    assert outcome.status is BidStatus.REJECTED
    assert "наша оценка" in outcome.reason


def test_negotiable_order_passes_when_estimate_is_high(store, filters, monkeypatch):
    from fbid.llm import BidDraft

    filters.common.min_budget = 20000
    monkeypatch.setattr(
        bidder_module,
        "generate_draft",
        lambda order, strict=False: BidDraft("берём", "", "CRM", REPLY, "от 45к",
                               price_from=45000, price_to=70000),
    )
    outcome, _ = _decide(store, filters, fl_order(budget_value=None))
    assert outcome.status is BidStatus.DRY_RUN


def test_negotiable_without_estimate_is_rejected(store, filters, monkeypatch):
    """Модель не смогла оценить работу — вслепую не откликаемся."""
    from fbid.llm import BidDraft

    filters.common.min_budget = 20000
    monkeypatch.setattr(
        bidder_module,
        "generate_draft",
        lambda order, strict=False: BidDraft("берём", "", "x", REPLY, "", price_from=0),
    )
    outcome, _ = _decide(store, filters, fl_order(budget_value=None))
    assert outcome.status is BidStatus.REJECTED
    assert "оценить работу не вышло" in outcome.reason


# --- Воронка ---------------------------------------------------------------

def test_funnel_groups_similar_reasons():
    """«бюджет 5000 ₽ ниже…» и «бюджет 10000 ₽ ниже…» — одна причина, не две."""
    from fbid.stats import _short_reason

    assert _short_reason("бюджет 5000 ₽ ниже минимума") == "низкий бюджет"
    assert _short_reason("бюджет 10000 ₽ ниже минимума") == "низкий бюджет"
    assert _short_reason("категория «Дизайн» в чёрном списке") == "не та категория"
    assert (
        _short_reason("рейтинг 5.6 из 10 ниже порога 7.0 — ждёт вечернего добора")
        == _short_reason("рейтинг 6.4 из 10 ниже порога 7.0 — ждёт вечернего добора")
    )


def test_funnel_counts_and_reply_rate(store):
    from fbid.models import BidStatus as BS
    from fbid.stats import collect

    for i, status in enumerate([BS.REJECTED, BS.REJECTED, BS.SENT, BS.SENT]):
        order = fl_order(ext_id=str(900 + i), url=f"https://www.fl.ru/projects/{900+i}/x.html")
        pk, _ = store.upsert_order(order)
        store.record_bid(pk, status, reason="бюджет 100 ₽ ниже минимума", cost_rub=50.0)
        # В бою Bidder.record пишет обе книги сразу: отклик и расход. Тест
        # обязан делать так же, иначе он проверяет несуществующее состояние.
        if status is BS.SENT:
            store.add_spend(order.source, 50.0)
    # Ответ клиента — это подтверждённая отметка, а не счётчик сообщений:
    # Profi.ru дописывает под откликом свой блок, и он тоже увеличивал счётчик.
    store.set_state("clientreplied:fl:900", "1")  # клиент правда ответил
    store.set_state("convo:fl:901", "3")  # сообщений больше, но ответа не было

    f = collect(store, days=7)
    assert f.sent == 2
    assert f.rejected == 2
    assert f.replies == 1
    assert f.reply_rate == 50.0
    assert f.spent == 100.0
    assert f.cost_per_reply == 100.0
    # В причины отсева попадают только отказы, отправленные отклики туда не идут.
    assert f.reasons == [("низкий бюджет", 2)]


def test_funnel_empty_store_does_not_divide_by_zero(store):
    from fbid.stats import collect, render

    f = collect(store, days=7)
    assert f.reply_rate == 0.0
    assert f.cost_per_reply is None
    assert "Заказов просмотрено: 0" in render(f, 7)


# --- Телеграм-бот: кнопки -------------------------------------------------

def test_parse_action():
    from fbid.tgbot import parse_action

    assert parse_action("stats:7") == __import__("fbid.tgbot", fromlist=["Action"]).Action("stats", "7")
    assert parse_action("status").code == "status"
    assert parse_action("").code == ""


def test_keyboard_has_all_buttons():
    from fbid.tgbot import MENU, keyboard

    kb = keyboard()["inline_keyboard"]
    codes = {b["callback_data"] for row in kb for b in row}
    assert codes == {code for row in MENU for _, code in row}
    assert "stats:1" in codes and "pause" in codes and "resume" in codes


def test_button_pause_and_resume(store):
    from fbid.bidder import Bidder
    from fbid.tgbot import Action, handle

    text, _ = handle(Action("pause"), store)
    assert "остановлен" in text.lower()
    assert Bidder(store).is_paused() is True

    text, _ = handle(Action("resume"), store)
    assert "стоп-кран снят" in text.lower() or "отклики снова" in text.lower()
    assert Bidder(store).is_paused() is False


def test_button_stats_returns_report(store):
    from fbid.tgbot import Action, handle

    text, _ = handle(Action("stats", "1"), store)
    assert "Заказов просмотрено" in text


def test_button_status_shows_limits(store):
    from fbid.tgbot import Action, handle

    text, _ = handle(Action("status"), store)
    assert "Состояние" in text and "Площадки" in text


def test_unknown_button_does_not_crash(store):
    from fbid.tgbot import Action, handle

    text, markup = handle(Action("нечто"), store)
    assert "Не понял" in text
    assert markup["inline_keyboard"]


# --- Приоритет заказов ----------------------------------------------------

def test_fresh_order_beats_old_one():
    """Суточный лимит откликов конечен — тратим его на свежие заказы."""
    from datetime import timedelta

    from fbid.pipeline import attractiveness

    fresh = fl_order(published_at=datetime.now(timezone.utc))
    old = fl_order(published_at=datetime.now(timezone.utc) - timedelta(hours=8))
    assert attractiveness(fresh) > attractiveness(old)


def test_bigger_budget_wins():
    from fbid.pipeline import attractiveness

    assert attractiveness(fl_order(budget_value=100000)) > attractiveness(fl_order(budget_value=20000))


def test_first_in_queue_beats_tenth():
    """Profi показывает, каким по счёту будет наш отклик. Первым быть лучше."""
    from fbid.pipeline import attractiveness

    first = profi_order(raw={"bid_rank": 1})
    tenth = profi_order(raw={"bid_rank": 10})
    assert attractiveness(first) > attractiveness(tenth)


def test_client_with_reviews_ranks_higher():
    from fbid.pipeline import attractiveness

    known = profi_order(raw={"reviews": 8, "phone_confirmed": True})
    empty = profi_order(raw={"reviews": 0})
    assert attractiveness(known) > attractiveness(empty)


def test_attractiveness_handles_missing_data():
    """Заказ без даты, бюджета и данных о клиенте не должен ронять сортировку."""
    from fbid.pipeline import attractiveness

    bare = fl_order(published_at=None, budget_value=None)
    assert attractiveness(bare) >= 0


# --- Горячие и отложенные заказы ------------------------------------------

def test_hot_order_goes_out_immediately(store, filters, draft_ok):
    """Свежий заказ на 45 000 ₽ по нашему профилю — отклик сразу."""
    outcome, _ = _decide(store, filters, fl_order())
    assert outcome.status is BidStatus.DRY_RUN
    assert outcome.score is not None and outcome.score.is_hot(7.0)


def test_cold_order_waits_for_evening(store, filters, draft_ok):
    outcome, pk = _decide(store, filters, fl_order(budget_value=20000))
    assert outcome.status is BidStatus.SKIPPED
    assert "рейтинг" in outcome.reason and "вечернего добора" in outcome.reason

    # Отложенный заказ не похоронен: повтор разрешён.
    Bidder(store, filters).record(fl_order(budget_value=20000), pk, outcome)
    assert store.has_bid(pk) is False


def test_evening_sweep_takes_the_cold_one(store, filters, draft_ok):
    """Вечером порог опускается, и отложенный заказ получает свой шанс."""
    order = fl_order(budget_value=20000)
    pk, _ = store.upsert_order(order)
    assert Bidder(store, filters).decide(order, pk).status is BidStatus.SKIPPED
    assert Bidder(store, filters).decide(order, pk, min_score=0).status is BidStatus.DRY_RUN


def test_cold_order_with_known_budget_costs_no_generation(store, filters, monkeypatch):
    """Холодный заказ с понятной суммой не должен уходить к модели вообще."""
    calls = []
    monkeypatch.setattr(
        bidder_module, "generate_draft", lambda order, strict=False: calls.append(order) or GOOD_DRAFT
    )
    _decide(store, filters, fl_order(budget_value=20000))
    assert calls == []


def test_evening_sweep_hour_switches_mode(monkeypatch):
    from datetime import datetime as dt

    from fbid import config
    from fbid.pipeline import evening_sweep_now
    from fbid.store import MSK

    monkeypatch.setenv("EVENING_SWEEP_HOUR", "21")
    config.get_settings.cache_clear()
    try:
        assert evening_sweep_now(dt(2026, 8, 6, 14, 0, tzinfo=MSK)) is False
        assert evening_sweep_now(dt(2026, 8, 6, 21, 30, tzinfo=MSK)) is True
    finally:
        config.get_settings.cache_clear()


# --- Кэш заходов: одна генерация на заказ ---------------------------------

def test_draft_is_generated_once_per_order(store, filters, monkeypatch):
    """Отложенный заказ не должен заново заказывать текст на каждом проходе."""
    calls = []
    monkeypatch.setattr(
        bidder_module, "generate_draft", lambda order, strict=False: calls.append(order) or GOOD_DRAFT
    )
    order = fl_order(budget_value=None)
    pk, _ = store.upsert_order(order)

    Bidder(store, filters).decide(order, pk)
    Bidder(store, filters).decide(order, pk)
    Bidder(store, filters).decide(order, pk, min_score=0)
    assert len(calls) == 1


def test_broken_cache_falls_back_to_generation(store, filters, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bidder_module, "generate_draft", lambda order, strict=False: calls.append(order) or GOOD_DRAFT
    )
    order = fl_order()
    pk, _ = store.upsert_order(order)
    store.save_draft(pk, "{это не json")

    outcome = Bidder(store, filters).decide(order, pk)
    assert outcome.status is BidStatus.DRY_RUN
    assert len(calls) == 1


# --- Один заказчик — один отклик ------------------------------------------

def test_second_order_from_same_client_is_skipped(store, filters, draft_ok):
    first = fl_order(ext_id="1", client_key="u777")
    outcome, pk = _decide(store, filters, first)
    Bidder(store, filters).record(first, pk, outcome)

    second = fl_order(ext_id="2", title="Совсем другая задача про бота", client_key="u777")
    outcome2, _ = _decide(store, filters, second)
    assert outcome2.status is BidStatus.REJECTED
    assert "этому заказчику уже писали" in outcome2.reason


def test_near_duplicate_order_is_skipped(store, filters, draft_ok):
    """Площадка заказчика не раскрыла — ловим по тексту заявки."""
    first = fl_order(ext_id="1", title="Нужен лендинг для стоматологии")
    outcome, pk = _decide(store, filters, first)
    Bidder(store, filters).record(first, pk, outcome)

    twin = fl_order(ext_id="2", title="Нужен лендинг для стоматологии срочно")
    outcome2, _ = _decide(store, filters, twin)
    assert outcome2.status is BidStatus.REJECTED
    assert "почти такой же заказ" in outcome2.reason


def test_different_order_from_unknown_client_passes(store, filters, draft_ok):
    first = fl_order(ext_id="1", title="Нужен лендинг для стоматологии")
    outcome, pk = _decide(store, filters, first)
    Bidder(store, filters).record(first, pk, outcome)

    other = fl_order(ext_id="2", title="Телеграм-бот для записи в автосервис")
    outcome2, _ = _decide(store, filters, other)
    assert outcome2.status is BidStatus.DRY_RUN


def test_client_dedup_is_per_platform(store, filters, draft_ok):
    """Один и тот же заказ на двух биржах — это две разные возможности."""
    fl = fl_order(ext_id="1", title="Нужен лендинг для стоматологии")
    outcome, pk = _decide(store, filters, fl)
    Bidder(store, filters).record(fl, pk, outcome)

    profi = profi_order(ext_id="2", title="Нужен лендинг для стоматологии")
    outcome2, _ = _decide(store, filters, profi, balance=5000)
    assert outcome2.status is BidStatus.DRY_RUN


# --- Правила площадок в решении -------------------------------------------

def test_short_reply_never_costs_a_bid(store, filters, monkeypatch):
    monkeypatch.setattr(
        bidder_module,
        "generate_draft",
        lambda order, strict=False: BidDraft("берём", "", "x", "Здравствуйте, сделаем.", ""),
    )
    outcome, _ = _decide(store, filters, profi_order(), balance=5000)
    assert outcome.status is BidStatus.REJECTED
    assert "слишком короткий" in outcome.reason


def test_final_price_check_uses_percentage_cap_too(store, filters, draft_ok):
    """На кнопку оплаты уходит тот же потолок, что и в фильтре, а не голый абсолютный.

    Иначе отклик за 700 ₽ на заказе за 20 000 ₽ (3.5% вместо разрешённых 2%)
    прошёл бы последнюю проверку, хотя фильтр его не пускал.
    """
    filters.per_source["profi"] = SourceRules(
        categories_allow=["Программист"], max_bid_pct=2.0
    )
    order = profi_order(budget_value=20_000, bid_price=700.0)
    bidder = Bidder(store, filters)

    assert bidder.max_bid_price(order) == 400  # 2% от 20 000
    outcome, _ = _decide(store, filters, order, balance=5000)
    assert outcome.status is BidStatus.REJECTED
    assert "потолок 400" in outcome.reason


def test_cheap_bid_on_rich_order_passes_the_final_check(store, filters, draft_ok):
    filters.per_source["profi"] = SourceRules(
        categories_allow=["Программист"], max_bid_pct=2.0
    )
    order = profi_order(budget_value=100_000, bid_price=700.0)
    assert Bidder(store, filters).max_bid_price(order) == 800  # упёрлись в потолок из .env
    outcome, _ = _decide(store, filters, order, balance=5000)
    assert outcome.status is BidStatus.DRY_RUN


# --- Срок и цена в форме FL ------------------------------------------------

def test_fl_form_gets_price_and_days(store, filters, monkeypatch):
    """На FL цена и срок — отдельные поля; пустые читаются как «не понял объём»."""
    from fbid.models import BidStatus

    draft = BidDraft("берём", "", "лендинг", REPLY, "от 45к", price_from=45000,
                     price_to=70000, days=6)
    monkeypatch.setattr(bidder_module, "generate_draft", lambda order, strict=False: draft)
    monkeypatch.setenv("DRY_RUN", "false")
    from fbid import config

    config.get_settings.cache_clear()
    try:
        captured = {}

        class FakeAdapter:
            name = "fl"
            source_enum = Source.FL

            def submit_bid(self, context, order, text, **kwargs):
                from fbid.models import SubmitResult

                captured.update(kwargs)
                return SubmitResult(True, price=order.bid_price, charged=True)

        order = fl_order()
        pk, _ = store.upsert_order(order)
        bidder = Bidder(store, filters)
        outcome = bidder.decide(order, pk)
        assert outcome.status is BidStatus.PLANNED

        bidder.execute(FakeAdapter(), None, order, outcome, pk)
        assert captured["client_price"] == 45000
        assert captured["days"] == 6
    finally:
        config.get_settings.cache_clear()


def test_days_fallback_when_model_did_not_say():
    from fbid.llm import estimate_days

    # Сроки сжаты 07.08.2026: на бирже рядом стоят отклики с двумя-тремя днями,
    # и лишняя неделя в поле срока стоит заказа.
    assert estimate_days(0) == 3
    assert estimate_days(15_000) == 3
    assert estimate_days(45_000) == 5
    assert estimate_days(90_000) == 10
    assert estimate_days(5_000_000) == 30
    # Срок всегда положительный: пустое поле формы хуже приблизительного.
    assert all(estimate_days(price) > 0 for price in (0, 1, 10**9))


# --- Деньги: что записываем в расход ----------------------------------------

def test_client_price_never_lands_in_spend(store, filters, monkeypatch):
    """Цена нашей работы — не списание.

    Адаптер FL однажды вернул её как price, и в суточный расход уехало 28 000 ₽
    вместо нуля: площадка закрылась на весь день по бюджету.
    """
    from fbid import bidder as bidder_module
    from fbid.bidder import BidOutcome
    from fbid.models import SubmitResult

    order = fl_order()
    bidder = Bidder(store, filters)
    outcome = BidOutcome(BidStatus.PLANNED, "план", cost=0.0)
    told = SubmitResult(True, price=28000.0, charged=True)

    sent = []
    monkeypatch.setattr(bidder_module.notify, "send", lambda text: sent.append(text))

    assert bidder._charged(order, told, outcome) == 0.0
    assert sent and "Странное списание" in sent[0]


def test_platform_price_wins_when_it_is_believable(store, filters):
    """Списание с кнопки площадки вернее плана: карточка бывает суточной давности."""
    from fbid.bidder import BidOutcome
    from fbid.models import SubmitResult

    order = profi_order()
    bidder = Bidder(store, filters)
    outcome = BidOutcome(BidStatus.PLANNED, "план", cost=42.0)

    assert bidder._charged(order, SubmitResult(True, price=120.0, charged=True), outcome) == 120.0
    assert bidder._charged(order, SubmitResult(True, charged=True), outcome) == 42.0


# --- FL: форма требует срок и цену ------------------------------------------

def test_fl_never_submits_without_price_and_days():
    """Пустые обязательные поля = кнопка молча не срабатывает.

    Так и вышло с заказом «прототип ИИ-чат-бота»: бот отчитался об отправке,
    а на заказе отклика не оказалось. Теперь отказ происходит ДО клика —
    отклик из пакета не сгорает.
    """
    from fbid.sources.fl import FlSource

    class Field:
        def click(self): pass
        def input_value(self): return "текст"

    class Page:
        def __init__(self): self.clicked = False
        def wait_for_timeout(self, _ms): pass
        def query_selector(self, sel): return Field() if sel == FlSource.TEXTAREA else None
        def content(self): return "uniqorepro" + "." * 3000
        def inner_text(self, _sel): return ""
        def fill(self, *a): self.clicked = True

    source = FlSource()
    page = Page()

    class Context:
        def __init__(self):
            self.pages = [page]

    import fbid.sources.fl as fl_module
    original = fl_module._open_with_retries
    fl_module._open_with_retries = lambda *a, **kw: None
    try:
        result = source.submit_bid(Context(), fl_order(), "Здравствуйте, готовы взяться.")
    finally:
        fl_module._open_with_retries = original

    assert result.sent is False
    assert result.charged is False
    assert "срок" in result.reason


# --- Деньги в отчёте и деньги в предохранителе — одни и те же ------------------


def test_report_money_comes_from_the_ledger(store):
    """Цена нашей работы, попавшая в стоимость отклика, не должна врать в отчёте.

    7 августа в стоимость одного отклика на FL записались 28 000 ₽ — цена
    работы, а не списание площадки. Отчёт считал по откликам и сообщал
    «потрачено 28 491 ₽» там, где площадки списали 491 ₽. Суточный
    предохранитель при этом смотрел в другую книгу и видел правду.
    """
    from fbid.models import BidStatus as BS
    from fbid.models import Source
    from fbid.stats import collect

    order = fl_order(ext_id="950", url="https://www.fl.ru/projects/950/x.html")
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BS.SENT, reason="", cost_rub=28_000.0)   # мусор в стоимости
    store.add_spend(Source.FL, 0.0)                               # площадка не списала ничего

    assert collect(store, days=7).spent == 0.0


def test_report_and_guard_agree(store):
    """Отчёт обязан говорить о тех же деньгах, что и суточный предохранитель."""
    from fbid.models import BidStatus as BS
    from fbid.models import Source
    from fbid.stats import collect

    order = fl_order(ext_id="951", url="https://www.fl.ru/projects/951/x.html")
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BS.SENT, reason="", cost_rub=999.0)
    store.add_spend(Source.PROFI, 42.0)

    _, guard_sees = store.spend_today(Source.PROFI)
    assert collect(store, days=7).spent == guard_sees == 42.0


def test_spent_is_counted_per_source(store):
    from fbid.models import Source
    from fbid.stats import collect

    store.add_spend(Source.PROFI, 100.0)
    store.add_spend(Source.FL, 0.0)
    assert collect(store, days=7, source=Source.PROFI).spent == 100.0
    assert collect(store, days=7, source=Source.FL).spent == 0.0


def test_yesterday_does_not_leak_into_todays_spend(store):
    """Сводка «за сутки» не должна прихватывать вчерашний расход.

    Списания пишутся московской датой, а окно отчёта считалось по UTC. Ночью,
    с полуночи до трёх по Москве, эти даты расходятся на сутки — и вчерашние
    деньги попадали в сегодняшние, удваивая цифру в отчёте.
    """
    from datetime import timedelta

    from fbid.models import Source
    from fbid.stats import collect
    from fbid.store import MSK, today_msk

    yesterday = (datetime.now(MSK) - timedelta(days=1)).date().isoformat()
    store.add_spend(Source.PROFI, 500.0, day=yesterday)
    store.add_spend(Source.PROFI, 42.0, day=today_msk())

    # «За сутки» — это сегодня, один день, а не сегодня плюс весь вчерашний.
    assert collect(store, days=1).spent == 42.0
    assert collect(store, days=2).spent == 542.0
    assert collect(store, days=7).spent == 542.0


# --- Заголовок-категория не должен убивать живые заказы -------------------------


def test_profi_category_title_is_not_a_twin(store, filters, draft_ok):
    """На Profi заголовок заказа — это название категории, одно на десятки заказов.

    Поиск близнецов по нему закрывал живые заказы пачками: «Программисты
    нейросетей» и «Создание сайтов на битрикс» были отброшены как дубли самих
    себя, притом что это разные заказы разных людей.
    """
    from fbid.models import BidStatus as BS

    first = profi_order(ext_id="801", title="Программисты", category="Программисты",
                        description="Нужен сайт-каталог с выгрузкой")
    pk, _ = store.upsert_order(first)
    store.record_bid(pk, BS.SENT, reason="", cost_rub=40.0)

    second = profi_order(ext_id="802", title="Программисты", category="Программисты",
                         description="Совсем другая задача: телеграм-бот для записи")
    outcome, _ = _decide(store, filters, second)
    assert outcome.status is not BidStatus.REJECTED or "уже отработан" not in outcome.reason


def test_fl_real_title_still_finds_twins(store, filters, draft_ok):
    """У FL заголовок настоящий — там защита от перевыложенного заказа работает."""
    from fbid.models import BidStatus as BS

    first = fl_order(ext_id="810", title="Нужен лендинг для стоматологии")
    pk, _ = store.upsert_order(first)
    store.record_bid(pk, BS.SENT, reason="", cost_rub=0.0)

    second = fl_order(ext_id="811", title="Нужен лендинг для стоматологии срочно")
    outcome, _ = _decide(store, filters, second)
    assert outcome.status is BidStatus.REJECTED
    assert "уже отработан" in outcome.reason


def test_category_title_is_recognised():
    from fbid.bidder import _title_is_category

    assert _title_is_category(profi_order(title="Дизайнеры", category="Дизайнеры"))
    assert _title_is_category(profi_order(title="  Дизайнеры ", category="дизайнеры"))
    assert not _title_is_category(fl_order(title="Нужен сайт", category="Сайты / Лендинги"))
    assert not _title_is_category(fl_order(title="", category=""))


# --- Зелёная галочка только за подтверждённый отклик ----------------------------


def test_unconfirmed_bid_is_not_reported_as_sent():
    """Деньги списаны, а отклика на странице нет — это не «✅ отправлен».

    Повторять такое нельзя (заплатим второй раз), но и рапортовать зелёной
    галочкой тоже: владелец решит, что отклик стоит на заказе.
    """
    from fbid import notify
    from fbid.bidder import BidOutcome
    from fbid.models import BidStatus

    outcome = BidOutcome(
        BidStatus.SENT, "биржа не подтвердила отправку, но деньги списаны",
        cost=42.0, confirmed=False,
    )
    card = notify.bid_card(profi_order(), outcome, sent=True)
    assert "не подтвердила" in card
    assert "✅" not in card
    assert "руками" in card


def test_confirmed_bid_keeps_the_green_check():
    from fbid import notify
    from fbid.bidder import BidOutcome
    from fbid.models import BidStatus

    outcome = BidOutcome(BidStatus.SENT, "отправлено", cost=42.0)
    card = notify.bid_card(profi_order(), outcome, sent=True)
    assert "✅ <b>Отклик отправлен</b>" in card


def test_charged_but_unconfirmed_still_blocks_a_retry(store, filters, draft_ok, monkeypatch):
    """Честный заголовок не должен превратиться в повторную оплату."""
    from fbid.models import BidStatus as BS

    order = profi_order(ext_id="820")
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BS.SENT, reason="биржа не подтвердила отправку, но деньги списаны")
    assert store.has_bid(pk)


# --- Разбор заказа кладёт заход в базу: об этом надо помнить -------------------

def test_decide_persists_the_draft_it_generated(tmp_path, monkeypatch):
    """`decide()` — не чистая функция: она ПИШЕТ в базу.

    Кэш готовых заходов лежит в той же строке bids, и decide() его заполняет,
    чтобы отложенный заказ не генерировался заново. Про это легко забыть — и
    забыли: батарея «только читала» боевую базу, вызывая decide() с подменённой
    моделью, и в кэш легли фальшивые тексты. Сборщик потом отправил их живым
    заказчикам за настоящие деньги.

    Тест закрепляет факт: любой вызов decide() с подменённой генерацией
    пачкает базу, и запускать такое на боевой базе нельзя.
    """
    from fbid import bidder as bidder_module
    from fbid.bidder import Bidder
    from fbid.filters import FilterSet
    from fbid.llm import VERDICT_TAKE, BidDraft
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(
            source=Source.FL, ext_id="1", url="https://fl.ru/1",
            title="Нужен сайт-каталог с корзиной",
            description="Каталог с корзиной и выгрузкой в 1С",
            category="Сайты", budget_value=60_000.0,
        )
        pk, _ = store.upsert_order(order)
        monkeypatch.setattr(
            bidder_module, "generate_draft",
            lambda _order: BidDraft(VERDICT_TAKE, "", "метка", "подложный текст", "", 1, 2, 3),
        )

        Bidder(store, FilterSet.load()).decide(order, pk, min_score=0)

        assert store.load_draft(pk), "decide() кладёт заход в базу — это и есть опасность"
        assert "подложный текст" in store.load_draft(pk)
    finally:
        store.close()


# --- Второй отклик тому же заказчику на Profi ----------------------------------


def test_a_repeated_profi_order_is_caught_by_its_description(tmp_path, monkeypatch):
    """На Profi заголовка у заказа нет — близнецов ищем по описанию.

    Заказчика Profi не раскрывает, а «заголовок» у неё всегда имя категории,
    одинаковое у десятков заказов. Обе защиты молчали, и одно задание,
    развешенное дважды, получало от нас два отклика — это читается как
    рассылка, а не как студия.
    """
    from fbid.bidder import Bidder
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        text = "Нужен лендинг для стоматологии, дизайн есть, срок две недели."
        first = Order(source=Source.PROFI, ext_id="1", url="https://profi.ru/1",
                      title="Создание сайтов", category="Создание сайтов", description=text)
        pk, _ = store.upsert_order(first)
        store.conn.execute(
            """INSERT INTO bids (order_pk, status, reason, text, cost_rub, created_at)
               VALUES (?, ?, '', 'наш отклик', 60, datetime('now'))""",
            (pk, BidStatus.SENT.value),
        )
        store.conn.commit()

        twin = Order(source=Source.PROFI, ext_id="2", url="https://profi.ru/2",
                     title="Создание сайтов", category="Создание сайтов", description=text)
        twin_pk, _ = store.upsert_order(twin)

        outcome = Bidder(store)._check_same_client(twin, twin_pk)
        assert outcome is not None, "одно задание, развешенное дважды, — это близнец"
        assert outcome.status is BidStatus.REJECTED
        assert "такой же" in outcome.reason
    finally:
        store.close()


def test_a_different_profi_order_is_not_a_twin(tmp_path):
    """А другое задание в той же категории близнецом не считается."""
    from fbid.bidder import Bidder
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        first = Order(source=Source.PROFI, ext_id="1", url="https://profi.ru/1",
                      title="Создание сайтов", category="Создание сайтов",
                      description="Нужен лендинг для стоматологии, дизайн есть.")
        pk, _ = store.upsert_order(first)
        store.conn.execute(
            """INSERT INTO bids (order_pk, status, reason, text, cost_rub, created_at)
               VALUES (?, ?, '', 'наш отклик', 60, datetime('now'))""",
            (pk, BidStatus.SENT.value),
        )
        store.conn.commit()

        other = Order(source=Source.PROFI, ext_id="2", url="https://profi.ru/2",
                      title="Создание сайтов", category="Создание сайтов",
                      description="Требуется интеграция amoCRM с телефонией и отчёты.")
        other_pk, _ = store.upsert_order(other)

        assert Bidder(store)._check_same_client(other, other_pk) is None
    finally:
        store.close()


# --- Потолок бюджета на договорных заказах -------------------------------------


def _bidder_with_budget_rules(store, *, minimum=None, ceiling=None):
    from fbid.bidder import Bidder
    from fbid.filters import CommonRules, FilterSet, SourceRules

    filters = FilterSet(
        CommonRules(min_budget=minimum, max_budget=ceiling),
        {"fl": SourceRules(), "profi": SourceRules()},
    )
    return Bidder(store, filters=filters)


def test_a_tender_sized_estimate_is_rejected(tmp_path):
    """Наша оценка выше потолка — это тендер, отклик там теряется.

    Верхний порог к оценке модели не применялся вовсе, хотя строка
    документации обещает «тот же порог, что и явный бюджет»: на договорных
    заказах мы платили за отклики на тендеры.
    """
    from fbid.llm import BidDraft
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        bidder = _bidder_with_budget_rules(store, minimum=5000, ceiling=300000)
        order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1",
                      title="Портал", description="Большой портал под ключ")
        draft = BidDraft("берём", "", "метка", "заход", "от 900к", 900000, 1200000)

        outcome = bidder._check_estimated_budget(order, draft)
        assert outcome is not None
        assert outcome.status is BidStatus.REJECTED
        assert "выше потолка" in outcome.reason
    finally:
        store.close()


def test_an_estimate_inside_the_range_passes(tmp_path):
    """А оценка в вилке проходит: тут запрещать нечего."""
    from fbid.llm import BidDraft
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        bidder = _bidder_with_budget_rules(store, minimum=5000, ceiling=300000)
        order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1",
                      title="Лендинг", description="Лендинг под ключ")
        draft = BidDraft("берём", "", "метка", "заход", "от 60к", 60000, 90000)

        assert bidder._check_estimated_budget(order, draft) is None
        assert order.raw["estimated_budget"] == 60000
    finally:
        store.close()


def test_a_missing_profi_button_is_a_verdict_not_a_failure(monkeypatch):
    """Нет кнопки отклика — заказ закрыт. Это приговор, а не сбой отправки.

    Сбой возвращает заказ в очередь: следующий проход снова платит модели за
    текст, снова открывает страницу и снова шлёт владельцу ту же тревогу — и
    так до тех пор, пока заказ не выпадет из окна свежести.
    """
    from fbid.models import BidUnavailable, Order, Source
    from fbid.sources import profi

    class Page:
        def content(self):
            return "<html>кабинет</html>"

        def wait_for_timeout(self, _ms):
            return None

    class Ctx:
        def __init__(self):
            self.pages = [Page()]

    monkeypatch.setattr(profi, "_goto", lambda page, url: None)
    monkeypatch.setattr(profi.ProfiSource, "is_logged_in", lambda self, html: True)
    monkeypatch.setattr(profi.ProfiSource, "_find_submit_button", lambda self, page: None)

    order = Order(
        source=Source.PROFI, ext_id="909",
        url="https://profi.ru/backoffice/n.php?o=909", title="Программист",
    )
    with pytest.raises(BidUnavailable):
        profi.ProfiSource().submit_bid(
            Ctx(), order, "Здравствуйте, готовы взяться.", max_price=300.0
        )


def test_a_closed_order_does_not_alert_the_owner_every_pass(tmp_path):
    """Приговор площадки — в статус UNAVAILABLE, и тревоги по нему нет."""
    from fbid.bidder import Bidder, BidOutcome
    from fbid.models import BidStatus, BidUnavailable, Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="910",
                      url="https://profi.ru/backoffice/n.php?o=910",
                      title="Программист", bid_price=40.0)
        pk, _ = store.upsert_order(order)

        class Adapter:
            name = "profi"

            def submit_bid(self, *a, **kw):
                raise BidUnavailable("кнопки отклика нет — заказ закрыт")

        planned = BidOutcome(BidStatus.PLANNED, "готово", cost=40.0, text="Заход " * 30)
        outcome = Bidder(store).execute(Adapter(), None, order, planned, pk)

        assert outcome.status is BidStatus.UNAVAILABLE, outcome.reason
        assert store.spend_today(Source.PROFI) == (0, 0.0), "денег за это не платили"
    finally:
        store.close()


# --- Шаблонный заход не должен уходить за деньги -------------------------------


def _wordy_order():
    """Заказ с описанием, в котором есть за что зацепиться."""
    return fl_order(
        title="Создание сайтов",
        description=(
            "Корпоративный сайт (сайт компании) · Платформа: WordPress · "
            "Функционал сайта: полный функционал · Контента нет · "
            "Требуется разработка двух сайтов, прошу предлагать варианты. "
            "Домен и хостинг свои, дизайн нужен простой, сроки не горят."
        ),
    )


def test_a_template_bid_is_asked_again_and_then_dropped(store, filters, monkeypatch):
    """Пять отправленных откликов подряд отличались только заголовком и ценой.

    «Делаем такое регулярно. Вижу два пути…» — так выглядит текст, написанный
    не глядя в заявку. Клиент на Profi видит рядом десяток таких, а стоит
    каждый как живой. Один переспрос модели, второй раз не платим.
    """
    from fbid.llm import BidDraft

    template = BidDraft(
        "берём", "", "",
        "Здравствуйте. По задаче «Создание сайтов» — делаем такое регулярно. "
        "Вижу два пути: быстрый запуск на готовом или сборка под вас с админкой. "
        "Ориентир 100000 ₽ и неделя. Скажите, что важнее — срок или гибкость?",
        "",
    )
    calls: list[bool] = []

    def fake(order, strict=False):
        calls.append(strict)
        return template

    monkeypatch.setattr(bidder_module, "generate_draft", fake)
    outcome, _ = _decide(store, filters, _wordy_order())

    assert calls == [False, True], "переспросить надо ровно один раз"
    assert outcome.status is BidStatus.REJECTED
    assert "шаблон" in outcome.reason


def test_a_bid_that_reads_the_task_goes_through(store, filters, monkeypatch):
    """Заход со словами из заявки — живой, его и отправляем."""
    from fbid.llm import BidDraft

    good = BidDraft(
        "берём", "", "",
        "Здравствуйте. Два сайта на WordPress, контента нет — тексты и фото готовим "
        "мы или дадите свои? От этого зависит срок. Ориентир 90 000 ₽ за оба. "
        "UniqOre, https://uniqore.ru",
        "",
    )
    calls: list[bool] = []

    def fake(order, strict=False):
        calls.append(strict)
        return good

    monkeypatch.setattr(bidder_module, "generate_draft", fake)
    outcome, _ = _decide(store, filters, _wordy_order())

    assert calls == [False], "переспрашивать не о чем"
    assert outcome.status is not BidStatus.REJECTED, outcome.reason

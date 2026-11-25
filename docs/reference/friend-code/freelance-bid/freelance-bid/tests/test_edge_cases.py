"""Краевые случаи: что произойдёт, если с биржи придёт что-то странное.

Софт работает без присмотра сутками, и любой из этих случаев рано или поздно
случится. Каждый тест здесь — ответ на вопрос «а если…», а не на «работает ли
счастливый путь».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fbid.filters import FilterSet
from fbid.models import Order, Source, parse_budget
from fbid.notify import TELEGRAM_LIMIT, bid_card, client_replied_card, order_card
from fbid.scoring import score_order

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def order(**kwargs) -> Order:
    data = {
        "source": Source.FL,
        "ext_id": "1",
        "url": "https://www.fl.ru/projects/1/x.html",
        "title": "Нужен сайт",
        "description": "Каталог",
        "category": "Сайты",
        "budget_value": 45_000.0,
        "published_at": NOW,
    }
    data.update(kwargs)
    return Order(**data)


# --- Заказы со странными данными ------------------------------------------

def test_html_in_title_does_not_break_telegram_card():
    """Заголовок с тегами не должен ломать разметку сообщения."""
    card = order_card(order(title="<b>СРОЧНО</b> сайт <script>alert(1)</script>"))
    assert "<script>" not in card
    assert "&lt;b&gt;" in card


def test_ampersand_in_title_is_escaped():
    card = order_card(order(title="Иванов & партнёры, сайт"))
    assert "&amp;" in card


def test_huge_description_fits_telegram_limit():
    card = order_card(order(description="очень длинное ТЗ. " * 2000))
    assert len(card) <= TELEGRAM_LIMIT


def test_empty_title_does_not_crash_card():
    assert order_card(order(title=""))


def test_emoji_only_title_survives():
    assert order_card(order(title="🔥🔥🔥"))


def test_card_without_any_optional_field():
    bare = Order(source=Source.FL, ext_id="9", url="https://fl.ru/x", title="Заказ")
    card = order_card(bare)
    assert "по договорённости" in card
    assert client_replied_card(bare)


# --- Рейтинг на мусорных данных -------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        {"reviews": "много"},
        {"reviews": None},
        {"bid_rank": "первый"},
        {"bid_rank": -5},
        {"phone_confirmed": "да"},
        {"estimated_budget": "около 50к"},
        {"estimated_budget": -100},
    ],
)
def test_score_survives_garbage_in_raw(raw):
    score = score_order(order(raw=raw), now=NOW)
    assert 0 <= score.total <= 10


def test_zero_budget_scores_low_but_does_not_crash():
    score = score_order(order(budget_value=0.0), now=NOW)
    assert 0 <= score.total <= 10


def test_future_publication_date_is_not_a_crash():
    """У бирж бывают часовые пояса вперёд: дата публикации «в будущем»."""
    score = score_order(order(published_at=NOW + timedelta(hours=3)), now=NOW)
    assert 0 <= score.total <= 10


def test_ancient_order_scores_but_stays_valid():
    score = score_order(order(published_at=NOW - timedelta(days=400)), now=NOW)
    assert score.freshness == 0
    assert score.total > 0


# --- Разбор бюджета --------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Заказ (Бюджет: 50 000 руб.)", 50000.0),
        ("Заказ (Бюджет: 1 000 000 руб.)", 1000000.0),
        ("Заказ без бюджета", None),
        ("Заказ (Бюджет: по договоренности)", None),
        ("", None),
    ],
)
def test_budget_parsing_edge_cases(text, expected):
    assert parse_budget(text)[1] == expected


# --- Фильтры при сломанных правилах ---------------------------------------

def test_broken_yaml_raises_instead_of_passing_everyone(tmp_path):
    """Сломанный файл правил не должен превратиться в «фильтров нет, шлём всем»."""
    bad = tmp_path / "filters.yaml"
    bad.write_text("common:\n  min_budget: [нельзя\n", encoding="utf-8")
    with pytest.raises(ValueError):
        FilterSet.load(bad)


def test_missing_file_falls_back_to_defaults(tmp_path):
    filters = FilterSet.load(tmp_path / "нет-такого.yaml")
    # Без ключевых слов и категорий заказ пройти не должен: пустые правила
    # означают «ничего не разрешено», а не «разрешено всё».
    assert not filters.decide(order()).passed


def test_empty_yaml_does_not_open_the_gates(tmp_path):
    empty = tmp_path / "filters.yaml"
    empty.write_text("", encoding="utf-8")
    assert not FilterSet.load(empty).decide(order()).passed


# --- Ответ модели ----------------------------------------------------------

def test_reversed_price_range_is_fixed_before_sending():
    """Модель перепутала «от» и «до» — форма Profi такое не примет.

    Перевёрнутую вилку не «чиним» выдумыванием потолка: цену, которой не было
    в тексте отклика, клиент всё равно прочитает как нашу. Берём названную
    моделью нижнюю границу и ставим её в оба поля — это одна цена, и она
    совпадает с тем, что клиент читает в самом отклике.
    """
    from fbid.sources.profi import ProfiSource

    class FakeInput:
        def __init__(self):
            self.value = None

        def is_visible(self):
            return True

        def fill(self, value):
            self.value = value

    fields = [FakeInput(), FakeInput()]

    class FakePage:
        def query_selector_all(self, _selector):
            return fields

        def wait_for_timeout(self, _ms):
            return None

    assert ProfiSource._fill_price_range(FakePage(), 50000, 10000) is True
    assert int(fields[0].value) <= int(fields[1].value), "перевёрнутой вилки быть не должно"
    assert fields[1].value == "50000", "потолок берём названный, а не выдуманный"


def test_absurd_price_from_model_is_dropped():
    from fbid.llm import _to_int

    assert _to_int(10**12) == 0
    assert _to_int(45000) == 45000
    assert _to_int(-5) == 0
    assert _to_int("не число") == 0


@pytest.mark.parametrize("verdict", ["берём!", "take", "", None, "возможно", "да"])
def test_unknown_verdict_never_becomes_take(verdict):
    """Непонятный ответ модели уходит человеку, а не в автоотправку."""
    from fbid.llm import VERDICT_ASK, normalize_verdict

    assert normalize_verdict(verdict) == VERDICT_ASK


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("БЕРЁМ", "берём"),
        (" берем ", "берём"),
        ("Берём", "берём"),
        ("не берем", "не берём"),
        ("НЕ БЕРЁМ", "не берём"),
        ("уточнить", "уточнить"),
    ],
)
def test_verdict_spelling_does_not_matter(verdict, expected):
    """Регистр и «е» вместо «ё» не должны терять нормальный заказ."""
    from fbid.llm import normalize_verdict

    assert normalize_verdict(verdict) == expected


def test_draft_cache_survives_broken_json():
    from fbid.llm import draft_from_json

    assert draft_from_json("{сломано") is None
    assert draft_from_json("[]") is None
    assert draft_from_json("null") is None


def test_draft_round_trip_keeps_everything():
    from fbid.llm import BidDraft, draft_from_json, draft_to_json

    draft = BidDraft("берём", "почему", "метка", "заход", "оценка", 10000, 20000)
    restored = draft_from_json(draft_to_json(draft))
    assert restored == draft


# --- Карточка отклика ------------------------------------------------------

def test_bid_card_without_draft_and_score():
    class Outcome:
        status = None
        reason = "почему-то"
        cost = 0.0
        text = ""
        draft = None
        score = None

    assert bid_card(order(), Outcome(), sent=False)


def test_bid_card_with_huge_reply_is_cut():
    class Outcome:
        reason = "ок"
        cost = 0.0
        text = "очень длинный заход. " * 1000
        draft = None
        score = None

    from fbid.notify import fit_to_limit

    assert len(fit_to_limit(bid_card(order(), Outcome(), sent=True))) <= TELEGRAM_LIMIT


# --- Браузер: один профиль — один Chromium ---------------------------------

def test_profile_lock_is_exclusive(tmp_path, monkeypatch):
    """Второй процесс не должен открыть тот же профиль: сессия биржи испортится."""
    from fbid import config
    from fbid.browser import ProfileBusy, profile_lock

    monkeypatch.setenv("PROFILES_DIR", str(tmp_path))
    config.get_settings.cache_clear()

    def second_attempt():
        with profile_lock("profi", wait=0.1):
            pass

    try:
        with profile_lock("profi"), pytest.raises(ProfileBusy):
            second_attempt()
        # Замок отпущен — профиль снова доступен.
        second_attempt()
    finally:
        config.get_settings.cache_clear()


def test_different_platforms_do_not_block_each_other(tmp_path, monkeypatch):
    from fbid import config
    from fbid.browser import profile_lock

    monkeypatch.setenv("PROFILES_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    try:
        with profile_lock("profi"), profile_lock("fl", wait=0.1):
            pass
    finally:
        config.get_settings.cache_clear()


def test_profile_busy_is_handled_like_missing_browser():
    """Занятый профиль не должен ронять проход: обработчики ловят его как «браузера нет»."""
    from fbid.browser import BrowserUnavailable, ProfileBusy

    assert issubclass(ProfileBusy, BrowserUnavailable)


# --- Карточка напоминания ---------------------------------------------------

def test_nudge_card_does_not_pretend_the_client_answered():
    """Напоминание молчуну — не ответ клиента.

    Живой случай: владелец получил «Клиент ответил — ответ готов» по заказу,
    где клиент не написал ни слова, и пошёл искать несуществующий ответ.
    """
    from fbid.dialog import DialogReply
    from fbid.notify import answer_sending_card

    answer = DialogReply(
        action="ответить", reason="клиент молчит",
        reply="Напомню о себе: если дело в цене или сроке — обсудим.",
        client_asked="Клиент не ответил, вопросов нет",
    )
    card = answer_sending_card(order(), answer, nudge=True)

    assert "Напоминаю о себе" in card
    assert "Клиент ответил" not in card
    assert "Отвечаю клиенту" not in card
    # Придуманный моделью «вопрос клиента» в напоминании показывать нельзя.
    assert "вопросов нет" not in card


def test_silent_card_says_it_is_about_silence():
    """Карточка владельцу по молчуну обязана говорить именно про молчание."""
    from fbid.notify import silent_client_card

    card = silent_client_card(order(), 1, placed="06.08 в 23:51", days_silent=2)

    assert "Клиент молчит" in card
    assert "напоминание 1 из 2" in card
    assert "Откликнулись 06.08 в 23:51" in card
    assert "2 дня" in card


def test_answer_card_shows_both_sides_of_the_exchange():
    """Модель отвечает сама — владелец должен видеть и вопрос, и ответ."""
    from fbid.dialog import DialogReply
    from fbid.notify import answer_sending_card

    answer = DialogReply(
        action="ответить", reason="", reply="Да, сделаем.", client_asked="Какой срок?",
    )
    card = answer_sending_card(order(), answer, said="А какой срок?")

    assert "Отвечаю клиенту" in card
    assert "А какой срок?" in card
    assert "Да, сделаем." in card


# --- Обрыв после клика по отправке не должен стоить второго списания ------------


class _PageBreaksAfterClick:
    """Страница, которая обрывается сразу после клика — как мобильный прокси."""

    def __init__(self):
        self.clicked = False
        self.value = ""

    def query_selector(self, sel):
        return self

    def query_selector_all(self, sel):
        return []

    def goto(self, *a, **kw):
        return None

    def wait_for_timeout(self, ms):
        if self.clicked:
            raise RuntimeError("туннель оборвался: сменился IP")

    def content(self):
        # Правдоподобная длина: живая страница FL — это десятки килобайт, и
        # адаптер по короткому ответу справедливо решает, что не отрисовалось.
        return "Мои отклики" + "." * 3000

    def input_value(self):
        return self.value

    def click(self):
        self.clicked = True

    def is_visible(self):
        return True

    def fill(self, *args):
        return None

    def press(self, *args):
        return None


def _fl_order():
    from datetime import datetime, timezone

    from fbid.models import Order, Source

    return Order(
        source=Source.FL, ext_id="1", url="https://www.fl.ru/projects/1/x.html",
        title="Нужен сайт", description="описание", category="Сайты",
        budget_value=45_000.0, published_at=datetime.now(timezone.utc),
    )


def test_fl_break_after_click_is_charged_not_retried(monkeypatch):
    """Клик прошёл — отклик из купленного пакета списан.

    Дальше связь оборвалась. Раньше исключение улетало наверх, становилось
    «ошибка отправки», заказ считался неотработанным — и на следующем проходе
    за него платили второй раз.
    """
    from fbid.sources.fl import FlSource

    source = FlSource()
    page = _PageBreaksAfterClick()
    text = "Заход длиной побольше сотни знаков, как требует регламент. " * 3

    monkeypatch.setattr(FlSource, "is_logged_in", staticmethod(lambda html: True))
    monkeypatch.setattr(
        FlSource, "_type_into",
        staticmethod(lambda p, field, value: setattr(p, "value", value)),
    )
    monkeypatch.setattr("fbid.sources.fl._open_with_retries", lambda p, url: None)

    class Ctx:
        def __init__(self, page):
            self.pages = [page]

    result = source.submit_bid(Ctx(page), _fl_order(), text, days=5, client_price=45_000.0)
    assert not result.sent
    assert result.charged is True          # значит повтора не будет
    assert "связь оборвалась" in result.reason


def test_charged_failure_closes_the_order_for_good(tmp_path):
    """Заказ, за который уже заплатили, не должен вернуться в очередь.

    Именно так bidder записывает исход «деньги ушли, подтверждения нет».
    """
    from fbid.models import BidStatus
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        pk, _ = store.upsert_order(_fl_order())
        store.record_bid(
            pk, BidStatus.SENT,
            reason="клик по отправке прошёл, дальше связь оборвалась",
            cost_rub=0.0,
        )
        assert store.has_bid(pk)
    finally:
        store.close()


# --- Обрезка по лимиту Telegram не должна ломать разметку ----------------------

def test_truncation_closes_every_open_tag():
    """Незакрытый тег — это не «текст без курсива», а отказ Telegram целиком.

    Раньше дозакрывались только три тега из списка, и <i> в него не входил —
    хотя им набрана половина подписей в карточках. Длинная карточка с курсивом
    просто не доходила.
    """
    from fbid.notify import fit_to_limit

    text = "<i>подпись " + "очень длинный текст " * 400 + "</i>"
    cut = fit_to_limit(text)

    assert len(cut) <= 4096
    assert cut.count("<i>") == cut.count("</i>")


def test_truncation_closes_tags_from_the_inside_out():
    """Вложенность обязана остаться правильной: сначала внутренний тег."""
    from fbid.notify import fit_to_limit

    text = "<blockquote><b>заголовок " + "слово " * 900 + "</b></blockquote>"
    cut = fit_to_limit(text)

    assert cut.count("<b>") == cut.count("</b>")
    assert cut.count("<blockquote>") == cut.count("</blockquote>")
    assert "</b></blockquote>" in cut, "нельзя закрывать цитату раньше жирного внутри неё"


def test_a_quote_in_the_link_does_not_break_the_card():
    """Кавычка в ссылке разваливала href, и карточка не приходила вовсе."""
    from fbid.notify import client_replied_card

    card = client_replied_card(order(url='https://fl.ru/p/1?q="х"'), said="Привет")

    assert '"х"' not in card, "кавычка обязана быть экранирована внутри href"
    assert "&quot;" in card


# --- Отчёт обязан сходиться и не выдавать наши сбои за отсев -------------------

def test_funnel_accounts_for_every_order_seen():
    """«Просмотрено 1110, отсеяно 228» без остатка читается как потеря заказов."""
    from fbid.stats import Funnel

    funnel = Funnel(seen=1000, rejected=200, skipped=50, sent=2)
    assert funnel.decided == 252
    assert funnel.dropped_early == 748
    assert funnel.decided + funnel.dropped_early == funnel.seen


def test_our_own_failure_is_not_a_filter_reason():
    """Молчащая модель — это упущенный заказ, а не правильно отсеянный."""
    from fbid.stats import is_our_failure

    assert is_our_failure("не удалось сгенерировать заход")
    assert is_our_failure("не смог прочитать баланс Profi.ru")
    assert not is_our_failure("не наш профиль")
    assert not is_our_failure("низкий бюджет")
    assert not is_our_failure("это вакансия в штат, а не заказ")


def test_dropped_early_never_goes_negative():
    """Записей о решениях может оказаться больше, чем заказов в окне.

    Заказ увидели вчера, решение приняли сегодня — окно у них разное, и
    вычитание уходило бы в минус, а на экране появилось бы «сразу мимо: -3».
    """
    from fbid.stats import Funnel

    assert Funnel(seen=1, rejected=5).dropped_early == 0


# --- Отчёт обязан говорить про один период, а не про два ----------------------

def test_report_window_matches_the_money_book():
    """Отклики и деньги обязаны считаться по одной границе суток.

    Живой случай: три отклика ушли вчера в 22:00 и стоили 1200 ₽, один сегодня
    в 10:00 за 100 ₽. Вечерняя сводка брала отклики за скользящие сутки, а
    деньги — за календарный день, и получалось «откликов 4 · потрачено 100 ₽».
    Владелец делит одно на другое и видит цену отклика в разы меньше настоящей.
    """
    from datetime import datetime

    from fbid.stats import window_start
    from fbid.store import MSK

    start = datetime.fromisoformat(window_start(1)).astimezone(MSK)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert start.date() == datetime.now(MSK).date(), "«за сутки» это сегодняшний день"


def test_week_window_starts_six_days_back():
    """«За неделю» — сегодня и шесть предыдущих дней, целыми сутками."""
    from datetime import datetime, timedelta

    from fbid.stats import window_start
    from fbid.store import MSK

    start = datetime.fromisoformat(window_start(7)).astimezone(MSK)
    assert start.date() == (datetime.now(MSK) - timedelta(days=6)).date()
    assert (start.hour, start.minute) == (0, 0)


def test_truncation_never_leaves_half_a_tag():
    """Разрез приходится в произвольное место, и однажды — внутрь тега.

    В конце остаётся «<b» или «<blockqu», и Telegram отвергает сообщение
    целиком: карточка не приходит вовсе, а выглядит это как «бот промолчал».
    """
    import re

    from fbid.notify import TELEGRAM_LIMIT, fit_to_limit

    edge = TELEGRAM_LIMIT - 80
    for shift in range(0, 14):
        text = "a" * (edge - shift) + "<blockquote>хвост</blockquote>" + "b" * 300
        cut = fit_to_limit(text)
        body = cut.split("\n\n… сообщение обрезано")[0]
        last = body.rfind("<")
        assert last < 0 or ">" in body[last:], f"обрубок тега при сдвиге {shift}: {body[-20:]!r}"
        # И разметка при этом осталась закрытой.
        assert len(re.findall(r"<blockquote>", cut)) == len(re.findall(r"</blockquote>", cut))

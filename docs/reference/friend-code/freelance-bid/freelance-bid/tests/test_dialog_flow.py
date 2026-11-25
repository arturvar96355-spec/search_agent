"""Путь ответа целиком: клиент написал → готовим → владелец жмёт → уходит.

Проверяется без сети и без браузера: модель, Telegram и площадка подменены.
Смысл в развилках — что происходит, когда модель молчит, когда зовёт человека,
когда отправка не удалась и когда владелец нажал кнопку дважды.
"""
from __future__ import annotations

import pytest

from fbid import config, dialog, notify, replies
from fbid.models import BidStatus, Order, Source
from fbid.store import Store
from fbid.tgbot import handle, parse_action


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "fbid.db")
    yield store
    store.close()


@pytest.fixture()
def order_in_db(store):
    order = Order(
        source=Source.FL,
        ext_id="5516643",
        url="https://www.fl.ru/projects/5516643/",
        title="Лендинг для клиники",
        description="Одностраничник с записью.",
    )
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BidStatus.SENT, reason="ok", text="Здравствуйте!", cost_rub=0.0)
    return order, pk


@pytest.fixture()
def sent_messages(monkeypatch):
    """Перехватываем всё, что ушло бы в Telegram."""
    box: list[dict] = []
    monkeypatch.setattr(
        notify, "send",
        lambda text, *, disable_preview=True, markup=None: box.append(
            {"text": text, "markup": markup}
        ) or True,
    )
    monkeypatch.setattr(notify, "send_alert", lambda text: box.append({"text": text}) or True)
    return box


def _answer(action=dialog.ACT_ANSWER, reply="По срокам неделя, входит вёрстка и форма записи."):
    return dialog.DialogReply(action, "клиент спросил про сроки", reply, "про сроки")


# --- Подготовка ответа ------------------------------------------------------

def test_client_answer_only_calls_the_owner(store, order_in_db, sent_messages, monkeypatch):
    """Клиент ответил — софт зовёт владельца и на этом останавливается.

    Правило владельца от 09.08.2026: на биржах софт отправляет только первый
    отклик, второе сообщение он пишет сам. Ни готового к отправке черновика,
    ни кнопки «Отправить» тут быть не должно.
    """
    order, pk = order_in_db
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())

    replies._prepare_answer(store, order, pk, "переписка")

    card = sent_messages[-1]
    assert "Клиент ответил" in card["text"]
    assert dialog.approved_orders(store) == []
    buttons = [
        b.get("callback_data", "")
        for row in (card.get("markup") or {}).get("inline_keyboard", [])
        for b in row
    ]
    assert not any(b.startswith("dlgsend") for b in buttons)


def test_auto_mode_answers_only_cheap_orders(store):
    """Модель отвечает сама только там, где сумма ниже порога.

    Правило владельца от 23.08.2026: дешёвые заказы съедают время, а цена
    ошибки в них невелика; на крупных деньгах неудачная формулировка дороже
    любой экономии времени.
    """
    from fbid import limits

    dialog.set_mode(store, dialog.MODE_AUTO)
    ceiling = limits.get("auto_reply_max_price", store)

    cheap = _fresh_bid(store, "c", ceiling - 1)
    edge = _fresh_bid(store, "e", ceiling)
    rich = _fresh_bid(store, "r", ceiling + 1)

    assert dialog.answers_itself(store, cheap) is True
    assert dialog.answers_itself(store, edge) is True, "порог включительно"
    assert dialog.answers_itself(store, rich) is False
    assert dialog.expensive_for_auto(store, rich) is True


def test_negotiable_price_is_treated_as_expensive(store):
    """Договорная цена — это «не знаем», а не «дёшево»: отвечает владелец."""
    dialog.set_mode(store, dialog.MODE_AUTO)
    vague = _fresh_bid(store, "v", None)
    assert dialog.answers_itself(store, vague) is False
    assert dialog.expensive_for_auto(store, vague) is True


def test_notify_mode_answers_nobody(store):
    """Выключенный режим сильнее порога: молчим по любой сумме."""
    dialog.set_mode(store, dialog.MODE_NOTIFY)
    cheap = _fresh_bid(store, "cn", 1000)
    assert dialog.answers_itself(store, cheap) is False
    assert dialog.expensive_for_auto(store, cheap) is False


def test_zero_threshold_disables_auto_answers(store):
    """Порог 0 — модель не отвечает никому, даже в режиме «отвечать самой»."""
    dialog.set_mode(store, dialog.MODE_AUTO)
    store.set_state("limit:auto_reply_max_price", "0")
    cheap = _fresh_bid(store, "cz", 1000)
    assert dialog.answers_itself(store, cheap) is False


def test_client_reply_is_marked(store, order_in_db, sent_messages, monkeypatch):
    """Ответ по существу означает, что клиент реально написал.

    Именно эта отметка двигает заказ во вкладку «Начали общение» — счётчику
    сообщений верить нельзя, он растёт и от приписки площадки.
    """
    order, pk = order_in_db
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())

    assert dialog.client_replied(store, order) is False
    replies._prepare_answer(store, order, pk, "переписка")
    assert dialog.client_replied(store, order) is True


def test_silence_does_not_mark_client_as_replied(store, order_in_db, sent_messages, monkeypatch):
    order, pk = order_in_db
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: dialog.DialogReply(dialog.ACT_SILENT, "последнее сообщение наше", ""),
    )

    replies._prepare_answer(store, order, pk, "переписка")
    assert dialog.client_replied(store, order) is False


def test_handover_has_no_send_button(store, order_in_db, sent_messages, monkeypatch):
    """Клиент согласился на созвон — дальше человек, отправлять нечего."""
    order, pk = order_in_db
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: dialog.DialogReply(dialog.ACT_HANDOVER, "согласился на созвон", ""),
    )

    replies._prepare_answer(store, order, pk, "переписка")

    assert dialog.load_draft(store, pk) is None
    assert "Нужен ваш ответ" in sent_messages[-1]["text"]
    assert sent_messages[-1].get("markup") is None


def test_silence_does_not_bother_the_owner(store, order_in_db, sent_messages, monkeypatch):
    """Если клиент ничего не писал, уведомления быть не должно.

    Счётчик сообщений на странице растёт не только от ответов: Profi.ru сама
    дописывает под наш отклик блок «Стоимость от … Контакты: …». Владелец
    получал «клиент написал» там, где ему никто не писал. Модель видит
    переписку целиком — если она говорит «отвечать не на что», молчим.
    """
    order, pk = order_in_db
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: dialog.DialogReply(dialog.ACT_SILENT, "последнее сообщение наше", ""),
    )

    replies._prepare_answer(store, order, pk, "переписка")

    assert dialog.load_draft(store, pk) is None
    assert sent_messages == [], "потревожили владельца на пустом месте"


def test_model_failure_does_not_swallow_the_reply(store, order_in_db, sent_messages, monkeypatch):
    """Модель упала — владелец всё равно получает «клиент написал»."""
    order, pk = order_in_db

    def boom(*_a, **_kw):
        raise RuntimeError("API недоступен")

    monkeypatch.setattr(dialog, "next_message", boom)
    replies._prepare_answer(store, order, pk, "переписка")

    assert "Клиент ответил" in sent_messages[-1]["text"]


def test_answer_with_forbidden_link_is_not_offered(store, sent_messages, monkeypatch):
    """На строгой площадке ссылка в ответе стоила бы аккаунта."""
    order = Order(
        source=Source.YOUDO, ext_id="1", url="https://youdo.com/t1", title="Бот",
        description="Нужен бот.",
    )
    pk, _ = store.upsert_order(order)
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: _answer(reply="Смотрите работы на https://uniqore.ru, там всё есть."),
    )

    replies._prepare_answer(store, order, pk, "переписка")

    assert dialog.load_draft(store, pk) is None
    assert "Клиент ответил" in sent_messages[-1]["text"]


# --- Отправка ---------------------------------------------------------------

class FakeAdapter:
    """Площадка, которая записывает отправленное вместо реальной биржи."""

    source_enum = Source.FL

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    def send_message(self, context, order, text):
        self.sent.append(text)
        return self.ok


def test_nothing_is_ever_sent_to_the_client(store, order_in_db, sent_messages):
    """Главная проверка модуля: на биржу не уходит ни одного нашего слова.

    Даже когда ответ подготовлен и помечен к отправке — а такое лежит в базе
    с прежних настроек. Правило владельца: софт отправляет первый отклик и
    дальше молчит, переписку ведёт человек.
    """
    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)
    adapter = FakeAdapter()

    assert dialog.flush_approved(store, adapter, context=None) == 0
    assert adapter.sent == [], "софт написал клиенту сам — этого быть не должно"


def test_expensive_order_answer_is_not_sent_even_if_approved(store, order_in_db, sent_messages):
    """Цена проверяется у самой двери, а не только при подготовке.

    Между подготовкой и отправкой владелец мог опустить порог, а на странице
    мог обновиться бюджет. Сообщение клиенту необратимо, поэтому последнее
    слово — за проверкой перед самой отправкой.
    """
    order, pk = order_in_db
    dialog.set_mode(store, dialog.MODE_AUTO)
    order.budget_value = 500_000  # выше любого разумного порога
    store.upsert_order(order)
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)

    adapter = FakeAdapter()
    assert dialog.flush_approved(store, adapter, context=None) == 0
    assert adapter.sent == [], "дорогому клиенту софт не пишет"
    assert dialog.approved_orders(store) == []
    assert "НЕ отправил" in sent_messages[-1]["text"]


def test_other_platform_answer_is_not_sent_either(store, order_in_db, sent_messages):
    """И через чужой адаптер тоже: дверь закрыта для всех площадок."""
    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)

    class ProfiAdapter(FakeAdapter):
        source_enum = Source.PROFI

    adapter = ProfiAdapter()
    assert dialog.flush_approved(store, adapter, context=None) == 0
    assert adapter.sent == []


def test_approval_without_draft_is_ignored(store, order_in_db, sent_messages):
    _order, pk = order_in_db
    store.set_state(dialog.approved_key(pk), "1")
    adapter = FakeAdapter()
    assert dialog.flush_approved(store, adapter, context=None) == 0
    assert dialog.approved_orders(store) == []


# --- Кнопки бота ------------------------------------------------------------

def test_old_send_button_does_not_promise_to_send(store, order_in_db):
    """Кнопка из старой карточки не должна отвечать «отправлю».

    Новых карточек с такой кнопкой уже не бывает, но в истории чата они
    лежат вечно и нажимаются в любой момент. Ответить «отправлю» и ничего не
    отправить — хуже, чем не иметь кнопки вовсе.
    """
    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())

    text, _markup = handle(parse_action(f"dlgsend:{pk}"), store)
    assert "Отправлю" not in text
    assert "из прошлой версии" in text
    assert dialog.approved_orders(store) == []
    assert dialog.load_draft(store, pk) is None


def test_old_button_without_draft_answers_the_same(store, order_in_db):
    _order, pk = order_in_db
    text, _markup = handle(parse_action(f"dlgsend:{pk}"), store)
    assert "из прошлой версии" in text


def test_skip_button_drops_the_draft(store, order_in_db):
    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)

    text, _markup = handle(parse_action(f"dlgskip:{pk}"), store)
    assert "убрал" in text
    assert dialog.load_draft(store, pk) is None
    assert dialog.approved_orders(store) == []


def test_button_with_broken_argument(store):
    text, _markup = handle(parse_action("dlgsend:чепуха"), store)
    assert "Не понял" in text


# --- Срок годности одобрения ------------------------------------------------

def test_stale_approval_is_not_sent(store, order_in_db, sent_messages):
    """Ответ, пролежавший полсуток, тем более никуда не уходит."""
    from datetime import datetime, timedelta, timezone

    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)
    old = (datetime.now(timezone.utc) - timedelta(hours=dialog.APPROVAL_TTL_HOURS + 1))
    store.set_state(dialog.approved_key(pk), old.isoformat(timespec="seconds"))

    adapter = FakeAdapter()
    assert dialog.flush_approved(store, adapter, context=None) == 0
    assert adapter.sent == []
    assert dialog.load_draft(store, pk) is None


def test_fresh_approval_is_not_sent_either(store, order_in_db, sent_messages):
    """Свежесть разрешения ничего не меняет: отправлять клиенту софт не будет."""
    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)
    assert dialog.approval_expired(store, pk) is False

    adapter = FakeAdapter()
    assert dialog.flush_approved(store, adapter, context=None) == 0
    assert adapter.sent == []


def test_old_format_approval_is_cleared(store, order_in_db, sent_messages):
    """Отметка из прежней версии кода («1») — тоже просто снимается."""
    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    store.set_state(dialog.approved_key(pk), "1")

    adapter = FakeAdapter()
    assert dialog.flush_approved(store, adapter, context=None) == 0
    assert adapter.sent == []
    assert dialog.approved_orders(store) == []


def test_expired_approval_dropped_for_any_platform(store, order_in_db, sent_messages):
    """Площадку могли выключить — тогда ответ не подберёт ни один проход.

    Чистка идёт отдельно от отправки, иначе такое разрешение висело бы вечно.
    """
    from datetime import datetime, timedelta, timezone

    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)
    old = datetime.now(timezone.utc) - timedelta(hours=dialog.APPROVAL_TTL_HOURS + 2)
    store.set_state(dialog.approved_key(pk), old.isoformat(timespec="seconds"))

    assert dialog.drop_expired(store) == 1
    assert dialog.approved_orders(store) == []
    assert dialog.load_draft(store, pk) is None
    assert "НЕ отправлен" in sent_messages[-1]["text"]


def test_fresh_approval_survives_cleanup(store, order_in_db, sent_messages):
    _order, pk = order_in_db
    dialog.save_draft(store, pk, _answer())
    dialog.approve(store, pk)

    assert dialog.drop_expired(store) == 0
    assert dialog.approved_orders(store) == [pk]


# --- Напоминание через сутки ------------------------------------------------

def _age_our_message(store, order, hours: float) -> None:
    """Делает вид, что наше сообщение отправлено `hours` часов назад."""
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    store.set_state(
        f"{dialog.LASTMSG_PREFIX}:{order.source.value}:{order.ext_id}",
        when.isoformat(timespec="seconds"),
    )


def test_nudge_not_due_right_after_bid(store, order_in_db):
    order, _pk = order_in_db
    dialog.touch_last_message(store, order)
    assert dialog.nudge_due(store, order) is False


def test_nudge_due_after_a_day_of_silence(store, order_in_db):
    order, _pk = order_in_db
    _age_our_message(store, order, 23.5)
    assert dialog.nudge_due(store, order) is True


def test_no_nudge_if_client_answered(store, order_in_db):
    """Клиент ответил — напоминать не о чем, идёт разговор."""
    order, _pk = order_in_db
    _age_our_message(store, order, 30)
    dialog.mark_client_replied(store, order)
    assert dialog.nudge_due(store, order) is False


def _age_nudge(store, order, hours: float) -> None:
    """Делает вид, что последнее касание готовили `hours` часов назад."""
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    key = f"{dialog.NUDGE_PREFIX}:{order.source.value}:{order.ext_id}"
    done = dialog.nudges_sent(store, order)
    store.set_state(key, f"{done}|{when.isoformat(timespec='seconds')}")


def test_second_nudge_waits_another_day(store, order_in_db):
    """После первого напоминания второе ждёт ещё почти сутки, а не идёт сразу."""
    order, _pk = order_in_db
    dialog.mark_nudged(store, order)

    _age_our_message(store, order, 5)
    _age_nudge(store, order, 5)
    assert dialog.nudge_due(store, order) is False

    _age_our_message(store, order, 23)
    _age_nudge(store, order, 23)
    assert dialog.nudge_due(store, order) is True


def test_second_nudge_is_measured_from_the_first(store, order_in_db):
    """Пока владелец не нажал «Отправить», сообщение никуда не ушло.

    Срок второго касания раньше отмерялся от нашего последнего СООБЩЕНИЯ, а
    его двигает только фактическая отправка. При выключенной автоотправке
    (режим по умолчанию) второе касание становилось положенным через минуту
    после первого, перезаписывало черновик — и владелец, нажимая «Отправить»
    под первой карточкой, слал клиенту прощальный текст.
    """
    order, _pk = order_in_db
    _age_our_message(store, order, 30)      # отклик ушёл 30 часов назад
    dialog.mark_nudged(store, order)        # касание 1 подготовлено только что

    assert dialog.nudge_due(store, order) is False, "второе касание не должно быть готово"

    _age_nudge(store, order, 23)            # прошли сутки после первого касания
    assert dialog.nudge_due(store, order) is True


def test_old_mark_without_time_still_works(store, order_in_db):
    """У заказов, отмеченных прежней версией, времени нет — не падаем."""
    order, _pk = order_in_db
    store.set_state(f"{dialog.NUDGE_PREFIX}:{order.source.value}:{order.ext_id}", "1")
    _age_our_message(store, order, 30)
    assert dialog.nudge_due(store, order) is True


def test_no_third_nudge_ever(store, order_in_db):
    """Два касания и всё. Третье — уже назойливость."""
    order, _pk = order_in_db
    dialog.mark_nudged(store, order)
    dialog.mark_nudged(store, order)
    _age_our_message(store, order, 100)
    assert dialog.nudge_due(store, order) is False


def test_old_flag_counts_as_one_nudge(store, order_in_db):
    """В базе с прошлой версии лежит единица-флаг: это одно напоминание, не ноль.

    Иначе после обновления всем, кому уже напомнили, прилетело бы ещё два.
    """
    order, _pk = order_in_db
    store.set_state(f"{dialog.NUDGE_PREFIX}:{order.source.value}:{order.ext_id}", "1")
    assert dialog.nudges_sent(store, order) == 1

    _age_our_message(store, order, 100)
    assert dialog.nudge_due(store, order) is True  # второе ещё положено
    dialog.mark_nudged(store, order)
    assert dialog.nudge_due(store, order) is False  # а третьего нет


def test_nudges_are_not_round_numbers():
    """Сообщение ровно через 24:00 выдаёт автомат — интервалы нарочно кривые."""
    first = dialog.nudge_delay_hours(1)
    second = dialog.nudge_delay_hours(2)
    assert first is not None and second is not None
    assert first % 1 != 0 and second % 1 != 0
    assert dialog.nudge_delay_hours(3) is None


def test_first_nudge_offers_to_discuss_price_and_deadline():
    """Первое напоминание снимает главную причину молчания: цена и срок."""
    from fbid.models import Order, Source

    order = Order(source=Source.FL, ext_id="1", url="u", title="Сайт", description="Нужен сайт")
    brief = dialog.conversation_brief(order, "наш отклик", 1, nudge=1)
    assert "цена" in brief.lower()
    assert "срок" in brief.lower()


def test_second_nudge_is_the_last_and_says_so():
    """Второе касание — прощальное, без нового захода на продажу."""
    from fbid.models import Order, Source

    order = Order(source=Source.FL, ext_id="1", url="u", title="Сайт", description="Нужен сайт")
    brief = dialog.conversation_brief(order, "наш отклик", 1, nudge=2)
    assert "последнее" in brief.lower()
    assert "цена или срок" not in brief.lower()


def test_no_nudge_without_our_message(store, order_in_db):
    order, _pk = order_in_db
    assert dialog.nudge_due(store, order) is False


def test_silent_client_only_reminds_the_owner(store, order_in_db, sent_messages, monkeypatch):
    """Клиент молчит — карточка владельцу, а не сообщение клиенту.

    Напоминание молчуну — такое же второе сообщение живому человеку, как и
    ответ, значит пишет его владелец. Модель для этого не нужна совсем: текста
    от неё не требуется, а ключ на все три системы один.
    """
    order, pk = order_in_db
    _age_our_message(store, order, 24)

    def model_must_not_be_called(*_a, **_kw):
        raise AssertionError("за напоминание платить модели не за что")

    monkeypatch.setattr(dialog, "next_message", model_must_not_be_called)

    page = _FakePage(
        "Заказы\nВаш отклик\n05.08.2026 в 21:40\n"
        "Здравствуйте! Я из студии UniqOre.\nПохожие заказы\n"
    )
    context = type("Ctx", (), {"pages": [page], "new_page": lambda self: page})()
    assert replies.nudge_silent_clients(store, context, [(order, pk)]) == 1

    assert dialog.nudged(store, order) is True
    assert "Клиент молчит" in sent_messages[-1]["text"]
    buttons = [b for row in sent_messages[-1]["markup"]["inline_keyboard"] for b in row]
    assert all("callback_data" not in b for b in buttons), "кнопки отправки быть не должно"
    # Напоминание не значит, что клиент написал.
    assert dialog.client_replied(store, order) is False


class _FakePage:
    """Страница заказа, которая всегда отдаёт один и тот же текст."""

    def __init__(self, text: str) -> None:
        self.text = text

    def goto(self, *_a, **_kw):
        return None

    def wait_for_timeout(self, *_a, **_kw):
        return None

    def wait_for_function(self, *_a, **_kw):
        return None

    def inner_text(self, _selector):
        return self.text

    def query_selector_all(self, _selector):
        return []


# --- Согласие на созвон -----------------------------------------------------

def test_call_agreement_gets_its_own_push(store, order_in_db, sent_messages, monkeypatch):
    """Согласие на созвон — отдельный заголовок, а не общее «нужен ваш ответ».

    Пропущенный час здесь стоит дороже, чем весь остальной софт.
    """
    order, pk = order_in_db
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: dialog.DialogReply(
            dialog.ACT_HANDOVER, "клиент назвал время", "", "давайте завтра в 14:00",
            ready_for_call=True,
        ),
    )

    replies._prepare_answer(store, order, pk, "переписка")

    assert "КЛИЕНТ ГОТОВ НА СОЗВОН" in sent_messages[-1]["text"]
    assert dialog.ready_for_call(store, order) is True


def test_other_handover_keeps_neutral_title(store, order_in_db, sent_messages, monkeypatch):
    """Договор и торг — тоже к человеку, но это не согласие на созвон."""
    order, pk = order_in_db
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: dialog.DialogReply(dialog.ACT_HANDOVER, "просит договор", ""),
    )

    replies._prepare_answer(store, order, pk, "переписка")

    assert "Нужен ваш ответ" in sent_messages[-1]["text"]
    assert dialog.ready_for_call(store, order) is False


def test_ready_for_call_parsed_from_model():
    reply = dialog._to_reply({
        "action": "передать", "reason": "согласился", "reply": "",
        "client_asked": "дал номер", "ready_for_call": "да",
    })
    assert reply.ready_for_call is True


# --- Сквозной сценарий развилки по деньгам --------------------------------------


def _fresh_bid(store, ext_id, price):
    """Заказ с откликом, отправленным ПРЯМО СЕЙЧАС — то есть после включения.

    Автоответ намеренно не трогает старые переписки, поэтому во всех тестах
    порога отклик должен быть свежим: иначе проверяется не порог, а отсечка.
    """
    order = Order(source=Source.FL, ext_id=ext_id, url=f"https://fl.ru/p/{ext_id}",
                  title="t", budget_value=price)
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BidStatus.SENT, reason="ok", text="Здравствуйте!", cost_rub=0.0)
    store.mark_bid_placed(pk)
    return order


def _order_priced(store, price, ext_id="9001"):
    order = Order(source=Source.FL, ext_id=ext_id, url=f"https://fl.ru/p/{ext_id}",
                  title="Лендинг", budget_value=price)
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BidStatus.SENT, reason="ok", text="Здравствуйте!", cost_rub=0.0)
    # Именно mark_message_sent, как в бою. mark_bid_placed — это вторая фаза,
    # которой больше нет ни на одной площадке: в боевой базе bid_at всегда
    # пустой. Заполняя его в тестах, мы проверяли путь, которого не бывает.
    store.mark_message_sent(pk)
    return order, pk


def test_cheap_client_gets_an_answer_from_the_model(store, sent_messages, monkeypatch):
    """Дешёвый заказ модель ведёт сама: готовит ответ и ставит его к отправке."""
    dialog.set_mode(store, dialog.MODE_AUTO)
    order, pk = _order_priced(store, 30_000)
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())

    replies._prepare_answer(store, order, pk, "переписка", said="сколько стоит?")

    assert dialog.approved_orders(store) == [pk], "ответ должен уйти клиенту"
    assert "Отвечаю клиенту" in sent_messages[-1]["text"]


def test_expensive_client_is_handed_to_the_owner(store, sent_messages, monkeypatch):
    """Дорогой заказ модель не трогает: карточка владельцу с суммой и текстом."""
    dialog.set_mode(store, dialog.MODE_AUTO)
    order, pk = _order_priced(store, 300_000, ext_id="9002")
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())

    replies._prepare_answer(store, order, pk, "переписка", said="хотим большой проект")

    assert dialog.approved_orders(store) == [], "крупному клиенту софт не пишет"
    card = sent_messages[-1]["text"]
    assert "Крупный клиент" in card
    assert "300 000 ₽" in card, "сумма должна быть видна сразу"
    assert "хотим большой проект" in card, "текст клиента обязан быть в карточке"


def test_expensive_client_ready_for_call_is_shouted_about(store, sent_messages, monkeypatch):
    """Готовность крупного клиента созвониться — самое ценное событие воронки."""
    dialog.set_mode(store, dialog.MODE_AUTO)
    order, pk = _order_priced(store, 200_000, ext_id="9003")
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: dialog.DialogReply(
            dialog.ACT_ANSWER, "готов", "текст", "давайте созвонимся", ready_for_call=True
        ),
    )

    replies._prepare_answer(store, order, pk, "переписка", said="давайте созвонимся завтра")

    card = sent_messages[-1]["text"]
    assert "ГОТОВ НА СОЗВОН" in card
    assert "200 000 ₽" in card


def test_bad_reply_from_the_model_is_not_sent_to_a_cheap_client(store, sent_messages, monkeypatch):
    """Ответ, не прошедший проверку правил, клиенту не уходит даже на дешёвом."""
    dialog.set_mode(store, dialog.MODE_AUTO)
    order, pk = _order_priced(store, 10_000, ext_id="9004")
    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: _answer(reply="Пишите на почту info@uniqore.ru"),
    )
    monkeypatch.setattr(dialog, "check_reply_rules", lambda text, order: "внешние контакты")

    replies._prepare_answer(store, order, pk, "переписка", said="как связаться?")

    assert dialog.approved_orders(store) == []
    assert "Клиент ответил" in sent_messages[-1]["text"]


def test_negotiable_card_does_not_claim_the_sum_is_too_big(store, sent_messages, monkeypatch):
    """На договорном заказе «сумма выше порога» — враньё: суммы там нет.

    Причина молчания другая, и назвать её надо точно: иначе владелец решит,
    что заказ крупный, и отнесётся к нему не так, как следует.
    """
    dialog.set_mode(store, dialog.MODE_AUTO)
    order, pk = _order_priced(store, None, ext_id="9005")
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())

    replies._prepare_answer(store, order, pk, "переписка", said="обсудим?")

    card = sent_messages[-1]["text"]
    assert "цену заказчик не назвал" in card
    assert "выше порога" not in card


def test_owner_sees_both_sides_when_the_model_answers(store, sent_messages, monkeypatch):
    """Модель ответила сама — владельцу показываем и вопрос клиента, и наш ответ.

    Кнопку он не нажимал, значит увидеть переписку должен целиком: это его
    имя и его аккаунт на бирже.
    """
    dialog.set_mode(store, dialog.MODE_AUTO)
    order, pk = _order_priced(store, 20_000, ext_id="9006")
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())

    replies._prepare_answer(store, order, pk, "переписка", said="а сроки какие?")

    card = sent_messages[-1]["text"]
    assert "а сроки какие?" in card, "вопрос клиента"
    assert "По срокам неделя" in card, "наш ответ"


def test_old_conversations_are_left_alone(store, sent_messages, monkeypatch):
    """Включили автоответ — модель не идёт в переписки, начатые до этого.

    Решение владельца от 23.08.2026. Клиент, которому мы откликнулись две
    недели назад и который тогда промолчал, получил бы сегодня сообщение ни
    к селу ни к городу — и выглядело бы это как рассылка от студии.
    """
    order, pk = _order_priced(store, 20_000, ext_id="9100")
    # Отклик ушёл ДО включения режима. Правим message_sent_at: именно его
    # заполняет боевая отправка, и именно по нему считается давность.
    store.conn.execute(
        "UPDATE bids SET message_sent_at = '2026-08-01T10:00:00+00:00', "
        "created_at = '2026-08-01T10:00:00+00:00' WHERE order_pk = ?", (pk,)
    )
    store.conn.commit()
    dialog.set_mode(store, dialog.MODE_AUTO)

    assert dialog.answers_itself(store, order) is False, "старую переписку не трогаем"

    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())
    replies._prepare_answer(store, order, pk, "переписка", said="ну что там?")
    assert dialog.approved_orders(store) == []


def test_the_cutoff_is_remembered_once(store):
    """Повторное включение не сдвигает отсечку вперёд.

    Иначе выключил-включил — и половина недавних переписок снова стала
    «старыми», а модель перестала отвечать тем, кому уже отвечала.
    """
    dialog.set_mode(store, dialog.MODE_AUTO)
    first = store.get_state(dialog.AUTO_SINCE_KEY)
    dialog.set_mode(store, dialog.MODE_NOTIFY)
    dialog.set_mode(store, dialog.MODE_AUTO)
    assert store.get_state(dialog.AUTO_SINCE_KEY) == first


def test_old_conversation_is_not_called_expensive(store, sent_messages, monkeypatch):
    """Старая переписка — не «крупный клиент». Причину называем точно.

    Раньше в «дорогие» сваливался любой отказ автоответа, и карточка писала
    «сумма выше порога» про заказ на двадцать тысяч. Владелец по такой
    карточке решает, что заказ крупный, и относится к нему не так.
    """
    order, pk = _order_priced(store, 20_000, ext_id="9200")
    store.conn.execute(
        "UPDATE bids SET message_sent_at = '2026-08-01T10:00:00+00:00' WHERE order_pk = ?", (pk,)
    )
    store.conn.commit()
    dialog.set_mode(store, dialog.MODE_AUTO)

    assert dialog.answers_itself(store, order) is False, "переписка началась до включения"
    assert dialog.expensive_for_auto(store, order) is False, "дело не в деньгах"

    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())
    replies._prepare_answer(store, order, pk, "переписка", said="ну что?")
    card = sent_messages[-1]["text"]
    assert "Крупный клиент" not in card
    assert "Клиент ответил" in card


def test_send_time_is_taken_from_the_real_send(store):
    """Отсечку меряем по времени ОТПРАВКИ, а не по первому взгляду на заказ.

    Строка в bids заводится на первом проходе (решение «отложить»), а отклик
    уходит вечером — разрыв доходит до девятнадцати часов. По created_at
    заказ, отвеченный уже после включения режима, считался старым.
    """
    order, pk = _order_priced(store, 20_000, ext_id="9201")
    # bid_at гасим тоже: иначе он подставится вместо created_at, и проверка
    # выродится в сравнение с текущим временем — тест начинал падать сам собой
    # на следующий день после написания.
    store.conn.execute(
        "UPDATE bids SET created_at = '2026-08-23T08:00:00+00:00', "
        "message_sent_at = NULL, bid_at = NULL WHERE order_pk = ?", (pk,)
    )
    store.conn.commit()
    dialog.set_mode(store, dialog.MODE_AUTO)
    store.set_state(dialog.AUTO_SINCE_KEY, "2026-08-23T12:00:00+00:00")

    assert dialog.answers_itself(store, order) is False, "по created_at заказ ещё старый"

    # Отклик реально ушёл вечером — уже после включения режима.
    store.conn.execute(
        "UPDATE bids SET message_sent_at = '2026-08-23T19:00:00+00:00' WHERE order_pk = ?", (pk,)
    )
    store.conn.commit()
    assert dialog.answers_itself(store, order) is True, "отправка была после включения"


# --- Напоминание молчуну пишет модель, пока заказ дешевле порога ---------------

def _silent_for_a_day(store, order) -> None:
    """Отматывает отсчёт так, будто клиент молчит почти сутки после отклика."""
    from datetime import datetime, timedelta, timezone

    long_ago = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    store.set_state(f"{dialog.LASTMSG_PREFIX}:{order.source.value}:{order.ext_id}", long_ago)


def _auto_mode(store, ceiling: float = 45_000) -> None:
    from fbid import limits

    dialog.set_mode(store, dialog.MODE_AUTO)
    limits.set_value(store, "auto_reply_max_price", ceiling)
    # Считаем отклик своим: отсечку по дате ставим в прошлое.
    store.set_state(dialog.AUTO_SINCE_KEY, "2000-01-01T00:00:00+00:00")


def test_cheap_order_gets_a_nudge_written_by_the_model(
    store, order_in_db, sent_messages, monkeypatch
):
    """До порога модель ведёт переписку сама — значит и напоминание пишет сама."""
    order, pk = order_in_db
    order.budget_value = 30_000.0
    store.upsert_order(order)
    _auto_mode(store)
    _silent_for_a_day(store, order)

    asked: dict = {}

    def fake_next(order_, conversation, exchanges=0, nudge=0):
        asked["nudge"] = nudge
        return _answer(reply="Если дело в цене или сроке — обсудим, подстроимся.")

    monkeypatch.setattr(dialog, "next_message", fake_next)
    monkeypatch.setattr(replies, "read_stable", lambda page, order_: "Ваш отклик\nСегодня")
    monkeypatch.setattr(replies, "read_messages", lambda page: [])

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

    sent = replies.nudge_silent_clients(store, Context(), [(order, pk)])

    assert sent == 1
    assert asked["nudge"] == 1, "модель должна знать, что это первое напоминание"
    assert dialog.nudges_sent(store, order) == 1, "касание засчитано"
    assert store.get_state(dialog.approved_key(pk)), "текст поставлен в отправку"


def test_expensive_order_still_goes_to_the_owner(store, order_in_db, sent_messages, monkeypatch):
    """Дороже порога модель клиенту не пишет ничего — даже напоминания."""
    order, pk = order_in_db
    order.budget_value = 300_000.0
    store.upsert_order(order)
    _auto_mode(store)
    _silent_for_a_day(store, order)

    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: pytest.fail("по дорогому заказу модель писать не должна"),
    )
    monkeypatch.setattr(replies, "read_stable", lambda page, order_: "Ваш отклик\nСегодня")
    monkeypatch.setattr(replies, "read_messages", lambda page: [])

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

    replies.nudge_silent_clients(store, Context(), [(order, pk)])

    assert not store.get_state(dialog.approved_key(pk)), "клиенту ничего не готовим"
    assert any("молч" in item["text"].lower() for item in sent_messages), (
        "владелец должен узнать, что клиент молчит"
    )


def test_model_may_decide_there_is_nothing_to_remind_about(
    store, order_in_db, sent_messages, monkeypatch
):
    """Клиент уже отказал — напоминание не пишем, но и возвращаться к нему не надо."""
    order, pk = order_in_db
    order.budget_value = 30_000.0
    store.upsert_order(order)
    _auto_mode(store)
    _silent_for_a_day(store, order)

    monkeypatch.setattr(
        dialog, "next_message",
        lambda *a, **kw: dialog.DialogReply(dialog.ACT_SILENT, "клиент отказал", "", ""),
    )
    monkeypatch.setattr(replies, "read_stable", lambda page, order_: "Ваш отклик\nСегодня")
    monkeypatch.setattr(replies, "read_messages", lambda page: [])

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

    replies.nudge_silent_clients(store, Context(), [(order, pk)])

    assert not store.get_state(dialog.approved_key(pk))
    assert dialog.nudges_sent(store, order) == 1, "к этому заказу больше не возвращаемся"


def test_owner_does_not_get_the_same_answer_twice(store, order_in_db, sent_messages, monkeypatch):
    """По одному ответу клиенту — одна простыня с текстом, а не две.

    Карточка «Отвечаю клиенту» уходит в момент решения и показывает текст
    целиком. Следом приходила вторая, после отправки, — с тем же текстом
    слово в слово. Две одинаковые простыни по каждому клиенту превращают чат
    в ленту, которую перестают читать.
    """
    order, pk = order_in_db
    order.budget_value = 30_000.0
    store.upsert_order(order)
    _auto_mode(store)
    # DRY_RUN гасит отправку клиенту целиком — здесь нам нужен боевой путь.
    monkeypatch.setenv("DRY_RUN", "false")
    config.get_settings.cache_clear()
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())

    replies._prepare_answer(store, order, pk, "переписка", said="сколько стоит?")

    class Adapter:
        source_enum = Source.FL

        def send_message(self, _context, _order, _text):
            return True

    dialog.flush_approved(store, Adapter(), object())

    full = [item for item in sent_messages if _answer().reply in item["text"]]
    assert len(full) == 1, "текст ответа должен прийти владельцу ровно один раз"
    assert any("ушёл клиенту" in item["text"] for item in sent_messages), "расписка нужна"


def test_nudge_counter_actually_counts(store, order_in_db):
    """Касаний ровно два. Счётчик обязан до двух доходить.

    В отметке лежит «<число>|<время>», и разбор всей строки целиком падал —
    счётчик навсегда застревал на единице. Раз он никогда не доходит до двух,
    «второе и последнее» напоминание считается положенным снова и снова:
    молчун получал его каждые сутки без конца.
    """
    order, _pk = order_in_db

    dialog.mark_nudged(store, order)
    assert dialog.nudges_sent(store, order) == 1
    assert dialog.nudge_delay_hours(2) is not None, "второе касание положено"

    dialog.mark_nudged(store, order)
    assert dialog.nudges_sent(store, order) == 2
    assert dialog.nudge_delay_hours(3) is None, "третьего касания не бывает"


def test_third_nudge_is_never_due(store, order_in_db):
    """После двух касаний заказ обязан замолчать навсегда."""
    order, _pk = order_in_db
    dialog.mark_nudged(store, order)
    dialog.mark_nudged(store, order)
    _age_nudge(store, order, 500)  # прошли недели

    assert dialog.nudge_due(store, order) is False


def test_ancient_flag_still_counts_as_one(store, order_in_db):
    """У старых записей в отметке лежала единица-флаг без времени."""
    order, _pk = order_in_db
    store.set_state(f"{dialog.NUDGE_PREFIX}:{order.source.value}:{order.ext_id}", "1")

    assert dialog.nudges_sent(store, order) == 1


def test_queued_answer_is_never_prepared_twice(store, order_in_db, sent_messages, monkeypatch):
    """Карточка владельцу не дошла — это не повод отвечать клиенту дважды.

    Возврат False просит вызывающий код откатить счётчик и разобрать переписку
    заново. Но ответ клиенту к этому моменту уже стоит в отправке, и второй
    разбор поставил бы в отправку второй — клиент получил бы два сообщения на
    один вопрос.
    """
    from fbid import notify

    order, pk = order_in_db
    order.budget_value = 30_000.0
    store.upsert_order(order)
    _auto_mode(store)
    monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _answer())
    monkeypatch.setattr(notify, "send", lambda *a, **kw: False)  # Телеграм лёг

    delivered = replies._prepare_answer(store, order, pk, "переписка", said="сколько стоит?")

    assert dialog.approved_orders(store) == [pk], "ответ поставлен в отправку"
    assert delivered is True, "счётчик откатывать нельзя — иначе ответим второй раз"


def test_a_link_in_the_conversation_is_clickable():
    """Голый домен в переписке — это не ссылка: клиент до портфолио не дойдёт.

    У отклика дописка https:// была, у переписки — нет, хотя именно здесь
    модель и шлёт ссылку: в ответ на «покажите примеры».
    """
    from fbid.models import Order, Source

    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
    reply = dialog._to_reply({
        "action": "ответить",
        "reason": "показываю примеры",
        "reply": "Вот наши работы: uniqore.ru/demo/klinika/ — посмотрите.",
    }, order)
    assert "https://uniqore.ru/demo/klinika/" in reply.reply


def test_a_strict_platform_text_is_not_touched_before_the_check():
    """На Юду правка до проверки сделала бы защиту от бана слепой.

    check_reply_rules отказывается отправлять текст, если чистить пришлось.
    Если бы мы правили ссылку заранее, «чистить пришлось» перестало бы
    срабатывать — а за ссылку в переписке там блокируют аккаунт на месяц.
    """
    from fbid.models import Order, Source

    order = Order(source=Source.YOUDO, ext_id="1", url="https://youdo.com/1", title="Сайт")
    raw = "Вот наши работы: uniqore.ru — посмотрите."
    reply = dialog._to_reply({"action": "ответить", "reason": "примеры", "reply": raw}, order)
    assert reply.reply == raw
    assert dialog.check_reply_rules(reply.reply, order), "текст со ссылкой на Юду обязан отказать"


def test_a_conversation_handed_over_stays_with_the_owner(tmp_path):
    """«Модель передала разговор» до сих пор означало только карточку.

    На следующее сообщение клиента модель отвечала снова — поверх того, что
    владелец успел написать сам. Клиент видел в одной переписке двух разных
    собеседников.
    """
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(
            source=Source.PROFI, ext_id="1", url="https://profi.ru/1",
            title="Сайт", budget_value=20000.0,
        )
        dialog.set_mode(store, dialog.MODE_AUTO)
        assert not dialog.owner_leads(store, order)

        dialog.mark_owner_leads(store, order, "клиент дошёл до договора")
        assert dialog.owner_leads(store, order)
        assert dialog.answers_itself(store, order) is False
    finally:
        store.close()

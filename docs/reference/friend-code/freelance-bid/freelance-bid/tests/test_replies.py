"""Тесты слежения за ответами клиентов.

Здесь два одинаково плохих исхода, и оба проверяем:
  * ложная тревога — «клиент написал», когда никто не писал;
  * тихая слепота — клиент написал, а владелец не узнал.
"""
from __future__ import annotations

import pytest

from fbid import notify, replies
from fbid.models import BidStatus, Order, Source
from fbid.replies import ReplyCheck, check_replies, count_messages, orders_awaiting_reply
from fbid.store import Store

# Как выглядит страница заказа на Profi под нашей сессией (сокращённо).
PAGE_ONE_MESSAGE = """Заказы
Чаты
Детали заказа
05.08.2026 в 21:40
Здравствуйте! Я из студии UniqOre. По вашей задаче готовы взяться.
Похожие заказы
"""

PAGE_TWO_MESSAGES = """Заказы
Чаты
Детали заказа
05.08.2026 в 21:40
Здравствуйте! Я из студии UniqOre. По вашей задаче готовы взяться.
Сегодня в 09:12
Добрый день, расскажите подробнее про сроки.
Похожие заказы
"""


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def profi_order(ext_id: str = "1") -> Order:
    return Order(
        source=Source.PROFI,
        ext_id=ext_id,
        url=f"https://profi.ru/backoffice/n.php?o={ext_id}",
        title="Нужен сайт",
    )


class FakePage:
    """Страница заказа: отдаёт заранее заданный текст."""

    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.calls = 0

    def goto(self, *_args, **_kwargs):
        return None

    def wait_for_function(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, *_args, **_kwargs):
        return None

    def inner_text(self, _selector):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        if text is None:
            raise RuntimeError("страница не открылась")
        return text


class FakeContext:
    def __init__(self, page):
        self.pages = [page]

    def new_page(self):
        return self.pages[0]


# --- Счётчик сообщений -----------------------------------------------------

def test_counts_messages_by_timestamps():
    assert count_messages(PAGE_ONE_MESSAGE) == 1
    assert count_messages(PAGE_TWO_MESSAGES) == 2


def test_empty_conversation_counts_zero():
    assert count_messages("") == 0
    assert count_messages("совсем другая страница") == 0


def test_counter_ignores_page_noise():
    """Счётчики просмотров и «был в сети» не должны считаться сообщениями."""
    noisy = PAGE_ONE_MESSAGE + "\n5 просмотров\nбыл в сети 3 минуты назад\n"
    assert count_messages(noisy) == count_messages(PAGE_ONE_MESSAGE)


# --- Первый проход молчит --------------------------------------------------

def test_first_pass_only_remembers(store, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or True)

    context = FakeContext(FakePage(PAGE_ONE_MESSAGE))
    stats = check_replies(store, context, [(profi_order(), 1)])

    assert stats.new_replies == 0
    assert sent == [], "на первом проходе уведомлять не о чем"


def test_new_message_triggers_notification(store, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or True)
    order = profi_order()

    check_replies(store, FakeContext(FakePage(PAGE_ONE_MESSAGE)), [(order, 1)])
    stats = check_replies(store, FakeContext(FakePage(PAGE_TWO_MESSAGES)), [(order, 1)])

    assert stats.new_replies == 1
    assert len(sent) == 1
    assert "Клиент ответил" in sent[0]


def test_same_page_twice_is_silent(store, monkeypatch):
    """Перезагрузка страницы без новых сообщений — не повод дёргать владельца."""
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or True)
    order = profi_order()

    check_replies(store, FakeContext(FakePage(PAGE_ONE_MESSAGE)), [(order, 1)])
    check_replies(store, FakeContext(FakePage(PAGE_ONE_MESSAGE)), [(order, 1)])
    check_replies(store, FakeContext(FakePage(PAGE_ONE_MESSAGE)), [(order, 1)])

    assert sent == []


def test_shrinking_conversation_does_not_alert(store, monkeypatch):
    """Биржа что-то свернула — счётчик упал. Это не ответ клиента."""
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or True)
    order = profi_order()

    check_replies(store, FakeContext(FakePage(PAGE_TWO_MESSAGES)), [(order, 1)])
    stats = check_replies(store, FakeContext(FakePage(PAGE_ONE_MESSAGE)), [(order, 1)])

    assert stats.new_replies == 0
    assert sent == []


def test_legacy_hash_in_state_is_replaced_quietly(store, monkeypatch):
    """В базе мог остаться старый формат (хэш) — это не повод слать тревогу."""
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent.append(text) or True)
    order = profi_order()
    store.set_state(f"convo:{order.source.value}:{order.ext_id}", "a1b2c3d4hash")

    stats = check_replies(store, FakeContext(FakePage(PAGE_TWO_MESSAGES)), [(order, 1)])
    assert stats.new_replies == 0
    assert sent == []


# --- Тихая слепота ---------------------------------------------------------

def test_unreadable_page_is_reported_not_swallowed(store, monkeypatch):
    """Если переписки перестали читаться, владелец должен об этом узнать."""
    alerts = []
    monkeypatch.setattr(notify, "send_alert", lambda text: alerts.append(text) or True)
    monkeypatch.setattr(notify, "send", lambda text, **kw: True)

    context = FakeContext(FakePage(None))
    stats = check_replies(store, context, [(profi_order(), 1)])

    assert stats.checked == 0
    assert stats.errors
    assert alerts and "не отслеживаются" in alerts[0]


def test_blind_alert_is_not_repeated_every_five_minutes(store, monkeypatch):
    alerts = []
    monkeypatch.setattr(notify, "send_alert", lambda text: alerts.append(text) or True)
    monkeypatch.setattr(notify, "send", lambda text, **kw: True)

    for _ in range(3):
        check_replies(store, FakeContext(FakePage(None)), [(profi_order(), 1)])
    assert len(alerts) == 1, "повторные жалобы каждые пять минут — это спам"


def test_partial_failure_does_not_raise_alarm(store, monkeypatch):
    """Одна страница не открылась, другая прочиталась — это не слепота."""
    alerts = []
    monkeypatch.setattr(notify, "send_alert", lambda text: alerts.append(text) or True)
    monkeypatch.setattr(notify, "send", lambda text, **kw: True)

    stats = ReplyCheck(checked=3, new_replies=0, errors=["один заказ"])
    replies._warn_if_blind(store, stats)
    assert alerts == []


# --- Кого вообще проверяем -------------------------------------------------

def test_only_orders_with_sent_bids_are_watched(store):
    sent_order = profi_order("100")
    skipped_order = profi_order("200")
    pk_sent, _ = store.upsert_order(sent_order)
    pk_skipped, _ = store.upsert_order(skipped_order)
    store.record_bid(pk_sent, BidStatus.SENT, reason="ушёл")
    store.record_bid(pk_skipped, BidStatus.SKIPPED, reason="отложен")

    watched = orders_awaiting_reply(store, Source.PROFI)
    assert [o.ext_id for o, _ in watched] == ["100"]


def test_old_bids_drop_out_of_watching(store):
    """Заказ месячной давности проверять бессмысленно — он давно закрыт."""
    from datetime import datetime, timedelta, timezone

    order = profi_order("300")
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BidStatus.SENT, reason="ушёл")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE bids SET created_at = ? WHERE order_pk = ?", (old, pk))
    store.conn.commit()

    assert orders_awaiting_reply(store, Source.PROFI) == []


def test_watch_list_is_capped(store):
    for i in range(40):
        pk, _ = store.upsert_order(profi_order(str(1000 + i)))
        store.record_bid(pk, BidStatus.SENT, reason="ушёл")

    watched = orders_awaiting_reply(store, Source.PROFI)
    assert len(watched) <= replies.WATCH_LIMIT


# --- Приписка площадки не должна читаться как ответ клиента ------------------

# Так выглядит переписка сразу после нашего отклика: Profi.ru дописывает под ним
# цену из анкеты и контакты. Владельцу при этом никто не писал.
OWN_REPLY_ONLY = """Ваш отклик
Сегодня в 21:27
Здравствуйте! Мы студия UniqOre, https://uniqore.ru, визитки на Tilda собираем часто.
По деньгам такая визитка выходит 30 тысяч, оплата 50/50. Заявки принимаем формой
на почту или сразу в телеграм?
Сегодня в 21:27
Стоимость от 25000 до 35000 руб. / усл.
Контакты:
+375-44-576-42-74
"""

WITH_CLIENT_ANSWER = OWN_REPLY_ONLY + """Сегодня в 22:10
артем: Давайте телеграм. А сроки какие?
"""


def test_platform_footer_is_not_a_message():
    """Приписка площадки под нашим откликом — не сообщение клиента."""
    from fbid.replies import count_messages

    assert count_messages(OWN_REPLY_ONLY) == 1


def test_real_client_answer_is_counted():
    from fbid.replies import count_messages

    assert count_messages(WITH_CLIENT_ANSWER) == 2


def test_footer_without_contacts_also_ignored():
    from fbid.replies import count_messages

    text = "Ваш отклик\nСегодня в 21:27\nЗдравствуйте!\nСегодня в 21:27\nСтоимость от 12 000 до 20 000 руб. / усл.\n"
    assert count_messages(text) == 1


# --- Кто написал: мы или клиент ---------------------------------------------

def _msg(group: str, text: str) -> dict:
    return {"group": group, "text": text}


def test_owner_message_is_ours_not_the_client():
    """Владелец написал клиенту руками — это наша сторона.

    Живой случай: владелец сам написал «когда будет удобно созвониться?»,
    а система приняла это за ответ клиента и полезла отвечать на него.
    Биржа кладёт наши сообщения в одну группу отправителя, клиента — в другую.
    """
    messages = [
        _msg("816413206-0", "Здравствуйте. Мы студия UniqOre, задача ясна."),
        _msg("816413206-0", "когда будет удобно созвониться?"),
    ]
    lines = replies.label_messages(messages, own_text="Здравствуйте. Мы студия UniqOre")

    assert [line.ours for line in lines] == [True, True]
    assert replies.client_wrote_last(lines) is False


def test_client_group_is_recognised():
    messages = [
        _msg("816413206-0", "Здравствуйте. Мы студия UniqOre, задача ясна."),
        _msg("777000111-0", "Здравствуйте, интересует срок."),
    ]
    lines = replies.label_messages(messages, own_text="Здравствуйте. Мы студия UniqOre")

    assert [line.ours for line in lines] == [True, False]
    assert replies.client_wrote_last(lines) is True


def test_first_message_is_ours_when_bid_text_is_lost():
    """Текст отклика не сохранился — но первым в переписке всегда пишем мы."""
    messages = [_msg("a", "Наш отклик"), _msg("b", "Ответ клиента")]
    lines = replies.label_messages(messages, own_text="")
    assert [line.ours for line in lines] == [True, False]


def test_transcript_has_signatures():
    lines = replies.label_messages(
        [_msg("a", "наш текст"), _msg("b", "вопрос клиента")], own_text="наш текст"
    )
    text = replies.transcript(lines)
    assert "Мы: наш текст" in text
    assert "Клиент: вопрос клиента" in text


def test_no_messages_means_no_labels():
    """Разметка биржи поменялась — работаем по старому пути, а не падаем."""
    assert replies.label_messages([], own_text="что-то") == []
    assert replies.client_wrote_last([]) is False


# --- Сверка с площадкой -----------------------------------------------------

def test_missing_bid_is_noticed():
    """Отклик числится отправленным, а на странице его нет — это тревога."""
    assert not replies.bid_is_on_the_page(
        "страница без нашего текста", "Здравствуйте, мы UniqOre", []
    )


def test_bid_found_by_our_own_text():
    page = "Ваш отклик\nЗдравствуйте, мы UniqOre и делаем ботов"
    assert replies.bid_is_on_the_page(page, "Здравствуйте, мы UniqOre и делаем ботов", [])


def test_bid_found_by_our_message_in_chat():
    lines = [replies.Line(True, "наш отклик")]
    assert replies.bid_is_on_the_page("что угодно", "совсем другой текст", lines)


def test_without_our_text_we_do_not_cry_wolf():
    """Текста отклика в базе нет — судить не по чему, тревожить владельца нельзя."""
    assert replies.bid_is_on_the_page("любая страница", "", []) is True


# --- Ложные тревоги «отклик пропал» --------------------------------------------


def _order_row(store):
    from fbid.models import BidStatus, Order, Source

    order = Order(source=Source.PROFI, ext_id="9001",
                  url="https://profi.ru/backoffice/n.php?o=9001", title="Программист")
    pk, _ = store.upsert_order(order)
    store.record_bid(pk, BidStatus.SENT, reason="", text="Здравствуйте. Мы студия UniqOre.",
                     cost_rub=40.0)
    return order, pk


def test_reply_survives_a_failed_notification(store, monkeypatch):
    """Телеграм ответил «слишком часто» — сообщение клиента не должно пропасть.

    Счётчик сообщений переписывался до подготовки ответа. Один сбой на
    уведомлении — и следующий проход видел «сколько было, столько и стало»:
    ни ответа клиенту, ни карточки владельцу, и никто об этом не узнавал.
    """
    order = profi_order()
    check_replies(store, FakeContext(FakePage(PAGE_ONE_MESSAGE)), [(order, 1)])

    def too_many(*_args, **_kwargs):
        raise RuntimeError("Too Many Requests: retry after 30")

    monkeypatch.setattr(replies, "_prepare_answer", too_many)
    check_replies(store, FakeContext(FakePage(PAGE_TWO_MESSAGES)), [(order, 1)])

    # Следующий проход — телеграм отпустило, ответ обязан подготовиться.
    seen = []
    monkeypatch.setattr(replies, "_prepare_answer", lambda *a, **kw: seen.append(a))
    stats = check_replies(store, FakeContext(FakePage(PAGE_TWO_MESSAGES)), [(order, 1)])

    assert stats.new_replies == 1, "ответ клиента должен найтись со второй попытки"
    assert seen, "ответ так и не подготовили"


def test_unreadable_conversation_is_not_a_missing_bid(store, monkeypatch):
    """Переписку не разобрали — судить не по чему.

    9 августа так пришли три ложные тревоги подряд, и одна потом сама
    «вернулась на место»: страница просто не догрузилась.
    """
    from fbid import notify, replies

    order, _pk = _order_row(store)
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **kw: sent.append(a))

    replies._warn_if_bid_missing(store, order, "какой-то текст", "Здравствуйте. Мы студия", [])
    assert sent == []


def test_missing_bid_never_bothers_the_owner(store, monkeypatch):
    """Про пропавший отклик владельцу не сообщаем совсем.

    Решение владельца от 09.08.2026: заказ забрал другой исполнитель, заказчик
    его снял, биржа отправила в архив — повлиять всё равно нельзя, а лишние
    тревоги приучают не читать уведомления вообще.
    """
    from fbid import notify, replies
    from fbid.replies import Line

    order, _pk = _order_row(store)
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **kw: sent.append(a))
    lines = [Line(False, "сообщение клиента")]

    for _ in range(5):
        replies._warn_if_bid_missing(store, order, "чужой текст", "Здравствуйте. Мы студия", lines)
    assert sent == []


def test_our_own_message_in_the_thread_counts_as_present(store, monkeypatch):
    from fbid import notify, replies
    from fbid.replies import Line

    order, _pk = _order_row(store)
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **kw: sent.append(a))

    replies._warn_if_bid_missing(store, order, "страница", "наш текст", [Line(True, "наш отклик")])
    assert sent == []


# --- Карточка владельцу: текст клиента и время ---------------------------------


def test_owner_sees_the_clients_own_words(store, monkeypatch):
    """В карточке — само сообщение клиента, а не пересказ одной строкой.

    Отвечает владелец, и решение «бежать сейчас или после обеда» он принимает
    по тексту: «сколько стоит» и «нам это больше не нужно» требуют разного.
    Раньше за текстом приходилось идти на биржу.
    """
    from fbid import notify, replies
    from fbid.replies import Line

    lines = [
        Line(True, "Здравствуйте, мы студия UniqOre"),
        Line(False, "Здравствуйте! А сколько будет стоить с интеграцией оплаты?"),
    ]
    assert replies.last_client_message(lines) == (
        "Здравствуйте! А сколько будет стоить с интеграцией оплаты?"
    )

    card = notify.client_replied_card(profi_order(), said=replies.last_client_message(lines))
    assert "интеграцией оплаты" in card
    assert "Здравствуйте, мы студия" not in card, "наше сообщение в карточку не идёт"


def test_last_client_message_is_empty_when_only_we_wrote(store):
    from fbid import replies
    from fbid.replies import Line

    assert replies.last_client_message([Line(True, "наш отклик")]) == ""
    assert replies.last_client_message([]) == ""


def test_reply_card_shows_the_time():
    """Без времени непонятно, ответ пришёл час назад или вчера."""
    import re

    from fbid import notify

    card = notify.client_replied_card(profi_order(), said="привет")
    assert re.search(r"\d{2}:\d{2}", card), "на карточке должно быть время"


def test_long_title_is_cut_on_a_word():
    """«интервью за возн» читается как опечатка и подрывает доверие к карточке."""
    from fbid import notify

    long_order = profi_order()
    long_order.title = (
        "Нужны эксперты по внешнеэкономической деятельности и международным "
        "расчётам для серии интервью за вознаграждение"
    )
    card = notify.client_replied_card(long_order, said="ок")
    body = card.split("\n")[2]
    assert body.endswith("…</b>")
    assert not body.rstrip("…</b>").endswith(" ")


def test_silence_is_counted_in_days_for_the_owner():
    """«Откликнулись 22.08 в 19:40» заставляет считать в уме."""
    from fbid import notify

    card = notify.silent_client_card(profi_order(), 1, placed="22.08 в 19:40", days_silent=2)
    assert "2 дня" in card
    assert notify._days_word(1) == "день"
    assert notify._days_word(5) == "дней"


# --- Приписка площадки против настоящего сообщения клиента ---------------------

PLATFORM_TAIL = """Детали заказа

23 августа
Здравствуйте. По задаче — делаем такое регулярно.
11:42
Стоимость от 100000 до 160000 руб. / усл.
Контакты:
+7-996-362-97-58
11:42
"""

CLIENT_NAMED_A_RANGE = """Детали заказа

23 августа
Здравствуйте, по задаче.
11:42
Сегодня
Стоимость от 50 000 до 80 000 руб. устроит? Нужно быстро.
14:03
"""


def test_platform_footer_is_not_counted_as_a_reply():
    """Profi дописывает под наш отклик свой блок с ценой и телефоном."""
    from fbid.replies import count_messages

    assert count_messages(PLATFORM_TAIL) == 1


def test_client_naming_a_price_range_is_not_eaten():
    """Клиент сам назвал вилку — это ОТВЕТ, а не приписка площадки.

    Шаблон приписки не требовал хвоста «/ усл.» или «Контакты:», и обычное
    сообщение заказчика вырезалось вместе с его отметкой времени: счётчик не
    рос, и живой ответ мы просто не замечали.
    """
    from fbid.replies import count_messages

    assert count_messages(CLIENT_NAMED_A_RANGE) == 2


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "клиент назвал единицу словом",
            "Сегодня 15:10\nСтоимость от 50000 до 80000 руб. / услугу — согласны?\n",
        ),
        (
            "клиент сам дал контакты",
            "Сегодня 15:10\nСтоимость от 50000 до 80000 руб.\nКонтакты: телеграм @ivan\n",
        ),
        (
            "клиент назвал вилку и спросил",
            "Сегодня 15:10\nСтоимость от 50000 до 80000 руб. устроит?\n",
        ),
    ],
)
def test_a_client_message_about_price_is_not_the_platform_footer(name, text):
    """Сообщение клиента про цену обязано считаться сообщением.

    Шаблон приписки Profi.ru совпадал по куску строки, а не по строке целиком.
    Живой ответ заказчика вырезался вместе со своей отметкой времени: счётчик
    не рос, уведомление не уходило, человек ждал ответа, а мы про него не
    знали.
    """
    assert count_messages(text) == 1, name


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "полная приписка",
            "Сегодня 14:02\nСтоимость от 100000 до 160000 руб. / усл.\n"
            "Контакты:\n+7 900 000-00-00\n",
        ),
        (
            "приписка без телефона",
            "Вчера 09:10\nСтоимость от 100 000 до 160 000 руб. / усл.\n",
        ),
        (
            "приписка без единицы",
            "Сегодня 14:02\nСтоимость от 100000 до 160000 руб.\nКонтакты:\n+7 900 000-00-00\n",
        ),
    ],
)
def test_the_platform_footer_is_still_cut_out(name, text):
    """А сама приписка площадки по-прежнему не считается ответом клиента."""
    assert count_messages(text) == 0, name


def test_an_unreadable_order_does_not_block_the_queue(tmp_path, monkeypatch):
    """Заказ, чья страница не открывается, обязан уйти в конец круговой очереди.

    Отметка о проверке ставилась только у прочитавшихся. Заказ, снятый с биржи
    или отдающийся с ошибкой, навсегда оставался первым и съедал места в
    проходе — за ним живые переписки не проверялись вовсе.
    """
    from fbid.replies import CHECKED_PREFIX, check_replies
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        broken = Order(source=Source.PROFI, ext_id="1", title="Молчит",
                       url="https://profi.ru/1", description="")
        alive = Order(source=Source.PROFI, ext_id="2", title="Живой",
                      url="https://profi.ru/2", description="")
        pk_broken, _ = store.upsert_order(broken)
        pk_alive, _ = store.upsert_order(alive)

        class Page:
            pass

        class Ctx:
            def __init__(self):
                self.pages = [Page()]

        monkeypatch.setattr(replies, "read_stable",
                            lambda page, order: None if order.ext_id == "1" else "Сегодня 10:00\n")
        monkeypatch.setattr(replies, "read_messages", lambda page: [])
        monkeypatch.setattr(replies, "_warn_if_bid_missing", lambda *a, **kw: None)

        stats = check_replies(store, Ctx(), [(broken, pk_broken), (alive, pk_alive)])
        assert stats.errors, "о непрочитанной странице надо сказать"

        marked = store.get_state(f"{CHECKED_PREFIX}:{Source.PROFI.value}:1")
        assert marked, "непрочитанный заказ тоже обязан подвинуться в конец очереди"
    finally:
        store.close()


# --- Стоп-кран обязан останавливать и переписку --------------------------------


def test_the_stop_switch_stops_writing_to_clients(tmp_path, monkeypatch):
    """Кнопка обещает: «софт больше никому не пишет и не тратит деньги».

    Гасила она только отклики. Переписка с живым клиентом шла как ни в чём не
    бывало: модель писала от имени владельца ровно после того, как он нажал
    «Остановить», и каждый разбор стоил денег.
    """
    from fbid import dialog, notify, replies
    from fbid.bidder import Bidder
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        Bidder(store).pause()
        order = Order(source=Source.PROFI, ext_id="1", title="Лендинг",
                      url="https://profi.ru/1", description="", budget_value=20000.0)
        pk, _ = store.upsert_order(order)

        asked_model: list[str] = []
        monkeypatch.setattr(dialog, "next_message",
                            lambda *a, **kw: asked_model.append("да") or None)
        cards: list[str] = []
        monkeypatch.setattr(notify, "send", lambda text, **kw: cards.append(text) or True)

        assert replies._prepare_answer(store, order, pk, "Клиент: сколько будет стоить?",
                                       said="сколько будет стоить?")
        assert asked_model == [], "модель дёргать нельзя: это деньги"
        assert cards, "но владельцу сказать надо — клиент-то написал"
    finally:
        store.close()


def test_the_stop_switch_stops_the_nudges(tmp_path, monkeypatch):
    """Напоминание — тоже сообщение клиенту от имени владельца."""
    from fbid import replies
    from fbid.bidder import Bidder
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        Bidder(store).pause()
        order = Order(source=Source.PROFI, ext_id="1", title="Лендинг",
                      url="https://profi.ru/1", description="")
        pk, _ = store.upsert_order(order)

        class Ctx:
            def __init__(self):
                self.pages = []

        assert replies.nudge_silent_clients(store, Ctx(), [(order, pk)]) == 0
    finally:
        store.close()


def test_the_stop_switch_holds_an_already_approved_answer(tmp_path, monkeypatch):
    """Владелец одобрил ответ, потом нажал «Остановить» — ответ ждёт."""
    from fbid import dialog
    from fbid.bidder import Bidder
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="1", title="Лендинг",
                      url="https://profi.ru/1", description="", budget_value=20000.0)
        pk, _ = store.upsert_order(order)
        dialog.save_draft(store, pk, dialog.DialogReply(
            action=dialog.ACT_ANSWER, reason="", reply="текст"
        ))
        assert dialog.approve(store, pk)

        Bidder(store).pause()

        sent: list[str] = []

        class Adapter:
            name = "profi"

            def send_message(self, context, order_, text):
                sent.append(text)
                return True

        assert dialog.flush_approved(store, Adapter(), None) == 0
        assert sent == [], "кнопка обещала, что клиенту больше ничего не уйдёт"
        assert dialog.load_draft(store, pk) is not None, "ответ не выбрасываем — он готов"
    finally:
        store.close()


# --- Отказ клиента — это результат, а не тишина --------------------------------


def _closing_answer(reason="клиент отказал"):
    from fbid import dialog

    return dialog.DialogReply(action=dialog.ACT_CLOSED, reason=reason, reply="")


def test_a_client_refusal_reaches_the_owner(tmp_path, monkeypatch):
    """«Не подходит, выбрали другого» — владелец обязан это увидеть.

    Отказ и «клиент ещё не отвечал» приходили от модели одним словом
    «молчать»: отказ исчезал в логе, заказ навсегда оставался в «Начали
    общение», а напоминания продолжали ходить к человеку, сказавшему «нет».
    """
    from fbid import dialog, notify, replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="1", title="Лендинг",
                      url="https://profi.ru/1", description="")
        pk, _ = store.upsert_order(order)

        monkeypatch.setattr(dialog, "next_message", lambda *a, **kw: _closing_answer())
        cards: list[str] = []
        monkeypatch.setattr(notify, "send", lambda text, **kw: cards.append(text) or True)

        assert replies._prepare_answer(
            store, order, pk, "Клиент: спасибо, выбрали другого", said="спасибо, выбрали другого"
        )
        assert cards, "об отказе надо сказать"
        assert "Разговор окончен" in cards[0]
        assert "выбрали другого" in cards[0]
        assert dialog.client_replied(store, order), "отметка об ответе прекращает напоминания"
        assert not dialog.nudge_due(store, order)
    finally:
        store.close()


def test_real_silence_still_says_nothing(tmp_path, monkeypatch):
    """А когда клиент ничего не писал — владельца по-прежнему не трогаем."""
    from fbid import dialog, notify, replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="2", title="Лендинг",
                      url="https://profi.ru/2", description="")
        pk, _ = store.upsert_order(order)

        monkeypatch.setattr(
            dialog, "next_message",
            lambda *a, **kw: dialog.DialogReply(
                action=dialog.ACT_SILENT, reason="последнее сообщение наше", reply=""
            ),
        )
        cards: list[str] = []
        monkeypatch.setattr(notify, "send", lambda text, **kw: cards.append(text) or True)

        assert replies._prepare_answer(store, order, pk, "Мы: здравствуйте", said="")
        assert cards == []
        assert not dialog.client_replied(store, order)
    finally:
        store.close()


def test_the_closing_action_is_recognised():
    """Слово из промпта и слово в коде обязаны совпадать."""
    import inspect

    from fbid import dialog

    assert dialog.normalize_action("разговор окончен") == dialog.ACT_CLOSED
    assert dialog.normalize_action("что-то новое") == dialog.ACT_HANDOVER

    # И то же слово должно быть в промпте: иначе модель его никогда не вернёт.
    source = inspect.getsource(dialog)
    assert source.count(f'"{dialog.ACT_CLOSED}"') >= 2, "слово из кода обязано быть в промпте"


def test_a_reply_counts_even_when_the_model_is_down(tmp_path, monkeypatch):
    """Владельцу сказали «клиент написал» — значит и в воронке это ответ.

    Отметку ставила только ветка с разобранным ответом. При молчащей модели
    ответ клиента пропадал из отчёта совсем, а напоминания продолжали ходить
    к человеку, который нам уже написал.
    """
    from fbid import dialog, notify, replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="1", title="Лендинг",
                      url="https://profi.ru/1", description="")
        pk, _ = store.upsert_order(order)

        def dead_model(*a, **kw):
            raise RuntimeError("модель не отвечает")

        monkeypatch.setattr(dialog, "next_message", dead_model)
        cards: list[str] = []
        monkeypatch.setattr(notify, "send", lambda text, **kw: cards.append(text) or True)

        assert replies._prepare_answer(store, order, pk, "Клиент: когда начнём?",
                                       said="когда начнём?")
        assert cards, "владельцу сказать надо"
        assert dialog.client_replied(store, order), "и в цифрах это тоже ответ"
        assert not dialog.nudge_due(store, order), "напоминать написавшему нечего"
    finally:
        store.close()


def test_a_client_who_wrote_does_not_burn_a_nudge(tmp_path, monkeypatch):
    """Клиент ответил сам — касание отменяется, а не сжигается.

    Здесь стояло mark_nudged, и оно тратило напоминание вхолостую: два таких
    случая — и обоих касаний больше нет, а если клиент потом замолчит,
    напомнить ему уже нечем.
    """
    from fbid import dialog, replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="1", title="Лендинг",
                      url="https://profi.ru/1", description="")
        pk, _ = store.upsert_order(order)

        class Page:
            pass

        class Ctx:
            def __init__(self):
                self.pages = [Page()]

        monkeypatch.setattr(dialog, "nudge_due", lambda st, o: True)
        monkeypatch.setattr(replies, "read_stable", lambda page, o: "Сегодня 10:00\n")
        monkeypatch.setattr(replies, "read_messages", lambda page: [])
        monkeypatch.setattr(
            replies, "label_messages",
            lambda messages, own: [replies.Line(False, "Клиент: когда начнём?")],
        )

        assert replies.nudge_silent_clients(store, Ctx(), [(order, pk)]) == 0
        assert dialog.nudges_sent(store, order) == 0, "касание тратить не за что"
        assert dialog.client_replied(store, order), "а вот ответ клиента записать надо"
    finally:
        store.close()


def test_blindness_on_one_platform_does_not_mute_the_other(tmp_path, monkeypatch):
    """Площадки ломаются порознь — и жаловаться надо про каждую.

    Ключ глушилки был один на всех: жалоба про FL глушила жалобу про Profi на
    половину суток, и вторая беда просто не доезжала до владельца.
    """
    from fbid import notify, replies
    from fbid.models import Source
    from fbid.replies import ReplyCheck
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        said: list[str] = []
        monkeypatch.setattr(notify, "send_alert", lambda text: said.append(text) or True)

        blind = ReplyCheck(checked=0, missed=1, errors=["з1: страница не прочиталась"])
        replies._warn_if_blind(store, blind, Source.FL)
        replies._warn_if_blind(store, blind, Source.PROFI)

        assert len(said) == 2, said
        assert "на fl" in said[0] and "на profi" in said[1]

        # А повтор по той же площадке молчит.
        replies._warn_if_blind(store, blind, Source.FL)
        assert len(said) == 2
    finally:
        store.close()


def test_a_partial_failure_is_not_blindness(tmp_path, monkeypatch):
    """Часть переписок прочиталась — это норма мобильного прокси, а не слепота."""
    from fbid import notify, replies
    from fbid.models import Source
    from fbid.replies import ReplyCheck
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        said: list[str] = []
        monkeypatch.setattr(notify, "send_alert", lambda text: said.append(text) or True)

        replies._warn_if_blind(store, ReplyCheck(checked=2, errors=["a", "b", "c"]), Source.FL)
        assert said == []
    finally:
        store.close()


def test_a_dead_platform_is_dropped_after_three_misses(tmp_path, monkeypatch):
    """Слетевший вход не должен съедать весь проход.

    Одна непрочитавшаяся переписка — это её беда: заказ снят, страница
    удалена. Три подряд — это площадка: слетел вход, забанен прокси. Каждая
    следующая попытка стоит полминуты (три захода с паузами), и на десятке
    заказов проход стоит пять минут — всё это время вторая площадка ждёт
    браузера, а на биржах решает скорость.
    """
    from fbid import replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        pending = []
        for number in range(10):
            order = Order(source=Source.FL, ext_id=str(number),
                          url=f"https://fl.ru/{number}", title="Заказ")
            pk, _ = store.upsert_order(order)
            pending.append((order, pk))

        tried: list[str] = []

        def never_reads(page, order):
            tried.append(order.ext_id)
            return None

        monkeypatch.setattr(replies, "read_stable", never_reads)

        class Ctx:
            def __init__(self):
                self.pages = [object()]

        stats = replies.check_replies(store, Ctx(), pending)

        assert len(tried) == replies.DEAD_STREAK, f"пробовали {len(tried)} раз"
        assert any("слетел вход" in text for text in stats.errors), stats.errors
    finally:
        store.close()


def test_one_bad_page_does_not_stop_the_sweep(tmp_path, monkeypatch):
    """А одна непрочитавшаяся страница между живыми — не повод бросать площадку."""
    from fbid import replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        pending = []
        for number in range(6):
            order = Order(source=Source.FL, ext_id=str(number),
                          url=f"https://fl.ru/{number}", title="Заказ")
            pk, _ = store.upsert_order(order)
            pending.append((order, pk))

        tried: list[str] = []

        def flaky(page, order):
            tried.append(order.ext_id)
            return None if int(order.ext_id) % 2 else "Ваш отклик\nСегодня 10:00\nтекст"

        monkeypatch.setattr(replies, "read_stable", flaky)
        monkeypatch.setattr(replies, "read_messages", lambda page: [])

        class Ctx:
            def __init__(self):
                self.pages = [object()]

        replies.check_replies(store, Ctx(), pending)
        assert len(tried) == 6, "проход оборвался на живой площадке"
    finally:
        store.close()


def test_the_blindness_alarm_counts_orders_not_error_lines(tmp_path, monkeypatch):
    """«Не прочитал ни одну переписку (N заказов)» — N это заказы.

    В errors кроме заказов лежит и строка про всю площадку («подряд не
    прочиталось три»), и она завышала число в тревоге: владелец читал про
    четыре заказа там, где их было три.
    """
    from fbid import notify, replies
    from fbid.models import Source
    from fbid.replies import ReplyCheck
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        said: list[str] = []
        monkeypatch.setattr(notify, "send_alert", lambda text: said.append(text) or True)

        stats = ReplyCheck(
            checked=0, missed=3,
            errors=["з1: страница не прочиталась", "з2: страница не прочиталась",
                    "з3: страница не прочиталась", "fl: подряд не прочиталось 3 переписок"],
        )
        replies._warn_if_blind(store, stats, Source.FL)
        assert said and "(3 заказов)" in said[0], said
    finally:
        store.close()


def test_an_old_timeless_mark_does_not_kill_the_sweep(tmp_path, monkeypatch):
    """Отметка без часового пояса не должна ронять проверку ответов.

    Такие отметки писала прежняя версия, и в боевой базе они переживут нас
    обоих. Вычитание такой из времени с поясом не «даёт неверный ответ», а
    бросает TypeError — и гасило проверку ответов по всей площадке.
    """
    from fbid import notify, replies
    from fbid.models import Source
    from fbid.replies import ReplyCheck
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        monkeypatch.setattr(notify, "send_alert", lambda text: True)
        # Ровно то, что писала прежняя версия: время без пояса.
        store.set_state(replies.blind_alert_key(Source.FL), "2026-08-25T10:00:00")
        replies._warn_if_blind(store, ReplyCheck(checked=0, missed=2, errors=["a", "b"]), Source.FL)
    finally:
        store.close()


def test_an_old_timeless_session_mark_does_not_kill_the_pass(tmp_path, monkeypatch):
    """То же самое для тревоги о слетевшей сессии: она гасила весь проход."""
    from fbid import notify
    from fbid.bidder import Bidder
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        monkeypatch.setattr(notify, "send_alert", lambda text: True)
        store.set_state("sessionlost:fl", "2026-08-25T10:00:00")
        order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Заказ")
        Bidder(store)._alert_session_lost(order, RuntimeError("сессия слетела"))
    finally:
        store.close()


def test_a_bad_read_does_not_reset_the_counter(tmp_path, monkeypatch):
    """Счётчик сообщений нельзя понижать по плохо прочитанной странице.

    Переписка не укорачивается. Меньшее число значит, что мы прочитали хуже:
    заглушка прокси, страница входа, кусок вёрстки. А после нуля любое
    нормальное чтение даёт «стало на три больше» — ложное «клиент написал»,
    карточка владельцу и платный разбор переписки моделью. Ровно так и
    случилось на живой базе, пока лежал прокси.
    """
    from fbid import replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="1",
                      url="https://profi.ru/backoffice/n.php?o=1", title="Сайт")
        pk, _ = store.upsert_order(order)
        store.set_state(replies._state_key(order), "3")

        # Страница открылась, но переписки на ней нет: ни одной отметки времени
        # и ни одного сообщения по разметке.
        monkeypatch.setattr(replies, "read_stable", lambda page, o: "Детали заказа")
        monkeypatch.setattr(replies, "read_messages", lambda page: [])

        class Ctx:
            def __init__(self):
                self.pages = [object()]

        replies.check_replies(store, Ctx(), [(order, pk)])
        assert store.get_state(replies._state_key(order)) == "3", "отметку сбили"
    finally:
        store.close()


def test_a_confident_read_may_lower_the_counter(tmp_path, monkeypatch):
    """Если страница разобралась по разметке, меньшему числу можно верить."""
    from fbid import replies
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="2",
                      url="https://profi.ru/backoffice/n.php?o=2", title="Сайт")
        pk, _ = store.upsert_order(order)
        store.set_state(replies._state_key(order), "5")

        monkeypatch.setattr(replies, "read_stable", lambda page, o: "Детали заказа\nСегодня 10:00\nтекст")
        monkeypatch.setattr(
            replies, "read_messages",
            lambda page: [{"group": "g1", "text": "наш отклик"}],
        )

        class Ctx:
            def __init__(self):
                self.pages = [object()]

        replies.check_replies(store, Ctx(), [(order, pk)])
        assert store.get_state(replies._state_key(order)) == "1"
    finally:
        store.close()

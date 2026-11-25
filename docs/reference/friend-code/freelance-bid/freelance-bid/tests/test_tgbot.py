"""Тесты пульта в Telegram.

Проверяем не «красиво ли», а «дойдёт ли человек до нужного экрана»: до каждого
действия должен быть путь и кнопкой, и командой.
"""
from __future__ import annotations

import pytest

from fbid.tgbot import (
    COMMAND_ACTIONS,
    COMMANDS,
    LIMIT_BUTTONS,
    LIMIT_ORDER,
    MENU,
    Action,
    handle,
    parse_action,
    parse_command,
)


@pytest.fixture()
def store():
    from fbid.config import get_settings
    from fbid.store import Store

    s = Store(get_settings().db_path)
    yield s
    s.close()


# --- Команды ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("/start", "menu"),
        ("/menu", "menu"),
        ("/report", "stats"),
        ("/week", "stats"),
        ("/limits", "limits"),
        ("/status", "status"),
        ("/orders", "orders"),
        ("/pause", "pause"),
        ("/resume", "resume"),
        ("/check", "replies"),
        ("/help", "help"),
    ],
)
def test_every_command_maps_to_action(text, code):
    action = parse_command(text)
    assert action is not None and action.code == code


def test_command_with_bot_name_works():
    """В группах Telegram дописывает имя бота: /report@uniqore_bot."""
    assert parse_command("/report@uniqore_bot").code == "stats"


def test_command_with_argument_works():
    assert parse_command("/start 12345").code == "menu"


def test_plain_text_is_not_a_command():
    assert parse_command("как дела") is None
    assert parse_command("1500") is None
    assert parse_command("") is None


def test_unknown_command_is_answered_not_ignored(store):
    action = parse_command("/чтотоневедомое")
    assert action.code == "unknown_command"
    text, markup = handle(action, store)
    assert "Не знаю команду" in text
    assert markup["inline_keyboard"]


def test_lonely_slash_does_not_crash(store):
    action = parse_command("/")
    assert action.code == "unknown_command"
    assert handle(action, store)[0]


# --- Меню ------------------------------------------------------------------

def test_every_registered_command_has_a_handler():
    """Команда в кнопке «Меню», для которой нет обработчика, — это тупик."""
    for name, _ in COMMANDS:
        assert name in COMMAND_ACTIONS, f"/{name} объявлена, но не обрабатывается"


def test_every_command_has_a_description():
    for name, description in COMMANDS:
        assert description and len(description) <= 60, name


def test_menu_buttons_all_lead_somewhere(store):
    """Каждая кнопка меню должна что-то отвечать, а не молчать."""
    for row in MENU:
        for label, data in row:
            assert label.strip()
            text, markup = handle(parse_action(data), store)
            assert text.strip(), f"кнопка {label} ответила пустотой"
            assert markup["inline_keyboard"]


def test_every_limit_has_a_button_label():
    for key in LIMIT_ORDER:
        assert key in LIMIT_BUTTONS, f"у лимита {key} нет подписи на кнопке"


def test_limits_screen_has_way_back(store):
    _, markup = handle(Action("limits"), store)
    codes = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert "menu" in codes, "с экрана лимитов нет выхода в меню"


def test_keyboard_rows_are_not_too_wide():
    """Больше двух кнопок в ряд на телефоне не читается."""
    for row in MENU:
        assert len(row) <= 2


def test_button_labels_fit_telegram_limit():
    for row in MENU:
        for label, data in row:
            assert len(label) <= 40
            assert len(data.encode()) <= 64  # ограничение callback_data


# --- Экраны отвечают на пустой базе ----------------------------------------

def test_orders_screen_survives_empty_base(tmp_path):
    from fbid.store import Store

    empty = Store(tmp_path / "empty.db")
    try:
        text, _ = handle(Action("orders"), empty)
        assert "пуст" in text.lower()
    finally:
        empty.close()


def test_status_screen_survives_empty_base(tmp_path):
    from fbid.store import Store

    empty = Store(tmp_path / "empty.db")
    try:
        text, _ = handle(Action("status"), empty)
        assert "Состояние" in text
    finally:
        empty.close()


def test_screens_fit_telegram_message_limit(store):
    """Сообщение длиннее 4096 символов Telegram просто не примет."""
    for code in ("menu", "help", "status", "orders", "limits", "stats:7"):
        text, _ = handle(parse_action(code), store)
        assert len(text) <= 4096, f"экран {code} длиннее лимита Telegram"


def test_html_tags_are_balanced(store):
    """Битая разметка — это ошибка Telegram и пустое сообщение вместо ответа."""
    for code in ("menu", "help", "status", "orders", "limits"):
        text, _ = handle(parse_action(code), store)
        for tag in ("b", "i", "a"):
            assert text.count(f"<{tag}>") + text.count(f"<{tag} ") == text.count(f"</{tag}>"), (
                f"экран {code}: тег <{tag}> не закрыт"
            )


# --- Долгие действия -------------------------------------------------------

def test_replies_check_is_not_started_twice(store, monkeypatch):
    """Второе нажатие не должно поднимать второй браузер к тем же страницам."""
    from fbid import tgbot

    calls = []

    def slow_check(store_arg):
        calls.append(1)
        # Изнутри проверки имитируем повторное нажатие.
        text, _ = tgbot.handle(Action("replies"), store_arg)
        assert "Уже проверяю" in text
        from fbid.pipeline import ReplySweep

        return ReplySweep(checked=1)

    monkeypatch.setattr("fbid.pipeline.check_replies_all", slow_check)
    tgbot.handle(Action("replies"), store)
    assert len(calls) == 1
    # Флаг снялся: следующая проверка снова возможна.
    assert tgbot._replies_running is False


def test_replies_flag_resets_after_error(store, monkeypatch):
    from fbid import tgbot

    def boom(_store):
        raise RuntimeError("браузер упал")

    monkeypatch.setattr("fbid.pipeline.check_replies_all", boom)
    with pytest.raises(RuntimeError):
        tgbot.handle(Action("replies"), store)
    assert tgbot._replies_running is False, "после ошибки кнопка осталась заблокированной"


# --- Маршрутизация входящих ------------------------------------------------

@pytest.fixture()
def outbox(monkeypatch):
    """Перехватывает всё, что бот шлёт в Telegram."""
    from fbid import tgbot

    sent = []
    monkeypatch.setattr(tgbot, "_call", lambda method, payload, **kw: sent.append((method, payload)) or {"ok": True})
    return sent


def _message(chat_id: str, text: str) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def _click(chat_id: str, data: str) -> dict:
    return {
        "update_id": 2,
        "callback_query": {"id": "q1", "data": data, "message": {"chat": {"id": chat_id}}},
    }


def test_command_from_owner_is_answered(store, outbox):
    from fbid.tgbot import _handle_update

    _handle_update(_message("777", "/status"), store, allowed="777")
    methods = [m for m, _ in outbox]
    assert "sendMessage" in methods
    assert "Состояние" in outbox[-1][1]["text"]


def test_message_from_stranger_is_ignored(store, outbox):
    """Бот тратит деньги владельца — чужие команды не выполняются."""
    from fbid.tgbot import _handle_update

    _handle_update(_message("999", "/pause"), store, allowed="777")
    assert outbox == []


def test_button_from_stranger_is_ignored(store, outbox):
    from fbid.bidder import Bidder
    from fbid.tgbot import _handle_update

    Bidder(store).resume()
    _handle_update(_click("999", "pause"), store, allowed="777")
    assert Bidder(store).is_paused() is False
    # Нажатие «гасим», но ничего не выполняем и не отвечаем текстом.
    assert all(method == "answerCallbackQuery" for method, _ in outbox)


def test_button_from_owner_works(store, outbox):
    from fbid.bidder import Bidder
    from fbid.tgbot import _handle_update

    Bidder(store).resume()
    _handle_update(_click("777", "pause"), store, allowed="777")
    assert Bidder(store).is_paused() is True
    assert any(m == "sendMessage" for m, _ in outbox)


def test_plain_text_shows_the_menu(store, outbox):
    from fbid.tgbot import _handle_update

    _handle_update(_message("777", "привет"), store, allowed="777")
    payload = outbox[-1][1]
    assert payload["reply_markup"]["inline_keyboard"]


def test_command_cancels_a_pending_limit_edit(store, outbox):
    """Человек передумал и нажал /status — это не значение лимита."""
    from fbid import limits
    from fbid.tgbot import PENDING_KEY, _handle_update, handle

    handle(Action("limit", "profi_daily_budget"), store)
    _handle_update(_message("777", "/status"), store, allowed="777")

    assert store.get_state(PENDING_KEY) is None
    assert limits.get("profi_daily_budget", store) == 1000


def test_number_after_limit_button_is_applied(store, outbox):
    from fbid import limits
    from fbid.tgbot import _handle_update, handle

    handle(Action("limit", "profi_daily_budget"), store)
    _handle_update(_message("777", "1900"), store, allowed="777")
    assert limits.get("profi_daily_budget", store) == 1900


def test_message_without_text_does_not_crash(store, outbox):
    """Стикер или фото — у сообщения просто нет поля text."""
    from fbid.tgbot import _handle_update

    _handle_update({"update_id": 3, "message": {"chat": {"id": "777"}}}, store, allowed="777")
    assert outbox


def test_update_without_message_or_click_is_ignored(store, outbox):
    from fbid.tgbot import _handle_update

    _handle_update({"update_id": 4, "edited_message": {}}, store, allowed="777")
    assert outbox == []


# --- Очередь: посмотреть всю и убрать лишнее -------------------------------

def _fill_queue(store, count=12):
    """Кладёт в базу заказы, которые пройдут фильтры и встанут в очередь."""
    from datetime import datetime, timezone

    from fbid.models import Order, Source

    titles = [
        "Интернет-магазин автозапчастей", "Телеграм-бот для записи в барбершоп",
        "Интеграция amoCRM с телефонией", "Парсер цен конкурентов на python",
        "Личный кабинет для страховой", "Сайт-каталог мебельной фабрики",
        "CRM для автосервиса под ключ", "Доработка Битрикс интернет-магазина",
        "Бот поддержки на нейросети", "Выгрузка заказов в 1С",
        "Лендинг для стоматологии", "Админ-панель для маркетплейса",
    ]
    for i in range(count):
        store.upsert_order(Order(
            source=Source.FL, ext_id=f"q{i}",
            url=f"https://www.fl.ru/projects/{9000 + i}/x.html",
            title=titles[i % len(titles)], category="Сайты",
            description="Нужна разработка под ключ", budget_value=60_000.0,
            published_at=datetime.now(timezone.utc),
        ))


def test_queue_screen_shows_top_five_with_remove_buttons(store):
    from fbid.tgbot import handle

    _fill_queue(store)
    text, markup = handle(Action("queue"), store)
    drops = [b for row in markup["inline_keyboard"] for b in row
             if b["callback_data"].startswith("drop:")]
    assert len(drops) == 5
    assert "Очередь на отклик" in text


def test_queue_has_show_all_and_clear(store):
    from fbid.tgbot import handle

    _fill_queue(store)
    _, markup = handle(Action("queue"), store)
    codes = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert any(c.startswith("queue_all") for c in codes), "нет кнопки «показать все»"
    assert "queue_clear" in codes, "нет кнопки очистки"


def test_full_queue_is_paginated(store):
    from fbid.tgbot import QUEUE_PAGE, handle

    _fill_queue(store)
    text, markup = handle(Action("queue_all", "0"), store)
    assert "страница 1 из" in text
    codes = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert "queue_all:1" in codes, "со страницы нельзя пролистать дальше"

    text2, markup2 = handle(Action("queue_all", "1"), store)
    assert "страница 2 из" in text2
    codes2 = {b["callback_data"] for row in markup2["inline_keyboard"] for b in row}
    assert "queue_all:0" in codes2, "со второй страницы нельзя вернуться"
    assert QUEUE_PAGE > 0


def test_removing_from_queue_shifts_the_rest(store):
    from fbid.pipeline import queue_size, waiting_queue
    from fbid.tgbot import handle

    _fill_queue(store)
    before = queue_size(store)
    first = waiting_queue(store, limit=1)[0]

    text, _ = handle(Action("drop", str(first.order_pk)), store)

    assert "Убрал" in text
    assert queue_size(store) == before - 1
    assert all(item.order_pk != first.order_pk for item in waiting_queue(store, limit=None))


def test_removing_from_a_page_returns_to_the_same_page(store):
    from fbid.pipeline import waiting_queue
    from fbid.tgbot import handle

    _fill_queue(store)
    item = waiting_queue(store, limit=None)[9]
    text, markup = handle(Action("drop", f"{item.order_pk}:1"), store)
    assert "страница 2 из" in text
    codes = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert any(c.startswith("drop:") for c in codes)


def test_clearing_the_queue_asks_first(store):
    from fbid.pipeline import queue_size
    from fbid.tgbot import handle

    _fill_queue(store)
    _, markup = handle(Action("queue_clear"), store)
    codes = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
    assert "queue_clear_yes" in codes
    assert queue_size(store) > 0, "очередь не должна чиститься без подтверждения"


def test_clearing_the_queue_empties_it(store):
    from fbid.pipeline import queue_size
    from fbid.tgbot import handle

    _fill_queue(store)
    text, _ = handle(Action("queue_clear_yes"), store)
    assert "убрано заказов" in text
    assert queue_size(store) == 0


def test_dropping_unknown_order_does_not_crash(store):
    from fbid.tgbot import handle

    text, _ = handle(Action("drop", "999999"), store)
    assert "уже нет" in text


def test_empty_queue_screen_is_friendly(store):
    from fbid.tgbot import handle

    text, markup = handle(Action("queue"), store)
    assert "пуст" in text.lower()
    assert markup["inline_keyboard"]


def test_known_actions_match_the_real_dispatch():
    """Список KNOWN_ACTIONS обязан совпадать с тем, что разбирает handle.

    Список читает Orbit: у систем один токен, и незнакомое нажатие приходит
    сюда. Разъедутся — либо живая кнопка биржи перестанет работать из Orbit,
    либо чужая снова подменит собой карточку с вопросом клиента.
    """
    import inspect
    import re

    from fbid import tgbot

    source = inspect.getsource(tgbot.handle)
    dispatched = set(re.findall(r'action\.code == "([a-z_]+)"', source))
    for group in re.findall(r'action\.code in \(([^)]+)\)', source):
        dispatched.update(re.findall(r'"([a-z_]+)"', group))
    assert dispatched == set(tgbot.KNOWN_ACTIONS), (
        f"лишние: {sorted(tgbot.KNOWN_ACTIONS - dispatched)}, "
        f"забытые: {sorted(dispatched - tgbot.KNOWN_ACTIONS)}"
    )


def test_knows_says_no_to_a_foreign_code():
    from fbid import tgbot

    assert tgbot.knows("queue")
    assert tgbot.knows("drop:12:3")
    assert not tgbot.knows("p:card:7")
    assert not tgbot.knows("")


# --- Повторное нажатие «Проверить ответы» --------------------------------------


def test_the_replies_button_refuses_a_second_walk_right_away(tmp_path, monkeypatch):
    """Обход занимает минуту, и всё это время бот молчит.

    Владелец жмёт кнопку второй раз — и нажатие ЖДЁТ в очереди обновлений, а
    потом поднимает браузер к тем же страницам ещё раз. Флага «идёт прямо
    сейчас» тут мало: к моменту второго нажатия первое уже закончилось.
    """
    from fbid import pipeline, tgbot
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        walks: list[int] = []
        monkeypatch.setattr(pipeline, "check_replies_all",
                            lambda st: walks.append(1) or pipeline.ReplySweep(checked=3))

        first, _ = tgbot._check_replies(store)
        assert "Проверил переписок: 3" in first
        assert len(walks) == 1

        second, _ = tgbot._check_replies(store)
        assert "только что проверены" in second, second
        assert len(walks) == 1, "второй обход по тем же страницам не нужен"
    finally:
        store.close()


def test_the_replies_button_works_again_after_the_cooldown(tmp_path, monkeypatch):
    """Прошла минута-другая — проверять снова можно."""
    from datetime import datetime, timedelta, timezone

    from fbid import pipeline, tgbot
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        walks: list[int] = []
        monkeypatch.setattr(pipeline, "check_replies_all",
                            lambda st: walks.append(1) or pipeline.ReplySweep(checked=3))

        tgbot._check_replies(store)
        long_ago = datetime.now(timezone.utc) - timedelta(
            seconds=tgbot.REPLIES_COOLDOWN_SECONDS + 5
        )
        store.set_state(tgbot.REPLIES_LAST_KEY, long_ago.isoformat(timespec="seconds"))

        tgbot._check_replies(store)
        assert len(walks) == 2
    finally:
        store.close()


def test_a_sweep_that_never_happened_is_not_reported_as_silence(tmp_path, monkeypatch):
    """«Проверил, тихо» и «проверить не смог» — разные новости.

    Кнопка рапортовала «новых ответов нет», хотя браузер не открылся или
    слетела сессия. Владелец читал это как «клиенты молчат» и не шёл
    проверять руками.
    """
    from fbid import pipeline, tgbot
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        monkeypatch.setattr(
            pipeline, "check_replies_all",
            lambda st: pipeline.ReplySweep(errors=["fl: браузер занят"]),
        )
        text, _ = tgbot._check_replies(store)
        assert "не вышло" in text, text
        assert "браузер занят" in text
        assert "не «ответов нет»" in text
    finally:
        store.close()


def test_a_real_quiet_sweep_says_how_many_it_looked_at(tmp_path, monkeypatch):
    """А состоявшийся обход называет число проверенных переписок."""
    from fbid import pipeline, tgbot
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        monkeypatch.setattr(
            pipeline, "check_replies_all", lambda st: pipeline.ReplySweep(checked=7)
        )
        text, _ = tgbot._check_replies(store)
        assert "Проверил переписок: 7" in text, text
        assert "Новых ответов нет" in text
    finally:
        store.close()


def test_the_status_screen_does_not_promise_replies_while_paused(tmp_path, monkeypatch):
    """Стоп-кран останавливает и ответы клиентам — экран обязан это говорить."""
    from fbid import config, tgbot
    from fbid.bidder import Bidder
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        monkeypatch.setattr(config.get_settings(), "dry_run", False, raising=False)
        Bidder(store).pause()
        text = tgbot._status_text(store)
        assert "ни откликов, ни ответов" in text, text
        assert "ответы клиентам уходят" not in text
    finally:
        store.close()


def test_the_queue_pager_does_not_promise_pages_that_are_empty(tmp_path, monkeypatch):
    """«Дальше →» не должна вести в пустоту.

    Экран смотрит свежий хвост базы, а счётчик очереди считает её целиком.
    Листалка по полному размеру рисовала «Дальше →» там, где дальше пусто:
    владелец жал и попадал на чистый экран.
    """
    from fbid import pipeline, tgbot
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        # В очереди много, а листается мало.
        monkeypatch.setattr(pipeline, "queue_size", lambda st: 900)
        monkeypatch.setattr(pipeline, "listed_queue_size", lambda st: 2)
        order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
        item = pipeline.QueueItem(order=order, order_pk=1, score=8.0, reason="ждёт")
        monkeypatch.setattr(pipeline, "waiting_queue",
                            lambda st, limit=None, offset=0: [item, item][:max(0, 2 - offset)])

        markup = tgbot.queue_page_keyboard(store, page=0)
        codes = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        assert "queue_all:1" not in codes, "дальше листать нечего"

        text = tgbot._queue_page_text(store, page=0)
        assert "страница 1 из 1" in text, text
        assert "Листаются 2 свежих" in text, text
    finally:
        store.close()


def test_the_pager_still_works_inside_the_window(tmp_path, monkeypatch):
    """А внутри окна листалка работает как раньше."""
    from fbid import pipeline, tgbot
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
        item = pipeline.QueueItem(order=order, order_pk=1, score=8.0, reason="ждёт")
        monkeypatch.setattr(pipeline, "queue_size", lambda st: 40)
        monkeypatch.setattr(pipeline, "listed_queue_size", lambda st: 40)
        monkeypatch.setattr(pipeline, "waiting_queue",
                            lambda st, limit=None, offset=0: [item] * (limit or 1))

        markup = tgbot.queue_page_keyboard(store, page=0)
        codes = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        assert "queue_all:1" in codes
    finally:
        store.close()


def test_any_other_button_cancels_a_half_started_limit_edit(tmp_path):
    """Нажали что-то другое — значит правку лимита бросили.

    Она ждала числа ещё десять минут: следующее сообщение из одних цифр —
    телефон клиента, скопированный в чат, — уходило в суточный бюджет
    площадки. Команды это уже умели, а кнопки — нет.
    """
    from fbid import tgbot
    from fbid.store import Store
    from fbid.tgbot import Action

    store = Store(tmp_path / "t.db")
    try:
        tgbot.handle(Action("limit", "profi_daily_budget"), store)
        assert tgbot._pending_limit(store) == "profi_daily_budget"

        tgbot.handle(Action("status"), store)
        assert tgbot._pending_limit(store) is None, "правка обязана сняться"

        # И число после этого уже не считается новым лимитом.
        assert tgbot.apply_pending_limit(store, "1500") is None
    finally:
        store.close()


def test_the_limit_dialog_survives_its_own_buttons(tmp_path):
    """А кнопки самой правки её не сбрасывают."""
    from fbid import tgbot
    from fbid.store import Store
    from fbid.tgbot import Action

    store = Store(tmp_path / "t.db")
    try:
        tgbot.handle(Action("limit", "profi_daily_budget"), store)
        tgbot.handle(Action("limits"), store)
        assert tgbot._pending_limit(store) == "profi_daily_budget"
    finally:
        store.close()


def test_the_status_screen_does_not_show_green_when_nothing_works(tmp_path):
    """«🟢 работает, отклики уходят» при мёртвой сессии и пустом счёте — ложь.

    Экран называется «Что сейчас происходит» и открывают его ровно тогда,
    когда что-то не так. Обе беды чинятся только владельцем, и обе он должен
    прочитать здесь, а не догадаться по нулям в строке «Сегодня».
    """
    from fbid.bidder import MODEL_STATE_KEY
    from fbid.store import Store
    from fbid.tgbot import handle, parse_action

    store = Store(tmp_path / "t.db")
    try:
        store.set_state("sessionlost:fl", "2026-08-26T08:06:16+00:00")
        store.set_state(MODEL_STATE_KEY, "кончились деньги на счёте модели")

        text, _markup = handle(parse_action("status"), store)

        assert "🟢" not in text, text
        assert "вход на FL.ru слетел" in text
        assert "fbid login fl" in text
        assert "кончились деньги" in text
    finally:
        store.close()


def test_the_status_screen_keeps_its_own_state_when_all_is_well(tmp_path):
    """Без бед строка состояния — своя (в тестовом окружении это dry-run)."""
    from fbid.store import Store
    from fbid.tgbot import handle, parse_action

    store = Store(tmp_path / "t.db")
    try:
        text, _markup = handle(parse_action("status"), store)
        assert "работает не всё" not in text, text
        assert "слетел" not in text
    finally:
        store.close()


def test_the_status_screen_shows_what_is_left_on_the_platform(tmp_path):
    """Остаток на Profi и пакет FL — две цифры, от которых зависит вся работа.

    Пустой кошелёк и пустой пакет останавливают отклики полностью, а жили эти
    числа только в логе: владелец мог узнать их, лишь открыв файл на сервере.
    """
    from fbid.models import Source
    from fbid.pipeline import remember_balance
    from fbid.store import Store
    from fbid.tgbot import handle, parse_action

    store = Store(tmp_path / "t.db")
    try:
        remember_balance(store, Source.PROFI, 170)
        remember_balance(store, Source.FL, 0)

        text, _markup = handle(parse_action("status"), store)

        assert "Осталось на площадке" in text
        assert "170 ₽" in text
        assert "0 откликов в пакете" in text
        assert "только что" in text
    finally:
        store.close()

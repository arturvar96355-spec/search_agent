"""Бот: доступ, роутер и устойчивость.

Две вещи здесь важнее остальных. Первая — чужой чат не должен получить ответ:
бот показывает базу контактов и умеет отправлять письма от лица студии.
Вторая — упавший раздел не должен вешать весь пульт: остальные вкладки обязаны
продолжать работать.
"""
from __future__ import annotations

import pytest

from orbit import bot, config
from orbit.registry import Health, Registry, Section
from orbit.ui import Screen


@pytest.fixture()
def registry():
    """Реестр из одного простого раздела."""
    def handle(store, action, arg):
        if action == "boom":
            raise RuntimeError("экран сломался")
        return Screen(f"раздел t, действие {action}", [[("Кнопка", "t:home")]])

    reg = Registry()
    reg.add(lambda: Section(
        code="t", title="Тест", icon="🧪",
        handle=handle,
        health=lambda store: Health(True, "работает"),
        commands=[("test", "Тестовый раздел", "t:home")],
    ))
    return reg


@pytest.fixture()
def owner_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    config.get_settings.cache_clear()
    yield "111"
    config.get_settings.cache_clear()


# --- Реестр -----------------------------------------------------------------

def test_broken_section_does_not_break_registry():
    reg = Registry()

    def broken():
        raise ImportError("нет модуля fbid")

    reg.add(broken)
    assert reg.sections == []
    assert "нет модуля fbid" in str(reg.broken)


def test_failed_health_is_shown_as_state(store):
    reg = Registry()
    reg.add(lambda: Section(
        code="x", title="Икс", icon="❌",
        handle=lambda *a: Screen("."),
        health=lambda store: 1 / 0,
    ))
    (_section, state), = reg.health(store)
    assert not state.ok
    assert "проверка не прошла" in state.summary


# --- Роутер -----------------------------------------------------------------

def test_home_lists_sections(registry, store):
    screen = bot.route(registry, store, "home")
    assert "Тест" in screen.text
    assert any(data == "t:home" for row in screen.rows for _label, data in row)


def test_unknown_button_says_so_instead_of_silently_going_home(registry, store):
    """Незнакомая кнопка не должна подменять сообщение главным меню.

    Так терялись карточки биржи: владелец жал «Отправить» под готовым
    ответом клиенту, а сообщение на его глазах превращалось в меню — вместе
    с вопросом клиента и текстом ответа.
    """
    screen = bot.route(registry, store, "нет_такого:x")
    assert "устарела" in screen.text
    assert "нет_такого" in screen.text


def test_section_action_passed_through(registry, store):
    assert "действие list" in bot.route(registry, store, "t:list:new:0").text


def test_broken_screen_does_not_crash_bot(registry, store):
    """Раздел упал — показываем причину и кнопку домой, а не молчим."""
    screen = bot.route(registry, store, "t:boom")
    assert "экран сломался" in screen.text
    assert any(data == "home" for row in screen.rows for _label, data in row)


def test_status_screen(registry, store):
    text = bot.route(registry, store, "status").text
    assert "Сводка" in text and "Тест" in text


def test_help_screen(registry, store):
    assert "Партнёры" in bot.route(registry, store, "help").text


# --- Команды ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [("/start", "home"), ("/menu", "home"), ("/status", "status"),
     ("/help", "help"), ("/test", "t:home"), ("/start@orbit_bot", "home")],
)
def test_commands_resolved(registry, text, expected):
    assert bot.command_to_code(text, registry) == expected


@pytest.mark.parametrize("text", ["привет", "", "/чепуха"])
def test_unknown_text_is_not_a_command(registry, text):
    assert bot.command_to_code(text, registry) is None


# --- Доступ -----------------------------------------------------------------

def test_stranger_gets_no_answer(registry, store, owner_chat, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "_call", lambda method, payload, **kw: calls.append(method))

    bot.handle_update(
        {"message": {"chat": {"id": 999}, "text": "/start"}}, registry, store
    )
    assert calls == []


def test_owner_gets_menu(registry, store, owner_chat, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "_call", lambda method, payload, **kw: calls.append(method) or {})

    bot.handle_update(
        {"message": {"chat": {"id": 111}, "text": "/start"}}, registry, store
    )
    assert "sendMessage" in calls


def test_without_chat_id_nobody_is_allowed(registry, store, monkeypatch):
    """Пустой TELEGRAM_CHAT_ID не должен открывать бота всему интернету."""
    config.get_settings.cache_clear()
    calls = []
    monkeypatch.setattr(bot, "_call", lambda method, payload, **kw: calls.append(method))

    bot.handle_update({"message": {"chat": {"id": 111}, "text": "/start"}}, registry, store)
    assert calls == []


def test_noop_button_only_stops_the_clock(registry, store, owner_chat, monkeypatch):
    """Подпись-счётчик в листалке ничего не делает, но часики гасит."""
    calls = []
    monkeypatch.setattr(bot, "_call", lambda method, payload, **kw: calls.append(method) or {})

    bot.handle_update({"callback_query": {
        "id": "1", "data": "noop", "message": {"chat": {"id": 111}, "message_id": 5},
    }}, registry, store)
    assert calls == ["answerCallbackQuery"]


def _telegram(monkeypatch, calls, *, edit_error: str | None = None):
    """Подменяет разговор с Telegram. edit_error — что ответить на правку."""
    def request(method, payload, **kw):
        calls.append(method)
        if method == "editMessageText" and edit_error is not None:
            return None, edit_error
        return {}, ""

    monkeypatch.setattr(bot, "_request", request)
    monkeypatch.setattr(bot, "_call", lambda method, payload, **kw: request(method, payload)[0])


def test_callback_edits_message(registry, store, owner_chat, monkeypatch):
    calls = []
    _telegram(monkeypatch, calls)

    bot.handle_update({"callback_query": {
        "id": "1", "data": "t:home", "message": {"chat": {"id": 111}, "message_id": 5},
    }}, registry, store)
    assert "editMessageText" in calls


def test_callback_falls_back_to_new_message(registry, store, owner_chat, monkeypatch):
    """Telegram отказал в правке по-настоящему — присылаем экран заново."""
    calls = []
    _telegram(monkeypatch, calls, edit_error="Bad Request: message to edit not found")

    bot.handle_update({"callback_query": {
        "id": "1", "data": "t:home", "message": {"chat": {"id": 111}, "message_id": 5},
    }}, registry, store)
    assert "sendMessage" in calls


def test_same_screen_does_not_spawn_a_copy(registry, store, owner_chat, monkeypatch):
    """Повторное нажатие не должно плодить одинаковые сообщения.

    Telegram отвечает отказом, когда правка ничего не меняет. Это самый обычный
    случай — «Лимит: 20» при лимите 20, отметка стадии у того, кто уже в ней, —
    и раньше каждое такое нажатие сыпало в чат копию экрана.
    """
    calls = []
    _telegram(monkeypatch, calls, edit_error=(
        "Bad Request: message is not modified: specified new message content and "
        "reply markup are exactly the same as a current content"
    ))

    bot.handle_update({"callback_query": {
        "id": "1", "data": "t:home", "message": {"chat": {"id": 111}, "message_id": 5},
    }}, registry, store)
    assert "sendMessage" not in calls


# --- Поиск контакта по сообщению в чат --------------------------------------

def _company(store, **kwargs):
    from orbit.models import Contact, Kind

    data = {
        "kind": Kind.PARTNER, "key": "mediasfera.ru", "name": "Медиасфера",
        "site": "mediasfera.ru", "emails": ["hello@mediasfera.ru"],
        "phones": ["+74951234567"],
    }
    data.update(kwargs)
    contact = Contact(**data)
    contact.pk = store.upsert_contact(contact)
    return store.get_contact(contact.pk)


@pytest.fixture()
def partner_registry(monkeypatch):
    """Реестр с настоящей вкладкой «Партнёры»: поиск открывает её карточку."""
    from orbit.sections import partners

    reg = Registry()
    reg.add(partners.load)
    monkeypatch.setattr(bot, "registry_for", lambda _store: reg)
    return reg


def test_email_in_chat_opens_the_card(store, partner_registry):
    """Скинул адрес в чат — открылась карточка того, кому мы писали."""
    contact = _company(store)

    screen = bot._search_reply(store, "hello@mediasfera.ru")

    assert screen is not None
    assert "Медиасфера" in screen.text
    assert str(contact.pk) in str(screen.rows)


def test_search_is_case_insensitive_in_russian(store):
    """SQLite не умеет приводить кириллицу к нижнему регистру — ищем сами."""
    _company(store)
    assert [c.name for c in store.search_contacts("МЕДИАСФЕРА")] == ["Медиасфера"]
    assert [c.name for c in store.search_contacts("медиасфера")] == ["Медиасфера"]


def test_phone_is_found_in_any_format(store):
    _company(store)
    for query in ("+74951234567", "8 (495) 123-45-67", "4951234567"):
        assert store.search_contacts(query), f"не нашёл по «{query}»"


def test_unknown_address_says_so(store):
    _company(store)
    screen = bot._search_reply(store, "kto-to@drugoy.ru")
    assert screen is not None and "Не нашёл" in screen.text


def test_several_matches_come_as_a_list(store, partner_registry):
    _company(store, key="a.ru", name="Медиасфера Москва", emails=["a@mediasfera.ru"])
    _company(store, key="b.ru", name="Медиасфера СПб", emails=["b@mediasfera.ru"])

    screen = bot._search_reply(store, "mediasfera.ru")

    assert "Нашлось 2" in screen.text
    assert len(screen.rows) == 3  # две компании плюс «в главное меню»


def test_short_text_is_not_a_search(store):
    """«ок» и «да» не должны превращаться в поиск по базе."""
    _company(store)
    assert bot._search_reply(store, "ок") is None
    assert bot._search_reply(store, "/start") is None


# --- Командная строка ----------------------------------------------------------


def test_validate_does_not_close_a_contact_with_telegram(store, monkeypatch, capsys):
    """`--close` закрывает по мёртвой почте — но не того, до кого есть телеграм."""
    from typer.testing import CliRunner

    from orbit import cli, config, mailcheck
    from orbit.models import Contact, Kind, Stage

    with_tg = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a", name="С телеграмом",
        emails=["info@ghost.ru"], telegram=["https://t.me/live_chat"],
    ))
    without = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="b", name="Без телеграма", emails=["info@ghost2.ru"],
    ))
    store.conn.commit()

    monkeypatch.setattr(config.get_settings(), "db_path", store.path, raising=False)
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(
        mailcheck, "domain_state",
        lambda domain: (mailcheck.DomainState.DEAD, "домена не существует"),
    )
    monkeypatch.setattr(store, "close", lambda: None)

    result = CliRunner().invoke(cli.app, ["validate", "--kind", "client", "--close"])
    assert result.exit_code == 0, result.output

    assert store.get_contact(with_tg).stage is not Stage.DEAD
    assert store.get_contact(without).stage is Stage.DEAD
    assert "есть телеграм" in result.output


# --- Новое значение лимита не должно теряться ----------------------------------


def _armed_exchange(monkeypatch, tmp_path, waiting: str | None):
    """Подменяет биржу: ждёт ли она значение лимита и что отвечает."""
    from orbit import bot as bot_mod
    from orbit.sections import freelance

    applied: list[str] = []

    class FakeTgbot:
        @staticmethod
        def _pending_limit(store):
            return waiting

        @staticmethod
        def apply_pending_limit(store, text):
            applied.append(text)
            return (f"Готово: {text}", {"inline_keyboard": []})

    class FakeStore:
        def close(self):
            pass

    monkeypatch.setattr(freelance, "_fbid", lambda: (FakeTgbot, None, None))
    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FakeStore())
    return bot_mod, applied


@pytest.mark.parametrize("text", ["2000 ₽", "2 000 ₽", "2000", "1,5", "2 000 ₽"])
def test_limit_value_reaches_the_exchange(monkeypatch, tmp_path, text):
    """Бот сам показывает «Сейчас: 1000 ₽» — ответ в том же виде обязан дойти.

    Свой фильтр был строже биржевого разборщика: значение молча терялось,
    вместо лимита открывался поиск по базе, а диалог оставался взведённым.
    """
    bot_mod, applied = _armed_exchange(monkeypatch, tmp_path, waiting="profi_daily_budget")
    screen = bot_mod._pending_limit_reply(text)
    assert screen is not None
    assert applied == [text.strip()]


def test_nothing_is_applied_when_the_exchange_is_not_waiting(monkeypatch, tmp_path):
    """Диалога нет — сообщение должно уйти по обычному пути, а не в лимиты."""
    bot_mod, applied = _armed_exchange(monkeypatch, tmp_path, waiting=None)
    assert bot_mod._pending_limit_reply("89151234567") is None
    assert applied == []


def test_garbage_is_answered_by_the_exchange(monkeypatch, tmp_path):
    """На мусор отвечает биржа — с кнопкой «Отмена», а не молчаливый провал."""
    bot_mod, applied = _armed_exchange(monkeypatch, tmp_path, waiting="profi_daily_budget")
    screen = bot_mod._pending_limit_reply("как дела")
    assert screen is not None
    assert applied == ["как дела"]


def test_empty_message_is_ignored(monkeypatch, tmp_path):
    bot_mod, applied = _armed_exchange(monkeypatch, tmp_path, waiting="profi_daily_budget")
    assert bot_mod._pending_limit_reply("   ") is None
    assert applied == []


def test_the_answer_to_a_limit_has_one_back_button(monkeypatch, tmp_path):
    """После ввода лимита биржа рисует свою «← В меню» — второй быть не должно.

    Здесь перевод кнопок был написан заново и про кнопку «домой» не знал: она
    превращалась в «f:fb:menu» и уводила в главное меню самой биржи, а сверху
    приписывалась ещё одна «К фрилансу». Две подряд выглядят как разные.
    """
    from orbit import bot as bot_mod
    from orbit.sections import freelance

    class FakeTgbot:
        @staticmethod
        def _pending_limit(store):
            return "profi_daily_budget"

        @staticmethod
        def apply_pending_limit(store, text):
            return ("Готово", {"inline_keyboard": [
                [{"text": "Порог отклика: 300 ₽", "callback_data": "limit:profi_max_bid"}],
                [{"text": "← В меню", "callback_data": "menu"}],
            ]})

    class FakeStore:
        def close(self):
            pass

    monkeypatch.setattr(freelance, "_fbid", lambda: (FakeTgbot, None, None))
    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FakeStore())

    screen = bot_mod._pending_limit_reply("1500")
    codes = [code for row in screen.rows for _text, code in row]
    assert codes.count("f:home") == 1, codes
    assert "f:fb:menu" not in codes, "кнопка уводила в главное меню биржи"


def test_reminder_count_matches_reality(store, monkeypatch):
    """«Просили вернуться сегодня: N» — это N контактов, а не N строк сводки.

    Здесь считались строки готового текста сводки (пустая строка, заголовок и
    по строке на контакт) минус одна «на заголовок». Число всегда выходило на
    единицу больше правды: один отложенный контакт показывался как два.
    """
    from orbit.models import Contact, Kind, Stage
    from orbit.store import today_msk

    store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="later.ru", name="Вернуться осенью",
        emails=["hi@later.ru"], stage=Stage.REPLIED,
        meta={"remind_after": today_msk()},
    ))

    lines = bot.attention_lines(store)
    reminder = next(line for line in lines if "Просили вернуться" in line)
    assert reminder.endswith(": 1"), reminder


# --- Мёртвая модель не должна выглядеть зелёным экраном ------------------------

def test_dead_model_is_the_first_thing_on_the_main_screen(store):
    """Без модели не поедет ничего — владелец обязан увидеть это сразу.

    Раньше пульт показывал три зелёных кружка и «Тексты: модель на месте»,
    пока не писалось вообще ни одного письма и ни одного отклика: проверка
    смотрела на наличие ключа, а ключ бывает рабочим при пустом счёте.
    """
    from orbit.schedule import MODEL_STATE

    store.set_state(MODEL_STATE, "кончились деньги на счёте модели")
    lines = bot.attention_lines(store)

    assert lines, "молчать об этом нельзя"
    assert "Модель не отвечает" in lines[0]
    assert "кончились деньги" in lines[0]


def test_status_screen_repeats_the_real_model_state(store, registry, monkeypatch):
    from orbit.schedule import MODEL_STATE

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ")
    config.get_settings.cache_clear()
    store.set_state(MODEL_STATE, "кончились деньги на счёте модели")

    text = bot.route(registry, store, "status").text
    assert "не отвечает" in text
    assert "модель отвечает" not in text


def test_status_screen_is_calm_when_the_model_is_alive(store, registry, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ")
    config.get_settings.cache_clear()

    text = bot.route(registry, store, "status").text
    assert "модель отвечает" in text


def test_attention_says_where_they_replied(store):
    """Одна цифра на три системы заставляет искать, где именно ответили.

    Живой случай: владелец увидел «Ответили и ждут вас: 1», пошёл в почту —
    а единица была с биржи.
    """
    from orbit.models import Contact, Kind, Stage

    order = Contact(kind=Kind.ORDER, key="fl:1", name="Заказ с биржи", stage=Stage.REPLIED)
    order.pk = store.upsert_contact(order)
    store.set_stage(order.pk, Stage.REPLIED, "вручную")

    line = next(x for x in bot.attention_lines(store) if "Ответили" in x)
    assert "фриланс 1" in line
    assert "партнёры" not in line, "пустые вкладки перечислять незачем"


def test_letters_with_unknown_fate_stay_visible(store, agency):
    """Сообщение о них уходит один раз — экран обязан помнить дольше.

    Если Телеграм не ответил в минуту перезапуска, факт пропадал совсем:
    письмо помечено неудачным, из очереди исчезло, а на экранах его нет нигде.
    """
    from orbit.models import Channel, Draft

    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Здравствуйте.",
    ))
    store.approve_draft(draft_pk)
    store.claim_draft(draft_pk)
    store.hand_to_smtp(draft_pk)
    store.release_stale_sending()

    assert store.unclear_drafts() == 1
    line = next(x for x in bot.attention_lines(store) if "неизвестной судьбой" in x)
    assert ": 1" in line


# --- Старые нажатия после перезапуска -----------------------------------------

def test_stale_presses_are_not_replayed_when_telegram_is_silent(monkeypatch, owner_chat):
    """Сорвавшийся вопрос про старые нажатия не должен означать «переиграй всё».

    Ноль в offset — это «отдай всю накопленную очередь». Среди неё есть
    необратимое: «Очистить очередь», «Отправить все», «Не писать». Сеть
    моргнула на старте — и бот выполняет вчерашние нажатия.

    Правильный ответ здесь — ждать, а не выдумывать смещение: сколько бы
    попыток ни ушло, отвечать на старые нажатия нельзя.
    """
    tries = {"n": 0}

    def flaky(method, payload, **kw):
        tries["n"] += 1
        if tries["n"] < 40:
            return None, "сеть отвалилась"
        return [{"update_id": 900}], ""

    monkeypatch.setattr(bot, "_request", flaky)
    monkeypatch.setattr(bot.time, "sleep", lambda _seconds: None)

    assert bot._drop_stale_updates() == 901
    assert tries["n"] == 40, "сдаваться и придумывать смещение нельзя — только ждать"


def test_a_made_up_offset_would_silence_the_bot(monkeypatch, owner_chat):
    """Смещение «побольше на всякий случай» глушило бота насмерть.

    Telegram отдаёт обновления с update_id НЕ МЕНЬШЕ смещения. Настоящие
    номера — миллионы, а не миллиарды, так что бот со смещением 2^31-1 не
    получал бы вообще ничего: ни кнопок, ни команд, и молчал бы до
    перезапуска. Пожаловаться он тоже не мог — жалуется он тем же Телеграмом.
    """
    asked: list[dict] = []

    def refuse(method, payload, **kw):
        asked.append(payload)
        return None, "сеть отвалилась"

    monkeypatch.setattr(bot, "_request", refuse)
    monkeypatch.setattr(bot.time, "sleep", lambda _seconds: (_ for _ in ()).throw(TimeoutError))

    with pytest.raises(TimeoutError):
        bot._drop_stale_updates()
    assert asked, "спросить всё-таки надо"
    assert not hasattr(bot, "SAFE_SKIP_OFFSET"), "выдуманного смещения быть не должно"


def test_empty_queue_starts_from_scratch(monkeypatch, owner_chat):
    """Telegram ответил «ничего нет» — значит и пропускать нечего."""
    monkeypatch.setattr(bot, "_request", lambda method, payload, **kw: ([], ""))

    assert bot._drop_stale_updates() == 0


def test_pending_presses_are_skipped(monkeypatch, owner_chat):
    monkeypatch.setattr(
        bot, "_request", lambda method, payload, **kw: ([{"update_id": 4242}], "")
    )

    assert bot._drop_stale_updates() == 4243


# --- Чужая кнопка не должна затирать карточку клиента ---------------------------


def _registry_with_exchange(knows):
    """Реестр с одним разделом «Фриланс», который сам решает, чей это код."""
    from orbit.registry import Health, Registry, Section

    calls: list[str] = []

    def handle(store, action, arg):
        calls.append(arg)
        return bot.Screen("экран биржи", [[("⬅️", "home")]])

    registry = Registry()
    registry.add(lambda: Section(
        code="f", title="Фриланс", icon="💼",
        handle=handle, health=lambda store: Health(True, "работает"),
        knows=knows,
    ))
    return registry, calls


def test_a_foreign_code_shows_the_stale_button_screen(store):
    """Незнакомый код не уходит на биржу и показывает «кнопка устарела».

    Биржа на ЛЮБОЙ чужой код отвечала «Не понял» и подменяла собой сообщение.
    Если в нём лежал вопрос клиента и готовый ответ — они пропадали вместе с
    ним, и восстановить их было неоткуда.
    """
    registry, calls = _registry_with_exchange(knows=lambda code: False)

    screen = bot.route(registry, store, "zzz:card:7")
    assert "устарела" in screen.text
    assert calls == [], "чужой код на биржу уходить не должен"


def test_an_exchange_code_still_reaches_the_exchange(store):
    """Проверка выше не должна закрыть живые кнопки карточек биржи."""
    registry, calls = _registry_with_exchange(knows=lambda code: code.startswith("dlgsend"))

    screen = bot.route(registry, store, "dlgsend:12")
    assert screen.text == "экран биржи"
    assert calls == ["dlgsend:12"]


def test_a_section_without_knows_takes_everything_as_before(store):
    """Раздел без списка кодов работает по-старому: ломать совместимость нельзя."""
    registry, calls = _registry_with_exchange(knows=None)

    screen = bot.route(registry, store, "чтоугодно")
    assert screen.text == "экран биржи"
    assert calls == ["чтоугодно"]


def test_search_opens_an_exchange_order_in_its_own_tab(store):
    """Заказчик с биржи — не клиент холодной рассылки.

    Вкладка выбиралась вопросом «партнёр или нет», и заказ с биржи открывался
    карточкой «Клиенты»: там кнопки «Написать письмо» и «Не писать», которых
    для биржи быть не должно — разговор с ней ведёт freelance-bid.
    """
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.ORDER, key="fl:1001", name="Нужен лендинг под ключ",
        meta={"platform": "fl"},
    ))
    screen = bot._search_reply(store, "лендинг под ключ")
    assert screen is not None
    assert "Написать письмо" not in screen.text
    codes = [code for row in screen.rows for _, code in row]
    assert not any(code.startswith("c:") for code in codes), "во вкладку «Клиенты» его нельзя"
    assert store.get_contact(pk).kind is Kind.ORDER


def test_search_list_names_an_exchange_order_correctly(store):
    """В списке найденного заказ с биржи не должен называться клиентом."""
    from orbit.models import Contact, Kind

    store.upsert_contact(Contact(kind=Kind.ORDER, key="fl:1", name="Лендинг для клиники"))
    store.upsert_contact(Contact(kind=Kind.CLIENT, key="c.ru", name="Лендинг Медиа",
                                 emails=["a@c.ru"]))
    screen = bot._search_reply(store, "лендинг")
    assert "Нашлось 2" in screen.text
    assert "заказ с биржи" in screen.text
    assert screen.text.count("клиент") == 1, "клиент здесь ровно один"
    codes = [code for row in screen.rows for _, code in row]
    assert any(code.startswith("f:card:") for code in codes)
    assert any(code.startswith("c:card:") for code in codes)


def test_a_command_cancels_a_half_started_limit_edit(monkeypatch, store, owner_chat):
    """Владелец начал править лимит и передумал, нажав команду.

    Правка оставалась взведённой ещё десять минут. Следующее сообщение из
    одних цифр — хоть телефон клиента, скопированный в чат, — уходило в
    суточный бюджет площадки.
    """
    cancelled: list[str] = []
    monkeypatch.setattr(bot, "_cancel_limit_edit", lambda: cancelled.append("да"))
    monkeypatch.setattr(bot, "send", lambda chat_id, screen: None)
    monkeypatch.setattr(bot, "registry_for", lambda store: bot.Registry())

    bot.handle_update(
        {"message": {"chat": {"id": owner_chat}, "text": "/start"}},
        bot.Registry(),
        store,
    )
    assert cancelled, "правку надо снять"


def test_any_other_button_cancels_the_limit_edit(monkeypatch, store):
    """И любое другое НАЖАТИЕ тоже: кнопки это раньше не умели."""
    cancelled: list[str] = []
    monkeypatch.setattr(bot, "_cancel_limit_edit", lambda: cancelled.append("да"))

    bot.route(bot.Registry(), store, "home")
    assert cancelled == ["да"]


def test_the_limit_dialog_buttons_do_not_cancel_it(monkeypatch, store):
    """А кнопки самого диалога правку не сбрасывают."""
    cancelled: list[str] = []
    monkeypatch.setattr(bot, "_cancel_limit_edit", lambda: cancelled.append("да"))

    bot.route(bot.Registry(), store, "f:fb:limit:profi_daily_budget")
    bot.route(bot.Registry(), store, "f:fb:limits")
    assert cancelled == []


def test_a_number_meant_for_the_limit_does_not_cancel_it(monkeypatch, store, owner_chat):
    """Число, которое биржа приняла как новый лимит, правку не отменяет."""
    cancelled: list[str] = []
    monkeypatch.setattr(bot, "_cancel_limit_edit", lambda: cancelled.append("да"))
    monkeypatch.setattr(bot, "send", lambda chat_id, screen: None)
    monkeypatch.setattr(
        bot, "_pending_limit_reply", lambda text: bot.Screen("лимит изменён", [])
    )

    bot.handle_update(
        {"message": {"chat": {"id": owner_chat}, "text": "1500"}},
        bot.Registry(),
        store,
    )
    assert cancelled == [], "это и был ответ на правку"


def test_a_message_that_means_nothing_lets_the_edit_go(monkeypatch, store, owner_chat):
    """А сообщение, которое не подошло ни правке, ни поиску, её снимает.

    Дальше владелец видит главное меню — значит от правки он ушёл, и держать
    её взведённой ещё десять минут не за чем.
    """
    cancelled: list[str] = []
    monkeypatch.setattr(bot, "_cancel_limit_edit", lambda: cancelled.append("да"))
    monkeypatch.setattr(bot, "send", lambda chat_id, screen: None)
    monkeypatch.setattr(bot, "_pending_limit_reply", lambda text: None)
    monkeypatch.setattr(bot, "_search_reply", lambda store, text: None)

    bot.handle_update(
        {"message": {"chat": {"id": owner_chat}, "text": "как дела"}},
        bot.Registry(),
        store,
    )
    assert cancelled == ["да"]


# --- Слетевший вход на биржу виден на пульте ------------------------------------


def test_a_lost_exchange_session_is_first_on_the_main_screen(store, monkeypatch):
    """Пока вход лежит, на биржу не уходят ни отклики, ни ответы клиентам.

    Снаружи мёртвая площадка неотличима от «сегодня нет подходящих заказов»,
    а чинится это только руками владельца. Вкладка при этом показывала бодрое
    «работает».
    """
    monkeypatch.setattr(bot, "_lost_exchange_sessions", lambda: ["fl"])

    lines = bot.attention_lines(store)
    assert lines and "Вход на FL.ru слетел" in lines[0], lines
    assert "fbid login fl" in lines[0]


def test_a_silent_mailbox_is_on_the_main_screen_too(store, monkeypatch):
    """Молчащий ящик — то же самое для почты."""
    from orbit.schedule import INBOX_STATE

    monkeypatch.setattr(bot, "_lost_exchange_sessions", list)
    store.set_state(INBOX_STATE, "сервер не отвечает")

    lines = bot.attention_lines(store)
    assert any("Ящик не читается" in line for line in lines), lines


def test_a_healthy_day_says_nothing_about_sessions(store, monkeypatch):
    """Всё живо — лишних строк на пульте быть не должно."""
    monkeypatch.setattr(bot, "_lost_exchange_sessions", list)

    lines = bot.attention_lines(store)
    assert not any("слетел" in line or "не читается" in line for line in lines), lines


def test_the_main_screen_refreshes_the_exchange(store, monkeypatch):
    """Главный экран считал заказы по копии, которую обновляла только вкладка.

    Пока владелец не заходил во «Фриланс», в блоке «требует вас» не было ни
    «готовы к созвону», ни «ответили с биржи» — то есть самого горячего.
    """
    from orbit.sections import freelance

    calls: list[int] = []
    monkeypatch.setattr(freelance, "sync", lambda _store: calls.append(1))
    freelance._synced_at[0] = 0.0

    bot.main_menu(bot.Registry(), store)
    assert calls, "биржу не обновили вовсе"

    # А вот дважды подряд ходить в чужую базу незачем: экран перерисовывается
    # на каждое нажатие, и минуты между обновлениями достаточно.
    bot.main_menu(bot.Registry(), store)
    assert len(calls) == 1, "обновление не притормозило"


def test_a_dead_exchange_does_not_break_the_main_screen(store, monkeypatch):
    from orbit.sections import freelance

    def boom(_store):
        raise RuntimeError("базы биржи нет")

    monkeypatch.setattr(freelance, "sync", boom)
    freelance._synced_at[0] = 0.0
    screen = bot.main_menu(bot.Registry(), store)
    assert screen.text

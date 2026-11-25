"""Переписка с заказчиком после отклика.

Здесь сообщение уходит живому человеку от имени студии, и отозвать его нельзя.
Поэтому проверяются в первую очередь тормоза: незнакомое решение модели = зовём
владельца, ссылка на строгой площадке = не отправляем, нажали «Отправить» —
отправляем ровно один раз.
"""
from __future__ import annotations

import pytest

from fbid import dialog
from fbid.models import Order, Source
from fbid.store import Store


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "fbid.db")
    yield store
    store.close()

CONVERSATION = """Ваш отклик
06.08.2026 в 14:22
Здравствуйте! Мы студия UniqOre. Соберём лендинг с записью.
Заявки принимаете формой на почту или сразу в бот?
Сегодня в 09:40
Марина: А сколько по срокам и что входит в цену?
"""


def _order(source: Source = Source.FL, **kwargs) -> Order:
    data = {
        "source": source,
        "ext_id": "5516643",
        "url": "https://www.fl.ru/projects/5516643/",
        "title": "Лендинг для клиники",
        "description": "Одностраничник с записью на приём.",
    }
    data.update(kwargs)
    return Order(**data)


# --- Решение модели ---------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ответить", dialog.ACT_ANSWER),
        ("звать на созвон", dialog.ACT_CALL),
        ("передать", dialog.ACT_HANDOVER),
        ("молчать", dialog.ACT_SILENT),
        ("ОТВЕТИТЬ", dialog.ACT_ANSWER),
    ],
)
def test_known_actions(raw, expected):
    assert dialog.normalize_action(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "что-то своё", "answer"])
def test_unknown_action_calls_the_owner(raw):
    """Не поняли, что делать — зовём человека, а не пишем клиенту наугад."""
    assert dialog.normalize_action(raw) == dialog.ACT_HANDOVER


def test_sendable_only_for_real_text():
    assert dialog.DialogReply(dialog.ACT_ANSWER, "", "Отвечаю по срокам.").sendable
    assert not dialog.DialogReply(dialog.ACT_ANSWER, "", "   ").sendable
    assert not dialog.DialogReply(dialog.ACT_HANDOVER, "", "текст").sendable
    assert not dialog.DialogReply(dialog.ACT_SILENT, "", "").sendable


def test_handover_flag():
    assert dialog.DialogReply(dialog.ACT_HANDOVER, "клиент согласился на созвон", "").needs_human


# --- Проверка текста перед показом -----------------------------------------

def test_empty_and_short_answers_rejected():
    assert dialog.check_reply_rules("", _order()) == "пустое сообщение"
    assert "коротко" in dialog.check_reply_rules("Ок", _order())


def test_normal_answer_passes():
    text = "По срокам укладываемся в неделю, в цену входит вёрстка, форма и подключение записи."
    assert dialog.check_reply_rules(text, _order()) is None


def test_link_is_fine_where_allowed():
    text = "Покажу похожие работы: https://uniqore.ru, там как раз клиники в портфолио."
    assert dialog.check_reply_rules(text, _order(Source.FL)) is None


def test_link_blocked_on_strict_platform():
    """На Юду за ссылку в переписке блокируют аккаунт."""
    text = "Наши работы смотрите на https://uniqore.ru, там всё есть по клиникам."
    assert dialog.check_reply_rules(text, _order(Source.YOUDO)) is not None


# --- Промт ------------------------------------------------------------------

def test_prompt_explains_dialogue_rules():
    prompt = dialog.build_prompt(_order())
    assert prompt
    assert "НЕ представляйся заново" in prompt
    assert "КОГДА ОСТАНОВИТЬСЯ И ПОЗВАТЬ ЧЕЛОВЕКА" in prompt


def test_prompt_keeps_the_call_rule_in_sync():
    """Порог созвона в промте должен быть тем же, что в коде."""
    prompt = dialog.build_prompt(_order())
    assert f"меньше {dialog.CALL_AFTER_EXCHANGES}" in prompt


def test_brief_includes_conversation_and_counter():
    brief = dialog.conversation_brief(_order(), CONVERSATION, exchanges=1)
    assert "Марина" in brief
    assert "уже было: 1" in brief


def test_brief_warns_about_strict_platform():
    brief = dialog.conversation_brief(_order(Source.YOUDO), CONVERSATION, 0)
    assert "запрещают внешние ссылки" in brief


def test_brief_trims_huge_conversation():
    """Переписка на сто сообщений не должна раздувать запрос без предела."""
    brief = dialog.conversation_brief(_order(), "текст " * 5000, 0)
    assert len(brief) < 8000


# --- Черновик и счётчик обменов --------------------------------------------

def test_draft_roundtrip(store):
    reply = dialog.DialogReply(dialog.ACT_ANSWER, "отвечаю", "Неделя, входит вёрстка.", "про сроки")
    dialog.save_draft(store, 42, reply)
    loaded = dialog.load_draft(store, 42)
    assert loaded.reply == "Неделя, входит вёрстка."
    assert loaded.client_asked == "про сроки"


def test_missing_draft_is_none(store):
    assert dialog.load_draft(store, 999) is None


def test_broken_draft_does_not_crash(store):
    store.set_state(dialog.draft_key(7), "{это не json")
    assert dialog.load_draft(store, 7) is None


def test_clear_draft(store):
    dialog.save_draft(store, 42, dialog.DialogReply(dialog.ACT_ANSWER, "", "Текст."))
    dialog.clear_draft(store, 42)
    assert dialog.load_draft(store, 42) is None


def test_exchanges_counter(store):
    order = _order()
    assert dialog.exchanges_done(store, order) == 0
    dialog.remember_exchange(store, order)
    dialog.remember_exchange(store, order)
    assert dialog.exchanges_done(store, order) == 2


def test_broken_counter_reads_as_zero(store):
    store.set_state(dialog.state_key(_order()), "много")
    assert dialog.exchanges_done(store, _order()) == 0


# --- Одобрение отправки -----------------------------------------------------

def test_approve_requires_draft(store):
    assert dialog.approve(store, 42) is False
    dialog.save_draft(store, 42, dialog.DialogReply(dialog.ACT_ANSWER, "", "Текст."))
    assert dialog.approve(store, 42) is True
    assert dialog.approved_orders(store) == [42]


def test_approved_list_survives_garbage(store):
    store.set_state(f"{dialog.APPROVED_PREFIX}:мусор", "1")
    dialog.save_draft(store, 5, dialog.DialogReply(dialog.ACT_ANSWER, "", "Текст."))
    dialog.approve(store, 5)
    assert dialog.approved_orders(store) == [5]


# --- Пустой счёт: узнать сразу, а не биться в API по каждому клиенту -----------

def test_dialog_recognises_an_empty_account(monkeypatch):
    """429 «Баланс токенов исчерпан» — это не «модель не ответила».

    Разница для владельца решающая: во втором случае надо идти в переписку,
    в первом — пополнить счёт, и это чинится за минуту. Раньше пустой счёт
    замечала только генерация откликов, а разбор переписок бился в API по
    каждому клиенту с пятью повторами.
    """
    from fbid import config, dialog, llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ")
    config.get_settings.cache_clear()
    called: list[str] = []
    monkeypatch.setattr(llm, "money_ran_out_recently", lambda: False)
    monkeypatch.setattr(dialog, "money_ran_out_recently", lambda: False)
    monkeypatch.setattr(dialog, "report_empty_account", lambda exc: called.append(str(exc)))

    class Boom:
        def __init__(self, *a, **kw):
            pass

        # Имя со строчной буквы — повторяем форму настоящего клиента.
        class messages:
            @staticmethod
            def create(**_kw):
                raise RuntimeError(
                    "Error code: 429 - Баланс токенов исчерпан. Пополните баланс в боте."
                )

    monkeypatch.setattr(dialog, "make_client", lambda **kw: Boom())
    monkeypatch.setattr(dialog, "build_dialog_prompt", lambda: "промт")

    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
    assert dialog.next_message(order, "Клиент: сколько стоит?") is None
    assert called, "владельцу должны сказать про деньги, а не про молчание модели"


def test_dialog_stops_calling_the_api_when_money_ran_out(monkeypatch):
    """Выяснили, что счёт пуст — следующие клиенты в API не идут."""
    from fbid import config, dialog

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ")
    config.get_settings.cache_clear()
    monkeypatch.setattr(dialog, "money_ran_out_recently", lambda: True)
    monkeypatch.setattr(
        dialog, "make_client",
        lambda **kw: pytest.fail("в API ходить не должны: счёт пуст"),
    )

    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
    assert dialog.next_message(order, "Клиент: сколько стоит?") is None


# --- Напоминание молчуну — не обмен в переписке --------------------------------


def test_a_nudge_does_not_count_as_an_exchange(tmp_path, monkeypatch):
    """Два напоминания не должны давать модели повод звать на созвон.

    Счётчик обменов решает, когда модель предлагает созвониться: до двух-трёх
    обменов — не предлагает. Напоминание молчуну шло через тот же код
    отправки и увеличивало счётчик, хотя клиент на него ещё ничего не сказал.
    Первый же настоящий ответ клиента модель встречала предложением
    созвониться — ровно то, чего владелец просил не делать.
    """
    from fbid import dialog
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="1", title="Лендинг",
                      url="https://profi.ru/1", description="")
        pk, _ = store.upsert_order(order)

        nudge = dialog.DialogReply(action=dialog.ACT_ANSWER, reason="молчит",
                                   reply="Напоминаю о себе")
        dialog.save_draft(store, pk, nudge, nudge=True)
        assert dialog.load_draft(store, pk).is_nudge

        answer = dialog.DialogReply(action=dialog.ACT_ANSWER, reason="ответил",
                                    reply="Отвечаю по существу")
        dialog.save_draft(store, pk, answer)
        assert not dialog.load_draft(store, pk).is_nudge
    finally:
        store.close()


def test_only_real_answers_move_the_exchange_counter(tmp_path, monkeypatch):
    """Счётчик обменов двигает ответ, а не напоминание."""
    from fbid import dialog
    from fbid.models import Order, Source
    from fbid.store import Store

    store = Store(tmp_path / "t.db")
    try:
        order = Order(source=Source.PROFI, ext_id="2", title="Лендинг",
                      url="https://profi.ru/2", description="", budget_value=20000.0)
        pk, _ = store.upsert_order(order)

        class Adapter:
            name = "profi"

            def send_message(self, context, order_, text):
                return True

        # Проверяем счётчик, а не денежную развилку и не режим проверки.
        monkeypatch.setattr(dialog, "answers_itself", lambda st, o: True)
        monkeypatch.setattr(dialog, "approval_expired", lambda st, opk: False)
        monkeypatch.setattr(dialog.get_settings(), "dry_run", False, raising=False)

        for is_nudge in (True, True, False):
            reply = dialog.DialogReply(action=dialog.ACT_ANSWER, reason="", reply="текст")
            dialog.save_draft(store, pk, reply, nudge=is_nudge)
            assert dialog.approve(store, pk)
            assert dialog.flush_approved(store, Adapter(), None) == 1

        assert dialog.exchanges_done(store, order) == 1, (
            "обменом считается только настоящий ответ клиенту"
        )
    finally:
        store.close()

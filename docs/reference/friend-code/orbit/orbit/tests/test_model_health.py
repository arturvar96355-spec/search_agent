"""Тревога о том, что модель молчит.

Ключ на все три системы один. Когда он перестаёт работать, снаружи это
выглядит как затишье: бот жив, кнопки нажимаются, просто в очереди не
появляется писем и на биржах не уходят отклики. Ровно так и случилось
8 августа — тридцать неудачных попыток за двадцать минут остались в логе,
и никто об этом не узнал.

Второе важное свойство — не превратить тревогу в шум: одинаковое сообщение
каждые полчаса перестают читать, и настоящая новость («заработало») теряется.
"""
from __future__ import annotations

import pytest

from orbit import llm, notify, schedule


@pytest.fixture()
def model(monkeypatch):
    """Модель отвечает так, как сказал тест."""

    def answer(alive: bool, why: str = ""):
        monkeypatch.setattr(llm, "ping", lambda: (alive, why))

    return answer


class Telegram(list):
    """Перехваченные сообщения владельцу плюс выключатель доставки."""

    ok = True

    def fail(self) -> None:
        self.ok = False

    def recover(self) -> None:
        self.ok = True


@pytest.fixture()
def telegram(monkeypatch):
    sent = Telegram()

    def tell(text, **kwargs):
        if not sent.ok:
            return False
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "tell", tell)
    return sent


def _run(store) -> None:
    schedule._job_model(store)


def test_silence_is_reported(store, model, telegram):
    model(False, "кончились деньги на счёте Anthropic")
    _run(store)
    assert len(telegram) == 1
    assert "не отвечает" in telegram[0]
    assert "кончились деньги" in telegram[0]


def test_the_same_trouble_is_reported_once(store, model, telegram):
    model(False, "кончились деньги на счёте Anthropic")
    for _ in range(3):
        _run(store)
    assert len(telegram) == 1


def test_a_new_trouble_is_reported_again(store, model, telegram):
    """Деньги кончились, потом отозвали ключ — это разные новости."""
    model(False, "кончились деньги на счёте Anthropic")
    _run(store)
    model(False, "ключ не принят: недействителен или отозван")
    _run(store)
    assert len(telegram) == 2
    assert "ключ не принят" in telegram[1]


def test_recovery_is_reported(store, model, telegram):
    model(False, "кончились деньги на счёте Anthropic")
    _run(store)
    model(True)
    _run(store)
    assert "снова отвечает" in telegram[-1]


def test_recovery_is_reported_once(store, model, telegram):
    model(False, "что-то сломалось")
    _run(store)
    model(True)
    _run(store)
    _run(store)
    assert len(telegram) == 2


def test_working_model_stays_quiet(store, model, telegram):
    """Пока всё хорошо, бот про модель молчит."""
    model(True)
    _run(store)
    _run(store)
    assert telegram == []


def test_alarm_names_what_stops(store, model, telegram):
    """Владельцу важно не «ошибка API», а что именно перестало работать."""
    model(False, "кончились деньги на счёте Anthropic")
    _run(store)
    assert "письма" in telegram[0] and "отклики" in telegram[0]


# --- Недоставленная тревога -----------------------------------------------------


def test_undelivered_alarm_is_repeated(store, model, telegram):
    """Телеграм не ответил — молчать нельзя: вторая тишина поверх первой.

    Раньше состояние записывалось до отправки, и сообщение, которое не ушло,
    считалось сказанным. Владелец не узнавал о поломке никогда.
    """
    model(False, "кончились деньги на счёте Anthropic")
    telegram.fail()
    _run(store)
    assert telegram == []

    telegram.recover()
    _run(store)
    assert len(telegram) == 1


def test_undelivered_recovery_is_repeated(store, model, telegram):
    """То же правило для хорошей новости: не дошла — скажем в следующий заход."""
    model(False, "что-то сломалось")
    _run(store)
    model(True)
    telegram.fail()
    _run(store)
    assert len(telegram) == 1  # только тревога

    telegram.recover()
    _run(store)
    assert "снова отвечает" in telegram[-1]


def test_job_returns_nothing_to_the_schedule(store, model, telegram):
    """Задача отправляет сама — расписанию отдавать нечего, иначе задвоится."""
    model(False, "беда")
    assert schedule._job_model(store) is None
    model(True)
    assert schedule._job_model(store) is None


def test_check_is_in_the_schedule():
    keys = {job.key: job for job in schedule.jobs()}
    assert "model" in keys
    assert keys["model"].every == 30


# --- Как ошибка превращается в человеческие слова -----------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Error code: 400 - {'message': 'Your credit balance is too low'}", "кончились деньги"),
        ("Error code: 401 - authentication_error: invalid x-api-key", "ключ не принят"),
        ("Error code: 429 - rate_limit_error", "лимит запросов"),
        ("Error code: 529 - overloaded_error", "перегрузка"),
    ],
)
def test_api_errors_are_translated(raw, expected):
    assert expected in llm._short_error(RuntimeError(raw))


def test_unknown_error_is_shown_as_is():
    assert "странное" in llm._short_error(RuntimeError("что-то странное"))


def test_long_error_is_cut():
    assert len(llm._short_error(RuntimeError("х" * 1000))) <= 200


def test_missing_key_is_not_an_api_call(monkeypatch):
    """Без ключа в API не ходим вовсе — ответ и так известен."""
    from orbit import config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.get_settings.cache_clear()
    alive, why = llm.ping()
    assert not alive
    assert "ANTHROPIC_API_KEY" in why


def test_same_trouble_with_a_new_request_id_is_not_a_new_trouble(store, model, telegram):
    """Номер обращения в каждом ответе новый — одна поломка не должна двоиться.

    Незнакомую ошибку показываем как есть, а в ней сидит request_id. По нему
    «та же самая беда» выглядела новой, и тревога уходила каждые полчаса.
    """
    model(False, llm._short_error(RuntimeError("Error: boom, request_id: req_011AAA")))
    _run(store)
    model(False, llm._short_error(RuntimeError("Error: boom, request_id: req_011BBB")))
    _run(store)
    assert len(telegram) == 1


def test_request_id_is_scrubbed_from_the_message():
    scrubbed = llm._short_error(RuntimeError("boom req_011CdqmbiS4Z1znnXUX8zMDz"))
    assert "req_011" not in scrubbed
    assert "boom" in scrubbed


def test_more_api_errors_are_translated():
    cases = {
        "Error code: 404 - not_found_error: model: claude-x": "модель не найдена",
        "Error code: 500 - internal_server_error": "перегрузка или сбой",
        "Connection error: getaddrinfo failed": "сеть или прокси",
        "Request timed out": "сеть или прокси",
    }
    for raw, expected in cases.items():
        assert expected in llm._short_error(RuntimeError(raw)), raw


def test_ping_asks_with_a_timeout():
    """Расписание не должно вставать на десять минут из-за одной проверки."""
    assert 0 < llm.PING_TIMEOUT <= 60


# --- Правда о модели и «уже сказали» — это две разные записи -------------------


def test_a_dead_model_is_recorded_even_when_telegram_is_silent(store, monkeypatch):
    """Не принятое телеграмом сообщение не должно стирать сам факт поломки.

    Отметка была одна на двоих: и «модель лежит», и «владельцу сказали». Если
    телеграм в эту минуту не отвечал, в базу не попадало ничего — и экран бота
    показывал «модель отвечает», пока не писалось вообще ничего: ни писем, ни
    откликов, ни разбора переписок.
    """
    from orbit import llm, schedule
    from orbit.notify import tell  # noqa: F401 — импорт ради подмены ниже

    monkeypatch.setattr(llm, "ping", lambda: (False, "кончились деньги на счёте модели"))
    monkeypatch.setattr("orbit.notify.tell", lambda text, **kw: False)

    schedule._job_model(store)

    assert store.get_state(schedule.MODEL_STATE) == "кончились деньги на счёте модели"
    assert not store.get_state(schedule.MODEL_TOLD), "сказать не вышло — значит не сказали"


def test_the_alarm_repeats_until_it_is_delivered(store, monkeypatch):
    """Пока сообщение не дошло, тревога повторяется на каждом заходе."""
    from orbit import llm, schedule

    monkeypatch.setattr(llm, "ping", lambda: (False, "кончились деньги"))
    attempts: list[str] = []
    monkeypatch.setattr("orbit.notify.tell", lambda text, **kw: attempts.append(text) and False)

    schedule._job_model(store)
    schedule._job_model(store)
    assert len(attempts) == 2, "недоставленную тревогу надо повторять"

    monkeypatch.setattr("orbit.notify.tell", lambda text, **kw: attempts.append(text) or True)
    schedule._job_model(store)
    schedule._job_model(store)
    assert len(attempts) == 3, "а доставленную — не повторять"


def test_recovery_clears_the_state_at_once(store, monkeypatch):
    """Модель ожила — экраны обязаны узнать об этом сразу, до всяких сообщений."""
    from orbit import llm, schedule

    monkeypatch.setattr(llm, "ping", lambda: (False, "кончились деньги"))
    monkeypatch.setattr("orbit.notify.tell", lambda text, **kw: True)
    schedule._job_model(store)
    assert store.get_state(schedule.MODEL_STATE)

    monkeypatch.setattr(llm, "ping", lambda: (True, ""))
    # Радостное сообщение не дошло — но правда о модели уже другая.
    monkeypatch.setattr("orbit.notify.tell", lambda text, **kw: False)
    schedule._job_model(store)
    assert not store.get_state(schedule.MODEL_STATE), "модель отвечает — экран не должен пугать"
    assert store.get_state(schedule.MODEL_TOLD), "а сказать про это ещё предстоит"

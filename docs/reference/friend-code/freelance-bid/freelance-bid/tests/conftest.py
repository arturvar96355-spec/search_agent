"""Изоляция тестов от боевого .env.

Без этого тесты читают рабочий конфиг владельца: поменял он потолок трат в .env —
и «упал» тест, который к его настройкам отношения не имеет. Хуже того, зелёные тесты
переставали что-либо гарантировать, потому что проверяли не заданные значения,
а те, что случайно оказались в .env.
"""
from __future__ import annotations

import pytest

# Значения, на которых написаны тесты. ОБЯЗАНЫ совпадать с дефолтами в
# config.py: разошлись — и тесты начинают проверять конфигурацию, которой в
# бою не существует. Так уже было с PROFI_MIN_BALANCE: в config.py владелец
# убрал остаток до нуля, а здесь остались 500, и проверка трат шла по чужим
# правилам. Есть тест, который следит за этим совпадением.
TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "ANTHROPIC_API_KEY": "",
    "PROXY_URL": "",
    "DRY_RUN": "true",
    "FL_ENABLED": "true",
    "PROFI_ENABLED": "false",
    "YOUDO_ENABLED": "false",
    "FL_DAILY_BIDS": "30",
    "PROFI_DAILY_BIDS": "15",
    "PROFI_MAX_BID_PRICE": "800",
    "PROFI_MAX_BID_PCT": "2",
    "PROFI_DAILY_BUDGET": "1000",
    "PROFI_MIN_BALANCE": "0",
    "HOT_SCORE_MIN": "7",
    "EVENING_SWEEP_HOUR": "21",
    "CLIENT_DEDUP_DAYS": "14",
    "FL_TWO_PHASE": "false",
    "PROFI_TWO_PHASE": "false",
    "FL_MESSAGE_DELAY": "120",
    "PROFI_MESSAGE_DELAY": "120",
    "FL_OPENER_CHARS": "220",
    "VERDICT_ASK_ACTION": "notify",
}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Подставляет фиксированные настройки и уводит базу во временный каталог."""
    from fbid import config

    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # Профиль браузера тоже уводим во временный каталог. Это не про чистоту, а
    # про сохранность: боевой профиль держит сессию биржи, и второй Chromium
    # на том же каталоге её портит. Один тест уже добирался туда — подмена
    # open_context стояла не на том модуле и молча не срабатывала, — и вставал
    # намертво, пока работающий сборщик держал профиль.
    monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "profiles"))

    # Боевой .env тоже читается, но переменные окружения в pydantic-settings
    # имеют более высокий приоритет и перекрывают его значения.
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_test_env_matches_production_defaults():
    """Тестовое окружение не должно расходиться с дефолтами config.py.

    Разъехались — и вся денежная часть проверяется по настройкам, которых
    в бою нет. Тест живёт в conftest намеренно: он про сам conftest.
    """
    import os

    from fbid.config import Settings

    # Читаем дефолты без .env и без переменных окружения.
    saved = dict(os.environ)
    try:
        for key in TEST_ENV:
            os.environ.pop(key, None)
        defaults = Settings(_env_file=None)
    finally:
        os.environ.clear()
        os.environ.update(saved)

    mismatched = []
    for key, value in TEST_ENV.items():
        field = key.lower()
        if field not in type(defaults).model_fields:
            continue
        actual = getattr(defaults, field)
        if isinstance(actual, bool):
            same = str(actual).lower() == value.lower()
        elif isinstance(actual, (int, float)):
            same = float(actual) == float(value)
        elif actual is None:
            same = value == ""
        else:
            same = str(actual) == value
        if not same:
            mismatched.append(f"{key}: в тестах {value!r}, в config.py {actual!r}")

    assert not mismatched, "тестовое окружение отстало от боевого:\n" + "\n".join(mismatched)


def test_tests_never_touch_the_live_browser_profile():
    """Профиль биржи держит живую сессию — тестам туда нельзя.

    Проверка дешёвая, а цена промаха высокая: два Chromium на одном каталоге
    профиля либо не стартуют, либо портят сессию, и владелец узнаёт об этом
    по «сессия слетела» посреди рабочего дня.
    """
    from fbid.config import PROJECT_ROOT, get_settings

    profiles = get_settings().profiles_dir.resolve()
    assert not profiles.is_relative_to(PROJECT_ROOT.resolve()), profiles

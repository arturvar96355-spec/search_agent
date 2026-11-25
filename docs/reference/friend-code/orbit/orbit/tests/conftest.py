"""Общая обвязка тестов.

Настройки Orbit кэшируются на процесс (lru_cache), поэтому подменяем их до
первого обращения и чистим кэш между тестами: иначе один тест, включивший
почту, ломает соседний, который проверяет её отсутствие.
"""
from __future__ import annotations

import threading

import pytest

from orbit import config, mailcheck
from orbit.models import Contact, Kind
from orbit.store import Store


@pytest.fixture(autouse=True)
def clean_settings(tmp_path, monkeypatch):
    """Изолированные настройки: ни почты, ни ключей, база во временной папке."""
    for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
        "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "AUTOSEND",
        "DAILY_LIMIT_PARTNERS", "DAILY_LIMIT_CLIENTS",
    ):
        monkeypatch.delenv(name, raising=False)
    # model_config это словарь, поэтому setitem, а не setattr.
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(tmp_path / "absent.env"))
    # Боевую базу уводим во временный каталог. Своя база у теста и так есть
    # (фикстура store), но settings.db_path по умолчанию указывает на рабочую —
    # и любой код, который возьмёт путь оттуда, писал бы в живую базу, пока с
    # ней работает запущенный бот.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "orbit.db"))
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """В тестах DNS не спрашиваем: домен всегда считается живым.

    Настоящий запрос сделал бы результат тестов зависимым от сети и от того,
    жив ли сегодня чужой домен из фикстуры. Тесты самой проверки подменяют
    ответ сами — им как раз важно, что скажет DNS.

    Подмену снимаем только после того, как допишут фоновые потоки. Пачка
    писем готовится в отдельном потоке (`_spawn_writer`), и он переживал
    конец теста: подмена уже снята, а поток идёт в живой DNS с чужими
    доменами из фикстур.
    """
    monkeypatch.setattr(
        mailcheck, "domain_state", lambda domain: (mailcheck.DomainState.ACCEPTS, "тест")
    )
    yield
    for thread in threading.enumerate():
        if thread.name == "orbit-writer" and thread.is_alive():
            thread.join(timeout=5.0)


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "orbit.db")
    yield store
    store.close()


@pytest.fixture()
def agency():
    return Contact(
        kind=Kind.PARTNER,
        key="mediasfera.ru",
        name="Медиасфера",
        city="Москва",
        site="mediasfera.ru",
        emails=["hello@mediasfera.ru"],
        phones=["+74951234567"],
        telegram=["https://t.me/mediasfera"],
        meta={"pitch": "сайты + CRM", "dev_level": "none"},
    )


@pytest.fixture()
def company():
    return Contact(
        kind=Kind.CLIENT,
        key="smile-dent.ru",
        name="Стоматология «Улыбка»",
        city="Москва",
        site="smile-dent.ru",
        phones=["+74957654321"],
        meta={"rubric": "Стоматология", "rating": "4.2", "complaints": ["не дозвониться"]},
    )


def test_tests_never_touch_the_live_database():
    """С боевой базой в это время работает запущенный бот — тестам туда нельзя."""
    from orbit.config import PROJECT_ROOT, get_settings

    db = get_settings().db_path.resolve()
    assert not db.is_relative_to(PROJECT_ROOT.resolve()), db

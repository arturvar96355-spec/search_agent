"""Где сейчас живёт мини-апп. Адрес меняется при каждом запуске туннеля.

Держим его в базе, а не в настройках: настройки читаются один раз при старте
процесса, а бот работает неделями и туннель переживает по нескольку раз.
Владельцу при этом ничего копировать не надо — он жмёт кнопку в боте.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from ..store import MSK, Store

URL_KEY = "webapp:url"
AT_KEY = "webapp:at"

# Через сколько часов записанный адрес считаем протухшим. Туннель живёт, пока
# открыто окно терминала; предлагать кнопку на вчерашний адрес — значит
# отправить владельца в пустоту и заставить гадать, что сломалось.
STALE_HOURS = 20


def remember(store: Store, url: str) -> None:
    """Запоминает адрес и сразу переставляет кнопку слева от поля ввода.

    Переставляем здесь, а не при следующем старте бота: адрес меняется при
    каждом запуске туннеля, а бот работает неделями. Ждать перезапуска значило
    бы оставить владельца с кнопкой на мёртвый адрес.
    """
    url = url.strip()
    store.set_state(URL_KEY, url)
    store.set_state(AT_KEY, datetime.now(MSK).isoformat(timespec="seconds"))
    logger.info("Мини-апп доступен по адресу {}", url)
    try:
        from ..bot import publish_menu_button

        publish_menu_button(url)
    except Exception as exc:  # noqa: BLE001 — адрес записан, кнопка догонит при старте
        logger.warning("Кнопку слева переставить не вышло: {}", exc)


def current(store: Store) -> str | None:
    """Живой адрес мини-аппа. None — туннель не поднят или адрес протух."""
    url = (store.get_state(URL_KEY) or "").strip()
    if not url.startswith("https://"):
        # Telegram открывает мини-апп только по https, и подсунуть ему http
        # значит получить молчаливо неработающую кнопку.
        return None
    raw = store.get_state(AT_KEY)
    if not raw:
        return url
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return url
    if when.tzinfo is None:
        when = when.replace(tzinfo=MSK)
    if datetime.now(MSK) - when > timedelta(hours=STALE_HOURS):
        return None
    return url


def forget(store: Store) -> None:
    store.delete_state(URL_KEY)
    store.delete_state(AT_KEY)

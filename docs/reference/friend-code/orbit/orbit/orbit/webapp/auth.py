"""Кто открыл мини-апп. Вся защита приложения держится на этом файле.

Telegram передаёт в мини-апп строку initData: кто открыл, когда и подпись.
Подпись считается на секрете, выведенном из токена бота, — то есть подделать
её, не зная токена, нельзя. Проверяем ДО того, как отдать хоть одну цифру:
адрес туннеля публичный, и без проверки любой, кто его угадал, увидел бы всю
базу контактов и переписку с клиентами.

Три условия, и все обязательны:
  1. Подпись сходится — значит данные пришли от Telegram, а не придуманы.
  2. Они не протухли — иначе однажды подсмотренная ссылка работала бы вечно.
  3. Открыл владелец — тот самый chat_id, которому отвечает бот.

Отдельная дверь для разработки (ORBIT_WEBAPP_DEV=1) открывается ТОЛЬКО на
localhost и только когда переменная выставлена руками. Так мини-апп можно
смотреть в браузере на маке, не заводя туннель, и при этом дверь не остаётся
открытой в бою: в туннель приходит чужой адрес, и проверка её не пустит.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from loguru import logger

from ..config import get_settings

# Сколько живёт открытая вкладка. Телеграм сам не обновляет initData, пока
# мини-апп не переоткрыть, так что сутки — разумный предел: дольше держать
# открытой вкладку никто не будет, а подсмотренная ссылка протухнет.
MAX_AGE_SECONDS = 24 * 3600

DEV_ENV = "ORBIT_WEBAPP_DEV"
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class Visitor:
    """Кто смотрит. dev=True — открыли локально, без Telegram."""

    chat_id: str
    name: str
    dev: bool = False


class NotAllowed(Exception):
    """Гостя не пускаем. Текст уходит наружу, поэтому без подробностей."""


def dev_mode() -> bool:
    return os.getenv(DEV_ENV, "").strip() in ("1", "true", "yes")


def check(init_data: str, *, client_host: str = "") -> Visitor:
    """Пускать ли этого человека. Бросает NotAllowed, если нет."""
    settings = get_settings()
    token = (settings.telegram_bot_token or "").strip()

    if not init_data:
        # Пустая строка бывает ровно в одном законном случае: страницу открыли
        # в браузере на маке, а не в Telegram. Пускаем только локально и только
        # при явно включённом режиме разработки.
        if dev_mode() and client_host in LOCAL_HOSTS:
            logger.warning("Мини-апп открыт в режиме разработки, без проверки Telegram")
            return Visitor(chat_id=settings.telegram_chat_id or "dev", name="разработка", dev=True)
        raise NotAllowed("Откройте приложение из Telegram")

    if not token:
        raise NotAllowed("Приложение не настроено")

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    given = fields.pop("hash", "")
    if not given:
        raise NotAllowed("Подпись не пришла")

    # Строка для подписи: все поля, кроме hash, по алфавиту, через перевод
    # строки. Порядок задан Telegram и отступать от него нельзя — подпись
    # считается по байтам.
    checked = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    mine = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
    # compare_digest, а не ==: обычное сравнение строк выходит из цикла на
    # первом несовпавшем символе, и по времени ответа подпись можно подобрать.
    #
    # Сравниваем БАЙТЫ. На строках compare_digest бросает TypeError, если в
    # них есть что-то кроме ASCII, — и подпись «подделка» кириллицей роняла
    # проверку с пятисоткой вместо честного отказа. Снаружи это выглядело как
    # сломанный сервер и подсказывало, чем его ломать дальше.
    if not hmac.compare_digest(mine.encode(), given.encode("utf-8", "replace")):
        raise NotAllowed("Подпись не сходится")

    age = time.time() - _to_int(fields.get("auth_date"))
    if age > MAX_AGE_SECONDS:
        raise NotAllowed("Ссылка устарела — переоткройте приложение")

    user = _user_of(fields.get("user", ""))
    chat_id = str(user.get("id", ""))
    if not chat_id:
        raise NotAllowed("Не понял, кто открыл")
    if chat_id not in settings.allowed_chats:
        # В лог — с именем: если кто-то нашёл адрес туннеля, владелец должен
        # об этом узнать, а не гадать по молчаливым отказам.
        logger.warning("Мини-апп: чужой вход, chat_id={} имя={}", chat_id, user.get("first_name"))
        raise NotAllowed("Это приложение не для вас")

    name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    ) or "владелец"
    return Visitor(chat_id=chat_id, name=name)


def _user_of(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _to_int(raw: str | None) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0

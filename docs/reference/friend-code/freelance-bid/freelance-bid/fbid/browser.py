"""Браузер с постоянным профилем — на нём держится «полная автономность».

Логика: один раз входишь руками (SMS/капча), Chromium сохраняет куки и localStorage
в profiles/<площадка>/, дальше софт открывает тот же профиль и уже авторизован.
Слетела сессия — не пытаемся логиниться скриптом (там SMS), а зовём тебя в TG.

Playwright — опциональная зависимость: сбор лент работает и без него.
"""
from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

from .config import get_settings

# Убираем самый заметный маркер автоматизации. От серьёзного антифрода это не спасёт,
# но снимает тривиальную детекцию navigator.webdriver.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,MediaRouter",
]

# Сколько ждём, пока профиль освободит другой процесс, и как часто проверяем.
PROFILE_LOCK_WAIT = 90.0
PROFILE_LOCK_POLL = 2.0


class BrowserUnavailable(RuntimeError):
    """Playwright не установлен или не поставлен Chromium."""


class ProfileBusy(BrowserUnavailable):
    """Профиль браузера занят другим процессом.

    Наследуемся от BrowserUnavailable намеренно: везде, где обрабатывается
    «браузера нет», занятый профиль — такой же повод пропустить проход,
    а не уронить цикл.
    """


def profile_dir(source: str) -> Path:
    path = get_settings().profiles_dir / source
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def single_instance(name: str) -> Iterator[bool]:
    """Не даёт запустить второй такой же процесс. Отдаёт True, если мы первые.

    Два одновременных `fbid watch` независимо решают по одним и тем же заказам
    и могут отправить два платных отклика на один заказ: дедуп в базе спасает
    только если второй процесс успел увидеть запись первого.
    """
    path = get_settings().db_path.parent / f"{name}.pid"
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def profile_lock(source: str, *, wait: float = PROFILE_LOCK_WAIT) -> Iterator[None]:
    """Занимает профиль на время работы: один Chromium на профиль.

    Основной цикл и телеграм-бот — это два процесса, и оба умеют открывать
    браузер (цикл — по расписанию, бот — по кнопке «проверить ответы»). Два
    Chromium на одном каталоге профиля не уживаются: второй либо не стартует,
    либо портит куки, а куки здесь — это живая сессия биржи, которую владелец
    восстанавливает руками через SMS.
    """
    path = profile_dir(source) / ".lock"
    handle = path.open("w")
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ProfileBusy(
                        f"Браузер для {source} занят другим процессом дольше "
                        f"{wait:.0f} сек. Обычно это соседний запуск: "
                        f"основной цикл и бот делят один профиль."
                    ) from None
                logger.debug("[{}] жду освобождения профиля браузера", source)
                time.sleep(PROFILE_LOCK_POLL)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def open_context(source: str, *, headless: bool | None = None) -> Iterator["object"]:
    """Открывает постоянный контекст Chromium для площадки. Отдаёт BrowserContext."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright не установлен. Поставь: pip install -e '.[browser]' "
            "&& playwright install chromium"
        ) from exc

    settings = get_settings()
    is_headless = settings.browser_headless if headless is None else headless
    user_data = profile_dir(source)

    launch_kwargs: dict = {}
    if settings.proxy_for_browser:
        from .proxy import parse_proxy

        proxy = parse_proxy(settings.proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy.playwright()
            logger.info("[{}] браузер идёт через прокси {}", source, proxy.safe)

    # Профиль занимаем на всё время работы: два Chromium на одном каталоге
    # либо не стартуют, либо портят сессию биржи.
    with profile_lock(source), sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=is_headless,
            args=LAUNCH_ARGS,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            **launch_kwargs,
        )
        context.set_default_timeout(settings.browser_timeout * 1000)
        try:
            yield context
        finally:
            context.close()


def interactive_login(source: str, url: str) -> None:
    """Открывает окно браузера и ждёт, пока ты войдёшь руками.

    Всё, что нужно от тебя: залогиниться (включая SMS и капчу) и нажать Enter в терминале.
    После этого сессия лежит в профиле и переиспользуется автоматически.
    """
    logger.info("Открываю {} — войди в аккаунт руками", url)
    with open_context(source, headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        print(
            f"\n  Браузер открыт на {url}\n"
            f"  1. Войди в аккаунт (логин, SMS, капча — всё руками).\n"
            f"  2. Убедись, что видишь личный кабинет.\n"
            f"  3. Вернись сюда и нажми Enter — сессия сохранится.\n"
        )
        input("  Нажми Enter, когда авторизуешься: ")
        logger.info("Профиль сохранён в {}", profile_dir(source))

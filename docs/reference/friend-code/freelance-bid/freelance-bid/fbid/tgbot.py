"""Телеграм-бот: пульт управления софтом. Кнопки, команды, лимиты трат.

Здесь владелец делает всё, ради чего иначе пришлось бы лезть в терминал:
смотрит отчёт и последние заказы, меняет лимиты трат, ставит отклики на паузу.

Два способа управления, оба обязаны работать:

* **Кнопка «Меню» слева от поля ввода** — её рисует Telegram по списку команд,
  который бот отправляет при старте (`setMyCommands`). Без этого вызова кнопки
  просто нет, и новый человек не понимает, что боту вообще можно написать.
* **Инлайн-кнопки под сообщением** — быстрый путь для того, кто уже в чате.

Безопасность: команды принимаются ТОЛЬКО от TELEGRAM_CHAT_ID. Бот тратит деньги
и пишет клиентам от лица владельца, поэтому чужие нажатия игнорируются молча.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from loguru import logger

from . import limits
from .config import get_settings
from .store import Store

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 50

# Ключ, под которым запоминается «жду число для этого лимита».
PENDING_KEY = "bot:pending_limit"
# Сколько секунд ждём присланное значение, прежде чем забыть о правке.
PENDING_TTL = 600.0

# Идёт ли прямо сейчас проверка переписок: она долгая и открывает браузер.
_replies_running = False

# Список для кнопки «Меню» слева от поля ввода. Порядок = порядок в меню.
COMMANDS = [
    ("start", "Главное меню"),
    ("report", "Отчёт за сутки"),
    ("week", "Отчёт за неделю"),
    ("orders", "Последние заказы"),
    ("queue", "Очередь на отклик"),
    ("status", "Что сейчас происходит"),
    ("limits", "Лимиты трат"),
    ("pause", "Остановить отклики"),
    ("resume", "Возобновить отклики"),
    ("check", "Проверить ответы клиентов"),
    ("help", "Как это работает"),
]

# Слово после слэша → действие. Одно и то же действие вызывается и командой,
# и кнопкой, чтобы не расходились.
COMMAND_ACTIONS = {
    "start": "menu",
    "menu": "menu",
    "report": "stats:1",
    "week": "stats:7",
    "orders": "orders",
    "queue": "queue",
    "status": "status",
    "limits": "limits",
    "pause": "pause",
    "resume": "resume",
    "check": "replies",
    "help": "help",
}

# Главное меню. Первый ряд — то, что открывают чаще всего.
MENU = [
    [("📊 Отчёт за сутки", "stats:1"), ("📈 За неделю", "stats:7")],
    [("📥 Очередь на отклик", "queue"), ("📋 Последние заказы", "orders")],
    [("⚙️ Статус", "status")],
    [("💰 Лимиты трат", "limits")],
    [("⏸ Остановить отклики", "pause"), ("▶️ Возобновить", "resume")],
    [("🔍 Проверить ответы", "replies"), ("❓ Как это работает", "help")],
]

BACK_BUTTON = ("← В меню", "menu")

# Порядок лимитов на экране: сверху то, что трогают чаще всего.
LIMIT_ORDER = [
    "profi_daily_budget",
    "profi_max_bid_price",
    "profi_max_bid_pct",
    "profi_daily_bids",
    "fl_daily_bids",
    "min_budget",
    "max_budget",
    "hot_score_min",
    "profi_min_balance",
    "auto_reply_max_price",
]

# Короткие подписи для кнопок: полное название лимита в кнопку не влезает.
LIMIT_BUTTONS = {
    "profi_daily_budget": "💸 Бюджет Profi в сутки",
    "profi_max_bid_price": "🏷 Потолок цены отклика",
    "profi_max_bid_pct": "📐 Доля от бюджета заказа",
    "profi_daily_bids": "🔢 Откликов Profi в сутки",
    "fl_daily_bids": "🔢 Откликов FL в сутки",
    "min_budget": "💰 Минимальный бюджет заказа",
    "max_budget": "🧱 Максимальный бюджет заказа",
    "hot_score_min": "⭐ Порог мгновенного отклика",
    "profi_min_balance": "🛟 Неснижаемый остаток",
    "auto_reply_max_price": "🤖 Отвечаю сам до",
}

GREETING = (
    "👋 <b>UniqOre — автоотклик на биржах</b>\n\n"
    "Софт круглосуточно смотрит FL.ru, Profi.ru и Юду, отбирает заказы по нашему "
    "профилю и сам отправляет отклики на лучшие из них.\n\n"
    "Управление — кнопками ниже или командами через кнопку «Меню» слева от поля ввода."
)

HELP = (
    "❓ <b>Как это работает</b>\n\n"
    "<b>1. Сбор.</b> Ленты бирж читаются круглосуточно, каждую минуту.\n\n"
    "<b>2. Отсев.</b> Сначала дешёвый фильтр: не наш профиль (дизайн, SMM, тексты, "
    "видео, вакансии), бюджет ниже минимума, заказ старше суток — мимо.\n\n"
    "<b>3. Разбор.</b> Что прошло — читает модель: понимает задачу, решает, наше это "
    "или нет, оценивает работу в деньгах и пишет текст отклика с ценой, сроком и "
    "экспертным вопросом. На созвон в первом сообщении не зовём: клиент, который "
    "нам ещё не ответил, читает это как обязательство. Разговор предлагаем позже, "
    "когда переписка пошла.\n\n"
    "<b>4. Рейтинг.</b> 0–10: деньги (до 5), совпадение с нашими услугами (до 2), "
    "свежесть (до 2), заказчик (до 1). Набрал порог — отклик уходит в ту же секунду.\n\n"
    "<b>5. Очередь.</b> Кончились деньги или суточный лимит — заказы копятся, "
    "а не теряются. Освободилось — лучшие уходят первыми. Смотреть: «Очередь».\n\n"
    "<b>6. Переписка — по деньгам.</b> Заказ дешевле порога «Отвечаю сам до» — модель "
    "ведёт разговор сама и зовёт вас, когда клиент готов созвониться. Дороже порога "
    "или цена договорная — модель не пишет клиенту ничего, вам приходит карточка "
    "«крупный клиент ответил» с его сообщением, разговор ведёте вы. Порог меняется "
    "в «Лимитах». Модель читает чат ещё и затем, чтобы отличить настоящий ответ от "
    "приписки площадки: Profi.ru дописывает под нашим откликом свой блок, и по "
    "счётчику он неотличим от ответа.\n\n"
    "<b>7. Напоминания.</b> Клиент молчит почти сутки после отклика — уходит "
    "короткое сообщение вдогонку. Через сутки после него — второе, и на этом всё: "
    "третье выглядит навязчиво. Ответил клиент — напоминания прекращаются сразу.\n\n"
    "<b>Что приходит в чат:</b> отправленный отклик с текстом и ссылкой, ответ клиента, "
    "тревоги (слетела сессия, кончились деньги). Сырую ленту заказов не шлём — "
    "в чате только то, что уже отработано.\n\n"
    "<b>Деньги.</b> Отклики на Profi платные. Потолки — в «Лимитах», выше них софт "
    "не потратит ни рубля, даже если очень захочется."
)


@dataclass
class Action:
    """Что делать по нажатию кнопки или команде."""

    code: str
    arg: str = ""


def parse_action(data: str) -> Action:
    """«stats:7» → Action(code='stats', arg='7')."""
    code, _, arg = (data or "").partition(":")
    return Action(code=code.strip(), arg=arg.strip())


def parse_command(text: str) -> Action | None:
    """«/limits», «/report@my_bot», «/start 123» → действие. Не команда — None."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    word = text[1:].split()[0] if len(text) > 1 else ""
    # В группах Telegram дописывает к команде имя бота: /report@uniqore_bot.
    word = word.split("@")[0].lower()
    data = COMMAND_ACTIONS.get(word)
    return parse_action(data) if data else Action("unknown_command", word)


def keyboard(rows: list[list[tuple[str, str]]] | None = None) -> dict:
    """Инлайн-клавиатура. Без аргумента — главное меню."""
    rows = MENU if rows is None else rows
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }


def limits_keyboard(store: Store) -> dict:
    """Экран лимитов: значение прямо на кнопке, чтобы не сверяться с текстом."""
    rows = []
    for key in LIMIT_ORDER:
        # display, а не format(get(...)): бюджетных порогов может не быть
        # вовсе, и «не задан» отличается от «ноль» ровно наоборот по смыслу.
        value = limits.display(key, store)
        mark = " ✏️" if limits.overridden(key, store) else ""
        rows.append([(f"{LIMIT_BUTTONS[key]}: {value}{mark}", f"limit:{key}")])
    rows.append([BACK_BUTTON])
    return keyboard(rows)


def _call(method: str, payload: dict, *, timeout: float = 20.0) -> dict | None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    try:
        resp = httpx.post(
            API.format(token=settings.telegram_bot_token, method=method),
            json=payload,
            timeout=timeout,
        )
        data = resp.json()
        if not data.get("ok"):
            if resp.status_code == 409:
                # Два запущенных бота с одним токеном отбирают друг у друга
                # сообщения, и кнопки работают через раз. Без явного сообщения
                # это выглядит как «бот сломался».
                logger.error(
                    "Telegram: с этим токеном уже работает другой процесс. "
                    "Оставь один: pkill -f 'fbid.cli bot'"
                )
            else:
                logger.error("Telegram {} вернул: {}", method, str(data)[:200])
            return None
        return data
    except Exception as exc:  # noqa: BLE001 — бот не должен ронять основной цикл
        logger.error("Telegram {} не ответил: {}", method, exc)
        return None


def register_commands() -> bool:
    """Отдаёт Telegram список команд — от этого появляется кнопка «Меню».

    Без этого вызова слева от поля ввода нет ничего, и понять, что боту можно
    что-то написать, невозможно.
    """
    settings = get_settings()
    ok = _call(
        "setMyCommands",
        {"commands": [{"command": name, "description": text} for name, text in COMMANDS]},
    )
    # Кнопка «Меню» вместо скрепки: показывает тот самый список команд.
    # Ставим именно для нашего чата: настройка «по умолчанию» на уже открытый
    # чат не распространяется, и кнопка у владельца не появляется.
    _call(
        "setChatMenuButton",
        {"chat_id": settings.telegram_chat_id, "menu_button": {"type": "commands"}},
    )
    if ok:
        logger.info("Список команд отправлен: кнопка «Меню» в чате появилась")
    return bool(ok)


def send_menu(text: str = GREETING) -> bool:
    """Сообщение с главным меню."""
    settings = get_settings()
    return bool(
        _call(
            "sendMessage",
            {
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": keyboard(),
            },
        )
    )


# Коды кнопок, которые этот экран умеет выполнять. Нужен не самому боту —
# нужен Orbit: у систем один токен, и незнакомое нажатие приходит сюда же.
# Без этого списка биржа отвечала «Не понял» на ЛЮБОЙ чужой код, подменяя
# собой сообщение, в котором лежал вопрос клиента и готовый ответ на него.
# Список держится в согласии с разбором ниже тестом test_known_actions_match.
KNOWN_ACTIONS = frozenset({
    "stats", "menu", "help", "status", "orders",
    "dlgsend", "dlgskip",
    "queue", "queue_all", "queue_clear", "queue_clear_yes", "drop",
    "limits", "limit", "limit_reset", "limit_cancel",
    "pause", "resume", "replies",
    "unknown_command",
})


def knows(code: str) -> bool:
    """Наш ли это код кнопки. Пустой код — не наш."""
    return bool(code) and code.split(":")[0].strip() in KNOWN_ACTIONS


# Действия, которые ЯВЛЯЮТСЯ частью правки лимита. Всё остальное означает,
# что владелец передумал.
LIMIT_DIALOG_ACTIONS = frozenset({"limits", "limit", "limit_reset", "limit_cancel"})


def handle(action: Action, store: Store) -> tuple[str, dict]:
    """Выполняет действие и возвращает (текст ответа, клавиатура)."""
    from .bidder import Bidder
    from .stats import collect, render

    # Нажали что-то другое — значит правку лимита бросили. Снимаем её здесь,
    # иначе она ждёт числа ещё десять минут: следующее сообщение из одних
    # цифр (телефон клиента, скопированный в чат) уходило в суточный бюджет
    # площадки. Команды это уже умели, а кнопки — нет.
    if action.code not in LIMIT_DIALOG_ACTIONS and _pending_limit(store):
        logger.info("Правка лимита отменена: владелец нажал «{}»", action.code)
        store.delete_state(PENDING_KEY)

    if action.code == "stats":
        days = int(action.arg) if action.arg.isdigit() else 7
        return render(collect(store, days=days), days), keyboard()

    if action.code == "menu":
        return GREETING, keyboard()

    if action.code == "help":
        return HELP, keyboard()

    if action.code == "status":
        return _status_text(store), keyboard()

    if action.code == "orders":
        return _orders_text(store), keyboard()

    if action.code in ("dlgsend", "dlgskip"):
        return _dialog_action(store, action)

    if action.code == "queue":
        return _queue_text(store), queue_keyboard(store)

    if action.code == "queue_all":
        page = int(action.arg) if action.arg.isdigit() else 0
        return _queue_page_text(store, page), queue_page_keyboard(store, page)

    if action.code == "queue_clear":
        from .pipeline import queue_size

        total = queue_size(store)
        if not total:
            return "Очередь и так пуста.", keyboard()
        return (
            f"🧹 Убрать из очереди все {total} заказов?\n\n"
            "Из отчётов они не пропадут — софт просто перестанет к ним возвращаться.",
            keyboard([[("Да, очистить", "queue_clear_yes")], [("Отмена", "queue")]]),
        )

    if action.code == "queue_clear_yes":
        from .pipeline import clear_queue

        return f"🧹 Очередь очищена, убрано заказов: {clear_queue(store)}", keyboard()

    if action.code == "drop":
        from .pipeline import drop_from_queue

        # «drop:<pk>» — с короткого экрана, «drop:<pk>:<страница>» — с полного
        # списка: после удаления возвращаем на ту же страницу, а не в начало.
        pk_text, _, page_text = action.arg.partition(":")
        title = drop_from_queue(store, int(pk_text)) if pk_text.isdigit() else None
        head = (
            f"🗑 Убрал: <b>{_esc(title[:60])}</b>\n\n"
            if title
            else "Этого заказа в очереди уже нет.\n\n"
        )
        if page_text.isdigit():
            page = int(page_text)
            return head + _queue_page_text(store, page), queue_page_keyboard(store, page)
        return head + _queue_text(store), queue_keyboard(store)

    if action.code == "limits":
        return limits_text(store), limits_keyboard(store)

    if action.code == "limit":
        return _ask_limit(store, action.arg)

    if action.code == "limit_reset":
        return _reset_limit(store, action.arg)

    if action.code == "limit_cancel":
        store.delete_state(PENDING_KEY)
        return "Отменил, лимит не изменился.", limits_keyboard(store)

    if action.code == "pause":
        Bidder(store).pause()
        return (
            "⏸ <b>Отклики остановлены.</b>\n\n"
            "Сбор заказов и уведомления продолжаются — просто софт больше "
            "никому не пишет и не тратит деньги: ни откликов, ни ответов "
            "клиентам, ни напоминаний. Готовые ответы дождутся снятия "
            "стоп-крана.",
            keyboard(),
        )

    if action.code == "resume":
        Bidder(store).resume()
        if get_settings().dry_run:
            return (
                "▶️ Стоп-кран снят, но включён режим проверки (DRY_RUN): софт "
                "готовит отклики и показывает их здесь, но не отправляет.\n\n"
                "Чтобы отправлял по-настоящему, нужно выключить DRY_RUN в настройках "
                "на сервере.",
                keyboard(),
            )
        return "▶️ <b>Отклики снова идут.</b>", keyboard()

    if action.code == "replies":
        return _check_replies(store)

    if action.code == "unknown_command":
        return (
            f"Не знаю команду /{action.arg}. Вот что умею — выбери кнопкой.",
            keyboard(),
        )

    return "Не понял. Выбери кнопкой ниже.", keyboard()


def _dialog_action(store: Store, action: Action) -> tuple[str, dict]:
    """Кнопки под старыми карточками с готовым ответом клиенту.

    Новых таких карточек больше не бывает: положения тумблера «модель пишет,
    владелец нажимает» не осталось — переписка делится по деньгам заказа. Но
    кнопки живут в истории чата вечно, и нажать их можно в любой момент.
    Отвечать «отправлю» на такое нажатие нельзя — это прямая неправда:
    отправки не будет. Говорим как есть и убираем заготовку.
    """
    from . import dialog

    if not action.arg.isdigit():
        return "Не понял, по какому заказу.", keyboard()
    order_pk = int(action.arg)

    dialog.clear_draft(store, order_pk)
    store.delete_state(dialog.approved_key(order_pk))
    if action.code == "dlgskip":
        return "🗑 Хорошо, эту заготовку убрал.", keyboard()

    order = store.order_by_pk(order_pk)
    link = f'\n\n<a href="{order.url}">Открыть чат</a>' if order else ""
    return (
        "Эта заготовка из прошлой версии — кнопка под ней больше ничего не "
        "отправляет, я её убрал.\n\nСейчас переписка делится по деньгам: заказы "
        "дешевле порога «Отвечаю сам до» я веду сам, дороже — показываю вам "
        "и молчу." + link
    ), keyboard()


# Через сколько секунд после проверки переписок разрешаем следующую.
# Обход занимает минуту-другую, и всё это время бот не отвечает: сообщение не
# правится, пока обработчик не вернулся. Владелец жмёт кнопку второй раз — и
# нажатие ЖДЁТ в очереди обновлений, а потом поднимает браузер к тем же
# страницам ещё раз. Флага «идёт прямо сейчас» тут мало: к моменту второго
# нажатия первое уже закончилось, и флаг снят.
REPLIES_COOLDOWN_SECONDS = 90
REPLIES_LAST_KEY = "replies:last_run"


def _checked_recently(store: Store) -> int:
    """Сколько секунд осталось до следующей разрешённой проверки. 0 — можно."""
    from datetime import datetime, timezone

    raw = store.get_state(REPLIES_LAST_KEY)
    if not raw:
        return 0
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return 0
    passed = (datetime.now(timezone.utc) - when).total_seconds()
    return max(0, int(REPLIES_COOLDOWN_SECONDS - passed))


def _check_replies(store: Store) -> tuple[str, dict]:
    """Проверка переписок. Долгая: открывает браузер и обходит заказы.

    Второй обход подряд по тем же страницам — лишний повод для антибота на
    бирже и ничего нового не даёт. Поэтому повторное нажатие в ближайшую
    минуту-другую просто отвечает, что проверка только что была.
    """
    global _replies_running

    if _replies_running:
        return "🔍 Уже проверяю переписки, подожди немного.", keyboard()

    left = _checked_recently(store)
    if left:
        return (
            f"🔍 Переписки только что проверены. Следующая проверка — через {left} сек.\n"
            f"Сборщик и сам обходит их постоянно, вручную это нужно редко.",
            keyboard(),
        )

    _replies_running = True
    try:
        from .browser import ProfileBusy
        from .pipeline import check_replies_all

        try:
            sweep = check_replies_all(store)
        except ProfileBusy:
            # Основной цикл прямо сейчас держит браузер. Это норма, а не поломка.
            return (
                "🔍 Браузер сейчас занят основным циклом. Попробуй через минуту.",
                keyboard(),
            )
    finally:
        _replies_running = False
        _remember_replies_run(store)

    if sweep.new_replies:
        return (
            f"📨 Новых ответов от клиентов: {sweep.new_replies}. Проверьте личные кабинеты.",
            keyboard(),
        )
    if not sweep.checked:
        # Ни одной переписки не открыли. «Новых ответов нет» тут — прямая
        # неправда: мы не смотрели вовсе. Владелец прочитал бы это как
        # «клиенты молчат» и не пошёл бы проверять руками.
        why = _esc(sweep.errors[0]) if sweep.errors else "не открылась ни одна переписка"
        return (
            f"⚠️ Проверить переписки не вышло: {why}\n"
            f"Это не «ответов нет» — это «не смотрел». Загляни в личные кабинеты сам.",
            keyboard(),
        )
    tail = f" (не открылось: {len(sweep.errors)})" if sweep.errors else ""
    return (
        f"Проверил переписок: {sweep.checked}{tail}. Новых ответов нет.",
        keyboard(),
    )


def _remember_replies_run(store: Store) -> None:
    """Запоминает момент проверки. Падать здесь нельзя: обход уже состоялся."""
    from datetime import datetime, timezone

    try:
        store.set_state(REPLIES_LAST_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001 — не записали, но проверка прошла
        logger.warning("Не записал время проверки переписок: {}", exc)


# --- Экраны ---------------------------------------------------------------


def limits_text(store: Store) -> str:
    """Пояснение к экрану лимитов. Сами значения — на кнопках."""
    lines = ["💰 <b>Лимиты трат</b>", ""]
    for key in LIMIT_ORDER:
        spec = limits.SPECS[key]
        if spec.hint:
            lines.append(f"<b>{LIMIT_BUTTONS[key].split(' ', 1)[1]}</b> — {spec.hint}")
    lines += [
        "",
        "Нажми на лимит, чтобы задать новое значение. ✏️ — значение уже менялось отсюда.",
        "Изменения применяются сразу, перезапуск не нужен.",
    ]
    return "\n".join(lines)


# Сколько заказов показываем на одной странице полного списка.
QUEUE_PAGE = 8


def queue_keyboard(store: Store) -> dict:
    """Под каждым заказом — кнопка «убрать», чтобы очередь сдвинулась."""
    from .pipeline import queue_size, waiting_queue

    rows = [
        [(f"🗑 Убрать: {item.order.title[:28]}", f"drop:{item.order_pk}")]
        for item in waiting_queue(store, limit=5)
    ]
    total = queue_size(store)
    if total > 5:
        rows.append([(f"📄 Показать все ({total})", "queue_all:0")])
    if total:
        rows.append([("🧹 Очистить очередь", "queue_clear")])
    rows.append([BACK_BUTTON])
    return keyboard(rows)


def queue_page_keyboard(store: Store, page: int = 0) -> dict:
    """Страница полного списка: убрать любой заказ и листать дальше."""
    from .pipeline import listed_queue_size, waiting_queue

    offset = page * QUEUE_PAGE
    items = waiting_queue(store, limit=QUEUE_PAGE, offset=offset)
    rows = [
        [(f"🗑 {offset + number}. {item.order.title[:26]}", f"drop:{item.order_pk}:{page}")]
        for number, item in enumerate(items, start=1)
    ]

    # Считаем по ЛИСТАЕМОЙ части, а не по всей очереди: экран показывает
    # свежий хвост базы, и «Дальше →» по полному размеру вело в пустоту.
    listed = listed_queue_size(store)
    nav = []
    if page > 0:
        nav.append(("← Назад", f"queue_all:{page - 1}"))
    if offset + QUEUE_PAGE < listed:
        nav.append(("Дальше →", f"queue_all:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("🧹 Очистить", "queue_clear"), BACK_BUTTON])
    return keyboard(rows)


def _queue_page_text(store: Store, page: int = 0) -> str:
    """Страница полного списка очереди."""
    from .pipeline import listed_queue_size, queue_size, waiting_queue

    total = queue_size(store)
    if not total:
        return "📥 <b>Очередь пуста</b>"

    offset = page * QUEUE_PAGE
    items = waiting_queue(store, limit=QUEUE_PAGE, offset=offset)
    if not items:
        return "📥 Дальше пусто — это была последняя страница."

    # Страниц столько, сколько реально можно пролистать: экран смотрит свежий
    # хвост базы. Считать их по всей очереди значит обещать «страница 1 из 12»
    # там, где страниц три.
    listed = listed_queue_size(store)
    pages = max(1, (listed + QUEUE_PAGE - 1) // QUEUE_PAGE)
    head = f"📥 <b>Вся очередь: {total} заказов</b> (страница {page + 1} из {pages})"
    if listed < total:
        head += f"\nЛистаются {listed} свежих — остальные ждут своей очереди."
    lines = [head, ""]
    for number, item in enumerate(items, start=offset + 1):
        order = item.order
        budget = (
            f"{order.budget_value:,.0f} ₽".replace(",", " ")
            if order.budget_value
            else "по договорённости"
        )
        lines.append(
            f'{number}. ⭐ {item.score:.1f} · '
            f'<a href="{_esc(order.url)}">{_esc(order.title[:50])}</a>'
        )
        line = f"   {SOURCES.get(order.source.value, order.source.value)} · {budget}"
        if order.bid_price:
            line += f" · отклик {order.bid_price:.0f} ₽"
        lines.append(line)
    lines.append("")
    lines.append("Кнопка с номером убирает заказ из очереди.")
    return "\n".join(lines)


def _queue_text(store: Store) -> str:
    """Что уйдёт следующим, когда освободятся деньги и лимиты."""
    from .pipeline import waiting_queue

    queue = waiting_queue(store, limit=5)
    if not queue:
        return (
            "📥 <b>Очередь пуста</b>\n\n"
            "Всё подходящее уже отработано. Как появятся новые заказы, "
            "лучшие уйдут сами."
        )

    lines = [
        "📥 <b>Очередь на отклик</b>",
        "",
        "Эти уйдут первыми, как только освободятся деньги и суточный лимит:",
        "",
    ]
    for number, item in enumerate(queue, start=1):
        order = item.order
        budget = (
            f"{order.budget_value:,.0f} ₽".replace(",", " ")
            if order.budget_value
            else "по договорённости"
        )
        lines.append(
            f'{number}. ⭐ {item.score:.1f} · '
            f'<a href="{_esc(order.url)}">{_esc(order.title[:55])}</a>'
        )
        line = f"   {SOURCES.get(order.source.value, order.source.value)} · {budget}"
        if order.bid_price:
            line += f" · отклик {order.bid_price:.0f} ₽"
        lines.append(line)
        lines.append(f"   <i>{_esc(item.reason[:70])}</i>")
        lines.append("")
    lines.append("Ненужное можно убрать кнопкой — следующие поднимутся на его место.")
    return "\n".join(lines)


def _orders_text(store: Store) -> str:
    """Последние заказы с рейтингом и тем, что софт по ним решил."""
    rows = store.conn.execute(
        """SELECT o.title, o.source, o.url, o.budget_value, b.status, b.reason
           FROM orders o LEFT JOIN bids b ON b.order_pk = o.pk
           ORDER BY o.pk DESC LIMIT 8"""
    ).fetchall()
    if not rows:
        return "База пустая — софт ещё не собрал ни одного заказа."
    lines = ["📋 <b>Последние заказы</b>", ""]
    for row in rows:
        budget = (
            f"{row['budget_value']:,.0f} ₽".replace(",", " ")
            if row["budget_value"]
            else "по договорённости"
        )
        verdict = _verdict_line(row["status"], row["reason"])
        lines.append(f'<a href="{row["url"]}">{_esc(row["title"][:60])}</a>')
        lines.append(f"   {SOURCES.get(row['source'], row['source'])} · {budget} · {verdict}")
        lines.append("")
    return "\n".join(lines)


SOURCES = {"fl": "FL.ru", "profi": "Profi.ru", "youdo": "Юду"}

_VERDICTS = {
    "sent": "✅ отклик отправлен",
    "dry_run": "📝 заход готов (режим проверки)",
    "planned": "⏳ в очереди на отправку",
    "failed": "⚠️ не удалось отправить",
    "unavailable": "🚫 биржа не даёт откликнуться",
}


def _verdict_line(status: str | None, reason: str | None) -> str:
    if status is None:
        return "⏳ ещё не разбирали"
    known = _VERDICTS.get(status)
    if known:
        return known
    short = (reason or "").split("—")[0].strip()
    if status == "skipped":
        return f"⏸ отложен: {short[:45] or 'ждёт своей очереди'}"
    return f"❌ не берём: {short[:45] or 'не наш профиль'}"


def _esc(text: str) -> str:
    import html

    return html.escape(text or "", quote=False)


def _pending_limit(store: Store) -> str | None:
    """Какой лимит мы сейчас ждём. None — не ждём никакого.

    Запись живёт PENDING_TTL секунд. Без срока годности брошенная правка
    висела бы вечно, и следующее сообщение владельца («как дела») ушло бы
    в разбор числа вместо меню.
    """
    raw = store.get_state(PENDING_KEY)
    if not raw:
        return None
    key, _, stamp = raw.partition("|")
    if key not in limits.SPECS:
        return None
    try:
        started = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if (datetime.now(timezone.utc) - started).total_seconds() > PENDING_TTL:
        store.delete_state(PENDING_KEY)
        return None
    return key


def cancel_pending_limit(store: Store) -> str | None:
    """Снимает взведённую правку лимита. Отдаёт, какой лимит ждали, или None.

    Нужна тому, кто перехватывает сообщения владельца раньше нас: у систем
    один бот, и команда («/start», «/limits») приходит мимо этого экрана.
    Правка при этом оставалась взведённой ещё десять минут — и следующее
    сообщение из одних цифр, хоть телефон клиента, уходило в суточный бюджет
    площадки.
    """
    key = _pending_limit(store)
    store.delete_state(PENDING_KEY)
    return key


def _ask_limit(store: Store, key: str) -> tuple[str, dict]:
    """Запоминает, какой лимит правим, и просит прислать число."""
    spec = limits.SPECS.get(key)
    if spec is None:
        return "Такого лимита нет.", limits_keyboard(store)

    store.set_state(PENDING_KEY, f"{key}|{datetime.now(timezone.utc).isoformat()}")
    rows = [
        [("↩️ Вернуть заводское значение", f"limit_reset:{key}")],
        [("✖️ Отмена", "limit_cancel")],
    ]
    text = (
        f"✏️ <b>{spec.title}</b>\n\n"
        f"Сейчас: <b>{limits.display(key, store)}</b>\n"
        f"Можно задать от {spec.format(spec.minimum)} до {spec.format(spec.maximum)}.\n"
    )
    if spec.hint:
        text += f"\n<i>{spec.hint}</i>\n"
    text += "\n<b>Пришли новое значение числом следующим сообщением.</b>"
    return text, keyboard(rows)


def _reset_limit(store: Store, key: str) -> tuple[str, dict]:
    spec = limits.SPECS.get(key)
    if spec is None:
        return "Такого лимита нет.", limits_keyboard(store)
    store.delete_state(PENDING_KEY)
    value = limits.reset(store, key)
    return (
        f"↩️ {spec.title}: вернул заводское значение — {spec.format(value)}",
        limits_keyboard(store),
    )


def apply_pending_limit(store: Store, text: str) -> tuple[str, dict] | None:
    """Если ждём число для лимита — применяет его. Иначе None."""
    key = _pending_limit(store)
    if key is None:
        return None

    spec = limits.SPECS[key]
    requested = _requested_number(text)
    if requested is None:
        return (
            f"«{_esc(text[:40])}» — это не число. Пришли, например, 1500.",
            keyboard([[("✖️ Отмена", "limit_cancel")]]),
        )

    value = limits.set_value(store, key, requested)
    store.delete_state(PENDING_KEY)

    note = ""
    if abs(requested - value) > 0.01:
        # Значение упёрлось в границу — сказать об этом обязательно, иначе
        # владелец будет думать, что лимит стоит там, куда он его поставил.
        note = (
            f"\n\n⚠️ Прислано {requested:.0f}, а допустимо от {spec.format(spec.minimum)} "
            f"до {spec.format(spec.maximum)}. Поставил {spec.format(value)}."
        )
    return f"✅ <b>{spec.title}: {spec.format(value)}</b>{note}", limits_keyboard(store)


def _requested_number(text: str) -> float | None:
    """Разбирает присланное значение тем же способом, что и запись лимита."""
    try:
        return limits.to_number(text)
    except ValueError:
        return None


def _status_text(store: Store) -> str:
    from .bidder import Bidder
    from .models import Source

    s = get_settings()
    paused = Bidder(store).is_paused()

    if s.dry_run:
        mode = "🟡 режим проверки: отклики готовятся, но не отправляются"
    elif paused:
        # Стоп-кран останавливает ровно то, что тратит деньги, — новые отклики.
        # Ответы клиентам, которые владелец уже одобрил, продолжают уходить:
        # человек, ждущий ответа на свой вопрос, не должен упереться в тишину
        # из-за того, что мы придержали расходы. Пишем это прямо, чтобы
        # «остановлено» не читалось как «остановлено всё».
        # Стоп-кран останавливает всё, что уходит клиенту от нашего имени:
        # и отклики, и ответы, и напоминания. Здесь стояло «ответы клиентам
        # уходят» — так было раньше, и это было неправдой уже тогда: кнопка
        # обещает «софт больше никому не пишет».
        mode = "⏸ остановлено: ни откликов, ни ответов клиентам"
    else:
        mode = "🟢 работает, отклики уходят"

    # Зелёный знак означает «отклики уходят». Две беды делают его неправдой, и
    # обе чинятся только владельцем: слетевший вход на площадку и пустой счёт
    # модели (без неё не пишется ни один заход). Экран называется «Что сейчас
    # происходит» — и при мёртвой сессии FL и нулевом балансе он всё равно
    # писал «работает». Это ровно тот случай, ради которого экран и открывают.
    from .bidder import MODEL_STATE_KEY
    from .pipeline import lost_sessions

    troubles: list[str] = []
    for platform in lost_sessions(store):
        troubles.append(
            f"🛑 вход на {SOURCES.get(platform, platform)} слетел — "
            f"нужна команда <code>fbid login {platform}</code>"
        )
    # Из базы, а не из памяти: бот и проход — разные процессы.
    model = store.get_state(MODEL_STATE_KEY)
    if model:
        troubles.append(f"🛑 {_esc(model)} — заходы не пишутся ни на одной площадке")
    if troubles:
        mode = "🛑 работает не всё"

    enabled = (
        ("FL.ru", s.fl_enabled),
        ("Profi.ru", s.profi_enabled),
        ("Юду", s.youdo_enabled),
    )
    platforms = ", ".join(name for name, on in enabled if on) or "нет включённых"

    lines = [
        "⚙️ <b>Что сейчас происходит</b>",
        "",
        f"Состояние: {mode}",
        f"Площадки: {platforms}",
        f"Заказов в базе: {store.orders_count()}",
        f"Отклик уходит сразу от: {limits.get('hot_score_min', store):.1f} из 10",
    ]
    if troubles:
        lines += ["", *troubles]
    lines += ["", "<b>Сегодня:</b>"]
    for src, limit_key in ((Source.FL, "fl_daily_bids"), (Source.PROFI, "profi_daily_bids")):
        bids, amount = store.spend_today(src)
        limit = limits.get(limit_key, store)
        line = f"  {SOURCES[src.value]}: откликов {bids} из {limit:.0f}"
        if src is Source.PROFI:
            budget = limits.get("profi_daily_budget", store)
            line += f", потрачено {amount:.0f} из {budget:.0f} ₽"
        lines.append(line)

    # Остаток на самих площадках. Это две цифры, от которых зависит, может ли
    # софт сегодня работать: пустой кошелёк Profi и пустой пакет FL
    # останавливают отклики полностью. Раньше они жили только в логе.
    from .pipeline import known_balance

    rest = []
    for src, word in ((Source.PROFI, "₽"), (Source.FL, "откликов в пакете")):
        known = known_balance(store, src)
        if known is None:
            continue
        value, when = known
        line = f"  {SOURCES[src.value]}: {value:g} {word}"
        # Рубли сами по себе ни о чём не говорят: отклик на Profi стоит от ста
        # до четырёхсот, и «170 ₽» — это не «мало денег», а «один отклик».
        left = bids_left(store, src, value)
        if left is not None:
            line += f" — это {_counted_bids(left)}"
        rest.append(line + _ago(when))
    if rest:
        lines += ["", "<b>Осталось на площадке:</b>", *rest]
    return "\n".join(lines)


def bids_left(store: Store, source, money: float) -> int | None:
    """На сколько ещё откликов хватит остатка. None — считать не по чему.

    Средняя цена берётся по последним отправкам, а не по потолку из настроек:
    потолок — это сколько мы согласны заплатить, а не сколько площадка берёт.
    """
    from .models import Source

    if source is not Source.PROFI:
        return None
    average = store.average_bid_cost(source)
    if not average:
        return None
    return int(money // average)


def _counted_bids(number: int) -> str:
    """«примерно 3 отклика». Своего склонения в проекте нет — считаем здесь."""
    if number <= 0:
        return "меньше чем на один отклик"
    tail = number % 10
    hundred = number % 100
    if tail == 1 and hundred != 11:
        word = "отклик"
    elif tail in (2, 3, 4) and hundred not in (12, 13, 14):
        word = "отклика"
    else:
        word = "откликов"
    return f"примерно {number} {word}"


def _ago(when: str) -> str:
    """«(проверено 20 минут назад)». Пусто, если время не разобрать.

    Возраст цифры здесь важнее самой цифры: остаток читается во время прохода,
    и вчерашнее число без пометки читалось бы как сегодняшнее.
    """
    from datetime import datetime, timezone

    from .store import as_utc

    moment = as_utc(when)
    if moment is None:
        return ""
    minutes = int((datetime.now(timezone.utc) - moment).total_seconds() // 60)
    if minutes < 1:
        return " (только что)"
    if minutes < 60:
        return f" (проверено {minutes} мин назад)"
    hours = minutes // 60
    if hours < 24:
        return f" (проверено {hours} ч назад)"
    return f" (проверено {hours // 24} сут назад)"


def run(poll_timeout: int = POLL_TIMEOUT) -> None:
    """Бесконечный цикл приёма команд. Запускается командой `fbid bot`."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.error("Telegram не настроен — бот не запущен")
        return

    allowed = str(settings.telegram_chat_id)
    store = Store(settings.db_path)
    logger.info("Бот запущен, слушаю команды. Разрешённый чат: {}", allowed)
    register_commands()
    send_menu()

    offset = _drop_stale_updates()
    try:
        while True:
            data = _call(
                "getUpdates",
                {"offset": offset, "timeout": poll_timeout},
                timeout=poll_timeout + 15,
            )
            if not data:
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    _handle_update(update, store, allowed)
                except Exception as exc:  # noqa: BLE001 — одно нажатие не гасит бота
                    logger.exception("Ошибка обработки команды: {}", exc)
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
    finally:
        store.close()


def _drop_stale_updates() -> int:
    """Пропускает всё, что накопилось, пока бот был выключен.

    Иначе после ночного простоя бот при старте разом отработает десяток старых
    нажатий — например, дважды снимет стоп-кран.
    """
    data = _call("getUpdates", {"offset": -1, "timeout": 0}, timeout=20)
    results = (data or {}).get("result") or []
    if not results:
        return 0
    last = results[-1]["update_id"] + 1
    logger.info("Пропускаю накопившиеся сообщения, начинаю с обновления {}", last)
    return last


def _reply(chat_id: str, text: str, markup: dict) -> None:
    from .notify import fit_to_limit

    _call(
        "sendMessage",
        {
            "chat_id": chat_id,
            # Длинный отчёт Telegram просто отвергает, и вместо ответа человек
            # видит тишину. Режем сами, не ломая разметку.
            "text": fit_to_limit(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": markup,
        },
    )


def _handle_update(update: dict, store: Store, allowed: str) -> None:
    message = update.get("message")
    if message:
        chat_id = str(message.get("chat", {}).get("id"))
        if chat_id != allowed:
            logger.warning(
                "Сообщение из чата {} — игнорирую (разрешён только {}). "
                "Если это твой чат, поправь TELEGRAM_CHAT_ID в .env",
                chat_id,
                allowed,
            )
            return
        text = message.get("text", "")

        command = parse_command(text)
        if command is not None:
            # Команда важнее незакрытой правки лимита: человек передумал.
            store.delete_state(PENDING_KEY)
            _reply(chat_id, *handle(command, store))
            return

        applied = apply_pending_limit(store, text)
        if applied is not None:
            _reply(chat_id, *applied)
            return

        _reply(chat_id, GREETING, keyboard())
        return

    query = update.get("callback_query")
    if not query:
        return

    chat_id = str(query.get("message", {}).get("chat", {}).get("id"))
    if chat_id != allowed:
        logger.warning(
            "Нажатие из чата {} — игнорирую (разрешён только {})", chat_id, allowed
        )
        _call("answerCallbackQuery", {"callback_query_id": query["id"]})
        return

    action = parse_action(query.get("data", ""))
    # Отвечаем на нажатие сразу: иначе кнопка «крутится» до конца работы.
    _call("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Секунду…"})

    text, markup = handle(action, store)
    _reply(chat_id, text, markup)

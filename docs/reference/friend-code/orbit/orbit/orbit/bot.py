"""Телеграм-бот Orbit: один вход во все системы.

Устройство простое. Кнопка присылает код вида «раздел:действие:аргумент»,
роутер находит раздел в реестре и отдаёт ему остальное. Раздел возвращает
экран, бот его показывает. Ничего про содержимое систем бот не знает.

ВАЖНО про токен: с одним токеном может работать только один процесс. Orbit
заменяет собой отдельных ботов систем («fbid bot», «parser bot»), а не
дополняет их. Если запустить их вместе, Telegram начнёт отдавать обновления
то одному, то другому, и половина нажатий будет пропадать — поэтому бот
явно ловит эту ситуацию и пишет о ней, а не молчит.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import time
from contextlib import contextmanager

import httpx
from loguru import logger

from .config import get_settings
from .models import STAGE_TITLES, Kind
from .registry import Registry, build
from .store import Store
from .ui import WEBAPP_PREFIX, Screen, clip, esc

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 25
# Пауза после сетевой ошибки: без неё падение сети превращается в бесконечный
# цикл запросов и забитый лог.
RETRY_PAUSE = 5.0
# Через сколько неудачных попыток спросить Telegram про накопившиеся нажатия
# перестаём писать в лог по строке на попытку и переходим на раз в минуту.
STALE_SKIP_ATTEMPTS = 5

# Команды в кнопке «Меню» слева от поля ввода. Разделы дописывают свои.
BASE_COMMANDS = [
    ("start", "Главное меню"),
    ("funnel", "Воронка в цифрах"),
    ("digest", "Итоги дня"),
    ("status", "Что работает прямо сейчас"),
    ("app", "Пульт: вся статистика в одном окне"),
    ("ready", "Готовность: что настроено, что ждёт вас"),
    ("help", "Как это устроено"),
]

GREETING = (
    "<b>🎛 UniqOre Orbit</b>\n"
    "Все системы студии в одном месте."
)

HELP = (
    "<b>❓ Как это устроено</b>\n\n"
    "<b>👥 Партнёры.</b> Агентства без своей разработки: SMM, таргет, digital. "
    "Они приводят клиенту трафик, но сайт или CRM сделать не могут. Мы заходим "
    "белым субподрядом: они продают под своим брендом, мы делаем руками.\n\n"
    "<b>🏢 Клиенты.</b> Компании с карт. Работают так же, как партнёры: есть почта — "
    "уходит письмо, есть телеграм — сообщение туда, а у кого нашёлся только номер, "
    "те попадают в отдельный список на обзвон с карточкой и зацепкой для разговора. "
    "Письмо идёт от их боли: жалобы на недозвон в отзывах, нет сайта, заявкам некуда падать.\n\n"
    "<b>💼 Фриланс.</b> Биржи FL.ru, Profi.ru и Юду. Заказы собираются круглосуточно, "
    "модель читает каждый и пишет отклик. Дальше сделка живёт по стадиям: "
    "откликнулись, начали общение, готовы к созвону, взяли в работу.\n\n"
    "<b>На биржах разговор делится по деньгам.</b> Заказ дешевле порога «Отвечаю сам "
    "до» — модель ведёт переписку сама и зовёт вас, когда клиент готов созвониться. "
    "Дороже порога или цена договорная — модель клиенту не пишет ничего: вам приходит "
    "карточка «крупный клиент ответил» с его сообщением и ссылкой на чат, разговор "
    "ведёте вы. Порог задаётся во вкладке «Фриланс» → «Лимиты трат».\n\n"
    "Кнопка «Клиент ответил» там же переключает всё разом: отвечать самой (в пределах "
    "порога) или только уведомлять.\n\n"
    "<b>Молчит — напомним дважды.</b> Первое напоминание уходит почти через сутки "
    "после отклика: если человека остановила цена или срок, предлагаем обсудить. "
    "Второе — ещё через сутки, короткое и прощальное. Третьего нет: дальше это "
    "уже навязчивость.\n\n"
    "<b>Во вкладке «Начали общение»</b> только те, кто нам действительно написал. "
    "Ваши собственные сообщения клиенту и приписки самой площадки за ответ не считаются.\n\n"
    "<b>Тексты.</b> Каждое письмо модель пишет под конкретную компанию. Шаблонов нет "
    "намеренно: одинаковые письма с одного домена узнают и получатель, и спам-фильтр.\n\n"
    "<b>Режимы рассылки.</b> Почта и телеграм включаются отдельно: «сама» или «по кнопке». "
    "В автоматическом режиме модель пишет текст и сразу отправляет, по одному письму "
    "с паузой в минуту-две — пачка за секунду выглядит как рассылка.\n\n"
    "<b>Лимит.</b> Двадцать писем в день на вкладку. Новый ящик, разославший сотню "
    "писем в первый же день, уезжает в спам вместе с доменом.\n\n"
    "<b>Телеграм.</b> Бот не может написать человеку первым — это ограничение Telegram, "
    "а не недоделка. Поэтому такие сообщения пока готовятся, а отправляются вашим "
    "нажатием через «Открыть чат». Автоматическая рассылка в телеграм появится, когда "
    "подключим отдельные аккаунты."
)


def _request(method: str, payload: dict, *, timeout: float = 20.0) -> tuple[dict | None, str]:
    """Запрос к Telegram: (результат, описание ошибки).

    Описание нужно вызывающему: Telegram отказывает по очень разным поводам, и
    часть отказов означает «всё в порядке» (см. edit()). Отличить их можно
    только по тексту, поэтому наружу отдаём и его.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN не задан — боту нечем работать")
        return None, "нет токена"
    url = API.format(token=settings.telegram_bot_token, method=method)
    try:
        response = httpx.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.warning("Telegram {} не ответил: {}", method, exc)
        return None, str(exc)

    if response.status_code == 409:
        # Тот же токен опрашивает кто-то ещё. Молча продолжать нельзя:
        # нажатия будут теряться через одно, и это выглядит как «бот сломался».
        logger.error(
            "Тем же токеном работает другой процесс. Остановите «fbid bot» и "
            "«parser bot»: Orbit заменяет их, а не дополняет."
        )
        return None, "чужой процесс с тем же токеном"
    if response.status_code != 200:
        description = ""
        try:
            description = str(response.json().get("description") or "")
        except ValueError:
            description = response.text[:200]
        logger.warning("Telegram {} вернул {}: {}", method, response.status_code, description[:200])
        return None, description
    return response.json().get("result"), ""


def _call(method: str, payload: dict, *, timeout: float = 20.0) -> dict | None:
    """Запрос к Telegram. Возвращает результат или None, если не вышло."""
    return _request(method, payload, timeout=timeout)[0]


def send(chat_id: str, screen: Screen) -> None:
    _call("sendMessage", {
        "chat_id": chat_id,
        "text": screen.text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": screen.markup,
    })


# Telegram отвечает отказом, когда правка ничего не меняет. Для нас это не
# ошибка, а самый обычный случай: «Лимит: 20» при лимите 20, «Ответил» у того,
# кто уже отмечен, повторное нажатие вкладки. Слово в ответе одно и то же.
NOT_MODIFIED = "message is not modified"


def edit(chat_id: str, message_id: int, screen: Screen) -> bool:
    """Перерисовывает сообщение. False — значит надо слать новое.

    «Ничего не изменилось» — это True: экран у владельца уже правильный, и
    слать копию нельзя. Раньше слали, и чат зарастал одинаковыми сообщениями
    от любого повторного нажатия — а вместе с ними уезжала вверх та карточка,
    ради которой чат и открывали.
    """
    result, error = _request("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": screen.text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": screen.markup,
    })
    if result is not None:
        return True
    return NOT_MODIFIED in error.lower()


def register_commands(registry: Registry) -> bool:
    """Ставит список команд и кнопку слева от поля ввода.

    Это две РАЗНЫЕ вещи, и раньше они делали одно и то же. Список команд
    вылезает сам, стоит начать набирать «/», — отдельная кнопка ради него не
    нужна. А кнопка слева стоит на самом видном месте чата, и занимать его
    дубликатом того, что и так под рукой, — расточительство: туда просится
    пульт, ради которого чат чаще всего и открывают.
    """
    commands = [{"command": name, "description": text} for name, text in BASE_COMMANDS]
    for section in registry.sections:
        for name, description, _action in section.commands:
            commands.append({"command": name, "description": description})

    ok = _call("setMyCommands", {"commands": commands}) is not None
    publish_menu_button()
    return ok


def publish_menu_button(url: str | None = None) -> bool:
    """Кнопка слева от поля ввода: пульт, если он поднят, иначе команды.

    url передают снаружи (скрипт запуска знает адрес раньше базы). Не передали
    — спрашиваем базу сами.

    Когда туннеля нет, возвращаем кнопку команд, а не оставляем мёртвый
    пульт: нажатие на протухший адрес открывает белый экран, и владелец идёт
    искать поломку там, где её нет.
    """
    settings = get_settings()
    if not settings.telegram_chat_id:
        return False
    if url is None:
        store = Store(settings.db_path)
        try:
            url = _webapp_url(store)
        finally:
            store.close()
    button = (
        {"type": "web_app", "text": "Пульт", "web_app": {"url": url}}
        if url
        else {"type": "commands"}
    )
    done = _call("setChatMenuButton", {
        "chat_id": settings.telegram_chat_id,
        "menu_button": button,
    }) is not None
    if done:
        logger.info("Кнопка слева: {}", "пульт" if url else "команды")
    return done


# --- Экраны самого Orbit ----------------------------------------------------

def attention_lines(store: Store) -> list[str]:
    """Что требует человека прямо сейчас. Пусто — значит всё идёт само.

    Пульт открывают между делами, и первый вопрос всегда один: «мне что-то
    надо сделать?». Раньше на него отвечали цифры состояния («в базе 115»),
    по которым это приходилось выводить самому. Здесь перечислено только то,
    где без человека дальше не поедет.
    """
    from .models import Kind, Stage

    urgent: list[str] = []

    ready_call = store.count(Kind.ORDER, Stage.CALL) + sum(
        store.count(kind, Stage.CALL) for kind in (Kind.PARTNER, Kind.CLIENT)
    )
    if ready_call:
        urgent.append(f"📞 <b>Готовы к созвону: {ready_call}</b> — это самое горячее")

    # Считаем по вкладкам и называем их. Одна цифра на три системы заставляла
    # владельца искать, где именно ответили: он видел «ждут вас: 1» и шёл в
    # почту, а единица была с биржи.
    where = [
        (title, store.count(kind, Stage.REPLIED))
        for kind, title in (
            (Kind.PARTNER, "партнёры"), (Kind.CLIENT, "клиенты"), (Kind.ORDER, "фриланс")
        )
    ]
    replied = sum(count for _title, count in where)
    if replied:
        parts = ", ".join(f"{title} {count}" for title, count in where if count)
        urgent.append(f"💬 Ответили и ждут вас: {replied} — {parts}")

    queued = sum(store.queue_size(kind) for kind in (Kind.PARTNER, Kind.CLIENT))
    if queued:
        urgent.append(f"✉️ Письма ждут отправки: {queued}")

    # Письма, оборвавшиеся на середине отправки. Сообщение о них уходит один
    # раз, при перезапуске отправщика, и если Телеграм в ту минуту не ответил,
    # факт пропадал совсем: из очереди письмо исчезло, а на экранах его нет.
    unclear = store.unclear_drafts()
    if unclear:
        urgent.append(
            f"❓ Письма с неизвестной судьбой: {unclear} — почта могла их и принять"
        )

    # Считаем сами контакты, а не строки сводки. Раньше здесь брался
    # `_reminders()` — а он возвращает готовый текст: пустую строку, заголовок
    # и по строке на контакт. Их и считали, вычитая единицу «на заголовок», и
    # число на главном экране всегда было на один больше правды: один
    # отложенный контакт показывался как два.
    from .store import today_msk

    due = store.due_reminders(today_msk())
    if due:
        urgent.append(f"⏰ Просили вернуться сегодня: {len(due)}")

    # Мёртвая модель — не «одно из», а причина, по которой ничего не поедет
    # дальше: без неё не пишутся письма, не разбираются ответы и не уходят
    # отклики. Ставим первой строкой, иначе владелец видит зелёный пульт и
    # думает, что просто тихий день.
    from .schedule import MODEL_STATE

    trouble = store.get_state(MODEL_STATE)
    if trouble:
        urgent.insert(0, f"🛑 <b>Модель не отвечает: {esc(trouble)}</b> — встало всё")

    # Молчащий ящик — то же самое для почты: ответы клиентов и возвраты не
    # разбираются вовсе, а выглядит это как спокойный день.
    from .schedule import INBOX_STATE

    mailbox = store.get_state(INBOX_STATE)
    if mailbox:
        urgent.insert(
            0, f"🛑 <b>Ящик не читается: {esc(mailbox)}</b> — ответы клиентов не разбираются"
        )

    # Слетевший вход на биржу без владельца не чинится: пока он лежит, туда не
    # уходят ни отклики, ни ответы клиентам.
    from .sections import freelance

    for platform in _lost_exchange_sessions():
        urgent.insert(
            0,
            f"🛑 <b>Вход на {esc(freelance.platform_title(platform))} слетел</b> — "
            f"нужна команда <code>fbid login {esc(platform)}</code>",
        )

    return urgent


def _lost_exchange_sessions() -> list[str]:
    """Площадки, вход на которые слетел. Спрашиваем у вкладки биржи.

    Тот же вопрос задаёт вечерняя сводка, и ответ обязан быть один и тот же.
    """
    from orbit.sections import freelance

    return freelance.lost_sessions_now()


def _webapp_url(store: Store) -> str | None:
    """Адрес мини-аппа, если он сейчас поднят. Падать здесь нельзя."""
    try:
        from .webapp.link import current

        return current(store)
    except Exception as exc:  # noqa: BLE001 — главный экран важнее кнопки
        logger.debug("Адрес мини-аппа не прочитался: {}", exc)
        return None


def app_screen(store: Store) -> Screen:
    """Экран «где пульт». Отдельная команда, потому что вопрос живой.

    Адрес туннеля меняется при каждом запуске, и владельцу надо понимать не
    «нажми кнопку», а что делать, когда кнопки нет.
    """
    url = _webapp_url(store)
    if url:
        return Screen(
            "<b>🖥 Пульт</b>\n\n"
            "Вся статистика, воронка, отклики и база — в одном окне.\n"
            "Открывается прямо здесь, выходить из Telegram не нужно.",
            [[("🖥 Открыть пульт", f"{WEBAPP_PREFIX}{url}")], [("⬅️ В главное меню", "home")]],
        )
    return Screen(
        "<b>🖥 Пульт не запущен</b>\n\n"
        "Он живёт на вашем маке и доступен, пока открыто окно терминала. "
        "Запустите одной строкой:\n\n"
        "<code>bash ~/PyCharmMiscProject/orbit/tools/miniapp.sh</code>\n\n"
        "Через полминуты вернитесь сюда и нажмите /app — появится кнопка.",
        [[("⬅️ В главное меню", "home")]],
    )


def main_menu(registry: Registry, store: Store) -> Screen:
    # Копия биржи живёт в базе Orbit, а обновлял её только вход во вкладку
    # «Фриланс». Все цифры ниже — и «готовы к созвону», и состояние вкладки —
    # считаются по этой копии, так что без обновления главный экран показывал
    # позавчерашнюю биржу. Внутри — не чаще раза в минуту и молча.
    with contextlib.suppress(Exception):
        from .sections import freelance

        freelance.refresh_quietly(store)

    lines = [GREETING, ""]
    try:
        urgent = attention_lines(store)
    except Exception as exc:  # noqa: BLE001 — сводка не должна ронять главный экран
        logger.warning("Блок «требует внимания» не собрался: {}", exc)
        urgent = []
    if urgent:
        lines.append("<b>Требует вас:</b>")
        lines += urgent
        lines.append("")
    for section, state in registry.health(store):
        lines.append(f"{state.mark} <b>{section.title}</b> — {esc(state.summary)}")
    if registry.broken:
        lines.append("")
        for name, reason in registry.broken.items():
            lines.append(f"🔴 <b>{esc(name)}</b> — не подключён: {esc(reason)}")
    if not registry.sections:
        lines.append("Ни одна система не подключилась. Проверьте пути в orbit/.env.")

    rows = [[section.button] for section in registry.sections]
    # Кнопка мини-аппа появляется, только когда туннель поднят. Показывать её
    # мёртвой нельзя: нажатие на протухший адрес открывает белый экран, и
    # владелец идёт искать поломку там, где её нет.
    app_url = _webapp_url(store)
    if app_url:
        rows.append([("🖥 Открыть пульт", f"{WEBAPP_PREFIX}{app_url}")])
    rows.append([("📈 Воронка", "funnel"), ("📊 Сводка", "status")])
    rows.append([("✅ Готовность", "ready")])
    rows.append([("❓ Как устроено", "help")])
    return Screen("\n".join(lines), rows)


def readiness_screen(store: Store) -> Screen:
    """Что настроено, а что ждёт человека. Экран перед запуском в дело.

    Открывают его в двух случаях: перед тем как показать систему кому-то, и
    после каждой правки в .env. Поэтому здесь не «состояние вообще», а ответ
    на один вопрос: поедет ли всё прямо сейчас, а если нет — что сделать.
    """
    from . import readiness

    try:
        groups = readiness.collect(store)
    except Exception as exc:  # noqa: BLE001 — экран не имеет права не открыться
        logger.warning("Готовность не собралась: {}", exc)
        return Screen(
            f"Проверку не удалось выполнить: {esc(str(exc))}",
            [[("⬅️ В главное меню", "home")]],
        )

    lines = ["<b>✅ Готовность к работе</b>", ""]
    for title, checks in groups:
        lines.append(f"<b>{esc(title)}</b>")
        for check in checks:
            note = f" — {esc(check.note)}" if check.note else ""
            lines.append(f"{check.mark} {esc(check.title)}{note}")
        lines.append("")

    waiting = readiness.waiting_for_you(groups)
    if waiting:
        lines.append(f"<b>Без вас не поедет: {len(waiting)}</b>")
        for check in waiting:
            lines.append(f"· {esc(check.title)}")
    else:
        lines.append("<b>Всё, что зависит от настроек, готово.</b>")
    lines.append("")
    lines.append(
        "<i>⚪️ — проверить не смогли. Это не «всё хорошо»: так помечено то, "
        "о чём мы честно не знаем.</i>"
    )
    return Screen(clip("\n".join(lines)), [[("⬅️ В главное меню", "home")]])


def funnel_screen(store: Store, days: int = 7) -> Screen:
    """Воронка по всем системам: где контакты застревают и во что это обходится."""
    from .digest import funnel_text

    other = 30 if days == 7 else 7
    return Screen(
        funnel_text(store, days=days),
        [
            [(f"За {other} дней", f"funnel:{other}")],
            [("🌙 Итоги дня", "digest")],
            [("⬅️ В главное меню", "home")],
        ],
    )


def digest_screen(store: Store) -> Screen:
    """Та же сводка, что приходит вечером, но по кнопке."""
    from .digest import build

    text = build(store) or "Сегодня ещё ничего не происходило."
    return Screen(text, [[("📈 Воронка", "funnel")], [("⬅️ В главное меню", "home")]])


def status_screen(registry: Registry, store: Store) -> Screen:
    lines = ["<b>📊 Сводка</b>", ""]
    for section, state in registry.health(store):
        lines.append(f"{state.mark} <b>{section.icon} {section.title}</b>")
        lines.append(esc(state.summary))
        for detail in state.details:
            lines.append(f"  {esc(detail)}")
        lines.append("")
    for name, reason in registry.broken.items():
        lines.append(f"🔴 <b>{esc(name)}</b> не подключён: {esc(reason)}")

    settings = get_settings()
    lines.append("<b>Почта:</b> " + ("настроена" if settings.smtp_ready else "не настроена"))
    lines.append("<b>Тексты:</b> " + _model_line(store))
    return Screen("\n".join(lines), [[("⬅️ В главное меню", "home")]])


def _model_line(store: Store) -> str:
    """Состояние модели словами. Наличие ключа состоянием не является.

    Раньше здесь стояло «модель на месте», если в настройках есть ключ. Но
    ключ бывает рабочим при пустом счёте, и тогда экран показывал зелёное,
    пока не писалось вообще ничего: ни писем, ни откликов, ни разбора
    переписок. Проверка расписания это уже знает — берём её вывод, а не
    факт наличия строки в настройках.
    """
    from .schedule import MODEL_STATE

    if not get_settings().anthropic_api_key:
        return "нет ключа"
    trouble = store.get_state(MODEL_STATE)
    return f"🛑 не отвечает — {esc(trouble)}" if trouble else "модель отвечает"


# --- Роутер -----------------------------------------------------------------

# Коды кнопок, которые ЯВЛЯЮТСЯ частью правки лимита на бирже. Всё остальное
# нажатие означает, что владелец правку бросил.
_LIMIT_DIALOG = ("f:fb:limit", "f:fb:limits")


def route(registry: Registry, store: Store, code: str) -> Screen:
    """«раздел:действие:аргумент» → экран."""
    # Нажали что-то не из диалога правки лимита — значит от неё ушли. Иначе
    # она ждёт числа ещё десять минут, и следующее сообщение из одних цифр
    # (телефон клиента, скопированный в чат) уходит в суточный бюджет
    # площадки. Команды это уже умели, кнопки — нет.
    if not code.startswith(_LIMIT_DIALOG):
        _cancel_limit_edit()

    if code in ("home", "start", ""):
        return main_menu(registry, store)
    if code == "status":
        return status_screen(registry, store)
    if code == "help":
        return Screen(HELP, [[("⬅️ В главное меню", "home")]])
    if code == "digest":
        return digest_screen(store)
    if code == "ready":
        return readiness_screen(store)
    if code == "app":
        return app_screen(store)
    if code == "funnel" or code.startswith("funnel:"):
        _, _, days = code.partition(":")
        return funnel_screen(store, int(days) if days.isdigit() else 7)

    section_code, _, rest = code.partition(":")
    section = registry.get(section_code)
    if section is None:
        # Кнопки из карточек биржи приходят с её собственными кодами
        # («dlgsend:12», «limit:profi_daily_budget»): их шлёт сборщик, а
        # обрабатывает этот бот. Раньше такое нажатие молча подменяло
        # сообщение главным меню — вместе с вопросом клиента и готовым
        # ответом, которые были в том же сообщении.
        bridged = _to_exchange(registry, code)
        if bridged is not None:
            return bridged
        logger.info("Незнакомая кнопка «{}»", code)
        return Screen(
            f"<b>Кнопка устарела</b>\n\nЭто сообщение из прошлой версии "
            f"(«{esc(code)}»), обработать нажатие уже нечем.",
            [[("⬅️ В главное меню", "home")]],
        )

    action, _, arg = rest.partition(":")
    try:
        return section.handle(store, action or "home", arg)
    except Exception as exc:  # noqa: BLE001 — упавший раздел не должен вешать бота
        logger.exception("Раздел «{}» упал на «{}»", section.title, code)
        return Screen(
            f"<b>{esc(section.title)}</b>\n\nЭкран не открылся: {esc(str(exc))}",
            [[("⬅️ В главное меню", "home")]],
        )


def _to_exchange(registry: Registry, code: str) -> Screen | None:
    """Отдаёт нажатие разделу «Фриланс», если код принадлежит бирже.

    Сборщик шлёт свои карточки сам, с собственными кодами кнопок. Раздел
    умеет их выполнять — надо только не потерять нажатие по дороге.
    """
    section = registry.get("f")
    if section is None:
        return None
    if section.knows is not None and not section.knows(code):
        # Код не наш и не биржи: пусть роутер покажет «кнопка устарела», а не
        # подменяет карточку с вопросом клиента чужим главным меню.
        return None
    try:
        return section.handle(None, "fb", code)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — чужой код не должен ронять бота
        logger.exception("Кнопка биржи «{}» не отработала", code)
        return Screen(
            f"<b>Не получилось</b>\n\nКнопка биржи не отработала: {esc(str(exc))}",
            [[("⬅️ В главное меню", "home")]],
        )


def _cancel_limit_edit() -> None:
    """Снимает взведённую правку лимита на бирже. Молча, если биржи нет."""
    try:
        from orbit.sections import freelance

        tgbot, _, _ = freelance._fbid()
        cancel = getattr(tgbot, "cancel_pending_limit", None)
        if cancel is None:
            return  # старая версия биржи — отменять нечем
        fbid_store = freelance._open_fbid_store()
    except Exception:  # noqa: BLE001 — биржа не подключена, значит и правки нет
        return
    try:
        dropped = cancel(fbid_store)
    finally:
        fbid_store.close()
    if dropped:
        logger.info("Правка лимита «{}» отменена: владелец прислал команду", dropped)


def _pending_limit_reply(text: str) -> Screen | None:
    """Владелец прислал число в ответ на «пришли новое значение».

    Диалог начинает биржа, а сообщение приходит сюда: у систем один бот.
    Без этой развязки новое значение лимита просто терялось, а бот в ответ
    показывал главное меню.
    """
    value = (text or "").strip()
    if not value:
        return None
    try:
        from orbit.sections import freelance

        tgbot, _, _ = freelance._fbid()
        fbid_store = freelance._open_fbid_store()
    except Exception:  # noqa: BLE001 — биржа не подключена, значит и диалога нет
        return None

    try:
        # Своего разбора числа здесь нет намеренно. Он был, и был строже
        # биржевого: тот понимает «1 500 ₽» — ровно в этом виде бот и
        # показывает текущее значение, — а здешний фильтр такой ответ
        # отбрасывал. Значение молча терялось, вместо лимита открывался поиск
        # по базе, а диалог оставался взведённым ещё десять минут: следующее
        # сообщение из одних цифр (телефон клиента, например) уходило в него.
        # Спрашиваем биржу, ждёт ли она значение, и отдаём текст как есть —
        # на мусор она ответит сама, кнопкой «Отмена».
        if tgbot._pending_limit(fbid_store) is None:
            return None
        result = tgbot.apply_pending_limit(fbid_store, value)
    finally:
        fbid_store.close()
    if result is None:
        return None

    answer, markup = result
    # Тем же переводом, что и остальные экраны биржи: свой был написан заново,
    # не знал про кнопку «домой» и приписывал вторую «назад» поверх неё.
    return freelance.screen_from_markup(answer, markup)


def _search_reply(store: Store, text: str) -> Screen | None:
    """Владелец кинул в чат почту, телефон, сайт или название.

    Самый частый случай: пришло письмо, непонятно, кто это и что мы ему
    писали. Копируешь адрес в чат — открывается карточка с историей.
    """
    query = (text or "").strip()
    if len(query) < 3 or query.startswith("/"):
        return None

    found = store.search_contacts(query)
    if not found:
        return Screen(
            f"<b>🔍 Не нашёл</b>\n\nПо запросу «{esc(query[:80])}» в базе никого нет.",
            [[("⬅️ В главное меню", "home")]],
        )

    if len(found) == 1:
        contact = found[0]
        screen = route(registry_for(store), store, f"{_home_of(contact)}:card:{contact.pk}")
        screen.alert = "Нашёл"
        return screen

    lines = [f"<b>🔍 Нашлось {len(found)}</b>", ""]
    rows = []
    for contact in found:
        lines.append(
            f"· <b>{esc(contact.name)}</b> — {WHERE_FROM[contact.kind]}, "
            f"{STAGE_TITLES[contact.stage].lower()}"
        )
        # Подпись кнопки — не html: экранирование превратило бы «Иванов и Ко»
        # с амперсандом в «Иванов &amp; Ко» прямо на кнопке.
        rows.append([(contact.name[:30], f"{_home_of(contact)}:card:{contact.pk}")])
    rows.append([("⬅️ В главное меню", "home")])
    return Screen(clip("\n".join(lines)), rows)


# Чей это контакт: вкладка, которой он принадлежит, и как назвать его в списке.
# Заказчик с биржи — не клиент холодной рассылки: разговор с ним ведёт
# freelance-bid, а карточка «Клиенты» предлагает написать ему письмо и закрыть
# кнопкой «Не писать». Раньше поиск отдавал его именно туда, потому что
# вкладка выбиралась вопросом «партнёр или нет».
SECTION_OF = {Kind.PARTNER: "p", Kind.CLIENT: "c", Kind.ORDER: "f"}
WHERE_FROM = {Kind.PARTNER: "партнёр", Kind.CLIENT: "клиент", Kind.ORDER: "заказ с биржи"}


def _home_of(contact) -> str:
    """Раздел, в котором живёт карточка этого контакта."""
    return SECTION_OF.get(contact.kind, "c")


# Реестр разделов собирается один раз за запуск: пересобирать его на каждое
# сообщение значит заново подключать все системы.
_REGISTRY: Registry | None = None


def registry_for(_store: Store) -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build()
    return _REGISTRY


def command_to_code(text: str, registry: Registry) -> str | None:
    """«/partners» → «p:home». Незнакомая команда → None."""
    word = text.strip().split()[0] if text.strip() else ""
    if not word.startswith("/"):
        return None
    name = word[1:].split("@")[0].lower()
    if name in {"start", "menu"}:
        return "home"
    if name in {"status", "help", "funnel", "digest", "ready", "app"}:
        return name
    for section in registry.sections:
        for command, _description, action in section.commands:
            if command == name:
                return action
    return None


# --- Цикл -------------------------------------------------------------------

def _allowed(chat_id: str) -> bool:
    allowed = get_settings().allowed_chats
    return bool(allowed) and str(chat_id) in allowed


def handle_update(update: dict, registry: Registry, store: Store) -> None:
    message = update.get("message") or {}
    callback = update.get("callback_query") or {}

    if callback:
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        if not _allowed(chat_id):
            return
        code = callback.get("data", "")
        # «noop» — подпись-счётчик в листалке, на неё просто гасим часики.
        if code == "noop":
            _call("answerCallbackQuery", {"callback_query_id": callback["id"]})
            return

        screen = route(registry, store, code)
        _call("answerCallbackQuery", {
            "callback_query_id": callback["id"],
            "text": (screen.alert or "")[:200],
        })
        message_id = callback.get("message", {}).get("message_id")
        # Новое сообщение — только если перерисовать не вышло по-настоящему.
        # «Ничего не изменилось» правкой не считается: экран уже нужный, а о
        # том, что нажатие дошло, владельцу говорит всплывашка выше.
        if not message_id or not edit(chat_id, message_id, screen):
            send(chat_id, screen)
        return

    if message:
        chat_id = str(message.get("chat", {}).get("id", ""))
        if not _allowed(chat_id):
            logger.info("Чужой чат {} — не отвечаю", chat_id)
            return
        text = message.get("text", "")
        code = command_to_code(text, registry)
        if code is not None:
            # Владелец сменил тему командой — значит начатую правку лимита он
            # бросил. Оставленная взведённой, она ждала числа ещё десять минут:
            # следующее сообщение из одних цифр, хоть телефон клиента из чата,
            # уходило в суточный бюджет площадки.
            _cancel_limit_edit()
        if code is None:
            # Не команда. Сначала смотрим, не ответ ли это на «пришли новое
            # значение лимита», потом — не ищет ли владелец контакт.
            pending = _pending_limit_reply(text)
            if pending is not None:
                send(chat_id, pending)
                return
            found = _search_reply(store, text)
            if found is not None:
                send(chat_id, found)
                return
        send(chat_id, route(registry, store, code or "home"))


def run(poll_timeout: int = POLL_TIMEOUT) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN не задан — запускать нечего")
        return

    with single_instance("orbit") as first:
        if not first:
            logger.error(
                "Orbit уже работает в другом процессе. Второй запуск отправил бы "
                "письма по второму разу: отправщик берёт одно и то же письмо из "
                "очереди, а отметка «отправлено» ставится только после доставки."
            )
            return
        _run(settings, poll_timeout)


@contextmanager
def single_instance(name: str):
    """Не даёт запустить второго бота на той же базе.

    Без этого два процесса делят одну очередь: оба берут первое одобренное
    письмо, оба его отправляют, получатель видит два одинаковых письма.
    """
    path = settings_pid_path(name)
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


def settings_pid_path(name: str):
    path = get_settings().db_path.parent / f"{name}.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _run(settings, poll_timeout: int) -> None:

    registry = build()
    if not registry.sections:
        logger.warning("Ни один раздел не подключился — бот покажет пустое меню")

    store = Store(settings.db_path)
    register_commands(registry)
    _start_sender(settings.db_path)
    _start_schedule(settings.db_path)
    logger.info("Orbit на связи. Разделов: {}", len(registry.sections))

    offset = _drop_stale_updates()
    try:
        while True:
            updates = _call(
                "getUpdates",
                {"offset": offset, "timeout": poll_timeout},
                timeout=poll_timeout + 10,
            )
            if updates is None:
                time.sleep(RETRY_PAUSE)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update, registry, store)
                except Exception:  # noqa: BLE001 — одно нажатие не роняет бота
                    logger.exception("Не смог обработать нажатие")
    except KeyboardInterrupt:
        logger.info("Orbit остановлен")
    finally:
        store.close()


def _start_sender(db_path) -> None:
    """Поднимает фоновую отправку писем рядом с ботом.

    Отдельным потоком, потому что между письмами стоит пауза в минуту-две, и
    ждать её в основном цикле значило бы, что бот на это время перестаёт
    отвечать на кнопки. Своя связь с базой у потока обязательна: sqlite не
    разрешает делить соединение между потоками.
    """
    import threading

    from .outreach import sender_loop

    threading.Thread(
        target=sender_loop, args=(db_path,), name="orbit-sender", daemon=True
    ).start()


def _start_schedule(db_path) -> None:
    """Поднимает фоновое расписание: бэкап, сводка, проверка почты.

    Тоже отдельным потоком и тоже со своей связью с базой — по той же
    причине, что и отправка: ждать в основном цикле значит не отвечать
    на кнопки.
    """
    import threading

    from .schedule import loop

    threading.Thread(
        target=loop, args=(db_path,), name="orbit-schedule", daemon=True
    ).start()


def _drop_stale_updates() -> int:
    """Пропускает всё, что накопилось, пока бот не работал.

    Иначе после перезапуска бот отвечает на нажатия часовой давности и
    вываливает пачку экранов, которых никто уже не ждёт. Хуже того, среди них
    есть необратимые: «Очистить очередь», «Отправить все», «Не писать».

    Спрашиваем, пока Telegram не ответит, — без ограничения по числу попыток.
    Ноль означает «отдай всё, что накопилось», то есть ровно ту беду, от
    которой функция защищает. Заведомо большое смещение не спасало тоже, и
    было хуже: Telegram отдаёт обновления с update_id НЕ МЕНЬШЕ смещения, а
    настоящие номера много меньше двух миллиардов. Бот с таким смещением не
    получал бы вообще ничего и молча стоял бы до перезапуска — а заметить это
    было бы некому: сказать о беде он может только тем же Телеграмом.

    Ждать не жалко: без Telegram у бота всё равно нет работы, а отправка
    писем и расписание идут своими потоками и уже запущены.

    offset=-1 просит только ПОСЛЕДНЕЕ обновление: по нему считается граница,
    и тащить всю очередь ради этого не нужно.
    """
    attempt = 0
    while True:
        attempt += 1
        result, error = _request("getUpdates", {"offset": -1, "timeout": 0})
        if result is not None:
            if not result:
                return 0
            last = result[-1]["update_id"] + 1
            logger.info("Начинаю с обновления {} — старые нажатия пропущены", last)
            return last
        # Первые попытки пишем каждую, дальше — примерно раз в минуту: сеть
        # может лежать часами, и строка на каждые пять секунд забьёт лог.
        every_minute = max(1, int(60 / RETRY_PAUSE))
        if attempt <= STALE_SKIP_ATTEMPTS or attempt % every_minute == 0:
            logger.warning(
                "Telegram не отвечает про старые нажатия (попытка {}): {} — жду",
                attempt, error,
            )
        time.sleep(RETRY_PAUSE)

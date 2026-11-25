"""Интерактивный Telegram-бот: собирает лиды по кнопкам и стримит проблемные.

Без тяжёлых зависимостей — long-polling через getUpdates (как и telegram.py).
Логика диалога кнопками:

    /start → выбрать нишу → выбрать город → выбрать сколько парсить
           → бот ищет в 2ГИС, добирает браузером телефоны+отзывы
           → шлёт КАРТОЧКОЙ только те, у кого в отзывах есть жалобы на связь,
             по мере находки, и в конце — сводку.

Команды: /start /niches /help /cancel. Один прогон на чат за раз (флаг busy).
"""
from __future__ import annotations

import asyncio
import html
import json
import math
import os
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger

from . import niches as niches_mod
from . import telegram as tg
from .config import Settings, get_settings
from .dedup import known_keys
from .leadbase import LeadBase
from .models import Organization
from .pipeline import resolve_source
from .pipeline import run as pipeline_run
from .providers.browser import iter_enrich_with_browser

# Сколько организаций пробивать браузером — варианты кнопкой «сколько парсить».
COUNTS = [10, 20, 50]

# Меню команд слева от поля ввода (кнопка «Menu» / «/»). Ставится через setMyCommands.
BOT_COMMANDS = [
    {"command": "start", "description": "🏠 Меню (поиск / база / сбор)"},
    {"command": "base", "description": "📋 Посмотреть базу лидов"},
    {"command": "stop", "description": "⏸ Остановить фоновый сбор"},
    {"command": "niches", "description": "📂 Список ниш"},
    {"command": "reset", "description": "🔄 Сбросить журнал (искать заново)"},
    {"command": "clearbase", "description": "🗑 Очистить базу начисто"},
    {"command": "cancel", "description": "⏹ Отменить текущий поиск"},
    {"command": "help", "description": "ℹ️ Справка"},
]

_HELP = (
    "<b>Лидоген-бот</b> — ищет организации, которым в отзывах жалуются на связь "
    "(«не дозвониться», «не перезвонили») — тёплые лиды под бота-автоответчик.\n\n"
    "<b>Команды:</b>\n"
    "/start — начать подбор (ниша → город → объём)\n"
    "/niches — список ниш\n"
    "/reset — сбросить журнал проверенных (искать заново, база сохраняется)\n"
    "/clearbase — очистить базу лидов начисто (сбор с нуля)\n"
    "/cancel — отменить текущий поиск\n"
    "/help — эта справка\n\n"
    "Просто нажимай кнопки — бот сам всё соберёт и пришлёт карточки."
)


@dataclass
class ChatState:
    """Незавершённый выбор пользователя в чате."""

    niche: str | None = None
    city: str | None = None
    busy: bool = False
    cancel: bool = False


# Сколько запросов подряд должны упасть, чтобы счесть фоновый сбор нерабочим и остановить.
# Одиночные сбои у мобильного прокси — норма, серия из 10 означает поломку окружения.
_COLLECT_MAX_ERRORS = 10

# Сколько ждать, пока фоновый сбор действительно встанет, прежде чем чистить базу.
# Он узнаёт о просьбе остановиться, лишь доработав текущую карточку: браузер через
# прокси, две загрузки и скролл. Тридцати секунд хватает на карточку; не хватило —
# честно отказываемся чистить, а не чистим под работающим сбором.
_COLLECT_STOP_WAIT = 30.0

# Для оценки времени браузерного поиска: столько организаций на одной странице выдачи 2ГИС
# и столько страниц провайдер листает максимум (browser._SEARCH_MAX_PAGES).
_SEARCH_CARDS_PER_PAGE = 12
_SEARCH_MAX_PAGES_HINT = 6


def _org_key(org: Organization) -> str:
    """Стабильный ключ организации для дедупа между прогонами (source_url надёжнее всего)."""
    return org.source_url or f"{org.name}|{org.address or ''}"


class SeenStore:
    """Персистентный набор ключей уже ПРОВЕРЕННЫХ (браузером) организаций.

    Чтобы повторный прогон не выдавал те же лиды: раз организацию уже смотрели —
    второй раз не берём. Хранится JSON-файлом рядом с output; переживает рестарт бота.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: set[str] = set()
        if path.exists():
            try:
                self.keys = set(json.loads(path.read_text("utf-8")))
            except Exception as exc:  # noqa: BLE001 — битый кэш не должен ронять бота
                logger.warning("SeenStore: не прочитал {} ({}) — начинаю с чистого.", path, exc)

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def add(self, key: str) -> None:
        if key not in self.keys:
            self.keys.add(key)
            self._save()

    def _save(self) -> None:
        # Атомарно: пишем во временный файл рядом и os.replace — иначе краш/OOM посреди
        # записи оставит битый JSON, и при старте весь журнал молча обнулится.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self.keys), ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)  # атомарная замена на том же ФС
        except Exception as exc:  # noqa: BLE001 — кэш не сохранился — потеря переживаемая
            logger.warning("SeenStore: не сохранил {} ({}).", self.path, exc)


class Bot:
    """Один экземпляр на процесс. Держит состояние диалогов и крутит long-polling."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.token = self.settings.telegram_bot_token
        if not self.token:
            raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в .env.")
        self.states: dict[int, ChatState] = {}
        self._offset: int | None = None
        self._tasks: set[asyncio.Task] = set()  # держим ссылки, иначе GC съест задачу на лету
        self._allowed = self.settings.allowed_chats()  # кто может управлять ботом
        self._denied_warned: set[int] = set()  # чтобы не спамить лог по чужим чатам
        self._open_access_warned = False  # предупреждение о пустом allowlist — один раз
        self._seen = SeenStore(self.settings.output_dir / "seen_leads.json")  # уже проверенные орги
        self._base = LeadBase(self.settings.output_dir / "leads_base.json")  # накопленные горячие лиды
        self._collecting = False  # идёт ли фоновый сбор
        self._collect_task: asyncio.Task | None = None
        self._collect_checked = 0  # сколько мест проверено в текущем сборе
        if not self._allowed:
            # Формулировка была ровно обратной правде: «бот отвечает ВСЕМ».
            # Поведение сменили — пустой список теперь никого не пускает, — а
            # эта строка осталась. Владелец, прочитав её при старте, шёл искать
            # дыру в доступе, которой нет, и не понимал, почему бот молчит.
            logger.error(
                "ALLOWED_CHAT_IDS/TELEGRAM_CHAT_ID не заданы — бот НИКОГО не пускает "
                "и будет молчать на любое сообщение. Узнать свой id: parser tg-chat-id"
            )

    def _state(self, chat_id: int) -> ChatState:
        return self.states.setdefault(chat_id, ChatState())

    # --- отправка ------------------------------------------------------------
    async def _send(self, client, chat_id: int, text: str, keyboard: dict | None = None) -> None:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            await tg.post(client, self.token, "sendMessage", payload)
        except Exception as exc:  # noqa: BLE001 — сбой отправки не роняет цикл бота
            logger.warning("Bot send: {}", exc)

    async def _edit(
        self, client, chat_id: int, message_id: int, text: str, keyboard: dict | None = None,
    ) -> None:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            await tg.post(client, self.token, "editMessageText", payload)
        except Exception:  # noqa: BLE001 — не отредактировалось — шлём новым сообщением
            await self._send(client, chat_id, text, keyboard)

    async def _answer_cb(self, client, callback_id: str, text: str = "") -> None:
        with suppress(Exception):  # подтверждение нажатия необязательно
            await tg.post(
                client, self.token, "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text},
            )

    # --- клавиатуры ----------------------------------------------------------
    def _niche_keyboard(self) -> dict:
        rows: list[list[tuple[str, str]]] = [[("🔍 Все ниши", "n:all")]]
        niches = niches_mod.all_niches()
        for i in range(0, len(niches), 2):  # по 2 в ряд
            row = [(n.title, f"n:{n.code}") for n in niches[i : i + 2]]
            rows.append(row)
        return tg.keyboard(rows)

    def _city_keyboard(self) -> dict:
        rows: list[list[tuple[str, str]]] = [[("🌍 Все города", "c:all")]]
        items = list(niches_mod.CITIES.items())
        for i in range(0, len(items), 2):
            row = [(name, f"c:{code}") for code, name in items[i : i + 2]]
            rows.append(row)
        rows.append([("« Назад к нишам", "back:niche")])
        return tg.keyboard(rows)

    def _count_keyboard(self) -> dict:
        row = [(f"{c} организаций", f"cnt:{c}") for c in COUNTS]
        return tg.keyboard([row, [("« Назад к городам", "back:city")]])

    @staticmethod
    def _niche_title(code: str | None) -> str:
        if code == "all":
            return "🔍 Все ниши"
        n = niches_mod.get(code) if code else None
        return n.title if n else (code or "—")

    @staticmethod
    def _city_title(city: str | None) -> str:
        return "🌍 Все города" if city == "all" else (city or "—")

    @staticmethod
    def _build_pairs(niche_code: str | None, city: str | None) -> list[tuple[str, str]]:
        """Раскрываем выбор в пары (рубрика, город) с учётом «все ниши» / «все города».

        «Все ниши» — по одной ГЛАВНОЙ рубрике каждой ниши (иначе запросов слишком много).
        «Все города» — по всем городам из справочника.
        """
        if niche_code == "all":
            rubrics = [n.rubrics[0] for n in niches_mod.all_niches() if n.rubrics]
        else:
            n = niches_mod.get(niche_code) if niche_code else None
            rubrics = list(n.rubrics) if n else []
        cities = list(niches_mod.CITIES.values()) if city == "all" else ([city] if city else [])
        return [(rubric, town) for town in cities for rubric in rubrics]

    # --- обработчики ---------------------------------------------------------
    async def _handle_command(self, client, chat_id: int, text: str) -> None:
        cmd = text.split()[0].split("@")[0].lower()
        state = self._state(chat_id)
        if cmd in {"/start", "/menu"}:
            state.niche = state.city = None
            await self._send(
                client, chat_id,
                "👋 <b>Лидоген-бот.</b> Что делаем?\n\n"
                "🔎 <b>Подобрать сейчас</b> — разовый поиск по нише (~5 мин).\n"
                "📋 <b>Посмотреть базу</b> — накопленные лиды, мгновенно.\n"
                "▶️ <b>Собрать базу в фоне</b> — парсит сам, копит лиды, пока ты занят.",
                self._main_keyboard(),
            )
        elif cmd in {"/база", "/base"}:
            await self._show_base(client, chat_id, 0)
        elif cmd in {"/stop", "/стоп"}:
            if self._collecting:
                self._stop_collect()
                await self._send(client, chat_id, "⏸ Останавливаю фоновый сбор…")
            else:
                await self._send(client, chat_id, "Фоновый сбор сейчас не идёт.", self._main_keyboard())
        elif cmd == "/niches":
            lines = ["<b>Ниши:</b>"]
            for n in niches_mod.all_niches():
                lines.append(f"{n.title} — <i>{', '.join(n.rubrics[:3])}…</i>")
            await self._send(client, chat_id, "\n".join(lines), self._niche_keyboard())
        elif cmd == "/cancel":
            state.cancel = True
            await self._send(client, chat_id, "⏹ Отменяю текущий поиск (если он идёт).")
        elif cmd == "/reset":
            n = len(self._seen.keys)
            self._seen.keys.clear()
            self._seen._save()
            await self._send(
                client, chat_id,
                f"🔄 Журнал проверенных очищен ({n} организаций). Теперь поиск пройдёт по ним заново.\n"
                "<i>Накопленная база лидов при этом сохранена. Чтобы очистить и её — /clearbase.</i>",
                self._niche_keyboard(),
            )
        elif cmd in {"/clearbase", "/clear_base"}:
            # Полный сброс: очищаем и базу лидов, и журнал проверенных — сбор с чистого листа.
            #
            # Сначала дожидаемся, пока сбор ДЕЙСТВИТЕЛЬНО встанет. `_stop_collect`
            # только просит остановиться, а задача узнаёт об этом, когда доработает
            # текущую карточку: до тридцати секунд браузера через прокси. Всё это
            # время она продолжает класть лиды — и они ложились уже после очистки,
            # прямо под сообщением «База очищена начисто». Владелец видел в свежей
            # базе чужие строки и не понимал, откуда.
            task = self._collect_task
            if self._collecting or (task is not None and not task.done()):
                self._stop_collect()
                await self._send(
                    client, chat_id,
                    "⏹ Сначала останавливаю сбор — он ещё дописывает текущую карточку…",
                )
                if task is not None:
                    # shield: по таймауту wait_for отменяет задачу, а обрывать
                    # сбор посреди записи на диск — верный способ потерять лид.
                    with suppress(Exception):
                        await asyncio.wait_for(
                            asyncio.shield(task), timeout=_COLLECT_STOP_WAIT
                        )
                if self._collecting or (task is not None and not task.done()):
                    await self._send(
                        client, chat_id,
                        "⚠️ <b>Сбор всё ещё доигрывает.</b> База не тронута: очистить сейчас "
                        "значит получить в чистой базе его последние лиды.\n"
                        "Повтори /clearbase через полминуты.",
                        self._main_keyboard(),
                    )
                    return
            try:
                nb = self._base.clear()
            except OSError as exc:
                # Файл не удалился — значит база вернётся при следующем старте.
                # Рапортовать «очищено начисто» здесь нельзя: владелец начнёт
                # собирать заново поверх старого и не поймёт, откуда дубли.
                await self._send(
                    client, chat_id,
                    "⚠️ <b>Базу очистить не вышло.</b> Файл на диске остался: "
                    f"{html.escape(str(exc))}\n"
                    "После перезапуска лиды вернутся. Ничего не трогал.",
                    self._main_keyboard(),
                )
                return
            ns = len(self._seen.keys)
            self._seen.keys.clear()
            self._seen._save()
            await self._send(
                client, chat_id,
                f"🗑 <b>База очищена начисто.</b> Удалено лидов: {nb}, записей журнала: {ns}.\n"
                "Фоновый сбор (если шёл) остановлен. Теперь сбор пойдёт с нуля.",
                self._main_keyboard(),
            )
        else:  # /help и всё непонятное
            await self._send(client, chat_id, _HELP, self._niche_keyboard())

    async def _handle_callback(self, client, cb: dict) -> None:
        data = cb.get("data") or ""
        message = cb.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("id") or message.get("message_id")
        callback_id = cb["id"]
        if chat_id is None:
            await self._answer_cb(client, callback_id)
            return
        state = self._state(chat_id)

        if data.startswith("n:"):
            state.niche = data[2:]
            title = self._niche_title(state.niche)
            await self._answer_cb(client, callback_id, title)
            await self._edit(
                client, chat_id, message_id,
                f"Ниша: <b>{title}</b>\nТеперь город:", self._city_keyboard(),
            )
        elif data.startswith("c:"):
            code = data[2:]
            state.city = "all" if code == "all" else niches_mod.CITIES.get(code)
            title = self._city_title(state.city)
            await self._answer_cb(client, callback_id, title)
            await self._edit(
                client, chat_id, message_id,
                f"Город: <b>{title}</b>\nСколько организаций пробить (чем больше — тем дольше)?",
                self._count_keyboard(),
            )
        elif data == "back:niche":
            state.niche = None
            await self._answer_cb(client, callback_id)
            await self._edit(client, chat_id, message_id, "Выбери нишу:", self._niche_keyboard())
        elif data == "back:city":
            await self._answer_cb(client, callback_id)
            await self._edit(client, chat_id, message_id, "Выбери город:", self._city_keyboard())
        elif data.startswith("cnt:"):
            await self._answer_cb(client, callback_id, "Запускаю поиск…")
            count = int(data[4:])
            await self._run_search(client, chat_id, message_id, count)
        elif data.startswith("info:"):
            await self._answer_cb(client, callback_id, "Смотрю реестр…")
            await self._send_company_card(client, chat_id, data[5:])
        elif data == "search":  # «Подобрать сейчас» → выбор ниши
            state.niche = state.city = None
            await self._answer_cb(client, callback_id)
            await self._edit(client, chat_id, message_id, "Выбери нишу:", self._niche_keyboard())
        elif data.startswith("base:"):  # показать базу с оффсета
            await self._answer_cb(client, callback_id)
            await self._show_base(client, chat_id, int(data[5:]))
        elif data == "collect:start":
            await self._answer_cb(client, callback_id, "Запускаю фоновый сбор…")
            if not self._start_collect(client, chat_id):
                await self._send(
                    client, chat_id,
                    "⏳ Предыдущий сбор ещё доигрывает текущую пачку (это до минуты). "
                    "Подожди и нажми «▶️ Собрать базу» ещё раз.",
                )
        elif data == "collect:stop":
            # Кнопка живёт в сообщении, а сообщение никуда не девается: нажать
            # её можно и через сутки после того, как сбор кончился сам. Тогда
            # «Останавливаю сбор…» — враньё, и владелец ждёт остановки того,
            # чего нет. Отвечаем по факту.
            if self._collecting:
                await self._answer_cb(client, callback_id, "Останавливаю сбор…")
                self._stop_collect()
            else:
                await self._answer_cb(client, callback_id, "Сбор и так не идёт")
        elif data == "basexlsx":
            await self._answer_cb(client, callback_id, "Готовлю Excel…")
            await self._send_export(client, chat_id, self._base.all_sorted())
        else:
            await self._answer_cb(client, callback_id)

    async def _enrich_inn(self, org: Organization, city: str | None = None) -> None:
        """Дотянуть ИНН/руководителя/год через DaData (в потоке — enrich синхронный).

        city — город из поиска (точнее матчинг). Тихо пропускаем, если ключей нет или
        DaData не ответил: карточка уйдёт без ИНН, но лид не потеряется.
        """
        if not (self.settings.dadata_api_key and self.settings.dadata_secret):
            return
        try:
            from . import enrich as enrich_mod

            await asyncio.to_thread(enrich_mod.enrich, [org], self.settings, None, city)
        except Exception as exc:  # noqa: BLE001 — ИНН не дотянулся — карточка уйдёт без него
            logger.warning("Бот: ИНН не дотянут для {}: {}", org.name, exc)

    async def _send_company_card(self, client, chat_id: int, inn: str) -> None:
        """По ИНН дотянуть карточку юрлица/ИП из DaData и прислать (кнопка «Подробнее по ИНН»)."""
        if not (self.settings.dadata_api_key and self.settings.dadata_secret):
            await self._send(client, chat_id, "DaData не настроен — карточка по ИНН недоступна.")
            return
        try:
            from . import enrich as enrich_mod

            data = await asyncio.to_thread(enrich_mod.fetch_by_inn, inn, self.settings)
        except Exception as exc:  # noqa: BLE001 — карточка по ИНН не обязательна
            logger.warning("Бот: карточка по ИНН {} не получена: {}", inn, exc)
            await self._send(client, chat_id, f"Не удалось получить карточку по ИНН {inn}.")
            return
        if not data:
            await self._send(client, chat_id, f"По ИНН {inn} в реестре ничего не нашлось.")
            return
        await self._send(client, chat_id, tg.format_company_card(data))

    @staticmethod
    def _is_new_lead(org: Organization, known: set[str]) -> bool:
        """Первый ли это раз, когда лид попадается в прогоне. Запоминает признаки.

        Узнаём теми же признаками, что и пакетный дедуп (ссылка на карточку,
        любой из телефонов, имя с адресом), плюс ИНН. Телефон появляется
        только после браузерного добора, поэтому дедуп, сделанный до него,
        филиалы сети развести не мог: владельцу уходили два лида с одним
        номером, и он звонил в одну компанию дважды. В командной строке этот
        второй дедуп есть (см. cli.py), но бот отдаёт лид сразу, как нашёл, —
        поэтому проверяет по одному, а не пачкой.
        """
        keys = set(known_keys(org))
        if org.inn:
            keys.add(f"inn:{org.inn}")
        if keys & known:
            return False
        known |= keys
        return True

    @staticmethod
    def _eta_minutes(n_pairs: int, n_cards: int, source: str = "2gis") -> int:
        """Грубая оценка времени прогона, мин: сбор списка + браузерный добор карточек.

        source меняет цену одного запроса на порядок: API отвечает за секунды, а браузерный
        поиск открывает SPA 2ГИС через прокси — 40–50с на СТРАНИЦУ выдачи (12 организаций,
        замер на живом прогоне). Поэтому цена пары растёт с запрошенным объёмом.
        """
        if source == "browser":
            pages = max(1, math.ceil(n_cards / _SEARCH_CARDS_PER_PAGE)) if n_cards else 1
            per_pair = 45.0 * min(pages, _SEARCH_MAX_PAGES_HINT)
        else:
            per_pair = 1.6
        # ~30с на карточку в браузере через прокси (2 загрузки + скролл); +запуск.
        seconds = n_pairs * per_pair + n_cards * 30 + 10
        return max(1, math.ceil(seconds / 60))

    # --- сам поиск -----------------------------------------------------------
    async def _run_search(self, client, chat_id: int, message_id: int, count: int) -> None:
        state = self._state(chat_id)
        # Фиксируем выбор в локальные переменные: пока идёт долгий поиск, пользователь может
        # нажать другую нишу/город в меню и перезаписать state — прогон должен идти по своему.
        niche, city = state.niche, state.city
        pairs = self._build_pairs(niche, city)
        if not pairs:
            logger.info("Поиск не запущен: пустой выбор (chat={}, niche={}, city={}). "
                        "Вероятно, состояние сброшено рестартом бота.", chat_id, niche, city)
            await self._send(client, chat_id, "Выбор сбросился (бот перезапускался). Начни заново: /start")
            return
        if state.busy:
            await self._send(client, chat_id, "⏳ Уже идёт поиск. Дождись окончания или /cancel.")
            return

        logger.info("Старт поиска: chat={} niche={!r} city={!r} count={} pairs={}",
                    chat_id, niche, city, count, len(pairs))

        state.busy = True
        state.cancel = False
        try:
            title = f"{self._niche_title(niche)} · {self._city_title(city)}"
            active = resolve_source(self.settings.search_source, self.settings)
            how = "браузером (без API-ключа, медленнее)" if active == "browser" else "через API 2ГИС"
            # До первого ответа API мы не знаем, жив ли ключ: после рестарта бота флаг
            # блокировки пуст. Честно предупреждаем, что при мёртвом ключе будет дольше.
            hedge = ("\n(если лимит ключа исчерпан — сам перейду на браузерный поиск, "
                     "это заметно дольше)"
                     if active == "2gis" and self.settings.search_source == "auto" else "")
            # У каждой пары просим ДОЛЮ от нужного объёма, а не полный объём.
            # Здесь стояло `max_results=count`, и на шести парах прогон забирал
            # у 2ГИС до шестисот карточек, из которых мы оставляли сотню:
            # пятьсот запросов демо-ключа (тысяча в месяц) сгорали впустую.
            #
            # Полуторный запас — на дедуп и на уже проверенные ранее компании;
            # десять — нижняя граница, иначе на десятке пар каждая отдавала бы
            # по паре строк и выборка получалась бы нищей.
            per_pair = max(10, math.ceil(count / len(pairs) * 1.5))
            # Считаем срок по тому объёму, который реально запросим у пары.
            # Раньше сюда уходил полный `count`, хотя каждая пара берёт долю:
            # при браузерном поиске цена пары растёт с объёмом страницами, и
            # на шести парах обещание раздувалось вчетверо. Владелец видел
            # «~90 мин» там, где проход занимал двадцать, и уходил.
            await self._edit(
                client, chat_id, message_id,
                f"🔎 Ищу: <b>{title}</b>\nОбъём: {count}. Собираю базу {how} ({len(pairs)} запросов)…\n"
                f"⏱ Примерно ~{self._eta_minutes(len(pairs), per_pair, active)} мин.{hedge}",
            )
            orgs = await pipeline_run(
                pairs,
                source=self.settings.search_source,
                max_results=per_pair,
                settings=self.settings,
                state_path=None,
                resume=False,
                total_limit=count * 2,
            )
            if not orgs:
                await self._send(client, chat_id, "😕 Ничего не нашлось. Попробуй другую нишу/город: /start")
                return

            # Убираем уже проверенные ранее орги — их не показываем и не тратим на них браузер.
            before = len(orgs)
            orgs = [o for o in orgs if _org_key(o) not in self._seen]
            skipped = before - len(orgs)
            if not orgs:
                await self._send(
                    client, chat_id,
                    f"Все найденные ({before}) я уже проверял раньше — новых нет. "
                    "Попробуй другую нишу/город: /start",
                )
                return

            # При «все ниши/города» pipeline отдаёт по порядку запросов — перемешаем,
            # чтобы срез count не был целиком из первого города/ниши.
            if niche == "all" or city == "all":
                random.shuffle(orgs)
            orgs = orgs[:count]
            skip_note = f" (пропустил {skipped} уже проверенных)" if skipped else ""
            await self._send(
                client, chat_id,
                f"📇 Нашёл {len(orgs)} новых организаций{skip_note}. "
                f"Проверяю отзывы браузером… ⏱ ~{self._eta_minutes(0, len(orgs))} мин.",
            )

            search_city = city if (city and city != "all") else None  # город из поиска для матчинга ИНН
            hot_leads: list[Organization] = []
            hot_keys: set[str] = set()  # признаки уже отданных лидов — дедуп в рамках прогона
            checked = 0
            failed = 0
            with_phone = 0
            step = max(5, len(orgs) // 4)  # частота сообщений о прогрессе
            async for org, ok in iter_enrich_with_browser(orgs, self.settings):
                if state.cancel:
                    await self._send(client, chat_id, "⏹ Поиск отменён.")
                    break
                if not ok:
                    # добор упал (таймаут прокси и т.п.) — НЕ помечаем seen, перепроверим потом.
                    failed += 1
                    continue
                checked += 1
                self._seen.add(_org_key(org))  # проверена успешно — второй раз не покажем
                if org.phone:
                    with_phone += 1
                if checked % step == 0:  # прогресс, чтобы не выглядело «завис» (#4)
                    await self._send(
                        client, chat_id,
                        f"⏳ Проверено {checked}/{len(orgs)}, горячих: {len(hot_leads)}…",
                    )
                if org.review_complaints:
                    await self._enrich_inn(org, search_city)  # ИНН/руководитель + год (для оценки)
                    if not self._is_new_lead(org, hot_keys):
                        continue  # то же юрлицо или тот же телефон — не дублируем
                    hot_leads.append(org)
                    # Лид уходит и в базу, а не только в чат. Раньше разовый
                    # поиск не клал их никуда: карточка в переписке и строка в
                    # выгрузке — всё. При этом организация помечалась
                    # проверенной и второй раз не показывалась уже никогда,
                    # так что потерять сообщение значило потерять лид.
                    self._base.add(org)
                    kb = None
                    if org.inn:  # кнопка есть только когда по ИНН реально есть что показать
                        kb = tg.keyboard([[("🔎 Подробнее по ИНН", f"info:{org.inn}")]])
                    await self._send(client, chat_id, tg.format_org(org), kb)
                    await asyncio.sleep(self.settings.request_delay)

            if not state.cancel:
                await self._finish_search(client, chat_id, hot_leads, checked, failed, with_phone)
        except Exception as exc:  # noqa: BLE001 — последний рубеж поиска: сбой уходит человеку
            logger.exception("Поиск упал: {}", exc)
            # Текст исключения уходит человеку в чат, а прокси пишет туда свой
            # URL вместе с логином и паролем. Чистим перед отправкой.
            await self._send(client, chat_id, f"⚠️ Ошибка при поиске: {tg.safe_text(exc)}")
        finally:
            state.busy = False

    async def _finish_search(self, client, chat_id, hot_leads, checked, failed, with_phone) -> None:
        """Итог прогона: топ по платёжеспособности/приоритету + предупреждения + экспорт."""
        from .score import assess_solvency, lead_score

        fail_note = f" ({failed} не удалось загрузить — перепроверю в след. раз)" if failed else ""
        # Похоже на бан прокси: карточки грузились, но телефонов нет ни у кого.
        block_note = ""
        if checked >= 5 and with_phone == 0:
            block_note = ("\n⚠️ Ни у одной карточки не раскрылся телефон — возможно, 2ГИС "
                          "блокирует прокси. Смени IP у прокси или проверь /proxy-check.")

        if not hot_leads:
            await self._send(
                client, chat_id,
                f"Проверил {checked}, но жалоб на связь в отзывах не нашёл{fail_note}.{block_note}\n"
                "Попробуй другую нишу или город: /start",
            )
            return

        ranked = sorted(hot_leads, key=lead_score, reverse=True)
        lines = [
            f"✅ Готово. Горячих лидов: <b>{len(hot_leads)}</b> из {checked} проверенных{fail_note}.",
            "",
            "🏆 <b>Топ по платёжеспособности:</b>",
        ]
        for i, o in enumerate(ranked[:5], 1):
            sol = assess_solvency(o)
            phone = o.phone[0] if o.phone else "тел. не найден"
            lines.append(f"{i}. {html.escape(o.name)} {sol['emoji']} — {html.escape(phone)}")
        if block_note:
            lines.append(block_note)
        lines.append("📥 Выгружаю все лиды в Excel…")
        await self._send(client, chat_id, "\n".join(lines))
        # Лиды разового поиска теперь ложатся в базу — значит и о несостоявшейся
        # записи говорить надо здесь же. Иначе о ней узнает следующий фоновый
        # сбор и скажет уже про чужие лиды, а к тому времени их и не вспомнить.
        unsaved = self._base.take_unsaved()
        if unsaved:
            names = ", ".join(html.escape(name) for name in unsaved[:5])
            more = f" и ещё {len(unsaved) - 5}" if len(unsaved) > 5 else ""
            await self._send(
                client, chat_id,
                f"⚠️ <b>Не записалось на диск: {len(unsaved)}</b>\n{names}{more}\n"
                "В базе они только до перезапуска — сохрани выгрузку сейчас.",
            )
        await self._send_export(client, chat_id, ranked)

    async def _send_export(self, client, chat_id, orgs) -> None:
        """Собираем горячие лиды в XLSX (отсортированные по приоритету) и шлём файлом."""
        try:
            from . import export

            # Имя файла уникально на каждую выгрузку. Один путь на чат
            # выдерживал ровно одну выгрузку за раз: вторая переписывала файл,
            # пока первую ещё читал отправщик, и владелец получал таблицу не
            # с теми строками, которые заказывал. Собрать выгрузку недолго,
            # а нажать «Выгрузить» дважды подряд — секунда.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.settings.output_dir / f"leads_{chat_id}_{stamp}.xlsx"
            await asyncio.to_thread(export.to_xlsx, orgs, path)
            await tg.send_document(client, self.token, chat_id, path,
                                   caption=f"📊 {len(orgs)} лидов (по убыванию приоритета)")
        except Exception as exc:  # noqa: BLE001 — выгрузка не обязательна, лиды уже в карточках
            logger.warning("Экспорт не отправлен: {}", exc)
            await self._send(client, chat_id, "📥 Excel-выгрузку сделать не удалось (лиды выше в карточках).")

    # --- фоновый сбор базы ---------------------------------------------------
    @staticmethod
    def _collect_combos() -> list[tuple[str, str]]:
        """Все (рубрика, город) по всем нишам и городам — коллектор идёт по ним по кругу."""
        rubrics = [r for n in niches_mod.all_niches() for r in n.rubrics]
        cities = list(niches_mod.CITIES.values())
        return [(r, c) for c in cities for r in rubrics]

    def _start_collect(self, client, chat_id: int) -> bool:
        """Запустить фоновый сбор. False — если предыдущий ещё доигрывает.

        Флага _collecting мало: /stop только просит остановиться, а задача узнаёт об этом
        лишь когда доработает текущую пачку (до 25с сна + сетевой запрос + очередь за
        Chromium). Всё это время меню уже показывает «▶️ Собрать базу», и второй запуск
        поднимал ВТОРОЙ коллектор: два цикла жгли API и прокси, а finally первого потом
        глушил второй с сообщением «Сбор остановлен» через секунду после запуска.
        """
        if self._collecting:
            return False
        task = self._collect_task
        if task is not None and not task.done():
            return False
        self._collecting = True
        self._collect_task = asyncio.ensure_future(self._collector_loop(client, chat_id))
        return True

    def _stop_collect(self) -> None:
        """Попросить сбор остановиться. Задача завершится не мгновенно — см. _start_collect."""
        self._collecting = False

    async def _collector_loop(self, client, chat_id: int) -> None:
        """Фоновый цикл: крутит ниши×города, копит горячие лиды в базу до цели, потом стоп."""
        from .score import assess_solvency

        combos = self._collect_combos()
        target = self.settings.collect_target
        self._collect_checked = 0

        # Цель уже набрана: цикл `while count < target` не сделал бы ни одной итерации,
        # и пользователь получал «Фоновый сбор запущен» + сразу «Цель достигнута», думая
        # что сбор идёт. Объясняем, как продолжить, вместо холостого запуска.
        if self._base.count() >= target:
            self._collecting = False
            await self._send(
                client, chat_id,
                f"✅ В базе уже {self._base.count()} лидов — это и есть цель ({target}), "
                "собирать больше нечего.\nЧтобы копить дальше — подними "
                "<code>COLLECT_TARGET</code> в .env и перезапусти бота. Забрать собранное — "
                "«📋 Посмотреть базу». Начать с нуля — /clearbase.",
                self._main_keyboard(),
            )
            return

        await self._send(
            client, chat_id,
            f"▶️ <b>Фоновый сбор запущен.</b> Цель: {target} лидов (сейчас в базе {self._base.count()}).\n"
            "Иди занимайся делами — соберётся само. Прогресс буду присылать. Остановить — /stop.",
        )
        i = 0
        # Выдача 2ГИС по каждому комбо стабильна, а глубина ограничена collect_per_query,
        # поэтому после полного круга топ выдачи весь уже в _seen и новых мест не будет.
        # Без этого счётчика цикл крутился бы вечно вхолостую, выжигая лимит API и прокси.
        exhausted = 0
        # Признаки уже собранных лидов. База узнаёт лид по ИНН, ссылке или
        # имени с адресом, но не по телефону, — а у филиалов сети он общий и
        # появляется только после браузерного добора. Без этой проверки в базу
        # ложились два лида с одним номером.
        collected_keys: set[str] = set()
        errors = 0  # подряд идущие падения запроса — см. _COLLECT_MAX_ERRORS
        # Подряд идущие ПУСТЫЕ ответы источника. Считаются отдельно от падений:
        # выгоревший ключ 2ГИС и мёртвый прокси исключения не бросают вовсе —
        # провайдер пишет в лог «запрос пропущен» и возвращает пустой список.
        # Защита по `errors` на это не срабатывала никогда, зато рос `exhausted`,
        # и цикл заканчивался сообщением «обошёл все ниши и города — новых мест
        # не осталось». То есть на мёртвом ключе владельцу сообщали, что
        # компании в стране кончились.
        barren = 0
        try:
            while self._collecting and self._base.count() < target:
                if exhausted >= len(combos):
                    await self._send(
                        client, chat_id,
                        f"🏁 Обошёл все ниши и города — новых мест не осталось "
                        f"(в базе {self._base.count()} из {target}).\n"
                        "Что дальше: /clearbase — начать с нуля, /reset — перепроверить "
                        "уже виденные места, либо поднять COLLECT_PER_QUERY в .env "
                        "(глубже выдача = больше мест на комбо).",
                    )
                    break
                rubric, cityname = combos[i % len(combos)]
                i += 1
                try:
                    orgs = await pipeline_run(
                        [(rubric, cityname)], source=self.settings.search_source,
                        max_results=self.settings.collect_per_query,
                        settings=self.settings, state_path=None, resume=False,
                    )
                except Exception as exc:  # noqa: BLE001 — одиночный сбой — норма, подряд считаем ниже
                    # Одиночный сбой — норма (моргнул прокси). Но если падает ВСЁ подряд
                    # (умер прокси, нет сети), молча крутиться бесполезно: гасим сбор
                    # и говорим об этом, иначе владелец ждёт лидов от мёртвого цикла.
                    errors += 1
                    logger.warning("Коллектор: {!r}/{!r} — {} (подряд {}/{})",
                                   rubric, cityname, exc, errors, _COLLECT_MAX_ERRORS)
                    if errors >= _COLLECT_MAX_ERRORS:
                        await self._send(
                            client, chat_id,
                            f"⛔️ Сбор остановлен: {errors} запросов подряд упали с ошибкой "
                            f"(<code>{html.escape(type(exc).__name__)}</code>).\n"
                            "Похоже на мёртвый прокси или сеть. Проверь /proxy-check, "
                            "почини и запусти сбор заново.",
                        )
                        break
                    await asyncio.sleep(self.settings.collect_delay)
                    continue
                errors = 0  # успешный запрос обнуляет серию сбоев

                # Источник не вернул НИЧЕГО — это не «места кончились», а
                # «спросить не вышло»: выгоревший ключ, мёртвый прокси,
                # заблокированная выдача.
                barren = 0 if orgs else barren + 1
                if barren >= _COLLECT_MAX_ERRORS:
                    await self._send(
                        client, chat_id,
                        f"⛔️ Сбор остановлен: {barren} запросов подряд вернули пусто, "
                        "хотя ошибок не было.\n"
                        "Так выглядит выгоревший ключ 2ГИС или мёртвый прокси — "
                        "проверь /proxy-check и лимит ключа, а не список ниш.",
                    )
                    break

                orgs = [o for o in orgs if _org_key(o) not in self._seen]
                exhausted = 0 if orgs else exhausted + 1
                async for org, ok in iter_enrich_with_browser(orgs, self.settings):
                    if not self._collecting:
                        break
                    if not ok:
                        continue
                    self._seen.add(_org_key(org))
                    self._collect_checked += 1
                    if org.review_complaints:
                        await self._enrich_inn(org, cityname)
                        if not self._is_new_lead(org, collected_keys):
                            continue  # тот же телефон или ИНН — это один лид
                        if self._base.add(org):
                            n = self._base.count()
                            emoji = assess_solvency(org)["emoji"]
                            phone = org.phone[0] if org.phone else "тел?"
                            await self._send(
                                client, chat_id,
                                f"➕ Лид <b>{n}/{target}</b>: {html.escape(org.name)} {emoji} — "
                                f"{html.escape(phone)} ({html.escape(cityname)})",
                            )
                            if n >= target:
                                break
                await asyncio.sleep(self.settings.collect_delay)
        except Exception as exc:  # noqa: BLE001 — последний рубеж коллектора
            logger.exception("Коллектор упал: {}", exc)
        finally:
            done = self._base.count() >= target
            self._collecting = False
            # Забираем список и очищаем его тем же движением: он копился без
            # очистки, и предупреждение повторялось после КАЖДОГО следующего
            # сбора, хотя новых сбоев не было.
            unsaved = self._base.take_unsaved()
            if unsaved:
                # Лиды есть в памяти и в этих сообщениях, но на диск не легли:
                # после перезапуска бота они пропадут. Молчать нельзя — снаружи
                # это неотличимо от нормального сбора.
                names = ", ".join(html.escape(n) for n in unsaved[:5])
                more = f" и ещё {len(unsaved) - 5}" if len(unsaved) > 5 else ""
                await self._send(
                    client, chat_id,
                    f"⚠️ <b>Не записалось на диск: {len(unsaved)}</b>\n"
                    f"{names}{more}\n"
                    "В базе они только до перезапуска — выгрузи сейчас.",
                )
            if done:
                await self._send(
                    client, chat_id,
                    f"🎉 <b>Цель достигнута!</b> В базе {self._base.count()} лидов. "
                    "Жми «📋 Посмотреть базу» — покажу по приоритету.",
                    self._main_keyboard(),
                )
            else:
                await self._send(
                    client, chat_id,
                    f"⏸ Сбор остановлен. В базе {self._base.count()} лидов "
                    f"(проверено мест: {self._collect_checked}).",
                    self._main_keyboard(),
                )

    async def _show_base(self, client, chat_id: int, offset: int = 0) -> None:
        """Показать накопленную базу лидов (по убыванию приоритета, страницами по 5)."""
        leads = self._base.all_sorted()
        if not leads:
            await self._send(
                client, chat_id,
                "📋 База пуста. Запусти фоновый сбор — кнопка «▶️ Собрать базу».",
                self._main_keyboard(),
            )
            return
        page = leads[offset:offset + 5]
        await self._send(
            client, chat_id,
            f"📋 В базе <b>{len(leads)}</b> лидов. "
            f"Показываю {offset + 1}–{offset + len(page)} (сначала самые перспективные):",
        )
        for org in page:
            kb = tg.keyboard([[("🔎 Подробнее по ИНН", f"info:{org.inn}")]]) if org.inn else None
            await self._send(client, chat_id, tg.format_org(org), kb)
            await asyncio.sleep(0.3)
        rows: list[list[tuple[str, str]]] = []
        if offset + 5 < len(leads):
            rows.append([(f"⬇️ Ещё 5 (осталось {len(leads) - offset - 5})", f"base:{offset + 5}")])
        rows.append([("📥 Вся база в Excel", "basexlsx")])
        await self._send(client, chat_id, "———", tg.keyboard(rows))

    def _main_keyboard(self) -> dict:
        """Главное меню: поиск сейчас, просмотр базы, старт/стоп фонового сбора."""
        base_n = self._base.count()
        collect = (
            ("⏸ Остановить сбор", "collect:stop") if self._collecting
            else ("▶️ Собрать базу в фоне", "collect:start")
        )
        return tg.keyboard([
            [("🔎 Подобрать сейчас", "search")],
            [(f"📋 Посмотреть базу ({base_n})", "base:0")],
            [collect],
        ])

    # --- цикл ----------------------------------------------------------------
    def _spawn(self, coro) -> None:
        """Запустить корутину задачей, СОХРАНИВ ссылку (иначе GC съест её на лету) и
        залогировав любое исключение (иначе оно молча проглатывается)."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.exception("Задача апдейта упала: {}", t.exception())

        task.add_done_callback(_done)

    def _allowed_chat(self, chat_id: int | None) -> bool:
        """Разрешён ли чат управлять ботом. Пустой allowlist = НЕ пускаем никого.

        Пустым список оказывается не только по задумке: при TELEGRAM_CHAT_ID=@канал
        числовых id нет вообще. Раньше это значило «пускаем всех», и бот молча
        открывался любому, кто его нашёл, — а он умеет выгрузить всю базу лидов
        и стереть её командой. Поэтому отказываем и громко объясняем, что задать.
        """
        if not self._allowed:
            if not self._open_access_warned:
                self._open_access_warned = True
                logger.error(
                    "ALLOWLIST ПУСТ — бот никого не пускает. Задай числовые id в "
                    "ALLOWED_CHAT_IDS (узнать свой: parser tg-chat-id). "
                    "TELEGRAM_CHAT_ID=@канал в allowlist не считается — там нужны именно числа."
                )
            # Раньше пустой список означал «пускаем всех»: удобно на своей
            # машине и опасно везде ещё. Пустым он оказывается не только по
            # задумке — при TELEGRAM_CHAT_ID=@канал числовых id нет вовсе, и
            # бот молча открывался любому, кто его нашёл: чужой человек мог
            # выгрузить всю базу лидов в Excel и стереть её командой.
            # В спорной ситуации молчим: не знаем, кто это — не пускаем.
            return False
        if chat_id in self._allowed:
            return True
        if chat_id is not None and chat_id not in self._denied_warned:
            self._denied_warned.add(chat_id)
            logger.warning("Отказ чужому чату {} (нет в allowlist).", chat_id)
        return False

    async def _process_update(self, client, update: dict) -> None:
        try:
            if "callback_query" in update:
                cb = update["callback_query"]
                chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
                if not self._allowed_chat(chat_id):
                    await self._answer_cb(client, cb["id"], "Доступ ограничен.")
                    return
                await self._handle_callback(client, cb)
                return
            message = update.get("message")
            if not message:
                return
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text")
            if chat_id is None or not text:
                return
            if not self._allowed_chat(chat_id):
                return  # тихо игнорируем чужих — не тратим ресурсы и не палим наличие бота
            if text.startswith("/"):
                await self._handle_command(client, chat_id, text)
            else:
                await self._handle_command(client, chat_id, "/start")
        except Exception:  # noqa: BLE001 — ни один апдейт не должен уронить цикл или исчезнуть без следа
            logger.exception("Ошибка обработки апдейта: {}", update.get("update_id"))

    async def _register_commands(self, client) -> None:
        """Ставим меню команд (кнопка слева от поля ввода) — setMyCommands."""
        try:
            await tg.post(client, self.token, "setMyCommands", {"commands": BOT_COMMANDS})
            logger.info("Меню команд зарегистрировано ({} команд).", len(BOT_COMMANDS))
        except Exception as exc:  # noqa: BLE001 — меню команд необязательно
            logger.warning("setMyCommands: {}", exc)

    async def _tell_if_rules_changed(self, client) -> None:
        """Говорит один раз, если правила поиска жалоб изменились.

        Журнал проверенных помнит, что компанию уже смотрели, и второй раз её
        никто не покажет. Когда шаблоны расширяются — а расширяются они
        потому, что жалобы находились не все, — прежние «чисто» становятся
        сомнительными. Перепроверять всё молча нельзя: это часы браузера через
        мобильный прокси и деньги за трафик. Поэтому решает владелец, а наше
        дело — не промолчать.
        """
        from .reviews import patterns_fingerprint

        mark = self.settings.output_dir / "seen_rules.txt"
        current = patterns_fingerprint()
        try:
            known = mark.read_text("utf-8").strip() if mark.exists() else ""
        except OSError:
            known = ""
        if known == current:
            return
        try:
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.write_text(current, "utf-8")
        except OSError as exc:  # noqa: BLE001 — не записали отметку, скажем ещё раз
            logger.warning("Отметку о правилах не сохранил: {}", exc)
        if not known or not self._seen.keys:
            return  # первый запуск или пустой журнал — сообщать не о чем
        for chat_id in self._allowed:
            await self._send(
                client, chat_id,
                "🔎 <b>Правила поиска жалоб обновились.</b>\n"
                f"В журнале {len(self._seen.keys)} уже проверенных компаний — среди них "
                "могут быть те, чьи жалобы прежние правила не замечали.\n"
                "Хотите перепроверить — /reset (накопленная база лидов при этом сохраняется).",
            )

    async def run(self) -> None:
        logger.info("Бот запущен. Ожидаю сообщения (Ctrl+C для остановки).")
        async with httpx.AsyncClient(timeout=self.settings.request_timeout + 30) as client:
            await self._register_commands(client)
            await self._tell_if_rules_changed(client)
            # Сбрасываем накопившиеся апдейты, чтобы не отвечать на старьё.
            try:
                data = await tg.post(client, self.token, "getUpdates", {"timeout": 0, "offset": -1})
                if data.get("result"):
                    self._offset = data["result"][-1]["update_id"] + 1
            except Exception:  # noqa: BLE001 — сбой чтения отметки не критичен
                pass

            while True:
                try:
                    data = await tg.post(
                        client, self.token, "getUpdates",
                        {"timeout": 25, "offset": self._offset,
                         "allowed_updates": ["message", "callback_query"]},
                    )
                except Exception as exc:  # noqa: BLE001 — цикл опроса не имеет права умереть
                    if "conflict" in str(exc).lower():  # 409: где-то запущен второй инстанс бота
                        logger.error(
                            "Telegram 409 Conflict — уже запущен ДРУГОЙ инстанс бота с этим токеном. "
                            "Оставь один процесс (проверь: pgrep -fa 'parser bot'). Пауза 15с."
                        )
                        await asyncio.sleep(15)
                    else:
                        logger.warning("getUpdates: {} — пауза 3с", exc)
                        await asyncio.sleep(3)
                    continue
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    # Каждый апдейт в своей задаче — долгий поиск не блокирует остальные чаты.
                    # _spawn держит ссылку на задачу и логирует падения (см. коммент внутри).
                    self._spawn(self._process_update(client, update))


def configure_logging(settings: Settings) -> None:
    """Если задан LOG_FILE — добавляем файловый сток с ротацией (иначе только stderr/journald)."""
    if not settings.log_file:
        return
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(settings.log_file),
        rotation=f"{settings.log_rotation_mb} MB",
        retention=f"{settings.log_retention_days} days",
        compression="zip",
        enqueue=True,  # безопасно при нескольких async-задачах
        level="INFO",
    )
    logger.info("Логи пишутся в файл {} (ротация {} МБ, хранение {} дн).",
                settings.log_file, settings.log_rotation_mb, settings.log_retention_days)


async def run_bot(settings: Settings | None = None) -> None:
    """Точка входа для CLI-команды `parser bot`."""
    settings = settings or get_settings()
    configure_logging(settings)
    await Bot(settings).run()

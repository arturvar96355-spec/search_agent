"""Обходчик бота лидогенерации: жмёт каждую кнопку и смотрит, что вышло.

Кнопки этого бота не проверял до сих пор никто: у Orbit обходчик есть, а здесь
интерфейс собирался руками и руками же проверялся — то есть проверялись те два
пути, по которым ходит владелец. Между тем ошибка в разметке одного экрана
означает не «некрасиво», а «Telegram отверг сообщение целиком»: незакрытый
<b> — и человек не увидит ни лида, ни причины.

Обход в ширину от /start: экран → все его кнопки → все их экраны, пока не
кончатся новые.

Что проверяется на каждом экране:
  · нажатие вообще что-то ответило (иначе кнопка мёртвая: обработчика нет);
  · текст непустой и влезает в лимит Telegram;
  · html-разметка закрыта — иначе Telegram отвергает сообщение целиком;
  · callback_data кнопки влезает в 64 байта;
  · в тексте не пролезло «None» вместо значения;
  · ряды кнопок не переполнены — на телефоне подписи схлопываются в «…»;
  · с экранов выбора есть путь назад, и он ведёт на шаг ближе к началу.

Наружу не уходит НИЧЕГО. Работаем на копии базы лидов и журнала проверенных, а
всё, что стоит денег или дёргает внешний мир, подменено заглушкой: поиск,
фоновый сбор, ИНН через DaData, выгрузка файла и любой запрос в Telegram. Что
именно подменено — видно в отчёте: обходчик честно говорит, чего он не нажимал
по-настоящему, чтобы это не выглядело как «проверено всё».

Запуск:  .venv/bin/python -m tools.walk_bot
"""
from __future__ import annotations

import asyncio
import re
import shutil
import sys
import tempfile
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="walk-leadgen-"))

# Сколько кнопок одного вида жмём. Ниш и городов десятки, устроены они
# одинаково, и три штуки покажут ровно те же ошибки, что и все.
SAME_KIND_LIMIT = 3
# Предел ряда: подписи длиннее схлопываются в «…» и кнопку не прочитать.
ROW_CHARS = 48

problems: list[tuple[str, str]] = []
stubbed: set[str] = set()


def flag(where: str, text: str) -> None:
    problems.append((where, text))


def _sandbox_settings():
    """Копия боевых данных и обезвреженные настройки.

    Копируем, а не «просто читаем»: обходчик жмёт и /clearbase, и «очистить
    журнал». На боевых файлах это стёрло бы накопленную базу лидов.
    """
    from parser.config import get_settings

    settings = get_settings()
    live = settings.output_dir
    for name in ("leads_base.json", "seen_leads.json"):
        source = live / name
        if source.exists():
            shutil.copy2(source, SANDBOX / name)
    settings.output_dir = SANDBOX
    settings.telegram_bot_token = "walker"  # наружу всё равно ничего не уходит
    return settings


def _seed_base(settings) -> int:
    """Кладёт в песочницу выдуманные лиды, если настоящих нет.

    Без них половина интерфейса не жмётся вовсе: карточка лида, кнопка «ещё
    пять», «подробнее по ИНН» и выгрузка появляются только когда база
    непустая. Обход по пустой базе прошёл бы «без проблем» ровно потому, что
    ничего и не смотрел. Двенадцать штук — чтобы листалка показала вторую
    страницу.
    """
    from parser.leadbase import LeadBase
    from parser.models import Organization

    base = LeadBase(SANDBOX / "leads_base.json")
    if base.count():
        return 0
    for number in range(12):
        base.add(Organization(
            source="2gis",
            name=f"Компания «Пример & Co» {number}",
            address=f"Москва, ул. Примерная, {number}",
            source_url=f"https://2gis.ru/moscow/firm/{number}",
            phone=[f"+7 900 000-00-{number:02d}"],
            inn=f"770000000{number:02d}",
            review_complaints=["не берут трубку", "не перезвонили"],
            rating=4.2,
            reviews_count=30 + number,
        ))
    return base.count()


def _refuse_if_live(settings) -> None:
    """Останавливает прогон, если песочница не подставилась.

    Подмена тихая: одна неверная правка выше — и она просто не сработает, а
    обход пойдёт по живым файлам как ни в чём не бывало. Молча стереть базу
    лидов, которую собирали неделю, — не та цена, которую стоит платить за
    краткость.
    """
    if not settings.output_dir.resolve().is_relative_to(SANDBOX.resolve()):
        raise SystemExit(
            f"ОТКАЗ: обходчик смотрит в боевой каталог {settings.output_dir}. "
            f"Он жмёт все кнопки подряд, включая очистку базы. Проверь _sandbox_settings()."
        )


# Теги, которые Telegram понимает в режиме HTML. Всё остальное он не
# «игнорирует», а отвергает СООБЩЕНИЕ ЦЕЛИКОМ («Unsupported start tag») — то
# есть владелец не получает ни карточки, ни объяснения.
#
# Копия такого же списка есть в Orbit (orbit/ui.py). Здесь она своя намеренно:
# это отдельный проект со своим окружением, импортировать соседний ради
# пятнадцати слов значит связать их запуск. Список задан самим Telegram и
# меняется примерно никогда.
TG_TAGS = frozenset({
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote",
})
_TAG_NAME_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


def _unbalanced(text: str) -> str | None:
    """Что в тексте не понравится Telegram: незакрытый или неизвестный тег."""
    stack: list[str] = []
    for match in _TAG_NAME_RE.finditer(text or ""):
        name = match.group(1).lower()
        if name not in TG_TAGS:
            return f"Telegram не знает тег <{name}>"
        if not match.group(0).startswith("</"):
            stack.append(name)
        elif not stack or stack[-1] != name:
            return f"лишний </{name}>"
        else:
            stack.pop()
    return f"не закрыт <{stack[-1]}>" if stack else None


def _check_text(where: str, text: str) -> None:
    if not (text or "").strip():
        flag(where, "пустой текст экрана")
        return
    if len(text) > 4096:
        flag(where, f"текст длиннее лимита Telegram: {len(text)}")
    broken = _unbalanced(text)
    if broken:
        flag(where, f"html-разметка: {broken}")
    if "None" in re.sub(r"<[^>]+>", "", text):
        flag(where, "в тексте пролезло None")


def _check_keyboard(where: str, markup: dict | None) -> list[str]:
    """Проверяет клавиатуру и возвращает коды её кнопок."""
    codes: list[str] = []
    if not markup:
        return codes
    seen: set[str] = set()
    for row in markup.get("inline_keyboard", []):
        if not row:
            flag(where, "пустой ряд кнопок")
            continue
        width = sum(len(button.get("text", "")) for button in row)
        if len(row) > 3 and width > 24:
            flag(where, f"в ряду {len(row)} кнопок — на телефоне это нечитаемо")
        if len(row) > 1 and width > ROW_CHARS:
            labels = " | ".join(button.get("text", "") for button in row)
            flag(where, f"ряд не влезет в экран телефона ({width} знаков): {labels}")
        for button in row:
            data = button.get("callback_data", "")
            if not button.get("text", "").strip():
                flag(where, f"кнопка без подписи: {data}")
            if len(data.encode()) > 64:
                flag(where, f"callback_data длиннее 64 байт: {data}")
            if data in seen:
                flag(where, f"две одинаковые кнопки на экране: {data}")
            seen.add(data)
            codes.append(data)
    return codes


def _make_bot(settings):
    """Бот, у которого весь внешний мир заменён записью в список."""
    from parser.bot import Bot

    class Walker(Bot):
        def __init__(self, conf):
            super().__init__(conf)
            self.screens: list[tuple[str, dict | None]] = []
            self.answers: list[str] = []

        async def _send(self, client, chat_id, text, kb=None):
            self.screens.append((text, kb))

        async def _edit(self, client, chat_id, message_id, text, kb=None):
            self.screens.append((text, kb))

        async def _answer_cb(self, client, callback_id, text=""):
            self.answers.append(text)

        # --- всё, что стоит денег или лезет наружу ---------------------------
        async def _run_search(self, client, chat_id, message_id, count):
            stubbed.add("поиск по кнопке «N организаций»")
            self.screens.append((f"[заглушка] поиск на {count} организаций", None))

        def _start_collect(self, client, chat_id):
            stubbed.add("фоновый сбор")
            return True

        async def _send_company_card(self, client, chat_id, inn):
            stubbed.add("карточка по ИНН (DaData)")
            self.screens.append((f"[заглушка] карточка по ИНН {inn}", None))

        async def _send_export(self, client, chat_id, orgs):
            stubbed.add("выгрузка XLSX")
            self.screens.append(("[заглушка] выгрузка в Excel", None))

    return Walker(settings)


COMMANDS = ("/start", "/help", "/niches", "/cancel", "/base", "/stop", "/что-то")
# Эти команды стирают данные, поэтому жмём их ПОСЛЕ обхода. Иначе /clearbase
# опустошает базу в самом начале, экран базы отвечает «пусто», и половина
# интерфейса — карточка лида, листалка, выгрузка — не обходится вовсе. Обход
# при этом рапортует «проблем не найдено», хотя ничего и не смотрел.
LAST_COMMANDS = ("/reset", "/clearbase")


def walk(bot) -> tuple[int, dict[str, int]]:
    """Обход в ширину от команд. Возвращает число нажатий и охват по видам."""
    presses = 0
    queue: deque[tuple[str, str]] = deque()
    visited: set[str] = set()
    families: dict[str, int] = {}

    for command in COMMANDS:
        bot.screens.clear()
        bot.answers.clear()
        asyncio.run(bot._handle_command(None, 1, command))
        presses += 1
        if not bot.screens:
            flag(command, "команда не ответила ничем")
            continue
        for text, markup in bot.screens:
            _check_text(command, text)
            for code in _check_keyboard(command, markup):
                queue.append((code, command))

    while queue:
        code, came_from = queue.popleft()
        if code in visited:
            continue
        visited.add(code)
        family = code.split(":", 1)[0]
        if families.get(family, 0) >= SAME_KIND_LIMIT:
            continue
        families[family] = families.get(family, 0) + 1

        bot.screens.clear()
        bot.answers.clear()
        asyncio.run(bot._handle_callback(None, {
            "id": "1",
            "data": code,
            "message": {"chat": {"id": 1}, "message_id": 1},
        }))
        presses += 1

        if not bot.screens and not any(a.strip() for a in bot.answers):
            flag(code, f"нажатие ничего не ответило (пришли с «{came_from}»)")
            continue

        for text, markup in bot.screens:
            _check_text(code, text)
            for nxt in _check_keyboard(code, markup):
                queue.append((nxt, code))

    return presses, families


def _after_the_wipe(bot, settings) -> int:
    """Команды, стирающие данные, и жизнь после них.

    Проверяем не только то, что они ответили, но и что бот остался рабочим:
    экран базы после очистки обязан открыться и честно сказать, что пусто, —
    а не упасть и не показать вчерашние цифры.
    """
    presses = 0
    for command in LAST_COMMANDS:
        bot.screens.clear()
        asyncio.run(bot._handle_command(None, 1, command))
        presses += 1
        if not bot.screens:
            flag(command, "команда не ответила ничем")
            continue
        for text, markup in bot.screens:
            _check_text(command, text)
            _check_keyboard(command, markup)

    bot.screens.clear()
    asyncio.run(bot._handle_callback(None, {
        "id": "1", "data": "base:0", "message": {"chat": {"id": 1}, "message_id": 1},
    }))
    presses += 1
    texts = " ".join(text for text, _ in bot.screens)
    if not texts.strip():
        flag("base:0", "после очистки экран базы не открылся")
    elif "пуст" not in texts.lower():
        flag("base:0", f"база очищена, а экран показывает своё: {texts[:80]}")
    return presses


def _check_the_way_back(bot) -> None:
    """«Назад» обязана возвращать на шаг раньше, а не куда-нибудь ещё.

    Проверяем самый частый путь: ниша → город → объём. С города «назад» ведёт
    к нишам, с объёма — к городам. Здесь это единственная навигация вглубь, и
    сломать её проще всего: обе кнопки лежат в разных клавиатурах.
    """
    async def press(code: str) -> list[tuple[str, dict | None]]:
        bot.screens.clear()
        await bot._handle_callback(None, {
            "id": "1", "data": code,
            "message": {"chat": {"id": 1}, "message_id": 1},
        })
        return list(bot.screens)

    async def scenario():
        after_niche = await press("n:all")
        if not any("город" in text.lower() for text, _ in after_niche):
            flag("n:all", "после выбора ниши не спросили город")
        after_city = await press("c:all")
        if not any("организаций" in text.lower() for text, _ in after_city):
            flag("c:all", "после выбора города не спросили объём")

        back_to_cities = await press("back:city")
        if not any("город" in text.lower() for text, _ in back_to_cities):
            flag("back:city", "«назад к городам» не вернула к городам")
        back_to_niches = await press("back:niche")
        if not any("ниш" in text.lower() for text, _ in back_to_niches):
            flag("back:niche", "«назад к нишам» не вернула к нишам")

    asyncio.run(scenario())


def main() -> int:
    settings = _sandbox_settings()
    _refuse_if_live(settings)

    print("═" * 78)
    print(" ОБХОД БОТА ЛИДОГЕНЕРАЦИИ · жмём каждую кнопку")
    print("═" * 78)
    print(f" песочница: {SANDBOX}")

    seeded = _seed_base(settings)
    if seeded:
        print(f" в песочницу положено выдуманных лидов: {seeded} (настоящих в базе нет)")

    bot = _make_bot(settings)
    presses, families = walk(bot)
    _check_the_way_back(bot)
    presses += _after_the_wipe(bot, settings)

    print(f"\n  нажатий проверено: {presses}")
    print("  по видам кнопок: " + ", ".join(
        f"{name} × {count}" for name, count in sorted(families.items())
    ))
    if stubbed:
        print("  по-настоящему НЕ нажимали (стоит денег или лезет наружу):")
        for name in sorted(stubbed):
            print(f"    · {name}")

    print("\n" + "═" * 78)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        print("═" * 78)
        for where, text in problems:
            print(f"  · [{where}] {text}")
        return 1
    print(" ПРОБЛЕМ НЕ НАЙДЕНО")
    print("═" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

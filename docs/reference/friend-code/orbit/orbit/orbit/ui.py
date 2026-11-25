"""Кирпичи интерфейса: экран, клавиатура, разметка карточек.

Здесь всё, что касается того, как выглядит бот. Логика систем этого не знает,
а секции не занимаются экранированием html и нарезкой на страницы.

Правила Telegram, из-за которых код такой:
- в сообщении не больше 4096 знаков, иначе API вернёт ошибку и экран не откроется;
- callback_data кнопки не больше 64 байт, а кириллица это два байта на букву,
  поэтому в кодах кнопок только латиница и цифры;
- html-разметка ломается на «<» и «&» в названиях компаний, их экранируем всегда.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Запас от лимита Telegram: хвост «…» и служебные строки должны влезть.
MAX_TEXT = 3900
# Сколько карточек показываем на одной странице списка.
PAGE_SIZE = 5

Rows = list[list[tuple[str, str]]]


@dataclass
class Screen:
    """Один экран бота: текст, кнопки под ним и всплывашка поверх."""

    text: str
    rows: Rows = field(default_factory=list)
    # Короткое сообщение поверх экрана: «Отправлено», «Лимит исчерпан».
    alert: str | None = None

    @property
    def markup(self) -> dict:
        return {"inline_keyboard": [[_button(label, data) for label, data in row]
                                    for row in self.rows]}


# Кнопка-ссылка задаётся в Telegram отдельным полем url, а не callback_data.
# Внутри Orbit такие кнопки помечаются приставкой «url:».
URL_PREFIX = "url:"
# Кнопка, открывающая мини-апп прямо в Telegram, — ещё одно отдельное поле.
# Отличие от обычной ссылки не косметическое: только такая кнопка передаёт
# приложению подпись, по которой оно узнаёт владельца. Открытая как ссылка,
# та же страница увидит гостя без подписи и никого не пустит.
WEBAPP_PREFIX = "app:"


def _button(label: str, data: str) -> dict:
    if data.startswith(WEBAPP_PREFIX):
        return {"text": label, "web_app": {"url": data[len(WEBAPP_PREFIX):]}}
    if data.startswith(URL_PREFIX):
        return {"text": label, "url": data[len(URL_PREFIX):]}
    return {"text": label, "callback_data": data}


def esc(text: str | None) -> str:
    """Экранирование для html-разметки Telegram.

    Кавычку экранируем тоже: значения вроде адреса сайта подставляются
    внутрь href="…", и одна кавычка в данных разваливает всю карточку.
    """
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Теги, которые Telegram понимает в режиме HTML. Всё остальное он не
# «игнорирует», а отвергает СООБЩЕНИЕ ЦЕЛИКОМ с ошибкой «Unsupported start
# tag» — то есть владелец не получает ни карточки, ни объяснения.
TG_TAGS = frozenset({
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote",
})
# Из них парные — те, у которых обязателен закрывающий тег.
TG_PAIRED = TG_TAGS

_TAG_NAME_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


def bad_html(text: str) -> str | None:
    """Что в тексте не понравится Telegram. None — всё в порядке.

    Живёт рядом с esc() и вызывается проверяющими средствами: и обходчиком
    кнопок, и ночной батареей. Определение одно на всех намеренно — разъехавшись,
    один из них начнёт пропускать то, на чём споткнётся другой.

    Две беды, и обе одинаково смертельны для сообщения: незакрытый тег и тег,
    которого Telegram не знает. Второй проверять не менее важно: обход ловил
    только первое, и подсунутое имя «<script>alert(1)</script>» проходило как
    «разметка в порядке», хотя Telegram отверг бы всю карточку.
    """
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


def clip(text: str, limit: int = MAX_TEXT) -> str:
    """Обрезает по границе строки, чтобы не рвать слово посередине.

    И, что важнее, не рвёт разметку. Обрыв внутри тега («<b») или незакрытый
    тег — это не «экран без жирного шрифта», а отказ Telegram разобрать
    сообщение целиком: экран не открывается вовсе, и нажатие выглядит как
    сломанная кнопка. Границу строки ищем не всегда: если она нашлась в первой
    половине куска, обрезка пришлась бы на середину длинной строки, и там как
    раз может оказаться тег.
    """
    if len(text) <= limit:
        return text

    budget = limit - len(MORE_MARK)
    # Хвост закрывающих тегов считаем, а не закладываем на глаз. Он бывает
    # длиннее любого разумного запаса, и тогда «обрезанный» текст выходил
    # ДЛИННЕЕ исходного лимита — то есть функция ломала ровно то, ради чего
    # существует.
    #
    # И режем С УЧЁТОМ хвоста: пока место под него вычиталось из уже
    # отрезанного куска, `len(cut) + len(tail) <= room` не выполнялось никогда
    # (len(cut) и есть room), и обычный список с одним <b> терял всю разметку.
    room = budget
    for _ in range(4):
        cut = _cut_at(text, room)
        tail = _closing_tags(cut)
        if len(cut) + len(tail) <= budget:
            return cut + tail + MORE_MARK
        room = budget - len(tail)
        if room <= 0:
            break
    # Разметка съела всё место. Текст важнее оформления.
    return _TAG.sub("", text)[:budget] + MORE_MARK


# Что дописываем на месте отрезанного хвоста.
MORE_MARK = "\n\n…список длиннее, листайте страницами."


def _cut_at(text: str, room: int) -> str:
    """Кусок текста длиной не больше room, не разрубающий строку и тег."""
    cut = text[:room]
    edge = cut.rfind("\n")
    if edge > room // 2:
        cut = cut[:edge]
    # Оборвались посреди тега — отступаем до его начала.
    opened = cut.rfind("<")
    if opened > cut.rfind(">"):
        cut = cut[:opened]
    return cut


# Парные теги Telegram: незакрытый ломает разбор всего сообщения.
_TAG = re.compile(r"</?([a-zA-Z-]+)[^>]*>")
_PAIRED = frozenset({"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"})


def _closing_tags(text: str) -> str:
    """Хвост из закрывающих тегов для всего, что осталось открытым."""
    stack: list[str] = []
    for match in _TAG.finditer(text):
        name = match.group(1).lower()
        if name not in _PAIRED:
            continue
        if match.group(0).startswith("</"):
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return "".join(f"</{name}>" for name in reversed(stack))


def pager(code: str, page: int, total: int, size: int = PAGE_SIZE) -> Rows:
    """Строка листания. Пустая, если всё уместилось на одной странице."""
    pages = max(1, (total + size - 1) // size)
    if pages <= 1:
        return []
    row: list[tuple[str, str]] = []
    if page > 0:
        row.append(("⬅️", f"{code}:{page - 1}"))
    row.append((f"{page + 1} из {pages}", "noop"))
    if page < pages - 1:
        row.append(("➡️", f"{code}:{page + 1}"))
    return [row]


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 письмо», «2 письма», «5 писем» — иначе интерфейс выглядит машинным."""
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def counted(count: int, one: str, few: str, many: str) -> str:
    return f"{count} {plural(count, one, few, many)}"

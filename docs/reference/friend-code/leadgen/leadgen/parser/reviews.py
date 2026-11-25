"""Поиск в тексте отзывов сигналов «плохой связи с клиентом».

Идея под B2B-аутрич: организации, которым в отзывах пишут «не дозвониться / не берут
трубку / долго отвечают» — тёплые лиды под бота-автоответчик/CRM. Здесь только матчер
по тексту (источник-агностичный); сам текст отзывов будет поставлять браузерный модуль
(шаг 6) — в официальном API 2ГИС текстов отзывов нет.

Разбор совпадений — через re.finditer (по требованию проекта), не findall.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Категории жалоб -> паттерны. Учитываем морфологию (\w* на хвостах) и ё/е.
# Корень «дозвониться» в двух написаниях: «дозвон-» (дозвонился, дозвониться)
# и «дозванива-» (дозванивался, дозваниваюсь). Второй писали шаблоны мимо, и
# «дозванивалась весь день, бесполезно» проходило как отзыв без жалоб. Один
# кусок на все шаблоны: разъедутся — половина перестанет ловить половину
# написаний, и заметить это будет нечем.
_CALL = r"дозв[оа]н\w+"

COMPLAINT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "no_answer": [
        re.compile(rf"не\s+{_CALL}", re.IGNORECASE),
        re.compile(rf"невозможно\s+{_CALL}", re.IGNORECASE),
        re.compile(rf"не\s+мог\w*\s+{_CALL}", re.IGNORECASE),
        re.compile(r"(?:не\s+берут|не\s+поднимают)\s+трубк\w+", re.IGNORECASE),
        re.compile(r"трубк\w+\s+не\s+бер\w+", re.IGNORECASE),
        re.compile(r"никто\s+не\s+(?:берёт|берет|ответил|отвеча\w+)", re.IGNORECASE),
        re.compile(r"телефон\w*\s+(?:не\s+отвеча\w+|молчит|недоступен)", re.IGNORECASE),
        re.compile(r"не\s+отвеча\w+\s+на\s+(?:звонк\w+|звонок|телефон\w*)", re.IGNORECASE),
        re.compile(r"(?:сбрасывают|игнорируют)\s+звонк\w+", re.IGNORECASE),
        re.compile(r"(?:так\s+и\s+)?не\s+перезвон\w+", re.IGNORECASE),
        # «Не смог / не смогла / не удалось / не получается дозвониться» —
        # самая частая формулировка ровно той боли, которую продаём. Шаблон
        # «не мог* дозвон*» её не ловил: там слово «смог», а не «мог», и между
        # ним и «дозвониться» бывает ещё одно («так и не смог сегодня»).
        re.compile(rf"не\s+(?:с?мог\w*|удал\w+|получ\w+|вышло|выход\w+)"
                   rf"(?:\s+\w+){{0,2}}\s+{_CALL}", re.IGNORECASE),
        # Обратный порядок слов: «дозвониться невозможно/нереально», а также
        # «дозванивался полдня — бесполезно». Прямой порядок уже был выше.
        re.compile(rf"{_CALL}[^.!?]{{0,40}}?(?:невозможно|нереально|бесполезно|безрезультатно)",
                   re.IGNORECASE),
        # «На звонки не реагируют». Обязательно с «не»: «на звонки отвечают» —
        # это похвала, и без этого условия шаблон переворачивал смысл.
        re.compile(r"на\s+звонк\w+\s+не\s+(?:реагир\w+|отвеча\w+|отклик\w+)", re.IGNORECASE),
        re.compile(r"телефон\w*\s+(?:вечно|постоянно|всегда)\s+занят", re.IGNORECASE),
        re.compile(r"ответа\s+(?:так\s+и\s+)?(?:нет|не\s+последовало|не\s+дождал\w+)",
                   re.IGNORECASE),
    ],
    "slow_reply": [
        re.compile(r"долго\s+(?:не\s+)?отвеча\w+", re.IGNORECASE),
        re.compile(r"отвеча\w+\s+(?:очень\s+)?долго", re.IGNORECASE),
        re.compile(r"долго\s+жда\w+\s+ответ\w*", re.IGNORECASE),
        re.compile(r"пришлось\s+(?:долго\s+)?ждать\s+ответ\w*", re.IGNORECASE),
        re.compile(r"медленно\s+отвеча\w+", re.IGNORECASE),
        # «Ждала ответа две недели». Единицу времени требуем явно: без неё
        # шаблон ловил бы и «ждала ответа и дождалась».
        re.compile(r"жда\w+\s+ответ\w*(?:\s+\w+){0,2}\s+(?:недел\w+|мес\w+|дн\w+|день|сут\w+)",
                   re.IGNORECASE),
        # «Ответили только через неделю». Считаем медленным от суток: «через
        # час» тоже не мгновенно, но это уже спор, а не жалоба.
        re.compile(r"(?:ответил\w*|перезвонил\w*|откликнул\w*)\s+(?:только\s+)?"
                   r"(?:через|спустя)\s+(?:\w+\s+)?(?:недел\w+|мес\w+|дн\w+|день|сут\w+)",
                   re.IGNORECASE),
    ],
}


def patterns_fingerprint() -> str:
    """Короткий отпечаток набора правил. Меняется, когда меняются шаблоны.

    Нужен вот зачем: журнал проверенных помнит, что компанию уже смотрели, и
    второй раз её никто не покажет. Когда шаблоны расширяются — а расширяются
    они как раз потому, что жалобы находились не все, — все прежние «чисто»
    становятся сомнительными. Молча перепроверять всё нельзя: это часы
    браузера через мобильный прокси. Поэтому просто говорим владельцу, что
    правила изменились и что /reset перепроверит старых.
    """
    import hashlib

    parts = [
        pattern.pattern
        for category in sorted(COMPLAINT_PATTERNS)
        for pattern in COMPLAINT_PATTERNS[category]
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# Сколько знаков цитаты показываем максимум. Цитата — это доказательство
# жалобы, а не сам отзыв: длиннее двух строк её всё равно никто не читает.
#
# Ограничение не косметическое. Совпадения, стоящие ближе двенадцати знаков
# друг к другу, склеиваются в один спан, а на вход подаётся текст всей
# страницы отзывов — двенадцать прокруток. Страница, где на недозвон жалуются
# подряд, давала «цитату» в четыре с лишним тысячи знаков: карточка
# организации переваливала за лимит Telegram, тот отвергал её целиком, и лид
# молча пропадал из отчёта — в логе строка, на экране ничего.
SNIPPET_LIMIT = 160


def _snippet(text: str, start: int, end: int, pad: int = 30) -> str:
    """Короткая цитата вокруг совпадения (с многоточиями по краям)."""
    left = max(0, start - pad)
    right = min(len(text), end + pad)
    fragment = re.sub(r"\s+", " ", text[left:right]).strip()
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    if len(fragment) > SNIPPET_LIMIT:
        # Режем по границе слова: «не дозвонишьс» читается как опечатка.
        fragment = fragment[:SNIPPET_LIMIT].rsplit(" ", 1)[0] or fragment[:SNIPPET_LIMIT]
        suffix = "…"
    return f"{prefix}{fragment}{suffix}"


def _merged_hits(text: str, gap: int = 12) -> list[tuple[set[str], int, int]]:
    """Все совпадения -> объединённые близкие/перекрытые спаны (categories, start, end)."""
    hits: list[tuple[int, int, str]] = []
    for category, patterns in COMPLAINT_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(text):  # re.finditer, не findall
                hits.append((match.start(), match.end(), category))
    hits.sort()

    merged: list[tuple[set[str], int, int]] = []
    for start, end, category in hits:
        if merged and start <= merged[-1][2] + gap:  # перекрытие или рядом — объединяем
            cats, prev_start, prev_end = merged[-1]
            cats.add(category)
            merged[-1] = (cats, prev_start, max(prev_end, end))
        else:
            merged.append(({category}, start, end))
    return merged


def find_complaints(text: str | None) -> list[tuple[str, str]]:
    """Список (категория, цитата) для одного текста отзыва. Пусто, если ничего не найдено.

    Близкие совпадения схлопнуты в одну цитату; категория — 'no_answer'/'slow_reply'
    (или 'no_answer/slow_reply', если в одном фрагменте оба сигнала).
    """
    if not text:
        return []
    return [
        ("/".join(sorted(cats)), _snippet(text, start, end))
        for cats, start, end in _merged_hits(text)
    ]


def scan_reviews(reviews: Iterable[str]) -> list[str]:
    """Прогон по нескольким отзывам -> список уникальных цитат-сигналов для Organization."""
    quotes: list[str] = []
    for review in reviews:
        for _category, quote in find_complaints(review):
            if quote not in quotes:
                quotes.append(quote)
    return quotes


def categories(reviews: Iterable[str]) -> set[str]:
    """Какие категории жалоб встретились (no_answer / slow_reply) — для фильтрации лидов."""
    result: set[str] = set()
    for review in reviews:
        for cats, _start, _end in _merged_hits(review):
            result.update(cats)
    return result

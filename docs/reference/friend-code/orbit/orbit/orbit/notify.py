"""Сообщения владельцу в тот же чат, где живёт бот.

Отдельно от bot.py намеренно: bot.py — это про кнопки и экраны, а сюда
приходят фоновые дела (бэкап, сводка, ответ на письмо). Им не нужен ни
роутер, ни реестр разделов, и тащить их за собой ради одной строки текста
значит связать половину проекта с половиной другой.

Молчать здесь нельзя, но и падать нельзя тоже: если Telegram недоступен,
фоновая задача обязана довести своё дело до конца и просто записать в лог,
что сообщить не удалось.
"""
from __future__ import annotations

import re
from html import unescape

import httpx
from loguru import logger

from .config import get_settings
from .ui import TG_TAGS

API = "https://api.telegram.org/bot{token}/sendMessage"
# Телеграм режет сообщения длиннее 4096 символов. Режем сами и с запасом,
# чтобы не потерять закрывающий тег и не получить ошибку разбора HTML.
LIMIT = 3900


def _clip(text: str) -> str:
    """Режет длинный текст, не разрубая разметку.

    Резать по счётчику символов нельзя: если граница попала внутрь тега или
    между `<b>` и `</b>`, Telegram отвечает ошибкой разбора и не показывает
    сообщение ВООБЩЕ. Ответ клиента при этом теряется целиком — вместо того
    чтобы показаться урезанным. Поэтому отходим назад до конца последнего
    целого тега и закрываем то, что осталось открытым.
    """
    if len(text) <= LIMIT:
        return text

    tail = "\n\n…текст длиннее, показал начало."
    budget = LIMIT - len(tail)
    # Длину хвоста из закрывающих тегов считаем, а не закладываем на глаз: на
    # тексте с сотней открытых тегов «обрезанное» сообщение выходило вдвое
    # длиннее лимита. Но и резать надо С УЧЁТОМ хвоста: пока место под него
    # вычиталось из уже отрезанного куска, условие не сходилось никогда, и
    # обычная карточка с одной цитатой теряла всю разметку и все ссылки.
    room = budget
    for _ in range(4):
        cut = text[:room]
        opened = cut.rfind("<")
        if opened > cut.rfind(">"):
            cut = cut[:opened]
        closing = _closing_for(cut)
        if len(cut) + len(closing) <= budget:
            return cut + closing + tail
        room = budget - len(closing)
        if room <= 0:
            break
    # Разметка съела всё место. Текст важнее оформления.
    return _TAG_RE.sub("", text)[:budget] + tail


# Дефис в имени тега значим: «tg-spoiler» и «tg-emoji» — обычные теги
# Telegram, а по [a-z]+ они не находились вовсе и при обрезке не закрывались.
_TAG_RE = re.compile(r"<(/?)([a-z][a-z0-9-]*)[^>]*>", re.IGNORECASE)
# Теги, которые Telegram разбирает в режиме HTML. Список один на весь проект
# (см. orbit/ui.py): здесь он был свой и короче — «ins», «del» и «tg-spoiler»
# в нём отсутствовали, и обрезка длинного текста их не закрывала.
_PAIRED = TG_TAGS


def _close_tags(html: str) -> str:
    """Текст вместе с хвостом закрывающих тегов."""
    return html + _closing_for(html)


def _closing_for(html: str) -> str:
    """Только хвост: теги, оставшиеся открытыми после обрезки."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(html):
        closing, name = match.group(1), match.group(2).lower()
        if name not in _PAIRED:
            continue
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
            elif name in stack:
                stack.remove(name)
        else:
            stack.append(name)
    return "".join(f"</{name}>" for name in reversed(stack))


def tell(text: str, *, buttons: list[tuple[str, str]] | None = None) -> bool:
    """Пишет владельцу. True — дошло.

    buttons — пары (подпись, ссылка). Только ссылки: у фоновых задач нет
    экрана, к которому Telegram мог бы привязать нажатие кнопки.
    """
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Некуда написать владельцу: нет токена или chat_id")
        return False

    body = _clip(text)
    payload: dict = {
        "chat_id": settings.telegram_chat_id,
        "text": body,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": label, "url": url}] for label, url in buttons]
        }

    url = API.format(token=settings.telegram_bot_token)
    try:
        response = httpx.post(url, json=payload, timeout=20.0)
    except httpx.HTTPError as exc:
        logger.warning("Не смог написать владельцу: {}", exc)
        return False
    if response.status_code != 200:
        logger.warning("Telegram вернул {}: {}", response.status_code, response.text[:200])
        # Чаще всего это разбор разметки: в текст попала угловая скобка из
        # чужого письма. Сообщение важнее оформления — повторяем без разметки,
        # иначе ответ клиента не дойдёт вовсе.
        # Слова разбираем по всему списку, а не по двум. Неизвестный тег
        # Telegram называет иначе — «unsupported start tag "script"», — и ни
        # «parse», ни «entity» в этом ответе нет: сообщение не повторялось
        # вовсе и пропадало совсем. А это ровно тот случай, когда в текст
        # попало чужое имя с угловой скобкой.
        trouble = response.text.lower()
        if any(word in trouble for word in ("parse", "entity", "tag", "unsupported")):
            payload["parse_mode"] = None
            # Не только снять теги, но и вернуть символы: без parse_mode
            # Telegram показывает «&amp;» и «&lt;» буквами, и вместо
            # «Иванов & Ко» владелец читает «Иванов &amp; Ко». Разметку мы
            # тут уже потеряли — терять ещё и текст незачем.
            payload["text"] = unescape(re.sub(r"<[^>]+>", "", body))
            try:
                retry = httpx.post(url, json=payload, timeout=20.0)
            except httpx.HTTPError as exc:
                logger.warning("Повтор без разметки тоже не прошёл: {}", exc)
                return False
            if retry.status_code == 200:
                logger.info("Отправил без разметки — Telegram не принял HTML")
                return True
            logger.warning("Повтор без разметки: {}", retry.text[:200])
        return False
    return True

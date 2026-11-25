"""Сообщения владельцу.

Здесь ошибка стоит не денег, а знания: не дошедшее уведомление про ответ
клиента невозможно отличить от «клиент не ответил». Поэтому режем текст так,
чтобы Telegram его принял, а если он всё-таки не принял — повторяем без
разметки, потому что содержание важнее оформления.
"""
from __future__ import annotations

import pytest

from orbit import notify


def test_short_text_is_untouched():
    assert notify._clip("<b>Коротко</b>") == "<b>Коротко</b>"


def test_long_text_gets_a_tail():
    text = "а" * (notify.LIMIT + 100)
    clipped = notify._clip(text)
    assert len(clipped) <= notify.LIMIT + 60
    assert "текст длиннее" in clipped


def test_cut_inside_a_tag_does_not_leave_a_stump():
    """Обрыв посреди `<b` — и Telegram не покажет сообщение вообще."""
    text = "а" * (notify.LIMIT - 2) + "<b>жирный</b>"
    clipped = notify._clip(text)
    assert "<b" not in clipped.replace("<b>", "").replace("</b>", "")


def test_open_tag_is_closed():
    """Граница попала между `<b>` и `</b>` — закрываем сами."""
    text = "<b>" + "я" * notify.LIMIT + "</b>"
    clipped = notify._clip(text)
    assert clipped.count("<b>") == clipped.count("</b>")


def test_nested_tags_are_closed_in_order():
    text = "<b><i>" + "я" * notify.LIMIT + "</i></b>"
    clipped = notify._clip(text)
    assert clipped.endswith("…текст длиннее, показал начало.")
    assert clipped.count("<i>") == clipped.count("</i>")
    assert clipped.count("<b>") == clipped.count("</b>")


def test_balanced_markup_is_not_touched():
    text = "<b>шапка</b>\n" + "я" * notify.LIMIT
    clipped = notify._clip(text)
    assert clipped.count("<b>") == clipped.count("</b>") == 1


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<b>раз", "<b>раз</b>"),
        ("<b>раз</b>", "<b>раз</b>"),
        ("<b><i>два", "<b><i>два</i></b>"),
        ('<a href="x">три', '<a href="x">три</a>'),
        ("обычный текст", "обычный текст"),
        ("<br>перенос", "<br>перенос"),
    ],
)
def test_close_tags(html, expected):
    assert notify._close_tags(html) == expected


def test_the_plain_text_retry_gives_back_real_characters(monkeypatch):
    """Повтор без разметки должен читаться, а не показывать «&amp;» буквами.

    Разметку на этом пути мы уже потеряли — Telegram её не принял. Терять
    ещё и текст незачем: вместо «Иванов & Ко» владелец читал «Иванов &amp; Ко».
    """
    import httpx

    from orbit import notify

    sent: list[dict] = []

    class Response:
        def __init__(self, code, text=""):
            self.status_code = code
            self.text = text

    def post(url, json, timeout):
        sent.append(json)
        if len(sent) == 1:
            return Response(400, '{"description":"can\'t parse entities"}')
        return Response(200)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(notify, "get_settings", lambda: _Settings())

    assert notify.tell("<b>Иванов &amp; Ко</b> — 5 &lt; 7")
    assert len(sent) == 2
    assert sent[1]["parse_mode"] is None
    assert sent[1]["text"] == "Иванов & Ко — 5 < 7"


class _Settings:
    telegram_bot_token = "токен"
    telegram_chat_id = "42"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("сотня открытых тегов", "<b>" * 3000 + "хвост"),
        ("вложенные цитаты", "<blockquote><b><i>" * 900 + "текст"),
        ("простой длинный текст", "а" * 9000),
    ],
)
def test_clip_for_telegram_really_fits(name, text):
    """Сообщение владельцу обязано влезть в лимит вместе с закрывающими тегами."""
    from orbit.notify import LIMIT, _clip

    assert len(_clip(text)) <= LIMIT, name


def test_clip_for_telegram_keeps_the_text_over_the_markup():
    """Если теги съели всё место — оставляем слова, а не разметку."""
    from orbit.notify import _clip

    out = _clip("<b>" * 3000 + "ответ клиента")
    assert "ответ клиента" in out
    assert "<b>" not in out


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("карточка с цитатой", "<blockquote>" + "слово " * 900 + "</blockquote>"),
        ("карточка со ссылкой",
         "<b>Ответ</b>\n" + "слово " * 800 + '<a href="https://firma.ru">письмо</a>'),
    ],
)
def test_a_normal_long_message_keeps_its_markup(name, text):
    """То же для сообщений владельцу: обычная длинная карточка остаётся размеченной."""
    from orbit.notify import LIMIT, _clip

    out = _clip(text)
    assert len(out) <= LIMIT, name
    assert "<" in out, f"{name}: разметку потеряли целиком"

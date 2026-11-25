"""Кирпичи интерфейса.

Ограничения Telegram жёсткие: длинное сообщение не отправится вообще, кнопка
с длинным кодом молча не сработает, а «<» в названии компании сломает разметку
и экран не откроется. Всё это ловится здесь.
"""
from __future__ import annotations

import pytest

from orbit.ui import MAX_TEXT, Screen, clip, counted, esc, pager


def test_escape_breaks_nothing():
    assert esc('ООО «Рога & <Копыта>»') == "ООО «Рога &amp; &lt;Копыта&gt;»"
    assert esc(None) == ""


def test_short_text_untouched():
    assert clip("коротко") == "коротко"


def test_long_text_cut_to_limit():
    text = "\n".join(f"строка {i}" for i in range(2000))
    result = clip(text)
    assert len(result) <= MAX_TEXT + 60
    assert result.endswith("листайте страницами.")


def test_cut_happens_on_line_boundary():
    """Обрыв посреди слова выглядит как сбой, а не как «дальше есть ещё»."""
    text = "\n".join("одинаковая строка" for _ in range(1000))
    body = clip(text).rsplit("\n\n", 1)[0]
    assert body.endswith("одинаковая строка")


# --- Листалка ---------------------------------------------------------------

def test_no_pager_for_single_page():
    assert pager("p:list", 0, 3) == []


def test_first_page_has_no_back_arrow():
    row = pager("p:list", 0, 20)[0]
    labels = [label for label, _ in row]
    assert "⬅️" not in labels
    assert "➡️" in labels


def test_last_page_has_no_forward_arrow():
    row = pager("p:list", 3, 20)[0]
    labels = [label for label, _ in row]
    assert "➡️" not in labels
    assert "⬅️" in labels


def test_pager_counts_pages():
    row = pager("p:list", 1, 12)[0]
    assert any("2 из 3" in label for label, _ in row)


# --- Кнопки -----------------------------------------------------------------

def test_callback_button():
    markup = Screen("текст", [[("Кнопка", "p:home")]]).markup
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == "p:home"
    assert "url" not in button


def test_url_button_uses_url_field():
    """Ссылка в Telegram задаётся отдельным полем, иначе кнопка не откроется."""
    markup = Screen("текст", [[("Открыть", "url:https://t.me/uniqore")]]).markup
    button = markup["inline_keyboard"][0][0]
    assert button["url"] == "https://t.me/uniqore"
    assert "callback_data" not in button


# --- Склонения --------------------------------------------------------------

@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 письмо"), (2, "2 письма"), (5, "5 писем"), (11, "11 писем"), (21, "21 письмо"),
     (114, "114 писем"), (0, "0 писем")],
)
def test_plural_forms(count, expected):
    assert counted(count, "письмо", "письма", "писем") == expected


# --- Главное меню: что требует человека ------------------------------------------


def test_attention_block_is_empty_when_nothing_needs_a_human(store):
    """Всё идёт само — блока быть не должно. Пустой заголовок это шум."""
    from orbit import bot

    assert bot.attention_lines(store) == []


def test_attention_block_names_what_is_waiting(store, agency, company):
    """Пульт открывают между делами, и первый вопрос — «мне что-то надо сделать?».

    Раньше на него отвечали цифры состояния («в базе 115»), по которым это
    приходилось выводить самому.
    """
    from orbit import bot
    from orbit.models import Channel, Draft, Stage

    a = store.upsert_contact(agency)
    c = store.upsert_contact(company)
    store.set_stage(a, Stage.CALL, "договорились")
    store.set_stage(c, Stage.REPLIED, "ответил")
    store.save_draft(Draft(contact_pk=a, channel=Channel.EMAIL, subject="Т", body="Текст."))

    lines = "\n".join(bot.attention_lines(store))
    assert "Готовы к созвону: 1" in lines
    assert "Ответили и ждут вас: 1" in lines
    assert "Письма ждут отправки" in lines


def test_call_ready_goes_first(store, agency, company):
    """Согласие на созвон — самое ценное событие, ему первая строка."""
    from orbit import bot
    from orbit.models import Stage

    a = store.upsert_contact(agency)
    c = store.upsert_contact(company)
    store.set_stage(a, Stage.REPLIED, "ответил")
    store.set_stage(c, Stage.CALL, "договорились")

    lines = bot.attention_lines(store)
    assert "созвону" in lines[0]


def test_main_menu_survives_a_broken_attention_block(store, monkeypatch):
    """Сводка не должна ронять главный экран: без него бот бесполезен."""
    from orbit import bot
    from orbit.registry import Registry

    def boom(_store):
        raise RuntimeError("база прилегла")

    monkeypatch.setattr(bot, "attention_lines", boom)
    screen = bot.main_menu(Registry(), store)
    assert screen.text.strip()


# --- Обрезка экрана не должна ломать разметку ---------------------------------

def test_clip_never_leaves_half_a_tag():
    """Обрыв внутри тега — это не «экран без жирного», а неоткрывшийся экран.

    Telegram отказывается разбирать такое сообщение целиком, и нажатие
    выглядит как сломанная кнопка.
    """
    from orbit.ui import MAX_TEXT, clip

    for shift in range(1, 16):
        text = "a" * (MAX_TEXT - shift) + "<blockquote>хвост</blockquote>"
        cut = clip(text)
        last = cut.rfind("<")
        assert last < 0 or ">" in cut[last:], f"обрубок тега при сдвиге {shift}"


def test_clip_closes_what_it_opened():
    from orbit.ui import MAX_TEXT, clip

    text = "<b>" + "слово " * MAX_TEXT
    cut = clip(text)
    assert cut.count("<b>") == cut.count("</b>")


def test_short_text_is_untouched():
    from orbit.ui import clip

    assert clip("<b>коротко</b>") == "<b>коротко</b>"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("сотня открытых тегов", "<b>" * 3000 + "хвост"),
        ("вложенные цитаты", "<blockquote><b><i>" * 900 + "текст"),
        ("простой длинный текст", "а" * 9000),
        ("длинный список с разметкой",
         "<b>Заголовок</b>\n" + "· <b>компания</b> — новая\n" * 500),
    ],
)
def test_clip_really_fits_the_limit(name, text):
    """Обрезка обязана уложиться в лимит, чего бы это ни стоило.

    Хвост из закрывающих тегов дописывался ПОСЛЕ обрезки, и его длину никто
    не считал: на тексте с сотней открытых тегов «обрезанный» результат
    выходил вдвое длиннее лимита. Telegram такое сообщение отвергает целиком,
    и экран не открывается вовсе — нажатие выглядит как сломанная кнопка.
    """
    from orbit.ui import MAX_TEXT, clip

    out = clip(text)
    assert len(out) <= MAX_TEXT, f"{name}: {len(out)} знаков"


def test_clip_keeps_the_text_when_markup_eats_all_the_room():
    """Разметка съела всё место — текст важнее оформления."""
    from orbit.ui import clip

    out = clip("<b>" * 3000 + "важное имя компании")
    assert "важное имя компании" in out
    assert "<b>" not in out


def test_clip_keeps_markup_balanced_on_a_normal_list():
    """Обычный длинный список остаётся размеченным и закрытым."""
    import re

    from orbit.ui import clip

    out = clip("<b>Клиенты</b>\n" + "· <b>Компания</b> — новая\n" * 400)
    opens = re.findall(r"<([a-z]+)[^>]*>", out)
    closes = re.findall(r"</([a-z]+)>", out)
    assert sorted(opens) == sorted(closes), out[-80:]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("список с выделением", "<b>Клиенты</b>\n" + "· <b>Компания</b> — новая\n" * 400),
        ("карточка со ссылкой",
         "<b>Карточка</b>\n" + "слово " * 800 + '<a href="https://firma.ru">сайт</a>'),
        ("длинная цитата", "<blockquote>" + "слово " * 900 + "</blockquote>"),
    ],
)
def test_a_normal_long_screen_keeps_its_markup(name, text):
    """Обрезка обычного экрана не должна снимать всю разметку.

    Место под хвост закрывающих тегов вычиталось из УЖЕ отрезанного куска, и
    условие «влезет» не выполнялось никогда: длина куска и есть остаток.
    """
    from orbit.ui import MAX_TEXT, clip

    out = clip(text)
    assert len(out) <= MAX_TEXT, name
    assert "<" in out, f"{name}: разметку потеряли целиком"


def test_telegram_rejects_more_than_unclosed_tags():
    """Незакрытый тег — не единственная беда: неизвестный тег тоже фатален.

    Telegram отвергает сообщение целиком с «Unsupported start tag», и владелец
    не получает ни карточки, ни объяснения. Проверка знала только про
    незакрытые, и имя вида «<script>alert(1)</script>» считалось нормальным.
    """
    from orbit.ui import bad_html

    assert bad_html("<b>всё хорошо</b>") is None
    assert bad_html("просто текст") is None
    assert bad_html('<a href="https://x.ru">ссылка</a>') is None
    assert bad_html("<b>не закрыт") == "не закрыт <b>"
    assert bad_html("лишний</b>") == "лишний </b>"
    assert bad_html("<script>alert(1)</script>") == "Telegram не знает тег <script>"
    assert bad_html("<div>карточка</div>") == "Telegram не знает тег <div>"
    # Экранированное — это уже текст, а не тег.
    from orbit.ui import esc

    assert bad_html(esc("<script>alert(1)</script>")) is None


def test_an_unsupported_tag_is_still_delivered(monkeypatch):
    """Неизвестный тег Telegram называет не так, как ошибку разбора.

    «unsupported start tag "script"» — ни «parse», ни «entity» в этом ответе
    нет, и повтор без разметки не делался: сообщение пропадало совсем. Это как
    раз тот случай, когда в текст попало чужое имя с угловой скобкой.
    """
    import httpx

    from orbit import notify

    monkeypatch.setattr(
        notify, "get_settings",
        lambda: type("S", (), {"telegram_bot_token": "t", "telegram_chat_id": "1"})(),
    )

    seen: list[dict] = []

    class Reply:
        def __init__(self, code, text):
            self.status_code = code
            self.text = text

    def post(url, json=None, timeout=None):
        seen.append(json)
        if len(seen) == 1:
            return Reply(400, 'Bad Request: unsupported start tag "script" at byte offset 12')
        return Reply(200, "ok")

    monkeypatch.setattr(httpx, "post", post)

    assert notify.tell("<script>привет</script>") is True
    assert len(seen) == 2, "повтора без разметки не было"
    assert seen[1]["parse_mode"] is None
    assert "привет" in seen[1]["text"]


def test_a_spoiler_is_closed_when_the_text_is_cut():
    """«tg-spoiler» и «tg-emoji» — обычные теги Telegram, и обрезка их не видела.

    Имя с дефисом не подходило под разбор, тег оставался незакрытым, и
    обрезанное сообщение Telegram отвергал целиком.
    """
    from orbit import notify

    long_text = "<tg-spoiler>" + "текст " * 2000
    cut = notify._clip(long_text)
    assert len(cut) <= notify.LIMIT
    from orbit.ui import bad_html

    assert bad_html(cut) is None, cut[-80:]

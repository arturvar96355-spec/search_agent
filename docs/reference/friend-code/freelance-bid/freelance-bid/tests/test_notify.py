import pytest


def test_a_card_rejected_for_markup_still_reaches_the_owner(monkeypatch):
    """Telegram не принял разметку — карточка обязана дойти простым текстом.

    Сюда приходят вопросы клиентов и готовые ответы на них. Потерять карточку
    целиком хуже, чем показать её без выделений, а без повтора она пропадала
    молча: в логе строка, на экране ничего.
    """
    import httpx

    from fbid import notify

    sent: list[dict] = []

    class Response:
        def __init__(self, code, text=""):
            self.status_code = code
            self.text = text

    def post(url, json, timeout):
        sent.append(json)
        if len(sent) == 1:
            return Response(400, '{"description":"Bad Request: can\'t parse entities"}')
        return Response(200)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(notify, "get_settings", lambda: _FakeSettings())

    assert notify.send("<b>Клиент &amp; Ко</b> спрашивает: 5 &lt; 7?")
    assert len(sent) == 2
    assert sent[1]["parse_mode"] is None
    assert sent[1]["text"] == "Клиент & Ко спрашивает: 5 < 7?"


def test_a_card_rejected_for_another_reason_is_not_retried(monkeypatch):
    """А на «чат не найден» повторять нечего — это не про разметку."""
    import httpx

    from fbid import notify

    sent: list[dict] = []

    class Response:
        def __init__(self, code, text=""):
            self.status_code = code
            self.text = text

    def post(url, json, timeout):
        sent.append(json)
        return Response(400, '{"description":"Bad Request: chat not found"}')

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(notify, "get_settings", lambda: _FakeSettings())

    assert not notify.send("<b>Что-то</b>")
    assert len(sent) == 1


class _FakeSettings:
    telegram_bot_token = "токен"
    telegram_chat_id = "42"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("сотня открытых тегов", "<b>" * 2000 + "хвост"),
        ("вложенные цитаты", "<blockquote><b><i>" * 600 + "текст"),
        ("простой длинный текст", "а" * 9000),
        ("ровно лимит плюс один", "б" * 4097),
        ("длинная карточка с цитатой",
         "<b>Клиент ответил</b>\n<blockquote>" + "длинное сообщение клиента. " * 200
         + "</blockquote>\n<i>подпись</i>"),
    ],
)
def test_fit_to_limit_really_fits(name, text):
    """Функция обязана уложиться в лимит Telegram, чего бы ей это ни стоило.

    Запас на закрывающие теги был угадан на глаз — восемьдесят знаков. На
    тексте с сотней открытых тегов хвост в него не влезал, и функция
    возвращала вдвое больше лимита: Telegram отвергал сообщение целиком, и
    карточка не приходила вовсе.
    """
    from fbid.notify import TELEGRAM_LIMIT, fit_to_limit

    out = fit_to_limit(text)
    assert len(out) <= TELEGRAM_LIMIT, f"{name}: {len(out)} знаков"


def test_fit_to_limit_keeps_the_text_when_markup_eats_all_the_room():
    """Разметка съела всё место — текст важнее оформления."""
    from fbid.notify import fit_to_limit

    out = fit_to_limit("<b>" * 2000 + "важные слова клиента")
    assert "важные слова клиента" in out
    assert "<b>" not in out


def test_fit_to_limit_keeps_markup_on_a_normal_card():
    """А обычная длинная карточка обязана остаться размеченной и закрытой."""
    import re

    from fbid.notify import fit_to_limit

    text = "<b>Клиент ответил</b>\n<blockquote>" + "сообщение клиента. " * 300 + "</blockquote>"
    out = fit_to_limit(text)
    opens = re.findall(r"<([a-z]+)[^>]*>", out)
    closes = re.findall(r"</([a-z]+)>", out)
    assert sorted(opens) == sorted(closes), out[-80:]
    assert "<b>Клиент ответил</b>" in out


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("карточка с цитатой",
         "<b>Клиент ответил</b>\n<blockquote>" + "слово " * 900 + "</blockquote>"),
        ("карточка со ссылкой",
         "<b>Заказ</b>\n" + "слово " * 800 + '<a href="https://fl.ru/1">Открыть</a>'),
        ("длинный список",
         "<b>Очередь</b>\n" + "· <i>заказ</i> — ждёт\n" * 400),
    ],
)
def test_a_normal_long_card_keeps_its_markup(name, text):
    """Обрезка обычной карточки не должна снимать разметку и ссылки.

    Место под хвост закрывающих тегов вычиталось из УЖЕ отрезанного куска, и
    условие «влезет» не выполнялось никогда: длина куска и есть остаток. Три
    захода подряд не сходились, и карточка уходила совсем без тегов.
    """
    from fbid.notify import TELEGRAM_LIMIT, fit_to_limit

    out = fit_to_limit(text)
    assert len(out) <= TELEGRAM_LIMIT, name
    assert "<" in out, f"{name}: разметку потеряли целиком"


def test_an_unsupported_tag_is_still_delivered(monkeypatch):
    """«unsupported start tag» — не «parse» и не «entity», а карточка пропадала.

    Сюда приходят чужие тексты: заголовки заказов и сообщения клиентов. Угловая
    скобка в них не редкость, и терять из-за неё вопрос клиента нельзя.
    """
    import httpx

    from fbid import notify

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

    assert notify.send("<script>вопрос клиента</script>") is True
    assert len(seen) == 2, "повтора без разметки не было"
    assert seen[1]["parse_mode"] is None
    assert "вопрос клиента" in seen[1]["text"]


def test_a_spoiler_is_closed_when_the_card_is_cut():
    """«tg-spoiler» стоял в списке парных, но разбор его не находил.

    Имя с дефисом не подходило под регулярку, тег оставался незакрытым, и
    обрезанную карточку Telegram отвергал целиком.
    """
    from fbid.notify import fit_to_limit

    cut = fit_to_limit("<tg-spoiler>" + "текст " * 2000)
    assert cut.count("<tg-spoiler>") == cut.count("</tg-spoiler>"), cut[-80:]

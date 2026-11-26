"""Карточки и сообщения, которые бот отдаёт в Telegram.

Здесь два рода проверок. Первый — что карточка организации читается человеком
и не разваливается на разметке: имена компаний приходят с «&», кавычками и
угловыми скобками, а Telegram на кривом HTML не показывает сообщение вовсе.
Второй — что наружу не уходит то, чему там не место: пароль прокси из текста
ошибки, ключи, токены.
"""
from __future__ import annotations

import pytest

from parser import telegram as tg
from parser.models import Organization


def _org(**kwargs) -> Organization:
    data = {
        "name": "Стоматология «Улыбка»",
        "rubric": "Стоматология",
        "phone": ["+7 495 123-45-67"],
        "source": "2gis",
        "source_url": "https://2gis.ru/firm/1",
    }
    data.update(kwargs)
    return Organization(**data)


# --- Карточка организации ------------------------------------------------------


def test_card_has_name_and_rubric():
    text = tg.format_org(_org())
    assert "Улыбка" in text
    assert "Стоматология" in text


def test_dangerous_characters_are_escaped():
    """«PR&Digital» и кавычки ломают разбор HTML — Telegram молча не покажет."""
    text = tg.format_org(_org(name="A&B <Digital>"))
    assert "&amp;" in text
    assert "<Digital>" not in text


def test_complaints_are_the_main_hook():
    """Жалоба на недозвон — самая сильная зацепка, она должна быть видна."""
    text = tg.format_org(_org(review_complaints=["не дозвониться", "не перезвонили"]))
    assert "Проблемы в отзывах" in text
    assert "не дозвониться" in text


def test_no_complaints_is_said_plainly():
    text = tg.format_org(_org(review_complaints=[]))
    assert "жалоб на связь" in text


def test_missing_phone_is_not_silent():
    """Пустое место читается как «телефон есть, просто не влез»."""
    text = tg.format_org(_org(phone=[]))
    assert "не найден" in text


def test_individual_entrepreneur_is_marked():
    """У ИП телефон звонит прямо владельцу — это меняет сценарий звонка."""
    text = tg.format_org(_org(entity_type="ИП"))
    assert "ИП" in text


def test_only_first_complaints_are_shown():
    """Десяток цитат превращает карточку в простыню."""
    text = tg.format_org(_org(review_complaints=[f"жалоба {i}" for i in range(10)]))
    assert text.count("• ") <= 3


# --- Карточка юрлица по ИНН ----------------------------------------------------


def test_company_card_shows_status_and_name():
    text = tg.format_company_card({
        "name": {"full_with_opf": "ООО «Ромашка»"},
        "state": {"status": "ACTIVE"},
        "inn": "7701234567",
    })
    assert "Ромашка" in text
    assert "Действует" in text


def test_company_card_survives_empty_data():
    """DaData может не отдать ничего — карточка всё равно не должна падать."""
    assert tg.format_company_card({})


@pytest.mark.parametrize(
    ("ms", "expected"),
    [(0, "01.01.1970"), ("не число", ""), (None, "")],
)
def test_date_from_ms(ms, expected):
    assert tg._date_from_ms(ms) == expected


# --- Клавиатура ----------------------------------------------------------------


def test_keyboard_shape():
    kb = tg.keyboard([[("Кнопка", "act:1")]])
    assert kb["inline_keyboard"][0][0]["text"] == "Кнопка"
    assert kb["inline_keyboard"][0][0]["callback_data"] == "act:1"


# --- Секреты не уходят человеку в чат ------------------------------------------


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Cannot connect to proxy http://user:REDACTED@proxy.example:12576", "REDACTED"),
        ("net::ERR_TUNNEL_CONNECTION_FAILED at http://user:pass@proxy:8080", "pass@"),
        ("https://login:VeryS3cret@host/path не отвечает", "VeryS3cret"),
    ],
)
def test_proxy_password_never_reaches_the_chat(raw, secret):
    """Прокси пишет в текст исключения полный URL с логином и паролем.

    Сообщение об ошибке уходит в телеграм как есть — один сбой, и доступ к
    прокси лежит в переписке навсегда, включая пересланные сообщения.
    """
    cleaned = tg.safe_text(raw)
    assert secret not in cleaned
    assert "***" in cleaned


def test_host_stays_readable():
    """Пароль убираем, адрес оставляем: без хоста ошибка перестаёт помогать."""
    cleaned = tg.safe_text("proxy http://user:pass@p12576.ltespace.net:12576 недоступен")
    assert "p12576.ltespace.net:12576" in cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "ключ sk-ant-api03-AAAAAAAAAAAAAAAAAAAA не принят",
        "токен 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw отозван",
        "хэш d41d8cd98f00b204e9800998ecf8427e",
    ],
)
def test_keys_and_tokens_are_removed(raw):
    assert "***" in tg.safe_text(raw)


def test_ordinary_error_stays_readable():
    """Обычная ошибка должна остаться читаемой, иначе она бесполезна."""
    text = "Сайт не открылся: таймаут 30 секунд"
    assert tg.safe_text(text) == text


def test_none_does_not_crash():
    assert tg.safe_text(None) == ""


def test_a_broken_tag_does_not_swallow_the_lead():
    """Название с угловой скобкой не должно уносить карточку лида целиком.

    Telegram на неизвестный или незакрытый тег отвечает ошибкой и НЕ показывает
    сообщение вовсе. Названия приходят с чужих карточек, и скобка в них —
    вопрос времени. Повтор простым текстом теряет выделения, но не телефон.
    """
    import asyncio

    from parser import telegram as tg

    seen: list[dict] = []

    class Reply:
        def __init__(self, ok, description=""):
            self.status_code = 200 if ok else 400
            self._ok = ok
            self._description = description

        def json(self):
            return {"ok": True} if self._ok else {"ok": False, "description": self._description}

    class Client:
        async def post(self, url, json=None):
            seen.append(json)
            if len(seen) == 1:
                return Reply(False, 'Bad Request: unsupported start tag "ооо"')
            return Reply(True)

    async def scenario():
        return await tg.post(Client(), "token", "sendMessage", {
            "chat_id": 1, "text": "<ооо>Рога и Копыта</ооо> +7 900 000-00-00",
            "parse_mode": "HTML",
        })

    asyncio.run(scenario())
    assert len(seen) == 2, "повтора без разметки не было"
    assert seen[1]["parse_mode"] is None
    assert "+7 900 000-00-00" in seen[1]["text"]

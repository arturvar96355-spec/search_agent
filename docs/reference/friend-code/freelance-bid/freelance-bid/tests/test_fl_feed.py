"""Тесты разбора ленты FL.ru и парсинга бюджета — на зафиксированном образце XML."""
from __future__ import annotations

from fbid.models import Source, parse_budget, strip_budget_suffix
from fbid.sources.fl import FlSource

SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <item>
    <title><![CDATA[Оформить витрину на озон (Бюджет: 50 000  &#8381;)]]></title>
    <link>https://www.fl.ru/projects/5516591/oformit-vitrinu-na-ozon.html</link>
    <description><![CDATA[ТЗ: Оформить витрину магазина Ozon<br/>Сегмент — книги. Что...]]></description>
    <category><![CDATA[Дизайн / Дизайн для маркетплейсов]]></category>
    <pubDate>Tue, 04 Aug 2026 20:10:06 GMT</pubDate>
  </item>
  <item>
    <title><![CDATA[Телеграм-бот для записи клиентов]]></title>
    <link>https://www.fl.ru/projects/5516700/telegram-bot.html</link>
    <description><![CDATA[Нужен бот с записью и напоминаниями.]]></description>
    <category><![CDATA[Программирование / Веб-программирование]]></category>
    <pubDate>Wed, 05 Aug 2026 08:20:03 GMT</pubDate>
  </item>
  <item>
    <title><![CDATA[Мусорный элемент без ссылки на проект]]></title>
    <link>https://www.fl.ru/users/someone/</link>
    <description><![CDATA[не заказ]]></description>
  </item>
</channel></rss>"""


def test_parse_feed_extracts_orders():
    orders = FlSource._parse_feed(SAMPLE)
    # Третий элемент без /projects/<id>/ отбрасывается.
    assert len(orders) == 2

    first = orders[0]
    assert first.source is Source.FL
    assert first.ext_id == "5516591"
    assert first.title == "Оформить витрину на озон"
    assert first.budget_value == 50000
    assert first.category == "Дизайн / Дизайн для маркетплейсов"
    assert first.published_at is not None
    assert "витрину магазина Ozon" in first.description
    assert first.description_full is False


def test_order_without_budget_keeps_none():
    """Бюджет не указан — это None, а не 0: иначе фильтр «от N ₽» съест переговорные заказы."""
    orders = FlSource._parse_feed(SAMPLE)
    assert orders[1].budget_value is None
    assert orders[1].ext_id == "5516700"


def test_parse_budget_variants():
    assert parse_budget("Задача (Бюджет: 50 000  ₽)")[1] == 50000
    assert parse_budget("Задача (Бюджет: 1500 руб.)")[1] == 1500
    assert parse_budget("Задача без бюджета")[1] is None
    assert parse_budget("")[1] is None


def test_strip_budget_suffix():
    assert strip_budget_suffix("Лендинг (Бюджет: 20 000 ₽)") == "Лендинг"
    assert strip_budget_suffix("Лендинг") == "Лендинг"


def test_broken_xml_returns_empty():
    assert FlSource._parse_feed("<rss><channel><item>") == []


# --- Обрыв туннеля при отправке ---------------------------------------------

class FlakyPage:
    """Страница, которая открывается только с N-й попытки.

    Ровно так ведёт себя мобильный прокси: каждые пять минут он меняет IP,
    и навигация в этот момент падает по таймауту.
    """

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    def goto(self, url, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("Page.goto: Timeout 30000ms exceeded")
        return None

    def wait_for_timeout(self, _ms):
        return None


def test_page_opens_after_a_stumble():
    """Один обрыв не должен стоить нам заказа."""
    from fbid.sources.fl import _open_with_retries

    page = FlakyPage(fail_times=1)
    _open_with_retries(page, "https://www.fl.ru/projects/1/")
    assert page.attempts == 2


def test_two_stumbles_still_recovered():
    from fbid.sources.fl import NAV_ATTEMPTS, _open_with_retries

    page = FlakyPage(fail_times=NAV_ATTEMPTS - 1)
    _open_with_retries(page, "https://www.fl.ru/projects/1/")
    assert page.attempts == NAV_ATTEMPTS


def test_dead_connection_raises_after_all_attempts():
    """Сеть лежит совсем — падаем явно, а не делаем вид, что открыли."""
    import pytest as _pytest

    from fbid.sources.fl import NAV_ATTEMPTS, _open_with_retries

    page = FlakyPage(fail_times=99)
    with _pytest.raises(RuntimeError, match="Timeout"):
        _open_with_retries(page, "https://www.fl.ru/projects/1/")
    assert page.attempts == NAV_ATTEMPTS

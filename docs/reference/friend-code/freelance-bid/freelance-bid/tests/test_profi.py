"""Тесты Profi.ru. Образцы текста сняты с живого кабинета 05.08.2026 (имена заменены)."""
from __future__ import annotations

import pytest

from fbid.sources.profi import (
    ProfiSource,
    no_price_reason,
    parse_balance,
    parse_bid_price,
    parse_card_text,
    parse_client_info,
)

GUEST_PAGE = """<html><head><title>Вход на Профи.ру</title></head>
<body><input data-testid="auth_login_input"/>
<button data-testid="enter_with_sms_btn">Войти по SMS</button></body></html>"""

LOGGED_IN_PAGE = """<html><head><title>Заказы</title></head><body>Ваши заказы</body></html>"""

# Вёрстка №1: «Цена отклика … руб.» + кнопка «Откликнуться за … руб.»
ORDER_PAGE_A = """Подбор и создание контента, составление контент-плана.
Платформа сайта: Instagram.
Постоянная работа (полная занятость).
Дистанционно
Москва (МСК+0)
Когда
можно предложить свои даты
Цена отклика
1 руб.
Заказ № 92388628
Заказ оставлен 46 минут назад
Заказчик
В сети 45 минут назад
Возможно, вакансия
На Профи.ру с 13 ноября 2021
Оставил19 отзывов
Подтвердилномер
В этом заказе ваш отклик будет 1-м по рейтингу
Откликнуться за 1 руб."""

# Вёрстка №2: «Отклик • 42 ₽» в блоке тарифа
ORDER_PAGE_B = """Веб-разработка.
Разработка с нуля.
Ищем педагога на проф дисциплины - почасовка (совмещение).
Дистанционно
Кемерово (МСК+4)
Когда
начать: 1 сент. 2026 г.
Заказ № 92365327
Заказ оставлен 9 часов назад
Отзывов от специалистов пока нет
Подтвердиланомер
В этом заказе ваш отклик будет 4-м по рейтингу
Тариф
Отклик • 42 ₽
Продолжить"""

CARD_TEXT = (
    "Программист\nдо 1000 ₽ false до1000 ₽\n\n"
    "Веб-разработка · Разработка с нуля · Ищем педагога на проф дисциплины\n\n"
    "Дистанционно · Кемерово \xa0+4\xa0часа\n1 сен. (Вт) - 30 июн. 2027 (Ср)\n"
    "Заказчик\n8 часов назад"
)


# --- Сессия ---------------------------------------------------------------

def test_guest_page_detected_as_logged_out():
    """Ложное «залогинен» опаснее ложного «слетела»: софт решит, что может откликаться."""
    assert ProfiSource.is_logged_in(GUEST_PAGE) is False


def test_backoffice_page_detected_as_logged_in():
    assert ProfiSource.is_logged_in(LOGGED_IN_PAGE) is True


# --- Цена отклика: на ней держится лимит 300 ₽ -----------------------------

def test_bid_price_layout_a():
    assert parse_bid_price(ORDER_PAGE_A) == 1


def test_bid_price_layout_b():
    assert parse_bid_price(ORDER_PAGE_B) == 42


def test_bid_price_with_narrow_space():
    """Profi.ru печатает числа с узким неразрывным пробелом — обычный \\s его не ловит."""
    assert parse_bid_price("Отклик • 1 250 ₽") == 1250


def test_bid_price_absent_returns_none():
    """Не нашли цену — значит откликаться нельзя. None, а не 0."""
    assert parse_bid_price("Заказ № 1\nОписание без цены") is None


def test_bid_price_takes_max_when_layouts_disagree():
    """Расхождение вёрсток трактуем в сторону большей суммы — чтобы не списать лишнего."""
    assert parse_bid_price("Цена отклика 50 руб.\nОткликнуться за 300 руб.") == 300


# --- Почему цены нет: три разных случая ------------------------------------
# Тексты сняты с живых страниц кабинета 07.08.2026.

HIDDEN_PAGE = (
    "Заказы Чаты 63 ₽ Анкета Поддержка\n"
    "Фирменный бланк\n"
    "Заказ № 92462271\n"
    "Заказ оставлен 4 часа назад\n"
    "Е ЕВГЕНИЯ В сети 2 часа назад\n"
    "Заказ скрыт — клиент пока не готов получать новые отклики.\n"
    "Профи.ру для специалистов\n"
)

GONE_PAGE = (
    "Заказы Чаты 63 ₽ Анкета Поддержка\n"
    "Профи.ру для специалистов\n"
    "Скачайте приложение — с ним ещё удобнее\n"
    "Чат с поддержкой 8 (800) 555-14-12\n"
)


def test_hidden_order_names_its_reason():
    """Клиент закрыл приём откликов — это не поломка разбора."""
    assert parse_bid_price(HIDDEN_PAGE) is None
    assert "скрыт" in no_price_reason(HIDDEN_PAGE)


def test_removed_order_names_its_reason():
    """Снятый заказ отдаёт голую шапку кабинета — цены там и не должно быть."""
    assert parse_bid_price(GONE_PAGE) is None
    assert "снят" in no_price_reason(GONE_PAGE)


def test_normal_page_has_no_excuse():
    """На обычной странице заказа причина «нет цены» не выдумывается.

    Если тут вернуть причину, фильтр похоронит заказ навсегда вместо того,
    чтобы попробовать ещё раз.
    """
    assert no_price_reason(ORDER_PAGE_B) == ""


def test_page_without_price_but_with_order_is_suspicious():
    """Заказ на месте, а цены нет — это уже повод посмотреть, что сломалось."""
    text = "Заказ № 92468984\n" + "Описание заказа. " * 40
    assert no_price_reason(text) == ""


# --- Карточка из списка ----------------------------------------------------

def test_card_text_parsed():
    card = parse_card_text(CARD_TEXT)
    assert card.category == "Программист"
    assert "1000" in card.budget_raw
    assert "педагога" in card.description
    assert card.location.startswith("Дистанционно")
    assert card.posted_ago == "8 часов назад"


def test_card_budget_is_not_bid_price():
    """«до 1000 ₽» в списке — бюджет заказчика; отклик по этому заказу стоит 42 ₽."""
    card = parse_card_text(CARD_TEXT)
    assert parse_bid_price(ORDER_PAGE_B) == 42
    assert "1000" in card.budget_raw


# --- Данные о заказчике ----------------------------------------------------

def test_client_info_layout_a():
    info = parse_client_info(ORDER_PAGE_A)
    assert info.reviews == 19
    assert info.phone_confirmed is True
    assert info.is_vacancy is True
    assert info.bid_rank == 1


def test_client_info_layout_b():
    info = parse_client_info(ORDER_PAGE_B)
    assert info.reviews == 0
    assert info.is_vacancy is False
    assert info.bid_rank == 4


# --- Мелочи ----------------------------------------------------------------

def test_parse_balance():
    assert parse_balance("15 ₽") == 15
    assert parse_balance("1 500 ₽") == 1500
    assert parse_balance("нет данных") is None


# --- Лимит сообщения на Profi.ru (500 символов) ---------------------------

def test_trim_message_cuts_on_sentence_boundary():
    """Заход длиннее лимита режется по границе предложения, а не на полуслове."""
    from fbid.sources.profi import MESSAGE_LIMIT, trim_message

    long_text = ("Здравствуйте. " + "Делаем такие задачи регулярно и быстро. " * 30).strip()
    out = trim_message(long_text)
    assert len(out) <= MESSAGE_LIMIT
    assert out.endswith(".")
    assert long_text.startswith(out)


def test_trim_message_keeps_short_text():
    from fbid.sources.profi import trim_message

    assert trim_message("Короткий заход.") == "Короткий заход."


def test_trim_message_never_exceeds_limit_without_punctuation():
    """Текст без точек тоже обязан уложиться в лимит."""
    from fbid.sources.profi import MESSAGE_LIMIT, trim_message

    out = trim_message("слово " * 300)
    assert len(out) <= MESSAGE_LIMIT


# --- Шапка кабинета не должна попадать в ТЗ --------------------------------

PAGE_WITH_CHROME = """Заказы
Чаты
515 ₽
Анкета
Поддержка
Редизайн корпоративного сайта на WordPress
до 50 000 ₽
Описание
Нужен опытный WordPress-разработчик.
Контент есть.
Когда
Сегодня
"""


def test_task_text_drops_backoffice_header():
    """В шапке кабинета висит баланс счёта — он уезжал в промт модели и в Telegram."""
    from fbid.sources.profi import _extract_task_text

    text = _extract_task_text(PAGE_WITH_CHROME)
    assert "515 ₽" not in text
    assert "Поддержка" not in text
    assert "Чаты" not in text
    assert text.startswith("Нужен опытный WordPress-разработчик")


def test_task_text_drops_page_tail():
    from fbid.sources.profi import _extract_task_text

    assert "Сегодня" not in _extract_task_text(PAGE_WITH_CHROME)


def test_task_text_survives_page_without_description_marker():
    """Нет слова «Описание» — текст всё равно должен остаться, просто без меню."""
    from fbid.sources.profi import _extract_task_text

    text = _extract_task_text("Заказы\nЧаты\n469 ₽\nНужен телеграм-бот для записи\n")
    assert "Нужен телеграм-бот для записи" in text
    assert "469 ₽" not in text


# --- Обрыв туннеля при навигации --------------------------------------------

class FlakyPage:
    """Страница, которая открывается только с N-й попытки.

    Мобильный прокси меняет IP каждые пять минут, и навигация в этот момент
    падает по таймауту. Раньше это роняло весь проход по доске: заказы,
    появившиеся за эти минуты, мы просто не видели.
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


def test_board_opens_after_a_stumble():
    from fbid.sources.profi import ORDERS_URL, _goto

    page = FlakyPage(fail_times=1)
    _goto(page, ORDERS_URL)
    assert page.attempts == 2


def test_navigation_gives_up_loudly():
    """Сеть лежит совсем — падаем явно, а не делаем вид, что открыли."""
    import pytest as _pytest

    from fbid.sources.profi import NAV_ATTEMPTS, ORDERS_URL, _goto

    page = FlakyPage(fail_times=99)
    with _pytest.raises(RuntimeError, match="Timeout"):
        _goto(page, ORDERS_URL)
    assert page.attempts == NAV_ATTEMPTS


def test_navigation_timeout_is_longer_than_default():
    """Тридцати секунд по умолчанию мобильному прокси не хватает."""
    from fbid.sources.profi import NAV_TIMEOUT_MS

    assert NAV_TIMEOUT_MS > 30000


# --- Диагностика нечитаемой цены --------------------------------------------

def test_price_context_shows_what_broke():
    """В лог должно попадать то место страницы, которое не разобралось."""
    from fbid.sources.profi import _price_context

    text = "Детали заказа\nЦена отклика   650 крон\nОткликнуться"
    assert "650 крон" in _price_context(text)


def test_price_context_empty_when_no_mention():
    from fbid.sources.profi import _price_context

    assert _price_context("Обычная страница без единого слова про стоимость") == ""


# --- Отправка сообщения клиенту: подтверждаем по переписке ---------------------

class _ProfiPage:
    """Страница заказа Profi: переписка начинается с «Детали заказа»."""

    def __init__(self, *, delivers: bool = True) -> None:
        self.delivers = delivers
        self.thread = "Детали заказа\nНужен сайт\n"
        self.value = ""
        self.pressed: list[str] = []

    # то, что зовёт боевой код
    def goto(self, _url, **_kw):
        pass

    def content(self):
        return "<html>Выйти</html>"

    def wait_for_timeout(self, _ms):
        pass

    def query_selector(self, selector):
        # Отвечаем ровно на тот селектор, которым код ищет переписку.
        from fbid.sources.profi import ProfiSource

        return self if selector in ProfiSource.CHAT_FIELDS else None

    def query_selector_all(self, _selector):
        return []

    def inner_text(self, _selector="body"):
        return self.thread

    # поведение поля ввода
    def fill(self, text):
        self.value = text

    def press(self, key):
        self.pressed.append(key)
        if self.delivers:
            self.thread += "\n" + self.value
        self.value = ""

    def input_value(self):
        return self.value


class _ProfiContext:
    def __init__(self, page):
        self.pages = [page]

    def new_page(self):  # pragma: no cover — страница всегда есть
        return self.pages[0]


def _profi_order():
    from fbid.models import Order, Source

    return Order(
        source=Source.PROFI, ext_id="900",
        url="https://profi.ru/backoffice/n.php?o=900", title="Сайт",
    )


def test_profi_message_is_confirmed_by_the_conversation(monkeypatch):
    from fbid.sources.profi import ProfiSource

    page = _ProfiPage()
    source = ProfiSource()
    monkeypatch.setattr(source, "_find_send_button", lambda _page: None)

    assert source.send_message(_ProfiContext(page), _profi_order(), "Здравствуйте, по срокам")
    assert page.pressed == ["Enter"]


def test_profi_empty_field_alone_is_not_proof(monkeypatch):
    """Поле опустело, а сообщения в переписке нет — это НЕ отправка.

    Раньше именно пустое поле считалось доказательством, и владельцу уходило
    «ответ отправлен» в случаях, когда о судьбе сообщения известно ровно ничего.
    """
    from fbid.sources.profi import ProfiSource

    page = _ProfiPage(delivers=False)
    source = ProfiSource()
    monkeypatch.setattr(source, "_find_send_button", lambda _page: None)

    assert source.send_message(_ProfiContext(page), _profi_order(), "Здравствуйте, по срокам") is False


# --- Сумма разбирается как число, а не как «все цифры подряд» ------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("до 5000 ₽", 5000.0),
        ("до 50 000 ₽", 50000.0),
        ("до 40 000 000 ₽", 40000000.0),
        ("от 5 000 до 20 000 ₽", 20000.0),
        ("до 2 000 ₽ до 2 000 ₽", 2000.0),
        ("договорная", None),
        ("", None),
        (None, None),
    ],
)
def test_amount_is_parsed_as_a_number(raw, expected):
    """Две суммы в одной подписи не должны склеиваться в одну гигантскую.

    Прежний разбор выбрасывал из строки всё нецифровое и склеивал остаток:
    «от 5 000 до 20 000 ₽» превращалось в 500020000. По бюджету считается и
    порог автоответа, и рейтинг, и фильтр по минимальной сумме.
    """
    from fbid.sources.profi import _to_number

    assert _to_number(raw) == expected


def test_profi_chat_field_is_named_not_guessed():
    """«Первая textarea» — та самая догадка, что на FL оказалась личной заметкой.

    Класс у Profi сгенерированный и меняется от сборки к сборке, поэтому
    держимся за подсказку поля.
    """
    from fbid.sources.profi import ProfiSource

    assert ProfiSource.CHAT_FIELDS
    assert all("placeholder" in selector for selector in ProfiSource.CHAT_FIELDS)


def test_profi_without_the_chat_field_does_not_send(monkeypatch):
    """Поля переписки нет — значит не отправили, и врать об этом нельзя."""
    from fbid.sources.profi import ProfiSource

    page = _ProfiPage()
    monkeypatch.setattr(page, "query_selector", lambda _selector: None)
    source = ProfiSource()

    assert source.send_message(_ProfiContext(page), _profi_order(), "Здравствуйте") is False
    assert page.value == "", "текст не должен попасть ни в какое другое поле"


# --- Вилку цены не выдумываем --------------------------------------------------


class _PriceInput:
    def __init__(self):
        self.value = None

    def is_visible(self):
        return True

    def fill(self, value):
        self.value = value


class _PricePage:
    def __init__(self):
        self.inputs = [_PriceInput(), _PriceInput()]

    def query_selector_all(self, _selector):
        return self.inputs

    def wait_for_timeout(self, _ms):
        pass


def test_a_single_price_is_not_inflated_into_a_range():
    """Модель дала одну цену — потолок выдумывать нельзя.

    Здесь стояло `price_from * 1.6`, и клиент видел под нашим же откликом
    строку «Стоимость от 50 000 до 80 000 руб.» — потолок, которого в тексте
    отклика не было ни разу. Число из анкеты клиент читает наравне с текстом,
    и расхождение выглядит как попытка накинуть сверху.
    """
    from fbid.sources.profi import ProfiSource

    page = _PricePage()
    assert ProfiSource._fill_price_range(page, 50000, 0)
    assert [i.value for i in page.inputs] == ["50000", "50000"]


def test_a_real_range_from_the_model_is_kept():
    """А настоящую вилку от модели ставим как есть."""
    from fbid.sources.profi import ProfiSource

    page = _PricePage()
    assert ProfiSource._fill_price_range(page, 50000, 80000)
    assert [i.value for i in page.inputs] == ["50000", "80000"]


def test_no_price_means_no_filling():
    """Цены нет вовсе — поля не трогаем и говорим об этом вызывающему."""
    from fbid.sources.profi import ProfiSource

    page = _PricePage()
    assert not ProfiSource._fill_price_range(page, 0, 0)
    assert [i.value for i in page.inputs] == [None, None]


# --- Возраст заказа на Profi ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hours_ago"),
    [
        ("5 минут назад", 5 / 60),
        ("45 минут назад", 45 / 60),
        ("1 минуту назад", 1 / 60),
        ("1 час назад", 1),
        ("2 часа назад", 2),
        # Число раньше словесной формы: «21 час назад» содержит «час назад»,
        # и словесная форма съедала число — суточный заказ выглядел часовым.
        ("21 час назад", 21),
        ("15 часов назад", 15),
        ("3 дня назад", 72),
        ("вчера", 24),
        ("позавчера", 48),
        ("неделю назад", 168),
        ("2 недели назад", 336),
        ("только что", 0),
    ],
)
def test_posted_ago_is_turned_into_a_date(text, hours_ago):
    """Своей даты публикации Profi не даёт — считаем её из «2 часа назад».

    Без разбора `published_at` оставался пустым, и правило max_age_hours не
    действовало на площадке вовсе: при пустой дате заказ считается свежим. Мы
    платили за отклики на заказы недельной давности, давно разобранные.
    """
    from datetime import datetime, timezone

    from fbid.sources.profi import parse_posted_ago

    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    got = parse_posted_ago(text, now=now)
    assert got is not None, text
    assert abs((now - got).total_seconds() / 3600 - hours_ago) < 0.01


@pytest.mark.parametrize("text", ["", "какая-то ерунда", None])
def test_an_unreadable_age_is_not_invented(text):
    """Не разобрали — так и говорим. Выдумывать дату нельзя."""
    from fbid.sources.profi import parse_posted_ago

    assert parse_posted_ago(text) is None


def test_an_old_profi_order_is_filtered_out():
    """Заказ трёхдневной давности не должен проходить правило по возрасту."""
    from datetime import datetime, timezone

    from fbid.filters import CommonRules, FilterSet, SourceRules
    from fbid.models import Order, Source
    from fbid.sources.profi import parse_posted_ago

    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    filters = FilterSet(
        CommonRules(max_age_hours=24),
        {"profi": SourceRules(categories_allow=["Сайты"])},
    )
    order = Order(
        source=Source.PROFI, ext_id="1", url="https://profi.ru/1",
        title="Сайты", category="Сайты", description="Нужен сайт-каталог",
        published_at=parse_posted_ago("3 дня назад", now=now),
    )
    decision = filters.decide(order, now=now)
    assert not decision.passed
    assert "лимит" in decision.reason


def test_the_board_card_carries_its_publication_date():
    """Заказ с доски обязан приехать с датой публикации.

    Она считается из строки «2 часа назад» — своей даты Profi не даёт. Пока
    `published_at` оставался пустым, правило max_age_hours на площадке не
    действовало: при пустой дате заказ считается свежим.
    """
    from datetime import datetime, timezone

    from fbid.sources.profi import order_from_card, parse_card_text

    card = parse_card_text(
        "Создание сайтов\n"
        "до 60 000 ₽\n"
        "Нужен сайт-каталог с оплатой\n"
        "Дистанционно\n"
        "2 часа назад"
    )
    order = order_from_card("123456", card)

    assert order.published_at is not None, "дата обязана быть"
    age = (datetime.now(timezone.utc) - order.published_at).total_seconds() / 3600
    assert 1.9 < age < 2.1, age
    assert order.category == "Создание сайтов"
    assert order.budget_value == 60000


def test_a_card_without_an_age_line_says_so():
    """Строки давности нет — дату не выдумываем."""
    from fbid.sources.profi import order_from_card, parse_card_text

    order = order_from_card("1", parse_card_text("Создание сайтов\nНужен сайт"))
    assert order.published_at is None


# --- «Не дождались» ≠ «заказ снят» ---------------------------------------------


def test_an_empty_page_is_not_a_removed_order():
    """Страница не отрисовалась — приговор выносить не по чему.

    Приговор «снят» отменяет цикл повторов и хоронит заказ навсегда. Через
    мобильный прокси страница недогружается регулярно, и живой заказ терялся
    из-за одной медленной загрузки.
    """
    from fbid.sources.profi import no_price_reason

    assert no_price_reason("") == ""
    assert no_price_reason("   \n  ") == ""
    assert no_price_reason("Загрузка…") == ""


def test_a_really_removed_order_is_still_recognised():
    """А снятый заказ отдаёт шапку кабинета без самого заказа — это приговор."""
    from fbid.sources.profi import GONE_TEXT_LIMIT, LOADED_TEXT_MIN, no_price_reason

    chrome = (
        "Profi.ru\nМои заказы\nСообщения\nНастройки\nПомощь\nВыйти\n"
        "О компании\nПользовательское соглашение\nПоддержка\n"
        "Служба поддержки\nБезопасная сделка\nКак это работает\n"
        "Стать специалистом\nВакансии\nПресса о нас\n"
    )
    assert LOADED_TEXT_MIN <= len(chrome.strip()) < GONE_TEXT_LIMIT, len(chrome.strip())
    assert "снят" in no_price_reason(chrome)


def test_a_hidden_order_is_recognised_even_on_a_short_page():
    """«Заказ скрыт» — это ответ, а не поломка, на любой длине страницы."""
    from fbid.sources.profi import no_price_reason

    assert "скрыт" in no_price_reason("Заказ скрыт")


# --- Сообщение клиенту режем до отправки ---------------------------------------


def test_a_long_message_is_trimmed_before_sending():
    """Поле площадки обрезает лишнее молча, а подтверждение смотрит начало.

    Обрезанное на полуслове сообщение засчитывалось как отправленное целиком,
    а клиент получал оборванную фразу.
    """
    from fbid.models import Order, Source
    from fbid.sources.profi import MESSAGE_LIMIT, ProfiSource

    filled: list[str] = []

    class Field:
        def fill(self, value):
            filled.append(value)

        def press(self, _key):
            pass

    class Page:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html>Мои заказы</html>"

        def query_selector(self, _sel):
            return None

        def query_selector_all(self, _sel):
            return []

    class Context:
        def __init__(self):
            self.pages = [Page()]

    source = ProfiSource()
    source._chat_field = lambda page: Field()
    source._find_send_button = lambda page: None
    source._message_is_in_the_thread = lambda page, order, text: True

    order = Order(source=Source.PROFI, ext_id="1", url="https://profi.ru/1", title="Заказ")
    long_text = "Здравствуйте! " + "Очень длинное предложение про задачу. " * 40

    assert source.send_message(Context(), order, long_text)
    assert filled and len(filled[0]) <= MESSAGE_LIMIT, len(filled[0])
    assert filled[0].endswith((".", "?", "!")), filled[0][-40:]

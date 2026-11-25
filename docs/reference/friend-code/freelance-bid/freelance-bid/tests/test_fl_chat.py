"""Отправка ответа клиенту на FL.ru: она обязана быть честной.

Здесь проверяется ровно одно свойство, но самое дорогое: функция не имеет
права сказать «отправлено», если сообщения в переписке нет.

Почему это отдельный файл с такими подробными проверками. На странице заказа
FL есть только одна textarea — «#noteTextarea», личная заметка, которую видим
мы одни. Отправка целилась в неё: текст ложился в заметку, функция возвращала
True после клика, владельцу приходила карточка «ответ ушёл клиенту». Клиент
при этом не получал ничего и молчал — и молчание выглядело как его выбор.
"""
from __future__ import annotations

import pytest

from fbid.models import Order, Source
from fbid.sources.fl import FlSource


def order() -> Order:
    return Order(
        source=Source.FL, ext_id="5517000",
        url="https://www.fl.ru/projects/5517000/lichnyiy-kabinet.html",
        title="Личный кабинет",
    )


class FakeField:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.value = ""

    def fill(self, text: str) -> None:
        self.value = text

    def press(self, key: str) -> None:
        self.page.pressed.append(key)
        if self.page.delivers:
            self.page.thread += " " + self.value


class FakePage:
    """Страница заказа: чат открывается кнопкой, отправка — клавишей."""

    def __init__(self, *, has_button: bool = True, delivers: bool = True) -> None:
        self.has_button = has_button
        self.delivers = delivers
        self.chat_open = False
        self.thread = "07 августа"
        self.pressed: list[str] = []
        self.note = FakeField(self)  # личная заметка: сюда попадать нельзя

    # --- то, что вызывает боевой код ---
    def goto(self, _url: str, **_kwargs) -> None:
        pass

    def content(self) -> str:
        return "<html><body>Мои проекты" + "." * 3000 + "</body></html>"

    def wait_for_timeout(self, _ms) -> None:
        pass

    def query_selector(self, selector: str):
        if selector == FlSource.CHAT_FIELD:
            return self.field if self.chat_open else None
        if selector == FlSource.CHAT_OPEN:
            return _Opener(self) if self.has_button else None
        if selector == "#noteTextarea":
            return self.note
        return None

    def wait_for_selector(self, selector: str, timeout: float = 0):
        if selector == FlSource.CHAT_FIELD and self.chat_open:
            return self.field
        raise TimeoutError("окно не открылось")

    def inner_text(self, selector: str) -> str:
        if selector == FlSource.CHAT_THREAD:
            return self.thread
        return ""

    @property
    def field(self) -> FakeField:
        if not hasattr(self, "_field"):
            self._field = FakeField(self)
        return self._field


class _Opener:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def click(self) -> None:
        self.page.chat_open = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]

    def new_page(self):  # pragma: no cover — страница всегда есть
        return self.pages[0]


def test_message_goes_to_the_chat_and_is_confirmed():
    page = FakePage()
    sent = FlSource().send_message(FakeContext(page), order(), "Здравствуйте, по срокам так")

    assert sent is True
    assert page.chat_open, "чат обязан открыться: на странице заказа поля переписки нет"
    assert page.field.value == "Здравствуйте, по срокам так"
    assert page.pressed == ["Enter"], "под полем написано «Enter для отправки»"
    assert page.note.value == "", "личная заметка не должна быть тронута"


def test_silent_failure_is_reported_as_failure():
    """Нажали, а сообщения в ленте нет — это НЕ отправка."""
    page = FakePage(delivers=False)
    sent = FlSource().send_message(FakeContext(page), order(), "Здравствуйте, по срокам так")

    assert sent is False


def test_no_chat_button_means_not_sent():
    """Заказ ушёл в архив или отклик сняли — писать некуда, и врать нельзя."""
    page = FakePage(has_button=False)
    sent = FlSource().send_message(FakeContext(page), order(), "Здравствуйте")

    assert sent is False
    assert page.note.value == "", "текст не должен уходить в заметку «хоть куда-нибудь»"


def test_empty_text_is_never_sent():
    page = FakePage()
    assert FlSource().send_message(FakeContext(page), order(), "   ") is False
    assert page.chat_open is False


def test_private_note_field_is_not_the_chat():
    """Тревожная растяжка: «#noteTextarea» чатом не является и не станет."""
    assert FlSource.CHAT_FIELD != "#noteTextarea"
    assert "qa-ui-textarea-message-chat" in FlSource.CHAT_FIELD


# --- Слетевшая сессия должна называться своим именем ---------------------------

def test_lost_session_is_not_reported_as_an_unreadable_package():
    """Гостю страница платных услуг числа не покажет.

    «Остаток не прочитался» читается как временная заминка: отклики встают
    намертво, а владельца никто не зовёт войти заново — хотя без него это
    не чинится.
    """
    import pytest

    from fbid.sources.fl import NotAuthorized

    class GuestPage:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            # Как выглядит шапка FL у гостя: обе кнопки ссылками.
            # Длина правдоподобная — иначе адаптер решит «не отрисовалось».
            return (
                '<html><body><a href="/login/">Вход</a>'
                '<a href="/registration/">Регистрация</a>'
                + "." * 3000 + "</body></html>"
            )

        def inner_text(self, _selector):
            return "Купите отклики"

    class Context:
        def __init__(self):
            self.pages = [GuestPage()]

    with pytest.raises(NotAuthorized):
        FlSource().read_package(Context())


def test_a_live_session_reads_the_package():
    class Page:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html><body>Мои проекты" + "." * 3000 + "</body></html>"

        def inner_text(self, _selector):
            return "Отклики на заказы\nУ вас 7 откликов"

    class Context:
        def __init__(self):
            self.pages = [Page()]

    assert FlSource().read_package(Context()) == 7


# --- Слово «Регистрация» в тексте заказа — не выход из аккаунта ----------------


@pytest.mark.parametrize(
    ("name", "html"),
    [
        ("регистрация пользователей в ТЗ",
         "<html><h1>Мои проекты</h1><div>Нужна Регистрация пользователей на сайте</div></html>"),
        ("регистрация ООО в заголовке",
         "<html><h1>Мои проекты</h1><div>Регистрация ООО под ключ</div></html>"),
        ("слово «вход» в описании",
         "<html><h1>Мои проекты</h1><div>Вход в личный кабинет и Регистрация</div></html>"),
    ],
)
def test_an_order_about_registration_is_not_a_lost_session(name, html):
    """На бирже веб-разработки «регистрация» — половина заказов.

    Признаком гостя было это слово где угодно в html. Живая сессия объявлялась
    слетевшей, проход обрывался, а тревога съедала четырёхчасовое окно
    глушителя — настоящий выход из аккаунта в эти часы владельцу уже не
    показали бы.
    """
    assert FlSource.is_logged_in(html), name


def test_a_real_guest_header_is_still_caught():
    """А настоящая гостевая шапка по-прежнему распознаётся."""
    guest = (
        '<html><header><a href="/login/">Вход</a>'
        '<a href="/registration/">Регистрация</a></header></html>'
    )
    assert not FlSource.is_logged_in(guest)


# --- Не отрисовавшаяся страница не приговор -----------------------------------


def test_an_unrendered_order_page_is_not_a_verdict():
    """Страница не отрисовалась — судить по ней нельзя ни о чём.

    Ниже стоит приговор BidUnavailable, который хоронит заказ навсегда. Через
    мобильный прокси недогруз случается регулярно, и живой заказ терялся из-за
    одной медленной загрузки.
    """
    from fbid.sources.fl import BidUnavailable, FlSource

    class Page:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html><body></body></html>"

        def query_selector(self, _sel):
            return None

    class Context:
        def __init__(self):
            self.pages = [Page()]

    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
    try:
        result = FlSource().submit_bid(Context(), order, "Здравствуйте", days=5, client_price=30000)
    except BidUnavailable as exc:  # pragma: no cover — это и есть дефект
        raise AssertionError(f"заказ похоронен по неотрисованной странице: {exc}") from exc

    assert result.sent is False
    assert result.charged is False
    assert "не открылась" in result.reason


def test_a_rendered_page_is_judged_as_before(monkeypatch):
    """А отрисованная страница разбирается как раньше."""
    from fbid.sources.fl import BidUnavailable, FlSource

    class Page:
        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html><body>Мои проекты" + "." * 3000 + "</body></html>"

        def query_selector(self, _sel):
            return None

    class Context:
        def __init__(self):
            self.pages = [Page()]

    source = FlSource()
    monkeypatch.setattr(source, "read_package", lambda _context: 7)
    order = Order(source=Source.FL, ext_id="1", url="https://fl.ru/1", title="Сайт")
    with pytest.raises(BidUnavailable):
        source.submit_bid(Context(), order, "Здравствуйте", days=5, client_price=30000)


def test_the_package_is_not_judged_from_the_wrong_page():
    """«Купить отклики» встречается и в шапке — вывод «пакет пуст» по чужой
    странице остановил бы отклики на ровном месте."""
    from fbid.sources.fl import FlSource

    class Page:
        url = "https://www.fl.ru/projects/1/x.html"

        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html><body>Мои проекты" + "." * 3000 + "</body></html>"

        def inner_text(self, _sel):
            return "Отклики на заказы Купить отклики"

    class Context:
        def __init__(self):
            self.pages = [Page()]

    assert FlSource().read_package(Context()) is None, "не та страница — не судим"


def test_an_unrendered_paid_page_is_not_an_empty_package():
    """Ноль отсюда остановил бы все отклики до покупки — а покупать нечего."""
    from fbid.sources.fl import FlSource

    class Page:
        url = "https://www.fl.ru/payed/"

        def goto(self, *a, **kw):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def content(self):
            return "<html></html>"

        def inner_text(self, _sel):
            return ""

    class Context:
        def __init__(self):
            self.pages = [Page()]

    assert FlSource().read_package(Context()) is None


@pytest.mark.parametrize(
    ("name", "html"),
    [
        (
            "ссылка на вход внутри текста чужого заказа",
            '<html><body><div class="order">Настроить авторизацию в '
            '<a href="https://www.fl.ru/account/login/">настройках профиля</a></div>'
            '<a href="/account/logout/" data-action="logout">Выйти</a></body></html>',
        ),
        (
            "мобильная кнопка регистрации висит и у авторизованного",
            '<html><body><div class="d-lg-none">'
            '<a href="/account/registration/?providerType=1">Зарегистрироваться</a></div>'
            '<a href="/account/logout/" data-action="logout">Выйти</a></body></html>',
        ),
    ],
)
def test_logout_link_outweighs_guest_looking_markup(name, html):
    """Есть «Выйти из аккаунта» — значит вошли, что бы ещё ни лежало на странице.

    Оба куска сняты с живой главной FL под авторизованным аккаунтом: из-за них
    проверка говорила «сессия слетела» сразу после настоящего входа, и владелец
    заходил заново по кругу.
    """
    assert FlSource.is_logged_in(html), name

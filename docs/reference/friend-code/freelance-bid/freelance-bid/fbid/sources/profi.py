"""Profi.ru.

Открытой ленты здесь нет: заказы видны только авторизованному исполнителю,
поэтому работаем браузером с постоянным профилем.

Разметка снята с живого кабинета (05.08.2026):
  * список заказов — /backoffice/n.php, карточки с data-testid="<id>_order-snippet";
  * страница заказа — /backoffice/n.php?o=<id>;
  * баланс — в шапке кабинета.

ГЛАВНОЕ ПРО ДЕНЬГИ. В списке у заказа висит «до N ₽» — это НЕ цена отклика
(проверено: у заказа с «до 1000 ₽» отклик стоит 42 ₽). Настоящая цена есть только
на странице заказа, и подана она в двух разных вёрстках:
    «Цена отклика  1 руб.» / «Откликнуться за 1 руб.»
    «Отклик • 42 ₽»
Если цену распарсить не удалось — заказ пропускается. Правило fail-closed: лучше
упустить заказ, чем откликнуться за неизвестную сумму.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from ..models import BidUnavailable, Order, Source, SubmitResult

BASE_URL = "https://profi.ru"
BACKOFFICE_URL = "https://profi.ru/backoffice/"
ORDERS_URL = "https://profi.ru/backoffice/n.php"
ORDER_URL = "https://profi.ru/backoffice/n.php?o={order_id}"

PROBE_PAGES = {
    "backoffice": BACKOFFICE_URL,
    "orders": ORDERS_URL,
    "bill": "https://profi.ru/backoffice/bill.php",
}

# Маркеры гостевой страницы (сняты с живой формы входа): data-testid стабильнее классов.
GUEST_MARKERS = (
    'data-testid="auth_login_input"',
    'data-testid="enter_with_sms_btn"',
    "<title>Вход на Профи.ру</title>",
)

CARD_SELECTOR = '[data-testid$="_order-snippet"]'
BALANCE_SELECTOR = '[aria-label="Баланс"]'
# Сколько ждём отрисовки доски. Через мобильный прокси 4 сек не хватало.
CARD_WAIT_MS = 25000

# Блок тарифа на странице заказа: в нём и цена, и кликабельный элемент отправки.
TARIFF_SELECTOR = '[data-testid="orderCard/tariffs"]'
# Подписи элемента отправки, встречавшиеся в разных вёрстках.
SUBMIT_LABELS = ("Продолжить", "Откликнуться за", "Предложить услуги")
# Второй шаг: форма с текстом. Именно эта кнопка списывает деньги.
PAY_BUTTON = '[data-testid="payment_methods_form_pay_button"]'
# Поле сообщения на Profi.ru жёстко ограничено 500 символами.
MESSAGE_LIMIT = 500
FORM_WAIT_MS = 20000
# Попыток открыть страницу заказа: прокси рвёт туннель при ротации IP.
DETAILS_ATTEMPTS = 3
DETAILS_RETRY_PAUSE_MS = 5000
_CARD_ID_RE = re.compile(r"^(\d+)_order-snippet$")

# Пробел внутри чисел на Profi.ru — узкий неразрывный (U+202F), обычный \s его не ловит.
_DIGITS = r"(\d[\d\s  ]*)"

# Порядок не важен: собираем все совпадения и берём МАКСИМУМ — при расхождении
# вёрсток дороже ошибиться в свою пользу, чем списать больше ожидаемого.
_BID_PRICE_PATTERNS = (
    re.compile(rf"Цена отклика\s*{_DIGITS}\s*(?:руб|₽)", re.IGNORECASE),
    re.compile(rf"Откликнуться за\s*{_DIGITS}\s*(?:руб|₽)", re.IGNORECASE),
    re.compile(rf"Отклик\s*[•·]\s*{_DIGITS}\s*(?:руб|₽)", re.IGNORECASE),
)

_RANK_RE = re.compile(r"ваш отклик будет\s*(\d+)-м по рейтингу", re.IGNORECASE)
_REVIEWS_RE = re.compile(r"Оставил[а]?\s*(\d+)\s*отзыв", re.IGNORECASE)
_BALANCE_RE = re.compile(rf"{_DIGITS}\s*₽")

# Заказчик отметил «Возможно, вакансия» — это найм в штат, а не разовый заказ.
VACANCY_MARKER = "Возможно, вакансия"
PHONE_CONFIRMED_MARKER = "номер"


# Открытие страницы через мобильный прокси: он меняет IP каждые пять минут, и
# в этот момент навигация падает по таймауту. Одна попытка по тридцать секунд
# роняла весь проход по доске — заказы за эти минуты мы просто не видели.
NAV_ATTEMPTS = 3
NAV_TIMEOUT_MS = 45000
NAV_RETRY_PAUSE_MS = 4000


def _goto(page, url: str) -> None:
    """Открывает страницу, переживая обрыв туннеля при смене IP."""
    last_error: Exception | None = None
    for attempt in range(1, NAV_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return
        except Exception as exc:  # noqa: BLE001 — разберём после всех попыток
            last_error = exc
            logger.debug("[profi] попытка {} открыть {} не удалась: {}", attempt, url, exc)
            if attempt < NAV_ATTEMPTS:
                page.wait_for_timeout(NAV_RETRY_PAUSE_MS)
    raise last_error if last_error else RuntimeError(f"[profi] не открыл {url}")


class NotAuthorized(RuntimeError):
    """Сессия слетела — нужен `fbid login profi`."""


# Число с разделителями разрядов: обычный пробел, узкий и неразрывный.
_AMOUNT_RE = re.compile(r"\d[\d\s\u00a0\u202f]*")


def _to_number(raw: str | None) -> float | None:
    """Сумма из подписи вроде «до 50 000 ₽». None — числа нет.

    Разбираем ЧИСЛА, а не «все цифры подряд». Прежний вариант выбрасывал из
    строки всё нецифровое и склеивал остаток: стоит площадке показать в одной
    подписи две суммы («от 5 000 до 20 000 ₽») или продублировать цену в
    разметке — и вместо двадцати тысяч получается пятьсот миллионов. Это не
    просто кривая цифра в карточке: по бюджету считается и порог автоответа,
    и рейтинг заказа, и фильтр по минимальной сумме.

    Из нескольких чисел берём наибольшее: подписи Profi — это потолок («до N»),
    а у диапазона потолок сверху.
    """
    if not raw:
        return None
    found = [
        int(re.sub(r"[\s\u00a0\u202f]", "", match))
        for match in _AMOUNT_RE.findall(raw)
        if re.sub(r"[\s\u00a0\u202f]", "", match)
    ]
    return float(max(found)) if found else None


def trim_message(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Подгоняет заход под лимит площадки, обрезая по границе предложения.

    Profi.ru принимает не больше 500 символов. Обрыв на полуслове читается как
    сбой, поэтому режем по последней точке или вопросу.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if boundary > limit // 3:
        return cut[: boundary + 1].strip()
    return cut.rsplit(" ", 1)[0].strip()


def parse_balance(text: str) -> float | None:
    """«15 ₽» из шапки → 15.0."""
    match = _BALANCE_RE.search(text or "")
    return _to_number(match.group(1)) if match else None


# Слова, рядом с которыми Profi.ru пишет стоимость отклика. По ним достаём
# кусок страницы для лога, когда разбор не удался.
_PRICE_HINTS = ("цена отклика", "откликнуться", "стоимость отклика", "отклик за", "списываем")

# Почему цены может не быть по делу, а не из-за поломки. Проверено на живых
# заказах: оба случая раньше выглядели в логе как «не нашёл цену отклика»,
# и было не отличить сломанный разбор от заказа, на который нельзя откликнуться.
HIDDEN_MARKER = "Заказ скрыт"
# Снятый заказ отдаёт почти пустую страницу: только шапка кабинета и подвал.
GONE_TEXT_LIMIT = 400
ORDER_MARKER = "Заказ №"
# А НЕДОГРУЖЕННАЯ страница пуста совсем: ни шапки, ни подвала. Разница между
# ними и есть разница между «заказ снят» и «мы не дождались». Приговор «снят»
# отменяет цикл повторов и хоронит заказ навсегда — выносить его по не
# отрисовавшейся странице нельзя. Порог снят с живых страниц: шапка кабинета
# со ссылками даёт больше двухсот знаков даже у снятого заказа.
LOADED_TEXT_MIN = 120

# Сколько ждём блок тарифа. Он дорисовывается позже остального: раньше мы
# читали страницу через фиксированные 3,5 секунды и на живых заказах с ценой
# 42 ₽ и 120 ₽ получали «цены нет» — заказы уходили в отказ ни за что.
TARIFF_WAIT_MS = 15000


def no_price_reason(page_text: str) -> str:
    """Почему на странице нет цены. Пусто — значит причина неизвестна.

    Отличать эти случаи важно: скрытый и снятый заказ — это «нам тут делать
    нечего», а неизвестная причина — повод посмотреть, не поехала ли вёрстка,
    и повод зайти ещё раз.

    Совсем пустая страница — это «не дождались», а не «снят». Через мобильный
    прокси она недогружается регулярно, а приговор «снят» отменяет цикл
    повторов: живой заказ хоронился навсегда из-за одной медленной загрузки.
    """
    text = (page_text or "").strip()
    if HIDDEN_MARKER in text:
        return "заказ скрыт — клиент пока не принимает отклики"
    if len(text) < LOADED_TEXT_MIN:
        # Страница не отрисовалась вовсе. Это НЕ «заказ снят»: причина
        # неизвестна, и приговор выносить не по чему — пусть цикл повторит.
        return ""
    if len(text) < GONE_TEXT_LIMIT and ORDER_MARKER not in text:
        return "страница заказа пустая — заказ снят или недоступен"
    return ""


def _price_context(page_text: str, width: int = 90) -> str:
    """Кусок страницы вокруг слов про цену — чтобы было видно, что чинить."""
    lowered = (page_text or "").lower()
    for hint in _PRICE_HINTS:
        index = lowered.find(hint)
        if index >= 0:
            start = max(0, index - 20)
            piece = page_text[start : index + width]
            return " ".join(piece.split())
    return ""


def parse_bid_price(page_text: str) -> float | None:
    """Цена отклика со страницы заказа. None — значит не нашли, откликаться нельзя."""
    found = [
        value
        for pattern in _BID_PRICE_PATTERNS
        for match in pattern.finditer(page_text)
        if (value := _to_number(match.group(1))) is not None
    ]
    return max(found) if found else None


@dataclass
class ClientInfo:
    """То, что Profi.ru рассказывает о заказчике — сырьё для фильтров."""

    reviews: int = 0
    phone_confirmed: bool = False
    is_vacancy: bool = False
    bid_rank: int | None = None  # каким по счёту будет наш отклик

    def as_dict(self) -> dict:
        return {
            "reviews": self.reviews,
            "phone_confirmed": self.phone_confirmed,
            "is_vacancy": self.is_vacancy,
            "bid_rank": self.bid_rank,
        }


def parse_client_info(page_text: str) -> ClientInfo:
    reviews_match = _REVIEWS_RE.search(page_text)
    rank_match = _RANK_RE.search(page_text)
    return ClientInfo(
        reviews=int(reviews_match.group(1)) if reviews_match else 0,
        phone_confirmed="Подтвердил" in page_text and PHONE_CONFIRMED_MARKER in page_text,
        is_vacancy=VACANCY_MARKER in page_text,
        bid_rank=int(rank_match.group(1)) if rank_match else None,
    )


@dataclass
class CardText:
    """Разбор текста карточки из списка. Порядок строк снят с живого кабинета."""

    category: str = ""
    budget_raw: str = ""
    description: str = ""
    location: str = ""
    posted_ago: str = ""
    extra: list[str] = field(default_factory=list)


def parse_card_text(text: str) -> CardText:
    """Строки карточки: категория / «до N ₽» / описание / где / когда / имя / когда оставлен.

    Часть строк может отсутствовать, поэтому опираемся не на номера строк,
    а на признаки: строка с ₽ — бюджет, строка с «назад» — давность, самая
    длинная — описание.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return CardText()

    card = CardText(category=lines[0])
    rest = lines[1:]

    for line in rest:
        if not card.budget_raw and "₽" in line:
            card.budget_raw = line.replace("false", "").strip()
        elif "назад" in line and not card.posted_ago:
            card.posted_ago = line
        elif line.startswith(("Дистанционно", "Выезд", "У себя")) and not card.location:
            card.location = line
        else:
            card.extra.append(line)

    if card.extra:
        card.description = max(card.extra, key=len)
    return card


# «5 минут назад», «1 час назад», «2 дня назад» — как Profi.ru показывает
# давность заказа. Своей даты публикации она не даёт вовсе.
_AGO_RE = re.compile(
    r"(\d+)\s*(секунд|минут|час|день|дня|дней|сутки|суток|недел|месяц)", re.IGNORECASE
)
# Давность словами, без числа. Смотрим их ПОСЛЕ чисел — иначе «21 час назад»
# совпадёт с «час назад». Порядок внутри тоже важен: «позавчера» содержит
# «вчера».
_AGO_WORDS = {
    "только что": 0.0,
    "меньше минут": 0.0,
    "минуту назад": 1 / 60,
    "час назад": 1.0,
    "позавчера": 48.0,
    "вчера": 24.0,
    "неделю назад": 24.0 * 7,
    "месяц назад": 24.0 * 30,
}
_AGO_HOURS = {
    "секунд": 1 / 3600, "минут": 1 / 60, "час": 1.0,
    "день": 24.0, "дня": 24.0, "дней": 24.0, "сутки": 24.0, "суток": 24.0,
    "недел": 24.0 * 7, "месяц": 24.0 * 30,
}


def parse_posted_ago(text: str, *, now: datetime | None = None) -> datetime | None:
    """«2 часа назад» → момент публикации. None — разобрать не вышло.

    Profi.ru не даёт заказу даты публикации, только эту строку. Без разбора
    `published_at` оставался пустым, и правило `max_age_hours` не действовало
    на площадке вовсе: `_check_age` при пустой дате пропускает заказ. Мы
    платили за отклики на заказы недельной давности, которые давно разобраны.
    """
    moment = now or datetime.now(timezone.utc)
    low = (text or "").lower()

    # СНАЧАЛА число, и только потом слова. Наоборот было нельзя: «21 час
    # назад» содержит подстроку «час назад», и словесная форма съедала число —
    # суточной давности заказ выглядел часовым и проходил ограничение по
    # возрасту, за которое мы платим откликом.
    match = _AGO_RE.search(text or "")
    if match:
        amount = int(match.group(1))
        hours = _AGO_HOURS.get(match.group(2).lower())
        if hours is not None:
            return moment - timedelta(hours=amount * hours)

    # Формы без числа. Без них «вчера» и «неделю назад» разбирались в None, а
    # None означает «считаем свежим» — то есть ровно старые заказы и проходили
    # мимо ограничения по возрасту.
    for word, hours in _AGO_WORDS.items():
        if word in low:
            return moment - timedelta(hours=hours)
    return None


def order_from_card(order_id: str, card: CardText) -> Order:
    """Карточка с доски → заказ. Отдельной функцией, чтобы её можно было проверить.

    Внутри `fetch_feed` этот кусок жил рядом с браузером, и проверить его
    можно было только подняв Chromium. Из-за этого пропажу даты публикации
    никто не заметил: `published_at` не заполнялся вовсе, и правило
    `max_age_hours` на площадке не действовало.
    """
    return Order(
        source=Source.PROFI,
        ext_id=order_id,
        url=ORDER_URL.format(order_id=order_id),
        title=card.category or f"Заказ {order_id}",
        description=card.description,
        category=card.category,
        # Осознанно НЕ кладём «до N ₽» в bid_price: это бюджет заказчика,
        # а не цена отклика. Цену берём только со страницы заказа.
        budget_raw=card.budget_raw,
        budget_value=_to_number(card.budget_raw),
        # Своей даты у Profi нет — считаем её из «2 часа назад».
        published_at=parse_posted_ago(card.posted_ago),
        description_full=False,
        raw={"posted_ago": card.posted_ago, "location": card.location},
    )


class ProfiSource:
    source_enum = Source.PROFI
    name = Source.PROFI.value
    # Доска и страница заказа видны только под сессией — всё через браузер.
    needs_browser = True

    # --- Список заказов ---------------------------------------------------

    def fetch_feed(self, context) -> list[Order]:
        """Читает доску заказов. Только чтение: ни одного клика по «Откликнуться»."""
        page = context.pages[0] if context.pages else context.new_page()
        _goto(page, ORDERS_URL)

        # Ждём именно карточки, а не фиксированную паузу: через мобильный прокси
        # страница рисуется заметно дольше, и на жёстких 4 секундах доска
        # возвращала ноль заказов при живой сессии.
        if not self._wait_for_cards(page):
            if not self.is_logged_in(page.content()):
                raise NotAuthorized("Сессия Profi.ru слетела — запусти: fbid login profi")
            logger.warning("[profi] доска не отрисовала карточки за {} сек", CARD_WAIT_MS / 1000)
            return []

        if not self.is_logged_in(page.content()):
            raise NotAuthorized("Сессия Profi.ru слетела — запусти: fbid login profi")

        orders: list[Order] = []
        for element in page.query_selector_all(CARD_SELECTOR):
            match = _CARD_ID_RE.match(element.get_attribute("data-testid") or "")
            if not match:
                continue
            order_id = match.group(1)
            orders.append(order_from_card(order_id, parse_card_text(element.inner_text())))
        logger.info("[profi] на доске {} заказов", len(orders))
        return orders

    @staticmethod
    def _wait_for_tariff(page) -> bool:
        """Ждёт блок с ценой отклика. False — не дождались.

        Ждём сам блок, а не «ещё немного»: цена — единственное, ради чего мы
        вообще открываем страницу заказа, и читать её раньше времени значит
        отказаться от заказа, на который можно было откликнуться за 42 ₽.
        """
        try:
            page.wait_for_selector(TARIFF_SELECTOR, timeout=TARIFF_WAIT_MS)
            return True
        except Exception:  # noqa: BLE001 — таймаут это «блока нет», разберёмся по тексту
            return False

    @staticmethod
    def _wait_for_cards(page) -> bool:
        """Ждёт появления карточек заказов. False — не дождались."""
        try:
            page.wait_for_selector(CARD_SELECTOR, timeout=CARD_WAIT_MS)
            return True
        except Exception:  # noqa: BLE001 — таймаут ожидания — это просто «карточек нет»
            return False

    # --- Страница заказа --------------------------------------------------

    def fetch_details(self, context, order: Order) -> Order:
        """Открывает заказ и достаёт полное ТЗ, цену отклика и данные о заказчике.

        С повторами: мобильный прокси рвёт туннель при ротации IP, а без цены
        отклика заказ отсеивается. Один сетевой сбой не должен стоить заказа.
        """
        page = context.pages[0] if context.pages else context.new_page()

        text = ""
        for attempt in range(1, DETAILS_ATTEMPTS + 1):
            try:
                # Здесь свой цикл попыток, поэтому обходимся без _goto —
                # но таймаут нужен тот же: тридцати секунд прокси не хватает.
                page.goto(order.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                # Ждём именно блок тарифа, а не «сколько-нибудь секунд»: он
                # приезжает последним, и на медленном прокси фиксированная
                # пауза кончалась раньше, чем появлялась цена.
                got_tariff = self._wait_for_tariff(page)
                text = page.inner_text("body")
                if got_tariff and text.strip():
                    break
                # Тарифа нет — либо заказ скрыт или снят (повторять незачем),
                # либо страница не догрузилась (тогда пробуем ещё раз).
                if no_price_reason(text):
                    break
                if attempt < DETAILS_ATTEMPTS:
                    logger.debug(
                        "[profi] {} — тарифа нет, пробую ещё раз ({}/{})",
                        order.ext_id,
                        attempt,
                        DETAILS_ATTEMPTS,
                    )
                    page.wait_for_timeout(DETAILS_RETRY_PAUSE_MS)
                    continue
                if text.strip():
                    break
            except Exception as exc:  # noqa: BLE001 — обрыв прокси ожидаем
                logger.debug(
                    "[profi] {} попытка {}/{}: {}", order.ext_id, attempt, DETAILS_ATTEMPTS, exc
                )
                if attempt == DETAILS_ATTEMPTS:
                    logger.warning(
                        "[profi] {} — страница заказа не открылась за {} попыток",
                        order.ext_id,
                        DETAILS_ATTEMPTS,
                    )
                    return order
                page.wait_for_timeout(DETAILS_RETRY_PAUSE_MS)

        if not self.is_logged_in(page.content()):
            raise NotAuthorized("Сессия Profi.ru слетела — запусти: fbid login profi")

        order.bid_price = parse_bid_price(text)
        if order.bid_price is None:
            # Показываем, ЧТО именно не разобралось. Без этого «не нашёл цену»
            # выглядит одинаково и когда вёрстка поехала, и когда страница
            # не догрузилась, — а чинить это разные вещи. Двенадцать заказов
            # за сутки ушли в отказ, и по логу было не понять почему.
            reason = no_price_reason(text)
            order.raw["no_price_reason"] = reason
            if reason:
                logger.info("[profi] {} — пропускаю: {}", order.ext_id, reason)
            else:
                around = _price_context(text)
                logger.warning(
                    "[profi] {} — не нашёл цену отклика, заказ будет пропущен. "
                    "Что было на странице: {}",
                    order.ext_id,
                    around or "(про цену отклика на странице ни слова)",
                )

        client = parse_client_info(text)
        order.raw.update(client.as_dict())
        order.description = _extract_task_text(text) or order.description
        order.description_full = True
        return order

    # --- Отправка отклика (ПЛАТНО) ----------------------------------------

    def submit_bid(
        self,
        context,
        order: Order,
        text: str,
        *,
        max_price: float,
        two_phase: bool = False,
        price_from: int = 0,
        price_to: int = 0,
    ) -> SubmitResult:
        """Занимает очередь по заказу. Каждый клик здесь стоит денег.

        Перед нажатием цена читается ЗАНОВО с самой кнопки: карточка могла
        устареть, а списывают по тому, что написано на кнопке в момент клика.
        Не нашли цену на кнопке — не жмём. Эту же цену возвращаем наружу:
        в учёт должна попасть она, а не то, что лежало в базе со вчера.

        ПРО ДВУХФАЗНОСТЬ. На Profi.ru её сделать нельзя, и параметр two_phase
        здесь ничего не меняет. Форма отклика устроена так: «Продолжить» →
        обязательные поля «текст» и «стоимость от/до» → «Откликнуться» (вот тут
        списываются деньги). Занять очередь без текста невозможно — кнопка
        оплаты просто не срабатывает. Параметр оставлен ради единого интерфейса
        с FL.ru, где двухфазность реальна.
        """
        if not text.strip():
            logger.error("[profi] пустой текст отклика — отправка отменена")
            return SubmitResult(False, reason="пустой текст отклика")

        page = context.pages[0] if context.pages else context.new_page()
        _goto(page, order.url)
        page.wait_for_timeout(3500)

        if not self.is_logged_in(page.content()):
            raise NotAuthorized("Сессия Profi.ru слетела — запусти: fbid login profi")

        button = self._find_submit_button(page)
        if button is None:
            # Кнопки нет — заказ закрыт, снят или отклик по нему уже ушёл. Ни
            # одна из причин не пройдёт сама, поэтому это приговор, а не сбой.
            # Обычным сбоем заказ возвращался в очередь: каждый следующий
            # проход снова платил модели за текст, снова открывал страницу и
            # снова слал владельцу ту же тревогу — и так пока заказ не выпадет
            # из окна свежести.
            logger.warning("[profi] {} — кнопки отклика нет, заказ мог закрыться", order.ext_id)
            raise BidUnavailable("кнопки отклика нет — заказ закрыт или отклик уже отправлен")

        # Цена прямо со страницы перед кликом — последний рубеж перед списанием.
        # Сначала блок тарифа (там «Отклик • 46 ₽» и «Продолжить 46 ₽»),
        # если пусто — текст самого элемента.
        price_on_button = parse_bid_price(self._tariff_text(page)) or parse_bid_price(
            button.inner_text() or ""
        )
        if price_on_button is None:
            logger.error(
                "[profi] {} — на кнопке нет цены, отправку отменяю", order.ext_id
            )
            return SubmitResult(False, reason="на кнопке нет цены")
        if price_on_button > max_price:
            logger.warning(
                "[profi] {} — на кнопке {:.0f} ₽ при потолке {:.0f} ₽, отменяю",
                order.ext_id,
                price_on_button,
                max_price,
            )
            return SubmitResult(False, reason="цена на кнопке выше потолка")

        # Шаг 1 — «Продолжить»: денег не списывает, открывает форму с текстом.
        button.click()
        try:
            page.wait_for_selector("textarea", timeout=FORM_WAIT_MS)
        except Exception:  # noqa: BLE001 — не открылась форма — отклик не отправляем
            logger.error("[profi] {} — форма отклика не открылась", order.ext_id)
            return SubmitResult(False, reason="форма отклика не открылась")

        field = page.query_selector("textarea")
        payload = trim_message(text)
        if len(text) > MESSAGE_LIMIT:
            logger.info(
                "[profi] {} — заход обрезан с {} до {} символов (лимит площадки)",
                order.ext_id,
                len(text),
                len(payload),
            )
        field.fill(payload)
        page.wait_for_timeout(800)

        # Поля «Стоимость услуги» (От / До) — ОБЯЗАТЕЛЬНЫЕ. Пока они пустые,
        # клик по кнопке оплаты молча ничего не делает: форма остаётся на месте,
        # ошибок нет, деньги не списываются. Проверено на живом заказе.
        # Поэтому не заполнили — выходим сразу, а не жмём вслепую: слепой клик
        # даёт «биржа не подтвердила отправку» и ложную тревогу владельцу.
        if not self._fill_price_range(page, price_from, price_to):
            logger.error(
                "[profi] {} — не заполнил стоимость услуги, отправку отменяю", order.ext_id
            )
            return SubmitResult(False, reason="не заполнил стоимость услуги")

        # Шаг 2 — здесь списываются деньги. Цену перечитываем ещё раз:
        # между открытием заказа и этим моментом прошло несколько секунд.
        pay = page.query_selector(PAY_BUTTON)
        if pay is None:
            logger.error("[profi] {} — нет кнопки оплаты, отправку отменяю", order.ext_id)
            return SubmitResult(False, reason="нет кнопки оплаты")

        final_price = parse_bid_price(self._tariff_text(page)) or price_on_button
        if final_price is None or final_price > max_price:
            logger.error(
                "[profi] {} — на шаге оплаты цена {} ₽ при потолке {} ₽, отменяю",
                order.ext_id,
                final_price,
                max_price,
            )
            return SubmitResult(False, reason="на шаге оплаты цена выше потолка")

        # С этого момента деньги считаются потраченными при любом исходе.
        # Форма закрылась — отклик точно ушёл. Не закрылась — мы не знаем,
        # но списание уже произошло, и повторять отправку нельзя: заплатим
        # второй раз за тот же заказ. Поэтому charged=True в обоих случаях —
        # включая тот, когда после клика оборвалась связь: исключение отсюда
        # наверху становится «ошибка отправки», заказ считается неотработанным
        # и на следующем проходе оплачивается второй раз.
        #
        # САМ КЛИК тоже внутри. Он стоял снаружи — а он и есть то место, где
        # деньги уходят: playwright поднимает исключение и после нажатия
        # (страница ушла на перезагрузку, элемент отвалился, истёк таймаут
        # ожидания). Такое исключение улетало наверх как «ошибка отправки», и
        # за уже оплаченный отклик мы платили второй раз.
        try:
            pay.click()
            page.wait_for_timeout(6000)
            closed = page.query_selector(PAY_BUTTON) is None
        except Exception as exc:  # noqa: BLE001 — деньги уже ушли, повторять нельзя
            logger.error("[profi] после клика по оплате всё сорвалось: {}", exc)
            return SubmitResult(
                False,
                price=final_price,
                charged=True,
                reason=f"оплата нажата, дальше связь оборвалась ({exc}) — "
                       f"проверь заказ руками, второй раз не отправляю",
            )
        return SubmitResult(
            sent=closed,
            price=final_price,
            charged=True,
            reason="" if closed else "биржа не подтвердила отправку, но деньги списаны",
        )

    @staticmethod
    def _fill_price_range(page, price_from: int, price_to: int) -> bool:
        """Заполняет «Стоимость услуги: От / До». Без этого отклик не отправится."""
        inputs = [i for i in page.query_selector_all("input[type=text]") if i.is_visible()]
        if len(inputs) < 2:
            logger.warning("[profi] не нашёл поля стоимости — отправка может не пройти")
            return False
        if price_from <= 0:
            logger.warning("[profi] модель не дала вилку цены — отправка может не пройти")
            return False

        # Верхнюю границу НЕ выдумываем. Здесь стояло `price_from * 1.6`, и
        # клиент видел под нашим же откликом строку «Стоимость от 50 000 до
        # 80 000 руб.» — потолок, которого в тексте отклика не было ни разу.
        # Число из анкеты клиент читает как нашу цену наравне с текстом, и
        # расхождение между ними выглядит как попытка накинуть сверху.
        # Модель не назвала потолок — значит цена одна, её и ставим в оба поля.
        upper = price_to if price_to > price_from else price_from
        inputs[0].fill(str(price_from))
        page.wait_for_timeout(400)
        inputs[1].fill(str(upper))
        page.wait_for_timeout(1000)
        if upper == price_from:
            logger.info("[profi] стоимость: {} ₽ (модель дала одну цену)", price_from)
        else:
            logger.info("[profi] стоимость: {} - {} ₽", price_from, upper)
        return True

    # Сколько знаков нашего текста ищем в переписке, чтобы убедиться в отправке.
    PROOF_CHARS = 60

    # Поле переписки с заказчиком. Снято с живой страницы 26.08.2026: на
    # странице заказа это единственная textarea, и подсказка у неё «Сообщение».
    #
    # Держимся за подсказку, а не за класс и не за порядок на странице. Класс
    # у Profi сгенерированный («CNXbgT») и меняется от сборки к сборке. А «первая
    # textarea» — это ровно та догадка, которая на FL оказалась полем личной
    # заметки: ответы уходили туда, клиент их не видел, а система рапортовала
    # «отправлено».
    CHAT_FIELDS = (
        "textarea[placeholder^='Сообщение']",
        "textarea[placeholder^='Написать']",
    )

    def _chat_field(self, page):
        """Поле переписки — и только оно. Не нашли именно его — не отправляем."""
        for selector in self.CHAT_FIELDS:
            field = page.query_selector(selector)
            if field is not None:
                return field
        return None

    def send_message(self, context, order: Order, text: str) -> bool:
        """Пишет клиенту в чат по заказу. False — сообщение НЕ ушло.

        Денег не тратит — контакт уже оплачен откликом. Не нашли поле ввода
        или не увидели своё сообщение в переписке — возвращаем False, и
        владельцу приходит тревога с готовым текстом: контакт оплачен, а
        клиенту никто не написал, это чинится руками за минуту.

        Подтверждаем именно по переписке, а не по опустевшему полю. Пустое
        поле — слабый признак: страница могла перерисоваться, а очищенным
        оказаться совсем другое поле. Хуже того, прежний код на исчезнувшее
        из DOM поле отвечал «отправлено» — то есть выдавал за успех ровно тот
        случай, когда о судьбе сообщения не известно ничего.
        """
        if not text.strip():
            logger.error("[profi] пустой текст сообщения — отправка отменена")
            return False

        page = context.pages[0] if context.pages else context.new_page()
        _goto(page, order.url)
        page.wait_for_timeout(3500)

        if not self.is_logged_in(page.content()):
            raise NotAuthorized("Сессия Profi.ru слетела — запусти: fbid login profi")

        field = self._chat_field(page)
        if field is None:
            logger.error(
                "[profi] {} — не нашёл поле переписки, сообщение НЕ отправлено", order.ext_id
            )
            return False

        # Режем ДО отправки, как и заход. Поле площадки обрезает лишнее само,
        # молча, а подтверждение смотрит только первые PROOF_CHARS знаков —
        # обрезанное на полуслове сообщение засчитывалось как отправленное
        # целиком. Клиент при этом получал оборванную фразу.
        text = trim_message(text)
        if not text:
            logger.error("[profi] {} — пустой текст, отправлять нечего", order.ext_id)
            return False

        field.fill(text)
        page.wait_for_timeout(500)

        send = self._find_send_button(page)
        if send is None:
            # Запасной путь: во многих чатах Enter отправляет сообщение.
            field.press("Enter")
        else:
            send.click()
        page.wait_for_timeout(3000)
        return self._message_is_in_the_thread(page, order, text)

    def _message_is_in_the_thread(self, page, order: Order, text: str) -> bool:
        """Видно ли наше сообщение в переписке после отправки."""
        from ..replies import extract_conversation

        probe = " ".join(text.split())[: self.PROOF_CHARS]
        if not probe:
            return False
        try:
            conversation = " ".join(extract_conversation(page, order).split())
        except Exception as exc:  # noqa: BLE001 — не прочитали, значит не подтвердили
            logger.warning(
                "[profi] {} — переписку после отправки не прочитал: {}", order.ext_id, exc
            )
            return False
        if probe in conversation:
            return True
        logger.error(
            "[profi] {} — своего сообщения в переписке не вижу: считаю, что оно НЕ ушло",
            order.ext_id,
        )
        return False

    @staticmethod
    def _find_submit_button(page):
        """Элемент, по которому уходит отклик.

        Это НЕ <button>: Profi.ru рисует его обычным div внутри блока тарифа
        [data-testid="orderCard/tariffs"]. Поиск по тегу button возвращал None
        на каждом заказе, и автоотклик молча не работал. Поэтому ищем по тексту
        среди любых тегов, приоритетно внутри блока тарифа.
        """
        for scope in (TARIFF_SELECTOR, None):
            for label in SUBMIT_LABELS:
                selector = f"text={label}" if scope is None else f"{scope} >> text={label}"
                try:
                    found = page.query_selector(selector)
                except Exception:  # noqa: BLE001 — невалидный селектор — пробуем следующий
                    continue
                if found is not None:
                    return ProfiSource._clickable_ancestor(found)
        return None

    @staticmethod
    def _clickable_ancestor(element):
        """Поднимается до верхнего предка с cursor:pointer.

        Текст «Продолжить» лежит в span внутри кликабельного div. Клик по самому
        span молча не срабатывал: страница не менялась, запросов не уходило,
        и автоотклик тихо ничего не делал.
        """
        return element.evaluate_handle(
            """e => {
                let cur = e;
                while (cur && cur.parentElement) {
                    if (getComputedStyle(cur.parentElement).cursor !== 'pointer') break;
                    cur = cur.parentElement;
                }
                return cur;
            }"""
        ).as_element()

    @staticmethod
    def _tariff_text(page) -> str:
        """Текст блока тарифа — там же лежит цена, по которой спишут."""
        block = page.query_selector(TARIFF_SELECTOR)
        return (block.inner_text() if block else "") or ""

    @staticmethod
    def _find_send_button(page):
        """Кнопка отправки сообщения в чате."""
        for selector in (
            '[data-testid*="send"]',
            "button:text-matches('Отправить')",
            'button[type="submit"]',
        ):
            found = page.query_selector(selector)
            if found is not None:
                return found
        return None

    # --- Баланс -----------------------------------------------------------

    def read_balance(self, context) -> float | None:
        """Остаток на счету из шапки кабинета. None — прочитать не удалось.

        None трактуется вызывающим кодом как «денег может не быть» и блокирует отклик:
        молча считать нулём или бесконечностью одинаково плохо.
        """
        page = context.pages[0] if context.pages else context.new_page()
        # Сравниваем БЕЗ query-строки: адрес заказа n.php?o=123 тоже начинается
        # с n.php, и проверка через startswith считала, что мы уже на доске —
        # баланс читался не с той страницы и возвращался None.
        if page.url.split("?")[0] != ORDERS_URL:
            _goto(page, ORDERS_URL)
            page.wait_for_timeout(4000)

        element = page.query_selector(BALANCE_SELECTOR)
        if not element:
            logger.warning("[profi] не нашёл баланс в шапке кабинета")
            return None
        return parse_balance(element.inner_text())

    # --- Сессия -----------------------------------------------------------

    @staticmethod
    def is_logged_in(page_html: str) -> bool:
        return not any(marker in page_html for marker in GUEST_MARKERS)


# Текст задачи идёт от начала страницы до служебного блока «Заказ № …».
_TASK_END_RE = re.compile(r"\n(?:Когда|Заказ №|Дистанционно)\b")
# Шапка кабинета: меню и БАЛАНС счёта. Без обрезки они уезжали и в промт
# модели, и в карточку Telegram — «Заказы | Чаты | 515 ₽ | Анкета | Поддержка»
# перед каждым ТЗ. Текст задания начинается после «Описание».
_TASK_START_RE = re.compile(r"\nОписание\s*\n")
# Запасной вариант, если «Описание» на странице нет: режем известные пункты меню.
_CHROME_LINES = frozenset(
    ["Заказы", "Чаты", "Анкета", "Поддержка", "Профиль", "Настройки", "Продвижение"]
)
_BALANCE_LINE_RE = re.compile(r"^\d[\d\s ]*₽$")


def _extract_task_text(page_text: str) -> str:
    """Текст задания без шапки кабинета и хвоста страницы.

    Шапку убираем обязательно: в ней висит баланс счёта, и он уезжал прямо
    в промт модели и в карточку Telegram.
    """
    body = page_text
    start = _TASK_START_RE.search(body)
    if start:
        body = body[start.end() :]

    match = _TASK_END_RE.search(body)
    if match:
        body = body[: match.start()]

    lines = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line in _CHROME_LINES or _BALANCE_LINE_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def probe(context, out_dir) -> list[str]:
    """Слепок страниц кабинета: HTML + скриншот. Только чтение."""
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    page = context.pages[0] if context.pages else context.new_page()
    for name, url in PROBE_PAGES.items():
        try:
            _goto(page, url)
            page.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001 — слепок делаем по тем страницам, что открылись
            logger.warning("[profi] не открыл {}: {}", url, exc)
            continue

        html_path = out_dir / f"profi-{name}.html"
        html_path.write_text(page.content(), encoding="utf-8")
        try:
            page.screenshot(path=str(out_dir / f"profi-{name}.png"), full_page=True)
        except Exception as exc:  # noqa: BLE001 — скриншот не критичен
            logger.debug("скриншот не снялся: {}", exc)

        logger.info("[profi] {} → {} (итоговый URL: {})", name, html_path.name, page.url)
        saved.append(str(html_path))

    return saved

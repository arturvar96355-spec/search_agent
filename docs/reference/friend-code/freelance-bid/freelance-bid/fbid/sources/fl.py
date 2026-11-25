"""FL.ru.

Сбор идёт по официальной RSS-ленте заказов — она отдаётся без авторизации,
не ломается от редизайна и не нагружает биржу. Лента даёт заголовок, бюджет,
категорию, ссылку и обрезанное описание; полное ТЗ добираем со страницы заказа.

Авторизация нужна только для отправки отклика (шаг 4) — там уже браузер с профилем.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from loguru import logger

from ..config import get_settings
from ..models import (
    BidUnavailable,
    Order,
    Source,
    SubmitResult,
    parse_budget,
    strip_budget_suffix,
)
from .base import html_to_text, http_get

# Сколько раз пробуем открыть страницу заказа и сколько ждём загрузки.
# Мобильный прокси меняет IP каждые пять минут, и в этот момент навигация
# падает по таймауту. Одна попытка по тридцать секунд оборачивалась потерей
# заказа: отклик помечался неудачным и больше не повторялся.
NAV_ATTEMPTS = 3
NAV_TIMEOUT_MS = 45000
NAV_RETRY_PAUSE_MS = 4000


def _open_with_retries(page, url: str) -> None:
    """Открывает страницу, переживая обрыв туннеля при смене IP."""
    last_error: Exception | None = None
    for attempt in range(1, NAV_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return
        except Exception as exc:  # noqa: BLE001 — разберём после всех попыток
            last_error = exc
            logger.debug("[fl] попытка {} открыть {} не удалась: {}", attempt, url, exc)
            if attempt < NAV_ATTEMPTS:
                page.wait_for_timeout(NAV_RETRY_PAUSE_MS)
    raise last_error if last_error else RuntimeError(f"[fl] не открыл {url}")

# Сколько знаков разметки означает «страница отрисовалась». Живая страница
# заказа на FL — это десятки килобайт: шапка, меню, подвал. Пустой ответ и
# страница-заглушка короче любого разумного порога, и по ним нельзя судить ни
# об авторизации, ни о наличии формы отклика.
PAGE_MIN_HTML = 2000


def _page_loaded(html: str) -> bool:
    """Отрисовалась ли страница вообще."""
    return len((html or "").strip()) >= PAGE_MIN_HTML


FEED_URL = "https://www.fl.ru/rss/all.xml"
BASE_URL = "https://www.fl.ru"
# Ссылка из меню авторизованного: у гостя её нет, поэтому это и есть признак входа.
LOGOUT_MARK = "/account/logout/"


class NotAuthorized(RuntimeError):
    """Сессия FL.ru слетела — нужен `fbid login fl`."""


# Отказ, который означает «в пакете кончились отклики». Строка одна на все
# места: её пишет адаптер, её же ищет проход, чтобы остановиться и не жечь
# модель на заказы, отправить которые всё равно нечем. Разъедутся — проход
# снова пойдёт по кругу за деньги.
PACKAGE_EMPTY = "кончился пакет откликов — купи новый на fl.ru/payed/"



_ID_RE = re.compile(r"/projects/(\d+)/")
# Блок полного ТЗ на странице заказа. Если FL.ru переименует класс — ловим это в логах
# и правим одну строку; сбор ленты при этом продолжает работать.
_DESCRIPTION_RE = re.compile(
    r'<div[^>]*class="[^"]*fl-project-content__description-text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)

# Сколько заказчик на бирже. Единственное, что FL.ru рассказывает о нём без
# авторизации, — и этого хватает, чтобы отличить человека, который разместил
# первый в жизни заказ, от того, кто здесь не первый год. Отзывы и история
# найма лежат под входом в аккаунт, анонимно их не видно.
_REGISTERED_RE = re.compile(
    r"Зарегистрирован:\s*</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>", re.IGNORECASE
)


class FlSource:
    source_enum = Source.FL
    name = Source.FL.value
    # Лента и ТЗ читаются обычным HTTP: RSS открыт, страница заказа тоже.
    # Браузер нужен только на отправку отклика.
    needs_browser = False

    def fetch_feed(self) -> list[Order]:
        resp = http_get(FEED_URL)
        return self._parse_feed(resp.text)

    # --- Разбор ленты ------------------------------------------------------

    @staticmethod
    def _parse_feed(xml_text: str) -> list[Order]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as exc:
            logger.error("RSS FL.ru не распарсился: {}", exc)
            return []

        orders: list[Order] = []
        for item in root.iterfind(".//item"):
            link = (item.findtext("link") or "").strip()
            match = _ID_RE.search(link)
            if not match:
                continue

            raw_title = (item.findtext("title") or "").strip()
            budget_raw, budget_value = parse_budget(raw_title)

            orders.append(
                Order(
                    source=Source.FL,
                    ext_id=match.group(1),
                    url=link,
                    title=strip_budget_suffix(raw_title),
                    description=html_to_text(item.findtext("description") or ""),
                    category=(item.findtext("category") or "").strip(),
                    budget_raw=budget_raw,
                    budget_value=budget_value,
                    published_at=_parse_date(item.findtext("pubDate")),
                    description_full=False,
                )
            )
        return orders

    # --- Полное ТЗ ---------------------------------------------------------

    def fetch_details(self, order: Order) -> Order:
        """Открывает страницу заказа и заменяет обрезанное описание полным.

        Ошибку не поднимаем: заказ с коротким описанием лучше, чем упавший проход.
        """
        try:
            resp = http_get(order.url)
        except Exception as exc:  # noqa: BLE001 — заказ с коротким описанием лучше упавшего прохода
            logger.warning("Не смог открыть {}: {}", order.url, exc)
            return order

        # Данные о заказчике снимаем с той же страницы: отдельный запрос ради
        # одной строки — лишняя нагрузка на биржу и лишний повод получить 403.
        order.raw.update(parse_client(resp.text))

        match = _DESCRIPTION_RE.search(resp.text)
        if not match:
            logger.warning(
                "На {} не нашёл блок описания — возможно, FL.ru поменял вёрстку", order.url
            )
            return order

        order.description = html_to_text(match.group(1))
        order.description_full = True
        return order

    # --- Отправка отклика --------------------------------------------------

    # Селекторы формы отклика сняты с живой страницы заказа под сессией (05.08.2026).
    TEXTAREA = "#el-descr"
    SUBMIT = "#el-submit"
    COST_FIELD = "#el-cost_from"
    DAYS_FIELD = "#el-time_from"
    # --- Чат с заказчиком ---------------------------------------------------
    #
    # Селекторы сняты с живой страницы под нашей сессией 23.08.2026.
    #
    # Главное про это место: НА САМОЙ СТРАНИЦЕ ЗАКАЗА ПОЛЯ ЧАТА НЕТ. Там есть
    # ровно одна textarea — «#noteTextarea», и это личная заметка по заказу,
    # которую видим только мы. Раньше отправка целилась именно в неё: текст
    # уходил в заметку, функция возвращала «отправлено», владельцу приходила
    # карточка «ответ ушёл клиенту» — а клиент не получал ничего и молчал.
    # Поэтому запасного «первого попавшегося поля» здесь больше нет: не нашли
    # именно чат — значит не отправили.
    #
    # Чат открывается кнопкой и живёт в модальном окне поверх страницы.
    CHAT_OPEN = "button:has-text('Чат')"
    CHAT_FIELD = "textarea[data-id='qa-ui-textarea-message-chat']"
    # Лента сообщений внутри окна — по ней подтверждаем, что письмо ушло.
    CHAT_THREAD = "div.fl-messages"
    # Кнопки отправки нет: под полем написано «Enter для отправки |
    # Ctrl + Enter для переноса строки».
    CHAT_SEND_KEY = "Enter"
    # Сколько ждём, пока окно чата отрисуется после нажатия кнопки.
    CHAT_OPEN_TIMEOUT_MS = 20000
    # Сколько знаков нашего текста ищем в ленте, чтобы убедиться в отправке.
    PROOF_CHARS = 60

    def submit_bid(
        self,
        context,
        order: Order,
        text: str,
        *,
        max_price: float | None = None,
        days: int | None = None,
        client_price: float | None = None,
        two_phase: bool = False,
        opener_chars: int = 220,
    ) -> SubmitResult:
        """Отправляет отклик на заказ.

        Возвращает не просто «получилось или нет», а ещё и то, была ли
        израсходована попытка: клик по кнопке тратит отклик из купленного
        пакета независимо от того, подтвердила ли форма отправку.

        Пустой текст не отправляем ни при каких условиях — пустой отклик хуже,
        чем его отсутствие.
        """
        if not text.strip():
            logger.error("[fl] пустой текст отклика — отправка отменена")
            return SubmitResult(False, reason="пустой текст отклика")

        # В двухфазном режиме первой уходит только «шапка» захода: на FL клиент
        # видит текст отклика сразу в списке, поэтому пустышку слать нельзя.
        payload = opening_part(text, opener_chars) if two_phase else text

        page = context.pages[0] if context.pages else context.new_page()
        _open_with_retries(page, order.url)
        page.wait_for_timeout(2000)

        html = page.content()
        if not _page_loaded(html):
            # Страница не отрисовалась вовсе: `goto` вернулся, а содержимого
            # нет. Судить по такой странице нельзя ни о чём — а ниже стоит
            # приговор BidUnavailable, который хоронит заказ навсегда.
            # «Не смогли посмотреть» и «посмотрели, формы нет» — разные
            # новости, и через мобильный прокси первая случается регулярно.
            logger.warning("[fl] страница {} не отрисовалась — вернусь позже", order.url)
            return SubmitResult(False, reason="страница заказа не открылась — вернусь позже")

        if not self.is_logged_in(html):
            raise NotAuthorized("Сессия FL.ru слетела — запусти: fbid login fl")

        field = page.query_selector(self.TEXTAREA)
        if field is None:
            # Формы нет по трём разным поводам, и они НЕ равнозначны: «уже
            # откликались» и «заказ закрыт» не изменятся никогда, а «кончился
            # пакет» пройдёт, как только владелец докупит отклики.
            #
            # Раньше все три складывались в BidUnavailable — статус, который
            # закрывает заказ навсегда. Пакет кончался посреди прохода, и
            # десяток живых заказов хоронился до конца времён вместе с ним.
            # Поэтому спрашиваем остаток и различаем случаи.
            left = self.read_package(context)
            if left == 0:
                logger.warning(
                    "[fl] на {} нет формы: кончился пакет откликов — заказ вернётся "
                    "в очередь, как только пакет пополнят", order.url,
                )
                return SubmitResult(False, reason=PACKAGE_EMPTY)
            if left is None:
                # Остаток не прочитался — значит мы НЕ ЗНАЕМ, почему нет формы.
                # BidUnavailable хоронит заказ навсегда, и ставить этот приговор
                # по неизвестной причине нельзя: страница платных услуг не
                # открылась — а живой заказ из-за этого больше не увидит никто.
                # «Не смогли посмотреть» и «посмотрели, всё занято» — разные
                # новости, и путать их здесь дороже всего.
                logger.warning(
                    "[fl] на {} нет формы, остаток пакета не прочитался — "
                    "приговор не выношу, вернусь позже", order.url,
                )
                return SubmitResult(
                    False, reason="остаток пакета не прочитался — вернусь к заказу позже"
                )
            logger.warning(
                "[fl] на {} нет формы отклика — уже откликались или заказ закрыт", order.url,
            )
            raise BidUnavailable(
                "форма отклика недоступна: уже откликались по этому заказу или заказ закрыт"
            )

        # Срок и стоимость на форме обязательные: с пустыми полями кнопка
        # отправки молча ничего не делает, а мы записывали такой заход как
        # отправленный. Проверяем до клика — так попытка из пакета не сгорит.
        if days is None or client_price is None:
            logger.error(
                "[fl] нет срока или цены для {} — отправлять нечего, форма их требует", order.url
            )
            return SubmitResult(False, reason="не заданы срок и стоимость работы")

        self._type_into(page, field, payload)
        page.fill(self.DAYS_FIELD, str(days))
        # Это цена НАШЕЙ работы, которую увидит клиент, а не списание с нас:
        # отклики на FL идут из купленного пакета. Возвращать её как price
        # нельзя — она уедет в суточный бюджет и съест его целиком.
        page.fill(self.COST_FIELD, str(int(client_price)))

        if not (field.input_value() or "").strip():
            logger.error("[fl] текст отклика не удержался в поле — отправку отменяю")
            return SubmitResult(False, reason="форма не приняла текст отклика")

        submit = page.query_selector(self.SUBMIT)
        if submit is None:
            logger.warning("[fl] не нашёл кнопку отправки на {}", order.url)
            return SubmitResult(False, reason="нет кнопки отправки")

        submit.click()

        # Всё, что после клика, — уже за деньги. Отклик из купленного пакета
        # списан самим кликом, поэтому ни одна помеха дальше не должна
        # выпустить исключение наружу: наверху оно становится «ошибка
        # отправки», заказ считается неотработанным и на следующем проходе
        # оплачивается второй раз. Мобильный прокси меняет IP каждые пять
        # минут, и оборваться здесь — обычное дело.
        try:
            page.wait_for_timeout(4000)

            after = page.query_selector(self.TEXTAREA)
            if after is not None and (after.input_value() or "").strip():
                logger.warning("[fl] после отправки текст остался в форме — считаю неудачей")
                return SubmitResult(
                    False,
                    charged=True,
                    reason="форма не приняла отклик, но попытка засчитана",
                )

            # Исчезнувшая форма — ещё не доказательство. Она пропадает и когда FL
            # подменяет её сообщением «кончился пакет откликов»: владелец видел
            # «отклик отправлен» в боте и пустое место на самом заказе. Поэтому
            # спрашиваем страницу заново и ищем свой отклик глазами.
            if self.bid_visible(page, order, payload):
                return SubmitResult(True, charged=True)
            logger.error("[fl] отклик по {} на странице не появился", order.url)
            return SubmitResult(
                False,
                charged=True,
                reason="после отправки отклика на странице заказа нет — "
                       "проверь пакет откликов на FL",
            )
        except Exception as exc:  # noqa: BLE001 — после клика повторять нельзя ни при чём
            logger.error("[fl] после клика по отправке всё сорвалось: {}", exc)
            return SubmitResult(
                False,
                charged=True,
                reason=f"клик по отправке прошёл, дальше связь оборвалась ({exc}) — "
                       f"проверь заказ руками, второй раз не отправляю",
            )

    @staticmethod
    def _type_into(page, field, text: str) -> None:
        """Вводит текст так, как это делает человек.

        Программная подстановка значения форму больше не устраивает: счётчик
        знаков под полем оставался на нуле, кнопка отправки считала отклик
        пустым и молча ничего не делала — а система записывала «отправлено».
        """
        try:
            field.click()
            page.keyboard.insert_text(text)
        except Exception as exc:  # noqa: BLE001 — на крайний случай остаётся подстановка
            logger.warning("[fl] ввод с клавиатуры не прошёл ({}) — подставляю значением", exc)
            field.fill(text)
        page.wait_for_timeout(800)

    # Сколько знаков нашего текста ищем на странице. Короткий кусок надёжнее:
    # FL режет длинные отклики в списке многоточием.
    PROBE_CHARS = 40

    def bid_visible(self, page, order: Order, payload: str) -> bool:
        """Стоит ли наш отклик на странице заказа. Проверка после отправки.

        Ищем два следа: свой логин в списке откликов и начало собственного
        текста. Хватает любого — в свёрнутом списке FL показывает то одно,
        то другое.
        """
        try:
            _open_with_retries(page, order.url)
            page.wait_for_timeout(2500)
            body = page.inner_text("body")
        except Exception as exc:  # noqa: BLE001 — не смогли проверить, значит не подтвердили
            logger.warning("[fl] не смог перечитать {}: {}", order.url, exc)
            return False

        login = get_settings().fl_login or ""
        if login and login.lower() in body.lower():
            return True
        probe = " ".join(payload.split())[: self.PROBE_CHARS]
        return bool(probe) and probe in " ".join(body.split())

    def send_message(self, context, order: Order, text: str) -> bool:
        """Пишет клиенту в чат по заказу. False — сообщение НЕ ушло.

        Отклик уже стоит на заказе, поэтому здесь ничего не расходуется.

        Отправку обязательно подтверждаем: перечитываем страницу и ищем свой
        текст в переписке. Раньше функция возвращала True сразу после клика —
        то есть выдавала «нажали кнопку» за «клиент увидит». Один раз этого
        хватило, чтобы владельцу пришла карточка «ответ отправлен», а клиент
        так ничего и не получил: текст ушёл в личную заметку по заказу.
        Ложное «отправлено» хуже честного «не смог»: по первому никто не
        пойдёт разбираться, по второму владелец ответит сам.
        """
        if not text.strip():
            return False

        page = context.pages[0] if context.pages else context.new_page()
        _open_with_retries(page, order.url)
        page.wait_for_timeout(2500)

        if not self.is_logged_in(page.content()):
            raise NotAuthorized("Сессия FL.ru слетела — запусти: fbid login fl")

        field = self._open_chat(page, order)
        if field is None:
            return False

        field.fill(text)
        page.wait_for_timeout(600)
        field.press(self.CHAT_SEND_KEY)
        page.wait_for_timeout(4000)
        return self._message_is_in_the_thread(page, order, text)

    def _open_chat(self, page, order: Order):
        """Открывает чат по заказу и отдаёт поле ввода. None — чата нет.

        Окно может быть уже открыто (например, после предыдущей отправки),
        поэтому сначала смотрим поле, и только потом жмём кнопку.
        """
        field = page.query_selector(self.CHAT_FIELD)
        if field is not None:
            return field

        opener = page.query_selector(self.CHAT_OPEN)
        if opener is None:
            logger.error(
                "[fl] {} — кнопки «Чат» на странице нет: заказ в архиве или отклик снят. "
                "Ответ НЕ отправлен",
                order.ext_id,
            )
            return None
        opener.click()
        try:
            page.wait_for_selector(self.CHAT_FIELD, timeout=self.CHAT_OPEN_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — окно не открылось, отправлять некуда
            logger.error("[fl] {} — чат не открылся: {}", order.ext_id, exc)
            return None
        return page.query_selector(self.CHAT_FIELD)

    def _message_is_in_the_thread(self, page, order: Order, text: str) -> bool:
        """Видно ли наше сообщение в ленте чата после отправки."""
        probe = " ".join(text.split())[: self.PROOF_CHARS]
        if not probe:
            return False
        try:
            thread = " ".join(page.inner_text(self.CHAT_THREAD).split())
        except Exception as exc:  # noqa: BLE001 — не прочитали, значит не подтвердили
            logger.warning("[fl] {} — ленту чата после отправки не прочитал: {}", order.ext_id, exc)
            return False
        if probe in thread:
            return True
        logger.error(
            "[fl] {} — своего сообщения в ленте чата не вижу: считаю, что оно НЕ ушло",
            order.ext_id,
        )
        return False

    # --- Сессия ------------------------------------------------------------

    def read_package(self, context) -> int | None:
        """Сколько откликов осталось в пакете. None — прочитать не удалось.

        На FL отклик стоит не рублей, а единицы из купленного пакета, и его
        остаток до сих пор никто не читал: на Profi баланс проверяется перед
        каждым списанием, а здесь мы узнавали о конце пакета только по тому,
        что отклик не появился на странице заказа — то есть задним числом и
        уже потратив попытку.

        None трактуется как «может и не быть» — по тому же правилу, что и
        баланс Profi: молча считать нулём или бесконечностью одинаково плохо.
        """
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _open_with_retries(page, PAID_URL)
            page.wait_for_timeout(4000)
            # Слетевшую сессию надо назвать своим именем. Гостю страница
            # платных услуг числа не покажет, и «остаток не прочитался»
            # выглядело как временная заминка: отклики вставали намертво, а
            # владельца никто не звал войти заново.
            html = page.content()
            if not _page_loaded(html):
                # Страница платных услуг не отрисовалась. Ноль отсюда означал
                # бы «пакет пуст» и остановил бы все отклики до покупки —
                # а покупать, возможно, нечего.
                logger.warning("[fl] страница платных услуг не отрисовалась")
                return None
            if not self.is_logged_in(html):
                raise NotAuthorized("Сессия FL.ru слетела — запусти: fbid login fl")
            # Убеждаемся, что мы там, куда шли: «Купить отклики» встречается и
            # в шапке других страниц, и вывод «пакет пуст» по чужой странице
            # остановил бы отклики на ровном месте.
            if PAID_URL.rstrip("/") not in str(getattr(page, "url", "") or PAID_URL):
                logger.warning("[fl] после перехода оказались не на странице платных услуг")
                return None
            return parse_package(page.inner_text("body"))
        except NotAuthorized:
            raise
        except Exception as exc:  # noqa: BLE001 — не прочитали, значит не знаем
            logger.warning("[fl] остаток пакета откликов не прочитался: {}", exc)
            return None

    @staticmethod
    def is_logged_in(page_html: str) -> bool:
        """Сначала ищем признак ВОШЕДШЕГО, и только потом — признаки гостя.

        Прямой признак один и однозначный: в меню авторизованного есть ссылка
        «Выйти из аккаунта». У гостя её нет никогда.

        Раньше решение принималось только по признакам гостя, и в них входили
        куски адресов /login и /registration. Оба оказались ложными: /login
        приезжает в ТЕКСТЕ чужого заказа (клиент дал ссылку на настройки
        профиля FL), а кнопка /registration висит в мобильном блоке и у
        авторизованного тоже. Живая сессия объявлялась слетевшей — ровно тот
        сбой, из-за которого владелец после настоящего входа на биржу видел
        «сессия слетела» и заходил заново по кругу.

        Подписи кнопок остались запасным признаком, и по-прежнему нужны обе
        сразу: гостевая шапка показывает и «Вход», и «Регистрация», а заказ,
        где в тексте окажутся обе — каждая ровно между закрывающей и
        открывающей скобкой, — не встречается.

        Не нашли ни того ни другого — считаем, что сессия жива: пустая или
        недорисованная страница не повод будить владельца тревогой.
        """
        html = page_html or ""
        if LOGOUT_MARK in html:
            return True
        entrance = ">Вход<" in html
        signup = ">Регистрация<" in html
        return not (entrance and signup)


# Страница платных услуг: там же лежит остаток купленного пакета откликов.
PAID_URL = "https://www.fl.ru/payed/"
_PACKAGE_RE = re.compile(r"У\s+вас\s+(\d+)\s+отклик", re.IGNORECASE)


def parse_package(page_text: str) -> int | None:
    """Сколько откликов осталось в купленном пакете. None — прочитать не вышло.

    Формулировка снята с живой страницы 09.08.2026: «Отклики на заказы
    У вас 4 отклика.» Число склоняется («1 отклик», «4 отклика», «10 откликов»),
    поэтому ловим корень.

    Пустой пакет — отдельный случай, и числа там нет вовсе: когда отклики
    кончились, страница (проверено вживую 22.08.2026) вовсе не показывает
    «У вас…» — только раздел «Отклики на заказы» с кнопкой «Купить отклики».
    Раздел прогрузился, числа нет — значит пакет пуст, и это ноль, а не
    «не прочитали»: иначе владельцу неделями сообщалось бы «страница не
    читается», хотя на деле пора покупать пакет.
    """
    flat = " ".join((page_text or "").split())
    match = _PACKAGE_RE.search(flat)
    if match:
        return int(match.group(1))
    if "Отклики на заказы" in flat and "Купить отклики" in flat:
        return 0
    return None


def parse_client(page_html: str) -> dict:
    """Что страница заказа рассказывает о заказчике. Пусто — значит не сказала.

    Пустой словарь и «заказчик новичок» — разные вещи. FL.ru мог поменять
    вёрстку, и тогда молчание разметки не должно превращаться в вывод о
    человеке: рейтинг считает только то, что действительно прочитали.
    """
    match = _REGISTERED_RE.search(page_html or "")
    if not match:
        return {}
    registered = html_to_text(match.group(1)).strip()
    return {"client_registered": registered} if registered else {}


def opening_part(text: str, limit: int) -> str:
    """Первые фразы захода — то, что клиент увидит в списке откликов.

    Режем по границе предложения: обрыв на полуслове выглядит как сбой бота.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("\n"))
    if boundary > limit // 3:
        return cut[: boundary + 1].strip()
    return cut.rsplit(" ", 1)[0].strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

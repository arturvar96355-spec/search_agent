"""Модели данных: заказ с биржи и результат отклика."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Source(str, Enum):
    FL = "fl"
    PROFI = "profi"
    YOUDO = "youdo"


class BidStatus(str, Enum):
    SKIPPED = "skipped"      # временный отказ: стоп-кран, лимит, бюджет — можно пробовать позже
    REJECTED = "rejected"    # окончательный отказ: фильтры, вердикт модели, цена —
                             # причина не изменится, повторно заказ не трогаем
    PLANNED = "planned"      # прошёл фильтры, ждёт отправки
    SENT = "sent"            # отклик отправлен
    FAILED = "failed"        # попытка была, но упала (капча, редизайн, нет денег)
    UNAVAILABLE = "unavailable"  # биржа не даёт откликнуться: уже откликались,
                                 # пакет откликов пуст или заказ закрыт
    DRY_RUN = "dry_run"      # прошёл бы, но включён dry_run


class BidUnavailable(RuntimeError):
    """Биржа не даёт откликнуться, и повтор ничего не изменит.

    Живёт здесь, а не в модуле одной площадки: приговор общий, его выносят и
    FL, и Profi, а ловит его отправщик. Пока класс лежал внутри fl.py, вторая
    площадка не могла его выговорить — и «кнопки отклика нет» уходило обычным
    сбоем: заказ возвращался в очередь, каждый проход заново платил модели за
    текст и слал владельцу ту же тревогу.
    """


@dataclass
class SubmitResult:
    """Чем кончилась отправка отклика.

    Раньше площадки возвращали просто True/False, и терялись две важные вещи.

    Первая — сколько на самом деле списали. Цена в базе может быть суточной
    давности, а списывают по той, что была на кнопке в момент клика; в учёт
    же уходила старая, и суточный бюджет считался по ней.

    Вторая — были ли вообще потрачены деньги. После клика по кнопке оплаты
    деньги уходят сразу, а подтверждение может не прийти: медленный прокси,
    оборванный туннель. Такой случай нельзя считать «не отправили» — иначе
    заказ вернётся в очередь и мы заплатим за него второй раз.
    """

    sent: bool
    price: float | None = None
    # Клик по кнопке оплаты состоялся: деньги, скорее всего, уже списаны.
    charged: bool = False
    reason: str = ""


class Order(BaseModel):
    """Заказ с биржи. ext_id уникален в рамках площадки."""

    source: Source
    ext_id: str
    url: str
    title: str
    description: str = ""
    category: str = ""
    # Бюджет заказчика: сырой текст как на бирже + распарсенное число, если вышло.
    budget_raw: str = ""
    budget_value: float | None = None
    # Сколько площадка возьмёт за отклик (Profi.ru). None = бесплатно/неизвестно.
    bid_price: float | None = None
    # Кто разместил заказ: логин или устойчивый отпечаток заказчика на площадке.
    # Пусто, если площадка заказчика не раскрывает — тогда дедуп по клиенту
    # просто не работает, а не срабатывает наугад.
    client_key: str = ""
    published_at: datetime | None = None
    # Полное ТЗ подгружается отдельно (лента отдаёт обрезку) — флаг, что описание полное.
    description_full: bool = False
    raw: dict = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source.value}:{self.ext_id}"


# «(Бюджет: 50 000  ₽)», «Бюджет: 5000 руб.», «по договоренности»
_BUDGET_RE = re.compile(r"бюджет\s*:?\s*([\d\s ]+)", re.IGNORECASE)


def parse_budget(text: str) -> tuple[str, float | None]:
    """Достаёт бюджет из строки. Возвращает (сырой текст, число или None).

    None значит «по договорённости» или бюджет не указан — это НЕ повод считать его нулём,
    иначе фильтр «от 5000 ₽» молча выкинет все переговорные заказы.
    """
    if not text:
        return "", None
    match = _BUDGET_RE.search(text)
    if not match:
        return "", None
    digits = re.sub(r"[^\d]", "", match.group(1))
    if not digits:
        return match.group(0).strip(), None
    return match.group(0).strip(), float(digits)


# Как в ссылке на заказ записан его номер, по площадкам.
# Profi обязателен первым: у всех его заказов один и тот же путь n.php, и
# отличаются они только параметром ?o=<номер>. Наивное сравнение ссылок
# «начало совпало — значит тот же заказ» находило первый попавшийся заказ Profi.
_URL_ID_PATTERNS = (
    (Source.PROFI, re.compile(r"[?&]o=(\d+)")),
    (Source.FL, re.compile(r"/projects/(\d+)")),
    (Source.YOUDO, re.compile(r"/t(\d+)")),
)


def order_ref(url: str) -> tuple[Source, str] | None:
    """Ссылка → (площадка, номер заказа). None — ссылку не узнали."""
    text = url or ""
    for source, pattern in _URL_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return source, match.group(1)
    return None


def strip_budget_suffix(title: str) -> str:
    """Убирает хвост «(Бюджет: … ₽)» из заголовка — он едет отдельным полем."""
    return re.sub(r"\s*\(\s*бюджет\s*:.*?\)\s*$", "", title, flags=re.IGNORECASE).strip()

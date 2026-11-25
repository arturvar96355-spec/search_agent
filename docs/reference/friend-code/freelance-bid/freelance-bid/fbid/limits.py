"""Лимиты, которые владелец меняет из Telegram, не заходя на сервер.

Значение берётся из базы, а если его там нет — из .env. Так правка кнопкой
переживает перезапуск, а .env остаётся тем, к чему всё возвращается по кнопке
«сбросить».

Два правила, ради которых это отдельный модуль:

1. **Границы жёсткие.** У каждого лимита свой потолок, выше которого не пустит
   никакая кнопка. Опечатка в один лишний ноль не должна опустошить счёт.
2. **Сбой чтения не расширяет траты.** Не открылась база — берём значение из
   .env, то есть то, что владелец задал руками. Молча поднять лимит нельзя.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from loguru import logger

from .config import get_settings

# Префикс ключей в таблице state, чтобы лимиты не смешивались со служебным.
PREFIX = "limit:"


@dataclass(frozen=True)
class LimitSpec:
    """Описание одного лимита: как показать, куда упереть, откуда взять базу."""

    key: str
    title: str
    unit: str
    minimum: float
    maximum: float
    integer: bool = False
    # Поле в Settings, откуда берётся значение по умолчанию. Пусто — значение
    # живёт не в .env (например, минимальный бюджет заказчика из filters.yaml).
    settings_field: str = ""
    yaml_default: float | None = None
    hint: str = ""

    def clean(self, raw: float) -> float:
        value = float(raw)
        if self.integer:
            value = float(int(value))
        return max(self.minimum, min(self.maximum, value))

    def format(self, value: float) -> str:
        number = f"{value:.0f}" if self.integer or value == int(value) else f"{value:.1f}"
        return f"{number} {self.unit}".strip()


SPECS: dict[str, LimitSpec] = {
    "profi_daily_budget": LimitSpec(
        key="profi_daily_budget",
        title="Бюджет на отклики Profi в сутки",
        unit="₽",
        minimum=0,
        maximum=20_000,
        settings_field="profi_daily_budget",
        hint="Потратили за сутки столько — автоотклик на Profi встаёт до завтра.",
    ),
    "profi_max_bid_price": LimitSpec(
        key="profi_max_bid_price",
        title="Потолок цены одного отклика",
        unit="₽",
        minimum=0,
        maximum=3_000,
        settings_field="profi_max_bid_price",
        hint="Дороже не платим никогда, каким бы вкусным ни выглядел заказ.",
    ),
    "profi_max_bid_pct": LimitSpec(
        key="profi_max_bid_pct",
        title="Доля бюджета заказа на отклик",
        unit="%",
        minimum=0,
        maximum=20,
        settings_field="profi_max_bid_pct",
        hint="Profi берёт тем больше, чем дороже заказ. Это защита от переплаты на мелочи.",
    ),
    "profi_daily_bids": LimitSpec(
        key="profi_daily_bids",
        title="Откликов на Profi в сутки",
        unit="шт",
        minimum=0,
        maximum=100,
        integer=True,
        settings_field="profi_daily_bids",
    ),
    "fl_daily_bids": LimitSpec(
        key="fl_daily_bids",
        title="Откликов на FL в сутки",
        unit="шт",
        minimum=0,
        maximum=100,
        integer=True,
        settings_field="fl_daily_bids",
    ),
    "profi_min_balance": LimitSpec(
        key="profi_min_balance",
        title="Неснижаемый остаток на Profi",
        unit="₽",
        minimum=0,
        maximum=50_000,
        settings_field="profi_min_balance",
        hint="0 — тратим до нуля.",
    ),
    "auto_reply_max_price": LimitSpec(
        key="auto_reply_max_price",
        title="До какой суммы модель отвечает клиенту сама",
        unit="₽",
        minimum=0,
        maximum=500_000,
        yaml_default=45_000,
        hint=(
            "Дешевле — модель ведёт переписку до готовности созвониться. "
            "Дороже и договорные — только уведомление, разговор ведёте вы. "
            "0 — модель не отвечает никому."
        ),
    ),
    "min_budget": LimitSpec(
        key="min_budget",
        title="Минимальный бюджет заказчика",
        unit="₽",
        minimum=0,
        maximum=1_000_000,
        yaml_default=20_000,
        hint="Ниже этой суммы заказ не берём и в Telegram не шлём.",
    ),
    "max_budget": LimitSpec(
        key="max_budget",
        title="Максимальный бюджет заказчика",
        unit="₽",
        minimum=0,
        maximum=100_000_000,
        yaml_default=170_000,
        hint="Выше этой суммы заказ не берём: это тендер, а не наш клиент.",
    ),
    "hot_score_min": LimitSpec(
        key="hot_score_min",
        title="Рейтинг для мгновенного отклика",
        unit="из 10",
        minimum=0,
        maximum=10,
        settings_field="hot_score_min",
        hint="Набрал столько — откликаемся сразу. Ниже — ждёт вечернего добора.",
    ),
}


def default_for(key: str) -> float:
    """Значение из .env (или из правил), к которому возвращает кнопка «сбросить»."""
    spec = SPECS[key]
    if spec.settings_field:
        return float(getattr(get_settings(), spec.settings_field))
    if key in ("min_budget", "max_budget"):
        # Эти пороги живут в filters.yaml. Импорт локальный: filters сам
        # обращается к лимитам, и на уровне модуля вышел бы круг.
        from .filters import yaml_budget

        value = yaml_budget(key)
        if value is not None:
            return float(value)
        # В файле порога нет — значит фильтр по бюджету не режет вовсе, и
        # показывать вместо этого заводское число нельзя. Так бот сообщал
        # «Минимальный бюджет: 20 000 ₽» там, где на самом деле проходили
        # заказы за пятьсот рублей, и владелец искал ошибку в фильтрах.
        return 0.0
    if spec.yaml_default is not None:
        return float(spec.yaml_default)
    return 0.0


def override_value(key: str, store=None) -> float | None:
    """Правка из бота, если она есть. None — значит действует значение из .env."""
    raw = _read_override(key, store)
    if raw is None:
        return None
    try:
        return SPECS[key].clean(float(raw))
    except (TypeError, ValueError):
        logger.warning("Битое значение лимита {} в базе: {!r} — беру из .env", key, raw)
        return None


def get(key: str, store=None) -> float:
    """Действующее значение лимита: правка из бота или значение из .env."""
    value = override_value(key, store)
    return default_for(key) if value is None else value


# Пороги, которых может не быть вовсе: они живут в filters.yaml, и там их
# законно оставить пустыми — тогда фильтр по бюджету не режет.
OPTIONAL = ("min_budget", "max_budget")


def in_force(key: str, store=None) -> float | None:
    """Действующее значение или None, если порога нет вовсе.

    Нужно экрану лимитов, чтобы не путать две разные вещи. «Не задан» и
    «задан нулём» ведут себя противоположно: при пустом max_budget фильтр не
    режет ничего, при нулевом — режет ВСЁ. Показывать их одинаково нельзя, а
    именно так и получалось, когда «не задан» решалось по нулю: владелец
    ставил ноль кнопкой, видел «не задан» и потом искал, почему ни один заказ
    не проходит.
    """
    override = override_value(key, store)
    if override is not None:
        return override
    if key in OPTIONAL:
        from .filters import yaml_budget

        return yaml_budget(key)
    return get(key, store)


def display(key: str, store=None) -> str:
    """Значение лимита для экрана бота, словами."""
    value = in_force(key, store)
    return "не задан" if value is None else SPECS[key].format(value)


def overridden(key: str, store=None) -> bool:
    """Правился ли лимит из бота.

    Именно через override_value, а не по наличию записи: битое значение в базе
    не считается правкой, потому что get() его всё равно игнорирует. Иначе бот
    рисовал бы «изменено ✏️» рядом со значением из .env.
    """
    return override_value(key, store) is not None


def set_value(store, key: str, raw: str | float) -> float:
    """Записывает новое значение. Возвращает то, что реально применилось."""
    spec = SPECS[key]
    value = spec.clean(to_number(raw))
    store.set_state(PREFIX + key, repr(value))
    logger.info("Лимит {} изменён из Telegram: {}", key, spec.format(value))
    return value


def reset(store, key: str) -> float:
    """Убирает правку: лимит снова берётся из .env."""
    store.delete_state(PREFIX + key)
    logger.info("Лимит {} сброшен к значению из .env", key)
    return default_for(key)


def snapshot(store=None) -> dict[str, tuple[float, bool]]:
    """Все лимиты разом: ключ → (значение, правился ли из бота)."""
    return {key: (get(key, store), overridden(key, store)) for key in SPECS}


def to_number(raw: str | float) -> float:
    """«1 500 ₽», «1500», «1,5» → число. Иначе ValueError с понятным текстом."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    text = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    if not text or text in ("-", ".", "-."):
        raise ValueError("не похоже на число")
    return float(text)


def _read_override(key: str, store=None):
    if key not in SPECS:
        raise KeyError(f"неизвестный лимит: {key}")
    if store is not None:
        return store.get_state(PREFIX + key)
    return _read_standalone(PREFIX + key)


def _read_standalone(state_key: str):
    """Читает лимит своим соединением — для мест, где Store под рукой нет.

    Открывать базу на каждое чтение дешевле, чем держать кэш: устаревший кэш
    здесь означал бы, что кнопка в боте нажата, а траты идут по-старому.
    """
    path = get_settings().db_path
    if not path.exists():
        return None
    try:
        # as_uri() экранирует пробелы и кириллицу в пути: без него подключение
        # к базе, лежащей в каталоге с пробелом, молча падало бы на .env-значения.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        logger.debug("Лимиты: база недоступна ({}) — беру значения из .env", exc)
        return None
    try:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (state_key,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error as exc:
        logger.debug("Лимиты: не прочитал {} ({}) — беру из .env", state_key, exc)
        return None
    finally:
        conn.close()

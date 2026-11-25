"""Движок фильтров: решает, стоит ли откликаться на заказ.

Правила лежат в filters.yaml и перечитываются на каждом проходе — правка файла
не требует перезапуска.

Два принципа, на которых всё держится:

1. **Fail-closed.** Любая неопределённость трактуется как «не откликаться».
   Не смогли прочитать цену отклика, не поняли дату, сломался файл правил —
   заказ пропускается. Пропущенный заказ стоит ноль, лишний отклик стоит денег.

2. **Потолки из .env главнее.** filters.yaml может ужесточить лимит цены,
   но не поднять его выше PROFI_MAX_BID_PRICE. Иначе правка правил втихую
   расширила бы разрешённые траты.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from loguru import logger

from . import limits
from .config import PROJECT_ROOT
from .models import Order, Source

DEFAULT_FILTERS_PATH = PROJECT_ROOT / "filters.yaml"


def yaml_budget(key: str, path: Path | None = None) -> float | None:
    """Порог бюджета прямо из filters.yaml, без учёта правок из бота.

    Нужен модулю лимитов, чтобы показать, к чему вернёт кнопка «сбросить».
    Ключ обязателен: раньше функция знала только про нижнюю границу, и сброс
    верхней вернул бы значение нижней.
    """
    path = path or DEFAULT_FILTERS_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return None
    value = (raw.get("common") or {}).get(key)
    return float(value) if value is not None else None


@dataclass(frozen=True)
class Decision:
    """Решение по заказу. reason всегда заполнен — он едет в лог и в Telegram."""

    passed: bool
    reason: str
    # Окончательный ли отказ. Категория и текст заказа не меняются — это
    # навсегда. А вот «цена не прочиталась» бывает из-за обрыва прокси, и
    # хоронить из-за этого нормальный заказ нельзя.
    permanent: bool = True

    def __bool__(self) -> bool:
        return self.passed


@dataclass
class SourceRules:
    enabled: bool = True
    categories_allow: list[str] = field(default_factory=list)
    categories_deny: list[str] = field(default_factory=list)
    allow_by_keywords: bool = True
    skip_vacancies: bool = True
    max_bid_price: float | None = None
    # Доля от бюджета заказчика в процентах. None — берём значение из .env.
    max_bid_pct: float | None = None
    max_bid_rank: int | None = None
    require_phone_confirmed: bool = False
    # Брать только удалённые задания. Нужно на Юду: там половина доски — это
    # курьеры и уборка «по адресу», куда мы физически не поедем.
    require_remote: bool = False


@dataclass
class CommonRules:
    max_age_hours: float | None = 24
    allow_negotiable: bool = True
    min_budget: float | None = None
    # Выше этой суммы заказ не берём. Крупный бюджет означает не «много денег
    # нам», а тендер: заказчик собирает предложения от студий с портфолио под
    # такие проекты, отдел продаж и юристов. Отклик там уходит в никуда, а на
    # Profi.ru ещё и стоит дороже всего — цена контакта растёт вместе с бюджетом.
    max_budget: float | None = None
    stop_words: list[str] = field(default_factory=list)
    keywords_any: list[str] = field(default_factory=list)
    # Роли и виды работ, которыми мы не занимаемся. Проверяются ТОЛЬКО по
    # заголовку и категории: «дизайн уже готов» в описании не должно убивать
    # заказ на разработку, а «Веб-дизайнер» в заголовке — должно.
    deny_roles: list[str] = field(default_factory=list)


class FilterSet:
    def __init__(self, common: CommonRules, per_source: dict[str, SourceRules]) -> None:
        self.common = common
        self.per_source = per_source

    # --- Загрузка ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "FilterSet":
        path = path or DEFAULT_FILTERS_PATH
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            logger.warning("{} не найден — беру правила по умолчанию", path.name)
            raw = {}
        except yaml.YAMLError as exc:
            # Сломанный YAML не должен превратиться в «фильтров нет, шлём всем».
            raise ValueError(f"Не разобрал {path.name}: {exc}") from exc

        common_raw = raw.get("common") or {}
        # Минимальный бюджет владелец правит из Telegram. Правка перебивает файл:
        # иначе кнопка в боте молча ничего бы не делала.
        min_budget = limits.override_value("min_budget")
        if min_budget is None:
            min_budget = common_raw.get("min_budget")
        max_budget = limits.override_value("max_budget")
        if max_budget is None:
            max_budget = common_raw.get("max_budget")

        common = CommonRules(
            max_age_hours=common_raw.get("max_age_hours", 24),
            allow_negotiable=bool(common_raw.get("allow_negotiable", True)),
            min_budget=min_budget,
            max_budget=max_budget,
            stop_words=[w.lower() for w in (common_raw.get("stop_words") or [])],
            keywords_any=[w.lower() for w in (common_raw.get("keywords_any") or [])],
            deny_roles=[w.lower() for w in (common_raw.get("deny_roles") or [])],
        )

        per_source: dict[str, SourceRules] = {}
        for source in Source:
            section = raw.get(source.value) or {}
            per_source[source.value] = SourceRules(
                enabled=bool(section.get("enabled", True)),
                categories_allow=list(section.get("categories_allow") or []),
                categories_deny=list(section.get("categories_deny") or []),
                allow_by_keywords=bool(section.get("allow_by_keywords", True)),
                skip_vacancies=bool(section.get("skip_vacancies", True)),
                max_bid_price=section.get("max_bid_price"),
                max_bid_pct=section.get("max_bid_pct"),
                max_bid_rank=section.get("max_bid_rank"),
                require_phone_confirmed=bool(section.get("require_phone_confirmed", False)),
                require_remote=bool(section.get("require_remote", False)),
            )
        return cls(common, per_source)

    # --- Решение ----------------------------------------------------------

    def decide(self, order: Order, *, now: datetime | None = None) -> Decision:
        rules = self.per_source.get(order.source.value, SourceRules())
        if not rules.enabled:
            return Decision(False, f"площадка {order.source.value} выключена в filters.yaml")

        # Категорию НЕ кладём в текст для ключевых слов: иначе «Продвижение сайтов»
        # само себя протаскивает по слову «сайт». Категорию судят category_allow/deny.
        haystack = f"{order.title}\n{order.description}"

        stop = _first_match(haystack, self.common.stop_words)
        if stop:
            return Decision(False, f"стоп-слово «{stop}»")

        # Роль ищем в заголовке и категории — там она и объявляется.
        role_field = f"{order.title}\n{order.category}"
        role = _first_match(role_field, self.common.deny_roles)
        if role:
            return Decision(False, f"не наш профиль: «{role}»")

        if rules.skip_vacancies and order.raw.get("is_vacancy"):
            return Decision(False, "это вакансия в штат, а не заказ")

        if _category_denied(order.category, rules):
            # Запрет категории абсолютен: ключевые слова его не перебивают.
            return Decision(False, f"категория «{order.category}» в чёрном списке")

        category_ok = _category_allowed(order.category, rules)
        keyword = _first_match(haystack, self.common.keywords_any)
        if not category_ok and not (rules.allow_by_keywords and keyword):
            return Decision(False, f"категория «{order.category or '—'}» не наша")

        # Сравниваем с None явно: у Decision переопределён __bool__ (он равен passed),
        # поэтому `if budget:` молча пропускал бы КАЖДЫЙ отказ, включая лимит по деньгам.
        budget = self._check_budget(order)
        if budget is not None:
            return budget

        age = self._check_age(order, now=now)
        if age is not None:
            return age

        money = self._check_money(order, rules)
        if money is not None:
            return money

        rank = order.raw.get("bid_rank")
        if rules.max_bid_rank is not None and rank is not None and rank > rules.max_bid_rank:
            return Decision(False, f"отклик будет {rank}-м, лимит {rules.max_bid_rank}")

        if rules.require_phone_confirmed and not order.raw.get("phone_confirmed"):
            return Decision(False, "у заказчика не подтверждён телефон")

        if rules.require_remote and not order.raw.get("remote"):
            return Decision(False, "задание не удалённое — ехать на адрес мы не будем")

        why = f"категория «{order.category}»" if category_ok else f"ключевое слово «{keyword}»"
        return Decision(True, f"подходит: {why}")

    # --- Отдельные проверки ----------------------------------------------

    def _check_budget(self, order: Order) -> Decision | None:
        if order.budget_value is None:
            if not self.common.allow_negotiable:
                return Decision(False, "бюджет не указан, а договорные выключены")
            return None
        if self.common.min_budget is not None and order.budget_value < self.common.min_budget:
            return Decision(
                False,
                f"бюджет {order.budget_value:.0f} ₽ ниже минимума {self.common.min_budget:.0f} ₽",
            )
        if self.common.max_budget is not None and order.budget_value > self.common.max_budget:
            return Decision(
                False,
                f"бюджет {order.budget_value:.0f} ₽ выше потолка "
                f"{self.common.max_budget:.0f} ₽ — это тендер, отклик уйдёт в никуда",
            )
        return None

    def _check_age(self, order: Order, *, now: datetime | None = None) -> Decision | None:
        if self.common.max_age_hours is None:
            return None
        if order.published_at is None:
            # Дату не разобрали — считаем заказ свежим, но это не денежное решение:
            # на деньги влияет только _check_money, и там fail-closed.
            return None
        now = now or datetime.now(timezone.utc)
        published = order.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_hours = (now - published).total_seconds() / 3600
        if age_hours > self.common.max_age_hours:
            return Decision(False, f"заказу {age_hours:.0f} ч, лимит {self.common.max_age_hours} ч")
        return None

    def _check_money(self, order: Order, rules: SourceRules) -> Decision | None:
        """Платные отклики. Здесь fail-closed строже всего."""
        if order.source is not Source.PROFI:
            return None

        # Бюджет заказчика, а если он не указан — наша оценка работы
        # (её кладёт bidder после генерации захода).
        budget = order.budget_value or order.raw.get("estimated_budget")
        limit = self.effective_max_bid_price(rules, budget)
        if order.bid_price is None:
            # Цены нет по двум разным причинам, и относиться к ним надо
            # по-разному. Заказ скрыт или снят — возвращаться незачем.
            # Причина неизвестна — скорее всего страница не догрузилась,
            # и на следующем проходе цена прочитается.
            reason = order.raw.get("no_price_reason") or ""
            if reason:
                return Decision(False, reason, permanent=True)
            return Decision(
                False,
                "цена отклика не прочиталась — не рискую деньгами",
                permanent=False,
            )
        if order.bid_price > limit:
            share = f" ({order.bid_price / budget * 100:.1f}% от бюджета)" if budget else ""
            return Decision(
                False,
                f"отклик стоит {order.bid_price:.0f} ₽, потолок {limit:.0f} ₽{share}",
            )
        return None

    @staticmethod
    def effective_max_bid_price(rules: SourceRules, budget: float | None = None) -> float:
        """Сколько максимум готовы отдать за отклик по конкретному заказу.

        Минимум из трёх величин: абсолютный потолок из .env, потолок из
        filters.yaml (может только ужесточить) и доля от бюджета заказчика.

        Доля нужна потому, что Profi.ru берёт за отклик тем больше, чем дороже
        заказ. Плоский потолок пропускал отклики за 380 ₽ на заказах за 2 000 ₽
        и блокировал отклик за 650 ₽ на заказе за 50 000 ₽ — работал наоборот.
        """
        limit = limits.get("profi_max_bid_price")
        if rules.max_bid_price is not None:
            limit = min(limit, float(rules.max_bid_price))

        pct = (
            rules.max_bid_pct
            if rules.max_bid_pct is not None
            else limits.get("profi_max_bid_pct")
        )
        # Ноль — это «не отдаём ни доли бюджета», то есть запрет, а не
        # «правила нет». Раньше нулевая доля просто не проходила проверку на
        # истинность, процентный потолок отключался целиком, и оставался один
        # абсолютный: владелец опускал долю до нуля, чтобы перестать
        # переплачивать на мелких заказах, а софт после этого начинал тратить
        # БОЛЬШЕ. Кнопка в боте разрешает ставить ноль, так что случай живой.
        if budget and pct is not None:
            limit = min(limit, budget * pct / 100.0)
        return limit


_TOKEN_RE = re.compile(r"[a-zа-я0-9]+")
# Знаки, которые токенизатор выбрасывает, а в названиях технологий они значимы:
# «c#», «.net», «ci/cd», «next.js», «з/п», «c++».
_NEEDS_RAW_RE = re.compile(r"[^a-zа-я0-9]")


# Латиница, написанная кириллицей, и наоборот. На биржах это норма, а не
# редкость: «3Д-моделирование» пишут русской «Д», в названиях мешают «с» и
# «c», «а» и «a». Список запретов из-за этого промахивался мимо: заказ
# «3Д-моделирование миниатюр» прошёл фильтр «3d-модел» и получил платный
# отклик — за работу, которую студия не делает.
#
# Приводим к одному виду обе стороны сравнения, поэтому чисто русские слова
# («сайт» → «caйт») продолжают совпадать сами с собой.
_LOOKALIKE = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ѕ": "s",
    # Не омоглиф, а способ записи: «3Д» — это «3D» по-русски.
    "д": "d",
})


def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е").translate(_LOOKALIKE)


def matches(text: str, needle: str) -> bool:
    """Совпадение по началу слова, а НЕ по подстроке.

    Наивный `"бот" in text` ловит «раз-работ-ать», «обработка», «работа», а «сайт» —
    категорию «Продвижение сайтов». На этом автоотклик уходил на логотипы и таргет.

    Поэтому: текст режется на слова, и ключевое слово должно совпасть с НАЧАЛОМ слова.
    Так «автоматизац» ловит «автоматизацию» и «автоматизации», но «бот» не ловит
    «разработать». Фразы с пробелом («в штат») проверяются как подстрока — там
    границы слов уже заданы самой фразой.
    """
    needle = normalize(needle).strip()
    if not needle:
        return False
    text = normalize(text)
    # Дефис — такой же разделитель, как пробел: «контент-менеджер» это фраза
    # из двух слов. Без этого она не находилась вообще, потому что токенизатор
    # режет текст по дефису, и целого токена «контент-менеджер» не существует.
    if " " in needle or "-" in needle:
        return needle in text or needle.replace("-", " ") in text

    # Слово со знаком внутри токенизатор разрезает: «c#» превращается в «c»,
    # «з/п» — в «з» и «п», «next.js» — в «next» и «js». Совпасть с началом
    # целого токена такое слово не может никогда, и шесть правил из
    # filters.yaml (.net, c#, ci/cd, make.com, next.js, з/п) не срабатывали
    # вовсе. «з/п» стоит в запретах — по нему отсеиваются вакансии, и каждая
    # непойманная стоила нам платного отклика.
    if _NEEDS_RAW_RE.search(needle):
        # Слева требуем границу, только если само слово начинается с буквы или
        # цифры: «c#» не должно находиться внутри «cc#», а «.net» обязано
        # находиться в «asp.net» — это тот же .NET.
        head = r"(?<![a-zа-я0-9])" if _TOKEN_RE.match(needle) else ""
        return re.search(head + re.escape(needle), text) is not None

    return any(token.startswith(needle) for token in _TOKEN_RE.findall(text))


def _first_match(haystack: str, needles: list[str]) -> str | None:
    return next((needle for needle in needles if matches(haystack, needle)), None)


def _category_denied(category: str, rules: SourceRules) -> bool:
    if not category:
        return False
    normalized = normalize(category).strip()
    return any(normalized.startswith(normalize(deny).strip()) for deny in rules.categories_deny)


def _category_allowed(category: str, rules: SourceRules) -> bool:
    """Совпадение по верхнему разделу («Программирование») или по полной категории."""
    if not category:
        return False
    normalized = normalize(category).strip()
    return any(normalized.startswith(normalize(allow).strip()) for allow in rules.categories_allow)

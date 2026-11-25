"""Тесты движка фильтров.

Отдельный акцент на двух вещах, которые стоят денег:
  * ключевые слова не должны срабатывать на кусок другого слова;
  * без известной цены отклика на Profi.ru решение всегда «нет».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fbid.filters import CommonRules, FilterSet, SourceRules, matches
from fbid.models import Order, Source


def make_order(**kwargs) -> Order:
    data = {
        "source": Source.FL,
        "ext_id": "1",
        "url": "https://www.fl.ru/projects/1/x.html",
        "title": "Заказ",
        "description": "",
        "category": "Программирование / Веб-программирование",
        "published_at": datetime.now(timezone.utc),
    }
    data.update(kwargs)
    return Order(**data)


def make_filters(**common) -> FilterSet:
    base = {
        "stop_words": ["вакансия", "в штат"],
        "keywords_any": ["бот", "сайт", "автоматизац", "crm"],
        "max_age_hours": 24,
    }
    base.update(common)
    rules = SourceRules(
        categories_allow=["Программирование", "Сайты"],
        categories_deny=["Дизайн", "Реклама и Маркетинг"],
    )
    profi_rules = SourceRules(categories_allow=["Программист"], max_bid_price=300)
    return FilterSet(CommonRules(**base), {"fl": rules, "profi": profi_rules})


# --- Матчинг слов: тут жил баг --------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["Разработать логотип", "Техническое задание на работы", "Обработка фотографий"],
)
def test_keyword_bot_does_not_match_inside_other_words(text):
    """«бот» не должен находиться в «разработать», «работы», «обработка»."""
    assert matches(text, "бот") is False


@pytest.mark.parametrize("text", ["Нужен бот", "Телеграм-бот для записи", "Боты под ключ"])
def test_keyword_bot_matches_real_bots(text):
    assert matches(text, "бот") is True


def test_prefix_matches_word_forms():
    """«автоматизац» должно ловить «автоматизацию» и «автоматизации»."""
    assert matches("Нужна автоматизация склада", "автоматизац") is True
    assert matches("по автоматизации процессов", "автоматизац") is True


def test_phrase_with_space_matches_as_substring():
    assert matches("Ищем разработчика в штат компании", "в штат") is True


def test_yo_is_normalized():
    assert matches("складской учёт", "учет") is True


# --- Категории -------------------------------------------------------------

def test_denied_category_beats_keywords():
    """Слово «сайт» в тексте не должно протаскивать заказ из «Дизайна»."""
    order = make_order(category="Дизайн / Логотипы", title="Логотип для сайта")
    decision = make_filters().decide(order)
    assert decision.passed is False
    assert "чёрном списке" in decision.reason


def test_allowed_category_passes():
    assert make_filters().decide(make_order()).passed is True


def test_foreign_category_passes_by_keyword():
    """Заказчик положил заказ не в тот раздел — ловим по ключевому слову."""
    order = make_order(category="Аутсорсинг и консалтинг / Прочее", title="Нужен телеграм-бот")
    decision = make_filters().decide(order)
    assert decision.passed is True
    assert "ключевое слово" in decision.reason


def test_category_in_text_does_not_self_match():
    """Категория не участвует в поиске ключевых слов — иначе «Продвижение сайтов» пройдёт само."""
    order = make_order(category="Продвижение сайтов (SEO, GEO) / Контекстная реклама",
                       title="Настроить рекламу", description="Кампании в Директе")
    assert make_filters().decide(order).passed is False


# --- Стоп-слова, бюджет, свежесть -----------------------------------------

def test_stop_word_rejects():
    order = make_order(title="Разработчик Python в штат")
    decision = make_filters().decide(order)
    assert decision.passed is False
    assert "стоп-слово" in decision.reason


def test_negotiable_allowed_by_default():
    assert make_filters().decide(make_order(budget_value=None)).passed is True


def test_negotiable_can_be_disabled():
    filters = make_filters(allow_negotiable=False)
    assert filters.decide(make_order(budget_value=None)).passed is False


def test_min_budget_does_not_touch_negotiable():
    """Минимальный бюджет режет только заказы с указанной суммой."""
    filters = make_filters(min_budget=5000)
    assert filters.decide(make_order(budget_value=1000)).passed is False
    assert filters.decide(make_order(budget_value=None)).passed is True


def test_old_order_rejected():
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    decision = make_filters().decide(make_order(published_at=old))
    assert decision.passed is False
    assert "лимит 24 ч" in decision.reason


# --- Деньги на Profi.ru ----------------------------------------------------

def profi_order(**kwargs) -> Order:
    data = {
        "source": Source.PROFI,
        "ext_id": "500",
        "url": "https://profi.ru/backoffice/n.php?o=500",
        "title": "Программист",
        "category": "Программист",
        "description": "Нужен сайт",
        "published_at": datetime.now(timezone.utc),
    }
    data.update(kwargs)
    return Order(**data)


def test_unknown_bid_price_is_rejected():
    """Цена отклика неизвестна — не откликаемся. Это главный предохранитель."""
    decision = make_filters().decide(profi_order(bid_price=None))
    assert decision.passed is False
    assert "не прочиталась" in decision.reason


def test_unknown_price_rejection_is_temporary():
    """Цену могло не прочитать из-за обрыва прокси — заказ не хороним навсегда."""
    decision = make_filters().decide(profi_order(bid_price=None))
    assert decision.permanent is False


def test_hidden_order_rejection_is_permanent():
    """Заказ скрыт клиентом — цены нет по делу, возвращаться к нему незачем.

    Раньше это выглядело так же, как обрыв прокси, и заказ бесконечно
    ходил по кругу «попробуем позже».
    """
    order = profi_order(bid_price=None)
    order.raw["no_price_reason"] = "заказ скрыт — клиент пока не принимает отклики"
    decision = make_filters().decide(order)

    assert decision.passed is False
    assert decision.permanent is True
    assert "скрыт" in decision.reason


def test_category_rejection_is_permanent():
    """Категория заказа не изменится — повторно его не трогаем."""
    decision = make_filters().decide(make_order(category="Дизайн / Логотипы", title="Логотип"))
    assert decision.passed is False
    assert decision.permanent is True


def test_bid_price_above_limit_rejected():
    """Потолок из filters.yaml (300 ₽) ужесточает абсолютный из .env."""
    decision = make_filters().decide(profi_order(bid_price=350))
    assert decision.passed is False
    assert "потолок" in decision.reason


def test_bid_price_within_limit_passes():
    assert make_filters().decide(profi_order(bid_price=42)).passed is True


def test_percentage_cap_blocks_expensive_bid_on_cheap_order():
    """Отклик за 380 ₽ на заказе за 2 000 ₽ — это 19% от бюджета, не берём."""
    decision = make_filters().decide(profi_order(bid_price=380, budget_value=2000))
    assert decision.passed is False
    assert "% от бюджета" in decision.reason


def test_yaml_cannot_raise_limit_above_env(monkeypatch):
    """filters.yaml может только ужесточить потолок, поднять выше .env нельзя."""
    from fbid import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("PROFI_MAX_BID_PRICE", "300")
    try:
        rules = SourceRules(max_bid_price=1000)  # попытка поднять лимит из yaml
        assert FilterSet.effective_max_bid_price(rules) == 300
    finally:
        config.get_settings.cache_clear()


def test_vacancy_rejected():
    order = profi_order(bid_price=10, raw={"is_vacancy": True})
    decision = make_filters().decide(order)
    assert decision.passed is False
    assert "вакансия" in decision.reason


# --- Фильтр по роли: мы пишем код, а не рисуем и не пишем тексты -----------

ROLE_FILTERS = FilterSet(
    CommonRules(
        keywords_any=["сайт", "бот", "crm"],
        max_age_hours=None,
        deny_roles=[
            "контент-менеджер", "создание контента", "копирайт", "переводчик",
            "дизайн", "дизайнер", "видеомонтаж", "smm", "таргет", "продвижение",
            "seo", "наложение изображ", "каптч", "помочь зарегистр", "репетитор",
        ],
    ),
    {"profi": SourceRules(categories_allow=["Программист", "Разработка"])},
)


def _role_order(title: str, category: str = "") -> Order:
    return Order(
        source=Source.PROFI, ext_id="1", url="x", title=title,
        category=category or title, description="нужна работа по проекту",
        bid_price=40.0, budget_value=30000,
        published_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "title",
    [
        "Контент-менеджер",
        "Вэб дизайнер",
        "Создание контента для ютуб/Рутуб/Дзен",
        "Разработка дизайна игры",
        "Устный переводчик",
        "Комплексное продвижение телеграм-канала",
        "Решение каптч на формах stripe",
        "Наложение изображений на готовую графику",
        "Помочь зарегистрировать textnow",
    ],
)
def test_not_our_roles_are_rejected(title):
    """Мы пишем код и собираем системы. Контент, дизайн, SMM и переводы — чужая работа."""
    decision = ROLE_FILTERS.decide(_role_order(title))
    assert decision.passed is False, f"{title} не должен проходить"
    assert "не наш профиль" in decision.reason


@pytest.mark.parametrize(
    "title",
    [
        "Редизайн корпоративного сайта на WordPress",
        "Написать бота для товарно-сырьевой биржи",
        "Разработка на Yii",
        "Поддержка сайтов на Bitrix",
        "Настройка CRM и интеграция с телефонией",
    ],
)
def test_our_development_orders_pass(title):
    """Разработка проходит, даже если в тексте мелькает слово «дизайн»."""
    assert ROLE_FILTERS.decide(_role_order(title, "Программист")).passed is True


def test_design_in_description_does_not_kill_development_order():
    """«Дизайн уже готов» в описании — это не заказ на дизайн."""
    order = Order(
        source=Source.PROFI, ext_id="2", url="x",
        title="Свёрстать сайт по макету", category="Программист",
        description="Дизайн уже готов в Figma, нужна вёрстка и подключение форм",
        bid_price=40.0, budget_value=30000, published_at=datetime.now(timezone.utc),
    )
    assert ROLE_FILTERS.decide(order).passed is True


def test_hyphenated_role_is_matched():
    """«Контент-менеджер» с дефисом: токенизатор режет по дефису, нужна фразовая проверка."""
    assert matches("Требуется Контент-менеджер", "контент-менеджер") is True
    assert matches("Требуется контент менеджер", "контент-менеджер") is True


def test_redesign_is_not_design():
    """«Редизайн» не должен ловиться словом «дизайн»: совпадение идёт по началу слова."""
    assert matches("Редизайн корпоративного сайта", "дизайн") is False
    assert matches("Разработка дизайна игры", "дизайн") is True


# --- Покрытие всех направлений разработки ---------------------------------
# Регрессия: расширяя ключевые слова, легко случайно впустить мусор обратно,
# а сужая чёрный список ролей — потерять целое направление.

FULL_FILTERS = FilterSet.load()


def _order(title: str, category: str) -> Order:
    return Order(
        source=Source.FL, ext_id="1", url="x", title=title, category=category,
        description=title, budget_value=30000, published_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    ("category", "title"),
    [
        ("Back-end", "Нужен backend на Python для сервиса"),
        ("Desktop приложения", "Десктопное приложение под Windows"),
        ("DevOps и серверы", "Настроить CI/CD и docker на сервере"),
        ("Front-end", "Фронтенд на React для панели"),
        ("Базы данных и SQL", "Оптимизировать запросы PostgreSQL"),
        ("Верстка сайтов", "Свёрстать макет из Figma"),
        ("Игровая разработка", "Разработка игры на Unity"),
        ("Интеграции и API", "Интеграция с API маркетплейса"),
        ("Конструкторы сайтов", "Собрать сайт на Тильде"),
        ("Мобильная разработка", "Приложение под Android и iOS"),
        ("Парсинг и сбор данных", "Спарсить каталог товаров"),
        ("Сайты под ключ", "Корпоративный сайт под ключ"),
        ("Скрипты и автоматизация", "Скрипт автоматизации отчётов"),
        ("Тестирование (QA)", "Автотестирование сайта"),
        ("Чат-боты", "Телеграм-бот для записи клиентов"),
        ("AI автоматизация", "ИИ-агент для обработки заявок"),
        ("CRM", "Внедрить amoCRM с интеграциями"),
    ],
)
def test_all_development_directions_are_covered(category, title):
    """Все направления, которые закрывает студия, должны проходить фильтры."""
    decision = FULL_FILTERS.decide(_order(title, category))
    assert decision.passed is True, f"{category}: {decision.reason}"


@pytest.mark.parametrize(
    "title",
    [
        "Контент-менеджер", "Вэб дизайнер", "Создание контента для ютуб",
        "Устный переводчик", "Комплексное продвижение телеграм-канала",
        "Решение каптч на формах", "Наложение изображений", "Логотип для компании",
        "Видеомонтаж роликов", "Таргетолог в Instagram", "SMM-специалист",
        "Копирайтер на постоянку", "Обработка фото для маркетплейса",
        "Презентация для инвестора",
    ],
)
def test_non_development_stays_rejected_after_keyword_expansion(title):
    """Расширение ключевых слов не должно впустить обратно контент, дизайн и маркетинг."""
    assert FULL_FILTERS.decide(_order(title, title)).passed is False


# --- Контент и SMM: ловим где угодно в тексте ------------------------------

@pytest.mark.parametrize(
    "title,description",
    [
        ("Создание сторителлингов для прогревов", "Платформа сайта: собственная платформа"),
        ("Нужен человек на сторис", "Ведение аккаунта, платформа сайта"),
        ("Контент-план на месяц", "Нужен сайт-визитка для оформления"),
        ("Съёмка рилс для бренда", "Платформа сайта: Instagram"),
        ("Наполнение сайта карточками", "Загрузить карточки товаров"),
    ],
)
def test_content_work_is_rejected_even_with_dev_keywords(title, description):
    """«Платформа сайта: Instagram» не должна протаскивать SMM-заказ по слову «сайт».

    Повод: 06.08.2026 «Создание сторителлингов для прогревов» прошло фильтры
    и набрало 7.0 из 10 — ровно по слову «сайт» из строки про платформу.
    """
    order = Order(
        source=Source.PROFI,
        ext_id="1",
        url="https://profi.ru/backoffice/n.php?o=1",
        title=title,
        category="",
        description=description,
        budget_value=40000,
        published_at=datetime.now(timezone.utc),
    )
    decision = FilterSet.load().decide(order)
    assert not decision.passed, f"{title} → {decision.reason}"


# --- Потолок бюджета --------------------------------------------------------

def _rules_with_budget(minimum=20_000, maximum=170_000):
    from fbid.filters import CommonRules, FilterSet

    return FilterSet(CommonRules(min_budget=minimum, max_budget=maximum), {})


def _budget_order(budget):
    from fbid.models import Order, Source

    return Order(
        source=Source.PROFI, ext_id="1", url="https://profi.ru/backoffice/n.php?o=1",
        title="Создание сайта", description="Корпоративный сайт.", budget_value=budget,
        category="Создание сайта",
    )


def _budget_verdict(budget, minimum=20_000, maximum=170_000):
    """Только проверка бюджета: остальные правила здесь не при чём."""
    return _rules_with_budget(minimum, maximum)._check_budget(_budget_order(budget))


def test_order_within_range_passes():
    assert _budget_verdict(120_000.0) is None


def test_too_expensive_order_is_rejected():
    """Бюджет под миллион — это тендер, а не наш клиент.

    Заказчик собирает предложения от студий с профильным портфолио и отделом
    продаж. Отклик там уходит в никуда, а на Profi.ru стоит дороже всего:
    цена контакта растёт вместе с бюджетом заказа.
    """
    decision = _budget_verdict(999_999.0)
    assert decision is not None and not decision.passed
    assert "выше потолка" in decision.reason


def test_border_value_still_passes():
    """Ровно потолок — ещё наш заказ."""
    assert _budget_verdict(170_000.0) is None


def test_one_ruble_over_is_rejected():
    assert _budget_verdict(170_001.0) is not None


def test_too_cheap_still_rejected_from_below():
    decision = _budget_verdict(5_000.0)
    assert decision is not None and "ниже минимума" in decision.reason


def test_no_ceiling_means_no_upper_cut():
    assert _budget_verdict(5_000_000.0, maximum=None) is None


def test_order_without_budget_is_not_cut_by_ceiling():
    """Договорной заказ оценивает модель — здесь резать нечего."""
    assert _budget_verdict(None) is None


def test_ceiling_and_floor_read_separately_from_yaml(tmp_path):
    """Сброс верхней границы не должен возвращать значение нижней."""
    from fbid.filters import yaml_budget

    path = tmp_path / "filters.yaml"
    path.write_text("common:\n  min_budget: 20000\n  max_budget: 170000\n", encoding="utf-8")
    assert yaml_budget("min_budget", path) == 20_000
    assert yaml_budget("max_budget", path) == 170_000


def test_real_rules_have_the_ceiling():
    """Потолок должен быть в боевых правилах, а не только в тестах."""
    from fbid.filters import FilterSet

    assert FilterSet.load().common.max_budget == 170_000


# --- Ноль в доле бюджета означает запрет, а не отсутствие правила --------------

def test_zero_percent_forbids_spending_instead_of_lifting_the_ceiling(monkeypatch):
    """Владелец опускает долю до нуля, чтобы не переплачивать на мелочи.

    Раньше ноль не проходил проверку на истинность, процентный потолок
    отключался целиком, оставался только абсолютный — и софт после такой
    настройки начинал тратить БОЛЬШЕ, а не меньше.
    """
    from fbid import limits
    from fbid.filters import FilterSet, SourceRules

    monkeypatch.setattr(limits, "get", lambda key, store=None: 800.0 if "price" in key else 0.0)
    rules = SourceRules()

    assert FilterSet.effective_max_bid_price(rules, 20_000) == 0.0


def test_a_normal_share_still_caps_the_price(monkeypatch):
    from fbid import limits
    from fbid.filters import FilterSet, SourceRules

    monkeypatch.setattr(limits, "get", lambda key, store=None: 800.0 if "price" in key else 2.0)

    assert FilterSet.effective_max_bid_price(SourceRules(), 20_000) == 400.0


# --- Слова со знаками внутри --------------------------------------------------


@pytest.mark.parametrize(
    ("needle", "text", "found"),
    [
        ("c#", "Нужен разработчик C# под Unity", True),
        ("c#", "нужен cc# специалист", False),
        (".net", "Бэкенд на ASP.NET Core", True),
        ("next.js", "Сайт на Next.js и Tailwind", True),
        ("ci/cd", "Настроить CI/CD в GitLab", True),
        ("make.com", "Связать сервисы через Make.com", True),
        ("з/п", "Вакансия в штат, з/п 120 000 на руки", True),
        ("з/п", "Нужен лендинг под ключ", False),
    ],
)
def test_a_rule_with_punctuation_actually_matches(needle, text, found):
    """Токенизатор режет «c#» на «c», и такое правило не срабатывало никогда.

    Шесть слов из filters.yaml (.net, c#, ci/cd, make.com, next.js, з/п) не
    находили даже сами себя. «з/п» стоит в запретах — по нему отсеиваются
    вакансии, и каждая непойманная стоила нам платного отклика.
    """
    from fbid.filters import matches

    assert matches(text, needle) is found


def test_every_rule_in_the_file_can_match_itself():
    """Правило, не находящее само себя, — мёртвое правило.

    Проверяем весь filters.yaml целиком: так новое слово со знаком внутри не
    проскочит в файл незамеченным.
    """
    import pathlib

    import yaml

    from fbid.filters import matches

    raw = yaml.safe_load(pathlib.Path("filters.yaml").read_text(encoding="utf-8"))
    words: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            words.add(node)

    walk(raw)
    dead = sorted(word for word in words if word.strip() and not matches(word, word))
    assert dead == [], f"мёртвые правила: {dead}"

"""Единая точка импорта и общие фикстуры для тестов.

Тесты разложены на два файла: tests/test_core.py (основные кейсы) и
tests/test_edge.py (краевые). Сетевые/тяжёлые тесты помечаем @pytest.mark.network:
    pytest -m "not network"
"""
from __future__ import annotations

import pytest

# Единая точка импорта проверяемых объектов — тесты берут их отсюда или из parser.*
from parser.contacts import (  # noqa: F401
    classify_email,
    detect_dev_level,
    detect_niches,
    detect_red_flags,
    extract_emails,
    extract_phones,
    extract_telegram,
    looks_like_big_agency,
)
from parser.dedup import deduplicate, deduplicate_agencies, deduplicate_by  # noqa: F401
from parser.export import AGENCY_COLUMNS, agencies_to_dataframe  # noqa: F401
from parser.models import (  # noqa: F401
    Agency,
    Email,
    Organization,
    extract_socials,
    normalize_domain,
    normalize_phone,
)
from parser.pipeline import resolve_source  # noqa: F401
from parser.providers.browser import CITY_SLUGS, _parse_search_card  # noqa: F401
from parser.providers.catalogs import parse_catalog  # noqa: F401
from parser.reviews import categories, find_complaints, scan_reviews  # noqa: F401
from parser.store import JsonlStore  # noqa: F401

# Разделитель блоков внутри карточки выдачи 2ГИС (zero-width space).
ZWSP = "​"


@pytest.fixture
def make_org():
    """Фабрика Organization с разумными дефолтами — переопределяй нужные поля."""

    def _make(**overrides) -> Organization:
        data = {"name": "Организация", "source": "2gis"}
        data.update(overrides)
        return Organization(**data)

    return _make


@pytest.fixture
def make_agency():
    """Фабрика Agency с разумными дефолтами — переопределяй нужные поля."""

    def _make(**overrides) -> Agency:
        data = {"name": "Агентство", "website": "agency.ru"}
        data.update(overrides)
        return Agency(**data)

    return _make


@pytest.fixture
def sample_reviews() -> list[str]:
    return [
        "Отличная клиника, но не дозвониться неделю! Трубку не берут вообще.",
        "Записался онлайн. Долго отвечали на сообщение, ждал ответа часа три.",
        "Врачи супер, всё понравилось, рекомендую.",
        "Звонил три раза — никто не ответил, потом так и не перезвонили.",
    ]

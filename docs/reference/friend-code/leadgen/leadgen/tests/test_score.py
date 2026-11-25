"""Оценка лида: платёжеспособность и приоритет в выдаче.

Модуль решает, кому звонить первым, и до сих пор не был покрыт ни одним
тестом. Проверяем три вещи: границы баллов не съезжают, «мы не знаем» не
выдаётся за «мы посмотрели, и там плохо», и мусор во входных данных не роняет
оценку.
"""
from __future__ import annotations

import pytest

from parser.models import Organization
from parser.score import assess_solvency, lead_score


def _org(**kwargs) -> Organization:
    data = {"name": "Стоматология «Улыбка»", "source": "2gis",
            "source_url": "https://2gis.ru/firm/1"}
    data.update(kwargs)
    return Organization(**data)


# --- Границы баллов ------------------------------------------------------------


@pytest.mark.parametrize(
    ("reviews", "points"),
    [(None, 0), (0, 0), (14, 0), (15, 1), (49, 1), (50, 2), (199, 2), (200, 3), (5000, 3)],
)
def test_review_thresholds(reviews, points):
    """Отзывы — главный доступный признак объёма бизнеса, границы важны."""
    assert assess_solvency(_org(reviews_count=reviews))["points"] == points


@pytest.mark.parametrize(
    ("year", "points"),
    [(None, 0), (2026, 0), (2024, 0), (2023, 1), (2020, 1), (2019, 2), (1990, 2)],
)
def test_age_thresholds(year, points):
    assert assess_solvency(_org(registered_year=year))["points"] == points


def test_legal_entity_and_site_add_a_point_each():
    assert assess_solvency(_org(entity_type="юрлицо"))["points"] == 1
    assert assess_solvency(_org(website="a.ru"))["points"] == 1
    assert assess_solvency(_org(entity_type="ИП"))["points"] == 0, "ИП не штрафуем, но и не плюсуем"


def test_everything_at_once_is_the_ceiling():
    rich = _org(reviews_count=300, registered_year=2016, entity_type="юрлицо", website="a.ru")
    assessment = assess_solvency(rich)
    assert assessment["points"] == 7
    assert assessment["level"] == "high"


# --- «Не знаем» и «знаем, что плохо» — разные вещи ------------------------------


def test_no_data_at_all_says_so_plainly():
    """Ни отзывов, ни года, ни типа, ни сайта — оценивать нечем."""
    assessment = assess_solvency(_org())
    assert assessment["level"] == "unknown"
    assert "не оценить" in assessment["label"]


@pytest.mark.parametrize(
    "known",
    [{"entity_type": "ИП"}, {"reviews_count": 0}, {"registered_year": 2025}],
)
def test_weak_but_known_is_not_the_same_as_unknown(known):
    """Данные есть и говорят «негусто» — это оценка, а не пробел в ней.

    Для звонящего это противоположные ситуации: в одной надо дособрать данные
    по ИНН, в другой — решить, стоит ли тратить время. Одинаковая подпись под
    ними заставляет либо зря обогащать, либо зря звонить.
    """
    assessment = assess_solvency(_org(**known))
    assert assessment["points"] == 0
    assert assessment["level"] == "low"
    assert "не оценить" not in assessment["label"]


# --- Мусор во входных данных не должен ронять оценку ----------------------------


@pytest.mark.parametrize(
    "broken",
    [
        {"reviews_count": -5},          # отрицательные отзывы
        {"registered_year": 2100},      # регистрация в будущем
        {"registered_year": 1800},      # девятнадцатый век
        {"review_complaints": []},
    ],
)
def test_garbage_does_not_crash_the_assessment(broken):
    assessment = assess_solvency(_org(**broken))
    assert assessment["points"] >= 0
    assert lead_score(_org(**broken)) >= 0


def test_future_registration_gives_no_age_bonus():
    """Год регистрации из будущего — это мусор, а не свежая компания."""
    assert assess_solvency(_org(registered_year=2100))["points"] == 0


# --- Приоритет в выдаче --------------------------------------------------------


def test_complaints_raise_the_priority():
    quiet = _org(reviews_count=60)
    hurting = _org(reviews_count=60, review_complaints=["не дозвониться", "не перезвонили"])
    assert lead_score(hurting) > lead_score(quiet)


def test_complaints_are_capped():
    """Десять жалоб не должны перевешивать всё остальное без предела."""
    five = _org(review_complaints=[f"жалоба {i}" for i in range(5)])
    ten = _org(review_complaints=[f"жалоба {i}" for i in range(10)])
    assert lead_score(five) == lead_score(ten)


def test_inn_adds_a_point_because_it_is_a_way_to_the_owner():
    assert lead_score(_org(inn="7701234567")) == lead_score(_org()) + 1


def test_richer_lead_outranks_poorer_one_all_else_equal():
    poor = _org(review_complaints=["не дозвониться"])
    rich = _org(review_complaints=["не дозвониться"], reviews_count=300, website="a.ru")
    assert lead_score(rich) > lead_score(poor)


def test_pain_outweighs_money():
    """Горящий со средним кошельком выше богатого без жалоб.

    Решение владельца от 23.08.2026: продукт снимает ровно эту боль, и звонок
    в первую очередь — тому, кто уже мучается, а не тому, кто просто богат.
    """
    hurting = _org(reviews_count=60, website="b.ru",
                   review_complaints=["не дозвониться", "не перезвонили", "трубку не берут"])
    rich_quiet = _org(reviews_count=300, registered_year=2016,
                      entity_type="юрлицо", website="a.ru")
    assert lead_score(hurting) > lead_score(rich_quiet)


def test_rich_with_pain_is_still_on_top():
    """Деньги не обнулились: богатый с жалобами — лучший лид из всех."""
    rich_hurting = _org(reviews_count=300, registered_year=2016, entity_type="юрлицо",
                        website="a.ru", review_complaints=["ж1", "ж2"])
    poor_hurting = _org(review_complaints=[f"ж{i}" for i in range(6)])
    assert lead_score(rich_hurting) > lead_score(poor_hurting)


# --- Экспорт: чужое название не должно исполняться в Excel ----------------------


def test_formula_in_org_name_is_defused_in_xlsx(tmp_path):
    """Название с «=» уходило в Excel живой формулой.

    Название приходит с чужих карт и сайтов — его пишет кто угодно. Точка с
    именем «=HYPERLINK(…)» превращалась в кликабельную ссылку в файле, который
    владелец открывает руками.
    """
    import openpyxl

    from parser import export

    orgs = [
        _org(name='=HYPERLINK("http://evil.ru";"открой")'),
        _org(name="Клиника «Улыбка»", phone=["+7 495 123-45-67"]),
    ]
    path = tmp_path / "leads.xlsx"
    export.to_xlsx(orgs, path)

    sheet = openpyxl.load_workbook(path).active
    assert sheet.cell(row=2, column=1).data_type != "f", "формула обязана стать текстом"
    header = [c.value for c in sheet[1]]
    phone_cell = sheet.cell(row=3, column=header.index("phone") + 1).value
    assert str(phone_cell).startswith("+7"), "телефон трогать нельзя"


def test_formula_is_defused_in_csv_too(tmp_path):
    """CSV владелец открывает тем же Excel — чистка обязана быть той же."""
    from parser import export

    path = tmp_path / "leads.csv"
    export.to_csv([_org(name="=2+2")], path)
    body = path.read_text(encoding="utf-8-sig")
    assert "'=2+2" in body


def test_ordinary_names_are_left_alone(tmp_path):
    """Обычные названия и телефоны с «+» экранировать не нужно."""
    from parser.export import _defuse_formula

    assert _defuse_formula("Клиника «Улыбка»") == "Клиника «Улыбка»"
    assert _defuse_formula("+7 495 123-45-67") == "+7 495 123-45-67"
    assert _defuse_formula("-Минус и текст") == "'-Минус и текст"
    assert _defuse_formula("=2+2") == "'=2+2"

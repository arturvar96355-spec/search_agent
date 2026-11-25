"""Оценка платёжеспособности лида и его приоритета для аутрича.

Прямых финданных (выручка/сотрудники) на free-тарифе DaData нет — они приходят None.
Поэтому платёжеспособность оцениваем по КОСВЕННЫМ, но доступным сигналам:

  • число отзывов в 2ГИС  — лучший бесплатный прокси объёма бизнеса (много отзывов =
                            много клиентов = есть выручка = есть на что купить);
  • возраст компании      — устоявшийся бизнес (годы работы) платёжеспособнее вчерашнего;
  • тип (юрлицо/ИП)        — у ООО/АО обычно больше бюджет, чем у ИП (но ИП не штрафуем);
  • наличие сайта          — вкладывается в присутствие = есть бюджет на инструменты.

Это эвристика, а не бухгалтерия — она ранжирует лиды, а не выносит вердикт.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .models import Organization


def _age_years(org: Organization) -> int | None:
    if not org.registered_year:
        return None
    return datetime.now(tz=UTC).year - org.registered_year


def _solvency_points(org: Organization) -> tuple[int, list[str]]:
    """Баллы платёжеспособности (0..7) + человекочитаемые причины."""
    points = 0
    reasons: list[str] = []

    reviews = org.reviews_count or 0
    if reviews >= 200:
        points += 3
        reasons.append(f"{reviews} отзывов (большой поток клиентов)")
    elif reviews >= 50:
        points += 2
        reasons.append(f"{reviews} отзывов (заметный поток)")
    elif reviews >= 15:
        points += 1
        reasons.append(f"{reviews} отзывов")

    age = _age_years(org)
    if age is not None:
        if age >= 7:
            points += 2
            reasons.append(f"на рынке ~{age} лет")
        elif age >= 3:
            points += 1
            reasons.append(f"на рынке ~{age} лет")

    if org.entity_type == "юрлицо":
        points += 1
        reasons.append("юрлицо (ООО/АО)")

    if org.website:
        points += 1
        reasons.append("есть сайт")

    return points, reasons


def _has_any_signal(org: Organization) -> bool:
    """Есть ли вообще из чего оценивать. Ноль баллов — это два разных случая.

    Первый: мы ничего о компании не знаем — ни отзывов, ни года, ни типа, ни
    сайта. Второй: всё это известно, и по всему выходит негусто. Для звонящего
    это противоположные ситуации: в первом случае надо дособрать данные, во
    втором — решить, стоит ли тратить время. Одинаковая подпись под ними
    заставляет либо зря обогащать по ИНН, либо зря звонить.
    """
    return any((
        org.reviews_count is not None,
        org.registered_year is not None,
        org.entity_type is not None,
        bool(org.website),
    ))


def assess_solvency(org: Organization) -> dict:
    """Уровень платёжеспособности лида: emoji, метка, причины."""
    points, reasons = _solvency_points(org)
    if points >= 5:
        level, emoji, label = "high", "💰💰💰", "высокая платёжеспособность"
    elif points >= 3:
        level, emoji, label = "mid", "💰💰", "средняя платёжеспособность"
    elif points >= 1:
        level, emoji, label = "low", "💰", "платёжеспособность ниже средней"
    elif _has_any_signal(org):
        # Данные есть, и они говорят «негусто». Это оценка, а не пробел в ней.
        level, emoji, label = "low", "💰", "платёжеспособность низкая"
    else:
        level, emoji, label = "unknown", "❔", "платёжеспособность не оценить (мало данных)"
    return {"level": level, "emoji": emoji, "label": label, "points": points, "reasons": reasons}


def lead_score(org: Organization) -> int:
    """Итоговый приоритет лида для ранжирования (больше = перспективнее).

    Совмещает БОЛЬ (сколько свежих жалоб на связь — их и продаём решать) и
    ПЛАТЁЖЕСПОСОБНОСТЬ (может ли клиент за это заплатить) + выход на владельца (ИНН).

    Боль весит больше денег (решение владельца от 23.08.2026). Продукт — бот,
    который снимает ровно эту боль: жалоба «не дозвониться» означает, что
    клиенту продавать почти не надо, он уже мучается. Прежние веса (деньги ×3,
    боль ×2) ставили богатого молчаливого выше бедного горящего — и звонок
    уходил тому, кому наш продукт не нужен. Теперь горящий со средним
    кошельком обгоняет богатого без единой жалобы, но богатый с парой жалоб
    всё равно у вершины: платёжеспособность никуда не делась, она второй
    множитель, а не первый.
    """
    solvency, _ = _solvency_points(org)
    pain = min(len(org.review_complaints), 5)  # больше жалоб — горячее, но с потолком
    contactable = 1 if org.inn else 0
    return pain * 3 + solvency * 2 + contactable

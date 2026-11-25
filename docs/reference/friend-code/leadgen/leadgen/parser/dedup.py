"""Дедупликация записей между источниками.

Организации (клиентский режим): запись узнаём по НЕСКОЛЬКИМ признакам — ссылка на карточку
2ГИС, любой из телефонов, пара (name + нормализованный address). Совпал хотя бы один —
это одна организация. Дубли (в т.ч. между 2ГИС, браузерным поиском и Яндексом) схлопываем
в одну запись, объединяя поля: пустые в базовой записи заполняем из второй.

Агентства (режим partners): ключ — нормализованный домен. Общий механизм один
(`deduplicate_by`), различаются только функции ключа и слияния.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import TypeVar

from .models import Agency, Organization

T = TypeVar("T")

_WS_RE = re.compile(r"\s+")

# Скалярные поля, которые при merge заполняем из второго источника, если пусты в первом.
_FILLABLE_FIELDS = (
    "address",
    "website",
    "rubric",
    "lat",
    "lon",
    "rating",
    "reviews_count",
    "hours",
    "source_url",
    "inn",
    # Появляются на поздних этапах (браузерный добор, DaData) и есть не у всех записей —
    # при слиянии филиалов их легко было потерять вместе с отброшенной записью.
    "director",
    "entity_type",
    "registered_year",
)


def _normalize_address(address: str | None) -> str:
    """Грубая нормализация адреса для сравнения: нижний регистр, схлопнутые пробелы."""
    if not address:
        return ""
    return _WS_RE.sub(" ", address.strip().lower())


def _dedup_keys(org: Organization) -> list[str]:
    """Все признаки, по которым запись можно узнать. Совпал любой — это одна организация.

    - карточка 2ГИС: одна фирма из API и из браузерного поиска имеет РАЗНЫЕ name и address
      (API — «Илатан, сеть стоматологических клиник» + адрес с городом; браузер — «Илатан» +
      адрес из текста карточки), но одинаковый /firm/{id};
    - телефон: у филиалов сети он общий, и появляется только после браузерного добора —
      поэтому дедуп прогоняется ещё раз уже с телефонами;
    - name+address: запасной признак, когда нет ни ссылки, ни телефона.
    """
    keys: list[str] = []
    if org.source_url and "/firm/" in org.source_url:
        keys.append(f"url:{org.source_url.rstrip('/')}")
    keys.extend(f"phone:{phone}" for phone in org.phone)
    if not keys:
        keys.append(f"na:{org.name.strip().lower()}|{_normalize_address(org.address)}")
    return keys


def known_keys(org: Organization) -> list[str]:
    """Те же признаки, что и у пакетного дедупа, но для потоковой обработки.

    Бот отдаёт горячий лид сразу, как нашёл, и списка целиком у него нет —
    а узнавать дубли надо ровно тем же правилом. Иначе два филиала сети с
    общим телефоном уезжают владельцу как два лида, и он звонит в одну
    компанию дважды. Телефон появляется только после браузерного добора,
    поэтому дедуп, сделанный до него, эти филиалы развести не мог.
    """
    return _dedup_keys(org)


def _merge(base: Organization, other: Organization) -> Organization:
    """Схлопываем две записи в одну: приоритет у base, пустые поля добираем из other."""
    data = base.model_dump()
    other_data = other.model_dump()

    for field in _FILLABLE_FIELDS:
        if not data.get(field) and other_data.get(field):
            data[field] = other_data[field]

    # Телефоны — объединяем (нормализация/дедуп внутри валидатора Organization).
    data["phone"] = [*base.phone, *other.phone]

    # Жалобы из отзывов — тоже объединяем, без дублей и с сохранением порядка: они и есть
    # причина, по которой лид считается горячим, и терять их вместе со второй записью нельзя.
    data["review_complaints"] = list(dict.fromkeys([*base.review_complaints, *other.review_complaints]))

    # Соцсети — объединяем, значения base приоритетнее.
    merged_socials = dict(other.socials)
    merged_socials.update(base.socials)
    data["socials"] = merged_socials

    return Organization(**data)


def deduplicate_by(
    items: list[T],
    key_func: Callable[[T], str],
    merge_func: Callable[[T, T], T],
) -> list[T]:
    """Общий дедуп: схлопываем записи с одинаковым ключом, сохраняя порядок первого появления.

    Ключ и слияние вынесены в параметры — так один механизм обслуживает и организации
    (ключ = телефон), и агентства (ключ = домен).
    """
    merged: dict[str, T] = {}
    order: list[str] = []
    for item in items:
        key = key_func(item)
        if key in merged:
            merged[key] = merge_func(merged[key], item)
        else:
            merged[key] = item
            order.append(key)
    return [merged[key] for key in order]


def deduplicate_by_keys(
    items: list[T],
    keys_func: Callable[[T], list[str]],
    merge_func: Callable[[T, T], T],
) -> list[T]:
    """Дедуп по НЕСКОЛЬКИМ признакам: записи склеиваются, если совпал хотя бы один.

    Одного ключа мало: карточка 2ГИС и та же фирма из браузерного поиска сходятся только
    по /firm/{id}, а филиалы сети — только по общему телефону, который появляется уже
    после браузерного добора. Совпадение по любому признаку объединяет и сами группы —
    иначе результат зависел бы от порядка записей на входе.
    """
    groups: dict[int, T] = {}
    key_owner: dict[str, int] = {}
    order: list[int] = []
    next_id = 0

    for item in items:
        keys = [k for k in keys_func(item) if k]
        hit_ids = list(dict.fromkeys(key_owner[k] for k in keys if k in key_owner))
        if not hit_ids:
            groups[next_id] = item
            order.append(next_id)
            for key in keys:
                key_owner[key] = next_id
            next_id += 1
            continue

        # Базовой берём САМУЮ РАННЮЮ из совпавших групп, а не первую по порядку ключей
        # записи: иначе приоритет при слиянии (merge_func(base, other)) и место в выдаче
        # достались бы более поздней записи, и результат зависел бы от порядка телефонов.
        hit_ids.sort(key=order.index)
        target = hit_ids[0]
        for other in hit_ids[1:]:  # запись связала ранее разные группы — сливаем их
            groups[target] = merge_func(groups[target], groups.pop(other))
            order.remove(other)
            for key, owner in key_owner.items():
                if owner == other:
                    key_owner[key] = target
        groups[target] = merge_func(groups[target], item)
        for key in keys:
            key_owner.setdefault(key, target)

    return [groups[gid] for gid in order]


def deduplicate(orgs: list[Organization]) -> list[Organization]:
    """Дедуп организаций: совпали карточка 2ГИС, телефон ИЛИ name+address — одна запись."""
    return deduplicate_by_keys(orgs, _dedup_keys, _merge)


# --- агентства (режим partners): ключ = домен ---------------------------------

# Скалярные поля Agency, которые добираем из второй записи, если пусты в первой.
_AGENCY_FILLABLE_FIELDS = (
    "name", "website", "city", "services_raw", "partner_page_url", "source", "catalog_info",
)


def _agency_key(agency: Agency) -> str:
    """Ключ дедупа агентства: нормализованный домен; без домена — имя.

    Без домена И без имени схлопывать нечем — даём уникальный ключ по id объекта,
    иначе все такие записи слиплись бы в одну.
    """
    if agency.website:
        return f"domain:{agency.website}"
    name = agency.name.strip().lower()
    return f"name:{name}" if name else f"anon:{id(agency)}"


def _merge_agencies(base: Agency, other: Agency) -> Agency:
    """Схлопываем два агентства: приоритет у base, пустые поля и списки добираем из other."""
    data = base.model_dump()
    other_data = other.model_dump()

    for field in _AGENCY_FILLABLE_FIELDS:
        if not data.get(field) and other_data.get(field):
            data[field] = other_data[field]

    # Email'ы объединяем по адресу (тип берём от первого встреченного).
    seen_addresses = {email.address for email in base.emails}
    data["emails"] = [
        *data["emails"],
        *(e.model_dump() for e in other.emails if e.address not in seen_addresses),
    ]

    # Телефоны/Telegram/метки — объединяем без дублей (нормализация телефонов в валидаторе).
    data["phones"] = [*base.phones, *other.phones]
    data["telegram"] = list(dict.fromkeys([*base.telegram, *other.telegram]))
    data["niche_tags"] = list(dict.fromkeys([*base.niche_tags, *other.niche_tags]))
    data["red_flags"] = list(dict.fromkeys([*base.red_flags, *other.red_flags]))
    data["has_partner_page"] = base.has_partner_page or other.has_partner_page
    # Выясненные факты не должны зависеть от порядка слияния. big_agency и
    # dev_level добываются обходом сайта, и если базовой оказалась запись из
    # каталога (где их нет), знание из другой копии молча терялось: крупный
    # сетевой игрок «уменьшался», а уровень разработки откатывался в unknown —
    # и питч подбирался не тот.
    data["big_agency"] = base.big_agency or other.big_agency
    if data.get("dev_level", "unknown") == "unknown" and other.dev_level != "unknown":
        data["dev_level"] = other.dev_level

    return Agency(**data)


def deduplicate_agencies(agencies: list[Agency]) -> list[Agency]:
    """Дедуп агентств по домену (режим partners)."""
    return deduplicate_by(agencies, _agency_key, _merge_agencies)

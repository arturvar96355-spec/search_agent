"""Шаг обогащения (enrich) — необязательный, изолированный модуль.

По названию (+городу) организации дотягиваем ИНН и ФИО директора через официальный
API DaData (предпочтительный путь: у Rusprofile/Checko есть анти-бот/платный доступ,
их не скрейпим). Изолирован: сеть и ключи DaData — только здесь.

Требует опциональную зависимость и ключи в .env:
    pip install -e '.[enrich]'          # клиент dadata
    DADATA_API_KEY=... / DADATA_SECRET=...
"""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pandas as pd
from loguru import logger

from .config import Settings, get_settings
from .models import Organization


def _cell(row, column: str) -> str | None:
    """Ячейка таблицы -> строка или None. Пустое/NaN/«nan» — это отсутствие значения.

    Отдельно чистим хвост «.0»: он остаётся у ИНН, если файл когда-то был сохранён
    прошлой версией, читавшей колонку как число.
    """
    value = row.get(column)
    if value is None or not pd.notna(value):
        return None
    text = str(value).strip()
    if text.lower() in {"", "nan", "none"}:
        return None
    if column == "inn" and text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _require_keys(settings: Settings) -> None:
    if not (settings.dadata_api_key and settings.dadata_secret):
        raise RuntimeError(
            "Не заданы DADATA_API_KEY / DADATA_SECRET в .env — обогащение через DaData недоступно."
        )


def _base_name(name: str) -> str:
    """Убираем хвост-рубрику 2ГИС («ДС Авто, автотехцентр» -> «ДС Авто»), НО не режем
    имена, где запятая — часть самого названия («Кузнецов, Иванов и партнёры» остаётся целым).

    Эвристика: рубрика 2ГИС идёт после ПОСЛЕДНЕЙ запятой и начинается со строчной буквы
    (автотехцентр, стоматология…), тогда как продолжение имени обычно с заглавной.
    """
    head, sep, tail = name.rpartition(",")
    tail = tail.strip()
    if sep and tail and tail[0].islower():
        return head.strip() or name.strip()
    return name.strip()


def _lookup(client, org: Organization, city_hint: str | None = None) -> dict | None:
    """Ищем организацию в DaData по названию (+городу) -> data карточки.

    Было 0/10 (мешал хвост-рубрика + город в одном запросе), стало ~72%. Среди топ-5
    подсказок предпочитаем ту, где город из адреса реально присутствует — так реже цепляем
    однофамильца из другого региона; если явного по городу нет — берём топ-1 (без потери покрытия).

    city_hint — город из самого поиска (бот его знает); он надёжнее, чем парсинг адреса
    (у пригородов адрес даёт мусор вроде «Вешки (Мытищи...»).
    """
    base = _base_name(org.name)
    city = (city_hint or "").strip()
    if not city and org.address:
        city = org.address.split("(")[0].split(",")[0].strip()
    # Бизнес назван в честь города («Москва, салон») — запрос выродится в город и даст
    # мусорный матч случайной фирмы. Лучше пустой ИНН, чем чужой.
    if city and base.lower() == city.lower():
        return None
    query = base if (not city or city.lower() in base.lower()) else f"{base} {city}"
    suggestions = client.suggest("party", query, count=5)
    if not suggestions:
        return None
    if city:
        for sug in suggestions:  # предпочитаем совпадение с нужным городом в адресе
            data = sug.get("data") or {}
            addr = (data.get("address") or {}).get("value", "") or ""
            if city.lower() in addr.lower():
                return data
    return suggestions[0].get("data") or {}


def _director_of(data: dict) -> str | None:
    """ФИО руководителя: для юрлица — management.name, для ИП — сам предприниматель (fio/name).

    ВАЖНО: fallback на name.full — ТОЛЬКО для ИП (там name.full = ФИО). Для юрлица без
    management руководителя нет — возвращаем None, иначе в директоры попадёт название фирмы.
    """
    management = data.get("management") or {}
    if management.get("name"):
        return management["name"]
    if (data.get("type") or "").upper() != "INDIVIDUAL":
        return None  # юрлицо без руководителя — не выдаём название компании за ФИО
    fio = data.get("fio") or {}
    parts = [fio.get("surname"), fio.get("name"), fio.get("patronymic")]
    full = " ".join(p for p in parts if p).strip()
    if full:
        return full
    # ИП без структурированного fio: у DaData name.full = «ИП Иванов Иван Иванович».
    name_full = (data.get("name") or {}).get("full") or ""
    return name_full or None


def _entity_type_of(data: dict) -> str | None:
    """'ИП' для INDIVIDUAL, 'юрлицо' для LEGAL, иначе None."""
    kind = (data.get("type") or "").upper()
    if kind == "INDIVIDUAL":
        return "ИП"
    if kind == "LEGAL":
        return "юрлицо"
    return None


def _registered_year_of(data: dict) -> int | None:
    """Год регистрации (из state.registration_date или ogrn_date, мс) — для оценки возраста."""
    ms = (data.get("state") or {}).get("registration_date") or data.get("ogrn_date")
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).year
    except (TypeError, ValueError, OSError):
        return None


def fetch_by_inn(inn: str, settings: Settings | None = None) -> dict | None:
    """Полная карточка организации из DaData по ИНН (для кнопки «Подробнее по ИНН»).

    Возвращает сырой data-объект DaData (форматирование — в telegram.format_company_card),
    либо None, если по ИНН ничего не найдено. Синхронно ходит в сеть — вызывать в потоке.
    """
    settings = settings or get_settings()
    _require_keys(settings)
    try:
        from dadata import Dadata  # опциональная зависимость
    except ImportError as exc:
        raise RuntimeError("Клиент dadata не установлен. Поставь: pip install -e '.[enrich]'") from exc

    with Dadata(settings.dadata_api_key, settings.dadata_secret) as client:
        found = client.find_by_id("party", inn)
    if not found:
        return None
    return found[0].get("data") or {}


def enrich(
    orgs: list[Organization],
    settings: Settings | None = None,
    limit: int | None = None,
    city: str | None = None,
) -> list[Organization]:
    """Заполняем org.inn/director/entity_type/registered_year по данным DaData.

    city — город из поиска (надёжнее адреса при матчинге). Уже заполненные ИНН пропускаем.
    """
    settings = settings or get_settings()
    _require_keys(settings)  # сначала ключи — понятная ошибка даже без установленного клиента

    try:
        from dadata import Dadata  # опциональная зависимость
    except ImportError as exc:
        raise RuntimeError("Клиент dadata не установлен. Поставь: pip install -e '.[enrich]'") from exc

    # Сначала отсеиваем уже обогащённых, и только потом режем лимитом. Раньше
    # порядок был обратный: на повторном прогоне того же файла первые N строк
    # оказывались заполненными, все они пропускались, и прогон не делал ничего.
    # Сколько раз ни запусти с --limit — файл не двигался с места, а команда
    # рапортовала «обогащено: 0», будто DaData ничего не знает.
    pending = [org for org in orgs if not org.inn]
    targets = pending if limit is None else pending[:limit]
    filled = 0
    with Dadata(settings.dadata_api_key, settings.dadata_secret) as client:
        for org in targets:
            try:
                data = _lookup(client, org, city_hint=city)
            except Exception as exc:  # noqa: BLE001 — одна организация не должна ронять весь прогон
                logger.warning("DaData: {} — {}", org.name, exc)
                continue
            if not data:
                continue
            org.inn = data.get("inn")
            director = _director_of(data)
            if director:
                org.director = director
            org.entity_type = _entity_type_of(data)
            org.registered_year = _registered_year_of(data)
            if org.inn:
                filled += 1

    logger.info("DaData: обогащено {} из {} организаций.", filled, len(targets))
    return orgs


def enrich_file(
    path: str | Path,
    settings: Settings | None = None,
    limit: int | None = None,
) -> Path:
    """Читаем файл лидов (.csv/.xlsx), дотягиваем ИНН/директора, пишем обратно в тот же файл.

    Файл перезаписывается НА МЕСТЕ, поэтому всё, что в нём уже было, обязано пережить прогон:
    заполненные ранее ИНН и ФИО читаются обратно в Organization, а не затираются пустотой.
    """
    path = Path(path)
    settings = settings or get_settings()

    # ИНН читаем СТРОКОЙ. Иначе при частично пустой колонке pandas даёт float64,
    # и 7701234567 превращается в «7701234567.0», а у ИНН регионов 01-09 пропадает
    # ведущий ноль. Такой ИНН и в выгрузке невалиден, и навсегда пропускается при
    # повторном прогоне (enrich считает его уже найденным).
    string_cols = {"inn": str, "director": str, "entity_type": str}
    if path.suffix.lower() == ".xlsx":
        df = pd.read_excel(path, dtype=string_cols)
    else:
        df = pd.read_csv(path, dtype=string_cols)

    orgs: list[Organization] = []
    for _, row in df.iterrows():
        orgs.append(
            Organization(
                name=str(row.get("name") or ""),
                address=(str(row["address"]) if pd.notna(row.get("address")) else None),
                inn=_cell(row, "inn"),
                # Уже найденные значения обязаны попасть в Organization: строки с ИНН
                # enrich пропускает, и без этого запись обратно затирала бы их пустотой.
                director=_cell(row, "director"),
                entity_type=_cell(row, "entity_type"),
                source="2gis",
            )
        )

    enrich(orgs, settings=settings, limit=limit)

    df["inn"] = [org.inn for org in orgs]
    df["director"] = [org.director for org in orgs]

    # Чистка та же, что в export: файл открывают Excel-ем, и ячейка-формула
    # из чужого названия исполнилась бы и здесь.
    from .export import _sanitize_for_xlsx

    df = _sanitize_for_xlsx(df)
    if path.suffix.lower() == ".xlsx":
        df.to_excel(path, index=False)
    else:
        df.to_csv(path, index=False, encoding="utf-8-sig")

    logger.info("enrich_file: обновлён {}", path)
    return path

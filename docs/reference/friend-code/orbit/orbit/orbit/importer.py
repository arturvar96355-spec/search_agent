"""Перенос собранного парсером в базу Orbit.

Парсер складывает результат в файлы: агентства и компании в .xlsx, сырые
организации в .jsonl. Формат менять не будем — выгрузки нужны владельцу как
есть, их открывают в Excel. Orbit просто читает их и раскладывает по своей
базе, добавляя то, чего в файлах нет: стадию, историю и очередь на отправку.

Импорт можно гонять сколько угодно раз: повторная строка не создаёт дубль и
не сбрасывает стадию уже начатого контакта (см. Store.upsert_contact).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from loguru import logger

from .models import Contact, Kind, normalize_telegram
from .store import Store

# «addr@dom.ru (partner)» из колонки emails — тип нам не нужен, адрес нужен.
_EMAIL_KIND_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean(value) -> str:
    """Ячейка excel/json → строка. NaN, None и «nan» становятся пустой строкой."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat", "<na>"}:
        return ""
    return text


def _split(value, separator: str = ",") -> list[str]:
    """Значение → список строк.

    Excel отдаёт телефоны и адреса одной строкой через запятую, а .jsonl —
    настоящим списком. Без этой развилки список превращался в мусорную строку
    вида «['+79991234567']», и контакт терял телефон.
    """
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in value if _clean(item)]
    text = _clean(value)
    if not text:
        return []
    return [part.strip() for part in text.split(separator) if part.strip()]


def _emails(value) -> list[str]:
    return [_EMAIL_KIND_RE.sub("", item).strip().lower() for item in _split(value)]


def domain_of(raw: str) -> str:
    """URL или домен → голый домен в нижнем регистре. Ключ дедупликации."""
    value = _clean(raw).lower()
    if not value:
        return ""
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    value = value.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    return value[4:] if value.startswith("www.") else value


# --- Агентства (партнёры) ---------------------------------------------------

def agency_to_contact(row: dict) -> Contact | None:
    """Строка выгрузки partners → контакт. None, если строка пустая или не наша."""
    name = _clean(row.get("name"))
    if not name:
        return None

    site = domain_of(row.get("website"))
    # Ключ: домен надёжнее названия («Медиа Сфера» и «Медиасфера» это одни и те же).
    # Домена нет — падаем на имя, иначе безымянные строки склеятся в одну.
    key = site or f"name:{name.lower()}"

    pitch = _clean(row.get("pitch"))
    meta = {
        "pitch": pitch,
        "dev_level": _clean(row.get("dev_level")),
        "niche": _split(row.get("niche_tags")),
        "services": _clean(row.get("services_raw"))[:600],
        "catalog": _clean(row.get("catalog_info")),
        "partner_page": _clean(row.get("partner_page_url")),
        "red_flags": _split(row.get("red_flags")),
    }
    emails = _emails(row.get("emails")) or _emails(row.get("best_email"))
    telegram = [t for t in (normalize_telegram(item) for item in _split(row.get("telegram"))) if t]

    return Contact(
        kind=Kind.PARTNER,
        key=key,
        name=name,
        city=_clean(row.get("city")) or None,
        site=site or None,
        emails=emails,
        phones=_split(row.get("phones")),
        telegram=telegram,
        source_url=_clean(row.get("partner_page_url")) or (f"https://{site}" if site else None),
        meta={k: v for k, v in meta.items() if v},
    )


# --- Компании с карт (клиенты) ----------------------------------------------

def org_to_contact(row: dict) -> Contact | None:
    """Строка выгрузки организаций (или org из .jsonl) → контакт."""
    name = _clean(row.get("name"))
    if not name:
        return None

    site = domain_of(row.get("website"))
    phones = _split(row.get("phone")) or _split(row.get("phones"))
    address = _clean(row.get("address"))
    source_url = _clean(row.get("source_url"))

    # Ключ по убыванию надёжности. Первой идёт ссылка на карточку: она
    # уникальна и есть у каждой записи с карт, а вот телефон в демо-режиме
    # 2ГИС не отдаётся. Если ключом сделать телефон, одна и та же компания
    # из двух выгрузок (с номером и без) превращается в две записи, и
    # владелец пишет ей дважды.
    # Две «Пятёрочки» на разных улицах при этом остаются двумя строками.
    key = source_url or site or (phones[0] if phones else f"{name.lower()}|{address.lower()}")

    # В .xlsx жалобы лежат одной строкой через «|», в .jsonl — уже списком.
    # Пустая ячейка excel приходит как NaN, поэтому list проверяем явно.
    raw_complaints = row.get("review_complaints")
    if isinstance(raw_complaints, list):
        complaints = [str(item) for item in raw_complaints if item]
    else:
        complaints = [c for c in _clean(raw_complaints).split("|") if c.strip()]

    socials = row.get("socials") or {}
    telegram_raw = _clean(row.get("telegram")) or _clean(socials.get("telegram"))
    telegram = [t for t in [normalize_telegram(telegram_raw)] if t]

    meta = {
        "rubric": _clean(row.get("rubric")),
        "rating": _clean(row.get("rating")),
        "reviews": _clean(row.get("reviews_count")),
        "complaints": [c.strip() for c in complaints][:5],
        "has_site": "есть" if site else "нет, только карточка",
        "address": address,
        "entity": _clean(row.get("entity_type")),
        "director": _clean(row.get("director")),
    }

    return Contact(
        kind=Kind.CLIENT,
        key=key,
        name=name,
        city=_city_from(address),
        site=site or None,
        emails=_emails(row.get("email")) + _emails(row.get("emails")),
        phones=phones,
        telegram=telegram,
        source_url=_clean(row.get("source_url")) or None,
        meta={k: v for k, v in meta.items() if v},
    )


def _city_from(address: str) -> str | None:
    """Город из адреса 2ГИС: он идёт первым куском до запятой.

    Скобку режем вместе с запятой. У подмосковных адресов 2ГИС пишет
    «Вешки (Мытищи городской округ, …» — и в город попадал кусок с
    незакрытой скобкой. Это не только уродливая строка в карточке: город
    уходит в промт письма как факт о компании, и модель писала клиенту
    «вижу, вы из Вешки (Мытищи городской округ».
    """
    if not address:
        return None
    head = address.split("(")[0].split(",")[0].strip()
    return head if head and len(head) < 40 else None


# --- Чтение файлов ----------------------------------------------------------

def read_xlsx(path: Path) -> list[dict]:
    try:
        import pandas as pd
    except ImportError:
        logger.error("Для чтения {} нужен pandas: pip install pandas openpyxl", path.name)
        return []
    frame = pd.read_excel(path)
    return frame.to_dict(orient="records")


def read_jsonl(path: Path) -> list[dict]:
    """Строки .jsonl парсера: интересны только те, где есть org."""
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue  # оборванная строка после падения прогона
            org = data.get("org")
            if isinstance(org, dict):
                rows.append(org)
    return rows


def read_any(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return read_xlsx(path)
    if suffix == ".jsonl":
        return read_jsonl(path)
    if suffix == ".csv":
        try:
            import pandas as pd
        except ImportError:
            logger.error("Для чтения {} нужен pandas", path.name)
            return []
        return pd.read_csv(path).to_dict(orient="records")
    logger.warning("Не знаю, как читать {}", path.name)
    return []


def looks_like_agencies(rows: list[dict]) -> bool:
    """Выгрузка партнёров отличается от выгрузки компаний своими колонками."""
    if not rows:
        return False
    keys = set(rows[0].keys())
    return bool({"pitch", "dev_level", "best_email"} & keys)


def import_file(store: Store, path: Path, kind: Kind | None = None) -> tuple[int, int]:
    """Читает файл в базу. Возвращает (сколько строк прочитано, сколько записано)."""
    path = Path(path)
    if not path.exists():
        logger.error("Файла нет: {}", path)
        return 0, 0

    rows = read_any(path)
    if not rows:
        return 0, 0

    if kind is None:
        kind = Kind.PARTNER if looks_like_agencies(rows) else Kind.CLIENT
    convert = agency_to_contact if kind is Kind.PARTNER else org_to_contact

    written = 0
    for row in rows:
        contact = convert(row)
        if contact is None:
            continue
        try:
            store.upsert_contact(contact)
            written += 1
        except Exception as exc:  # noqa: BLE001 — одна кривая строка не рушит импорт
            logger.warning("Строку «{}» не записал: {}", _clean(row.get("name"))[:40], exc)

    logger.info("{}: прочитано {}, записано {}", path.name, len(rows), written)
    return len(rows), written


def import_dir(store: Store, directory: Path) -> tuple[int, int, list[str]]:
    """Все выгрузки парсера разом. Третьим — имена файлов, которые не прочлись.

    Ни один файл по имени не пропускаем: у выгрузок нет договорённости об
    именовании, и «audit_partners.xlsx» оказался боевой базой агентств, а не
    служебным срезом. Что импортировать, решает содержимое (есть ли колонка
    name), а повторные строки схлопывает дедуп по домену.

    Список непрочитанных возвращаем, а не только пишем в лог: раньше он
    оставался внутри, и кнопка «Загрузить из парсера» на десяти битых файлах
    отвечала «выгрузок не нашлось». Владелец шёл искать пропавший каталог,
    хотя каталог был на месте, а сломан был экспорт парсера.
    """
    directory = Path(directory)
    if not directory.exists():
        logger.error("Каталога выгрузок нет: {}", directory)
        return 0, 0, []

    total_rows = total_written = 0
    broken: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in {".xlsx", ".xls", ".jsonl", ".csv"}:
            continue
        try:
            rows, written = import_file(store, path)
        except Exception as exc:  # noqa: BLE001 — один битый файл не рушит выгрузку
            # Раньше недочитанный файл ронял весь проход: остальные выгрузки не
            # импортировались вовсе, а владелец видел только «команда упала».
            # Файл в середине списка так забирал с собой всё, что за ним.
            logger.error("Выгрузка {} не прочиталась: {}", path.name, exc)
            broken.append(path.name)
            continue
        total_rows += rows
        total_written += written

    if broken:
        logger.warning(
            "Не прочитались файлы ({}): {}", len(broken), ", ".join(broken[:5])
        )
    return total_rows, total_written, broken

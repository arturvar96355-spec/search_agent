"""Экспорт в CSV / XLSX / Google Sheets.

Организации (клиентский режим) и агентства-партнёры (режим partners) выгружаются одним
и тем же писателем XLSX — различаются только наборы колонок.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from loguru import logger

from .config import Settings, get_settings
from .models import Agency, Organization

# Порядок и состав колонок выгрузки. Соцсети раскладываем по отдельным колонкам.
COLUMNS = [
    "name",
    "address",
    "phone",
    "website",
    "telegram",
    "vk",
    "instagram",
    "rubric",
    "lat",
    "lon",
    "rating",
    "reviews_count",
    "hours",
    "has_problems",
    "review_complaints",
    "source",
    "source_url",
    "inn",
    "director",
    # Форма юрлица и год регистрации приезжают из ДаData вместе с ИНН — то
    # есть за них уже заплачено, и по ним же считается платёжеспособность.
    # В файл они не попадали: владелец работает именно в этой таблице, а
    # «ООО с 2012 года» и «ИП, зарегистрирован в марте» — разные разговоры.
    "entity_type",
    "registered_year",
    # Приоритет, по которому строки отсортированы. Без колонки он живёт только
    # в порядке строк и пропадает от первой же сортировки в Excel — а подпись
    # к файлу обещает «по убыванию приоритета».
    "priority",
]


def _to_records(orgs: list[Organization]) -> list[dict]:
    from .score import lead_score


    records: list[dict] = []
    for org in orgs:
        socials = org.socials or {}
        records.append(
            {
                "name": org.name,
                "address": org.address,
                "phone": ", ".join(org.phone),
                "website": org.website,
                "telegram": socials.get("telegram"),
                "vk": socials.get("vk"),
                "instagram": socials.get("instagram"),
                "rubric": org.rubric,
                "lat": org.lat,
                "lon": org.lon,
                "rating": org.rating,
                "reviews_count": org.reviews_count,
                "hours": org.hours,
                "has_problems": "да" if org.review_complaints else "",
                "review_complaints": " | ".join(org.review_complaints),
                "source": org.source,
                "source_url": org.source_url,
                "inn": org.inn,
                "director": org.director,
                "entity_type": org.entity_type,
                "registered_year": org.registered_year,
                "priority": lead_score(org),
            }
        )
    return records


def to_dataframe(orgs: list[Organization]) -> pd.DataFrame:
    """Плоская таблица организаций в фиксированном порядке колонок."""
    return pd.DataFrame(_to_records(orgs), columns=COLUMNS)


def to_csv(orgs: list[Organization], path: str | Path) -> Path:
    """Выгрузка в CSV. utf-8-sig — чтобы Excel корректно открыл кириллицу.

    Чистка та же, что у XLSX: CSV владелец открывает тем же Excel, и ячейка
    «=HYPERLINK(…)» из чужого названия исполняется там точно так же.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = _sanitize_for_xlsx(to_dataframe(orgs))
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("CSV сохранён: {} ({} строк)", path, len(df))
    return path


def to_xlsx(orgs: list[Organization], path: str | Path) -> Path:
    """Выгрузка организаций в XLSX (клиентский режим)."""
    return _write_xlsx(to_dataframe(orgs), path, sheet_name="leads")


# Управляющие символы, которые openpyxl не принимает (текст с чужих сайтов бывает грязным).
_ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _defuse_formula(value: str) -> str:
    """Ячейка, начинающаяся с «=», уходит в Excel живой формулой — глушим.

    Название организации приходит с чужих карт и сайтов, то есть его пишет
    кто угодно. Точка с именем «=HYPERLINK(…)» превращалась в кликабельную
    ссылку в файле, который владелец открывает руками; в старых Excel через
    DDE так запускали и команды. Апостроф впереди — штатный способ Excel
    сказать «это текст»: в ячейке он не отображается и в значение при чтении
    файла не входит.

    «+» и «-» опасны только как начало формулы, а у нас с «+» начинается
    каждый телефон — их глушим лишь когда дальше идёт не цифра.
    """
    if value.startswith(("=", "@", "\t", "\r")):
        return "'" + value
    if value.startswith(("+", "-")) and len(value) > 1 and not value[1].isdigit():
        return "'" + value
    return value


def _sanitize_for_xlsx(df: pd.DataFrame) -> pd.DataFrame:
    """Чистим строковые ячейки: управляющие символы и ячейки-формулы.

    Управляющие символы роняют весь экспорт в openpyxl, а формулы — способ
    подсунуть владельцу ссылку или команду через название организации.

    Проверяем не только dtype == object: в pandas 3.0 у строковых колонок dtype 'str',
    и проверка по object молча пропускала грязный текст с чужих сайтов.
    """
    cleaned = df.copy()
    for column in cleaned.columns:
        if cleaned[column].dtype == object or pd.api.types.is_string_dtype(cleaned[column]):
            cleaned[column] = cleaned[column].map(
                lambda v: _defuse_formula(_ILLEGAL_XLSX_RE.sub("", v)) if isinstance(v, str) else v
            )
    return cleaned


def _write_xlsx(df: pd.DataFrame, path: str | Path, sheet_name: str) -> Path:
    """Общий писатель XLSX: жирные заголовки, автоширина колонок, закреплённая шапка.

    Минимализм под печать — без заливки, рамок и прочего декора.
    """
    from openpyxl.styles import Alignment, Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = _sanitize_for_xlsx(df)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]

        # Жирные заголовки.
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(vertical="center")

        # Автоширина по содержимому (с разумным потолком, чтобы длинные адреса не растягивали лист).
        for column_index, column_name in enumerate(df.columns, start=1):
            letter = worksheet.cell(row=1, column=column_index).column_letter
            width = len(str(column_name))
            for value in df[column_name]:
                if pd.isna(value):
                    continue
                width = max(width, len(str(value)))
            worksheet.column_dimensions[letter].width = min(width + 2, 60)

        worksheet.freeze_panes = "A2"  # шапка не уезжает при прокрутке

    logger.info("XLSX сохранён: {} ({} строк)", path, len(df))
    return path


AGENCY_COLUMNS = [
    "name",
    "website",
    "pitch",
    "dev_level",
    "best_email",
    "email_found",
    "emails",
    "phones",
    "telegram",
    "city",
    "niche_tags",
    "red_flags",
    "has_partner_page",
    "partner_page_url",
    "catalog_info",
    "source",
    "services_raw",
]


def _agency_records(agencies: list[Agency]) -> list[dict]:
    records: list[dict] = []
    for agency in agencies:
        records.append(
            {
                "name": agency.name,
                "website": agency.website,
                # С чем идти к агентству (правило из брифа продаж) — главная колонка для аутрича.
                "pitch": agency.recommended_pitch,
                "dev_level": agency.dev_level,
                "best_email": agency.best_email or "",
                # Явная метка «не найдено» — чтобы пустая ячейка не читалась как «не смотрели».
                "email_found": "да" if agency.email_found else "не найдено",
                "emails": ", ".join(f"{e.address} ({e.kind})" for e in agency.emails),
                "phones": ", ".join(agency.phones),
                "telegram": ", ".join(agency.telegram),
                "city": agency.city,
                "niche_tags": ", ".join(agency.niche_tags),
                "red_flags": ", ".join(agency.red_flags),
                "has_partner_page": "да" if agency.has_partner_page else "",
                "partner_page_url": agency.partner_page_url,
                "catalog_info": agency.catalog_info,
                "source": agency.source,
                "services_raw": agency.services_raw,
            }
        )
    return records


def agencies_to_dataframe(agencies: list[Agency]) -> pd.DataFrame:
    """Плоская таблица агентств-партнёров в фиксированном порядке колонок."""
    return pd.DataFrame(_agency_records(agencies), columns=AGENCY_COLUMNS)


def agencies_to_xlsx(agencies: list[Agency], path: str | Path) -> Path:
    """Выгрузка агентств-партнёров в XLSX (режим partners)."""
    return _write_xlsx(agencies_to_dataframe(agencies), path, sheet_name="agencies")


_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _open_spreadsheet(client, spreadsheet: str):
    """Открываем таблицу по URL / ключу / имени; если имени нет — создаём новую."""
    import gspread

    if spreadsheet.startswith("http"):
        return client.open_by_url(spreadsheet)
    try:
        return client.open(spreadsheet)
    except gspread.SpreadsheetNotFound:
        sheet = client.create(spreadsheet)
        logger.warning(
            "Создал новую таблицу {!r} (владелец — сервисный аккаунт). "
            "Открой доступ себе или используй существующую по URL.",
            spreadsheet,
        )
        return sheet


def to_google_sheets(
    orgs: list[Organization],
    spreadsheet: str,
    settings: Settings | None = None,
) -> str:
    """Выгрузка в Google Sheets через gspread + сервисный аккаунт.

    `spreadsheet` — URL / ключ / имя таблицы. Путь к JSON сервисного аккаунта берём
    из settings.google_service_account_json. Лист перезаписываем целиком.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    settings = settings or get_settings()
    creds_path = settings.google_service_account_json
    if not creds_path:
        raise RuntimeError("Не задан GOOGLE_SERVICE_ACCOUNT_JSON в .env (путь к JSON сервисного аккаунта).")
    creds_path = Path(creds_path)
    if not creds_path.exists():
        raise FileNotFoundError(f"Файл сервисного аккаунта не найден: {creds_path}")

    creds = Credentials.from_service_account_file(str(creds_path), scopes=_GOOGLE_SCOPES)
    client = gspread.authorize(creds)
    sheet = _open_spreadsheet(client, spreadsheet)
    worksheet = sheet.sheet1

    # Чистка та же, что в XLSX: Google Sheets исполняет «=…» из ячейки точно
    # так же, а IMPORTDATA/IMPORTXML там ещё и тянут данные наружу сами, без
    # клика — чужое название не должно становиться формулой.
    df = _sanitize_for_xlsx(to_dataframe(orgs))
    header = list(df.columns)
    # NaN -> "" (JSON в Google Sheets не принимает NaN).
    rows = [["" if pd.isna(value) else value for value in record]
            for record in df.itertuples(index=False, name=None)]

    worksheet.clear()
    # RAW: значения ложатся как текст, а не проходят разбор формул на стороне
    # Google. Второй замок к апострофу из чистки.
    worksheet.update(range_name="A1", values=[header] + rows, value_input_option="RAW")
    worksheet.format("1:1", {"textFormat": {"bold": True}})  # жирная шапка

    logger.info("Google Sheets обновлён: {} ({} строк)", sheet.url, len(rows))
    return sheet.url

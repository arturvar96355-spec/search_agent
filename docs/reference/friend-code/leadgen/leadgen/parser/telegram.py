"""Доставка собранных лидов в Telegram через Bot API.

Без тяжёлых зависимостей — прямые вызовы Bot API через httpx. Каждая организация
уходит отдельной карточкой в чат (TELEGRAM_CHAT_ID). Удобно для ручного аутрича:
видно телефон, рейтинг и сигналы из отзывов («не дозвониться / долго отвечают»).
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC

import httpx
from loguru import logger

from .config import Settings, get_settings
from .models import Organization

_API = "https://api.telegram.org/bot{token}/{method}"
_CATEGORY_LABEL = {"no_answer": "не дозвониться", "slow_reply": "долго отвечают"}


def _esc(value: object) -> str:
    return html.escape(str(value)) if value not in (None, "") else ""


def format_org(org: Organization) -> str:
    """HTML-карточка организации для Telegram.

    Порядок под ручной аутрич: название · ниша → проблемы в отзывах → телефон → ссылка.
    """
    header = f"<b>{_esc(org.name)}</b>"
    if org.rubric:
        header += f" · {_esc(org.rubric)}"
    lines = [header, ""]

    # Главный хук — есть ли в отзывах жалобы на связь.
    if org.review_complaints:
        lines.append("⚠️ <b>Проблемы в отзывах:</b>")
        for quote in org.review_complaints[:3]:
            lines.append(f"• {_esc(quote)}")
    else:
        lines.append("✅ Явных жалоб на связь в отзывах нет")
    lines.append("")

    if org.phone:
        lines.append("📞 " + ", ".join(_esc(p) for p in org.phone))
    else:
        lines.append("📞 <i>телефон не найден</i>")

    if org.inn:
        lines.append(f"🧾 ИНН: <code>{_esc(org.inn)}</code>")
    if org.director:
        lines.append(f"👤 Руководитель: {_esc(org.director)}")
    if org.entity_type == "ИП":
        # Для ИП владелец = сам предприниматель, значит телефон выше звонит прямо ему.
        lines.append("🔥 <b>ИП</b> — владелец = сам предприниматель, телефон выше звонит ему напрямую")
    elif org.entity_type:
        lines.append(f"🏢 {_esc(org.entity_type)}")

    if org.website:
        lines.append(f"🌐 {_esc(org.website)}")
    if org.socials:
        lines.append("💬 " + ", ".join(f"{net}: {_esc(url)}" for net, url in org.socials.items()))

    if org.rating is not None:
        reviews = f", {org.reviews_count} отз." if org.reviews_count else ""
        lines.append(f"⭐ {_esc(org.rating)}{reviews}")

    # Оценка платёжеспособности лида (косвенная, по объёму/возрасту/типу/сайту).
    from .score import assess_solvency

    sol = assess_solvency(org)
    line = f"{sol['emoji']} {_esc(sol['label'])}"
    if sol["reasons"]:
        line += f" — {_esc(', '.join(sol['reasons']))}"
    lines.append(line)

    if org.source_url:
        lines.append(f"🔗 {_esc(org.source_url)}")
    return "\n".join(lines)


def _date_from_ms(ms: object) -> str:
    """Метка времени DaData (мс) -> 'ДД.ММ.ГГГГ'. Пусто, если не число."""
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%d.%m.%Y")
    except (TypeError, ValueError, OSError):
        return ""


_STATUS_LABEL = {
    "ACTIVE": "✅ Действует",
    "LIQUIDATING": "⚠️ Ликвидируется",
    "LIQUIDATED": "❌ Ликвидировано",
    "BANKRUPT": "❌ Банкротство",
    "REORGANIZING": "🔄 Реорганизация",
}


def format_company_card(data: dict) -> str:
    """HTML-карточка юрлица/ИП по данным DaData (кнопка «Подробнее по ИНН»).

    Только легально доступные по ИНН поля: название, тип, статус, ОГРН, адрес,
    руководитель, ОКВЭД. Личных контактов тут нет и быть не может (152-ФЗ).
    """
    name = (data.get("name") or {}).get("full_with_opf") or (data.get("name") or {}).get("full") or "—"
    kind = "ИП" if (data.get("type") or "").upper() == "INDIVIDUAL" else "Юрлицо"
    lines = [f"<b>{_esc(name)}</b>", f"📄 {kind}"]

    state = data.get("state") or {}
    status = _STATUS_LABEL.get(state.get("status") or "", state.get("status") or "")
    reg = _date_from_ms(state.get("registration_date") or data.get("ogrn_date"))
    if status:
        lines.append(f"{status}" + (f" · с {reg}" if reg else ""))

    if data.get("inn"):
        kpp = f" / КПП {_esc(data.get('kpp'))}" if data.get("kpp") else ""
        lines.append(f"🧾 ИНН {_esc(data['inn'])}{kpp}")
    if data.get("ogrn"):
        lines.append(f"🔢 ОГРН {_esc(data['ogrn'])}")

    director = _director_line(data)
    if director:
        lines.append(f"👤 {director}")

    address = (data.get("address") or {}).get("value")
    if address:
        lines.append(f"📍 {_esc(address)}")

    okved = data.get("okved")
    if okved:
        lines.append(f"🏷 ОКВЭД {_esc(okved)}")

    lines.append("")
    lines.append("<i>Личный телефон руководителя по ИНН не отдаёт ни один легальный источник — "
                 "звони на номер компании и проси по имени.</i>")
    return "\n".join(lines)


def _director_line(data: dict) -> str:
    """Строка руководителя для карточки по ИНН (ФИО + должность, если есть)."""
    management = data.get("management") or {}
    if management.get("name"):
        line = _esc(management["name"])
        if management.get("post"):
            line += _esc(f" — {management['post'].lower()}")
        return line
    fio = data.get("fio") or {}
    parts = [fio.get("surname"), fio.get("name"), fio.get("patronymic")]
    full = " ".join(p for p in parts if p).strip()
    return _esc(full) if full else ""


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Инлайн-клавиатура из строк кнопок (текст, callback_data) → reply_markup."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


async def post(client: httpx.AsyncClient, token: str, method: str, payload: dict) -> dict:
    """Публичная обёртка над Bot API (для bot.py) — с ретраем на 429."""
    return await _post(client, token, method, payload)


async def send_document(client: httpx.AsyncClient, token: str, chat_id: int | str,
                        path, caption: str = "") -> dict:
    """Отправить файл в чат (sendDocument, multipart). Для выгрузки лидов в Excel."""
    from pathlib import Path

    path = Path(path)
    with open(path, "rb") as fh:
        files = {"document": (path.name, fh.read())}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    resp = await client.post(_API.format(token=token, method="sendDocument"), data=data, files=files)
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description") or f"sendDocument HTTP {resp.status_code}")
    return result


async def _post(client: httpx.AsyncClient, token: str, method: str, payload: dict) -> dict:
    """POST к Bot API с одной повторной попыткой на 429 (Too Many Requests)."""
    resp = await client.post(_API.format(token=token, method=method), json=payload)
    data = resp.json()
    if data.get("ok"):
        return data
    if resp.status_code == 429:
        retry_after = data.get("parameters", {}).get("retry_after", 1)
        logger.warning("Telegram 429 — жду {}с и повторяю.", retry_after)
        await asyncio.sleep(retry_after + 0.5)
        resp = await client.post(_API.format(token=token, method=method), json=payload)
        data = resp.json()
        if data.get("ok"):
            return data

    # Разметка. Названия организаций приходят с чужих карточек, и однажды в
    # них будет угловая скобка: Telegram на такое отвечает ошибкой и не
    # показывает сообщение ВООБЩЕ — лид пропадает целиком вместе с телефоном.
    # Повторяем один раз простым текстом: без выделений, но с содержимым.
    trouble = str(data.get("description") or "").lower()
    if payload.get("parse_mode") and any(
        word in trouble for word in ("parse", "entity", "tag", "unsupported")
    ):
        plain = dict(payload)
        plain["parse_mode"] = None
        plain["text"] = html.unescape(re.sub(r"<[^>]+>", "", str(payload.get("text") or "")))
        resp = await client.post(_API.format(token=token, method=method), json=plain)
        data = resp.json()
        if data.get("ok"):
            logger.info("Отправил без разметки — Telegram не принял HTML.")
            return data

    raise RuntimeError(data.get("description") or f"Telegram HTTP {resp.status_code}")


async def recent_chats(settings: Settings | None = None) -> dict[int, str]:
    """chat_id недавних чатов бота (через getUpdates) — чтобы узнать свой chat_id."""
    settings = settings or get_settings()
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в .env.")
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        data = await _post(client, token, "getUpdates", {})
    chats: dict[int, str] = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    return chats


async def send_orgs(
    orgs: list[Organization],
    settings: Settings | None = None,
    chat_id: str | None = None,
    hot_only: bool = False,
) -> int:
    """Отправляем карточки организаций в Telegram. hot_only — только с сигналами в отзывах."""
    settings = settings or get_settings()
    token = settings.telegram_bot_token
    chat_id = chat_id or settings.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в .env.")

    targets = [org for org in orgs if org.review_complaints] if hot_only else list(orgs)
    if not targets:
        logger.warning("Telegram: нечего отправлять (после фильтра hot_only={}).", hot_only)
        return 0

    suffix = " (только с сигналами в отзывах)" if hot_only else ""
    sent = 0
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        await _post(client, token, "sendMessage",
                    {"chat_id": chat_id, "text": f"🗂 Лиды: {len(targets)}{suffix}"})
        for org in targets:
            try:
                await _post(
                    client,
                    token,
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": format_org(org),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001 — одно сообщение не должно ронять всю рассылку
                logger.warning("Telegram: {} — {}", org.name, exc)
            await asyncio.sleep(settings.request_delay)  # вежливый rate limiting

    logger.info("Telegram: отправлено {} из {} организаций.", sent, len(targets))
    return sent


# --- Секреты наружу не отдаём -------------------------------------------------

# Логин и пароль внутри URL: «http://user:pass@host:port». Так их пишут прокси,
# и так они попадают в текст исключения httpx и playwright.
_URL_CREDENTIALS_RE = re.compile(r"(?<=//)([^/\s:@]+):([^/\s@]+)@")
# Ключи и токены в свободном тексте.
_TOKEN_RE = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{8,}|\d{6,}:[A-Za-z0-9_\-]{20,}|[A-Fa-f0-9]{32,})\b"
)


def safe_text(text: str) -> str:
    """Текст без паролей и ключей — для отправки человеку в чат.

    Сообщение об ошибке уходит в телеграм как есть, а прокси пишет в текст
    исключения полный URL вместе с логином и паролем: «Cannot connect to proxy
    http://user:pass@host:port». Один такой сбой — и доступ к прокси лежит в
    переписке навсегда, включая пересланные сообщения.
    """
    cleaned = _URL_CREDENTIALS_RE.sub("***:***@", str(text or ""))
    return _TOKEN_RE.sub("***", cleaned)

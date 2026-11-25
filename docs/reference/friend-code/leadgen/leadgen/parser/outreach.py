"""Email-аутрич по агентствам-партнёрам: читаем выгрузку partners и шлём письма малыми партиями.

Правила зашиты из брифа продаж и EMAIL_OUTREACH.md (юридика — там же, раздел 0):
- по умолчанию DRY-RUN: письма печатаются, но НЕ отправляются (отправка — явным --send);
- лимит писем в день (OUTREACH_DAILY_LIMIT, по умолчанию 10) + пауза 60–120с между письмами;
- каждому домену пишем ОДИН раз (+ один follow-up через 3 рабочих дня), журнал — outreach_sent.json;
- шаблон выбирается по колонке pitch: «сайты + CRM» / «только CRM», остальных пропускаем;
- цену не называем, «без абонплаты/0 ₽» не пишем, в каждом письме строка отписки;
- только корпоративные ящики: адреса на бесплатных доменах (gmail/mail.ru/…) пропускаем.

Отправка — стандартный smtplib (без внешних зависимостей). Порт 465 = SSL, иначе STARTTLS.
"""
from __future__ import annotations

import json
import os
import random
import smtplib
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from loguru import logger

from .config import Settings, get_settings
from .models import normalize_domain

# Питчи, которым пишем. Остальные («пропустить…», «уточнить вручную») не трогаем.
TARGET_PITCHES = {"сайты + CRM", "только CRM"}

# Личные ящики физлиц не трогаем (см. EMAIL_OUTREACH.md, правило 3):
# адрес на бесплатном домене — это не корпоративный контакт агентства.
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru",
    "yandex.ru", "ya.ru", "rambler.ru", "icloud.com", "outlook.com", "hotmail.com",
})

# Через сколько РАБОЧИХ дней без ответа уходит follow-up (правило брифа).
FOLLOWUP_BUSINESS_DAYS = 3

# Пауза между реальными отправками, сек: мгновенная пачка = типичный признак бота.
SEND_PAUSE_RANGE = (60.0, 120.0)

_SIGNATURE = "Менеджер Uniqore · uniqore.ru · +7 961 649-51-07 · TG: @UniqoreManager"
_UNSUBSCRIBE = "Если неактуально — просто ответьте «стоп», больше не побеспокою."

# Шаблоны нарочно РАЗНЫЕ по структуре (одинаковый скелет палится и получателем,
# и спам-фильтром). Плейсхолдеры: {name}, {niche_line}, {partner_line}.
# Цены нет нигде — она для созвона. Цель письма — 15 минут разговора, не продажа.
TEMPLATES: dict[str, list[dict[str, str]]] = {
    "сайты + CRM": [
        {
            "subject": "Сайты и CRM для клиентов {name} — партнёрство",
            "body": (
                "Здравствуйте!\n\n"
                "Видел {name}{niche_line} — судя по кейсам, разработку сайтов вы не делаете сами.\n"
                "Мы — техническая команда Uniqore: собираем сайты и CRM-ботов для клиентов "
                "агентств как белый субподряд. Вы продаёте под своим брендом, мы делаем руками, "
                "агентству — комиссия с каждого проекта.\n\n"
                "{partner_line}"
                "Есть смысл созвониться на 15 минут? Покажу живые проекты и расскажу условия.\n\n"
                f"{_SIGNATURE}\n{_UNSUBSCRIBE}\n"
            ),
        },
        {
            "subject": "Партнёрство для {name}: закрываем «а сайт вы сделаете?»",
            "body": (
                "Добрый день!\n\n"
                "Частая история у агентств: клиент просит сайт или CRM, а держать разработчиков "
                "невыгодно. Мы в Uniqore закрываем это как субподряд — под вашим брендом, "
                "с вашей наценкой.\n\n"
                "Почему пишу именно вам: {name}{niche_line} — ваши клиенты как раз те, кому "
                "мы обычно строим сайты и ботов для заявок.\n\n"
                "{partner_line}"
                "Готов показать примеры на коротком созвоне — 15 минут, без обязательств.\n\n"
                f"{_SIGNATURE}\n{_UNSUBSCRIBE}\n"
            ),
        },
    ],
    "только CRM": [
        {
            "subject": "CRM-боты для клиентов {name}",
            "body": (
                "Здравствуйте!\n\n"
                "{name}{niche_line} — сайты вы делаете, а вот CRM-ботов и автоматизацию заявок, "
                "судя по услугам, отдаёте или не берёте. Мы в Uniqore делаем ровно это: боты, "
                "которые ловят заявки и пропущенные обращения клиентов, под брендом агентства.\n\n"
                "{partner_line}"
                "Предлагаю 15-минутный созвон: покажу, как это выглядит у действующих партнёров.\n\n"
                f"{_SIGNATURE}\n{_UNSUBSCRIBE}\n"
            ),
        },
        {
            "subject": "{name} + Uniqore: автоматизация заявок для ваших клиентов",
            "body": (
                "Добрый день!\n\n"
                "Пишу с конкретным предложением для {name}. Ваши клиенты{niche_line} теряют "
                "заявки на пропущенных звонках и сообщениях — мы строим CRM-ботов, которые это "
                "закрывают. Для агентств работаем как белый субподряд: вы продаёте, мы делаем.\n\n"
                "{partner_line}"
                "15 минут созвона хватит, чтобы понять, интересно ли. Когда удобно?\n\n"
                f"{_SIGNATURE}\n{_UNSUBSCRIBE}\n"
            ),
        },
    ],
}

FOLLOWUP_TEMPLATE = {
    "subject": "Re: партнёрство с {name}",
    "body": (
        "Здравствуйте!\n\n"
        "Писал вам пару дней назад про субподряд по сайтам и CRM для клиентов {name} — "
        "возможно, письмо утонуло. Если тема в принципе интересна, предложите удобное "
        "время для короткого созвона; если нет — одно слово «стоп», и я больше не пишу.\n\n"
        f"{_SIGNATURE}\n"
    ),
}


@dataclass
class Target:
    """Один получатель из выгрузки partners."""

    name: str
    domain: str
    email: str
    pitch: str
    niche_tags: str = ""
    partner_page_url: str = ""


@dataclass
class Letter:
    """Готовое к отправке письмо (или его dry-run превью)."""

    target: Target
    subject: str
    body: str
    kind: str = "first"  # first | followup


def _clean_cell(value: object) -> str:
    """Ячейка pandas -> строка ('' для NaN/None): в XLSX пустые значения приходят как NaN."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def load_targets(path: str | Path) -> list[Target]:
    """Выгрузка partners (.xlsx/.csv) -> цели рассылки.

    Фильтры (каждый отсев логируем, чтобы «мало писем» не выглядело загадкой):
    целевой pitch, есть best_email, домен почты не бесплатный.
    """
    import pandas as pd

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Файл выгрузки не найден: {path}. Сначала: parser partners …")
    df = pd.read_excel(path) if path.suffix.lower() in {".xlsx", ".xls"} else pd.read_csv(path)

    required = {"name", "website", "pitch", "best_email"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"В {path.name} нет колонок {sorted(missing)} — это выгрузка parser partners?"
        )

    targets: list[Target] = []
    skipped_pitch = skipped_email = skipped_free = 0
    for row in df.to_dict("records"):
        pitch = _clean_cell(row.get("pitch"))
        email = _clean_cell(row.get("best_email")).lower()
        name = _clean_cell(row.get("name"))
        domain = normalize_domain(_clean_cell(row.get("website"))) or ""
        if pitch not in TARGET_PITCHES:
            skipped_pitch += 1
            continue
        if not email or "@" not in email:
            skipped_email += 1
            continue
        if email.rsplit("@", 1)[1] in FREE_MAIL_DOMAINS:
            skipped_free += 1
            continue
        targets.append(
            Target(
                name=name or domain,
                domain=domain or email.rsplit("@", 1)[1],
                email=email,
                pitch=pitch,
                niche_tags=_clean_cell(row.get("niche_tags")),
                partner_page_url=_clean_cell(row.get("partner_page_url")),
            )
        )
    logger.info(
        "Outreach: в файле {} строк; целей {} (отсеяно: питч не наш {}, нет email {}, "
        "личная почта {}).",
        len(df), len(targets), skipped_pitch, skipped_email, skipped_free,
    )
    return targets


# --- журнал отправок ----------------------------------------------------------


class Journal:
    """Кому/когда/что писали. Один домен = одно письмо (+ один follow-up). JSON на диске.

    Формат: {домен: {"email", "sent_at", "template", "kind", "followup_sent_at"?, "replied"?}}.
    Поле "replied" проставляется ВРУЧНУЮ владельцем (true/строка) — тогда follow-up не уходит.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # Битый журнал — стоп, а не молча с нуля: иначе всем адресатам уйдёт повтор.
                raise RuntimeError(
                    f"Журнал {self.path} повреждён ({exc}). Почини или удали его осознанно."
                ) from exc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # атомарно: обрыв на середине не портит журнал

    def already_sent(self, domain: str) -> bool:
        return domain in self._data

    def mark_sent(self, letter: Letter, now: datetime) -> None:
        if letter.kind == "followup":
            entry = self._data.get(letter.target.domain, {})
            entry["followup_sent_at"] = now.isoformat(timespec="seconds")
            self._data[letter.target.domain] = entry
        else:
            self._data[letter.target.domain] = {
                "email": letter.target.email,
                "name": letter.target.name,  # для персонализации follow-up
                "pitch": letter.target.pitch,
                "sent_at": now.isoformat(timespec="seconds"),
                "subject": letter.subject,
                "kind": letter.kind,
            }
        self._save()

    def sent_today(self, today: date) -> int:
        """Сколько писем (первых + follow-up) уже ушло сегодня — для дневного лимита."""
        count = 0
        for entry in self._data.values():
            for key in ("sent_at", "followup_sent_at"):
                stamp = entry.get(key)
                if stamp and datetime.fromisoformat(stamp).date() == today:
                    count += 1
        return count

    def followup_due(self, today: date) -> list[dict]:
        """Записи, которым пора слать follow-up: прошло >=3 рабочих дней, ответа и догона нет."""
        due = []
        for domain, entry in self._data.items():
            if entry.get("replied") or entry.get("followup_sent_at"):
                continue
            sent = datetime.fromisoformat(entry["sent_at"]).date()
            if business_days_between(sent, today) >= FOLLOWUP_BUSINESS_DAYS:
                due.append({"domain": domain, **entry})
        return due


def business_days_between(start: date, end: date) -> int:
    """Полные рабочие дни (пн–пт) между датами, не считая стартовый день."""
    if end <= start:
        return 0
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


# --- сборка письма ------------------------------------------------------------


def render_letter(target: Target, template_index: int = 0) -> Letter:
    """Шаблон по питчу + персонализация. Индекс ротирует варианты между письмами."""
    variants = TEMPLATES[target.pitch]
    template = variants[template_index % len(variants)]

    # «Универсальное» — это отсутствие ниши, упоминать нечего. Скобки, а не тире:
    # вставка живёт посреди чужой фразы и не должна ломать её синтаксис.
    niches = [n.strip() for n in target.niche_tags.split(",") if n.strip() and n.strip() != "универсальное"]
    niche_line = f" (вы работаете с нишами {', '.join(niches)})" if niches else ""
    partner_line = (
        "Кстати, видел у вас страницу для партнёров — значит, формат вам знаком.\n\n"
        if target.partner_page_url else ""
    )
    return Letter(
        target=target,
        subject=template["subject"].format(name=target.name),
        body=template["body"].format(name=target.name, niche_line=niche_line, partner_line=partner_line),
        kind="first",
    )


def render_followup(target: Target) -> Letter:
    return Letter(
        target=target,
        subject=FOLLOWUP_TEMPLATE["subject"].format(name=target.name),
        body=FOLLOWUP_TEMPLATE["body"].format(name=target.name),
        kind="followup",
    )


# --- отправка -----------------------------------------------------------------


def smtp_ready(settings: Settings) -> tuple[bool, str]:
    """Заполнены ли SMTP-настройки. Вторая часть — что именно не хватает (для сообщений)."""
    missing = [
        var for var, value in (
            ("SMTP_HOST", settings.smtp_host),
            ("SMTP_USER", settings.smtp_user),
            ("SMTP_PASSWORD", settings.smtp_password),
        ) if not value
    ]
    return not missing, ", ".join(missing)


def send_via_smtp(settings: Settings, letter: Letter) -> None:
    """Одно письмо через SMTP. 465 -> SSL, другой порт -> STARTTLS."""
    msg = EmailMessage()
    msg["From"] = formataddr((settings.smtp_from_name, settings.smtp_user))
    msg["To"] = letter.target.email
    msg["Subject"] = letter.subject
    msg.set_content(letter.body)

    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    try:
        if settings.smtp_port != 465:
            server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    finally:
        # QUIT отдельно и молча: `with` бросает, если сервер ответил не 221 (напр. 421 при
        # rate-limit) — но письмо к этому моменту УЖЕ принято, и такая ошибка не должна
        # выглядеть как несостоявшаяся отправка, иначе адресат не попадёт в журнал
        # и получит то же холодное письмо второй раз.
        with suppress(Exception):  # quit() после отправки, письмо уже ушло
            server.quit()


# --- оркестрация --------------------------------------------------------------


@dataclass
class OutreachResult:
    prepared: list[Letter] = field(default_factory=list)
    sent: int = 0
    skipped_already: int = 0
    limited: int = 0
    dry_run: bool = True


def run_outreach(
    file: str | Path | None,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    send: bool = False,
    followup: bool = False,
    journal_path: str | Path | None = None,
    send_fn: Callable[[Settings, Letter], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> OutreachResult:
    """Главный вход: собрать письма, применить журнал и лимит, отправить (или показать).

    send=False (по умолчанию) — dry-run: письма только формируются, ничего не уходит.
    followup=True — вместо первых писем шлём догоны тем, кому пора (3 рабочих дня).
    send_fn/sleep_fn/now — инъекции для тестов (реальная отправка = send_via_smtp).
    """
    settings = settings or get_settings()
    now = now or datetime.now()
    # НЕ `limit or …`: явный 0 («поставить на паузу») обязан означать ноль писем,
    # а не подмену дефолтом с реальной отправкой десятка холодных писем.
    limit = settings.outreach_daily_limit if limit is None else limit
    journal = Journal(journal_path or (settings.output_dir / "outreach_sent.json"))
    result = OutreachResult(dry_run=not send)

    if send:
        ready, missing = smtp_ready(settings)
        if not ready:
            raise RuntimeError(
                f"SMTP не настроен: заполни {missing} в .env (см. EMAIL_OUTREACH.md, раздел 2)."
            )

    # --- какие письма кандидаты ---
    letters: list[Letter] = []
    if followup:
        for entry in journal.followup_due(now.date()):
            target = Target(
                # У старых записей журнала поля name могло не быть — тогда домен.
                name=entry.get("name") or entry["domain"],
                domain=entry["domain"],
                email=entry["email"],
                pitch=entry.get("pitch") or "сайты + CRM",  # для догона шаблон один
            )
            letters.append(render_followup(target))
    else:
        if file is None:
            raise ValueError("Укажи --file с выгрузкой parser partners (или --followup).")
        seen_domains: set[str] = set()  # «один домен = одно письмо» и ВНУТРИ запуска:
        for index, target in enumerate(load_targets(file)):
            if journal.already_sent(target.domain) or target.domain in seen_domains:
                result.skipped_already += 1  # склейка выгрузок даёт дубли доменов
                continue
            seen_domains.add(target.domain)
            letters.append(render_letter(target, template_index=index))

    # --- дневной лимит (учитывает уже отправленное сегодня) ---
    budget = max(0, limit - journal.sent_today(now.date()))
    if len(letters) > budget:
        result.limited = len(letters) - budget
        letters = letters[:budget]
    result.prepared = letters

    if not letters:
        logger.info("Outreach: слать нечего (журнал/лимит/фильтры). Пропущено как повторы: {}.",
                    result.skipped_already)
        return result

    # --- отправка или dry-run ---
    sender = send_fn or send_via_smtp
    for i, letter in enumerate(letters):
        if not send:
            continue  # dry-run: письма уже в result.prepared, наружу их печатает CLI
        if i > 0:
            sleep_fn(random.uniform(*SEND_PAUSE_RANGE))  # пауза «как человек», не пачкой
        sender(settings, letter)
        journal.mark_sent(letter, now)
        result.sent += 1
        logger.info("Outreach: отправлено {} ({}) — {}", letter.target.email,
                    letter.kind, letter.subject)
    return result

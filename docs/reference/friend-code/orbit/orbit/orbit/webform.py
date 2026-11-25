"""Форма обратной связи на сайте компании — запасной канал.

Зачем: у части компаний почты на сайте нет вовсе, зато есть форма «оставьте
заявку». Формально это тот же способ связаться, которым пользуется любой
посетитель.

ВАЖНО: отправка по умолчанию ВЫКЛЮЧЕНА и включается только настройкой
WEBFORM_ENABLED. Пока владелец не сказал «включай», модуль умеет лишь
находить формы и показывать, что он нашёл. Ни одна форма не отправляется,
даже если очень хочется: заявка уходит живому человеку, и ошибка здесь
стоит дороже, чем неотправленное письмо.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from loguru import logger

from .config import get_settings
from .models import Contact
from .sitescan import PAUSE_SECONDS, TIMEOUT, USER_AGENT, Robots, site_url, visible_text

# Страницы, где обычно живёт форма.
PAGES: tuple[str, ...] = ("", "/contacts", "/kontakty", "/contact", "/zayavka", "/feedback")

_FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_ACTION_RE = re.compile(r"\baction=[\"']([^\"']*)[\"']", re.IGNORECASE)
_METHOD_RE = re.compile(r"\bmethod=[\"']([^\"']*)[\"']", re.IGNORECASE)
_FIELD_RE = re.compile(
    r"<(input|textarea|select)\b([^>]*)>", re.IGNORECASE | re.DOTALL
)
_ATTR_RE = re.compile(r"(\w[\w-]*)\s*=\s*[\"']([^\"']*)[\"']")

# Как называются поля, которые нам нужно заполнить. Списки намеренно длинные:
# единого стандарта нет, каждый сайт называет поля по-своему.
ROLES: dict[str, tuple[str, ...]] = {
    "name": ("name", "fio", "imya", "имя", "username", "your-name", "client"),
    "phone": ("phone", "tel", "телефон", "your-phone"),
    "email": ("email", "mail", "почта", "e-mail", "your-email"),
    "message": ("message", "comment", "text", "question", "сообщение", "vopros", "your-message"),
}
# Поля, которые заполнять нельзя: это ловушки для роботов. Заполнил — заявка
# полетит в спам, а сайт запомнит наш адрес.
HONEYPOT_HINTS = ("honeypot", "hp_", "_gotcha", "winnie", "trap", "spam")

# Признаки, что форма — не обратная связь, а что-то другое.
NOT_FEEDBACK = ("search", "poisk", "login", "signin", "auth", "subscribe", "newsletter", "cart")


@dataclass
class Field:
    """Одно поле формы."""

    name: str
    kind: str = "text"       # text | textarea | hidden | select | checkbox
    value: str = ""
    required: bool = False
    role: str = ""           # name | phone | email | message | ""


@dataclass
class Form:
    """Найденная форма обратной связи."""

    url: str                 # страница, где она стоит
    action: str              # куда отправлять
    method: str = "post"
    fields: list[Field] = field(default_factory=list)

    @property
    def roles(self) -> set[str]:
        return {f.role for f in self.fields if f.role}

    @property
    def usable(self) -> bool:
        """Годится ли для заявки: нужно, куда написать текст и как нас найти."""
        return "message" in self.roles and bool({"phone", "email"} & self.roles)

    def describe(self) -> str:
        known = ", ".join(sorted(self.roles)) or "поля не распознаны"
        return f"{self.url} → {self.action or 'та же страница'} ({known})"


def _attrs(raw: str) -> dict[str, str]:
    return {key.lower(): value for key, value in _ATTR_RE.findall(raw)}


def _role_of(name: str, placeholder: str = "") -> str:
    haystack = f"{name} {placeholder}".lower()
    for role, words in ROLES.items():
        if any(word in haystack for word in words):
            return role
    return ""


def _is_honeypot(attributes: dict[str, str], name: str) -> bool:
    if any(hint in name.lower() for hint in HONEYPOT_HINTS):
        return True
    style = attributes.get("style", "").replace(" ", "").lower()
    return "display:none" in style or "hidden" in attributes.get("class", "").lower()


def parse_forms(html: str, page_url: str) -> list[Form]:
    """Все формы со страницы, кроме поиска, входа и подписки."""
    forms: list[Form] = []
    for match in _FORM_RE.finditer(html or ""):
        head = html[match.start() : match.start(1)]
        inner = match.group(1)
        attributes = _attrs(head)

        marker = " ".join([
            attributes.get("id", ""), attributes.get("class", ""), attributes.get("action", "")
        ]).lower()
        if any(word in marker for word in NOT_FEEDBACK):
            continue

        fields: list[Field] = []
        skip = False
        for field_match in _FIELD_RE.finditer(inner):
            tag = field_match.group(1).lower()
            attrs = _attrs(field_match.group(2))
            name = attrs.get("name", "")
            if not name:
                continue
            kind = "textarea" if tag == "textarea" else attrs.get("type", "text").lower()
            if kind in ("submit", "button", "image", "file"):
                continue
            if _is_honeypot(attrs, name):
                # Ловушку не трогаем, но и форму не бракуем: скрытое поле
                # часто соседствует с обычной формой обратной связи.
                continue
            if kind in ("password",):
                skip = True
                break
            role = "" if kind == "hidden" else _role_of(name, attrs.get("placeholder", ""))
            fields.append(Field(
                name=name,
                kind="hidden" if kind == "hidden" else ("textarea" if tag == "textarea" else kind),
                value=attrs.get("value", ""),
                required="required" in field_match.group(2).lower(),
                role=role,
            ))
        if skip or not fields:
            continue

        action = _ACTION_RE.search(head)
        method = _METHOD_RE.search(head)
        forms.append(Form(
            url=page_url,
            action=urljoin(page_url, action.group(1)) if action and action.group(1) else page_url,
            method=(method.group(1).lower() if method else "post"),
            fields=fields,
        ))
    return forms


class SiteUnreachable(RuntimeError):
    """Ни одна страница сайта не открылась.

    Отдельно от «формы нет» намеренно. Пометка в карточке ставится навсегда и
    навсегда же выводит компанию из обхода: сайт, прилёгший на минуту, терял
    свою форму обратной связи насовсем. «Не смогли посмотреть» и «посмотрели,
    формы нет» — разные новости, и путать их нельзя.
    """


def find(site: str, *, client: httpx.Client | None = None, robots: Robots | None = None) -> Form | None:
    """Ищет на сайте форму обратной связи. None — не нашли.

    Поднимает SiteUnreachable, если не открылась ни одна страница: это не
    ответ на вопрос «есть ли форма», а отсутствие ответа.
    """
    import time

    base = site_url(site)
    if not base:
        return None

    robots = robots or Robots()
    close_after = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    )
    opened = 0
    try:
        for path in PAGES:
            url = urljoin(base + "/", path.lstrip("/")) if path else base
            if not robots.allowed(client, url):
                continue
            try:
                response = client.get(url)
            except httpx.HTTPError:
                continue
            time.sleep(PAUSE_SECONDS)
            if response.status_code != 200:
                continue
            opened += 1
            for form in parse_forms(response.text, str(response.url)):
                if form.usable:
                    return form
        if not opened:
            raise SiteUnreachable(f"ни одна страница {base} не открылась")
        return None
    finally:
        if close_after:
            client.close()


def fill(form: Form, contact: Contact, text: str) -> dict[str, str]:
    """Готовит, что именно отправится. Ничего не отправляет."""
    settings = get_settings()
    values: dict[str, str] = {}
    for item in form.fields:
        if item.kind == "hidden":
            values[item.name] = item.value  # токены CSRF и служебные поля — как есть
            continue
        if item.role == "name":
            values[item.name] = settings.smtp_from_name
        elif item.role == "email":
            values[item.name] = settings.smtp_user or ""
        elif item.role == "phone":
            values[item.name] = settings.contact_phone or ""
        elif item.role == "message":
            values[item.name] = text
        elif item.required:
            # Обязательное поле без понятной роли: пустым его не пропустят,
            # а выдумывать значение нельзя — форма просто не наш случай.
            values[item.name] = ""
    return values


class Disabled(RuntimeError):
    """Отправка форм выключена настройкой."""


def submit(form: Form, contact: Contact, text: str, *, client: httpx.Client | None = None) -> bool:
    """Отправляет заявку. Работает только при включённой настройке.

    Предохранитель здесь, а не у вызывающего: заявка уходит живому человеку,
    и «случайно отправилось» — это не та ошибка, которую можно отменить.
    """
    settings = get_settings()
    if not settings.webform_enabled:
        raise Disabled(
            "Отправка форм выключена. Включается WEBFORM_ENABLED=true в orbit/.env"
        )

    payload = fill(form, contact, text)
    close_after = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        response = (
            client.post(form.action, data=payload)
            if form.method == "post"
            else client.get(form.action, params=payload)
        )
    except httpx.HTTPError as exc:
        logger.warning("Форма {} не приняла заявку: {}", form.action, exc)
        return False
    finally:
        if close_after:
            client.close()

    ok = response.status_code in (200, 201, 202, 302)
    logger.info("Форма {}: ответ {}", form.action, response.status_code)
    return ok


def looks_accepted(html: str) -> bool:
    """Похоже ли, что заявку приняли. Нужно для будущей проверки отправки."""
    text = visible_text(html).lower()
    return any(word in text for word in (
        "спасибо", "заявка принята", "мы свяжемся", "успешно отправлено", "ваше сообщение отправлено",
    ))


FORM_KEY = "webform"


def scout(store, kind, *, limit: int = 20) -> tuple[int, int]:
    """Обходит сайты и запоминает, у кого есть форма. Ничего не отправляет."""
    candidates = [
        contact
        for contact in store.contacts(kind, limit=1000)
        if contact.site and not contact.reachable and FORM_KEY not in contact.meta
    ][:limit]
    if not candidates:
        return 0, 0

    robots = Robots()
    found = 0
    with httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    ) as client:
        for contact in candidates:
            try:
                form = find(contact.site or "", client=client, robots=robots)
            except SiteUnreachable as exc:
                # Пометку НЕ ставим: она навсегда, а мы ничего не увидели.
                # С пометкой «формы нет» компания выпадала из обхода насовсем
                # из-за сайта, прилёгшего на минуту.
                logger.info("{}: сайт не открылся — вернусь позже ({})", contact.name, exc)
                continue
            except Exception as exc:  # noqa: BLE001 — один сайт не роняет проход
                logger.warning("{}: форму найти не вышло — {}", contact.name, exc)
                continue
            # Через update_meta, а не set_meta: обход чужого сайта занимает
            # минуты, и владелец за это время успевает нажать кнопку, которая
            # пишет пометки. Запись всего словаря целиком стирала его правку.
            store.update_meta(contact.pk or 0, {FORM_KEY: form.describe() if form else "нет"})
            found += int(bool(form))
    logger.info("Формы обратной связи: обошли {}, нашли {}", len(candidates), found)
    return len(candidates), found

"""Чтение почтового ящика: ответы и возвраты.

Письмо ушло — и дальше тишина, потому что смотреть в ящик было некому.
Здесь бот сам заходит по IMAP и разбирает, что пришло:

* ответ живого человека — двигаем контакт в «Начали общение» и зовём владельца;
* возврат (адрес не существует, ящик переполнен) — помечаем контакт, чтобы
  больше туда не писать: повторные письма на мёртвый адрес портят репутацию
  домена быстрее всего остального;
* автоответ «я в отпуске» — отмечаем, но за ответ не считаем и владельца
  не дёргаем.

Флаги писем не трогаем: ящиком пользуется живой человек, и «прочитано» там
должно значить «прочитал человек». Своё место помним по UID.
"""
from __future__ import annotations

import email
import imaplib
import re
from contextlib import suppress
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message as EmailMessage
from email.utils import parseaddr

from loguru import logger

from . import llm
from .config import get_settings
from .models import Kind, Stage
from .outreach import looks_like_stop
from .store import Store, now_msk, today_msk
from .ui import esc

# Beget держит IMAP на этом хосте. Порт 993 = SSL.
IMAP_PORT = 993
LAST_UID_KEY = "inbox:last_uid"
# День последнего удачного захода в ящик. Нужен, только когда место в ящике
# потеряно: тогда по нему считается, с какого дня читать (см. _first_day).
LAST_READ_KEY = "inbox:last_read_day"
# Сколько писем разбираем за один заход. Ящик может накопиться за время
# простоя, но вываливать владельцу сотню уведомлений разом незачем.
BATCH = 30
# Сколько символов ответа показываем в уведомлении.
PREVIEW = 700

# Что письмо значит для нас.
REPLY = "reply"
BOUNCE = "bounce"
AUTO = "auto"
OTHER = "other"

_BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon", "mailerdaemon")
_BOUNCE_SUBJECTS = (
    "undelivered", "undeliverable", "delivery status notification", "delivery failure",
    "mail delivery failed", "returned mail", "failure notice",
    "не доставлено", "недоставленное", "возврат письма",
)
_AUTO_SUBJECTS = (
    "автоответ", "auto-reply", "autoreply", "out of office", "отпуск", "вне офиса",
)
# Отчёты почтового сервера бывают двух пород, и путать их дорого. «Delayed» —
# это «пока не доставили, продолжаю пытаться»: письмо ещё может дойти, а мы по
# такому отчёту закрывали контакт навсегда. Настоящий отказ приходит отдельным
# письмом позже.
_DELAY_MARKS = (
    "delayed", "delay notification", "warning: message still undelivered",
    "still being delivered", "задержк", "отложен",
)
# Код ответа сервера в теле отчёта: 4xx — временно, 5xx — окончательно.
_SMTP_CODE_RE = re.compile(r"\b([45])\.\d{1,3}\.\d{1,3}\b|\bstatus:\s*([45])\.", re.IGNORECASE)
# Поле Action из машинного отчёта о доставке (RFC 3464). Оно и есть настоящий
# ответ на вопрос «это ещё пытаются или уже сдались», а код 4xx/5xx — нет.
# Postfix, простояв письмо в очереди положенный срок, возвращает его с
# Action: failed и при этом СОХРАНЯЕТ статус 4.4.1: сервер получателя всё это
# время отвечал «занят». По коду такой отчёт неотличим от промежуточного
# «пока не доставили» — и окончательный отказ читался как задержка: адрес
# оставался живым, владельцу не говорили ничего, а письмо не дошло и не дойдёт.
_DSN_ACTION_RE = re.compile(r"^\s*action:\s*(failed|delayed|delivered|expanded|relayed)",
                            re.IGNORECASE | re.MULTILINE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Пометки в карточке.
BOUNCED_KEY = "email_bounced"
AUTOREPLY_KEY = "autoreply_at"


@dataclass
class Letter:
    """Разобранное письмо из ящика."""

    uid: int
    sender: str
    subject: str
    body: str
    auto_submitted: bool = False

    @property
    def kind(self) -> str:
        low_subject = self.subject.lower()
        low_sender = self.sender.lower()
        # Успешный отчёт о доставке проверяем ПЕРВЫМ: и отправитель, и тема у
        # него ровно те же, что у возврата, а значит любая проверка ниже
        # объявила бы доставленное письмо не дошедшим.
        if self.delivery_succeeded:
            return OTHER
        if any(word in low_sender for word in _BOUNCE_SENDERS):
            return BOUNCE
        if any(word in low_subject for word in _BOUNCE_SUBJECTS):
            return BOUNCE
        if self.auto_submitted or any(word in low_subject for word in _AUTO_SUBJECTS):
            return AUTO
        return REPLY

    @property
    def delivery_succeeded(self) -> bool:
        """Отчёт о ДОСТАВКЕ, а не о возврате: письмо дошло.

        Такие отчёты (RFC 3464, Action: delivered / relayed / expanded) шлют
        и шлюзы, и списки рассылки, и почтовые серверы, настроенные слать
        подтверждения. Заголовок у них тот же — «Delivery Status Notification»,
        отправитель тот же mailer-daemon, — и по теме они неотличимы от
        возврата. Раньше такой отчёт разбирался как окончательный отказ:
        живой адрес вычёркивался, а компания с одним ящиком закрывалась
        навсегда ровно в ту минуту, когда письмо до неё дошло.
        """
        action = _DSN_ACTION_RE.search(self.body)
        return bool(action) and action.group(1).lower() in ("delivered", "relayed", "expanded")

    @property
    def temporary_bounce(self) -> bool:
        """Отчёт «пока не доставили», а не «доставить не смогли».

        Почтовые серверы шлют такие каждые несколько часов, пока пытаются. По
        ним контакт закрывать нельзя: письмо ещё может дойти, а окончательный
        отказ придёт отдельным письмом.
        """
        # Сначала машинный отчёт: он говорит прямо, а не намёками.
        action = _DSN_ACTION_RE.search(self.body)
        if action:
            return action.group(1).lower() == "delayed"

        head = f"{self.subject}\n{self.body[:2000]}".lower()
        if any(mark in head for mark in _DELAY_MARKS):
            return True
        match = _SMTP_CODE_RE.search(head)
        return bool(match) and (match.group(1) or match.group(2)) == "4"

    def failed_address(self) -> str | None:
        """Чей адрес не принял письмо. Ищем в теле возврата."""
        settings = get_settings()
        ours = (settings.smtp_user or "").lower()
        for match in _EMAIL_RE.finditer(self.body):
            address = match.group(0).lower()
            # Свой адрес и адреса почтовых служб в возврате всегда есть —
            # нам нужен чужой.
            if address == ours or address.startswith(_BOUNCE_SENDERS):
                continue
            if any(word in address for word in _BOUNCE_SENDERS):
                continue
            return address
        return None


def _text(value) -> str:
    """Заголовок письма → нормальная строка, даже если он в кодировке."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — кривой заголовок не повод падать
        return str(value)


def body_of(message: EmailMessage) -> str:
    """Текст письма. Берём text/plain, если его нет — чистим HTML."""
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() == "message/delivery-status":
                # Машинная часть возврата — контейнер, а не текст:
                # get_payload(decode=True) на ней всегда None, и строки
                # «Status: 5.1.1» / «Final-Recipient» молча терялись. На
                # возвратах, где кода нет в человекочитаемой части, различие
                # «временный/постоянный» держится ровно на этих строках.
                parts.extend(str(sub) for sub in part.get_payload())
                continue
            if part.get_content_type() == "text/plain":
                parts.append(_decode(part))
        # Проверяем, есть ли ТЕКСТ, а не просто набранный список. Почтовые
        # клиенты сплошь и рядом кладут в multipart/alternative пустую
        # текстовую часть рядом с настоящим html. Пустая строка делала список
        # непустым, запасной путь пропускался, и живой ответ клиента приезжал
        # владельцу как «(пустое письмо)» — то есть терялся целиком.
        if not any(part.strip() for part in parts):
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    parts.append(_from_html(_decode(part)))
    else:
        raw = _decode(message)
        parts.append(_from_html(raw) if "html" in message.get_content_type() else raw)
    return "\n".join(part for part in parts if part).strip()


# Куски html, у которых содержимое — не текст письма, а инструкции браузеру.
_NOT_TEXT_RE = re.compile(
    r"<(style|script|head|title)\b.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL
)
# Теги, на месте которых должен остаться перевод строки, а не пробел.
_BREAKS_RE = re.compile(r"<(br|/p|/div|/tr|/li|/h[1-6])\b[^>]*>", re.IGNORECASE)


def _from_html(raw: str) -> str:
    """Текст письма из html-части. Разметку убираем, содержимое — оставляем.

    Резать одним `<[^>]+>` мало: у `<style>` и `<script>` содержимое лежит
    МЕЖДУ тегами, и после такой чистки ответ клиента приезжал владельцу
    начинённым таблицами стилей — на письмах из Outlook и Битрикса это пара
    тысяч символов CSS, за которыми настоящий текст не помещался в предпросмотр
    вовсе. Заодно возвращаем переводы строк: без них абзацы письма слипались
    в одну ленту.
    """
    from html import unescape

    text = _NOT_TEXT_RE.sub(" ", raw)
    text = _BREAKS_RE.sub("\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    # Пробелов после чистки остаётся много: схлопываем, но абзацы бережём.
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _decode(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def strip_quoted(text: str) -> str:
    """Убирает цитату нашего письма из ответа.

    Иначе в уведомление уезжает всё исходное письмо, и найти в нём ответ
    из двух строк невозможно.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^-{2,}\s*(Original Message|Пересылаемое сообщение)", stripped, re.I):
            break
        if re.match(r"^\d{1,2}\.\d{2}\.\d{4}.*(написал|wrote)", stripped, re.I):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def connect(settings=None):
    """Открывает ящик. Вызывающий обязан закрыть."""
    settings = settings or get_settings()
    if not settings.imap_ready:
        raise RuntimeError("почта для чтения не настроена")
    box = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30)
    box.login(settings.smtp_user or "", settings.smtp_password or "")
    return box


UID_VALIDITY_KEY = "inbox:uid_validity"


def _place_in_the_mailbox(store: Store, box) -> int:
    """С какого номера читать письма. Проверяет, тот ли это ящик.

    Номера писем (UID) уникальны не вообще, а внутри одного «поколения» ящика.
    Поколение помечено меткой UIDVALIDITY, и почтовый сервер меняет её, если
    ящик пересоздали, перенесли между серверами или восстановили из копии.
    После этого нумерация начинается заново — с единицы.

    Без проверки метки это тихая катастрофа: мы помним, что дочитали до 900-го
    письма, новые приходят под номерами 1, 2, 3, и запрос «дай всё после 900»
    не возвращает ничего. Никогда. Почта работает, письма приходят, ответы
    клиентов лежат в ящике — а система молчит, и снаружи это неотличимо от
    «нам никто не пишет».
    """
    stored = store.get_state(UID_VALIDITY_KEY)
    current = ""
    try:
        status, data = box.status("INBOX", "(UIDVALIDITY)")
        if status == "OK" and data:
            match = re.search(rb"UIDVALIDITY\s+(\d+)", data[0] or b"")
            if match:
                current = match.group(1).decode()
    except Exception as exc:  # noqa: BLE001 — не прочитали метку, но письма читать надо
        logger.debug("UIDVALIDITY не прочиталась ({}) — продолжаю с прежнего места", exc)

    if not current:
        # Метку не прочитали. Это не повод обнулять место: читаем как читали.
        logger.debug("IMAP не отдал UIDVALIDITY — продолжаю с прежнего места")
        return int(store.get_state(LAST_UID_KEY) or 0)

    if stored and stored != current:
        logger.warning(
            "Ящик пересоздан (UIDVALIDITY {} → {}) — начинаю читать сначала", stored, current
        )
        store.set_state(LAST_UID_KEY, "0")
        store.set_state(UID_VALIDITY_KEY, current)
        return 0

    if not stored:
        store.set_state(UID_VALIDITY_KEY, current)
    return int(store.get_state(LAST_UID_KEY) or 0)


# Названия месяцев IMAP ждёт английские и только такие. Через strftime было бы
# короче, но там они зависят от локали машины, а на другой локали сервер молча
# не поймёт запрос и отдаст пустой список — то есть ровно ту тишину, от которой
# всё это и защищает.
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _first_day(store: Store) -> str:
    """С какого дня читать, когда место в ящике потеряно. Формат IMAP."""
    from datetime import date, timedelta

    raw = store.get_state(LAST_READ_KEY) or today_msk()
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        day = date.fromisoformat(today_msk())
    # Сутки назад: часовой пояс сервера может отличаться от нашего, а SINCE
    # сравнивает даты, а не мгновения. Лишний день — это несколько писем,
    # потерянные сутки — это ответ клиента, которого никто не увидит.
    day -= timedelta(days=1)
    return f"{day.day:02d}-{_IMAP_MONTHS[day.month - 1]}-{day.year}"


def fetch(store: Store, *, limit: int = BATCH) -> list[Letter]:
    """Забирает новые письма. Флаги не трогает, место помнит по UID."""
    settings = get_settings()
    box = connect(settings)
    letters: list[Letter] = []
    try:
        box.select("INBOX", readonly=True)
        last_uid = _place_in_the_mailbox(store, box)
        criteria = f"UID {last_uid + 1}:*"
        if last_uid == 0:
            # Места в ящике нет: либо система заведена только что, либо ящик
            # пересоздали и нумерация началась заново. «Читать всё» тут значит
            # разобрать весь архив: владелец получает сотни уведомлений о
            # письмах, на которые ответили полгода назад, модель берёт деньги
            # за каждое, а разбор ещё и меняет стадии контактов задним числом —
            # закрытые оживают, а «интересно» из марта приходит как новость.
            # Поэтому берём только последние сутки: свежий ответ не потеряем,
            # архив не трогаем.
            criteria = f"{criteria} SINCE {_first_day(store)}"
        status, data = box.uid("search", None, criteria)
        if status != "OK":
            logger.warning("IMAP не отдал список писем: {}", status)
            return []
        store.set_state(LAST_READ_KEY, today_msk())

        uids = sorted(int(raw) for raw in (data[0] or b"").split() if int(raw) > last_uid)
        handled = last_uid
        for uid in uids[:limit]:
            status, payload = box.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                # Письмо не забралось — на этом останавливаемся и отметку
                # дальше не двигаем. Раньше она перешагивала через такое
                # письмо, и оно терялось навсегда: ящик мы больше не
                # перечитываем, а это мог быть ответ клиента или возврат.
                logger.warning("Письмо {} не забралось ({}) — вернусь за ним позже", uid, status)
                break
            # Двигаем отметку только по разобранным письмам. Иначе при пачке
            # больше лимита всё, что не поместилось, терялось навсегда —
            # вместе с ответами клиентов и возвратами.
            handled = uid
            message = email.message_from_bytes(payload[0][1])
            letters.append(Letter(
                uid=uid,
                sender=parseaddr(_text(message.get("From")))[1].lower(),
                subject=_text(message.get("Subject")),
                body=body_of(message),
                auto_submitted="auto" in (message.get("Auto-Submitted") or "").lower(),
            ))
        if handled > last_uid:
            store.set_state(LAST_UID_KEY, str(handled))
        if len(uids) > limit:
            logger.info("В ящике ещё {} писем — разберу в следующий заход", len(uids) - limit)
    finally:
        # Ящик мог отвалиться прямо сейчас; на выходе это уже не важно.
        with suppress(Exception):
            box.logout()
    return letters


def handle(store: Store, letter: Letter) -> str | None:
    """Что сделать с одним письмом. Возвращает текст владельцу или None."""
    kind = letter.kind

    if kind == OTHER:
        # Машинный отчёт, который нас ни к чему не обязывает: письмо дошло.
        # Тревожить владельца нечем, а трогать контакт тем более незачем.
        logger.info("Отчёт о доставке (не возврат): {}", letter.subject[:80])
        return None

    if kind == BOUNCE:
        address = letter.failed_address()
        contact = store.contact_by_email(address or "")
        if contact is None or contact.pk is None:
            logger.info("Возврат письма, но чей адрес — непонятно: {}", letter.subject[:80])
            return None
        if letter.temporary_bounce:
            # «Пока не доставили» — сервер ещё пытается. Закрыть контакт по
            # такому отчёту значит похоронить компанию из-за чужой очереди.
            store.log_event(contact.pk, "bounce_delay", letter.subject[:200])
            logger.info("«{}»: доставка задерживается, контакт не трогаю", contact.name)
            return None
        if contact.stage not in COLD_STAGES:
            # С человеком уже идёт разговор, а вернулось письмо на общий ящик.
            # Закрывать такой контакт нельзя — скажем владельцу, решит он.
            store.log_event(contact.pk, "bounce", letter.subject[:200])
            # Соседей по сети всё равно чистим. Ящик на сайте один на все
            # филиалы, и то, что по нему идёт разговор с одним из них, ничего
            # не говорит об остальных: им туда по-прежнему уходят холодные
            # письма — каждое за деньги и каждое обратно. Раньше эта ветка
            # выходила до вызова ниже, и мёртвый адрес оставался у всех
            # соседей. Какой филиал попадётся первым — дело случая: у
            # contact_by_email нет порядка, она отдаёт первую подходящую строку.
            also = store.close_same_email(
                address or "", skip_pk=contact.pk, note="письмо вернулось"
            )
            tail = f"\nЗакрыл ещё {also} с тем же адресом." if also else ""
            return (
                f"📭 <b>Письмо вернулось</b>\n{esc(contact.name)} · {esc(address or '')}\n"
                f"Контакт не закрываю: с ним уже идёт разговор.{tail}"
            )
        contact.meta[BOUNCED_KEY] = now_msk()
        store.set_meta(contact.pk, contact.meta)
        store.log_event(contact.pk, "bounce", letter.subject[:200])

        # Мёртвым стал АДРЕС, а не компания. Вычёркиваем адрес и закрываем
        # контакт только если достучаться больше нечем: у соседних контактов
        # это делает close_same_email, а сам адресат раньше закрывался
        # безусловно — вместе с живым вторым ящиком и телеграмом. Компанию
        # теряли из-за одного канала связи.
        left = [a for a in contact.emails if a.strip().lower() != (address or "").strip().lower()]
        if left != contact.emails:
            store.set_emails(contact.pk, left)
            contact.emails = left
        # Спрашиваем сам контакт, а не длину списка адресов. Второй ящик на
        # mail.ru или gmail для рассылки не годится (см. is_corporate_email), и
        # непустой список тут ничего не значит: `bool(left)` объявлял компанию
        # достижимой, владельцу сообщалось «остальные каналы живы — попробую
        # через них», а на деле писать было некуда, и контакт зависал навсегда.
        # Ровно этот же вопрос — contact.reachable — задаёт close_same_email
        # соседям по сети, и ответы у них должны совпадать.
        if contact.reachable:
            store.log_event(contact.pk, "channel", f"адрес мёртв: {address}")
            note = "Адрес вычеркнул, остальные каналы живы — попробую через них."
        else:
            store.set_stage(contact.pk, Stage.DEAD, f"письмо вернулось: {address}")
            note = "Больше туда не пишем: повторные письма на мёртвый адрес портят репутацию домена."

        # Тот же адрес мог достаться нескольким филиалам сети: почта на сайте
        # одна на всех. Закрываем всех, иначе завтра письмо уйдёт на тот же
        # мёртвый ящик от имени соседнего филиала.
        also = store.close_same_email(address or "", skip_pk=contact.pk, note="письмо вернулось")
        tail = f"\nЗакрыл ещё {also} с тем же адресом." if also else ""
        return (
            f"📭 <b>Письмо вернулось</b>\n"
            f"{esc(contact.name)} · {esc(address or '')}\n"
            f"{note}{tail}"
        )

    contact = store.contact_by_email(letter.sender)
    if contact is None:
        # Ответить может не тот, кому писали: письмо ушло на info@, отвечает
        # директор со своего адреса на том же домене. Раньше такой ответ
        # выбрасывался молча, и человек оставался без ответа.
        contact = store.contact_by_domain(letter.sender)
        if contact is not None:
            logger.info(
                "Ответ с другого адреса компании «{}»: {}", contact.name, letter.sender
            )
    if contact is None or contact.pk is None:
        logger.debug("Письмо не от нашего контакта: {}", letter.sender)
        return None

    if kind == REPLY and looks_like_stop(letter.body):
        # «Не пишите нам больше» — это тоже ответ, но разговаривать не о чем.
        # Закрываем только с холодных стадий — то же правило, что у возврата
        # и отказа ниже. Дальше по воронке за контакт отвечает владелец:
        # «стоп» в живой переписке о сделке может значить что угодно («стоп,
        # давайте пересчитаем»), и хоронить сделку по слову — не наше решение.
        if contact.stage in COLD_STAGES:
            store.set_stage(contact.pk, Stage.DEAD, "просил не писать")
            store.log_event(contact.pk, "stop", letter.subject[:200])
            return (
                f"🚫 <b>Просят не писать</b>\n{esc(contact.name)} · {esc(letter.sender)}\n"
                f"Закрыл контакт, больше туда ничего не уйдёт."
            )
        store.log_event(contact.pk, "stop", letter.subject[:200])
        return (
            f"🚫 <b>Похоже на «не пишите»</b>\n{esc(contact.name)} · {esc(letter.sender)}\n"
            f"С вами уже идёт разговор, поэтому сам не закрываю — решите вы.\n"
            f"\n<b>{esc(letter.subject)}</b>\n{esc(strip_quoted(letter.body)[:PREVIEW])}"
        )

    if kind == AUTO:
        # Автоответ — это не разговор. Ставим пометку и молчим.
        contact.meta[AUTOREPLY_KEY] = now_msk()
        store.set_meta(contact.pk, contact.meta)
        store.log_event(contact.pk, "autoreply", letter.subject[:200])
        logger.info("Автоответ от «{}» — за ответ не считаю", contact.name)
        return None

    whole = strip_quoted(letter.body)
    text = whole[:PREVIEW] or "(пустое письмо)"
    store.log_event(contact.pk, "reply", text[:500])

    # Модели отдаём письмо целиком, а не то, что поместилось на экран. PREVIEW —
    # это ширина уведомления, и вопрос «сколько влезет в телеграм» не имеет
    # отношения к вопросу «что человек ответил». На семистах знаках обрезалось
    # ровно то, ради чего разбор и затевался: вежливое вступление помещалось, а
    # «вернёмся к этому в сентябре» — уже нет, и «позже» читалось как согласие.
    # Свой предел у classify_reply есть (3000 знаков), второй раз не режем.
    verdict = _classify(store, contact, letter, whole)
    was = contact.stage
    _apply(store, contact, verdict)
    closed = was is not Stage.DEAD and store.get_contact(contact.pk).stage is Stage.DEAD

    where = "Партнёры" if contact.kind is Kind.PARTNER else "Клиенты"
    head, note = _headline(verdict, closed=closed)
    # Всё, что пришло из чужого письма, экранируем: угловая скобка в подписи
    # ломает разбор HTML, Telegram отвечает ошибкой и уведомление не доходит
    # вовсе — а второй попытки не будет, письмо уже отмечено прочитанным.
    lines = [
        f"{head}\n{esc(contact.name)} · {esc(letter.sender)}",
        f"Вкладка «{where}»",
    ]
    if note:
        lines.append(note)
    lines.append(f"\n<b>{esc(letter.subject)}</b>\n{esc(text)}")
    return "\n".join(lines)


# --- Что человек ответил ------------------------------------------------------
#
# Ответы приходят разные, и вести их одинаково нельзя: интерес требует ответа
# сегодня, отказ — закрыть контакт, «позже» — вернуться через месяц, а не через
# три дня следующим касанием цепочки. Тип определяет модель, но решение по
# контакту всё равно видно владельцу в уведомлении: подпись говорит, что
# именно мы сделали.

# Стадии, на которых разговора ещё не было. Только с них разбор письма имеет
# право закрыть контакт: дальше по воронке за него отвечает владелец, и
# «нам не нужно» в переписке о сделке значит совсем не то, что в ответ на
# холодное письмо.
COLD_STAGES = (Stage.NEW, Stage.QUEUED, Stage.WRITTEN)

# Пометка в карточке: когда вернуться к тем, кто попросил «позже».
REMIND_KEY = "remind_after"
# Чем модель это объяснила — чтобы через два месяца было понятно, откуда срок.
REMIND_NOTE = "remind_reason"

_HEADLINES = {
    llm.INTEREST: ("🔥 <b>Интересно!</b>", "Отвечает владелец — бот сюда не пишет."),
    llm.REFUSAL: ("🚫 <b>Отказ</b>", ""),
    llm.LATER: ("🕓 <b>Просят вернуться позже</b>", ""),
    llm.WRONG_PERSON: ("↪️ <b>Ответил не тот человек</b>", ""),
}


def _classify(store: Store, contact, letter: Letter, text: str):
    """Разбор ответа моделью. None — не вышло, дальше как раньше."""
    letters = store.sent_letters(contact.pk or 0)
    try:
        return llm.classify_reply(
            contact, letter.subject, text, our_letter=letters[-1] if letters else None
        )
    except Exception as exc:  # noqa: BLE001 — разбор не должен терять письмо
        logger.error("Разбор ответа от «{}» не удался: {}", contact.name, exc)
        return None


def _apply(store: Store, contact, verdict) -> None:
    """Двигает контакт по итогам разбора.

    Без разбора (модель молчит) остаётся прежнее поведение: ответил — значит
    «Начали общение», а дальше смотрит владелец.
    """
    pk = contact.pk
    if pk is None:
        return

    kind = verdict.kind if verdict else llm.UNCLEAR
    if kind == llm.REFUSAL:
        store.log_event(pk, "refusal", verdict.reason[:200])
        if contact.stage in COLD_STAGES:
            # Отказ закрывает контакт: цепочка касаний обрывается сразу, иначе
            # человеку, сказавшему «не нужно», через три дня уйдёт напоминание.
            store.set_stage(pk, Stage.DEAD, f"отказ по почте: {verdict.reason}"[:200])
        else:
            # Разговор уже идёт, назначен созвон или заказ в работе. Здесь
            # «нам не нужно» может относиться к чему угодно — к одной услуге,
            # к срокам, к соседнему письму. Закрывать сделку по разбору письма
            # нельзя, это решение владельца: он видит уведомление целиком.
            logger.info(
                "«{}»: похоже на отказ, но контакт на стадии «{}» — не закрываю",
                contact.name, contact.stage.value,
            )
        return

    if kind == llm.LATER:
        # Пустой срок поверх назначенного не пишем: второе письмо «сейчас
        # не до этого» стирало бы дату из первого («вернитесь в сентябре»),
        # и напоминание пропадало совсем.
        if verdict.when_date or not contact.meta.get(REMIND_KEY):
            contact.meta[REMIND_KEY] = verdict.when_date or ""
        contact.meta[REMIND_NOTE] = verdict.when or verdict.reason
        store.set_meta(pk, contact.meta)
        store.log_event(pk, "later", (verdict.when or verdict.reason)[:200])

    if contact.stage in (Stage.NEW, Stage.QUEUED, Stage.WRITTEN):
        # Стадия одна на все живые ответы: разговор начался. Куда он пойдёт
        # дальше — решает владелец, а не разбор письма.
        store.set_stage(pk, Stage.REPLIED, "ответили на письмо")


def _headline(verdict, closed: bool = False) -> tuple[str, str]:
    """Заголовок уведомления и строка о том, что мы с этим сделали.

    `closed` — закрыли ли контакт на самом деле. Подпись должна совпадать с
    делом: «Закрыл контакт» под контактом, который остался в работе, — это
    ровно то враньё интерфейса, из-за которого потом не сходятся цифры.
    """
    if verdict is None or verdict.kind == llm.UNCLEAR:
        return "💬 <b>Ответили на письмо</b>", ""

    head, note = _HEADLINES[verdict.kind]
    if verdict.kind == llm.REFUSAL:
        note = (
            "Закрыл контакт, писем больше не будет."
            if closed
            else "Контакт не закрываю: с ним уже идёт работа. Решать вам."
        )
    if verdict.kind == llm.LATER:
        when = verdict.when or "срок не назвали"
        note = f"Вернуться: {esc(when)}"
        if verdict.when_date:
            note += f" ({verdict.when_date})"
        note += ". Писем до этого срока не будет."
    elif verdict.kind == llm.WRONG_PERSON and verdict.hint:
        note = f"Отправил к: {esc(verdict.hint)}"
    return head, note


class MailboxUnavailable(RuntimeError):
    """Ящик не прочитался. Это НЕ «в ящике пусто».

    Разница дорогая: пока ящик молчит, ответы клиентов не разбираются вовсе —
    ни «интересно», ни «не пишите», ни возвраты. Снаружи это выглядит ровно
    как спокойный день, и раньше выглядело именно так: строка в логе и
    возврат None, неотличимый от пустого ящика.
    """


def check(store: Store, *, limit: int = BATCH) -> str | None:
    """Заход в ящик по расписанию. Возвращает одно сообщение владельцу.

    Поднимает MailboxUnavailable, если ящик не открылся: решать, тревожить ли
    владельца, должен вызывающий — он один знает, говорили ли об этом уже.
    """
    settings = get_settings()
    if not settings.imap_ready:
        return None

    try:
        letters = fetch(store, limit=limit)
    except Exception as exc:  # noqa: BLE001 — почта могла лечь, расписание живёт дальше
        logger.error("Почту прочитать не вышло: {}", exc)
        raise MailboxUnavailable(str(exc)) from exc

    # Каждое письмо разбираем под своим предохранителем. Отметка «дочитано»
    # уже сдвинута в fetch(), и упади разбор одного письма — все письма пачки
    # пропали бы молча: ящик по этим номерам больше не перечитывается. Одно
    # кривое письмо не должно топить остальные, а о себе — сказать вслух.
    messages = []
    for letter in letters:
        try:
            text = handle(store, letter)
        except Exception as exc:  # noqa: BLE001 — одно письмо не топит пачку
            logger.exception("Письмо {} не разобралось: {}", letter.uid, exc)
            text = (
                f"⚠️ Письмо от {letter.sender or 'неизвестного'} не разобралось "
                f"({type(exc).__name__}). Загляните в ящик руками — "
                f"тема: {letter.subject[:80] or 'без темы'}"
            )
        if text:
            messages.append(text)
    if not messages:
        return None
    return _one_message(messages)


# Сколько знаков оставляем под склейку уведомлений. Меньше лимита Telegram:
# запас на заголовок и на то, что notify дописывает свою метку обрезки.
BATCH_LIMIT = 3500


def _one_message(messages: list[str]) -> str:
    """Склеивает уведомления одного захода в одно сообщение.

    Два правила, и оба выстраданные.

    Первое: счётчик идёт СВЕРХУ. Раньше «…и ещё N» стояло в конце, а всё
    сообщение потом проходило через обрезку по длине — и хвост срезался
    первым. То есть пропадала ровно та строка, которая была единственным
    признаком, что писем было больше показанных.

    Второе: вмещаем столько, сколько влезает, а не первые пять. Письма уже
    отмечены прочитанными в ящике, второй раз их никто не покажет, поэтому
    выбрасывать их пачкой ради круглого числа нельзя.
    """
    separator = "\n\n———\n\n"
    shown: list[str] = []
    size = 0
    for text in messages:
        cost = len(text) + len(separator)
        if shown and size + cost > BATCH_LIMIT:
            break
        shown.append(text)
        size += cost

    body = separator.join(shown)
    if len(shown) == len(messages):
        return body
    left = len(messages) - len(shown)
    return (
        f"📬 <b>Писем за этот заход: {len(messages)}</b>, показываю {len(shown)}. "
        f"Ещё {left} — в ящике.\n\n{body}"
    )

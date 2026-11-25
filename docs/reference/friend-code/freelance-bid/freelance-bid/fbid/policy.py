"""Правила площадок: где можно давать ссылки и контакты, а где за это банят.

Зачем отдельный модуль, а не строчка в промте: на Кворке владелец уже получил
блокировку на месяц за переданный контакт (05.08.2026). Инструкция модели —
это пожелание, а вот вычистка ссылок и телефонов из готового текста перед
отправкой — гарантия. Одна ошибка здесь стоит месяца работы площадки.

Правило простое: если площадка запрещает контакты, из отклика вырезаются
ссылки, телефоны, почты и ники мессенджеров. Если после вычистки текст
разваливается — отклик не отправляется вообще.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from .models import Source


@dataclass(frozen=True)
class PlatformPolicy:
    """Что площадка разрешает в тексте отклика."""

    # Внешние ссылки, включая наш сайт.
    allow_links: bool = True
    # Телефоны, почты, ники в мессенджерах.
    allow_contacts: bool = True
    # Чем заменяем вычищенное, чтобы фраза не разваливалась.
    link_replacement: str = "покажу в переписке"


# FL.ru и Profi.ru ссылки на портфолио разрешают — там это норма делового отклика.
# Юду и Кворк за ссылку или контакт в переписке до сделки блокируют аккаунт.
POLICIES: dict[Source, PlatformPolicy] = {
    Source.FL: PlatformPolicy(),
    Source.PROFI: PlatformPolicy(),
    Source.YOUDO: PlatformPolicy(allow_links=False, allow_contacts=False),
}

# По умолчанию считаем площадку строгой: лучше отправить текст без ссылки,
# чем поймать бан на новой площадке, правила которой ещё не изучены.
STRICT_DEFAULT = PlatformPolicy(allow_links=False, allow_contacts=False)

_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>()\"']+|\b[a-z0-9-]+\.(?:ru|com|net|org|io|pro|me|dev)\b(?:/\S*)?",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
# +7 999 123-45-67, 8(999)1234567, 79991234567 и прочие человеческие написания.
# Голая семёрка без плюса — тоже номер: так его пишут в ссылках tel: и просто
# в тексте. Здесь это не мелочь: на строгих площадках номер в переписке стоит
# месяца блокировки, а этот вид записи вычистка пропускала.
_PHONE_RE = re.compile(r"(?:\+7|\b8|\b7)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b")
_HANDLE_RE = re.compile(r"(?<![\w@])@[a-z0-9_]{4,}", re.IGNORECASE)
_MESSENGER_RE = re.compile(
    r"\b(?:телеграм|телега|telegram|whatsapp|вотсап|ватсап|вайбер|viber|скайп|skype)\b",
    re.IGNORECASE,
)


def policy_for(source: Source) -> PlatformPolicy:
    return POLICIES.get(source, STRICT_DEFAULT)


# Домен без схемы: биржи не делают из него кликабельную ссылку, и клиент
# до сайта просто не доходит. Ловим «uniqore.ru», «uniqore.ru/demo/klinika/»,
# но не трогаем то, что уже написано целиком, и не портим почту.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w/@.])((?:[a-z0-9-]+\.)+(?:ru|com|net|org|io|pro|me|dev))(/\S*)?",
    re.IGNORECASE,
)


def ensure_clickable_links(text: str) -> str:
    """Дописывает https:// к ссылкам, написанным голым доменом.

    Это не косметика: на FL.ru «uniqore.ru» остаётся обычным текстом, и весь
    смысл ссылки в отклике пропадает. Промт просит модель писать адрес целиком,
    но промт — это пожелание, а здесь гарантия.
    """
    if not text:
        return text

    def fix(match: re.Match) -> str:
        domain, path = match.group(1), match.group(2) or ""
        return f"https://{domain}{path}"

    return _BARE_DOMAIN_RE.sub(fix, text)


def sanitize(text: str, source: Source) -> str:
    """Убирает из текста то, что запрещено правилами площадки.

    Возвращает готовый к отправке текст. Ничего не запрещено — текст не меняется.
    """
    rules = policy_for(source)
    cleaned = text or ""

    if not rules.allow_contacts:
        # Порядок важен: почту вырезаем ДО ссылок, иначе доменная часть адреса
        # («hi@uniqore.ru») подменяется как ссылка и остаётся огрызок «hi@».
        cleaned = _EMAIL_RE.sub("", cleaned)
        cleaned = _PHONE_RE.sub("", cleaned)
        cleaned = _HANDLE_RE.sub("", cleaned)
        # Название мессенджера без контакта безобидно, но именно на нём площадки
        # ловят «уход в личку», поэтому убираем и его.
        cleaned = _MESSENGER_RE.sub("", cleaned)
    if not rules.allow_links:
        cleaned = _URL_RE.sub(rules.link_replacement, cleaned)

    cleaned = _tidy(cleaned)
    if cleaned != _tidy(text or ""):
        logger.warning(
            "[{}] в заходе было запрещённое площадкой — текст пришлось чистить", source.value
        )

    # Ссылки дописываем последними и только там, где они вообще разрешены:
    # на строгой площадке дописывать нечего, там ссылок быть не должно.
    if rules.allow_links:
        cleaned = ensure_clickable_links(cleaned)
    return cleaned


def is_usable(original: str, cleaned: str, source: Source) -> bool:
    """Можно ли отправлять то, что осталось после вычистки.

    На строгих площадках (Кворк, Юду) правило жёсткое: если чистить вообще
    пришлось, отправлять нельзя. Регулярка вырезает подстроку и оставляет
    рваную фразу («напишите в или на,»), а платформа за такое сообщение уже
    один раз забанила аккаунт на месяц. Модель предупреждена не писать туда
    ссылок — значит, отличие означает, что текст написан неправильно.

    На FL и Profi ссылки разрешены, чистка не идёт, и проверять нечего.
    Длину текста здесь НЕ проверяем — это забота bidder.MIN_REPLY_CHARS,
    и путать эти две причины отказа в одном сообщении нельзя.
    """
    cleaned = (cleaned or "").strip()
    if not cleaned:
        return False

    rules = policy_for(source)
    if rules.allow_links and rules.allow_contacts:
        return True
    return cleaned == _tidy(original or "")


def _tidy(text: str) -> str:
    """Приводит текст в порядок после вырезаний: лишние пробелы и знаки."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,;:])\s*([,.;:])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()

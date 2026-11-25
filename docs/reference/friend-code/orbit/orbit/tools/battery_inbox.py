"""Батарея разбора входящих: 40 писем разного рода через настоящий код.

Модель не зовём — шлюз сейчас не тянет наши промты, да и проверяем мы здесь
не её, а всё вокруг: определение вида письма, вытаскивание адреса из возврата,
отрезание цитаты, экранирование чужого текста.

Запуск:  .venv/bin/python tools/battery_inbox.py
"""
from __future__ import annotations

import email
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orbit import inbox

problems: list[str] = []


def flag(text: str) -> None:
    problems.append(text)
    print("  ⚠️ ", text)


def letter(raw: bytes) -> inbox.Letter:
    message = email.message_from_bytes(raw)
    from email.utils import parseaddr

    return inbox.Letter(
        uid=1,
        sender=parseaddr(inbox._text(message.get("From")))[1].lower(),
        subject=inbox._text(message.get("Subject")),
        body=inbox.body_of(message),
        auto_submitted="auto" in (message.get("Auto-Submitted") or "").lower(),
    )


def plain(sender: str, subject: str, body: str, extra: str = "") -> bytes:
    return (
        f"From: {sender}\r\nSubject: {subject}\r\n{extra}"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}"
    ).encode()


# Сорок писем: как они приходят в жизни.
CASES: list[tuple[str, bytes, str]] = [
    ("живой интерес", plain("boss@agency.ru", "Re: субподряд",
                            "Здравствуйте! Интересно, расскажите подробнее."), "reply"),
    ("отказ", plain("boss@agency.ru", "Re: субподряд", "Спасибо, нам это не нужно."), "reply"),
    ("стоп", plain("boss@agency.ru", "Re: субподряд", "Не пишите нам больше."), "reply"),
    ("стоп капсом", plain("boss@agency.ru", "Re:", "СТОП. Отписка."), "reply"),
    ("вернитесь позже", plain("boss@agency.ru", "Re:", "Вернитесь в сентябре."), "reply"),
    ("пустое письмо", plain("boss@agency.ru", "Re:", ""), "reply"),
    ("только цитата", plain("boss@agency.ru", "Re:", "> Здравствуйте, мы UniqOre"), "reply"),
    ("html-письмо", (
        "From: boss@agency.ru\r\nSubject: Re\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><p>Интересно!</p></body></html>"
    ).encode(), "reply"),
    ("угловые скобки в тексте", plain("boss@agency.ru", "Re:",
                                      "Пишите на <boss@agency.ru> & звоните"), "reply"),
    ("автоответ по заголовку", plain("boss@agency.ru", "Отпуск", "Меня нет до 1 сентября.",
                                     "Auto-Submitted: auto-replied\r\n"), "auto"),
    ("возврат: ящик не найден", plain(
        "MAILER-DAEMON@beget.com", "Undelivered Mail Returned to Sender",
        "This is the mail system at host smtp.beget.com.\n"
        "<hello@deadagency.ru>: host mx.dead.ru said: 550 5.1.1 User unknown"), "bounce"),
    ("возврат: временный", plain(
        "MAILER-DAEMON@beget.com", "Warning: message delayed",
        "THIS IS A WARNING ONLY. YOU DO NOT NEED TO RESEND YOUR MESSAGE.\n"
        "<slow@agency.ru>: connect to mx.agency.ru: Connection timed out"), "bounce"),
    ("возврат: ящик переполнен", plain(
        "postmaster@mail.ru", "Delivery Status Notification (Failure)",
        "<full@agency.ru>: 552 5.2.2 Mailbox full"), "bounce"),
    ("письмо от чужого", plain("stranger@nowhere.io", "Реклама", "Купите наш курс!"), "reply"),
    ("длинное письмо", plain("boss@agency.ru", "Re:", "Очень интересно. " * 400), "reply"),
]

# Добираем до сорока однотипными, но разными по содержанию — так проверяется
# устойчивость к вариациям, а не к одному тексту.
VARIANTS = [
    "Добрый день, а сколько стоит?", "Пришлите примеры работ.",
    "Мы уже работаем с другими.", "Позвоните мне: +7 999 000-00-00",
    "Интересно, но бюджета сейчас нет.", "Давайте созвонимся завтра.",
    "А вы делаете интеграции с 1С?", "Спасибо, подумаем.",
    "Отправьте КП на почту.", "Что по срокам?",
    "Нам нужен только дизайн.", "Пришлите договор.",
    "Кто у вас главный?", "Мы в другом городе, это важно?",
    "Сколько человек в команде?", "У нас свой разработчик.",
    "Можно пример похожего проекта?", "Что входит в поддержку?",
    "А если не понравится?", "Работаете по предоплате?",
    "Напишите в телеграм.", "Мы сейчас на паузе.",
    "Отправил ТЗ, посмотрите.", "Какая гарантия?",
    "Нужно срочно, за неделю.",
]
for index, text in enumerate(VARIANTS):
    CASES.append((f"живой ответ {index + 1}", plain("boss@agency.ru", "Re: субподряд", text), "reply"))


def main() -> int:
    print("═" * 74)
    print(f" РАЗБОР ВХОДЯЩИХ · {len(CASES)} писем")
    print("═" * 74)

    counts: dict[str, int] = {}
    for name, raw, expected in CASES:
        item = letter(raw)
        kind = item.kind
        counts[kind] = counts.get(kind, 0) + 1
        mark = "  " if kind == expected else "≠ "
        preview = " ".join(inbox.strip_quoted(item.body).split())[:38]
        print(f"  {mark}{name[:26]:28} → {kind:7} │ {preview}")

        if kind != expected:
            flag(f"{name}: разобрано как «{kind}», ожидали «{expected}»")
        if kind == inbox.BOUNCE:
            address = item.failed_address()
            if not address:
                flag(f"{name}: адрес из возврата не вытащился")
            elif "@" not in address:
                flag(f"{name}: из возврата вышел не адрес: {address}")
        if inbox.looks_like_stop(item.body) and "стоп" not in name and "Не пишите" not in name:
            flag(f"{name}: принято за «стоп», хотя это обычный ответ")

    print(f"\n  по видам: {counts}")

    print("\n" + "─" * 74)
    print(" ЭКРАНИРОВАНИЕ ЧУЖОГО ТЕКСТА")
    print("─" * 74)
    from orbit.ui import esc

    for danger in ("<b>жирный</b>", "A & B", "<script>alert(1)</script>", "5 > 3 < 7"):
        escaped = esc(danger)
        ok = "<" not in escaped.replace("&lt;", "") and "&amp;" in escaped or "<" not in escaped
        print(f"  {'  ' if ok else '⚠️ '}{danger[:32]:34} → {escaped[:34]}")
        if "<script>" in escaped:
            flag(f"скрипт не экранирован: {danger}")

    print("\n" + "═" * 74)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for item in dict.fromkeys(problems):
            print("  ·", item)
    else:
        print(" ПРОБЛЕМ НЕ НАЙДЕНО")
    print("═" * 74)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

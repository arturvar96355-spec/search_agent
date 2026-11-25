"""Ночной прогон: все задачи расписания, как они выполнятся без человека.

Ночью и ранним утром система работает сама: в 4:30 бэкап, в 11:00 письма
вдогонку, в 21:00 сводка за день, каждые десять минут разбор почты, каждые
полчаса — проверка модели. Никто на это не смотрит. Задача, упавшая в 4:30,
или сводка с незакрытым тегом (Telegram отвергает такое сообщение целиком)
снаружи выглядят одинаково — как тихая ночь.

Тесты проверяют каждую задачу по отдельности и на выдуманных данных. Здесь
задачи выполняются ПО-НАСТОЯЩЕМУ и на копии боевой базы: с теми контактами,
именами и остатками, которые накопились за месяцы. Проверяется то, что видит
владелец: дошёл ли текст, закрыта ли разметка, влезает ли в лимит Telegram.

Наружу не уходит ничего, и денег не тратится:
  · база — копия, живая не открывается вовсе (иначе задачи её изменят);
  · Telegram подменён записью в список;
  · модель подменена: письма вдогонку иначе стоили бы денег за каждый контакт;
  · почтовый ящик подменён — и «письма есть», и «ящик молчит», обе ветки;
  · бэкап пишет в песочницу, а не в боевой каталог копий.

Запуск:  .venv/bin/python -m tools.battery_night
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT))

SANDBOX = Path(tempfile.mkdtemp(prefix="battery-night-"))

problems: list[str] = []
sent: list[str] = []


def flag(text: str) -> None:
    problems.append(text)
    print("  ⚠️ ", text)


def ok(text: str) -> None:
    print(f"     {text}")


def head(title: str) -> None:
    print("\n" + "─" * 74)
    print(" " + title)
    print("─" * 74)


def sandbox_db(source: Path) -> Path:
    """Копия базы. Отказ вместо тихого продолжения по живой."""
    target = SANDBOX / source.name
    if not source.exists():
        raise SystemExit(f"ОТКАЗ: базы {source} нет — проверять нечего")
    shutil.copy2(source, target)
    for tail in ("-wal", "-shm"):
        extra = source.with_name(source.name + tail)
        if extra.exists():
            shutil.copy2(extra, target.with_name(target.name + tail))
    if target.resolve() == source.resolve():
        raise SystemExit(f"ОТКАЗ: копия базы {source} не сделалась — по живой не работаю")
    return target


# --- Проверка текста, который увидит владелец --------------------------------

def _unbalanced(text: str) -> str | None:
    """Разбор общий — см. orbit/ui.py: незакрытые и неизвестные Telegram теги."""
    from orbit.ui import bad_html

    return bad_html(text)


def check_text(where: str, text: str | None) -> None:
    if text is None:
        return
    if not text.strip():
        flag(f"{where}: пустое сообщение владельцу")
        return
    if len(text) > 4096:
        flag(f"{where}: текст длиннее лимита Telegram ({len(text)})")
    broken = _unbalanced(text)
    if broken:
        flag(f"{where}: html-разметка — {broken} (Telegram отвергнет сообщение целиком)")
    if "None" in re.sub(r"<[^>]+>", "", text):
        flag(f"{where}: в тексте пролезло None")
    if "Traceback" in text:
        flag(f"{where}: в сообщение попал стек ошибки")


# --- Подмена внешнего мира ---------------------------------------------------

def _mute_the_world(monkey) -> None:
    """Телеграм, модель, почта и каталог копий — в песочницу."""
    from orbit import backup, followup, inbox, llm, notify

    def tell(text: str, **_kwargs) -> bool:
        sent.append(text)
        return True

    # Достаточно одной подмены: задачи берут tell локальным импортом внутри
    # функции, а он ищет имя в модуле notify уже в момент вызова.
    monkey(notify, "tell", tell)
    # Каталог копий. Именно backup_dir(), а не мифическое поле: подмена
    # несуществующего имени тихо не срабатывает, и первый прогон этой батареи
    # положил настоящую копию в настоящий каталог — ровно то, чего обещал не
    # делать. Ниже monkey() на такое теперь отказывается работать.
    monkey(backup, "backup_dir", lambda: SANDBOX / "backups")

    # Модель. Письма вдогонку иначе стоили бы денег за каждый контакт, а
    # проверка «жива ли модель» — за каждый заход.
    monkey(llm, "ping", lambda: (True, ""))
    monkey(followup, "write_one", lambda store, contact: (True, "касание 2 из 4"))

    # Почта. Настоящий заход в ящик здесь недопустим дважды: он лезет в сеть и
    # отдаёт письма на разбор МОДЕЛИ — то есть за деньги, по письму за штуку.
    # Первый прогон этой батареи ходил в ящик по-настоящему: обещание в
    # заголовке было, подмены не было.
    monkey(inbox, "check", lambda store: "📬 <b>Ответ</b>\nФирма · a@firma.ru\nВкладка «Клиенты»")


def _run_job(name: str, store) -> None:
    """Выполняет одну задачу расписания по-настоящему и проверяет её ответ."""
    from orbit import schedule

    job = next((item for item in schedule.jobs() if item.key == name), None)
    if job is None:
        flag(f"задачи «{name}» в расписании нет — батарея отстала от кода")
        return

    before = len(sent)
    # Время берём ЕГО СОБСТВЕННОЕ. Бэкап назначен на 4:30, сводка на 21:00, и
    # в момент запуска батареи они просто не наступили: tick их пропускал, а
    # батарея печатала «сообщений владельцу 0» — то есть «проверено, всё
    # хорошо» про задачу, которая не выполнялась вовсе. «Не смогли проверить»
    # и «проверили, и всё хорошо» — разные вещи, и путать их нельзя нигде, а
    # в проверяющем средстве особенно.
    moment = datetime.now(schedule.MSK)
    if job.at:
        moment = moment.replace(hour=job.at[0], minute=job.at[1], second=0, microsecond=0)
    if not schedule.due(job, store, moment):
        flag(f"задача «{job.title}» не запустилась — проверить её не вышло")
        return
    # Через tick(), а не job.run(): именно tick ловит падение, ставит отметки
    # и решает, что уйдёт владельцу. Задача, падающая молча, — как раз то,
    # ради чего эта батарея и написана.
    messages = schedule.tick(store, now=moment, only=[job])
    for text in messages:
        check_text(f"задача «{job.title}»", text)
        if "не выполнилась" in text:
            flag(f"задача «{job.title}» упала: {text.splitlines()[0][:120]}")
    for text in sent[before:]:
        check_text(f"задача «{job.title}» (сама отправила)", text)
    total = len(messages) + len(sent) - before
    ok(f"{job.title}: выполнилась, сообщений владельцу {total}")


def check_schedule() -> None:
    from orbit import bot, schedule
    from orbit.store import Store

    head("РАСПИСАНИЕ · каждая задача выполняется по-настоящему")
    store = Store(sandbox_db(bot.get_settings().db_path))
    try:
        # Отметки «уже сделано за сегодня» снимаем: иначе задача просто не
        # запустится, а батарея отрапортует «проблем нет», ничего не проверив.
        for job in schedule.jobs():
            store.delete_state(schedule._state_key(job))
            store.delete_state(schedule._fail_key(job))
            store.delete_state(schedule._told_key(job))
        for job in schedule.jobs():
            _run_job(job.key, store)
    finally:
        store.close()


def check_inbox_outage() -> None:
    """Молчащий ящик: тревога должна уйти один раз, а потом — «снова читается»."""
    from orbit import bot, inbox, schedule
    from orbit.store import Store

    head("ЯЩИК МОЛЧИТ · тревога один раз, потом выздоровление")
    store = Store(sandbox_db(bot.get_settings().db_path))
    try:
        store.delete_state(schedule.INBOX_STATE)
        store.delete_state(schedule.INBOX_TOLD)

        def broken(_store):
            raise inbox.MailboxUnavailable("сервер не отвечает")

        real = inbox.check
        inbox.check = broken
        try:
            before = len(sent)
            schedule._job_inbox(store)
            first = len(sent) - before
            schedule._job_inbox(store)
            second = len(sent) - before - first
        finally:
            inbox.check = real

        ok(f"тревог за два захода: {first} и {second}")
        if first != 1:
            flag(f"о молчащем ящике сказали {first} раз вместо одного")
        if second:
            flag("о той же беде сказали второй раз — тревога перестанет читаться")

        inbox.check = lambda _store: None
        try:
            before = len(sent)
            schedule._job_inbox(store)
            healed = len(sent) - before
        finally:
            inbox.check = real
        if healed != 1:
            flag(f"о починке ящика сказали {healed} раз вместо одного")
        else:
            ok("о починке сказали ровно один раз")
        if store.get_state(schedule.INBOX_STATE):
            flag("ящик читается, а отметка «не читается» осталась — экраны соврут")
    finally:
        store.close()


def main() -> int:
    print("═" * 74)
    print(" НОЧНОЙ ПРОГОН · то, что система делает без человека")
    print("═" * 74)
    print(f" песочница: {SANDBOX}")

    patched: list[tuple[object, str, object]] = []

    def monkey(target, name, value):
        # Подменять можно только то, что есть. Опечатка в имени иначе создаёт
        # новое поле, подмена не срабатывает, а батарея продолжает утверждать,
        # что внешний мир отключён: настоящий каталог копий так и узнал о себе.
        if not hasattr(target, name):
            raise SystemExit(
                f"ОТКАЗ: у {getattr(target, '__name__', target)} нет «{name}» — "
                f"подмена не сработает, а батарея решит, что сработала"
            )
        patched.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    _mute_the_world(monkey)
    try:
        check_schedule()
        check_inbox_outage()
    finally:
        for target, name, value in patched:
            setattr(target, name, value)

    print("\n" + "═" * 74)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        print("═" * 74)
        for text in problems:
            print(f"  · {text}")
        return 1
    print(" ПРОБЛЕМ НЕ НАЙДЕНО")
    print("═" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

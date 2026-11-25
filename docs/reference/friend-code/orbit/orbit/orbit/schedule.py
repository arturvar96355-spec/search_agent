"""Фоновые дела по часам: бэкап, сводка, проверка почты, напоминания.

Отдельного демона нет намеренно — лишний процесс это лишний способ забыть
его запустить. Расписание живёт потоком внутри бота: бот и так работает
круглосуточно, а если он не работает, то и сводку слать некому.

Отметка о выполнении лежит в базе, а не в памяти. Перезапуск бота посреди
дня не должен привести ко второму бэкапу и второй сводке.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from .store import Store
from .ui import esc

MSK = timezone(timedelta(hours=3))
TICK_SECONDS = 60
# Пауза после падения задачи: не даём сломанной задаче крутиться в цикле.
FAIL_PAUSE = 300
# Через сколько минут пробовать снова, если задача упала.
RETRY_MINUTES = 15
# На сколько задача может опоздать и всё-таки выполниться (минуты).
# Четыре часа: бот перезапустили — задача догонит, бот лежал сутки — нет.
DEFAULT_WINDOW = 240
# Сколько недоставленных сообщений держим на повтор. Это строки в памяти,
# места они не стоят, а каждая — событие, о котором владелец иначе не узнает.
UNDELIVERED_KEEP = 30


@dataclass
class Job:
    """Одно фоновое дело.

    at — ежедневно в это время по Москве, every — раз в столько минут.
    Ровно одно из двух.
    """

    key: str
    title: str
    run: Callable[[Store], str | None]
    at: tuple[int, int] | None = None
    every: int | None = None
    # На сколько минут допустимо опоздать. Бот мог быть выключен, и тогда
    # задачу лучше пропустить до завтра, чем выполнить не вовремя.
    window: int = DEFAULT_WINDOW

    def __post_init__(self) -> None:
        if bool(self.at) == bool(self.every):
            raise ValueError(f"Задача {self.key}: нужно либо at, либо every")


def _state_key(job: Job) -> str:
    return f"job:{job.key}"


def _fail_key(job: Job) -> str:
    return f"job:{job.key}:failed"


def _told_key(job: Job) -> str:
    """Ключ «об этой поломке владельцу уже сказали»."""
    return f"job:{job.key}:told"


def mark_failed(job: Job, store: Store, now: datetime) -> bool:
    """Записывает неудачу. True — сегодня владельцу об этом ещё не говорили.

    Про одну и ту же поломку говорим ОДИН РАЗ В СУТКИ. Бэкап в 4:30
    повторяется каждые пятнадцать минут до конца окна в четыре часа: это
    семнадцать попыток и семнадцать одинаковых тревог подряд. Одинаковая
    тревога по кругу перестаёт читаться на третий раз.

    Но и «один раз навсегда» не годится: отметка снималась только успехом, и
    задача, падающая каждый день, сообщала о себе однажды — а дальше молчала
    месяцами. Сутки — верная мера: за день ничего не меняется, а назавтра это
    уже новость.
    """
    store.set_state(_fail_key(job), now.isoformat(timespec="seconds"))
    told = store.get_state(_told_key(job)) or ""
    today = now.date().isoformat()
    if told[:10] == today:
        return False
    store.set_state(_told_key(job), now.isoformat(timespec="seconds"))
    return True


def due(job: Job, store: Store, now: datetime) -> bool:
    """Пора ли выполнять."""
    # Задача только что упала — даём ей передышку. Без неё сломанный бэкап
    # пробовал бы себя каждую минуту и засыпал владельца одинаковыми
    # сообщениями об ошибке.
    failed = store.get_state(_fail_key(job))
    if failed:
        try:
            when = datetime.fromisoformat(failed)
        except ValueError:
            when = None
        if when and (now - when).total_seconds() < RETRY_MINUTES * 60:
            return False

    last = store.get_state(_state_key(job))

    if job.every:
        if not last:
            return True
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            return True
        return (now - when).total_seconds() >= job.every * 60

    hour, minute = job.at or (0, 0)
    planned = hour * 60 + minute
    current = now.hour * 60 + now.minute
    if current < planned:
        return False
    # Сегодня уже делали — второй раз не надо.
    if (last or "")[:10] == now.date().isoformat():
        return False
    # Опоздание больше окна — задачу пропускаем до завтра. Бот, поднятый в
    # полночь, раньше догонял всё разом: письма вдогонку уходили людям ночью,
    # а бэкап «на время, когда никто не работает» вставал посреди дня.
    return current - planned <= job.window


def mark_done(job: Job, store: Store, now: datetime) -> bool:
    """Отмечает выполнение. True — задача до этого падала, и это новость."""
    store.set_state(_state_key(job), now.isoformat(timespec="seconds"))
    store.delete_state(_fail_key(job))
    recovered = bool(store.get_state(_told_key(job)))
    store.delete_state(_told_key(job))
    return recovered


def jobs() -> list[Job]:
    """Что и когда делаем.

    Время развели по часам не случайно: бэкап ночью, когда никто не работает,
    сводка вечером, когда день уже закончился, письма вдогонку — утром, чтобы
    не приходить к человеку ночью.
    """
    return [
        Job("backup", "бэкап баз", _job_backup, at=(4, 30)),
        Job("followup", "письма вдогонку", _job_followup, at=(11, 0)),
        Job("inbox", "проверка почты", _job_inbox, every=10),
        Job("model", "жива ли модель", _job_model, every=30),
        Job("digest", "сводка за день", _job_digest, at=(23, 0)),
    ]


class BackupFailed(RuntimeError):
    """Бэкап отработал, но что-то не скопировалось."""


def _job_backup(store: Store) -> str | None:
    from . import alerts, backup

    report = backup.run()
    if not report.ok:
        # Отчёт с ошибкой — это НЕ выполненная задача. Возврат текста наверх
        # означал бы «сделано», и расписание поставило бы отметку: следующая
        # попытка была бы только через сутки, а копии за сегодня так и не
        # появилось бы. Бросаем — тогда сработает общая ветка отказа: отметка
        # не ставится, через четверть часа пробуем снова, владельцу уходит
        # сообщение с причиной.
        raise BackupFailed(backup.summary(report))
    # Удачный бэкап — не новость, а отчёт о сделанном, и приходил он сообщением
    # в половине пятого утра каждую ночь. Ночью с ним всё равно нечего делать:
    # строка ждёт вечерней сводки. Неудачный по-прежнему летит сразу — он
    # выше, через BackupFailed, и это уже просьба вмешаться.
    alerts.deliver(store, alerts.BACKUP_DONE, backup.summary(report))
    return None


def _job_followup(store: Store) -> str | None:
    from . import alerts, followup

    text = followup.run(store)
    if text:
        # Готовые письма лежат в очереди и никуда не денутся до кнопки.
        # Дёргать ради них в одиннадцать утра незачем — скажем вечером.
        alerts.deliver(store, alerts.FOLLOWUP_READY, text)
    return None


# Правда о ящике и «владельцу уже сказали» — раздельно, по тем же причинам,
# что у модели: не принятое телеграмом сообщение не должно стирать сам факт.
INBOX_STATE = "inbox:down"
INBOX_TOLD = "inbox:down_told"


def _job_inbox(store: Store) -> str | None:
    """Разбор входящих. Молчащий ящик — новость, а не тихий день.

    Пока ящик не читается, ответы клиентов не разбираются вовсе: ни «нам
    интересно», ни «не пишите», ни возвраты. Снаружи это выглядит ровно как
    спокойный день, и выглядело именно так — строка в логе, которую не читают.
    """
    from . import inbox
    from .notify import tell

    told = store.get_state(INBOX_TOLD)
    try:
        text = inbox.check(store)
    except inbox.MailboxUnavailable as exc:
        store.set_state(INBOX_STATE, str(exc)[:200])
        if told == str(exc)[:200]:
            return None  # та же беда, что и десять минут назад — молчим
        if tell(
            f"🛑 <b>Почтовый ящик не читается: {esc(str(exc))}</b>\n"
            f"Пока это так, ответы клиентов и возвраты не разбираются — "
            f"выглядит это как тишина в ящике.\n"
            f"Скажу, когда заработает."
        ):
            store.set_state(INBOX_TOLD, str(exc)[:200])
        else:
            logger.warning("Тревога о ящике не доставлена — повторю в следующий заход")
        return None

    store.delete_state(INBOX_STATE)
    if told and tell("✅ <b>Почтовый ящик снова читается.</b> Разбор входящих пошёл дальше."):
        store.delete_state(INBOX_TOLD)
    return text


def _job_digest(store: Store) -> str | None:
    from . import digest

    return digest.build(store)


# Что с моделью прямо сейчас. Пустое значение — всё в порядке. Это ПРАВДА о
# модели, и её читают экраны бота и вечерняя сводка, поэтому пишется она
# всегда, независимо от того, дошло ли сообщение владельцу.
MODEL_STATE = "model:down"
# А это — «про эту беду владельцу уже сказали». Разведено с MODEL_STATE
# намеренно: одна отметка на двоих значила, что не принятое телеграмом
# сообщение стирало и сам факт поломки — экран бота показывал «модель
# отвечает», пока не писалось вообще ничего.
MODEL_TOLD = "model:down_told"


def _job_model(store: Store) -> str | None:
    """Проверяет, отвечает ли модель. Говорит только когда что-то поменялось.

    Без модели встаёт всё сразу: письма не пишутся, ответы не разбираются, на
    биржах не уходят отклики. Снаружи это выглядит как затишье, а не как
    поломка, — потому и проверяем отдельно.

    Отправляет сам, а не возвращает текст наверх, и держит ДВЕ отметки.
    MODEL_STATE — правда о модели, её пишем всегда: её читают экраны бота и
    вечерняя сводка, и молчащая модель обязана быть видна там сразу.
    MODEL_TOLD — «владельцу уже сказали», и вот она ставится только после
    доставки. Одна отметка на двоих давала вторую тишину поверх первой:
    телеграм не ответил, сообщение потерялось, а экран бота при этом бодро
    показывал «модель отвечает».
    """
    from . import llm
    from .notify import tell

    alive, why = llm.ping()
    told = store.get_state(MODEL_TOLD)

    if alive:
        # Состояние снимаем сразу: модель отвечает, и экраны обязаны говорить
        # это немедленно. Радостное сообщение — отдельно, и только тем, кому
        # мы до этого жаловались.
        store.delete_state(MODEL_STATE)
        if not told:
            return None
        if tell("✅ <b>Модель снова отвечает.</b> Письма и отклики пошли дальше."):
            store.delete_state(MODEL_TOLD)
        return None

    store.set_state(MODEL_STATE, why)
    if told == why:
        return None  # та же беда, что и полчаса назад — молчим
    delivered = tell(
        f"🛑 <b>Модель не отвечает: {esc(why)}</b>\n"
        f"Пока это так, не пишутся письма партнёрам и клиентам, не разбираются "
        f"ответы по почте и не уходят отклики на биржах — ключ на всё один.\n"
        f"Скажу, когда заработает."
    )
    if delivered:
        store.set_state(MODEL_TOLD, why)
    else:
        logger.warning("Тревога о модели не доставлена — повторю в следующий заход")
    return None


def tick(store: Store, *, now: datetime | None = None, only: list[Job] | None = None) -> list[str]:
    """Один проход по расписанию. Возвращает тексты, которые надо отправить."""
    moment = now or datetime.now(MSK)
    messages = []
    for job in only or jobs():
        if not due(job, store, moment):
            continue
        logger.info("Расписание: {}", job.title)
        try:
            text = job.run(store)
        except Exception as exc:  # noqa: BLE001 — одна задача не роняет остальные
            logger.exception("Задача «{}» упала", job.title)
            # Отметку не ставим: задача не выполнена, и в оставшееся окно
            # стоит попробовать ещё раз. Раньше упавший в 4:30 бэкап
            # считался сделанным и не повторялся до следующих суток.
            if mark_failed(job, store, moment):
                messages.append(
                    f"⚠️ Задача «{esc(job.title)}» не выполнилась: "
                    f"{esc(type(exc).__name__)}: {esc(str(exc))}\n"
                    f"Пробую снова каждые {RETRY_MINUTES} минут — "
                    f"повторять эту строку не буду, скажу, когда получится."
                )
            continue
        if mark_done(job, store, moment):
            messages.append(f"✅ Задача «{esc(job.title)}» снова работает.")
        if text:
            messages.append(text)
    return messages


def loop(db_path: str | Path) -> None:
    """Крутится рядом с ботом и выполняет расписание."""
    from .notify import tell

    store = Store(db_path)
    logger.info("Расписание работает: {} задач", len(jobs()))
    # Сообщения, которые телеграм не принял. Держим до следующего круга:
    # сводка за день или отчёт о бэкапе бывают раз в сутки, и потерять их
    # из-за одной сетевой заминки значит просто не узнать, что было.
    undelivered: list[str] = []
    try:
        while True:
            try:
                pending = undelivered + tick(store)
                undelivered = []
                for text in pending:
                    if not tell(text):
                        undelivered.append(text)
                if undelivered:
                    logger.warning(
                        "Не доставлено сообщений: {} — повторю через минуту", len(undelivered)
                    )
                # Копить бесконечно нельзя: если телеграм молчит час, пачка
                # из шестидесяти одинаковых уведомлений хуже их отсутствия.
                #
                # Режем ХВОСТ, а не начало. Было наоборот (`[-KEEP:]`), и
                # выбрасывались как раз те сообщения, ради которых очередь
                # заведена: сводка за день и отчёт о бэкапе бывают раз в
                # сутки и встают в очередь первыми, а проверка почты бежит
                # каждые десять минут и вытесняла их своими повторами.
                if len(undelivered) > UNDELIVERED_KEEP:
                    dropped = undelivered[UNDELIVERED_KEEP:]
                    undelivered = undelivered[:UNDELIVERED_KEEP]
                    # Молча не выбрасываем: в логе должно остаться, что именно
                    # владелец не увидит.
                    logger.error(
                        "Очередь недоставленных переполнена — выбросил {}: {}",
                        len(dropped),
                        " | ".join(text.splitlines()[0][:60] for text in dropped[:5]),
                    )
                time.sleep(TICK_SECONDS)
            except Exception:  # noqa: BLE001 — расписание не имеет права умереть
                logger.exception("Сбой в расписании")
                time.sleep(FAIL_PAUSE)
    finally:
        store.close()

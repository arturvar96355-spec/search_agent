"""Расписание фоновых дел.

Проверяем ровно то, что может испортить владельцу день: задача, выполненная
дважды, задача, выполненная не вовремя, и упавшая задача, которая утащила
за собой остальные.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from orbit import schedule
from orbit.schedule import MSK, Job


def at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=MSK)


def _job(**kwargs) -> Job:
    kwargs.setdefault("key", "test")
    kwargs.setdefault("title", "проверка")
    kwargs.setdefault("run", lambda store: "готово")
    return Job(**kwargs)


def test_daily_waits_for_its_hour(store):
    job = _job(at=(21, 0))
    assert not schedule.due(job, store, at(20, 59))
    assert schedule.due(job, store, at(21, 0))


def test_daily_runs_once_a_day(store):
    job = _job(at=(4, 30))
    assert schedule.tick(store, now=at(4, 30), only=[job]) == ["готово"]
    assert schedule.tick(store, now=at(5, 0), only=[job]) == []
    # Следующий день — снова можно.
    assert schedule.tick(store, now=at(4, 30, day=8), only=[job]) == ["готово"]


def test_late_start_still_runs_today(store):
    """Бот подняли в 23:00, а сводка была на 21:00 — она должна уйти.

    Два часа опоздания — это перезапуск бота, а не сутки простоя.
    """
    job = _job(at=(21, 0))
    assert schedule.tick(store, now=at(23, 10), only=[job]) == ["готово"]


def test_every_waits_the_interval(store):
    job = _job(every=10, at=None)
    assert schedule.due(job, store, at(12, 0))
    schedule.mark_done(job, store, at(12, 0))
    assert not schedule.due(job, store, at(12, 9))
    assert schedule.due(job, store, at(12, 10))


def test_job_must_have_exactly_one_schedule():
    with pytest.raises(ValueError):
        Job("x", "x", lambda store: None)
    with pytest.raises(ValueError):
        Job("x", "x", lambda store: None, at=(1, 0), every=5)


def test_broken_job_does_not_stop_the_rest(store):
    """Упавшая задача сообщает о себе и не мешает следующим."""
    def boom(_store):
        raise RuntimeError("нет места на диске")

    jobs = [
        _job(key="bad", title="бэкап баз", run=boom, at=(4, 30)),
        _job(key="good", title="сводка", at=(4, 30)),
    ]
    messages = schedule.tick(store, now=at(4, 30), only=jobs)

    assert len(messages) == 2
    assert "не выполнилась" in messages[0]
    assert "нет места на диске" in messages[0]
    assert messages[1] == "готово"


def test_broken_job_waits_before_retrying(store):
    """Задача упала — пробуем ещё раз, но не каждую минуту.

    Повторить нужно: бэкап, не сделанный из-за занятой базы, иначе пропал бы
    на сутки. А вот долбить каждую минуту нельзя — это лишние заходы подряд.
    """
    calls = []

    def boom(_store):
        calls.append(1)
        raise RuntimeError("нет места")

    job = _job(run=boom, at=(4, 30))
    assert schedule.tick(store, now=at(4, 30), only=[job])
    assert schedule.tick(store, now=at(4, 31), only=[job]) == []
    assert len(calls) == 1

    later = at(4, 30) + timedelta(minutes=schedule.RETRY_MINUTES + 1)
    schedule.tick(store, now=later, only=[job])
    assert len(calls) == 2, "через четверть часа заход обязан повториться"


def test_the_owner_hears_about_a_broken_job_once(store):
    """Про одну и ту же поломку говорим один раз, а не семнадцать.

    Бэкап в 4:30 повторяется каждые пятнадцать минут до конца окна в четыре
    часа: это семнадцать заходов и семнадцать одинаковых тревог подряд.
    Одинаковая тревога по кругу перестаёт читаться на третий раз, и настоящая
    новость среди них теряется.
    """
    def boom(_store):
        raise RuntimeError("нет места")

    job = _job(run=boom, at=(4, 30))
    said = []
    moment = at(4, 30)
    for _ in range(17):
        said += schedule.tick(store, now=moment, only=[job])
        moment += timedelta(minutes=schedule.RETRY_MINUTES + 1)

    assert len(said) == 1, said
    assert "нет места" in said[0]


def test_a_job_that_recovers_says_so(store):
    """Заработало — это новость, и о ней говорим отдельно.

    Иначе первая тревога («не выполнилась») остаётся последним словом, и
    владелец не знает, чинилось это само или так и лежит.
    """
    calls = []

    def flaky(_store):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("нет места")
        return None  # тихая задача: своих слов у неё нет

    job = _job(run=flaky, at=(4, 30))
    assert schedule.tick(store, now=at(4, 30), only=[job])
    later = at(4, 30) + timedelta(minutes=schedule.RETRY_MINUTES + 1)
    said = schedule.tick(store, now=later, only=[job])
    assert said and "снова работает" in said[0], said


def test_a_job_that_never_failed_does_not_report_recovery(store):
    """А задача, которая и не падала, «снова работает» не рапортует."""
    job = _job(run=lambda store: None, at=(4, 30))
    assert schedule.tick(store, now=at(4, 30), only=[job]) == []


def test_successful_run_clears_the_failure_mark(store):
    """После удачного захода отметка о падении не должна мешать завтрашнему."""
    calls = []

    def flaky(_store):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("первый раз не вышло")
        return "готово"

    job = _job(run=flaky, at=(4, 30))
    schedule.tick(store, now=at(4, 30), only=[job])
    later = at(4, 30) + timedelta(minutes=schedule.RETRY_MINUTES + 1)
    assert schedule.tick(store, now=later, only=[job])[-1] == "готово"
    assert schedule.tick(store, now=at(23, 0), only=[job]) == []


def test_late_start_beyond_the_window_skips_until_tomorrow(store):
    """Бот подняли в полночь — письма вдогонку не летят людям среди ночи."""
    job = _job(at=(11, 0), window=240)
    assert schedule.tick(store, now=at(23, 50), only=[job]) == []
    assert schedule.tick(store, now=at(12, 0, day=8), only=[job]) == ["готово"]


def test_silent_job_sends_nothing(store):
    """Задаче есть что делать, но нечего сказать — молчим."""
    job = _job(run=lambda store: None, at=(4, 30))
    assert schedule.tick(store, now=at(4, 30), only=[job]) == []


def test_registered_jobs_are_sane():
    for job in schedule.jobs():
        assert job.key and job.title
        assert bool(job.at) != bool(job.every)


# --- Недоставленные уведомления ------------------------------------------------


def test_undelivered_message_is_retried(store, monkeypatch):
    """Сводка за день бывает раз в сутки: потерять её из-за заминки нельзя."""
    from orbit import notify
    from orbit import schedule as sched

    sent: list[str] = []
    state = {"ok": False}

    def tell(text, **kwargs):
        if not state["ok"]:
            return False
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "tell", tell)

    # Один круг цикла руками: задача отработала, телеграм не принял.
    undelivered: list[str] = []
    for text in ["итоги дня"]:
        if not tell(text):
            undelivered.append(text)
    assert undelivered == ["итоги дня"]
    assert sent == []

    # Следующий круг: связь вернулась, придержанное уходит.
    state["ok"] = True
    for text in list(undelivered):
        assert tell(text)
    assert sent == ["итоги дня"]
    assert sched.UNDELIVERED_KEEP >= 1


def test_undelivered_queue_is_bounded():
    """Час без связи не должен обернуться пачкой из сотни уведомлений."""
    assert 1 <= schedule.UNDELIVERED_KEEP <= 50


def test_the_undelivered_queue_keeps_the_oldest():
    """Резать надо хвост: суточные отчёты встают в очередь первыми.

    Было наоборот, и выбрасывались как раз те сообщения, ради которых очередь
    заведена: сводка за день и отчёт о бэкапе бывают раз в сутки, а проверка
    почты бежит каждые десять минут и вытесняла их своими повторами.
    """
    keep = schedule.UNDELIVERED_KEEP
    queue = ["🌙 Итоги дня", "💾 Бэкап не удался"] + [f"почта {n}" for n in range(keep * 3)]
    trimmed = queue[:keep] if len(queue) > keep else queue

    assert trimmed[0] == "🌙 Итоги дня"
    assert trimmed[1] == "💾 Бэкап не удался"
    assert len(trimmed) == keep


# --- Неудавшийся бэкап не считается сделанным ---------------------------------

def test_failed_backup_is_retried_not_marked_done(store, monkeypatch):
    """Отчёт с ошибкой — это не выполненная задача.

    Раньше `backup.run()` возвращал отчёт с ok=False, задача отдавала его
    текст наверх как обычный результат, и расписание ставило отметку
    «сделано». Следующая попытка была бы только через сутки — а копии за
    сегодня не появилось бы вовсе.
    """
    from orbit import backup, schedule

    broken = backup.Report(items=[backup.Item("orbit.db", ok=False, note="файла нет")])
    monkeypatch.setattr(backup, "run", lambda *a, **kw: broken)

    job = next(j for j in schedule.jobs() if j.key == "backup")
    moment = datetime(2026, 8, 24, 4, 31, tzinfo=MSK)
    messages = schedule.tick(store, now=moment, only=[job])

    assert not store.get_state(schedule._state_key(job)), "отметку «сделано» ставить нельзя"
    assert any("не выполнилась" in text for text in messages)


def test_successful_backup_is_marked_done(store, monkeypatch):
    from orbit import backup, schedule

    good = backup.Report(items=[backup.Item("orbit.db", ok=True, note="скопирована")])
    monkeypatch.setattr(backup, "run", lambda *a, **kw: good)

    job = next(j for j in schedule.jobs() if j.key == "backup")
    moment = datetime(2026, 8, 24, 4, 31, tzinfo=MSK)
    messages = schedule.tick(store, now=moment, only=[job])

    # Сообщением это больше не приходит: удачный бэкап — отчёт о сделанном,
    # а приходил он в половине пятого утра. Строка ждёт вечерней сводки.
    assert not messages, "среди ночи этим не будим"
    notes = store.quiet_notes()
    assert any("копирова" in note.lower() or "Бэкап" in note for note in notes), notes
    assert not schedule.due(job, store, moment), "второй раз за день не делаем"


def test_a_job_that_fails_every_day_is_reported_every_day(store):
    """Отметка «уже сказали» обязана протухать за сутки.

    Она снималась только успехом, и задача, падающая каждый день, сообщала о
    себе однажды — а дальше молчала месяцами. За день ничего не меняется, а
    назавтра это уже новость.
    """
    def boom(_store):
        raise RuntimeError("нет места")

    job = _job(run=boom, at=(4, 30))
    said = []
    for day in (7, 8, 9):
        moment = at(4, 30, day=day)
        for _ in range(5):
            said += schedule.tick(store, now=moment, only=[job])
            moment += timedelta(minutes=schedule.RETRY_MINUTES + 1)

    assert len(said) == 3, f"ждали по одной тревоге в день, получили {len(said)}"

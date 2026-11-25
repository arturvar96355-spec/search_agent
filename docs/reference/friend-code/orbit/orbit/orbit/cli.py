"""Командная строка Orbit.

Всё то же самое можно сделать кнопками в боте — это для отладки и для запуска
по расписанию, когда бот не нужен.
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from loguru import logger

from . import importer, outreach
from .config import get_settings
from .models import Channel, Kind, Stage
from .store import Store

app = typer.Typer(add_completion=False, help="UniqOre Orbit: все системы под одним ботом.")


def _store() -> Store:
    return Store(get_settings().db_path)


def _kind(name: str) -> Kind:
    try:
        return Kind(name)
    except ValueError:
        typer.echo(f"Не знаю тип «{name}». Есть: partner, client, order.")
        raise typer.Exit(1) from None


@app.command()
def bot() -> None:
    """Запускает телеграм-бота (основной режим)."""
    from .bot import run

    run()


@app.command("import")
def import_cmd(
    path: str = typer.Argument("", help="Файл или каталог выгрузок. По умолчанию — output парсера."),
    kind: str = typer.Option("", help="partner или client. По умолчанию определяется по колонкам."),
) -> None:
    """Загружает выгрузки парсера в базу Orbit."""
    settings = get_settings()
    target = Path(path) if path else settings.leads_path / "output"
    store = _store()
    try:
        broken: list[str] = []
        if target.is_dir():
            rows, written, broken = importer.import_dir(store, target)
        else:
            rows, written = importer.import_file(store, target, _kind(kind) if kind else None)
    finally:
        store.close()
    typer.echo(f"Прочитано строк: {rows}, записано контактов: {written}")
    if broken:
        typer.echo(f"Не прочитались файлы ({len(broken)}): {', '.join(broken)}")


@app.command()
def validate(
    kind: str = typer.Option("", help="partner или client. По умолчанию — все."),
    limit: int = typer.Option(500, help="Сколько адресов проверить за раз."),
    close: bool = typer.Option(
        False, "--close", help="Закрывать контакты, у которых не осталось живых адресов."
    ),
) -> None:
    """Проверяет адреса в базе: живы ли домены, нет ли служебных ящиков.

    Проверка стоит одного запроса в DNS на домен, а один возврат письма стоит
    доверия ко всему нашему домену. Поэтому лучше пройтись по базе заранее,
    чем узнавать про мёртвый адрес в момент отправки.

    Без `--close` ничего не меняет: только показывает, что нашлось.
    """
    from . import mailcheck

    kinds = [_kind(kind)] if kind else [Kind.PARTNER, Kind.CLIENT]
    store = _store()
    good = bad = unknown = closed = 0
    try:
        for target in kinds:
            for stage in (Stage.NEW, Stage.QUEUED):
                for contact in store.contacts(target, stage, limit=limit):
                    if contact.channel is not Channel.EMAIL:
                        continue
                    address, verdict = mailcheck.pick_address(store, contact)
                    if address:
                        good += 1
                        continue
                    if verdict.permanent:
                        bad += 1
                        # Мёртвая почта — не приговор компании, если у неё есть
                        # телеграм: контакт останется живым, просто пойдёт
                        # другим каналом.
                        via_telegram = bool(contact.best_telegram)
                        mark = " (но есть телеграм — не закрываю)" if via_telegram else ""
                        typer.echo(f"✗ {contact.name}: {verdict.reason}{mark}")
                        if close and contact.pk is not None and not via_telegram:
                            store.set_stage(
                                contact.pk, Stage.DEAD, f"адрес не годится: {verdict.reason}"[:200]
                            )
                            closed += 1
                    else:
                        unknown += 1
                        typer.echo(f"? {contact.name}: {verdict.reason}")
    finally:
        store.close()

    typer.echo(f"\nЖивых адресов: {good}, негодных: {bad}, не проверено: {unknown}")
    if bad and not close:
        typer.echo("Контакты не тронуты. Чтобы закрыть негодные, повторите с --close.")
    elif closed:
        typer.echo(f"Закрыто контактов: {closed}")


@app.command()
def prepare(
    kind: str = typer.Argument(..., help="partner или client"),
    count: int = typer.Option(5, help="Сколько писем подготовить."),
) -> None:
    """Пишет пачку писем и кладёт их в очередь (не отправляя)."""
    from .sections.base import write_for

    target = _kind(kind)
    store = _store()
    try:
        candidates = [c for c in store.contacts(target, Stage.NEW, limit=count * 3) if c.reachable]
        if not candidates:
            typer.echo("Некому писать: новых контактов с почтой или телеграмом нет.")
            return
        done = 0
        for contact in candidates[:count]:
            ok, reason = write_for(store, contact)
            status = "готово" if ok else reason
            typer.echo(f"{contact.name}: {status}")
            done += int(ok)
        typer.echo(f"\nВ очереди: {store.queue_size(target)} (добавлено {done})")
    finally:
        store.close()


@app.command()
def send(
    kind: str = typer.Argument(..., help="partner или client"),
    yes: bool = typer.Option(False, "--yes", help="Подтверждение: письма реально уйдут."),
) -> None:
    """Отправляет очередь. Без --yes только показывает, что ушло бы."""
    target = _kind(kind)
    store = _store()
    try:
        queue = store.queue(target, limit=1000)
        if not queue:
            typer.echo("Очередь пустая.")
            return
        import random
        import time

        sent_any = False
        for draft, contact in queue:
            if not yes:
                verdict = outreach.can_send(store, draft, contact)
                typer.echo(f"[проверка] {contact.name}: {verdict.reason}")
                continue

            # Пауза нужна только между письмами, которые РЕАЛЬНО уходят на
            # почтовый сервер. Раньше спали после каждого письма подряд — и
            # когда очередь упиралась в суточный лимит, команда честно спала
            # по полторы минуты перед каждым отказом: пять отказов, семь минут
            # сна и ни одного письма. Телеграм тоже не считаем: его отправляет
            # владелец руками, ящик при этом не нагружается.
            verdict = outreach.can_send(store, draft, contact)
            if sent_any and verdict.ok and not verdict.manual:
                # Тот же темп, что у фонового отправщика. Без паузы эта команда
                # выпускала всю очередь за секунду — а двадцать писем, ушедших
                # разом, это первый признак рассылки и для получателя, и для
                # спам-фильтра. Суточный лимит от этого не спасает: он про
                # количество, а не про темп.
                pause = random.uniform(*outreach.PAUSE_RANGE)
                typer.echo(f"  … пауза {pause:.0f} с")
                time.sleep(pause)

            result = outreach.send(store, draft.pk or 0)
            typer.echo(f"{contact.name}: {result.reason}")
            sent_any = sent_any or result.ok
        if not yes:
            typer.echo("\nНичего не отправлено. Повторите с --yes.")
    finally:
        store.close()


@app.command("mailcheck")
def mail_check() -> None:
    """Проверяет вход на почтовый сервер. Ничего не отправляет.

    Нужна ровно один раз — после того, как в .env вписали пароль ящика.
    Отдельная команда потому, что неверный пароль иначе вскрылся бы только
    на первом живом письме, а к тому моменту очередь уже помечена ошибками.
    """
    import smtplib

    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_user:
        typer.echo("Не заполнены SMTP_HOST или SMTP_USER в orbit/.env")
        raise typer.Exit(1)
    if not settings.smtp_password:
        typer.echo(f"Пароль для {settings.smtp_user} не вписан в orbit/.env")
        raise typer.Exit(1)

    typer.echo(f"Подключаюсь к {settings.smtp_host}:{settings.smtp_port} как {settings.smtp_user}…")
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            server.starttls()
        with server:
            server.login(settings.smtp_user, settings.smtp_password)
    except smtplib.SMTPAuthenticationError as exc:
        typer.echo(f"Сервер не принял пароль: {exc}")
        raise typer.Exit(1) from None
    except Exception as exc:  # noqa: BLE001 — показываем владельцу что угодно, что пошло не так
        typer.echo(f"Не подключился: {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from None

    typer.echo("Готово: сервер принял пароль, письма можно отправлять.")


@app.command("findmail")
def findmail(
    kind: str = typer.Argument("client", help="partner или client"),
    limit: int = typer.Option(20, help="Сколько компаний обойти за раз."),
    retry: bool = typer.Option(False, "--retry", help="Пройти заново тех, у кого ничего не нашлось."),
) -> None:
    """Ищет почту и телеграм на сайтах тех, кому пока некуда написать.

    Из карт приходит один телефон, а адрес почти всегда лежит на сайте
    компании. Обход вежливый: robots.txt соблюдаем, между запросами пауза.
    """
    from . import sitescan

    store = _store()
    try:
        target = _kind(kind)
        if retry:
            forgotten = sitescan.forget_failures(store, target)
            typer.echo(f"Возвращаю в обход: {forgotten}")
        scanned, gained = sitescan.enrich(store, target, limit=limit)
        if not scanned:
            typer.echo("Обходить некого: у всех либо есть канал связи, либо мы уже ходили.")
            return
        typer.echo(f"Обошли компаний: {scanned}, каналов связи добавилось: {gained}")
    finally:
        store.close()


@app.command("backup")
def backup_cmd(
    quiet: bool = typer.Option(False, "--quiet", help="Не писать владельцу в телеграм."),
) -> None:
    """Делает копию баз и выгрузок парсера.

    Бот делает это сам каждую ночь, команда нужна перед рискованными
    изменениями — когда копия нужна прямо сейчас, а не в 4:30.
    """
    from . import backup as backup_mod

    report = backup_mod.run() if quiet else backup_mod.run_and_tell()
    for item in report.items:
        mark = "ok" if item.ok else "ОШИБКА"
        typer.echo(f"[{mark}] {item.name}: {item.note}")
    typer.echo(f"\nКуда: {report.folder}")
    typer.echo(f"Копий: {report.copies} · {backup_mod.human_size(report.total_size)}")
    if not report.ok:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Короткая сводка: что в базе и что настроено."""
    settings = get_settings()
    store = _store()
    try:
        for kind in (Kind.PARTNER, Kind.CLIENT, Kind.ORDER):
            counts = store.stage_counts(kind)
            total = sum(counts.values())
            if not total:
                continue
            parts = [f"{stage.value}={count}" for stage, count in counts.items() if count]
            typer.echo(f"{kind.value}: всего {total} · " + " ".join(parts))
        bad = store.bad_addresses(limit=1000)
        if bad:
            typer.echo(f"\nадресов забраковано проверкой: {len(bad)}")
            for row in bad[:5]:
                typer.echo(f"  {row['address']} — {row['reason']}")

        typer.echo(f"\nпочта: {'настроена' if settings.smtp_ready else 'НЕ настроена'}")
        typer.echo(f"ключ модели: {'есть' if settings.anthropic_api_key else 'НЕТ'}")
        typer.echo(f"токен бота: {'есть' if settings.telegram_bot_token else 'НЕТ'}")
        typer.echo(f"база: {settings.db_path}")
    finally:
        store.close()


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <7} | {message}")
    settings = get_settings()
    if settings.log_file:
        logger.add(settings.log_file, rotation="10 MB", retention=5, encoding="utf-8")
    app()


if __name__ == "__main__":
    main()

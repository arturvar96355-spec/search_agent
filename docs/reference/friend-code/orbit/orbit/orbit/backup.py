"""Ежедневная копия всего, что нажито.

Базы живут в WAL, и обычное копирование файла даёт битую копию: часть данных
в этот момент лежит в -wal, а не в самой базе. Поэтому копируем через
sqlite backup API — он снимает согласованный слепок прямо на ходу, не мешая
ни боту, ни сборщику.

Каждая копия сразу проверяется: открываем её, спрашиваем quick_check и
считаем строки. Копия, о которой мы не знаем, читается ли она, — не копия,
а надежда.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

from .config import get_settings
from .store import today_msk

# Сколько дней держим копии. Две недели — компромисс: заметить, что данные
# испортились, обычно успеваешь за несколько дней, а место копии занимают.
KEEP_DAYS = 14

# Что считаем в каждой базе, чтобы в отчёте была не только величина файла.
# Файл может весить сколько угодно и при этом быть пустым внутри.
COUNTED = {
    "orbit.db": ("contacts", "контактов"),
    "fbid.db": ("orders", "заказов"),
}


@dataclass
class Item:
    """Одна строка отчёта: что копировали и чем кончилось."""

    name: str
    ok: bool
    note: str
    size: int = 0


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    folder: Path | None = None
    total_size: int = 0
    copies: int = 0
    dropped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(item.ok for item in self.items)


def human_size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} МБ"
    if value >= 1024:
        return f"{value / 1024:.0f} КБ"
    return f"{value} Б"


def backup_dir() -> Path:
    return get_settings().db_path.parent / "backups"


def sources() -> list[Path]:
    """Базы, которые копируем. Порядок важен только для читаемости отчёта."""
    settings = get_settings()
    return [
        settings.db_path,
        settings.bids_path / "data" / "fbid.db",
    ]


def copy_db(src: Path, dst: Path) -> Item:
    """Снимает слепок базы и тут же проверяет, что он читается."""
    if not src.exists():
        return Item(src.name, ok=False, note="файла нет")

    try:
        # mode=ro: копируем даже если базу прямо сейчас пишет другой процесс.
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
        try:
            target = sqlite3.connect(dst, timeout=30.0)
            try:
                source.backup(target)
                # Копия наследует WAL, и рядом с ней остаются -wal и -shm.
                # Для восстановления это лишние детали: переводим копию в
                # обычный режим, чтобы она была ровно одним файлом.
                target.execute("PRAGMA journal_mode = DELETE")
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error as exc:
        return Item(src.name, ok=False, note=f"не скопировалась: {exc}")

    return _verify(src.name, dst)


def _verify(name: str, path: Path) -> Item:
    """Открывает копию и убеждается, что данные на месте."""
    size = path.stat().st_size if path.exists() else 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
        try:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
            if verdict != "ok":
                return Item(name, ok=False, note=f"копия битая: {verdict}", size=size)
            note = "цела"
            table, word = COUNTED.get(name, ("", ""))
            if table:
                rows = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
                note = f"{rows} {word}, цела"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Item(name, ok=False, note=f"копия не открывается: {exc}", size=size)
    return Item(name, ok=True, note=note, size=size)


def copy_exports(dst_root: Path) -> Item:
    """Копирует выгрузки парсера — то, что стоило часов сбора."""
    src = get_settings().leads_path / "output"
    if not src.is_dir():
        return Item("выгрузки парсера", ok=True, note="каталога нет, пропустил")

    files = [p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")]
    if not files:
        return Item("выгрузки парсера", ok=True, note="пусто, пропустил")

    dst = dst_root / "exports"
    dst.mkdir(parents=True, exist_ok=True)
    size = 0
    copied = 0
    for path in files:
        try:
            relative = path.relative_to(src)
            target = dst / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            size += target.stat().st_size
            copied += 1
        except OSError as exc:
            logger.warning("Не скопировал {}: {}", path.name, exc)
    if not copied:
        return Item("выгрузки парсера", ok=False, note="ни один файл не скопировался")
    if copied < len(files):
        # Частичная копия — это НЕ успех. Раньше успехом считалось «хоть
        # что-то скопировалось», а от ok зависит право удалять старые копии:
        # неполный слепок разрешал стереть полный, сделанный две недели назад.
        # Так теряются именно те выгрузки, ради которых бэкап и заведён.
        missed = len(files) - copied
        return Item(
            "выгрузки парсера", ok=False,
            note=f"скопировано {copied} из {len(files)}, не вышло {missed}", size=size,
        )
    return Item("выгрузки парсера", ok=True, note=f"{copied} файлов", size=size)


def drop_old(root: Path, *, keep_days: int = KEEP_DAYS, today: str | None = None) -> list[str]:
    """Удаляет копии старше срока.

    Смотрим на имя папки, а не на дату файла: файл легко «помолодеет» от
    любого касания, а имя копии — это дата, ради которой её и делали.
    """
    stamp = today or today_msk()
    try:
        edge = date.fromisoformat(stamp) - timedelta(days=keep_days)
    except ValueError:
        return []

    dropped = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        if not folder.is_dir():
            continue
        try:
            when = date.fromisoformat(folder.name)
        except ValueError:
            continue  # не наша папка — не трогаем
        if when < edge:
            shutil.rmtree(folder, ignore_errors=True)
            dropped.append(folder.name)
    return dropped


def run(*, root: Path | None = None) -> Report:
    """Делает копию всего и возвращает отчёт."""
    root = root or backup_dir()
    folder = root / today_msk()
    folder.mkdir(parents=True, exist_ok=True)

    report = Report(folder=folder)
    for src in sources():
        report.items.append(copy_db(src, folder / src.name))
    report.items.append(copy_exports(folder))

    # Старые копии удаляем, только если новая получилась целиком. Иначе один
    # неудачный проход (переименовали каталог, база занята) выносил историю:
    # ротация работала, а новой копии взамен не появлялось.
    if report.ok:
        report.dropped = drop_old(root)
    else:
        logger.warning("Бэкап неполный — старые копии оставляю как есть")
    report.total_size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    report.copies = len([p for p in root.iterdir() if p.is_dir()]) if root.is_dir() else 0

    for item in report.items:
        level = logger.info if item.ok else logger.error
        level("Бэкап {}: {}", item.name, item.note)
    return report


def summary(report: Report) -> str:
    """Текст для владельца."""
    head = "💾 <b>Бэкап</b>" if report.ok else "⚠️ <b>Бэкап с ошибкой</b>"
    lines = [f"{head} · {report.folder.name if report.folder else '—'}", ""]
    for item in report.items:
        mark = "·" if item.ok else "❗️"
        size = f" · {human_size(item.size)}" if item.size else ""
        lines.append(f"{mark} {item.name} — {item.note}{size}")
    lines.append("")
    lines.append(
        f"Копий на диске: {report.copies} · {human_size(report.total_size)} · "
        f"храню {KEEP_DAYS} дней"
    )
    if report.dropped:
        lines.append(f"Удалил старые: {', '.join(report.dropped)}")
    if not report.ok:
        lines.append("\nЧто-то не скопировалось — стоит посмотреть лог.")
    return "\n".join(lines)


def run_and_tell(*, root: Path | None = None) -> Report:
    """Делает копию и пишет владельцу, чем кончилось."""
    from .notify import tell

    report = run(root=root)
    tell(summary(report))
    return report

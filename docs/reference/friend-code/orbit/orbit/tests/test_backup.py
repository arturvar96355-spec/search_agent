"""Бэкап баз.

Главное, что проверяем: копия читается и в ней те же данные. Копия, которую
никто не открывал, годится только для спокойствия, а не для восстановления.
"""
from __future__ import annotations

import sqlite3

import pytest

from orbit import backup, config
from orbit.models import Contact, Kind
from orbit.store import Store


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Свой маленький мир: база Orbit, база биржи и выгрузки парсера."""
    orbit_db = tmp_path / "orbit" / "orbit.db"
    bids = tmp_path / "freelance-bid"
    leads = tmp_path / "leadgen"
    (bids / "data").mkdir(parents=True)
    (leads / "output").mkdir(parents=True)

    monkeypatch.setenv("DB_PATH", str(orbit_db))
    monkeypatch.setenv("BIDS_PATH", str(bids))
    monkeypatch.setenv("LEADS_PATH", str(leads))
    config.get_settings.cache_clear()

    store = Store(orbit_db)
    store.upsert_contact(Contact(kind=Kind.PARTNER, key="a.ru", name="Агентство А"))
    store.upsert_contact(Contact(kind=Kind.CLIENT, key="b.ru", name="Компания Б"))

    fbid_db = bids / "data" / "fbid.db"
    conn = sqlite3.connect(fbid_db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, title TEXT)")
    conn.executemany("INSERT INTO orders (title) VALUES (?)", [("раз",), ("два",), ("три",)])
    conn.commit()
    conn.close()

    (leads / "output" / "agencies.csv").write_text("name;site\nА;a.ru\n", encoding="utf-8")

    yield tmp_path
    store.close()
    config.get_settings.cache_clear()


def test_copies_everything(workspace, tmp_path):
    report = backup.run(root=tmp_path / "backups")
    assert report.ok
    names = {item.name for item in report.items}
    assert names == {"orbit.db", "fbid.db", "выгрузки парсера"}
    assert (report.folder / "orbit.db").exists()
    assert (report.folder / "fbid.db").exists()
    assert (report.folder / "exports" / "agencies.csv").exists()


def test_copy_is_readable_and_complete(workspace, tmp_path):
    """Копия должна открываться и содержать те же строки, что оригинал."""
    report = backup.run(root=tmp_path / "backups")

    copy = Store(report.folder / "orbit.db")
    try:
        assert copy.count(Kind.PARTNER) == 1
        assert copy.count(Kind.CLIENT) == 1
    finally:
        copy.close()

    conn = sqlite3.connect(report.folder / "fbid.db")
    try:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 3
    finally:
        conn.close()


def test_copy_is_a_single_file(workspace, tmp_path):
    """Рядом с копией не должно быть -wal и -shm: восстанавливать надо один файл."""
    report = backup.run(root=tmp_path / "backups")
    leftovers = [p.name for p in report.folder.iterdir() if p.suffix in (".db-wal", ".db-shm")]
    assert not leftovers
    assert not list(report.folder.glob("*-wal"))
    assert not list(report.folder.glob("*-shm"))


def test_report_counts_rows_not_only_bytes(workspace, tmp_path):
    """В отчёте должно быть видно содержимое: файл может весить и быть пустым."""
    report = backup.run(root=tmp_path / "backups")
    by_name = {item.name: item for item in report.items}
    assert "2 контактов" in by_name["orbit.db"].note
    assert "3 заказов" in by_name["fbid.db"].note


def test_copies_base_that_is_being_written(workspace, tmp_path):
    """Бот пишет в базу прямо сейчас — копия всё равно должна получиться.

    Ради этого и нужен sqlite backup API: при WAL часть свежих данных лежит
    в отдельном файле, и простое копирование даёт обрубок.
    """
    live = Store(config.get_settings().db_path)
    try:
        live.upsert_contact(Contact(kind=Kind.PARTNER, key="c.ru", name="Агентство В"))
        report = backup.run(root=tmp_path / "backups")
    finally:
        live.close()

    assert report.ok
    copy = Store(report.folder / "orbit.db")
    try:
        assert copy.find_contact(Kind.PARTNER, "c.ru") is not None
    finally:
        copy.close()


def test_missing_file_is_reported_not_swallowed(workspace, tmp_path):
    """Базы биржи нет — бэкап обязан сказать об этом, а не отчитаться «всё хорошо»."""
    (workspace / "freelance-bid" / "data" / "fbid.db").unlink()
    report = backup.run(root=tmp_path / "backups")

    assert not report.ok
    broken = next(item for item in report.items if item.name == "fbid.db")
    assert broken.note == "файла нет"
    assert "ошибк" in backup.summary(report).lower()
    # Остальное при этом всё равно скопировалось.
    assert (report.folder / "orbit.db").exists()


def test_broken_file_does_not_pass_as_backup(workspace, tmp_path):
    """Вместо базы мусор — копия не должна считаться удачной."""
    (workspace / "freelance-bid" / "data" / "fbid.db").write_bytes(b"not a database at all")
    report = backup.run(root=tmp_path / "backups")

    assert not report.ok
    broken = next(item for item in report.items if item.name == "fbid.db")
    assert "не" in broken.note


def test_old_copies_are_dropped(tmp_path):
    root = tmp_path / "backups"
    for name in ("2026-07-01", "2026-07-24", "2026-08-07", "чужая-папка"):
        (root / name).mkdir(parents=True)

    dropped = backup.drop_old(root, keep_days=14, today="2026-08-07")

    assert dropped == ["2026-07-01"]
    assert not (root / "2026-07-01").exists()
    assert (root / "2026-07-24").exists()
    assert (root / "чужая-папка").exists()


def test_second_run_same_day_overwrites(workspace, tmp_path):
    """Два бэкапа за день — одна папка, а не гора одинаковых копий."""
    first = backup.run(root=tmp_path / "backups")
    second = backup.run(root=tmp_path / "backups")
    assert first.folder == second.folder
    assert second.copies == 1


def test_summary_is_readable(workspace, tmp_path):
    text = backup.summary(backup.run(root=tmp_path / "backups"))
    assert "Бэкап" in text
    assert "orbit.db" in text
    assert "храню 14 дней" in text
    assert len(text) <= 3900


def test_partial_export_copy_is_not_a_success(workspace, tmp_path, monkeypatch):
    """Скопировалась половина выгрузок — старые копии удалять нельзя.

    От ok зависит право стирать прошлые слепки. Неполная копия, засчитанная
    как успех, разрешала удалить полную двухнедельной давности — то есть
    ровно то, ради чего бэкап и заведён.
    """
    import shutil

    from orbit import backup

    leads = backup.get_settings().leads_path / "output"
    (leads / "второй.csv").write_text("name;site\nБ;b.ru\n", encoding="utf-8")

    real_copy = shutil.copy2
    calls = {"n": 0}

    def flaky(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("диск отвалился")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(backup.shutil, "copy2", flaky)

    report = backup.run(root=tmp_path / "backups")
    exports = next(item for item in report.items if item.name == "выгрузки парсера")

    assert not exports.ok
    assert "из" in exports.note
    assert not report.ok, "неполный бэкап не должен разрешать удаление старых копий"
    assert report.dropped == [], "старые копии трогать нельзя"

"""База: дедупликация, стадии и очередь.

Главное, что здесь проверяется, — повторный импорт. Выгрузку парсера гоняют
не по одному разу, и второй прогон не должен ни плодить дубли, ни сбрасывать
стадию контакта, с которым уже разговаривают.
"""
from __future__ import annotations

from orbit.models import Channel, Contact, Draft, Kind, Stage


def test_new_contact_saved(store, agency):
    pk = store.upsert_contact(agency)
    saved = store.get_contact(pk)
    assert saved is not None
    assert saved.name == "Медиасфера"
    assert saved.stage is Stage.NEW


def test_same_key_does_not_duplicate(store, agency):
    store.upsert_contact(agency)
    store.upsert_contact(agency)
    assert store.count(Kind.PARTNER) == 1


def test_reimport_keeps_stage(store, agency):
    """Контакт уже в работе — повторная выгрузка не должна вернуть его в «новые»."""
    pk = store.upsert_contact(agency)
    store.set_stage(pk, Stage.REPLIED)
    store.upsert_contact(agency)
    assert store.get_contact(pk).stage is Stage.REPLIED


def test_reimport_adds_new_contacts_without_losing_old(store, agency):
    """Во второй выгрузке нашёлся ещё один адрес — он добавляется к прежним."""
    pk = store.upsert_contact(agency)
    enriched = Contact(
        kind=Kind.PARTNER, key=agency.key, name=agency.name, emails=["sales@mediasfera.ru"]
    )
    store.upsert_contact(enriched)
    saved = store.get_contact(pk)
    assert saved.emails == ["hello@mediasfera.ru", "sales@mediasfera.ru"]


def test_same_key_different_kind_is_another_contact(store, agency):
    """Агентство и компания с одним доменом — разные записи разных вкладок."""
    store.upsert_contact(agency)
    store.upsert_contact(Contact(kind=Kind.CLIENT, key=agency.key, name=agency.name))
    assert store.count(Kind.PARTNER) == 1
    assert store.count(Kind.CLIENT) == 1


# --- Стадии -----------------------------------------------------------------

def test_stage_counts(store, agency, company):
    a = store.upsert_contact(agency)
    store.upsert_contact(company)
    store.set_stage(a, Stage.WRITTEN)
    counts = store.stage_counts(Kind.PARTNER)
    assert counts[Stage.WRITTEN] == 1
    assert counts[Stage.NEW] == 0


def test_unknown_stage_does_not_break_reading(store, agency):
    """Кривая строка в базе не должна ронять экран."""
    pk = store.upsert_contact(agency)
    store.conn.execute("UPDATE contacts SET stage = 'зигзаг' WHERE pk = ?", (pk,))
    store.conn.commit()
    assert store.get_contact(pk).stage is Stage.NEW


def test_only_reachable_filter(store, agency, company):
    """Компания без почты и телеграма идёт в обзвон, а не в рассылку."""
    store.upsert_contact(agency)
    store.upsert_contact(company)
    assert store.count(Kind.PARTNER, only_reachable=True) == 1
    assert store.count(Kind.CLIENT, only_reachable=False) == 1
    assert store.count(Kind.CLIENT, only_reachable=True) == 0


# --- Очередь ----------------------------------------------------------------

def _draft(pk: int, body: str = "Здравствуйте, пишу по делу.") -> Draft:
    return Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body=body)


def test_draft_saved_and_found(store, agency):
    pk = store.upsert_contact(agency)
    store.save_draft(_draft(pk))
    assert store.queue_size(Kind.PARTNER) == 1
    assert store.draft_for(pk) is not None


def test_regenerating_replaces_draft(store, agency):
    """«Переписать» заменяет письмо, а не кладёт второе тому же адресату."""
    pk = store.upsert_contact(agency)
    store.save_draft(_draft(pk, "первый вариант"))
    store.save_draft(_draft(pk, "второй вариант"))
    assert store.queue_size(Kind.PARTNER) == 1
    assert store.draft_for(pk).body == "второй вариант"


def test_sent_draft_leaves_queue(store, agency):
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(_draft(pk))
    store.mark_sent(draft_pk)
    assert store.queue_size(Kind.PARTNER) == 0
    assert store.draft_for(pk) is None


def test_failed_draft_stays_in_queue(store, agency):
    """Почта не настроена — письмо ждёт, а не теряется."""
    pk = store.upsert_contact(agency)
    draft_pk = store.save_draft(_draft(pk))
    store.mark_failed(draft_pk, "почта не настроена")
    assert store.queue_size(Kind.PARTNER) == 1
    assert store.get_draft(draft_pk).error == "почта не настроена"


def test_drop_and_clear(store, agency, company):
    a = store.upsert_contact(agency)
    c = store.upsert_contact(company)
    store.save_draft(_draft(a))
    store.save_draft(_draft(c))
    assert store.drop_draft(store.draft_for(a).pk) is True
    assert store.queue_size(Kind.PARTNER) == 0
    assert store.clear_queue(Kind.CLIENT) == 1


def test_queue_returns_contact_with_draft(store, agency):
    pk = store.upsert_contact(agency)
    store.save_draft(_draft(pk))
    items = store.queue(Kind.PARTNER)
    assert len(items) == 1
    draft, contact = items[0]
    assert contact.name == "Медиасфера"
    assert draft.subject == "Тема"


# --- Счётчики и настройки ---------------------------------------------------

def test_daily_counter(store):
    assert store.sent_today(Kind.PARTNER) == 0
    store.add_send(Kind.PARTNER)
    store.add_send(Kind.PARTNER)
    assert store.sent_today(Kind.PARTNER) == 2
    assert store.sent_today(Kind.CLIENT) == 0


def test_state_roundtrip(store):
    assert store.get_state("limit:partners") is None
    store.set_state("limit:partners", "30")
    assert store.get_state("limit:partners") == "30"
    store.delete_state("limit:partners")
    assert store.get_state("limit:partners") is None


def test_events_written_on_stage_change(store, agency):
    pk = store.upsert_contact(agency)
    store.set_stage(pk, Stage.WRITTEN, "написали (почта)")
    events = store.events(pk)
    assert events and events[0]["text"] == "написали (почта)"


def test_deleting_contact_removes_its_drafts(store, agency):
    """Внешний ключ с каскадом: письмо без адресата в очереди не нужно."""
    pk = store.upsert_contact(agency)
    store.save_draft(_draft(pk))
    store.conn.execute("DELETE FROM contacts WHERE pk = ?", (pk,))
    store.conn.commit()
    assert store.queue_size(Kind.PARTNER) == 0


# --- Обновление старой базы -------------------------------------------------

def test_old_database_gets_new_columns(tmp_path):
    """База, созданная прежней версией, должна дожить до новой без правки руками.

    CREATE TABLE IF NOT EXISTS не трогает существующую таблицу, поэтому новая
    колонка добавляется отдельно. Без этого живая база падала на первом же
    запросе к очереди.
    """
    import sqlite3

    from orbit.store import Store

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE contacts (
               pk INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL,
               name TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'new', city TEXT, site TEXT,
               emails TEXT NOT NULL DEFAULT '[]', phones TEXT NOT NULL DEFAULT '[]',
               telegram TEXT NOT NULL DEFAULT '[]', source_url TEXT,
               meta TEXT NOT NULL DEFAULT '{}', note TEXT,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(kind, key));
           CREATE TABLE drafts (
               pk INTEGER PRIMARY KEY AUTOINCREMENT, contact_pk INTEGER NOT NULL,
               channel TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'draft', error TEXT,
               created_at TEXT NOT NULL, sent_at TEXT);"""
    )
    old.commit()
    old.close()

    store = Store(path)
    try:
        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(drafts)")}
        assert "approved_at" in columns
        # И запросы к очереди работают, а не падают.
        assert store.approved_drafts(Kind.PARTNER) == []
    finally:
        store.close()


# --- Город — не сеть -----------------------------------------------------------

def test_a_city_name_is_never_treated_as_a_chain(store):
    """«Москва, стоматологическая клиника» — не филиал сети «Москва».

    Защита «разные сайты — разные компании» на данных 2ГИС не работает: сайта
    нет у большинства карточек, и развести компании ей нечем. Живая клиника
    закрывалась как филиал чужой сети и не получала письма вовсе.
    """
    from orbit.models import Contact, Kind, Stage

    first = Contact(
        kind=Kind.CLIENT, key="one", name="Москва, стоматологическая клиника",
        city="Москва", emails=["a@one.ru"], stage=Stage.WRITTEN,
    )
    first.pk = store.upsert_contact(first)
    store.set_stage(first.pk, Stage.WRITTEN, "написали")

    second = Contact(
        kind=Kind.CLIENT, key="two", name="Москва, стоматология на Арбате",
        city="Москва", emails=["b@two.ru"],
    )
    second.pk = store.upsert_contact(second)

    siblings = store.written_siblings(Kind.CLIENT, second.name, skip_pk=second.pk)
    assert siblings == [], "две несвязанные клиники не должны считаться одной сетью"


def test_a_real_chain_is_still_caught(store):
    """Настоящая сеть по-прежнему получает одно письмо, а не тринадцать."""
    from orbit.models import Contact, Kind, Stage

    first = Contact(
        kind=Kind.CLIENT, key="medsi-1", name="Медси, сеть медицинских центров",
        city="Москва", emails=["a@medsi.ru"], stage=Stage.WRITTEN,
    )
    first.pk = store.upsert_contact(first)
    store.set_stage(first.pk, Stage.WRITTEN, "написали")

    second = Contact(
        kind=Kind.CLIENT, key="medsi-2", name="Медси, сеть медицинских центров",
        city="Москва", emails=["b@medsi.ru"],
    )
    second.pk = store.upsert_contact(second)

    siblings = store.written_siblings(Kind.CLIENT, second.name, skip_pk=second.pk)
    assert [c.pk for c in siblings] == [first.pk]


def test_a_reply_from_another_address_goes_to_whoever_got_the_letter(store):
    """На домене два контакта — ответ принадлежит тому, кому мы писали.

    Порядок брался по updated_at, а его трогает что угодно: импорт выгрузки,
    обход сайтов, правка стадии, пометка о возврате. Наверх всплывал тот, чью
    карточку последней тронул фон, — и ответ директора уходил соседнему
    филиалу, двигая ЕГО стадию и его переписку.
    """
    from orbit.models import Contact, Kind

    wrote_to = store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="agency-msk", name="Агентство, Москва",
        emails=["info@agency.ru"],
    ))
    neighbour = store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="agency-spb", name="Агентство, Питер",
        emails=["spb@agency.ru"],
    ))

    store.log_event(wrote_to, "sent", "почта")
    # А соседа после этого тронул фон: перезалили выгрузку парсера, и
    # updated_at у него стал свежее. Ставим его руками, чтобы не зависеть от
    # разрешения часов: в бою между письмом и перезаливкой проходят часы.
    store.conn.execute(
        "UPDATE contacts SET updated_at = '2099-01-01 00:00:00' WHERE pk = ?", (neighbour,)
    )
    store.conn.commit()

    found = store.contact_by_domain("d.petrov@agency.ru")
    assert found is not None
    assert found.pk == wrote_to, f"ответ ушёл в «{found.name}»"


def test_without_any_letters_the_domain_match_still_answers(store):
    """Никому на домене не писали — сопоставление всё равно должно сработать."""
    from orbit.models import Contact, Kind

    pk = store.upsert_contact(Contact(
        kind=Kind.PARTNER, key="agency", name="Агентство", emails=["info@agency.ru"],
    ))
    found = store.contact_by_domain("d.petrov@agency.ru")
    assert found is not None and found.pk == pk

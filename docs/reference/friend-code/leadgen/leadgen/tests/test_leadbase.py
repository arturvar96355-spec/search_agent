

def test_unsaved_names_are_reported_once(tmp_path, monkeypatch):
    """Список «не записалось» очищается при чтении.

    Он копился без очистки, и предупреждение повторялось после КАЖДОГО
    следующего сбора, хотя новых сбоев не было. Повторяющаяся тревога
    перестаёт читаться, а вместе с ней теряется настоящая.
    """
    from parser.leadbase import LeadBase
    from parser.models import Organization

    base = LeadBase(tmp_path / "leads.jsonl")

    def no_disk(*args, **kwargs):
        raise OSError("диск переполнен")

    monkeypatch.setattr("builtins.open", no_disk)
    base.add(Organization(name="Улыбка", source="2gis",
                          source_url="https://2gis.ru/firm/1"))
    monkeypatch.undo()

    assert base.take_unsaved() == ["Улыбка"]
    assert base.take_unsaved() == [], "второй раз о том же сбое не сообщаем"


def test_new_failures_are_reported_again(tmp_path, monkeypatch):
    """А новый сбой — новая тревога."""
    from parser.leadbase import LeadBase
    from parser.models import Organization

    base = LeadBase(tmp_path / "leads.jsonl")

    def no_disk(*args, **kwargs):
        raise OSError("диск переполнен")

    monkeypatch.setattr("builtins.open", no_disk)
    base.add(Organization(name="А", source="2gis", source_url="https://2gis.ru/firm/1"))
    monkeypatch.undo()
    assert base.take_unsaved() == ["А"]

    monkeypatch.setattr("builtins.open", no_disk)
    base.add(Organization(name="Б", source="2gis", source_url="https://2gis.ru/firm/2"))
    monkeypatch.undo()
    assert base.take_unsaved() == ["Б"]

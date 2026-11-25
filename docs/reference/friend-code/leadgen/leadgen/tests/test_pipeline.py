

# --- Квота 2ГИС: не спрашиваем больше, чем нужно -------------------------------


class _FakeProvider:
    """Провайдер, который считает, сколько раз его спросили."""

    name = "fake"

    def __init__(self, asked, per_query=None):
        self.asked = asked
        self.per_query = per_query

    async def search(self, rubric, city, max_results):
        from parser.models import Organization

        self.asked.append((rubric, city, max_results))
        count = self.per_query if self.per_query is not None else max_results
        return [
            Organization(
                name=f"{city}-{n}", source="2gis",
                source_url=f"https://2gis.ru/{city}/{n}",
            )
            for n in range(count)
        ]


def _patch_providers(monkeypatch, asked, per_query=None):
    from parser import pipeline

    provider = _FakeProvider(asked, per_query)
    monkeypatch.setattr(pipeline, "build_providers", lambda active, settings: [provider])
    monkeypatch.setattr(pipeline, "resolve_source", lambda source, settings: "2gis")
    monkeypatch.setattr(pipeline, "_refresh", lambda s, a, p, st: (a, p))
    monkeypatch.setattr(pipeline.asyncio, "sleep", _no_sleep)


async def _no_sleep(_seconds):
    return None


def test_the_run_stops_once_it_has_enough(monkeypatch):
    """Дошли до нужного числа — дальше провайдера не дёргаем.

    Прогон по шести парам с потолком 100 забирал у 2ГИС до шестисот карточек,
    а вызывающий оставлял себе первую сотню: пятьсот запросов демо-ключа (их
    всего тысяча в месяц) сгорали впустую.
    """
    import asyncio

    from parser import pipeline

    asked: list[tuple[str, str, int]] = []
    _patch_providers(monkeypatch, asked)

    pairs = [("рубрика", f"город{n}") for n in range(6)]
    orgs = asyncio.run(pipeline.run(pairs, source="2gis", max_results=20, total_limit=20))

    assert len(orgs) >= 20
    assert len(asked) == 1, f"спросили {len(asked)} пар вместо одной"


def test_without_a_total_limit_every_pair_is_asked(monkeypatch):
    """Без потолка по сумме обходим все пары — старое поведение сохраняется."""
    import asyncio

    from parser import pipeline

    asked: list[tuple[str, str, int]] = []
    _patch_providers(monkeypatch, asked, per_query=3)

    pairs = [("рубрика", f"город{n}") for n in range(4)]
    asyncio.run(pipeline.run(pairs, source="2gis", max_results=3))
    assert len(asked) == 4


def test_each_pair_is_asked_for_its_share(monkeypatch):
    """Потолок на пару — доля от нужного объёма, а не весь объём."""
    import asyncio

    from parser import pipeline

    asked: list[tuple[str, str, int]] = []
    _patch_providers(monkeypatch, asked, per_query=1)

    pairs = [("рубрика", f"город{n}") for n in range(4)]
    asyncio.run(pipeline.run(pairs, source="2gis", max_results=8, total_limit=100))
    assert {row[2] for row in asked} == {8}, "потолок на запрос передаётся как есть"

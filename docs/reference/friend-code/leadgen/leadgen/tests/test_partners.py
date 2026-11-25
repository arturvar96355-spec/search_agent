import pytest

# --- Название агентства из выдачи ----------------------------------------------


@pytest.mark.parametrize(
    ("title", "host", "expected"),
    [
        ("Маркетинг для клиник — Агентство Атлант", "atlant.ru", "Агентство Атлант"),
        ("Агентство Атлант — маркетинг для клиник", "atlant.ru", "Агентство Атлант"),
        ("Продвижение сайтов в Москве | WunderDigital", "wunder-digital.ru", "WunderDigital"),
        ("SMMashing Media", "smmashing.media", "SMMashing Media"),
    ],
)
def test_the_brand_wins_over_the_slogan(title, host, expected):
    """Из «Слоган — Бренд» нужно имя, а не половина заголовка.

    Бралась именно первая половина, то есть в название попадал СЛОГАН: он
    уходил в холодное письмо («Здравствуйте, Маркетинг для клиник!») и мимо
    него проходил стоп-лист крупных агентств, который сверяется по имени.
    """
    from parser.providers.search import _clean_title

    assert _clean_title(title, host) == expected


@pytest.mark.parametrize(
    ("title", "host"),
    [
        ("Главная", "agency.ru"),
        ("Комплексный интернет-маркетинг для среднего бизнеса и не только", "mrk.ru"),
        ("", "x.ru"),
    ],
)
def test_a_title_that_is_not_a_name_falls_back_to_the_domain(title, host):
    """«Главная» и длинный слоган — не имя компании: обращаться к ним нельзя."""
    from parser.providers.search import _clean_title

    assert _clean_title(title, host) == host



# --- Яндекс: не перепутать организацию ---------------------------------------

def test_yandex_takes_the_matching_place_not_the_first_one():
    """Первая ссылка в выдаче — не обязательно наша фирма.

    Чужие жалобы, попавшие в карточку лида, уезжают прямо в холодное письмо:
    «вижу, у вас жалуются на…» — о том, чего у компании нет.
    """
    from parser.providers.yandex import _same_place

    cards = [
        {"href": "https://yandex.ru/maps/org/shinomontazh/111", "text": "Шиномонтаж 24"},
        {"href": "https://yandex.ru/maps/org/ds_avto/222?ll=1", "text": "ДС-Авто · автосервис"},
    ]
    assert _same_place(cards, "ДС Авто") == "https://yandex.ru/maps/org/ds_avto/222"


def test_yandex_stays_silent_when_the_place_is_not_in_the_list():
    from parser.providers.yandex import _same_place

    cards = [{"href": "https://yandex.ru/maps/org/other/111", "text": "Автомир"}]
    assert _same_place(cards, "ДС Авто") is None


def test_yandex_does_not_guess_by_a_two_letter_name():
    """«АС» найдётся внутри половины названий — такое совпадение ничего не значит."""
    from parser.providers.yandex import _same_place

    cards = [{"href": "https://yandex.ru/maps/org/kvartal/1", "text": "Квартал АСфальт"}]
    assert _same_place(cards, "АС") is None


# --- Цитата не должна вырасти в целую страницу отзывов -------------------------


def test_a_page_full_of_complaints_gives_a_short_quote():
    """Цитата — доказательство жалобы, а не сам отзыв.

    Совпадения ближе двенадцати знаков склеиваются в один спан, а на вход
    подаётся текст всей страницы отзывов — двенадцать прокруток. Страница, где
    на недозвон жалуются подряд, давала «цитату» в четыре с лишним тысячи
    знаков: карточка организации переваливала за лимит Telegram, тот отвергал
    её целиком, и лид молча пропадал из отчёта.
    """
    from parser.reviews import SNIPPET_LIMIT, find_complaints

    text = " ".join(["не дозвонишься совсем"] * 200)
    quotes = [quote for _category, quote in find_complaints(text)]

    assert quotes, "жалобы тут точно есть"
    longest = max(len(quote) for quote in quotes)
    assert longest <= SNIPPET_LIMIT + 2, f"цитата в {longest} знаков — это уже не цитата"


def test_a_long_quote_is_cut_on_a_word_boundary():
    """«не дозвонишьс» читается как опечатка — режем по пробелу."""
    from parser.reviews import find_complaints

    text = "не дозвониться " + "очень длинное продолжение отзыва " * 20
    quote = find_complaints(text)[0][1]
    assert quote.endswith("…")
    assert not quote.rstrip("…").endswith(" ")
    assert " " in quote


def test_a_short_complaint_is_untouched():
    """Короткая жалоба обрезаться не должна вовсе."""
    from parser.reviews import find_complaints

    quote = find_complaints("Звонил три раза — не дозвониться никак.")[0][1]
    assert "не дозвониться" in quote
    assert len(quote) < 60


def test_the_whole_card_fits_into_telegram():
    """Карточка организации со страницей жалоб обязана влезть в лимит Telegram."""
    from parser.models import Organization
    from parser.reviews import scan_reviews
    from parser.telegram import format_org

    org = Organization(
        name="Улыбка", source="2gis", phone=["+74951112233"],
        review_complaints=scan_reviews([" ".join(["не дозвонишься совсем"] * 200)]),
    )
    assert len(format_org(org)) <= 4096


import pytest  # noqa: E402

from parser.reviews import find_complaints  # noqa: E402

# --- Как люди на самом деле пишут про недозвон --------------------------------
#
# Каждый пропуск здесь — это потерянный лид: жалоба на связь и есть товар,
# который система ищет. Каждое ложное срабатывание — звонок компании, у
# которой всё в порядке. Поэтому оба списка проверяем вместе.

COMPLAINTS = [
    "Не смог дозвониться три дня подряд",
    "Не смогла дозвониться ни разу",
    "Не смогли дозвониться до администратора",
    "Не удалось дозвониться",
    "Не получается дозвониться уже неделю",
    "Так и не смог сегодня дозвониться",
    "дозванивался полдня, бесполезно",
    "дозваниваюсь неделю — невозможно",
    "Не дозванивалась ни разу",
    "Дозвониться нереально",
    "На звонки не реагируют",
    "Телефон вечно занят",
    "Ответа нет до сих пор",
    "Ждала ответа две недели",
    "Ответили только через неделю",
    "Перезвонили спустя три дня",
]

CLEAN = [
    "Дозвонился с первого раза, всё отлично",
    "Наконец-то дозвонились, спасибо",
    "Перезвонили через минуту, молодцы",
    "Трубку берут сразу",
    "На звонки отвечают моментально",
    "Ответили через 10 минут",
    "Дозвониться легко",
    "Ждала ответа и дождалась",
    "Дозванивалась и дозвонилась, всё хорошо",
]


@pytest.mark.parametrize("text", COMPLAINTS)
def test_a_real_complaint_is_found(text):
    assert find_complaints(text), f"жалоба не найдена: {text}"


@pytest.mark.parametrize("text", CLEAN)
def test_a_good_review_is_not_a_complaint(text):
    assert not find_complaints(text), f"похвалу приняли за жалобу: {text}"

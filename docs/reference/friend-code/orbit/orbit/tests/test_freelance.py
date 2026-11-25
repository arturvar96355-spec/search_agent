"""Вкладка «Фриланс»: воронка поверх биржи.

Проверяем главное: стадия только повышается. Биржа знает лишь «отклик
отправлен» и «в чате появился ответ», а то, что дошли до созвона, знает
владелец. Синхронизация не должна затирать его отметки.

Тесты работают на временной базе биржи, боевая не трогается.
"""
from __future__ import annotations

from datetime import UTC

import pytest

from orbit.models import Kind, Stage
from orbit.sections import freelance


@pytest.fixture()
def fbid_db(tmp_path, monkeypatch):
    """Пустая база биржи с одним отправленным откликом."""
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store as FbidStore

    path = tmp_path / "fbid.db"
    fbid_store = FbidStore(path)
    order = Order(
        source=Source.FL,
        ext_id="5516643",
        url="https://www.fl.ru/projects/5516643/",
        title="Лендинг для клиники",
        description="Нужен одностраничник с записью.",
        budget_value=45000.0,
    )
    order_pk, _is_new = fbid_store.upsert_order(order)
    fbid_store.record_bid(
        order_pk, BidStatus.SENT, reason="отправлено", text="Здравствуйте!", cost_rub=26.0
    )
    fbid_store.close()

    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FbidStore(path))
    return path


def test_sync_creates_order_contact(store, fbid_db):
    moved = freelance.sync(store)
    assert moved == 1
    contact = store.find_contact(Kind.ORDER, "fl:5516643")
    assert contact is not None
    assert contact.stage is Stage.WRITTEN
    assert contact.source_url.endswith("5516643/")


def test_sync_is_idempotent(store, fbid_db):
    freelance.sync(store)
    freelance.sync(store)
    assert store.count(Kind.ORDER) == 1


def test_client_reply_moves_to_talking(store, fbid_db):
    """Во вкладку «Начали общение» двигает подтверждённый ответ клиента."""
    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    fbid_store.set_state("clientreplied:fl:5516643", "1")
    fbid_store.close()

    freelance.sync(store)
    assert store.find_contact(Kind.ORDER, "fl:5516643").stage is Stage.REPLIED


def test_message_count_alone_does_not_move_to_talking(store, fbid_db):
    """Счётчику сообщений верить нельзя.

    Profi.ru дописывает под нашим откликом блок «Стоимость от … Контакты: …»,
    а владелец мог написать клиенту сам. И то и другое увеличивает счётчик,
    но ответом клиента не является — заказ должен остаться в «Откликнулись».
    """
    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    fbid_store.set_state("convo:fl:5516643", "3")
    fbid_store.close()

    freelance.sync(store)
    assert store.find_contact(Kind.ORDER, "fl:5516643").stage is Stage.WRITTEN


def test_sync_never_lowers_stage(store, fbid_db):
    """Владелец отметил «в работе» — биржа не должна вернуть заказ назад."""
    freelance.sync(store)
    contact = store.find_contact(Kind.ORDER, "fl:5516643")
    store.set_stage(contact.pk, Stage.DEAL)

    freelance.sync(store)
    assert store.get_contact(contact.pk).stage is Stage.DEAL


def test_screens_open(store, fbid_db):
    freelance.sync(store)
    contact = store.find_contact(Kind.ORDER, "fl:5516643")
    for action, arg in (("home", ""), ("list", "written:0"), ("card", str(contact.pk))):
        screen = freelance.handle(store, action, arg)
        assert screen.text.strip()
        assert len(screen.text) <= 4096


def test_card_has_link_to_chat(store, fbid_db):
    freelance.sync(store)
    contact = store.find_contact(Kind.ORDER, "fl:5516643")
    screen = freelance.handle(store, "card", str(contact.pk))
    links = [data for row in screen.rows for _label, data in row if data.startswith("url:")]
    assert links and links[0].endswith("5516643/")


def test_mark_moves_stage(store, fbid_db):
    freelance.sync(store)
    contact = store.find_contact(Kind.ORDER, "fl:5516643")
    freelance.handle(store, "mark", f"call:{contact.pk}")
    assert store.get_contact(contact.pk).stage is Stage.CALL


def test_home_survives_broken_exchange(store, monkeypatch):
    """Биржа недоступна — вкладка обязана открыться и сказать об этом."""
    def boom():
        raise RuntimeError("база биржи не найдена")

    monkeypatch.setattr(freelance, "_open_fbid_store", boom)
    screen = freelance.home(store)
    assert "не отвечает" in screen.text


def test_browser_action_is_not_run_from_orbit(store):
    """Проверку переписок Orbit не запускает: браузером занят сборщик.

    Раньше такая кнопка тихо возвращала «новых ответов нет» — в окружении
    Orbit нет Playwright, ошибка глохла в логе. Владелец при этом решал бы,
    что клиенты молчат.
    """
    screen = freelance.handle(store, "fb", "replies")
    assert "занимается сборщик" in screen.text
    assert "ответов нет" not in screen.text


def test_mode_button_toggles_between_two_positions(store, fbid_db):
    """Кнопка переключает молчание и автоответ дешёвым — и ничего больше.

    Прежний тумблер на три положения включал автоответ сразу всем, включая
    заказы на сотни тысяч. Теперь «отвечать» ограничено порогом суммы, и сам
    порог живёт в лимитах — случайным нажатием он не сдвигается.
    """
    from fbid import dialog
    from fbid.store import Store as FbidStore

    seen = []
    for _ in range(4):
        freelance.handle(store, "auto", "")
        fbid_store = FbidStore(fbid_db)
        try:
            seen.append(dialog.mode(fbid_store))
        finally:
            fbid_store.close()

    assert seen == [dialog.MODE_AUTO, dialog.MODE_NOTIFY, dialog.MODE_AUTO, dialog.MODE_NOTIFY]


def test_auto_mode_alert_names_the_threshold(store, fbid_db):
    """Владелец должен видеть сумму порога, а не догадываться о ней."""
    from fbid import dialog
    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    try:
        dialog.set_mode(fbid_store, dialog.MODE_NOTIFY)
    finally:
        fbid_store.close()

    screen = freelance.handle(store, "auto", "")
    assert "₽" in screen.alert, "в подсказке должна быть сумма порога"
    assert "до созвона" in screen.alert


def test_home_shows_current_mode(store, fbid_db):
    screen = freelance.home(store)
    labels = [label for row in screen.rows for label, _ in row]
    assert any("Клиент ответил" in label for label in labels)


def test_default_mode_is_notify_only(store, fbid_db):
    """По умолчанию бот пишет только первый отклик, дальше зовёт человека."""
    from fbid import dialog
    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    try:
        assert dialog.mode(fbid_store) == dialog.MODE_NOTIFY
    finally:
        fbid_store.close()


def test_call_agreement_moves_to_call_stage(store, fbid_db):
    """Клиент согласился созвониться — заказ сам переезжает в «Готовы к созвону».

    Это самое ценное событие воронки, и оно не должно ждать, пока владелец
    нажмёт кнопку вручную.
    """
    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    fbid_store.set_state("clientreplied:fl:5516643", "1")
    fbid_store.set_state("readycall:fl:5516643", "1")
    fbid_store.close()

    freelance.sync(store)
    assert store.find_contact(Kind.ORDER, "fl:5516643").stage is Stage.CALL


def test_call_stage_is_not_lowered_back(store, fbid_db):
    """Взяли в работу — согласие на созвон не должно откатить стадию назад."""
    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    fbid_store.set_state("readycall:fl:5516643", "1")
    fbid_store.close()

    freelance.sync(store)
    contact = store.find_contact(Kind.ORDER, "fl:5516643")
    store.set_stage(contact.pk, Stage.DEAL)

    freelance.sync(store)
    assert store.get_contact(contact.pk).stage is Stage.DEAL


# --- Список отправленных откликов -------------------------------------------

def test_today_bids_show_the_order(store, fbid_db):
    """Кнопка «Отклики за сегодня»: заказ, время, ссылка и сколько списали."""
    screen = freelance.bids(store, "today:0")

    assert "Отклики за сегодня" in screen.text
    assert "Лендинг для клиники" in screen.text
    assert "www.fl.ru/projects/5516643" in screen.text
    assert "списано 26 ₽" in screen.text


def test_all_bids_have_a_switch(store, fbid_db):
    screen = freelance.bids(store, "all:0")

    assert "Все отклики" in screen.text
    codes = [data for row in screen.rows for _label, data in row]
    assert "f:bids:today:0" in codes


def test_yesterday_bid_is_not_in_today(store, fbid_db):
    """Вчерашний отклик в «сегодня» попадать не должен — иначе цифре нельзя верить."""
    from datetime import datetime, timedelta

    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    try:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
        fbid_store.conn.execute("UPDATE bids SET created_at = ?, bid_at = NULL", (yesterday,))
        fbid_store.conn.commit()
    finally:
        fbid_store.close()

    assert "Сегодня ещё ни одного" in freelance.bids(store, "today:0").text
    assert "Лендинг для клиники" in freelance.bids(store, "all:0").text


def test_bids_button_is_on_the_home_screen(store, fbid_db):
    codes = [data for row in freelance.home(store).rows for _label, data in row]
    assert "f:bids:today:0" in codes and "f:bids:all:0" in codes


# --- Зависимость от соседнего проекта ------------------------------------------


def test_missing_bids_package_says_what_to_do(store, monkeypatch, tmp_path):
    """«Проекта нет» и «проект не установлен» — разные беды с разным лечением.

    Вкладка читает базу биржи напрямую, то есть Orbit зависит от соседнего
    проекта. В зависимостях его нет: это каталог рядом, а не пакет с PyPI.
    Раньше при установленном каталоге, но не установленном пакете владелец
    видел невнятное «проверка не прошла».
    """
    from orbit import config
    from orbit.sections import freelance

    (tmp_path / "freelance-bid").mkdir()
    monkeypatch.setattr(config.get_settings(), "bids_path", tmp_path / "freelance-bid",
                        raising=False)

    def not_installed():
        raise freelance.BidsNotInstalled(
            "проект биржи не установлен в окружение Orbit: "
            ".venv/bin/pip install -e ../freelance-bid"
        )

    monkeypatch.setattr(freelance, "_fbid", not_installed)
    state = freelance.health(store)
    assert not state.ok
    assert "не установлен" in state.summary
    assert any("pip install -e" in d for d in state.details)


def test_missing_bids_directory_is_a_different_message(store, monkeypatch, tmp_path):
    from orbit import config
    from orbit.sections import freelance

    monkeypatch.setattr(config.get_settings(), "bids_path", tmp_path / "нет-такого",
                        raising=False)
    state = freelance.health(store)
    assert not state.ok
    assert "не найден" in state.summary


def test_reply_mark_with_a_date_still_counts(store, fbid_db):
    """Отметка ответа теперь хранит дату, а не «1».

    Сравнение с «1» после этого перехода молча выключало вкладку «Начали
    общение»: ответы клиентов были в базе биржи, а в воронке не появлялись.
    """
    from fbid.store import Store as FbidStore

    fbid_store = FbidStore(fbid_db)
    fbid_store.set_state("clientreplied:fl:5516643", "2026-08-23T10:00:00+00:00")
    fbid_store.close()

    freelance.sync(store)
    assert store.find_contact(Kind.ORDER, "fl:5516643").stage is Stage.REPLIED


def test_sync_does_not_revive_a_closed_order(store, fbid_db):
    """Владелец отметил «отказались» — биржа не должна вернуть заказ в работу.

    DEAD не входит в список стадий воронки и получал ранг -1, то есть был
    «ниже» любой другой стадии: синхронизация поднимала его обратно вопреки
    обещанию «стадию только повышаем».
    """
    freelance.sync(store)
    contact = store.find_contact(Kind.ORDER, "fl:5516643")
    store.set_stage(contact.pk, Stage.DEAD, "клиент отказался")

    freelance.sync(store)
    assert store.get_contact(contact.pk).stage is Stage.DEAD


# --- «Назад» не должна открывать страницу, которой уже нет ---------------------


def test_back_from_a_card_lands_on_a_page_that_still_exists(store):
    """Список укоротился ровно потому, что владелец нажал «Отказались».

    «Назад» помнит страницу — и это правильно. Но возврат на вторую страницу
    однострочного списка показывал «Пусто.» на НЕПУСТОМ списке, без листалки
    (листать нечего) и без дороги к первой странице.
    """
    from orbit.models import Contact, Kind, Stage
    from orbit.sections import freelance
    from orbit.ui import PAGE_SIZE

    for n in range(PAGE_SIZE + 2):
        store.upsert_contact(Contact(
            kind=Kind.ORDER, key=f"fl:{n}", name=f"Заказ {n}",
            stage=Stage.WRITTEN, meta={"platform": "fl"},
        ))

    # Со второй страницы всё ушло: заказы перевели в другую стадию.
    for contact in store.contacts(Kind.ORDER, Stage.WRITTEN, limit=100)[PAGE_SIZE:]:
        store.set_stage(contact.pk, Stage.DEAD, "отказались")

    screen = freelance.show_list(store, f"{Stage.WRITTEN.value}:1")
    assert "Пусто." not in screen.text, screen.text
    codes = [code for row in screen.rows for _label, code in row]
    assert any(code.startswith("f:card:") for code in codes), "список не пустой — покажи его"


def test_an_empty_stage_still_says_it_is_empty(store):
    """А по-настоящему пустой список так и должен читаться."""
    from orbit.models import Stage
    from orbit.sections import freelance

    screen = freelance.show_list(store, f"{Stage.CALL.value}:0")
    assert "Пусто." in screen.text


def test_the_card_shows_when_the_bid_was_sent(store, monkeypatch, tmp_path):
    """Строка «Откликнулись» обязана появляться.

    Время бралось из `bid_at`, который заполнялся только в двухфазном режиме,
    и в боевой базе пуст у всех откликов. Строка не появлялась НИ РАЗУ, и
    владелец не видел в карточке главного — когда наш отклик ушёл.
    """
    from fbid.models import BidStatus, Order, Source
    from fbid.store import Store as FbidStore

    from orbit.models import Kind
    from orbit.sections import freelance

    exchange = FbidStore(tmp_path / "fbid.db")
    try:
        order = Order(source=Source.FL, ext_id="777", url="https://fl.ru/777",
                      title="Нужен лендинг", description="")
        pk, _ = exchange.upsert_order(order)
        exchange.conn.execute(
            """INSERT INTO bids (order_pk, status, reason, text, cost_rub,
                                 created_at, message_sent_at)
               VALUES (?, ?, '', 'наш отклик', 60, '2026-08-05T10:00:00+00:00',
                       '2026-08-05T18:11:16+00:00')""",
            (pk, BidStatus.SENT.value),
        )
        exchange.conn.commit()
    finally:
        exchange.close()

    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FbidStore(tmp_path / "fbid.db"))
    freelance.sync(store)

    contact = store.find_contact(Kind.ORDER, "fl:777")
    assert contact is not None
    assert contact.meta["bid_at"] == "2026-08-05T18:11:16+00:00"

    text = freelance.card(store, str(contact.pk)).text
    assert "Откликнулись: 05.08 21:11" in text, text


def test_the_tab_turns_red_when_the_session_is_lost(store, monkeypatch, tmp_path):
    """Слетевший вход — это стоящая площадка, а не «работает»."""
    from fbid.store import Store as FbidStore

    from orbit.sections import freelance

    exchange = FbidStore(tmp_path / "fbid.db")
    exchange.close()

    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FbidStore(tmp_path / "fbid.db"))
    monkeypatch.setattr(freelance, "_lost_sessions", lambda st: ["fl"])
    monkeypatch.setattr(freelance, "_fbid", lambda: (None, None, None))

    state = freelance.health(store)
    assert not state.ok
    assert "вход слетел" in state.summary
    assert any("fbid login fl" in line for line in state.details)


def test_the_tab_is_green_when_every_session_is_alive(store, monkeypatch, tmp_path):
    """А при живых входах вкладка зелёная, как и была."""
    from fbid.store import Store as FbidStore

    from orbit.sections import freelance

    exchange = FbidStore(tmp_path / "fbid.db")
    exchange.close()

    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FbidStore(tmp_path / "fbid.db"))
    monkeypatch.setattr(freelance, "_lost_sessions", lambda st: [])
    monkeypatch.setattr(freelance, "_fbid", lambda: (None, None, None))

    state = freelance.health(store)
    assert state.ok
    assert "работает" in state.summary


def test_the_marks_we_read_are_the_marks_the_exchange_writes():
    """Orbit читает чужие отметки по имени — имена обязаны совпадать.

    Опечатка или переименование на стороне биржи не вызовет ошибки: LIKE просто
    ничего не найдёт, и вкладки «Начали общение» и «Готовы к созвону» тихо
    перестанут наполняться. Спрашиваем имена у самой биржи, а не сверяем две
    строки в одном файле — иначе тест переживёт переименование вместе с багом.
    """
    from fbid.dialog import readycall_key, replied_key
    from fbid.models import Order, Source

    from orbit.sections import freelance

    order = Order(source=Source.PROFI, ext_id="1", url="https://profi.ru/1", title="Заказ")
    assert replied_key(order).startswith(f"{freelance.REPLIED_MARK}:")
    assert readycall_key(order).startswith(f"{freelance.READYCALL_MARK}:")


def test_one_platform_has_one_name_on_every_screen():
    """«fl», «FL» и «FL.ru» — это была одна площадка на трёх экранах.

    Имя берём у самой биржи: там оно уже есть и уже уходит владельцу в
    карточках откликов.
    """
    from fbid.notify import SOURCE_TITLES

    from orbit.sections import freelance

    assert freelance.platform_title("fl") == SOURCE_TITLES["fl"]
    assert freelance.platform_title("PROFI") == SOURCE_TITLES["profi"]
    # Незнакомая площадка не должна исчезать с экрана вовсе.
    assert freelance.platform_title("kwork") == "KWORK"
    assert freelance.platform_title("") == ""


def test_the_tab_is_red_when_a_platform_stops_answering(monkeypatch, store):
    """«Работает · сегодня 0 откликов» при мёртвой площадке — это ложь.

    5 сентября Profi замолчал на восемь суток из-за мёртвого прокси, и всё это
    время вкладка горела зелёным: заказов ноль, откликов ноль, и ни слова о том,
    что площадка вообще не отвечает.
    """
    import fbid.pipeline as pipeline

    from orbit.sections import freelance

    class FakeStore:
        def close(self):
            pass

        @staticmethod
        def orders_count():
            return 5475

        @staticmethod
        def spend_today(source):
            return 0, 0.0

    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FakeStore())
    monkeypatch.setattr(freelance, "_lost_sessions", lambda s: [])
    monkeypatch.setattr(pipeline, "down_sources", lambda s: ["profi"])
    monkeypatch.setattr("fbid.bidder.Bidder.is_paused", lambda self: False)

    state = freelance.health(store)
    assert state.ok is False
    assert "Profi.ru не отвечает" in state.summary
    assert any("check-proxy" in line for line in state.details)

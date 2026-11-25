"""Пакетная отправка: очередь, паузы и суточный лимит.

Двадцать писем, ушедших за одну секунду, — первый признак рассылки и для
получателя, и для спам-фильтра. Поэтому «Отправить все» ничего не отправляет
сразу: оно только ставит письма в очередь, а темп держит фоновый отправщик.
"""
from __future__ import annotations

import pytest

from orbit import config, outreach
from orbit.models import Channel, Contact, Draft, Kind, Stage
from orbit.sections import partners


@pytest.fixture()
def smtp_ready(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.yandex.ru")
    monkeypatch.setenv("SMTP_USER", "partnership@uniqore.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "секрет")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture()
def delivered(monkeypatch):
    """Перехватываем реальную отправку почты."""
    box: list[tuple] = []
    monkeypatch.setattr(
        outreach, "deliver_email", lambda to, subject, body: box.append((to, subject))
    )
    return box


@pytest.fixture(autouse=True)
def warmed_up(store):
    """Тесты про лимиты — не про прогрев: считаем, что ящик уже разогнан.

    Прогрев отдельно проверяется в test_outreach.py.
    """
    warm_up(store)


def _fill(store, count: int) -> None:
    for i in range(count):
        pk = store.upsert_contact(Contact(
            kind=Kind.PARTNER, key=f"agency{i}.ru", name=f"Агентство {i}",
            emails=[f"hi@agency{i}.ru"],
        ))
        store.save_draft(Draft(
            contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст письма."
        ))


# --- Постановка в очередь ---------------------------------------------------

def test_send_all_does_not_send_immediately(store, smtp_ready, delivered):
    _fill(store, 5)
    screen = partners.handle(store, "sendall", "")

    assert delivered == [], "письма не должны уходить пачкой в момент нажатия"
    assert store.approved_count(Kind.PARTNER) == 5
    assert "Поставил в отправку 5" in (screen.alert or "")


def test_queue_shows_what_is_in_flight(store, smtp_ready):
    _fill(store, 3)
    outreach.approve_queue(store, Kind.PARTNER)
    screen = partners.handle(store, "queue", "0")
    assert "Уже в отправке: 3" in screen.text


def test_approve_respects_daily_limit(store, smtp_ready):
    """Ставить в очередь больше суточного остатка бессмысленно."""
    _fill(store, 30)
    store.set_state(outreach.LIMIT_KEYS[Kind.PARTNER], "5")

    assert outreach.approve_queue(store, Kind.PARTNER) == 5
    assert store.approved_count(Kind.PARTNER) == 5


def test_already_sent_today_shrinks_the_batch(store, smtp_ready):
    _fill(store, 10)
    store.set_state(outreach.LIMIT_KEYS[Kind.PARTNER], "5")
    for _ in range(3):
        store.add_send(Kind.PARTNER)

    assert outreach.approve_queue(store, Kind.PARTNER) == 2


def test_exhausted_limit_says_so(store, smtp_ready):
    _fill(store, 3)
    for _ in range(outreach.tab_limit(store, Kind.PARTNER)):
        store.add_send(Kind.PARTNER)

    screen = partners.handle(store, "sendall", "")
    assert "лимит исчерпан" in (screen.alert or "").lower()
    assert store.approved_count(Kind.PARTNER) == 0


def test_approving_twice_does_not_double_count(store, smtp_ready):
    _fill(store, 3)
    outreach.approve_queue(store, Kind.PARTNER)
    assert outreach.approve_queue(store, Kind.PARTNER) == 0
    assert store.approved_count(Kind.PARTNER) == 3


# --- Отправка по одному -----------------------------------------------------

def test_send_next_takes_one_letter(store, smtp_ready, delivered):
    _fill(store, 3)
    outreach.approve_queue(store, Kind.PARTNER)

    result = outreach.send_next(store, Kind.PARTNER)

    assert result.ok
    assert len(delivered) == 1
    assert store.approved_count(Kind.PARTNER) == 2
    assert store.sent_today(Kind.PARTNER) == 1


def test_send_next_on_empty_queue(store, smtp_ready):
    assert outreach.send_next(store, Kind.PARTNER) is None


def test_letters_go_in_order_of_approval(store, smtp_ready, delivered):
    _fill(store, 3)
    outreach.approve_queue(store, Kind.PARTNER)

    for _ in range(3):
        outreach.send_next(store, Kind.PARTNER)

    assert [to for to, _subject in delivered] == [
        "hi@agency0.ru", "hi@agency1.ru", "hi@agency2.ru"
    ]


def test_failed_letter_leaves_the_flight(store, smtp_ready, monkeypatch):
    """Не ушло — письмо возвращается в очередь, а не долбится каждые полминуты."""
    def boom(*_args):
        raise RuntimeError("сервер отверг пароль")

    monkeypatch.setattr(outreach, "deliver_email", boom)
    _fill(store, 1)
    outreach.approve_queue(store, Kind.PARTNER)

    result = outreach.send_next(store, Kind.PARTNER)

    assert not result.ok
    assert store.approved_count(Kind.PARTNER) == 0
    assert store.queue_size(Kind.PARTNER) == 1


def test_letter_to_closed_contact_is_dropped_from_flight(store, smtp_ready, delivered):
    """Контакт закрыли, пока письмо ждало очереди.

    Письмо снимается сразу же, вместе с закрытием: закрытому контакту оно всё
    равно не уйдёт, а лёжа в очереди оно попадало в счётчик «Готовы к
    отправке» и в цифру на кнопке «Отправить все» — кнопка обещала работу,
    которой нет, а владелец видел в очереди компанию, которую сам же закрыл.
    """
    _fill(store, 1)
    outreach.approve_queue(store, Kind.PARTNER)
    contact = store.contacts(Kind.PARTNER, limit=1)[0]
    store.set_stage(contact.pk, Stage.DEAD)

    assert store.draft_for(contact.pk) is None, "письмо обязано уйти из очереди"
    assert store.queue_size(Kind.PARTNER) == 0
    assert outreach.send_next(store, Kind.PARTNER) is None, "отправлять нечего"
    assert delivered == []


def test_without_mail_nothing_leaves(store, delivered):
    _fill(store, 2)
    outreach.approve_queue(store, Kind.PARTNER)

    result = outreach.send_next(store, Kind.PARTNER)

    assert not result.ok
    assert "почта не настроена" in result.reason
    assert delivered == []


# --- Темп -------------------------------------------------------------------

def test_pause_is_long_enough_to_look_human():
    """Минута между письмами — нижняя граница, при которой это не выглядит ботом."""
    low, high = outreach.PAUSE_RANGE
    assert low >= 60.0
    assert high > low, "разброс нужен: ровный интервал выдаёт машину не хуже пачки"


# --- Телеграм не должен затыкать почту ------------------------------------------


def test_telegram_drafts_do_not_block_email(store):
    """Бот не может отправить телеграм сам, и такие черновики копятся в голове.

    Отбор «взять пачку и отфильтровать» упирался в них: окно целиком уходило
    на телеграм, и ни одно письмо не вставало в очередь на отправку.
    """
    from orbit.models import Channel, Contact, Draft, Kind

    for number in range(30):
        contact = Contact(kind=Kind.PARTNER, key=f"tg{number}", name=f"Тг {number}",
                          telegram=["https://t.me/some_chat"])
        pk = store.upsert_contact(contact)
        store.save_draft(Draft(contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Текст"))

    contact = Contact(kind=Kind.PARTNER, key="mail", name="Почта", emails=["info@a.ru"])
    pk = store.upsert_contact(contact)
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))

    approved = outreach.approve_queue(store, Kind.PARTNER, channel=Channel.EMAIL)
    assert approved == 1
    assert store.draft_for(pk).approved


def test_queue_can_be_asked_for_one_channel(store):
    from orbit.models import Channel, Contact, Draft, Kind

    for key, channel in (("a", Channel.EMAIL), ("b", Channel.TELEGRAM)):
        contact = Contact(kind=Kind.PARTNER, key=key, name=key,
                          emails=["info@a.ru"], telegram=["https://t.me/chat_here"])
        pk = store.upsert_contact(contact)
        store.save_draft(Draft(contact_pk=pk, channel=channel, subject="", body="Текст"))

    only_mail = store.queue(Kind.PARTNER, channel=Channel.EMAIL)
    assert [d.channel for d, _ in only_mail] == [Channel.EMAIL]
    assert len(store.queue(Kind.PARTNER)) == 2


# --- Двое отправляют одно письмо ------------------------------------------------


def test_draft_is_claimed_before_sending(store, monkeypatch):
    """Отправляют двое: фоновый отправщик и владелец кнопкой.

    Между проверками и отметкой «отправлено» живут запрос в DNS и разговор
    с почтовым сервером — несколько секунд. Оба успевали войти в это окно,
    и одно письмо уходило дважды.
    """
    from orbit import config
    from orbit.models import Contact, Draft

    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "sales@uniqore.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "секрет")
    config.get_settings.cache_clear()
    try:
        pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key="k", name="К",
                                          emails=["info@a.ru"]))
        store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
        draft_pk = store.draft_for(pk).pk

        sent: list[str] = []

        def deliver(to, *args, **kwargs):
            # Пока первый «разговаривает с сервером», второй пытается влезть.
            second = outreach.send(store, draft_pk)
            assert not second.ok
            assert "уже отправляет" in second.reason
            sent.append(to)

        monkeypatch.setattr(outreach, "deliver_email", deliver)
        assert outreach.send(store, draft_pk).ok
        assert sent == ["info@a.ru"]
        assert store.sent_today(Kind.PARTNER) == 1
    finally:
        config.get_settings.cache_clear()


def test_failed_send_returns_the_draft_to_the_queue(store, monkeypatch):
    """Захват не должен пережить неудачу: письмо обязано остаться в очереди."""
    from orbit import config
    from orbit.models import Contact, Draft

    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "sales@uniqore.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "секрет")
    config.get_settings.cache_clear()
    try:
        pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key="k", name="К",
                                          emails=["info@a.ru"]))
        store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
        draft_pk = store.draft_for(pk).pk

        def refuse(*args, **kwargs):
            raise RuntimeError("сеть отвалилась")

        monkeypatch.setattr(outreach, "deliver_email", refuse)
        assert not outreach.send(store, draft_pk).ok
        assert store.get_draft(draft_pk).status == "draft"
    finally:
        config.get_settings.cache_clear()


def test_stale_claim_is_released_on_restart(store):
    """Процесс умер посреди отправки — письмо не должно застрять навсегда."""
    from orbit.models import Contact, Draft

    pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key="k", name="К",
                                      emails=["info@a.ru"]))
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Тема", body="Текст"))
    draft_pk = store.draft_for(pk).pk

    assert store.claim_draft(draft_pk)
    assert store.draft_for(pk) is None          # из очереди пропал
    assert store.release_stale_sending()[0] == 1
    assert store.draft_for(pk) is not None      # и вернулся


def test_rewritten_letter_does_not_break_the_release(store):
    """Владелец нажал «Переписать», пока отправщик держал старое письмо.

    На живой черновик стоит уникальный индекс — один на контакт. Возврат
    занятого письма поверх нового падал с IntegrityError, письмо навсегда
    оставалось «отправляемым», а причина отказа терялась.
    """
    from orbit.models import Contact, Draft

    pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key="k", name="К",
                                      emails=["info@a.ru"]))
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="первое", body="текст"))
    first = store.draft_for(pk).pk
    assert store.claim_draft(first)

    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="второе", body="текст"))
    store.release_draft(first)                       # раньше здесь падало

    assert store.get_draft(first).status == "failed"
    assert "переписали" in (store.get_draft(first).error or "")
    assert store.draft_for(pk).subject == "второе"   # в очереди свежий текст


def test_release_returns_the_draft_when_nothing_replaced_it(store):
    from orbit.models import Contact, Draft

    pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key="k", name="К",
                                      emails=["info@a.ru"]))
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="одно", body="текст"))
    draft_pk = store.draft_for(pk).pk
    assert store.claim_draft(draft_pk)
    store.release_draft(draft_pk)
    assert store.draft_for(pk).pk == draft_pk


def test_stale_release_survives_a_rewritten_letter(store):
    """Перезапуск после падения не должен ронять весь возврат из-за одного письма."""
    from orbit.models import Contact, Draft

    for number in range(3):
        pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key=f"k{number}",
                                          name=f"К{number}", emails=["info@a.ru"]))
        store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="s", body="текст"))
        store.claim_draft(store.conn.execute(
            "SELECT pk FROM drafts WHERE contact_pk=? ORDER BY pk DESC LIMIT 1", (pk,)
        ).fetchone()["pk"])
        if number == 1:                              # одному из них написали новое письмо
            store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL,
                                   subject="новое", body="текст"))

    assert store.release_stale_sending()[0] == 3
    assert store.conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE status='sending'").fetchone()[0] == 0


def test_cli_send_keeps_the_pace(store, monkeypatch):
    """Команда отправки очереди обязана держать тот же темп, что фоновый отправщик.

    Без паузы она выпускала всю очередь за секунду. Суточный лимит от этого не
    спасает: он про количество, а не про темп, а двадцать писем, ушедших разом,
    и есть первый признак рассылки.
    """
    from orbit import cli, config
    from orbit.models import Contact, Draft

    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_USER", "sales@uniqore.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "секрет")
    config.get_settings.cache_clear()
    try:
        for number in range(3):
            pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key=f"k{number}",
                                              name=f"К{number}", emails=[f"info@a{number}.ru"]))
            store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL,
                                   subject="Тема", body="Текст"))

        pauses: list[float] = []
        monkeypatch.setattr(cli, "_store", lambda: store)
        monkeypatch.setattr(store, "close", lambda: None)
        monkeypatch.setattr(outreach, "deliver_email", lambda *a, **kw: None)
        monkeypatch.setattr("time.sleep", lambda s: pauses.append(s))

        from typer.testing import CliRunner

        result = CliRunner().invoke(cli.app, ["send", "partner", "--yes"])
        assert result.exit_code == 0, result.output
        # Три письма — две паузы между ними, перед первым ждать нечего.
        assert len(pauses) == 2
        assert all(min(outreach.PAUSE_RANGE) <= p <= max(outreach.PAUSE_RANGE) for p in pauses)
    finally:
        config.get_settings.cache_clear()


def test_cli_dry_run_does_not_pause(store, monkeypatch):
    """Проверка без --yes ничего не шлёт, значит и ждать нечего."""
    from orbit import cli
    from orbit.models import Contact, Draft

    pk = store.upsert_contact(Contact(kind=Kind.PARTNER, key="k", name="К",
                                      emails=["info@a.ru"]))
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL, subject="Т", body="Текст"))

    pauses: list[float] = []
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(store, "close", lambda: None)
    monkeypatch.setattr("time.sleep", lambda s: pauses.append(s))

    from typer.testing import CliRunner

    CliRunner().invoke(cli.app, ["send", "partner"])
    assert pauses == []


# --- Что успевает случиться, пока модель пишет письмо -------------------------


def test_contact_closed_while_the_model_was_writing_is_not_queued(store, monkeypatch):
    """Владелец нажал «Не писать», пока модель писала письмо.

    Проверка стадии стоит ДО обращения к модели, а модель отвечает по десять
    секунд. Ровно в это окно владелец и читает карточку и закрывает контакт.
    Письмо всё равно ложилось в очередь, а `set_stage(QUEUED)` возвращал
    закрытый контакт в работу — и отправщик писал человеку, которого владелец
    только что закрыл руками.
    """
    from orbit import llm
    from orbit.models import Contact, Kind, Stage
    from orbit.sections import base

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="closed.ru", name="Улыбка", emails=["info@closed.ru"]
    ))

    def slow_model(contact, channel=None):
        # Пока «модель думает», владелец закрывает контакт кнопкой.
        store.set_stage(pk, Stage.DEAD, "не писать")
        return llm.Letter("пишем", "", "Тема", "Текст письма.")

    monkeypatch.setattr(llm, "write_letter", slow_model)

    ok, reason = base.write_for(store, store.get_contact(pk))
    assert not ok, reason
    assert store.draft_for(pk) is None, "закрытому контакту письмо в очередь не кладём"
    assert store.get_contact(pk).stage is Stage.DEAD, "стадию обратно поднимать нельзя"


def test_contact_that_replied_while_the_model_was_writing_is_not_queued(store, monkeypatch):
    """То же самое, но контакт успел ответить: холодное письмо здесь лишнее."""
    from orbit import llm
    from orbit.models import Contact, Kind, Stage
    from orbit.sections import base

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="replied.ru", name="Улыбка", emails=["info@replied.ru"]
    ))

    def slow_model(contact, channel=None):
        store.set_stage(pk, Stage.REPLIED, "ответили на письмо")
        return llm.Letter("пишем", "", "Тема", "Текст письма.")

    monkeypatch.setattr(llm, "write_letter", slow_model)

    ok, _ = base.write_for(store, store.get_contact(pk))
    assert not ok
    assert store.draft_for(pk) is None
    assert store.get_contact(pk).stage is Stage.REPLIED


def test_writer_lock_is_released_when_the_database_will_not_open(monkeypatch):
    """База не открылась — кнопка «Подготовить письма» обязана остаться живой.

    Связь с базой открывалась ДО try/finally: отказ на ней уносил поток вместе
    с замком, и кнопка до перезапуска бота молча отвечала «уже готовлю».
    """
    import threading

    from orbit.models import Contact, Kind
    from orbit.sections import base

    said: list[str] = []
    monkeypatch.setattr(base, "Store", lambda path: (_ for _ in ()).throw(OSError("диск занят")))
    done = threading.Event()
    monkeypatch.setattr(base, "_tell_batch_died", lambda exc: (said.append(str(exc)), done.set()))

    contact = Contact(kind=Kind.CLIENT, key="a.ru", name="А", emails=["a@a.ru"])
    assert base._spawn_writer("/nowhere/db.sqlite", [contact])
    assert done.wait(timeout=5), "поток обязан пожаловаться, а не умереть молча"
    assert said == ["диск занят"], "владельцу надо сказать, что пачка не пошла"
    # Замок снимается в finally, то есть на волос позже жалобы, — ждём его.
    assert base._writer_lock.acquire(timeout=5), "замок обязан сняться и после падения"
    base._writer_lock.release()


def test_send_all_button_counts_only_what_the_press_will_move(store):
    """Цифра на «Отправить все (N)» — это N писем, которые сдвинутся.

    Одобренное письмо лежит в том же статусе 'draft', пока его не заберёт
    отправщик, и попадало в общий счёт: кнопка обещала «Отправить все (3)»
    при трёх уже стоящих в отправке, а нажатие отвечало «Отправлять нечего».
    """
    from orbit.models import Channel, Contact, Draft, Kind
    from orbit.sections import base
    from orbit.sections.clients import TAB

    pks = []
    for n in range(3):
        pk = store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"c{n}.ru", name=f"Компания {n}", emails=[f"a@c{n}.ru"]
        ))
        store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL,
                               subject="Тема", body="Текст"))
        pks.append(pk)

    screen = base.queue(TAB, store, "0")
    labels = [label for row in screen.rows for label, _ in row]
    assert any("Отправить все (3)" in label for label in labels)

    # Два письма уже стоят в отправке — нажатие сдвинет ровно одно.
    for pk in pks[:2]:
        store.approve_draft(store.draft_for(pk).pk)

    screen = base.queue(TAB, store, "0")
    labels = [label for row in screen.rows for label, _ in row]
    assert any("Отправить все (1)" in label for label in labels), labels
    assert not any("Отправить все (3)" in label for label in labels)

    # А когда в отправке уже всё — кнопки быть не должно вовсе.
    store.approve_draft(store.draft_for(pks[2]).pk)
    screen = base.queue(TAB, store, "0")
    labels = [label for row in screen.rows for label, _ in row]
    assert not any("Отправить все" in label for label in labels), labels


def test_closing_a_network_neighbour_clears_its_queued_letter(store, smtp_ready):
    """Возврат по общему адресу закрыл соседа по сети — его письмо тоже уходит.

    Стадию закрывали, а готовое письмо оставалось в очереди: попадало в
    счётчик «Готовы к отправке», в цифру на кнопке «Отправить все» и каждый
    заход отправщика получало отметку об отказе.
    """
    from orbit.models import Channel, Contact, Draft, Kind, Stage

    pks = []
    for n in range(2):
        pk = store.upsert_contact(Contact(
            kind=Kind.CLIENT, key=f"branch{n}.ru", name=f"Медси, филиал {n}",
            emails=["info@medsi.ru"],
        ))
        store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL,
                               subject="Тема", body="Текст"))
        store.set_stage(pk, Stage.QUEUED, "текст готов")
        pks.append(pk)

    assert store.queue_size(Kind.CLIENT) == 2

    closed = store.close_same_email("info@medsi.ru", note="письмо вернулось")
    assert closed == 2

    assert store.queue_size(Kind.CLIENT) == 0, "письма закрытых контактов в очереди не место"
    for pk in pks:
        assert store.get_contact(pk).stage is Stage.DEAD
        assert store.draft_for(pk) is None


def test_a_manual_telegram_draft_survives_the_closing(store):
    """Телеграм-черновик остаётся: кнопка «Отправил» записывает свершившийся факт."""
    from orbit.models import Channel, Contact, Draft, Kind, Stage

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="tg.ru", name="Улыбка", telegram=["https://t.me/ulybka"]
    ))
    draft_pk = store.save_draft(Draft(
        contact_pk=pk, channel=Channel.TELEGRAM, subject="", body="Текст"
    ))
    store.set_stage(pk, Stage.DEAD, "просил не писать")

    assert store.get_draft(draft_pk).status == "draft", (
        "иначе владелец не сможет записать сообщение, которое уже отправил руками"
    )


def test_a_second_press_does_not_pay_for_a_second_letter(store, monkeypatch):
    """Второе нажатие «Написать письмо» не должно заказывать второй текст.

    Модель отвечает по десять-двадцать секунд, и всё это время бот молчит.
    Владелец жмёт кнопку второй раз — нажатие ЖДЁТ в очереди обновлений, а
    потом заказывает у модели ВТОРОЙ текст по тому же контакту. Оба стоят
    денег, оба отвечают «Письмо готово», а в очередь ложится только последний.
    """
    from orbit import llm
    from orbit.models import Contact, Kind
    from orbit.sections import base
    from orbit.sections.clients import TAB

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a.ru", name="Улыбка", emails=["info@a.ru"]
    ))
    calls: list[int] = []
    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact, channel=None: calls.append(1) or llm.Letter("пишем", "", "Т", "Текст."),
    )

    # Письмо по этому контакту уже пишется — второе нажатие пришло, пока
    # первое ещё у модели.
    base._writing_now.add(pk)
    try:
        screen = base.generate_one(TAB, store, str(pk))
    finally:
        base._writing_now.discard(pk)

    assert "уже пишется" in (screen.alert or ""), screen.alert
    assert calls == [], "второй платный вызов модели делать не за что"
    assert store.draft_for(pk) is None


def test_the_guard_is_released_after_the_letter(store, monkeypatch):
    """Замок снимается: следующее нажатие обязано работать."""
    from orbit import llm
    from orbit.models import Contact, Kind
    from orbit.sections import base
    from orbit.sections.clients import TAB

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="b.ru", name="Улыбка", emails=["info@b.ru"]
    ))
    monkeypatch.setattr(
        llm, "write_letter",
        lambda contact, channel=None: llm.Letter("пишем", "", "Тема", "Текст письма."),
    )

    assert base.generate_one(TAB, store, str(pk)).alert == "Письмо готово"
    assert base.generate_one(TAB, store, str(pk)).alert == "Письмо готово"
    assert pk not in base._writing_now


def test_pressing_send_all_twice_does_not_double_the_daily_norm(store, smtp_ready):
    """Нажатие «Отправить все» не должно одобрять сверх суточной нормы.

    Уже одобренные письма не считались, и каждое нажатие одобряло ещё столько
    же: три нажатия давали трёхкратную норму. Потом кнопка переставала делать
    что-либо — свободных писем в очереди уже не было.
    """
    from orbit.models import Channel, Contact, Draft, Kind

    norm = outreach.letters_left_today(store, Kind.PARTNER)
    for n in range(norm * 3):
        pk = store.upsert_contact(Contact(
            kind=Kind.PARTNER, key=f"a{n}.ru", name=f"Агентство {n}",
            emails=[f"info@a{n}.ru"],
        ))
        store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL,
                               subject="Тема", body="Текст"))

    first = outreach.approve_queue(store, Kind.PARTNER, channel=Channel.EMAIL)
    assert first == norm

    second = outreach.approve_queue(store, Kind.PARTNER, channel=Channel.EMAIL)
    assert second == 0, "второе нажатие одобрять уже нечего"
    assert store.approved_count(Kind.PARTNER) == norm, "норма на сутки одна"


def test_an_unclear_letter_frees_the_contact(store):
    """Письмо с неизвестной судьбой уходит из очереди — контакт не должен застрять.

    Компания оставалась в «Готовы к отправке» без единого письма: в очереди
    её нет, а пачка берёт только «новых» — она выпадала из работы совсем и
    молча.
    """
    from orbit.models import Channel, Contact, Draft, Kind, Stage

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a.ru", name="Улыбка", emails=["info@a.ru"]
    ))
    draft_pk = store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL,
                                      subject="Тема", body="Текст"))
    store.set_stage(pk, Stage.QUEUED, "текст готов")

    store.mark_unclear(draft_pk)

    assert store.draft_for(pk) is None
    assert store.get_contact(pk).stage is Stage.NEW
    assert store.unclear_drafts() == 1


def test_a_network_bounce_frees_a_telegram_only_contact(store):
    """Почты не осталось, телеграм жив — письмо в никуда убираем.

    Компания висела в «Готовы к отправке» с письмом на вычеркнутый адрес, и
    пачка не могла взять её, чтобы подготовить телеграм.
    """
    from orbit.models import Channel, Contact, Draft, Kind, Stage

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="medsi.ru", name="Медси",
        emails=["info@medsi.ru"], telegram=["https://t.me/medsi"],
    ))
    store.save_draft(Draft(contact_pk=pk, channel=Channel.EMAIL,
                           subject="Тема", body="Текст"))
    store.set_stage(pk, Stage.QUEUED, "текст готов")

    store.close_same_email("info@medsi.ru", note="письмо вернулось")

    contact = store.get_contact(pk)
    assert contact.stage is Stage.NEW, "иначе пачка его больше не возьмёт"
    assert contact.reachable, "телеграм жив — компанию не теряем"
    assert store.draft_for(pk) is None


def test_the_batch_and_the_button_do_not_pay_twice_for_one_contact(store, monkeypatch):
    """Пачка и кнопка «Написать письмо» встречаются на одном контакте.

    Пока модель отвечает (десять-двадцать секунд), они успевают сойтись, и за
    один и тот же текст мы платим дважды, а в очередь ложится только
    последний. Замок в обработчике кнопки от пачки не спасал: у неё свой
    поток и свой вызов write_for.
    """
    from orbit import llm
    from orbit.models import Contact, Kind
    from orbit.sections import base

    pk = store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a.ru", name="Улыбка", emails=["info@a.ru"]
    ))
    contact = store.get_contact(pk)
    calls: list[int] = []
    monkeypatch.setattr(
        llm, "write_letter",
        lambda c, channel=None: calls.append(1) or llm.Letter("пишем", "", "Т", "Текст."),
    )

    # Кнопка уже пишет это письмо — пачка не должна платить второй раз.
    base._writing_now.add(pk)
    try:
        ok, reason = base.write_for(store, contact)
    finally:
        base._writing_now.discard(pk)

    assert not ok
    assert "уже пишется" in reason
    assert calls == []


def test_a_busy_batch_says_which_tab_holds_it(store, monkeypatch):
    """Замок один на обе вкладки — и он обязан назвать себя.

    Владелец нажимал «Подготовить письма» у клиентов, получал «уже готовлю» и
    шёл искать письма в клиентской очереди, а писалась в это время пачка
    партнёров.
    """
    from orbit.models import Contact, Kind
    from orbit.sections import base
    from orbit.sections.clients import TAB

    store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a.ru", name="Улыбка", emails=["info@a.ru"]
    ))
    monkeypatch.setattr(base, "_spawn_writer", lambda *a, **kw: False)
    base._writing_tab[:] = ["Партнёры"]
    try:
        screen = base.prepare(TAB, store)
    finally:
        base._writing_tab.clear()

    assert "Партнёры" in (screen.alert or ""), screen.alert
    assert "счёт за модель общий" in screen.alert


def test_a_busy_batch_on_the_same_tab_says_the_usual_thing(store, monkeypatch):
    """А своя же пачка — обычное «уже готовлю»."""
    from orbit.models import Contact, Kind
    from orbit.sections import base
    from orbit.sections.clients import TAB

    store.upsert_contact(Contact(
        kind=Kind.CLIENT, key="a.ru", name="Улыбка", emails=["info@a.ru"]
    ))
    monkeypatch.setattr(base, "_spawn_writer", lambda *a, **kw: False)
    base._writing_tab[:] = [TAB.title]
    try:
        screen = base.prepare(TAB, store)
    finally:
        base._writing_tab.clear()

    assert "Уже готовлю письма" in (screen.alert or ""), screen.alert


def warm_up(store, days: int = 6) -> None:
    """Делает вид, что ящик уже разогнан: столько-то дней подряд уходили письма.

    Прогрев считается по дням РЕАЛЬНЫХ отправок, а не по календарю (см.
    outreach.warmup_limit), поэтому и в тестах его надо набирать отправками.
    """
    from datetime import date, timedelta

    from orbit.store import today_msk

    today = date.fromisoformat(today_msk())
    for back in range(days, 0, -1):
        day = (today - timedelta(days=back)).isoformat()
        store.conn.execute(
            "INSERT OR REPLACE INTO sends (day, kind, count, by_mail) VALUES (?, 'partner', 1, 1)",
            (day,),
        )
    store.conn.commit()

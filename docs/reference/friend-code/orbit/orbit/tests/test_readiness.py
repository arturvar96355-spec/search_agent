"""Экран готовности: что настроено, а что ждёт человека.

Главное правило этого экрана — не ставить зелёную галочку на веру. «Не смогли
проверить» и «проверили, всё хорошо» обязаны выглядеть по-разному: первое
приглашает посмотреть глазами, второе закрывает вопрос. Тесты держат именно
эту границу.
"""
from __future__ import annotations

from orbit import readiness


def test_an_unknown_answer_is_not_a_green_mark():
    assert readiness.Check("что-то", None).mark == "⚪️"
    assert readiness.Check("что-то", True).mark == "✅"
    assert readiness.Check("что-то", False).mark == "🔴"


def test_dmarc_missing_is_a_red_line(monkeypatch):
    """Без DMARC письма чаще уходят в спам, а по нашей стороне это не видно.

    Отправка проходит успешно, отчёта о недоставке нет — просто не приходит
    ответов. Поэтому строка красная, а не «не проверяли».
    """
    monkeypatch.setattr(
        readiness, "_txt",
        lambda name: ["v=spf1 redirect=beget.com"] if name == "firma.ru" else [],
    )
    monkeypatch.setattr(
        readiness, "get_settings", _settings, raising=False,
    )

    checks = {check.title: check for check in readiness.mail_checks()}
    assert checks["SPF у firma.ru"].ok is True
    assert checks["DMARC у firma.ru"].ok is False
    # DKIM без найденного селектора — именно «не знаем»: селектор придумывает
    # хостинг, и наш список общих имён мог его просто не угадать.
    assert checks["DKIM у firma.ru"].ok is None
    # Пока DMARC нет, подпись важна — просим человека.
    assert checks["DKIM у firma.ru"].who == "вы"


def test_without_dkim_but_with_dmarc_nobody_is_bothered(monkeypatch):
    """DMARC проходит, если совпал ХОТЬ ОДИН из двух: SPF или DKIM.

    У нас совпадает SPF, значит почта проверку проходит и без подписи. Красить
    отсутствие DKIM красным и звать человека здесь — врать: без него всё едет.
    """
    def txt(name):
        if name == "firma.ru":
            return ["v=spf1 redirect=beget.com"]
        if name == "_dmarc.firma.ru":
            return ["v=DMARC1; p=none; rua=mailto:info@firma.ru"]
        return []

    monkeypatch.setattr(readiness, "_txt", txt)
    monkeypatch.setattr(readiness, "get_settings", _settings, raising=False)

    dkim = {check.title: check for check in readiness.mail_checks()}["DKIM у firma.ru"]
    assert dkim.ok is None
    assert dkim.who == "", "без человека всё работает — звать его незачем"
    assert "DMARC проходит по SPF" in dkim.note


def test_dkim_found_by_selector(monkeypatch):
    monkeypatch.setattr(
        readiness, "_txt",
        lambda name: ["v=DKIM1; k=rsa; p=MIGf"] if name.startswith("beget._domainkey") else [],
    )
    monkeypatch.setattr(readiness, "get_settings", _settings, raising=False)

    checks = {check.title: check for check in readiness.mail_checks()}
    assert checks["DKIM у firma.ru"].ok is True
    assert "beget" in checks["DKIM у firma.ru"].note


def test_what_waits_for_the_owner_is_listed():
    groups = [
        ("Почта", [readiness.Check("SMTP", True), readiness.Check("DMARC", False, who="вы")]),
        ("Модель", [readiness.Check("Модель", False, "кончились деньги", who="вы")]),
    ]
    waiting = readiness.waiting_for_you(groups)
    assert [check.title for check in waiting] == ["DMARC", "Модель"]


def _settings():
    class Fake:
        smtp_ready = True
        imap_ready = True
        smtp_user = "info@firma.ru"

    return Fake()


def test_money_is_told_in_bids_not_in_roubles(monkeypatch):
    """«170 ₽» ни о чём не говорит: отклик на Profi стоит от ста до четырёхсот.

    Владельцу важно не число на счёте, а сколько откликов оно покроет. И если
    меньше трёх — это не «есть деньги», а «сегодня закончится посреди дня».
    """
    from fbid.models import Source

    class FakeStore:
        @staticmethod
        def average_bid_cost(source, last=10):
            return 170.0

    poor = readiness._balance_check(FakeStore(), Source.PROFI, "Profi.ru", 170, "₽")
    assert poor.ok is False
    assert "примерно 1 отклик" in poor.note, poor.note
    assert poor.who == "вы"

    rich = readiness._balance_check(FakeStore(), Source.PROFI, "Profi.ru", 2000, "₽")
    assert rich.ok is True
    assert "примерно 11 откликов" in rich.note


def test_an_empty_balance_is_red():
    from fbid.models import Source

    class FakeStore:
        @staticmethod
        def average_bid_cost(source, last=10):
            return None

    check = readiness._balance_check(FakeStore(), Source.FL, "FL.ru", 0, "откликов в пакете")
    assert check.ok is False
    assert check.who == "вы"


def test_the_model_is_asked_right_now(monkeypatch, tmp_path):
    """Экран готовности спрашивает модель заново, а не берёт вчерашнюю запись.

    Записи расписания бывает до получаса. Именно на этом экране такая давность
    недопустима: его открывают, когда что-то не так, — и он показывал «модель
    молчит» через сорок минут после того, как связь вернулась.
    """
    from orbit import llm
    from orbit.schedule import MODEL_STATE
    from orbit.store import Store

    store = Store(tmp_path / "t.db")
    try:
        store.set_state(MODEL_STATE, "кончились деньги на счёте модели")
        monkeypatch.setattr(
            readiness, "get_settings",
            lambda: type("S", (), {"anthropic_api_key": "sk-ant-x"})(), raising=False,
        )
        monkeypatch.setattr(llm, "ping", lambda: (True, ""))

        check = readiness.model_checks(store)[0]
        assert check.ok is True
        assert store.get_state(MODEL_STATE) is None, "устаревшую запись надо снять"

        monkeypatch.setattr(llm, "ping", lambda: (False, "кончились деньги на счёте модели"))
        check = readiness.model_checks(store)[0]
        assert check.ok is False
        assert store.get_state(MODEL_STATE) == "кончились деньги на счёте модели"
    finally:
        store.close()


def test_a_platform_that_stopped_answering_is_visible(monkeypatch, tmp_path):
    """«Вход слетел» и «площадка не отвечает» — разные беды с разным лечением.

    Вторая обычно означает мёртвый прокси: молчат сразу все браузерные проходы,
    и восемь суток это выглядело как «на площадке тихо».
    """
    import fbid.pipeline as pipeline

    monkeypatch.setattr(pipeline, "down_sources", lambda store: ["profi"])
    monkeypatch.setattr(pipeline, "lost_sessions", lambda store: [])
    monkeypatch.setattr(pipeline, "known_balance", lambda store, source: (500.0, ""))

    from orbit.sections import freelance

    class FakeStore:
        def close(self):
            pass

        @staticmethod
        def get_state(key, default=None):
            return None

    monkeypatch.setattr(freelance, "_open_fbid_store", lambda: FakeStore())
    monkeypatch.setattr("fbid.bidder.paused", lambda store: False)

    checks = readiness.exchange_checks()
    broken = [c for c in checks if "Связь с" in c.title]
    assert broken and broken[0].ok is False
    assert "check-proxy" in broken[0].note
    assert broken[0].who == "вы"

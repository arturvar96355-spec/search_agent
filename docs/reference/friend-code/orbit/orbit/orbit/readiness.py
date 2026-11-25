"""Готовность к работе: что настроено, а что ждёт человека.

Экран, который открывают перед тем, как включить систему в дело, и после
каждой правки в .env. Он отвечает на один вопрос: поедет ли всё прямо сейчас,
а если нет — что именно сделать и кому.

Проверки только настоящие. Ни одна строка не говорит «готово» по факту
наличия настройки: ключ бывает задан и пуст, домен бывает без подписи,
сессия бывает записана и слетевшей. Где проверить нельзя без денег или без
браузера, так и написано — «не проверял», а не «всё хорошо». Разница здесь
принципиальная: зелёная галочка, поставленная на веру, дороже красной.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from loguru import logger

from .config import get_settings

# Сколько ждём DNS. Записи домена читаются при каждом открытии экрана, и
# долгое ожидание превратило бы его в «бот завис».
DNS_TIMEOUT = 4.0

# Селекторы DKIM, которые ставят почтовые хостинги по умолчанию. Своего имени
# селектора мы не знаем — оно задаётся при выпуске ключа, — поэтому проверяем
# распространённые. Не нашли ни одного: скорее всего подписи нет вовсе, но
# утверждать это наверняка нельзя, и строка так и говорит.
DKIM_SELECTORS = (
    "default", "mail", "dkim", "beget", "selector1", "selector2",
    "google", "mx", "key1", "s1",
)


@dataclass(frozen=True)
class Check:
    """Одна строка готовности."""

    title: str
    ok: bool | None          # None — проверить не смогли
    note: str = ""           # что сделать или что выяснилось
    who: str = ""            # «вы» — без человека не поедет

    @property
    def mark(self) -> str:
        if self.ok is None:
            return "⚪️"
        return "✅" if self.ok else "🔴"


def _txt(name: str) -> list[str]:
    """TXT-записи домена. Пусто — записи нет или DNS не ответил."""
    try:
        import dns.resolver
    except ImportError:
        return []
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT
    resolver.lifetime = DNS_TIMEOUT
    try:
        answer = resolver.resolve(name, "TXT")
    except Exception:  # noqa: BLE001 — нет записи или молчит DNS: обе новости здесь одинаковы
        return []
    out = []
    for row in answer:
        try:
            out.append(b"".join(row.strings).decode(errors="replace"))
        except Exception:  # noqa: BLE001 — кривая запись не должна ронять экран
            continue
    return out


def mail_checks() -> list[Check]:
    """Почта: доступ к ящику и подписи домена.

    Подписи важнее, чем кажется. Письмо без DKIM и DMARC уходит в спам у
    заметной части ящиков, и понять это по нашей стороне невозможно: отправка
    проходит успешно, отчёта о доставке нет, ответов просто не приходит.
    """
    settings = get_settings()
    checks = [
        Check(
            "Доступ к ящику (SMTP)",
            bool(settings.smtp_ready),
            "" if settings.smtp_ready else "впишите SMTP_HOST, SMTP_USER, SMTP_PASSWORD в orbit/.env",
            who="" if settings.smtp_ready else "вы",
        ),
        Check(
            "Чтение входящих (IMAP)",
            bool(getattr(settings, "imap_ready", False)),
            "" if getattr(settings, "imap_ready", False)
            else "без него ответы клиентов и возвраты не разбираются",
            who="" if getattr(settings, "imap_ready", False) else "вы",
        ),
    ]

    domain = (settings.smtp_user or "").split("@")[-1].strip().lower()
    if not domain:
        checks.append(Check("Подписи домена", None, "почта не настроена — проверять нечего"))
        return checks

    spf = [row for row in _txt(domain) if row.lower().startswith("v=spf1")]
    checks.append(Check(
        f"SPF у {domain}",
        bool(spf),
        spf[0][:70] if spf else "добавьте TXT-запись v=spf1 у регистратора домена",
        who="" if spf else "вы",
    ))

    dmarc = [row for row in _txt(f"_dmarc.{domain}") if row.lower().startswith("v=dmarc1")]
    checks.append(Check(
        f"DMARC у {domain}",
        bool(dmarc),
        dmarc[0][:70] if dmarc
        else "TXT-запись _dmarc: v=DMARC1; p=none; rua=mailto:… — без неё письма чаще уходят в спам",
        who="" if dmarc else "вы",
    ))

    found = next(
        (name for name in DKIM_SELECTORS if _txt(f"{name}._domainkey.{domain}")),
        None,
    )
    if found:
        checks.append(Check(f"DKIM у {domain}", True, f"селектор «{found}»"))
    elif spf and dmarc:
        # DMARC проходит, если совпал ХОТЬ ОДИН из двух: SPF или DKIM. У нас
        # совпадает SPF (письмо уходит с адреса на своём домене, а его SPF
        # разрешает сервер отправителя), поэтому почта проверку проходит и без
        # подписи. DKIM тут не «чинить», а «добавить запаса»: подпись переживает
        # пересылку, на которой SPF ломается. Красить это красным и требовать
        # человека — врать: без человека всё едет.
        checks.append(Check(
            f"DKIM у {domain}", None,
            "подписи нет, но DMARC проходит по SPF — почта работает. "
            "Подпись добавит запас на пересылках",
        ))
    else:
        checks.append(Check(
            f"DKIM у {domain}", None,
            "не нашёл ни одного обычного селектора — включите подпись DKIM в панели хостинга",
            who="вы",
        ))
    return checks


def model_checks(store) -> list[Check]:
    """Модель: есть ли ключ и отвечает ли она ПРЯМО СЕЙЧАС.

    Спрашиваем заново, а не берём записанное расписанием. Записи бывает до
    получаса, и именно на этом экране такая давность недопустима: его
    открывают, когда что-то не так, — и он показывал «модель молчит» через
    сорок минут после того, как связь вернулась (в тот раз у ноутбука отвалился
    интернет: разом не отвечали и телеграм, и почта, и Anthropic).

    Цена вопроса — один токен: запрос сделан на ответ длиной в одно слово.
    Заодно поправляем запись в базе, чтобы остальные экраны не спорили с этим.
    """
    from . import llm
    from .schedule import MODEL_STATE

    settings = get_settings()
    if not settings.anthropic_api_key:
        return [Check("Модель", False, "ANTHROPIC_API_KEY не задан в orbit/.env", who="вы")]

    try:
        alive, why = llm.ping()
    except Exception as exc:  # noqa: BLE001 — экран не имеет права падать из-за модели
        return [Check("Модель", None, f"спросить не вышло: {exc}")]

    if alive:
        store.delete_state(MODEL_STATE)
        return [Check("Модель", True, "отвечает")]
    store.set_state(MODEL_STATE, why)
    return [Check("Модель", False, why, who="вы")]


def exchange_checks() -> list[Check]:
    """Биржи: вход, остаток на счёте, стоп-кран."""
    from .sections import freelance

    try:
        fbid_store = freelance._open_fbid_store()
    except Exception as exc:  # noqa: BLE001 — биржа не подключена
        return [Check("Биржи", None, f"система не подключена: {exc}")]

    checks: list[Check] = []
    try:
        from fbid.bidder import paused
        from fbid.models import Source
        from fbid.pipeline import down_sources, known_balance, lost_sessions

        lost = set(lost_sessions(fbid_store))
        # «Вход слетел» и «площадка не отвечает» — разные беды с разным
        # лечением. Вторая обычно означает мёртвый прокси, и тогда молчат сразу
        # все браузерные проходы: сбор, остаток пакета, проверка ответов.
        down = set(down_sources(fbid_store))
        for source in (Source.FL, Source.PROFI):
            name = freelance.platform_title(source.value)
            if source.value in lost:
                checks.append(Check(
                    f"Вход на {name}", False,
                    f"слетел — нужна команда fbid login {source.value}", who="вы",
                ))
            else:
                checks.append(Check(f"Вход на {name}", True, "жив"))

            if source.value in down:
                checks.append(Check(
                    f"Связь с {name}", False,
                    "площадка не отвечает — чаще всего это прокси: fbid check-proxy",
                    who="вы",
                ))

            known = known_balance(fbid_store, source)
            word = "₽" if source is Source.PROFI else "откликов в пакете"
            if known is None:
                checks.append(Check(
                    f"Остаток на {name}", None,
                    "ещё не читали — цифра появится после первого прохода с браузером",
                ))
            else:
                value, _when = known
                checks.append(_balance_check(fbid_store, source, name, value, word))

        checks.append(Check(
            "Стоп-кран", not paused(fbid_store),
            "отклики остановлены кнопкой" if paused(fbid_store) else "отключён, отклики идут",
        ))
    except Exception as exc:  # noqa: BLE001 — биржа отвечает не так, как мы ждём
        logger.warning("Готовность: биржу не опросил: {}", exc)
        checks.append(Check("Биржи", None, f"не опросил: {exc}"))
    finally:
        with suppress(Exception):
            fbid_store.close()
    return checks


# Ниже скольки откликов остаток считаем красным. Один-два — это «сегодня
# закончится посреди дня», и узнать об этом лучше заранее.
BIDS_ENOUGH = 3


def _balance_check(fbid_store, source, name: str, value: float, word: str) -> Check:
    """Остаток на площадке словами, которые что-то значат.

    Рубли сами по себе не говорят ничего: отклик на Profi стоит от ста до
    четырёхсот, и «170 ₽» — это не «мало денег», а «один отклик». Считаем по
    последним отправкам, а не по потолку из настроек: потолок — это сколько мы
    согласны заплатить, а не сколько площадка берёт.
    """
    if value <= 0:
        return Check(f"Остаток на {name}", False, f"пусто — пополните ({word})", who="вы")

    try:
        # Считаем и склоняем тем же кодом, что и экран самой биржи: две
        # разные формулировки одного числа на соседних экранах читаются как
        # два разных числа.
        from fbid.tgbot import _counted_bids, bids_left

        left = bids_left(fbid_store, source, value)
        words = _counted_bids(left) if left is not None else ""
    except Exception:  # noqa: BLE001 — не посчитали, значит просто покажем сумму
        left, words = None, ""

    if left is None:
        return Check(f"Остаток на {name}", True, f"{value:g} {word}")
    if left < BIDS_ENOUGH:
        return Check(
            f"Остаток на {name}", False, f"{value:g} {word} — это {words}, пополните", who="вы",
        )
    return Check(f"Остаток на {name}", True, f"{value:g} {word} — это {words}")


def base_checks(store) -> list[Check]:
    """Сама база: есть ли кому писать."""
    from .models import Kind

    partners = store.count(Kind.PARTNER)
    clients = store.count(Kind.CLIENT)
    return [
        Check(
            "Есть кому писать", bool(partners or clients),
            f"партнёров {partners}, клиентов {clients}" if (partners or clients)
            else "база пустая — загрузите выгрузки парсера",
            who="" if (partners or clients) else "вы",
        ),
    ]


def collect(store) -> list[tuple[str, list[Check]]]:
    """Все проверки по разделам. Порядок — от того, что дороже стоит."""
    return [
        ("Почта", mail_checks()),
        ("Модель", model_checks(store)),
        ("Биржи", exchange_checks()),
        ("База", base_checks(store)),
    ]


def waiting_for_you(groups: list[tuple[str, list[Check]]]) -> list[Check]:
    """Только то, что без человека не поедет."""
    return [check for _title, checks in groups for check in checks if check.who == "вы"]

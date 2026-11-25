"""Проверка адреса до того, как на него ушло письмо.

Почты, собранные с чужих сайтов, — самое грязное сырьё в базе. Домен закрыт
год назад, в разметке лежала подпись веб-мастера, форма обратной связи
подсунула образец. Отправленное такому адресату письмо возвращается ошибкой,
а десяток возвратов подряд почтовые службы считают признаком рассылки по
купленной базе и режут доверие ко всему домену — включая письма клиентам и
счета.

Отдельная беда — спам-ловушки: адреса вроде `abuse@` и `postmaster@` заведены
не для переписки, а чтобы ловить тех, кто пишет всем подряд. Отличить ловушку
от живого ящика после отправки нельзя, только до.

Что проверяем и чего не проверяем. Синтаксис, служебные ящики и живость
домена — да. Существование самого ящика (постучаться к почтовому серверу и
спросить `RCPT TO`) — намеренно нет: половина серверов отвечает «да» на любой
адрес, вторая половина заносит спрашивающего в серый список, и проверка сама
превращается в то, от чего защищает.

Итог проверки бывает трёх видов, и путать их нельзя:

* можно писать;
* нельзя никогда — домена нет, ящик служебный, адрес личный (это приговор,
  контакт закрывается);
* сейчас не проверить — DNS не ответил (это не приговор: письмо ждёт).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from loguru import logger

from .models import Contact, is_corporate_email
from .store import Store, today_msk

# Сколько ждём ответа DNS. Больше пяти секунд ждать незачем: живой резолвер
# отвечает за миллисекунды, а долгое молчание значит, что сети нет вовсе.
DNS_TIMEOUT = 5.0
# Одной попытки мало. При проверке базы подряд уходит под сотню запросов, и
# резолвер начинает их придерживать: на живых доменах с MX мы честно получали
# «не ответил вовремя», хотя dig отвечал по ним мгновенно. Разница между
# «домена нет» и «нас притормозили» слишком дорогая, чтобы решать её с одной
# попытки.
DNS_ATTEMPTS = 3
DNS_RETRY_PAUSE = 1.5
# Сколько живёт удачная проверка. Домен могут закрыть, но не за неделю.
FRESH_DAYS = 30

# Ящики, заведённые не для переписки. Часть из них (postmaster, abuse) по RFC
# обязана быть у каждого домена и чаще всего никем не читается, а у почтовых
# служб именно они работают спам-ловушками: письмо туда — прямая жалоба.
SERVICE_MAILBOXES = frozenset({
    "postmaster", "abuse", "hostmaster", "webmaster", "root", "security",
    "spam", "spamtrap", "junk", "trap", "blackhole", "devnull", "null",
    "noreply", "no-reply", "notreply", "donotreply", "do-not-reply",
    "bounce", "bounces", "mailer-daemon", "mailerdaemon", "daemon",
    "undisclosed", "unsubscribe", "remove", "listserv", "majordomo",
})

# Одноразовая почта: живёт десять минут и заводится, чтобы что-то скачать.
# Компании такими адресами не пользуются, а в базу они попадают из форм.
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "tempmail.com", "temp-mail.org", "10minutemail.com",
    "guerrillamail.com", "yopmail.com", "trashmail.com", "fakeinbox.com",
    "dropmail.me", "getnada.com", "maildrop.cc", "sharklasers.com",
    "throwawaymail.com", "mytemp.email", "temp-mail.ru", "vremenaya-pochta.ru",
})

# Тот же разбор, что в моделях, но здесь он нужен до всех прочих проверок:
# кривой адрес незачем искать в DNS.
#
# Буквы намеренно не ограничены латиницей. Домен «компания.рф» — обычный
# рабочий домен российской компании, а не мусор: DNS его понимает (переводит
# в punycode сам). Латинская проверка объявляла такой адрес «это не почтовый
# адрес» навсегда и закрывала контакт — то есть теряла живого клиента из-за
# кириллицы в домене.
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)*\.[^@\s.]{2,}$")


class DomainState(StrEnum):
    """Что DNS сказал про домен."""

    ACCEPTS = "accepts"   # есть куда доставлять почту
    DEAD = "dead"         # домена нет или он почту не принимает
    UNKNOWN = "unknown"   # DNS не ответил — про домен мы ничего не узнали


@dataclass(frozen=True)
class Verdict:
    """Приговор адресу.

    `permanent` отделяет «этому адресу писать нельзя никогда» от «сейчас не
    смогли проверить». Первое закрывает контакт, второе только откладывает
    письмо — иначе одна упавшая сеть вычистила бы половину базы.
    """

    ok: bool
    reason: str
    permanent: bool = False

    @property
    def retry_later(self) -> bool:
        return not self.ok and not self.permanent


def check(store: Store | None, address: str) -> Verdict:
    """Полная проверка адреса. Результат кэшируется в базе, если она передана.

    Без базы (в разовых сценариях) проверка работает так же, просто каждый раз
    заново ходит в DNS.
    """
    normalized = (address or "").strip().lower()

    offline = _check_offline(normalized)
    if offline is not None:
        return offline

    if store is not None:
        cached = _from_cache(store, normalized)
        if cached is not None:
            return cached

    domain = normalized.rsplit("@", 1)[-1]
    state, detail = domain_state(domain)
    if state is DomainState.UNKNOWN:
        # Кэшировать нечего: мы ничего не узнали. Кэш «не знаю» на месяц
        # означал бы, что домен, недоступный одну минуту, месяц считается
        # непроверяемым.
        return Verdict(False, f"не удалось проверить домен: {detail}")
    if state is DomainState.DEAD:
        verdict = Verdict(False, f"домен не принимает почту: {detail}", permanent=True)
    else:
        verdict = Verdict(True, "адрес проверен")

    if store is not None:
        store.save_email_check(normalized, verdict.ok, verdict.reason, verdict.permanent)
    return verdict


def known_address(store: Store, contact: Contact) -> str | None:
    """Адрес, на который уйдёт письмо, — по тому, что уже известно.

    В DNS не ходит намеренно: это для экрана. Карточка письма обязана
    показывать тот же адрес, который получит адресат, иначе владелец видит
    одно, а уходит другое — самая дорогая порода ошибок в этом хозяйстве.

    Пропускаем ровно то же, что пропустит отправка: и забракованное DNS (это
    лежит в кэше), и забракованное без всякой сети — служебный ящик,
    одноразовую почту, кривой адрес. Второе в кэш не попадает вовсе, и без
    этой проверки экран показывал `webmaster@` или `info@mailinator.com`,
    а письмо уходило на следующий адрес.
    """
    for address in contact.email_candidates:
        if _check_offline(address) is not None:
            continue  # отсеется без всякой сети — письмо уйдёт не на него
        row = store.email_check(address)
        if row is not None and not row["ok"] and row["permanent"]:
            continue  # этот уже забракован навсегда
        return address
    return None


def pick_address(store: Store | None, contact: Contact) -> tuple[str | None, Verdict]:
    """Первый адрес контакта, прошедший проверку.

    У компании нередко несколько ящиков: `info@` со старого домена и `sales@`
    с нового. Проверять только лучший значит терять компанию из-за адреса,
    которым она сама давно не пользуется.

    Если ни один не прошёл, вернувшийся приговор — от последнего проверенного,
    и по его `permanent` видно, закрывать контакт или ждать: одного
    непроверяемого адреса хватает, чтобы отложить письмо целиком.
    """
    candidates = contact.email_candidates
    if not candidates:
        return None, Verdict(False, "нет корпоративного адреса", permanent=True)

    verdicts = []
    for address in candidates:
        verdict = check(store, address)
        if verdict.ok:
            return address, verdict
        verdicts.append(verdict)

    # Приговор «нельзя никогда» выносим, только если так решили про все адреса.
    # Иначе среди них есть непроверенный, и контакт ещё может ожить.
    if any(v.retry_later for v in verdicts):
        return None, next(v for v in verdicts if v.retry_later)
    return None, verdicts[0]


def _check_offline(address: str) -> Verdict | None:
    """Проверки, для которых сеть не нужна. None — придётся смотреть DNS.

    Регистр приводим здесь же, а не полагаемся на вызывающего: сравнения ниже
    идут со строчными списками, и один `POSTMASTER@` мимо них — это письмо в
    спам-ловушку.
    """
    address = (address or "").strip().lower()
    if not address:
        return Verdict(False, "адреса нет", permanent=True)
    # Пределы из RFC 5321. Ограничение здесь не про красоту: без него мусор
    # вроде склеенной строки из разметки уезжает в DNS целиком и тратит
    # запросы на то, чего заведомо не существует.
    if len(address) > 320 or len(address.rsplit("@", 1)[-1]) > 253:
        return Verdict(False, "это не почтовый адрес: слишком длинный", permanent=True)
    if not _ADDRESS_RE.match(address):
        return Verdict(False, "это не почтовый адрес", permanent=True)

    local, domain = address.rsplit("@", 1)
    # «info.msk» и «sales-spb» — те же отделы; служебный ящик узнаём так же.
    head = re.split(r"[.\-_+]", local)[0]
    if local in SERVICE_MAILBOXES or head in SERVICE_MAILBOXES:
        return Verdict(False, "служебный ящик, туда пишут только машины", permanent=True)
    if domain in DISPOSABLE_DOMAINS:
        return Verdict(False, "одноразовая почта", permanent=True)
    if not is_corporate_email(address):
        return Verdict(False, "личный ящик, холодные письма туда не шлём", permanent=True)
    return None


def _from_cache(store: Store, address: str) -> Verdict | None:
    """Что мы уже знаем про этот адрес. None — не знаем или знание протухло."""
    row = store.email_check(address)
    if row is None:
        return None
    if row["permanent"]:
        # Домена нет — он от этого не появится. Такое знание не протухает.
        return Verdict(bool(row["ok"]), row["reason"], permanent=True)
    try:
        checked = date.fromisoformat(row["checked_at"])
    except (TypeError, ValueError):
        return None
    if date.fromisoformat(today_msk()) - checked > timedelta(days=FRESH_DAYS):
        return None
    return Verdict(bool(row["ok"]), row["reason"], permanent=False)


def domain_state(domain: str) -> tuple[DomainState, str]:
    """Принимает ли домен почту. Вторым значением — что именно выяснилось.

    «Принимает» верим с первого раза: тут ошибиться нечем. Остальные два
    ответа переспрашиваем, и по разным причинам.

    Молчание — чаще всего не мёртвый домен, а придержанный запрос: при обходе
    базы подряд уходит под сотню запросов, и живые домены с MX честно отвечали
    «не ответил вовремя».

    «Домена нет» — приговор навсегда: контакт закрывается, и вернуть его
    некому. Один NXDOMAIN от подменённого DNS (гостиничный вайфай, сбой у
    провайдера, перехваченный резолвер) стоил бы живой компании. Поэтому
    хороним домен только тогда, когда две попытки подряд сказали одно и то же.
    """
    import time

    state, detail = _ask_dns(domain)
    if state is DomainState.ACCEPTS:
        return state, detail

    for _ in range(DNS_ATTEMPTS - 1):
        time.sleep(DNS_RETRY_PAUSE)
        again, detail_again = _ask_dns(domain)
        if again is DomainState.ACCEPTS:
            return again, detail_again
        if again is state and state is DomainState.DEAD:
            return state, detail  # два раза подряд одно и то же — верим
        state, detail = again, detail_again

    if state is DomainState.DEAD:
        # Последняя попытка сказала «нет», но подтверждения не было. Это не
        # приговор: пусть письмо подождёт следующего захода.
        return DomainState.UNKNOWN, "«домена нет» не подтвердилось повтором"
    return state, detail


def _ask_dns(domain: str) -> tuple[DomainState, str]:
    """Один заход в DNS.

    Сначала MX: это запись «почту для меня принимает вот этот сервер». Если её
    нет, по RFC 5321 письмо всё равно можно везти на сам домен, поэтому
    смотрим A и AAAA. Нет ни того, ни другого — доставлять некуда.
    """
    try:
        import dns.exception
        import dns.rdatatype
        import dns.resolver
    except ImportError:  # pragma: no cover — библиотека в зависимостях
        # Молча пропускать проверку нельзя: письма пойдут на непроверенные
        # адреса, а мы будем думать, что проверка работает.
        return DomainState.UNKNOWN, "не установлен dnspython, проверять нечем"

    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT
    resolver.lifetime = DNS_TIMEOUT
    # SERVFAIL от одного сервера имён — это его беда, а не приговор домену.
    resolver.retry_servfail = True

    try:
        answer = resolver.resolve(domain, dns.rdatatype.MX)
        if any(str(record.exchange).strip(".") for record in answer):
            return DomainState.ACCEPTS, "есть MX"
        # Пустой ответ бывает у домена с «нулевым MX» (точка вместо сервера) —
        # это официальный способ сказать «почту сюда не возить».
        return DomainState.DEAD, "MX пустой: домен почту не принимает"
    except dns.resolver.NXDOMAIN:
        return DomainState.DEAD, "домена не существует"
    except dns.resolver.NoAnswer:
        pass  # MX нет — но, возможно, есть A; смотрим ниже
    except dns.resolver.NoNameservers:
        return DomainState.UNKNOWN, "серверы имён домена не отвечают"
    except dns.exception.Timeout:
        return DomainState.UNKNOWN, "DNS не ответил вовремя"
    except Exception as exc:  # noqa: BLE001 — сбой DNS не должен ронять отправку
        logger.warning("DNS для {} не ответил: {}", domain, exc)
        return DomainState.UNKNOWN, str(exc)

    for kind in (dns.rdatatype.A, dns.rdatatype.AAAA):
        try:
            if list(resolver.resolve(domain, kind)):
                return DomainState.ACCEPTS, "MX нет, но домен отвечает по адресу"
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException:
            return DomainState.UNKNOWN, "DNS не ответил вовремя"
        except Exception as exc:  # noqa: BLE001 — сбой резолвера это не «домена нет»
            # Ловим широко намеренно: любое неожиданное исключение отсюда
            # улетало наружу и роняло проход. Незнание домена дешевле.
            logger.warning("DNS для {} не ответил: {}", domain, exc)
            return DomainState.UNKNOWN, str(exc)
    return DomainState.DEAD, "ни MX, ни адреса — доставлять некуда"

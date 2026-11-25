"""Отправка писем: проверки, лимиты, почта.

Здесь живут все запреты. Любая отправка проходит через `send()`, и если хоть
одна проверка не прошла — письмо не уходит. Правило то же, что на биржах:
в спорной ситуации молчим, потому что отозвать отправленное нельзя.

Телеграм отправляется иначе, чем почта, и это ограничение платформы, а не
недоделка: бот физически не может написать человеку первым, Telegram
разрешает ему отвечать только тем, кто сам ему написал. Поэтому телеграм
здесь готовится к ручному нажатию, а автоматически уходит только почта.
"""
from __future__ import annotations

import io
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.generator import BytesGenerator
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid, parseaddr

from loguru import logger

from . import mailcheck
from .config import get_settings
from .models import Channel, Contact, Draft, Kind, Stage
from .store import MSK, Store, today_msk

SIGNATURE = "Менеджер Uniqore · https://uniqore.ru · TG: @UniqoreManager"

# Строки «если неактуально, ответьте стоп» в письме больше нет (решение
# владельца от 07.08.2026, после первой живой проверки). Она выдавала
# рассылку с порога: живой человек, написавший одному адресату, такое не
# приписывает. Само право отказаться никуда не делось — ответ «стоп»
# по-прежнему закрывает контакт навсегда, см. looks_like_stop().

# Ответ, после которого контакт закрывается навсегда.
# Начала строк, по которым узнаём отказ. Именно НАЧАЛА: «стоп» внутри строки
# бывает в «стопроцентно» и «стоп-кран».
STOP_WORDS = ("стоп", "отписка", "unsubscribe", "не пишите")
# А это ищем в любом месте строки. Люди отказываются вежливо и длинно
# («Прошу больше не писать на этот адрес», «Уберите нас из рассылки»), и такой
# ответ проверка по началу строки не ловила вовсе — цепочка касаний продолжала
# писать тому, кто попросил перестать. Ошибиться в сторону «замолчали» здесь
# дешевле: закрытый по ошибке контакт владелец видит в уведомлении и может
# вернуть, а жалобу на спам вернуть нельзя.
#
# Каждая строка однозначна сама по себе, без «стоп» и прочих коротких слов:
# они и живут в списке выше, где привязаны к началу строки.
STOP_PHRASES = (
    "не пишите",
    "не писать",
    "не присылайте",
    "не присылать",
    "отпишите",
    "отписаться",
    "отпишитесь",
    "удалите из базы",
    "удалите из рассылки",
    "уберите из базы",
    "уберите из рассылки",
    "уберите нас из рассылки",
    "исключите из рассылки",
    "исключите нас",
    "прекратите рассылку",
    "больше не беспокоить",
)

# Ключи настроек, которые владелец меняет из бота.
LIMIT_KEYS = {Kind.PARTNER: "limit:partners", Kind.CLIENT: "limit:clients"}

# Автоотправка включается отдельно для каждого канала. Разделение не
# формальное: почта уходит сама, а телеграм упирается в ограничение платформы
# (бот не может написать человеку первым) и требует либо ручного нажатия,
# либо отдельного пользовательского аккаунта.
AUTOSEND_KEYS = {
    Channel.EMAIL: "autosend:email",
    Channel.TELEGRAM: "autosend:telegram",
}
# Прежний общий ключ. Читаем его как значение для почты, чтобы у тех, кто уже
# включил автоотправку, она не выключилась молча после обновления.
LEGACY_AUTOSEND_KEY = "autosend"
# Выше этого суточный лимит поднять нельзя даже кнопкой: ящик, разославший
# за день две сотни холодных писем, уезжает в спам вместе с доменом.
MAX_DAILY = 200


@dataclass(frozen=True)
class SendResult:
    ok: bool
    reason: str
    manual: bool = False  # текст готов, но отправить должен человек


def letters_left_today(store: Store, kind: Kind) -> int:
    """Сколько ПИСЕМ этой вкладке ещё можно отправить сегодня.

    Ровно та же арифметика, что в can_send, и держится она в одном месте
    намеренно. Считались эти две нормы порознь: `can_send` пропускал письмо,
    а очередь и экраны брали «потолок минус касания» — то есть прогревный
    потолок ЯЩИКА против касаний ВСЕМИ каналами. Три сообщения в телеграм
    руками — и фоновая очередь молча одобряла ноль писем, хотя проверка у
    самой двери была не против.

    Норм две, и обе обязательны:
      · касания вкладки — про компанию (телеграм тоже касание);
      · прогрев ящика — про ящик (телеграм с личного аккаунта его не трогал).
    """
    by_touches = tab_limit(store, kind) - store.sent_today(kind)
    by_warmup = warmup_limit(store, kind) - store.mailed_today()
    return max(0, min(by_touches, by_warmup))


def tab_limit(store: Store, kind: Kind) -> int:
    """Норма КАСАНИЙ вкладки за сутки: письма и ручной телеграм вместе.

    Отдельно от прогрева намеренно. Норм две, и меряются они разным:
    касания — про компанию (получателю всё равно, чьими руками ему написали),
    прогрев — про ящик (телеграм уходит с личного аккаунта владельца, ящик при
    этом молчит). Одна общая цифра сваливала их в кучу: три сообщения в
    телеграм в первый день закрывали ящику всю прогревную норму, писем не
    уходило ни одного, а отсчёт дней прогрева начинается от первого УШЕДШЕГО
    письма — то есть не начинался никогда.
    """
    settings = get_settings()
    default = (
        settings.daily_limit_partners if kind is Kind.PARTNER else settings.daily_limit_clients
    )
    raw = store.get_state(LIMIT_KEYS.get(kind, ""), None)
    if raw is None:
        return default
    try:
        return max(0, min(MAX_DAILY, int(float(raw))))
    except (TypeError, ValueError):
        logger.warning("Кривой лимит в базе: {} — беру значение по умолчанию", raw)
        return default


# Как набираем объём в первые дни. Новый ящик, который сразу начал слать
# двадцать писем в день, уезжает в спам вместе с доменом: у него нет истории
# отправок, и для почтового сервера это выглядит как взломанный аккаунт.
WARMUP_STEPS = (3, 5, 8, 12, 16)
# После скольких дней тишины прогрев начинается заново. Неделя молчания для
# принимающей стороны means ящик снова новый: репутация отправителя держится
# на регулярности, а не на том, что когда-то он писал.
WARMUP_RESET_DAYS = 7
# Отметка прежней версии: день первого письма. Больше ни на что не влияет —
# прогрев считается по дням реальных отправок (см. warmup_limit). Оставлена,
# чтобы старые базы не спотыкались на её чтении.
WARMUP_KEY = "warmup:first_day"


def warmup_limit(store: Store, kind: Kind) -> int:
    """Потолок на сегодня по прогреву. День первый — три письма, дальше больше.

    Считаем ДНИ, В КОТОРЫЕ ПИСЬМА ДЕЙСТВИТЕЛЬНО УХОДИЛИ, а не календарь.
    Разница не теоретическая: ящик отправил восемь писем 7 августа и замолчал
    на девятнадцать дней. По календарю прогрев к 12 августа считался
    пройденным, и первый же заход после паузы пошёл бы с потолка в двести
    писем — с ящика, у которого за всю жизнь восемь отправок. Для почтовой
    службы это выглядит как взломанный аккаунт, и уезжает в спам весь домен
    вместе с письмами клиентам и счетами.

    Долгая пауза откатывает прогрев в начало по той же причине: репутация
    отправителя набирается регулярностью, а молчащий месяц ящик для
    принимающей стороны снова новый.
    """
    from datetime import date, timedelta

    if not get_settings().smtp_ready:
        return MAX_DAILY

    today = today_msk()
    days = [day for day in store.mail_days() if day < today]
    if not days:
        return WARMUP_STEPS[0]

    try:
        last = date.fromisoformat(days[-1])
        if date.fromisoformat(today) - last >= timedelta(days=WARMUP_RESET_DAYS):
            # Ящик молчал слишком долго — начинаем набирать объём заново.
            return WARMUP_STEPS[0]
    except ValueError:
        return WARMUP_STEPS[0]

    step = len(days)
    return WARMUP_STEPS[step] if step < len(WARMUP_STEPS) else MAX_DAILY


def autosend_on(store: Store, channel: Channel = Channel.EMAIL) -> bool:
    """Включена ли автоотправка в этом канале."""
    raw = store.get_state(AUTOSEND_KEYS.get(channel, ""))
    if raw is None and channel is Channel.EMAIL:
        raw = store.get_state(LEGACY_AUTOSEND_KEY)  # настройка из прежней версии
    if raw is None:
        return get_settings().autosend
    return raw == "1"


def set_autosend(store: Store, channel: Channel, enabled: bool) -> None:
    store.set_state(AUTOSEND_KEYS[channel], "1" if enabled else "0")


# Каналы, которые в принципе умеют отправлять сами. Телеграма здесь нет и не
# будет: бот не имеет права написать первым — это ограничение платформы, а не
# ненастроенная мелочь. Переключатель «отправлять самой» для такого канала —
# мёртвая кнопка: она зажигает зелёный знак и не включает ничего.
SELF_SENDING = (Channel.EMAIL,)


def channel_ready(channel: Channel) -> tuple[bool, str]:
    """Может ли канал отправлять сам. Вторым значением — почему нет."""
    if channel is Channel.EMAIL:
        if not get_settings().smtp_ready:
            return False, "нет доступа к ящику в .env"
        return True, ""
    if channel is Channel.TELEGRAM:
        # Ограничение платформы, а не недоделка: Telegram разрешает боту
        # отвечать только тем, кто сам ему написал. Автоматическая рассылка
        # потребует отдельного пользовательского аккаунта.
        return False, "бот не может написать первым, нужен отдельный аккаунт"
    return False, "этот канал не рассылает"


# Сколько раз пробуем отправить одно письмо. Три неудачи подряд — это не
# «сеть моргнула», а мёртвый адрес или закрытый ящик.
MAX_ATTEMPTS = 3

# Стадии, на которых холодное письмо уже неуместно: человек ответил, и
# дальше идёт нормальный разговор.
ALREADY_TALKING = (Stage.REPLIED, Stage.CALL, Stage.DEAL)


def compose(draft: Draft, contact: Contact) -> str:
    """Текст письма целиком: тело от модели плюс подпись.

    Подпись добавляем кодом, а не просим у модели: она её иногда забывает,
    а письмо без обратного адреса выглядит подозрительно.
    """
    return "\n".join([draft.body.strip(), "", SIGNATURE])


def _stale_days(draft: Draft) -> float | None:
    """Сколько дней назад написан текст, если он уже протух. Иначе None.

    Порог лежит в followup: там же цепочка касаний, ради которой он и нужен.
    Импорт внутри функции — followup сам берёт кое-что из outreach, и на
    верхнем уровне это кольцо.
    """
    from .followup import STALE_DAYS

    raw = draft.created_at
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        # Отметки нет или она кривая — это не повод придержать письмо.
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=MSK)
    days = (datetime.now(MSK) - when).total_seconds() / 86400.0
    return days if days >= STALE_DAYS else None


def can_send(store: Store, draft: Draft, contact: Contact) -> SendResult:
    """Все проверки перед отправкой. Первая же непройденная останавливает письмо."""
    settings = get_settings()

    if draft.status == "sending":
        # Письмо прямо сейчас отправляет другой: фоновый отправщик или
        # владелец кнопкой. Формулировка важна — «уже отправлено» тут врало бы.
        return SendResult(False, "письмо уже отправляет кто-то другой")
    if draft.status != "draft":
        return SendResult(False, "письмо уже отправлено")
    if contact.stage.value == "dead":
        return SendResult(False, "контакт закрыт: просил не писать или не подошёл")
    if contact.stage in ALREADY_TALKING:
        # Пока письмо стояло в очереди, человек ответил. Отправлять теперь
        # заготовленный текст — значит писать в одну сторону поверх живого
        # разговора; дальше пишет владелец.
        return SendResult(False, "уже идёт разговор — письмо из очереди не отправляю")

    # Текст, пролежавший в очереди слишком долго, отправлять нельзя: он
    # написан про «сейчас». Особенно это верно для касания вдогонку — оно
    # прямо говорит «писал вам недавно». Уборка в followup.drop_stale снимет
    # такое письмо и закажет новое, а эта проверка здесь на случай, если
    # уборка ещё не проходила: отправка идёт круглые сутки, уборка раз в день.
    stale = _stale_days(draft)
    if stale is not None:
        return SendResult(False, f"текст написан {stale:.0f} дней назад — перепишем заново")

    # Две нормы, и обе обязательны. Первая — касания вкладки: сюда идёт и
    # ручная отправка в телеграм, потому что получателю всё равно, чьими
    # руками ему написали.
    touched = store.sent_today(contact.kind)
    touches = tab_limit(store, contact.kind)
    if touched >= touches:
        return SendResult(False, f"суточный лимит исчерпан: {touched} из {touches}")

    # Вторая — прогрев ЯЩИКА, и она считает только письма. Телеграм уходит с
    # личного аккаунта владельца, нагрузкой на ящик не был и его норму
    # съедать не должен.
    if draft.channel is Channel.EMAIL:
        mailed = store.mailed_today()
        allowed = warmup_limit(store, contact.kind)
        if mailed >= allowed:
            return SendResult(False, f"прогрев ящика: сегодня можно {allowed} писем, ушло {mailed}")

    if draft.channel is Channel.TELEGRAM:
        if not contact.best_telegram:
            return SendResult(False, "телеграм контакта не разобрался")
        # Не недоделка, а ограничение Telegram: первым бот написать не может.
        return SendResult(True, "готово к отправке вручную", manual=True)

    if draft.channel is Channel.PHONE:
        return SendResult(False, "писать некуда, только телефон")

    if not contact.best_email:
        return SendResult(False, "нет корпоративного адреса")
    if not settings.smtp_ready:
        return SendResult(False, "почта не настроена: нет доступа к ящику в .env")
    return SendResult(True, "готово")


def send(store: Store, draft_pk: int) -> SendResult:
    """Отправляет одно письмо. Считает лимит только при реальной отправке."""
    draft = store.get_draft(draft_pk)
    if draft is None:
        return SendResult(False, "черновик не найден")
    contact = store.get_contact(draft.contact_pk)
    if contact is None:
        return SendResult(False, "контакт не найден")

    verdict = can_send(store, draft, contact)
    if not verdict.ok:
        # counts=False: до почтового сервера дело не дошло, попыткой доставки
        # это не было. Иначе три исчерпанных лимита подряд снимали бы письмо
        # с очереди навсегда — за то, что мы сами его придержали.
        store.mark_failed(draft_pk, verdict.reason, counts=False)
        # Разрешение снимаем: письмо остаётся в очереди с пометкой, но
        # отправщик не будет биться о него каждые полминуты.
        store.unapprove_draft(draft_pk)
        return verdict
    if verdict.manual:
        # Отправит человек, нажав кнопку в карточке. Здесь ничего не делаем,
        # чтобы не пометить отправленным то, что ещё не ушло.
        return verdict

    # Дальше идут запрос к DNS и разговор с почтовым сервером — несколько
    # секунд, в которые успевал войти и фоновый отправщик, и владелец кнопкой,
    # и письмо уходило дважды. Занимаем письмо за собой атомарно.
    if not store.claim_draft(draft_pk):
        return SendResult(False, "письмо уже отправляет кто-то другой")

    # Последний рубеж: адрес мог пролежать в очереди неделю, а домен за это
    # время закрыться. Отсюда же берём сам адрес — проверка могла отбраковать
    # первый ящик и выбрать второй.
    address, verdict = mailcheck.pick_address(store, contact)
    if address is None:
        store.release_draft(draft_pk)
        # Тоже не попытка доставки: письмо даже не собиралось. Особенно важно
        # для «DNS не ответил» — это состояние временное, и три молчания
        # резолвера не должны стоить письма.
        store.mark_failed(draft_pk, verdict.reason, counts=False)
        store.unapprove_draft(draft_pk)
        if verdict.permanent:
            # Домена нет и не будет: это письмо снимаем совсем.
            store.give_up_draft(draft_pk, verdict.reason)
            if contact.pk is not None and not contact.best_telegram:
                store.set_stage(contact.pk, Stage.DEAD, f"адрес не годится: {verdict.reason}"[:200])
            elif contact.pk is not None:
                # Почта мёртвая, но телеграм живой. Контакт закрывать нельзя:
                # компания достижима, просто другим каналом. То же правило,
                # что при подготовке письма и в `orbit validate --close`.
                store.log_event(contact.pk, "channel", f"почта не годится: {verdict.reason}"[:200])
            # Письмо исчезает из очереди совсем, а стоило платного вызова
            # модели. В автоматическом режиме отправкой занимается фоновый
            # поток, и результат его работы не видит никто: единственным
            # следом оставалась строка в логе, которую не читают.
            _tell_address_rejected(store, contact, verdict.reason)
            return SendResult(False, f"адрес не годится: {verdict.reason}")
        return SendResult(False, f"адрес не проверить: {verdict.reason}")

    text = compose(draft, contact)
    # Отметку ставим до разговора с почтой, а не после. Между «сервер принял» и
    # «мы это записали» процесс может умереть, и тогда по базе письмо выглядит
    # неотправленным — при следующем запуске оно ушло бы получателю вторично.
    store.hand_to_smtp(draft_pk)
    try:
        deliver_email(address, draft.subject, text)
    except MaybeDelivered as exc:
        # Связь оборвалась ПОСЛЕ того, как текст письма ушёл в сокет. Сервер
        # мог его принять и разослать — а мог и не успеть. Возвращать в
        # очередь нельзя: вторая копия холодного письма хуже, чем его
        # отсутствие. Решать владельцу, и сказать ему надо сразу, а не при
        # следующем перезапуске отправщика.
        logger.error("Письмо для «{}»: судьба неизвестна — {}", contact.name, exc)
        store.mark_unclear(draft_pk)
        if contact.pk is not None:
            store.log_event(contact.pk, "channel", f"судьба письма неизвестна: {exc}"[:200])
        _tell_letter_unclear(store, contact, exc)
        return SendResult(False, "связь оборвалась на отправке — неизвестно, ушло ли письмо")
    except Exception as exc:  # noqa: BLE001 — упавшая почта не роняет бота
        logger.error("Письмо для «{}» не ушло: {}", contact.name, exc)
        store.release_draft(draft_pk)
        # Счётчик попыток меряет отказы АДРЕСАТА. Неверный пароль в .env или
        # пропавшая сеть — это наша сторона, и адрес в этом не виноват: без
        # такого разделения одна ошибка в пароле за три круга хоронила всю
        # очередь, каждое письмо навсегда, а причина оставалась в логе.
        # Временный отказ адресата — не отказ. Считать его попыткой значит
        # хоронить письмо за то, что чужой сервер попросил подождать.
        if wait_and_retry(exc):
            store.mark_failed(draft_pk, f"сервер получателя просит подождать: {exc}", counts=False)
            store.unapprove_draft(draft_pk)
            logger.info(
                "«{}»: временный отказ ({}) — письмо остаётся в очереди", contact.name, exc
            )
            return SendResult(False, f"сервер получателя просит подождать: {exc}")

        ours = _our_side(exc)
        attempts = store.mark_failed(draft_pk, str(exc), counts=not ours)
        store.unapprove_draft(draft_pk)
        if ours:
            logger.error("Это наша сторона, а не адресат — письмо оставляю в очереди")
            return SendResult(False, f"почта не работает у нас: {exc}")
        if attempts >= MAX_ATTEMPTS:
            # Адрес не принимает письма третий раз подряд. Дальше стучаться
            # некуда: почтовый сервер считает это перебором и блокирует ящик
            # целиком, а вместе с ним и всю рассылку.
            # Адрес запоминаем как негодный ДО того, как отпустить контакт.
            # `give_up_draft` возвращает его в «новые», и без этой записи
            # пачка написала бы новое письмо на тот же мёртвый ящик: счётчик
            # попыток живёт на черновике, а не на адресе, и с новым письмом
            # обнулился бы. Получился бы круг: платный текст — три отказа —
            # снова платный текст.
            #
            # Но вычёркиваем только при отказе САМОМУ АДРЕСУ («нет такого
            # ящика»). Отказ после текста письма — это приговор ПИСЬМУ:
            # спам-фильтр не принял формулировку, ссылку, домен отправителя.
            # Адрес там живой, и вычеркнуть его значит потерять компанию за
            # то, в чём она не виновата.
            if _address_was_refused(exc):
                store.save_email_check(
                    address, ok=False, reason=f"не принимает письма: {exc}"[:200], permanent=True
                )
            else:
                logger.info(
                    "«{}»: отказ пришёл на текст письма, а не на адрес — адрес не вычёркиваю",
                    contact.name,
                )
            store.give_up_draft(draft_pk, f"после {attempts} попыток: {exc}")
            logger.warning(
                "«{}»: {} попытки подряд неудачны — письмо снял с отправки, адрес вычеркнул",
                contact.name, attempts,
            )
            # Владельцу об этом надо сказать вслух. Письмо готовила модель, оно
            # стоило денег и висело в очереди — а теперь просто исчезает. В
            # автоматическом режиме отправкой занимается фоновый поток, и
            # результат его работы не видит никто: единственным следом
            # оставалась строка в логе, которую никто не читает.
            if contact.pk is not None:
                store.log_event(contact.pk, "bounce", f"снял с отправки: {exc}"[:200])
            _tell_letter_dropped(store, contact, attempts, exc)
            return SendResult(False, f"не отправляется {attempts} раз подряд, снял с очереди")
        return SendResult(False, f"почта не приняла письмо: {exc}")

    _after_send(store, draft_pk, contact, "почта")
    return SendResult(True, "отправлено")


def mark_sent_manually(store: Store, draft_pk: int) -> SendResult:
    """Владелец сам отправил текст в телеграме и нажал «Отправил».

    Лимит здесь считается тоже: адресат не различает, чьими руками пришло
    сообщение, а нам важно не превысить дневную норму по контакту.
    """
    draft = store.get_draft(draft_pk)
    if draft is None:
        return SendResult(False, "черновик не найден")
    if draft.status != "draft":
        # Кнопка в телеграме нажимается второй раз легко: экран не обновился,
        # палец соскользнул. Второе нажатие не должно съедать ещё одно письмо
        # из суточной нормы — сообщение-то ушло одно.
        return SendResult(False, "это сообщение уже отмечено отправленным")
    contact = store.get_contact(draft.contact_pk)
    if contact is None:
        return SendResult(False, "контакт не найден")

    # Проверки тут — только предупреждение, не запрет. Кнопка описывает УЖЕ
    # случившееся: сообщение отправлено руками, и отказ его записать не вернёт
    # сообщение назад — он лишь разведёт базу с реальностью: черновик остался
    # бы «неотправленным», а лимит — несчитанным, и то же самое ушло бы ещё
    # раз. Свершившийся факт записываем всегда, а если проверка не прошла —
    # говорим об этом владельцу в ответе, чтобы он знал, что вышел за рамки.
    verdict = can_send(store, draft, contact)
    _after_send(store, draft_pk, contact, "телеграм вручную", by_mail=False)
    if not verdict.ok:
        return SendResult(True, f"записал, но имейте в виду: {verdict.reason}")
    return SendResult(True, "записал как отправленное")


def _report_interrupted(store: Store) -> None:
    """Возвращает в очередь застрявшие письма и рассказывает о непонятных.

    Письмо, которое успели отдать почтовому серверу перед падением процесса,
    в очередь не возвращается: вторая отправка того же холодного письма тому
    же человеку хуже неотправленной. Решать, что с ним делать, должен
    владелец — но узнать об этом он мог только из лога, которого не читает.
    """
    from . import alerts

    _returned, unclear = store.release_stale_sending()
    if not unclear:
        return
    names = "\n".join(f"· {name}" for name in unclear[:10])
    tail = "" if len(unclear) <= 10 else f"\n…и ещё {len(unclear) - 10}"
    try:
        alerts.deliver(
            store,
            alerts.LETTER_UNCLEAR,
            f"❓ <b>Письма с неизвестной судьбой: {len(unclear)}</b>\n"
            f"Отправка прервалась на середине — почта их могла и принять, и нет. "
            f"Второй раз не отправляю: повтор холодного письма хуже, чем его "
            f"отсутствие.\n\n{names}{tail}\n\n"
            f"Загляните в «Отправленные» ящика и решите сами."
        )
    except Exception as exc:  # noqa: BLE001 — не смогли сказать, но работаем дальше
        logger.warning("Не отправилось сообщение о прерванных письмах: {}", exc)


def _tell_letter_unclear(store: Store, contact: Contact, exc: Exception) -> None:
    """Говорит владельцу про письмо, судьба которого неизвестна.

    Молчать нельзя: письмо ушло из очереди, на экранах его больше нет, а
    получатель мог его получить, мог не получить. Проверить это можно только
    руками, в «Отправленных» ящика.
    """
    from . import alerts, ui

    try:
        alerts.deliver(
            store,
            alerts.LETTER_UNCLEAR,
            f"❓ <b>Судьба письма неизвестна</b>\n"
            f"{ui.esc(contact.name)}\n\n"
            f"Связь с почтой оборвалась уже после того, как текст письма ушёл: "
            f"сервер мог его принять, а мог и нет. Второй раз не отправляю — "
            f"повтор холодного письма хуже, чем его отсутствие.\n\n"
            f"Загляните в «Отправленные» ящика и решите сами.\n"
            f"<i>{ui.esc(str(exc))}</i>"
        )
    except Exception as tell_exc:  # noqa: BLE001 — не смогли сказать, но работаем дальше
        logger.warning("Не отправилось сообщение о непонятном письме: {}", tell_exc)


def _tell_address_rejected(store: Store, contact: Contact, reason: str) -> None:
    """Говорит владельцу, что письмо снято из-за приговора адресу.

    Приговор окончательный — домена нет, ящик служебный. Письмо при этом
    уходит из очереди навсегда, и без этой строки владелец узнаёт об этом
    ниоткуда.
    """
    from . import alerts, ui

    tail = (
        "Телеграм у контакта живой — напишу туда, когда подготовлю."
        if contact.best_telegram
        else "Других каналов нет, контакт закрыл."
    )
    try:
        alerts.deliver(
            store,
            alerts.ADDRESS_BAD,
            f"🚫 <b>Адрес не годится</b>\n"
            f"{ui.esc(contact.name)}\n"
            f"{ui.esc(reason)}\n\n"
            f"Письмо снял с отправки. {tail}"
        )
    except Exception as exc:  # noqa: BLE001 — не смогли сказать, но живём дальше
        logger.warning("Не отправилось сообщение о забракованном адресе: {}", exc)


def _tell_letter_dropped(store: Store, contact: Contact, attempts: int, exc: Exception) -> None:
    """Говорит владельцу, что письмо снято с отправки. Падать здесь нельзя.

    Имя контакта и текст отказа идут через esc: в названии компании живут и
    «&», и угловые скобки, а отказ почтового сервера цитирует адрес целиком.
    Без экранирования Telegram отвергает разметку, и владелец видит карточку
    без единого выделения — либо не видит её вовсе.
    """
    from . import alerts, ui

    try:
        alerts.deliver(
            store,
            alerts.LETTER_DROPPED,
            f"🗑 <b>Письмо снял с отправки</b>\n"
            f"{ui.esc(contact.name)} — адрес не принял письмо {attempts} раза подряд.\n"
            f"Причина последнего отказа: {ui.esc(str(exc))}\n\n"
            f"Дальше стучаться нельзя: почтовый сервер считает это перебором "
            f"и блокирует наш ящик целиком."
        )
    except Exception as send_exc:  # noqa: BLE001 — не смогли сказать, но живём дальше
        logger.warning("Не отправилось сообщение о снятом письме: {}", send_exc)


def _after_send(
    store: Store, draft_pk: int, contact: Contact, how: str, *, by_mail: bool = True
) -> None:
    """Общий хвост отправки: пометить, посчитать, подвинуть стадию, записать.

    by_mail — ушло ли это письмо ИЗ НАШЕГО ЯЩИКА. Телеграм отправляется руками
    владельца и к ящику отношения не имеет.
    """
    store.mark_sent(draft_pk)
    store.add_send(contact.kind, by_mail=by_mail)
    # Прогрев отсчитывается отсюда: первое ушедшее письмо — и есть первый день.
    #
    # Именно письмо. Раньше часы прогрева запускало и нажатие «Отправил» в
    # телеграме — а телеграм уходит с личного аккаунта владельца, из ящика при
    # этом не ушло ни одного письма. Ящик «взрослел» на бумаге, суточный лимит
    # рос, и первое же настоящее письмо отправлялось с потолком, заслуженным
    # чужим каналом. Ради этого прогрев и существует, так что считать надо
    # строго свои отправки.
    if by_mail and not store.get_state(WARMUP_KEY):
        store.set_state(WARMUP_KEY, today_msk())
    if contact.pk is not None:
        # Стадию двигаем только вперёд по холодной части воронки. Пока письмо
        # шло, контакт мог успеть ответить (REPLIED) или попросить не писать
        # (DEAD) — разбор входящих работает параллельно. Безусловный WRITTEN
        # молча откатывал такое, и цепочка касаний продолжала писать человеку,
        # который уже ответил или отказался.
        fresh = store.get_contact(contact.pk)
        if fresh is not None and fresh.stage in (Stage.NEW, Stage.QUEUED, Stage.WRITTEN):
            store.set_stage(contact.pk, Stage.WRITTEN, f"написали ({how})")
        store.log_event(contact.pk, "sent", how)


def _ascii_address(address: str) -> str:
    """Адрес, который примет любой SMTP-сервер: домен переводим в punycode.

    «info@компания.рф» — легальный адрес, и проверка его пропускает намеренно.
    Но smtplib отдаёт его серверу как есть, а сервер без SMTPUTF8 (наш Beget —
    без) отвечает отказом ВСЕГДА. Отказ этот классифицируется как «наша
    сторона», попытки не считаются — письмо зависало в очереди навсегда и
    каждые пару минут заново логинилось в SMTP: ровно тот перебор
    авторизаций, за который хостер блокирует ящик целиком.

    Домен punycode решает: «компания.рф» → «xn--….xn--p1ai», это тот же ящик.
    Кириллица ДО собаки punycode-ом не лечится — такой адрес отклоняем сразу
    как негодный, чтобы он честно ушёл в отказы адресата, а не в вечный цикл.
    """
    local, _, domain = address.partition("@")
    try:
        local.encode("ascii")
    except UnicodeEncodeError:
        raise smtplib.SMTPRecipientsRefused({
            address: (553, b"non-ascii local part needs SMTPUTF8"),
        }) from None
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise smtplib.SMTPRecipientsRefused({address: (553, b"bad domain")}) from None
    return f"{local}@{domain}"


def deliver_email(to_address: str, subject: str, body: str) -> None:
    """Собственно отправка. Порт 465 = SSL, остальные = STARTTLS."""
    settings = get_settings()
    if not settings.smtp_ready:
        raise RuntimeError("почта не настроена")

    message = EmailMessage()
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_user or ""))
    message["To"] = _ascii_address(to_address)
    message["Subject"] = subject or "Вопрос по сотрудничеству"
    # Date и Message-ID обязательны по RFC 5322, а мы их не ставили вовсе.
    # Письмо без даты и без идентификатора спам-фильтры считают признаком
    # рассылки, собранной на коленке: у нормального почтового клиента они есть
    # всегда. Для холодных писем это лишний минус ровно там, где он не нужен.
    message["Date"] = format_datetime(datetime.now(MSK))
    message["Message-ID"] = make_msgid(domain=(settings.smtp_user or "@uniqore.ru").split("@")[-1])
    message.set_content(body)

    _hand_over(message)
    logger.info("Письмо отправлено на {}", to_address)
    _keep_a_copy(message)


def _hand_over(message: EmailMessage) -> None:
    """Отдаёт письмо почтовому серверу.

    Ошибку поднимаем ТОЛЬКО до того, как сервер письмо принял. Всё, что
    случается после, — прощание, разорванная связь, таймаут на `quit` —
    доставки уже не отменяет.

    Раньше отправка жила в `with`, и выход из блока вызывал `quit()` сам.
    Разрыв связи на прощании поднимал исключение из уже состоявшейся
    отправки: вызывающий возвращал письмо в очередь, и на следующем круге
    получатель видел его второй раз. Отметку «отдано на почту» ставят
    заранее именно ради этого — а такое исключение её снимало.

    Разговор ведём по шагам, а не одним `send_message`, ровно из-за середины:
    после ответа «354 давай текст» письмо уже в пути, и обрыв связи на этом
    месте не значит «не дошло» — значит «мы не узнали ответа». Такой случай
    поднимает MaybeDelivered, и вызывающий не отправляет письмо заново:
    вторая копия у клиента хуже, чем ненаписанное письмо.
    """
    settings = get_settings()
    host, port = settings.smtp_host or "", settings.smtp_port
    server = (
        smtplib.SMTP_SSL(host, port, timeout=30)
        if port == 465
        else smtplib.SMTP(host, port, timeout=30)
    )
    try:
        if port != 465:
            server.starttls()
        server.login(settings.smtp_user or "", settings.smtp_password or "")
        _say_the_letter(server, message)
    except Exception:
        _close_quietly(server)
        raise
    _close_quietly(server)


class MaybeDelivered(Exception):
    """Письмо ушло на сервер, а ответа мы не дождались.

    Единственное состояние, в котором отправлять заново нельзя: сервер мог
    письмо принять и разослать. Повтор даст клиенту вторую копию, а клиент
    не отличит нашего сбоя от навязчивости.
    """


def _say_the_letter(server: smtplib.SMTP, message: EmailMessage) -> None:
    """MAIL FROM → RCPT TO → DATA, каждый шаг отдельно.

    `send_message` делает то же самое, но заворачивает всё в один вызов, и
    по исключению из него нельзя понять, где оборвалось: до текста письма
    или после. Здесь граница видна: до ответа «354» любая ошибка безопасна
    для повтора, после — нет.
    """
    from_address = parseaddr(message["From"])[1]
    to_address = parseaddr(message["To"])[1]

    server.ehlo_or_helo_if_needed()
    code, reply = server.mail(from_address)
    if code != 250:
        server.rset()
        raise smtplib.SMTPSenderRefused(code, reply, from_address)
    code, reply = server.rcpt(to_address)
    if code not in (250, 251):
        server.rset()
        raise smtplib.SMTPRecipientsRefused({to_address: (code, reply)})

    code, reply = server.docmd("DATA")
    if code != 354:
        server.rset()
        raise smtplib.SMTPDataError(code, reply)

    # Дальше пути назад нет: текст письма уходит в сокет, и сервер уже может
    # его принять. Любая ошибка отсюда — «не знаем», а не «не дошло».
    try:
        server.send(_wire_bytes(message))
        code, reply = server.getreply()
    except Exception as exc:
        raise MaybeDelivered(f"связь оборвалась после отправки текста: {exc}") from exc
    if code != 250:
        raise smtplib.SMTPDataError(code, reply)


def _wire_bytes(message: EmailMessage) -> bytes:
    """Письмо в том виде, в каком его ждёт SMTP после «354».

    Переводы строк — CRLF, точка в начале строки удваивается (иначе строка
    «.» посреди письма обрубит его на этом месте), в конце — точка отдельной
    строкой. Ровно это делает smtplib внутри `data()`.
    """
    buffer = io.BytesIO()
    BytesGenerator(buffer, policy=message.policy.clone(linesep="\r\n")).flatten(message)
    # Та же замена, что делает smtplib внутри sendmail: точка в начале ЛЮБОЙ
    # строки, а не только после CRLF. Своя, более узкая замена однажды
    # пропустила бы строку, разделённую одним переводом строки, и сервер
    # обрубил бы письмо на этом месте.
    body = re.sub(rb"(?m)^\.", b"..", buffer.getvalue())
    return body + b"\r\n.\r\n"


def _close_quietly(server: smtplib.SMTP) -> None:
    """Прощается с сервером. Письмо уже принято — молчать об ошибке можно."""
    import contextlib

    try:
        server.quit()
    except Exception as exc:  # noqa: BLE001 — доставку это не отменяет
        logger.debug("Почтовый сервер не попрощался: {}", exc)
        with contextlib.suppress(Exception):  # сокет уже мёртв, закрывать нечего
            server.close()


def _keep_a_copy(message: EmailMessage) -> None:
    """Кладёт копию письма в «Отправленные» почтового ящика.

    Прямая отправка через SMTP копию туда не кладёт: папку заполняет почтовый
    клиент, когда человек пишет руками. Из-за этого владелец открывал ящик и
    не находил там ни одного письма, отправленного ботом, — письма уходили
    по-настоящему, а следа не оставалось, и проверить их было негде.

    Молча падать здесь нельзя, но и ронять отправку тоже: письмо уже ушло,
    и неудачная копия этого не отменяет.
    """
    settings = get_settings()
    if not settings.imap_ready:
        return

    import imaplib
    import time

    try:
        with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30) as box:
            box.login(settings.smtp_user or "", settings.smtp_password or "")
            folder = _sent_folder(box)
            if not folder:
                logger.warning("Не нашёл папку «Отправленные» — копию письма не сохранил")
                return
            box.append(
                folder, "\\Seen", imaplib.Time2Internaldate(time.time()), message.as_bytes()
            )
    except Exception as exc:  # noqa: BLE001 — письмо уже ушло, копия не критична
        logger.warning("Копия письма в «Отправленные» не легла: {}", exc)


# Как папка «Отправленные» называется у разных почтовых служб. Beget и Яндекс
# зовут её по-разному, а некоторые отдают её только с флагом \Sent.
SENT_FOLDER_NAMES = ("Sent", "INBOX.Sent", "Отправленные", "INBOX.Отправленные", "Sent Items")


def _sent_folder(box) -> str | None:
    """Ищет папку «Отправленные» в ящике. None — не нашлась."""
    try:
        status, data = box.list()
    except Exception:  # noqa: BLE001 — не смогли спросить, копию не кладём
        return None
    if status != "OK":
        return None

    known = []
    for raw in data or []:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        # Строка LIST: (флаги) "разделитель" имя. Имя может быть в кавычках и
        # с пробелом («Sent Items») — резать по последнему пробелу нельзя, от
        # «"Sent Items"» оставалось «Items"», и копии писем молча не
        # сохранялись. Берём имя как всё после разделителя, снимая кавычки.
        match = re.search(r'(?:"[^"]*"|NIL)\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>\S+))\s*$', line)
        if not match:
            continue
        name = match.group("quoted") or match.group("bare") or ""
        # Сервер, поддерживающий SPECIAL-USE, сам помечает нужную папку.
        if "\\Sent" in line:
            return name
        known.append(name)

    for name in SENT_FOLDER_NAMES:
        if name in known:
            return name
    return None


# --- Фоновая отправка ---------------------------------------------------------

# Пауза между письмами. Двадцать писем, ушедших за одну секунду, — первый
# признак рассылки и для получателя, и для спам-фильтра. Разброс нужен затем,
# что ровный интервал выдаёт машину не хуже мгновенной пачки.
PAUSE_RANGE = (60.0, 120.0)
# Как часто отправщик просыпается, когда отправлять нечего.
IDLE_PAUSE = 30.0


def approve_queue(
    store: Store,
    kind: Kind,
    limit: int | None = None,
    channel: Channel | None = None,
) -> int:
    """Ставит очередь на отправку. Возвращает, сколько писем поставлено.

    channel — фильтр: можно отправить только почту или только телеграм.
    Больше суточного остатка не берём: смысла ставить в очередь то, что всё
    равно упрётся в лимит, нет — письмо просто провисит до завтра с ошибкой.

    Остаток считает letters_left_today — та же арифметика, что у проверки
    перед самой отправкой. Разойдутся — очередь снова начнёт молча одобрять
    ноль писем там, где отправка ничего против не имеет.
    """
    # Вычитаем уже одобренные: они займут суточную норму, как только их
    # заберёт отправщик. Без этого каждое нажатие «Отправить все» одобряло
    # ещё столько же — три нажатия давали трёхкратную норму, а потом кнопка
    # переставала делать что-либо: свободных писем в очереди уже не было.
    left = max(0, letters_left_today(store, kind) - store.approved_count(kind))
    if limit is not None:
        left = min(left, limit)
    if left <= 0:
        return 0

    approved = 0
    for draft, _contact in store.queue(kind, limit=left * 3, channel=channel):
        if approved >= left:
            break
        if draft.pk and not draft.approved and store.approve_draft(draft.pk):
            approved += 1
    return approved


def send_next(store: Store, kind: Kind) -> SendResult | None:
    """Отправляет одно одобренное письмо. None — отправлять нечего.

    Только почта. Сообщение в телеграм бот отправить не может, и такой
    черновик остаётся одобренным навсегда: стоя первым в очереди, он
    останавливал всю рассылку — отправщик каждые полминуты «отправлял» его
    заново, а живые письма не уходили вовсе.
    """
    approved = store.approved_drafts(kind, limit=1, channel=Channel.EMAIL)
    if not approved:
        return None
    draft, _contact = approved[0]
    return send(store, draft.pk or 0)


def sender_loop(db_path, stop_event=None) -> None:
    """Фоновая отправка: по одному письму с паузой между ними.

    Живёт рядом с ботом и просыпается сам. Всё, что решает, сколько писем
    уйдёт, — суточный лимит и то, что владелец поставил в очередь; здесь
    только темп.
    """
    import time

    store = Store(db_path)
    # Письма, занятые упавшим процессом, иначе остались бы «отправляемыми»
    # навсегда и не ушли бы никогда.
    _report_interrupted(store)
    logger.info("Отправщик писем запущен")
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                sent_any = _sender_round(store)
            except Exception:  # noqa: BLE001 — один сбой не должен убивать поток
                # Раньше try стоял снаружи цикла: любое неожиданное исключение
                # заканчивало отправку насовсем, а бот при этом жил и молчал.
                logger.exception("Круг отправки не удался")
                sent_any = False
            if not sent_any:
                time.sleep(IDLE_PAUSE)
    finally:
        store.close()


def _sender_round(store: Store) -> bool:
    """Один проход по обеим вкладкам. True — что-то отправляли."""
    import random
    import time

    sent_any = False
    for kind in (Kind.PARTNER, Kind.CLIENT):
        # Каждый канал включается отдельно. Телеграм сам отправить нельзя
        # (бот не пишет первым), поэтому в автоочередь он не попадает вовсе.
        for channel, key in AUTOSEND_KEYS.items():
            ready, _why = channel_ready(channel)
            if ready and autosend_on(store, channel):
                approve_queue(store, kind, channel=channel)
            elif store.get_state(key) == "1" and not ready:
                logger.debug("{}: автоотправка включена, но канал не готов", channel.value)

        result = send_next(store, kind)
        if result is None:
            continue
        sent_any = True
        logger.info("{}: {}", kind.value, result.reason)
        # Пауза нужна и после неудачи. Без неё мёртвый адрес превращался
        # в непрерывный поток подключений к почтовому серверу.
        time.sleep(random.uniform(*PAUSE_RANGE))
    return sent_any


# Сколько первых строк ответа читаем в поисках отказа. Человек здоровается, а
# просьбу отстать пишет второй строкой — раньше такой ответ не ловился вовсе,
# и мы продолжали писать тому, кто попросил перестать.
#
# Пять, а не три: вежливый отказ идёт после приветствия и слов «спасибо за
# предложение», и на трёх строках он не помещался. Дальше не идём — там уже
# начинается разговор, и «не пишите в эту ветку» отказом от нас не является.
STOP_LINES = 5

# Слова из STOP_WORDS — только целиком и только в начале строки. По голому
# startswith «Стопроцентно интересно, давайте обсудим» начиналось на «стоп», и
# самый горячий ответ, какой вообще бывает, закрывал контакт навсегда.
_STOP_START = re.compile(r"^(?:" + "|".join(re.escape(word) for word in STOP_WORDS) + r")\b", re.IGNORECASE)


# Сбои нашей стороны: пароль, сеть, наш собственный почтовый сервер. Адресат к
# ним отношения не имеет, и записывать их ему в счёт нельзя — иначе одна
# опечатка в пароле за три круга хоронит всю очередь, каждое письмо навсегда.
_OUR_SMTP = (
    smtplib.SMTPAuthenticationError,   # неверный пароль в .env
    smtplib.SMTPConnectError,          # до сервера не достучались
    smtplib.SMTPServerDisconnected,    # оборвал соединение
    smtplib.SMTPHeloError,             # не принял приветствие
    smtplib.SMTPNotSupportedError,
    smtplib.SMTPSenderRefused,         # наш ящик, а не адрес получателя
)
# А это уже про адресата: его сервер отказался принимать письмо.
_THEIR_SMTP = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPDataError,
)


def _address_was_refused(exc: Exception) -> bool:
    """Отказ адресату, а не письму.

    «Нет такого ящика» приходит на RCPT TO и относится к адресу — такой адрес
    вычёркиваем. Отказ после DATA относится к ПИСЬМУ: спам-фильтр не принял
    формулировку, ссылку или домен отправителя, а сам ящик живой. Путать их
    значит терять компанию за то, в чём она не виновата.
    """
    return isinstance(exc, smtplib.SMTPRecipientsRefused)


def wait_and_retry(exc: Exception) -> bool:
    """Отказ временный: сервер получателя просит зайти позже.

    Код 4xx по RFC 5321 означает ровно это — «сейчас не могу, повтори».
    Самый частый его повод — грейлистинг: почтовый сервер отклоняет ПЕРВОЕ
    письмо с незнакомого адреса и ждёт, что настоящий отправитель повторит
    через несколько минут, а рассылочный скрипт не повторит. То есть отказ
    получают все нормальные отправители, и мы в том числе.

    Раньше такой отказ шёл адресату в счёт наравне с «нет такого ящика»:
    три круга отправщика укладывались в те самые несколько минут, письмо
    снималось навсегда, а владельцу сообщалось, что адрес не принимает почту
    третий раз подряд. Живой адресат терялся ровно потому, что вёл себя как
    положено.
    """
    codes: list[int] = []
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _text in (exc.recipients or {}).values()]
    elif isinstance(exc, smtplib.SMTPResponseException):
        codes = [exc.smtp_code]
    # Пусто — судить не о чем, и «временный» тут не заявляем: молчим в пользу
    # прежнего разбора, а не выдумываем повод не считать попытку.
    return bool(codes) and all(400 <= code < 500 for code in codes)


def _our_side(exc: Exception) -> bool:
    """Виноваты мы, а не адрес. Тогда попытку адресату не засчитываем.

    Порядок проверок важен: `smtplib.SMTPException` наследуется от `OSError`,
    и «поймать OSError» означало бы записать в свою сторону в том числе отказ
    получателя.
    """
    if isinstance(exc, _THEIR_SMTP):
        return False
    if isinstance(exc, _OUR_SMTP):
        return True
    if isinstance(exc, smtplib.SMTPException):
        # Незнакомая ошибка протокола: судить о ней нечем, и адресу она в счёт
        # не идёт. Молчим в его пользу — письмо просто ждёт следующего раза.
        return True
    # Всё остальное (обрыв сети, DNS, таймаут) — это сеть, а не адрес.
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def looks_like_stop(text: str) -> bool:
    """Ответ «стоп» и его варианты. Такой контакт закрываем навсегда.

    Смотрим начало каждой из первых строк, а не только самое первое слово
    письма. Ошибиться в сторону «замолчали» здесь дешевле, чем в другую:
    закрытый по ошибке контакт владелец видит в уведомлении и может вернуть,
    а жалобу на спам вернуть нельзя.
    """
    lines = [line.strip().lower() for line in (text or "").splitlines()]
    for line in [line for line in lines if line][:STOP_LINES]:
        # Цитату нашего же письма не читаем: там наша подпись и наш текст.
        if line.startswith(">"):
            continue
        if _STOP_START.match(line):
            return True
        if any(phrase in line for phrase in STOP_PHRASES):
            return True
    return False

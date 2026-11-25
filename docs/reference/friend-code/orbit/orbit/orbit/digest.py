"""Воронка в цифрах и вечерняя сводка.

Три системы работают круглосуточно, и без цифр невозможно понять, что из
этого приносит разговоры, а что просто тратит деньги и время. Здесь всё
собирается в одном месте: собрали → отсеяли → написали → ответили → созвон
→ сделка, плюс сколько стоил один ответ.

Данные берём из двух баз: своей (партнёры и клиенты) и биржевой (отклики).
Если биржа недоступна, сводка всё равно уходит — просто без её части.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from .models import STAGE_TITLES, Kind, Stage
from .schedule import MODEL_STATE
from .store import Store, today_msk
from .ui import clip, esc

# Стадии, которые показываем в воронке, и в каком порядке.
FUNNEL_STAGES: tuple[Stage, ...] = (
    Stage.NEW, Stage.QUEUED, Stage.WRITTEN, Stage.REPLIED, Stage.CALL, Stage.DEAL,
)

# Как называются события в сводке.
EVENT_TITLES = {
    "sent": "писем ушло",
    "reply": "ответов на письма",
    "call": "звонков",
    "bounce": "вернулось писем",
    "followup": "писем вдогонку",
    "site_scan": "нашли контактов на сайтах",
    "autoreply": "автоответов",
    "stale": "текстов протухло и переписано",
}


@dataclass
class Side:
    """Одна система в воронке."""

    title: str
    stages: dict[Stage, int] = field(default_factory=dict)
    note: str = ""

    @property
    def total(self) -> int:
        return sum(self.stages.values())


def _side(store: Store, kind: Kind, title: str) -> Side:
    counts = store.stage_counts(kind)
    return Side(title=title, stages={stage: counts.get(stage, 0) for stage in Stage})


def exchange_connected() -> bool:
    """Подключена ли вообще система бирж.

    «Не подключена» и «подключена, но не отвечает» — разные новости, и путать
    их нельзя. О первой говорить нечего: у владельца просто нет этой системы.
    Вторая означает, что мы ослепли, и молчать о ней — значит выдать поломку
    за спокойный вечер.
    """
    try:
        import fbid.stats  # noqa: F401
    except ImportError:
        return False
    return True


def exchange_funnel(days: int = 7):
    """Воронка биржи. None — если система не подключена или не отвечает."""
    try:
        from fbid.config import get_settings as fbid_settings
        from fbid.stats import collect
        from fbid.store import Store as FbidStore
    except ImportError:
        return None

    try:
        store = FbidStore(fbid_settings().db_path)
    except Exception as exc:  # noqa: BLE001 — база биржи может быть недоступна
        logger.warning("Биржа не отвечает: {}", exc)
        return None
    try:
        return collect(store, days=days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Воронку биржи посчитать не вышло: {}", exc)
        return None
    finally:
        store.close()


def funnel_text(store: Store, *, days: int = 7) -> str:
    """Экран воронки: три системы рядом.

    Подписи разные, и это не небрежность. У партнёров и клиентов мы показываем
    СНИМОК: где контакты стоят прямо сейчас. У биржи — период: сколько заказов
    просмотрено и сколько откликов ушло за N дней. Заголовок был общий, «за N
    дней», и это была неправда для двух третей экрана: кнопка «За 30 дней» их
    цифры не меняла вовсе, и владелец видел одни и те же числа, думая, что
    смотрит разные периоды.
    """
    lines = ["<b>📊 Воронка</b>", ""]

    for kind, title in ((Kind.PARTNER, "👥 Партнёры"), (Kind.CLIENT, "🏢 Клиенты")):
        side = _side(store, kind, title)
        if not side.total:
            continue
        lines.append(f"<b>{title}</b> · сейчас в базе {side.total}")
        row = " · ".join(
            f"{STAGE_TITLES[stage].lower()} {side.stages.get(stage, 0)}"
            for stage in FUNNEL_STAGES
            if side.stages.get(stage)
        )
        lines.append(row or "движения пока нет")
        # Отказы показываем отдельно, иначе сумма по стадиям не сходится
        # с «всего», и это выглядит как ошибка в цифрах.
        dead = side.stages.get(Stage.DEAD, 0)
        if dead:
            lines.append(f"не подошли: {dead}")
        lines.append("")

    exchange = exchange_funnel(days)
    if exchange is None:
        lines.append("<b>💼 Фриланс</b>\nБиржа не отвечает — цифр по ней нет.")
    else:
        lines.append(f"<b>💼 Фриланс · за {days} дней</b> · просмотрено {exchange.seen}")
        lines.append(
            f"отсеяно {exchange.rejected} · откликнулись {exchange.sent} · "
            f"ответили {exchange.replies}"
        )
        if exchange.dropped_early:
            # Без этой строки цифры не сходятся, и это выглядит как потеря
            # заказов: «просмотрено 1110, отсеяно 228» оставляет без ответа
            # вопрос про остальные восемьсот.
            lines.append(
                f"сразу мимо по правилам: {exchange.dropped_early} "
                f"(не наш профиль, вакансии, копейки)"
            )
        if exchange.sent:
            lines.append(f"отвечают на {exchange.reply_rate:.0f}% откликов")
        if exchange.median_speed is not None:
            # Скорость отклика решает на биржах больше, чем текст: первые
            # откликнувшиеся получают кратно больше просмотров.
            lines.append(
                f"откликаемся за {exchange.median_speed:.0f} мин · "
                f"в первые 5 минут {exchange.fast_share:.0f}%"
            )
        if exchange.spent:
            one = (
                f", один ответ {exchange.cost_per_reply:.0f} ₽"
                if exchange.cost_per_reply
                else ""
            )
            lines.append(f"потрачено {exchange.spent:.0f} ₽{one}")
        if exchange.reasons:
            lines.append("")
            lines.append("<b>Почему отсеивались:</b>")
            # Причины приходят из базы биржи как есть — экранируем.
            lines += [f"· {count} — {esc(reason)}" for reason, count in exchange.reasons[:5]]
        if exchange.our_failures:
            # Отдельно и другими словами: это не сработавший фильтр, а заказы,
            # которые мы упустили сами. В общем списке они читались как
            # «отсеяли правильно» — и молчащая модель выглядела нормой.
            lines.append("")
            lines.append("<b>Упустили по нашей вине:</b>")
            lines += [f"· {count} — {esc(reason)}" for reason, count in exchange.our_failures[:4]]

    return clip("\n".join(lines))


def build(store: Store, *, day: str | None = None) -> str | None:
    """Вечерняя сводка за сегодня. None — если сегодня ничего не происходило."""
    when = day or today_msk()
    events = store.event_counts(when)

    exchange = exchange_funnel(days=1)
    bids = exchange.sent if exchange else 0
    spent = exchange.spent if exchange else 0.0
    replies = exchange.replies if exchange else 0

    # Напоминания считаем до раннего выхода. Человек попросил вернуться именно
    # сегодня, и «сегодня ничего не происходило» — не причина промолчать: без
    # этой строки срок, назначенный на тихий день, пропадал совсем.
    reminders = _reminders(store, when)

    # Молчащая модель — главная причина тихого дня, и именно её сводка обязана
    # назвать вслух. Без модели не пишутся письма, не разбираются ответы и не
    # уходят отклики: цифры при этом честные нули, «ничего не происходило»
    # формально правда, и сводка просто не уходила. День, когда всё встало,
    # выглядел точно как день, когда просто не было заказов.
    #
    # Берём записанное состояние, а не спрашиваем модель заново: спрашивает её
    # расписание раз в полчаса, и платить за второй запрос ради строки в
    # сводке незачем.
    model_down = store.get_state(MODEL_STATE)

    # Просмотренные заказы — тоже событие дня. Считался только отправленный
    # отклик, и день, в который биржа перебрала двести заказов и не отправила
    # ни одного, выглядел как «ничего не происходило»: сводка не уходила
    # вовсе. А это ровно тот день, о котором надо знать — значит всё отсеяли
    # фильтры, кончились деньги или встал автоотклик.
    # Слетевший вход спрашиваем у вкладки биржи — тем же вопросом, что и
    # главный экран, чтобы ответы не разъезжались.
    from .sections.freelance import (
        down_sources_now,
        lost_sessions_now,
        platform_title,
        quiet_notes_now,
    )

    lost_sessions = lost_sessions_now()
    down_sources = down_sources_now()

    # Всё, о чём решили не дёргать владельца в ту же секунду. День без единого
    # события, но с тремя снятыми адресами — не тихий день, и сводка обязана
    # уйти: иначе тихий канал превращается в яму, куда события проваливаются.
    quiet = store.quiet_notes(when)
    # Биржа — чужая система со своей базой, и правило про неё общее: она не
    # роняет то, что живёт без неё. Сводка уходит и без биржевых строк.
    try:
        quiet += quiet_notes_now(when)
    except Exception as exc:  # noqa: BLE001 — сводка важнее её биржевой части
        logger.warning("Сводка: отложенные строки с бирж не прочитались: {}", exc)

    happened = sum(events.values()) + bids + (exchange.seen if exchange else 0)
    # Недоступная биржа — это тоже новость, и молчать о ней нельзя. Раньше её
    # цифры превращались в нули, «ничего не произошло» становилось правдой, и
    # сводка не уходила вовсе — вместе со строкой «биржа не отвечает». Тихий
    # вечер выглядел одинаково и когда всё в порядке, и когда система ослепла.
    if (
        not happened
        and not reminders
        and not model_down
        and not lost_sessions
        and not down_sources
        and not quiet
        and not (exchange is None and exchange_connected())
    ):
        return None

    lines = [f"<b>🌙 Итоги дня · {when}</b>", ""]
    if model_down:
        lines += [
            f"🛑 <b>Модель молчит: {esc(model_down)}</b>",
            "Пока это так, письма не пишутся, ответы не разбираются и отклики "
            "не уходят — ключ на все три системы один.",
            "",
        ]

    letters = []
    for kind, title in EVENT_TITLES.items():
        count = events.get(kind, 0)
        if count:
            letters.append(f"· {title}: {count}")
    if letters:
        lines.append("<b>Партнёры и клиенты</b>")
        lines += letters
        lines.append("")

    lines += _named_lists(store, when)

    lines.append("<b>Биржи</b>")
    for platform in down_sources:
        # Ноль откликов сам себя не объясняет. Восемь вечеров подряд сводка
        # писала «откликов отправлено: 0» — и читалась как тихий день, хотя
        # площадка не отвечала вовсе из-за мёртвого прокси. Цифра, которая не
        # называет причину, хуже её отсутствия: она успокаивает.
        lines.append(
            f"· 🛑 {esc(platform_title(platform))} не отвечает — сбор и отклики оттуда встали, "
            f"чаще всего это прокси: <code>fbid check-proxy</code>"
        )
    for platform in lost_sessions:
        # Слетевший вход — это не «сегодня было тихо», а «сегодня туда не ушло
        # ни одного отклика». Главный экран об этом говорил, сводка молчала —
        # а читают вечером именно её, не открывая бота.
        lines.append(
            f"· 🛑 вход на {esc(platform_title(platform))} слетел — нужна команда "
            f"<code>fbid login {esc(platform)}</code>"
        )
    if exchange is None:
        lines.append("· биржа не отвечает")
    else:
        lines.append(f"· заказов просмотрено: {exchange.seen}")
        lines.append(f"· откликов отправлено: {bids}")
        if spent:
            lines.append(f"· потрачено: {spent:.0f} ₽")
        lines.append(f"· ответов от клиентов: {replies}")
        # Кто вёл переписку — вопрос вечернего разбора: по нему видно, стоит
        # ли двигать порог. Без этой строки владелец не знает, почему одних
        # клиентов бот довёл сам, а других отдал ему.
        mode_line = _reply_mode_line()
        if mode_line:
            lines.append(mode_line)

    if quiet:
        # Заголовок именно такой: владелец должен понимать, что эти строки
        # НЕ приходили ему днём, и это было решением, а не сбоем связи.
        lines += ["", "<b>Без сообщений за день</b>"]
        lines += [_one_line(note) for note in quiet[:QUIET_LIMIT]]
        if len(quiet) > QUIET_LIMIT:
            lines.append(f"· …и ещё {len(quiet) - QUIET_LIMIT}")

    queues = [
        f"{title} {store.queue_size(kind)}"
        for kind, title in ((Kind.PARTNER, "партнёрам"), (Kind.CLIENT, "клиентам"))
        if store.queue_size(kind)
    ]
    if queues:
        lines += ["", f"<b>Ждут отправки:</b> {', '.join(queues)}"]

    # Числовой строки «Ждут ответа: N» больше нет — тех же людей сводка
    # называет по именам в блоке «Молчат», это полезнее цифры.
    lines += reminders
    # Режем и здесь: сводка уходит и в Телеграм, и на экран по кнопке «Итоги
    # дня». На базе с сотней событий она перерастала лимит, и экран переставал
    # открываться вовсе — нажатие выглядело как сломанная кнопка.
    return clip("\n".join(lines))


# Сколько отложенных строк показываем. Каждая из них — целая карточка в
# несколько предложений; вываливать их в сводку десятками значит утопить всё
# остальное. Полный список всегда виден в карточке контакта.
QUIET_LIMIT = 10


def _one_line(note: str) -> str:
    """Схлопывает многострочную карточку в одну строку сводки.

    Карточки писались, чтобы прийти в телеграм поодиночке: заголовок, имя,
    причина, что делать дальше. В сводке их десяток подряд, и в прежнем виде
    они занимали бы два экрана. Берём первые две строки — заголовок и имя.
    """
    parts = [part.strip() for part in note.splitlines() if part.strip()]
    head = " — ".join(parts[:2]) if parts else ""
    return f"· {head}" if head else "· (пусто)"


# Сколько имён показывать в каждом списке сводки. Дальше — «и ещё N»: сводка
# читается с телефона, и полсотни строк превращают её в простыню.
NAMES_LIMIT = 12


def _named_lists(store: Store, when: str) -> list[str]:
    """Именные списки для сводки: кому написали, кто ответил, кто молчит.

    Решение владельца от 22.08.2026: цифры «писем ушло: 6» не говорят, с кем
    именно завтра продолжать разговор. Имя + давность — говорят.
    """
    lines: list[str] = []

    sent_today = store.day_event_contacts("sent", when)
    if sent_today:
        lines.append("<b>Кому написали сегодня</b>")
        for name, note in sent_today[:NAMES_LIMIT]:
            how = f" ({esc(note)})" if note else ""
            lines.append(f"· {esc(name)}{how}")
        if len(sent_today) > NAMES_LIMIT:
            lines.append(f"· … и ещё {len(sent_today) - NAMES_LIMIT}")
        lines.append("")

    replied_today = store.day_event_contacts("reply", when)
    if replied_today:
        lines.append("<b>Ответили сегодня — ждут вас</b>")
        for name, _note in replied_today[:NAMES_LIMIT]:
            lines.append(f"· {esc(name)}")
        # Хвост «и ещё N» был у соседних списков, а у этого — нет. Именно у
        # этого он нужнее всех: здесь люди, которые ждут ответа. Тринадцатый
        # ответивший просто не существовал для владельца.
        if len(replied_today) > NAMES_LIMIT:
            lines.append(f"· … и ещё {len(replied_today) - NAMES_LIMIT}")
        lines.append("")

    waiting = store.waiting_contacts()
    if waiting:
        lines.append("<b>Молчат</b>")
        for name, since in waiting[:NAMES_LIMIT]:
            day_part = f" — с {since[8:10]}.{since[5:7]}" if len(since) == 10 else ""
            lines.append(f"· {esc(name)}{day_part}")
        if len(waiting) > NAMES_LIMIT:
            lines.append(f"· … и ещё {len(waiting) - NAMES_LIMIT}")
        lines.append("")

    return lines


def _reply_mode_line() -> str:
    """Строка про режим переписки на биржах. Пусто — биржа недоступна."""
    try:
        from fbid import dialog, limits
        from fbid.config import get_settings as fbid_settings
        from fbid.store import Store as FbidStore
    except ImportError:
        return ""
    try:
        store = FbidStore(fbid_settings().db_path)
    except Exception as exc:  # noqa: BLE001 — база биржи может быть недоступна
        logger.warning("Режим переписки не прочитался: {}", exc)
        return ""
    try:
        if dialog.mode(store) != dialog.MODE_AUTO:
            return "· переписку веду не я — все ответы за вами"
        ceiling = limits.get("auto_reply_max_price", store)
        return f"· сам отвечаю по заказам до {ceiling:.0f} ₽, дороже — зову вас"
    except Exception as exc:  # noqa: BLE001 — строка сводки не должна её ронять
        logger.warning("Режим переписки не прочитался: {}", exc)
        return ""
    finally:
        store.close()


def _reminders(store: Store, when: str) -> list[str]:
    """Кто просил вернуться позже, и этот «позже» настал.

    Само по себе «вернитесь осенью» теряется: контакт стоит в «Начали
    общение» и ничем не отличается от того, с кем разговор идёт прямо сейчас.
    Поэтому срок всплывает в сводке того дня, на который его назначили.
    """
    due = store.due_reminders(when)
    if not due:
        return []

    lines = ["", f"<b>🕓 Просили вернуться — {len(due)}</b>"]
    for contact in due[:10]:
        note = str(contact.meta.get("remind_reason") or "").strip()
        # Слова человека из его письма — экранируем.
        tail = f" — {esc(note)}" if note else ""
        lines.append(f"· {esc(contact.name)}{tail}")
    if len(due) > 10:
        lines.append(f"…и ещё {len(due) - 10}")
    return lines

"""Воронка и деньги: что софт сделал за период.

Без этих цифр невозможно понять, работает система или впустую жжёт баланс:
сколько заказов просмотрено, сколько отсеяно и почему, сколько откликов ушло,
сколько клиентов ответило и во что это обошлось.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import BidStatus, Source
from .store import MSK, SENT_AT, Store


@dataclass
class Funnel:
    """Путь заказа: увидели → отсеяли → откликнулись → получили ответ."""

    seen: int = 0
    rejected: int = 0
    skipped: int = 0
    unavailable: int = 0
    dry_run: int = 0
    sent: int = 0
    failed: int = 0
    replies: int = 0
    spent: float = 0.0
    reasons: list[tuple[str, int]] = field(default_factory=list)
    # Отдельно — то, где виноваты мы, а не заказ: молчащая модель, слетевшая
    # сессия, непрочитанный баланс. Это потерянные заказы, а не отработавший
    # фильтр, и мешать их в один список нельзя.
    our_failures: list[tuple[str, int]] = field(default_factory=list)
    # Сколько минут проходит от появления заказа до нашего отклика.
    # На биржах это главная переменная: первые откликнувшиеся получают
    # кратно больше просмотров, а через час заказ уже разобран.
    speed_minutes: list[float] = field(default_factory=list)
    # Сколько заказов без решения ещё ждут своей очереди. Это не «мимо»,
    # а «руки не дошли»: очередь разберётся, как только освободится лимит.
    waiting: int = 0

    @property
    def median_speed(self) -> float | None:
        """Медиана задержки отклика. Медиана, а не среднее: один заказ,
        пролежавший до вечернего добора, перекашивает среднее."""
        if not self.speed_minutes:
            return None
        values = sorted(self.speed_minutes)
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2

    @property
    def fast_share(self) -> float:
        """Доля откликов, ушедших в первые пять минут."""
        if not self.speed_minutes:
            return 0.0
        return sum(1 for value in self.speed_minutes if value <= 5) / len(self.speed_minutes) * 100

    @property
    def decided(self) -> int:
        """По скольким заказам решение вообще принималось и записано."""
        return (
            self.rejected + self.skipped + self.unavailable
            + self.dry_run + self.sent + self.failed
        )

    @property
    def dropped_early(self) -> int:
        """Заказы, по которым в базе нет записи о решении.

        Их большинство: вакансии, дизайн, тексты, копейки. Строку в bids они
        не заводят намеренно — нет смысла хранить решение по заказу, который
        отсекается по категории за микросекунду.

        Но в отчёте молчать о них нельзя. «Просмотрено 1110, отсеяно 228,
        откликнулись 2» читается как «а где ещё восемьсот?» — и владелец не
        может отличить нормальную работу фильтра от потерянных заказов.

        Дешёвый фильтр — не единственная причина попасть сюда. Заказ мог
        пройти фильтр и стоять в очереди, а мог выпасть из окна свежести,
        пока проход до него не дошёл. Поэтому строка в отчёте называется
        «мимо без решения», а не «сразу мимо по правилам», как раньше:
        отчитываться о нашей неуспевшей очереди как об отработавшем фильтре
        значит прятать единственный случай, где что-то делать надо.

        Часы у двух слагаемых разные, и это осознанно: заказы считаются по
        появлению, отправленные отклики — по отправке. Заказ, увиденный
        вечером и отправленный утром, попадёт в «просмотрено» вчера, а в
        «отправлено» сегодня, и эта цифра на пару штук уплывёт. Иначе
        расходились бы деньги с откликами, а это заметно куда сильнее:
        «откликов 0 · потрачено 491 ₽». max(0, …) держит цифру осмысленной.
        """
        return max(0, self.seen - self.decided)

    @property
    def reply_rate(self) -> float:
        """Доля откликов, на которые ответил клиент. Главная метрика качества."""
        return (self.replies / self.sent * 100) if self.sent else 0.0

    @property
    def cost_per_reply(self) -> float | None:
        """Во сколько обошёлся один ответ клиента."""
        return (self.spent / self.replies) if self.replies else None


def window_start(days: int) -> str:
    """Начало окна отчёта — граница суток по Москве, в UTC как лежит в базе.

    Считаем календарными днями по Москве, а не «столько-то часов назад», и
    ровно по той же границе, по какой пишется книга расхода (см. _spent).
    Раньше эти две границы расходились: заказы и отклики брались за скользящие
    сутки, а деньги — за календарный день. Вечерняя сводка получалась про
    разные периоды сразу: «откликов 4 · потрачено 100 ₽», хотя три отклика из
    четырёх ушли вчера вечером и стоили 1200 ₽. Владелец делит одно на другое
    и получает цену отклика в разы меньше настоящей.
    """
    back = max(0, days - 1)
    midnight = (datetime.now(MSK) - timedelta(days=back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def collect(store: Store, days: int = 7, source: Source | None = None) -> Funnel:
    """Считает воронку за последние N календарных суток по Москве."""
    since = window_start(days)
    funnel = Funnel()

    where_source = " AND o.source = ?" if source else ""
    params: list = [since]
    if source:
        params.append(source.value)

    row = store.conn.execute(
        f"SELECT COUNT(*) AS n FROM orders o WHERE o.first_seen >= ?{where_source}", params
    ).fetchone()
    funnel.seen = int(row["n"])

    status_map = {
        BidStatus.REJECTED.value: "rejected",
        BidStatus.SKIPPED.value: "skipped",
        BidStatus.UNAVAILABLE.value: "unavailable",
        BidStatus.DRY_RUN.value: "dry_run",
        BidStatus.SENT.value: "sent",
        BidStatus.FAILED.value: "failed",
    }
    # Отклик попадает в окно по времени ОТПРАВКИ, а не заведения строки.
    # `created_at` — момент, когда модель написала заход; между ним и отправкой
    # заказ мог пролежать в очереди до вечера, а деньги списываются в момент
    # отправки и считаются по календарному дню списания. Из-за расхождения
    # сводка выдавала «откликов 0 · потрачено 491 ₽»: деньги за сегодня,
    # отклики — за вчера. Остальные статусы (отсеяли, пропустили, недоступен)
    # решаются именно в момент created_at, и для них он верен.
    in_window = (
        f"(CASE WHEN b.status = '{BidStatus.SENT.value}' "
        f"THEN {SENT_AT} ELSE b.created_at END) >= ?"
    )
    rows = store.conn.execute(
        f"""SELECT b.status, COUNT(*) AS n, SUM(b.cost_rub) AS cost
            FROM bids b JOIN orders o ON o.pk = b.order_pk
            WHERE {in_window}{where_source} GROUP BY b.status""",
        params,
    ).fetchall()
    for row in rows:
        attr = status_map.get(row["status"])
        if attr:
            setattr(funnel, attr, int(row["n"]))

    funnel.spent = _spent(store, days, source)

    # Топ причин отсева — по ним видно, что подкрутить в filters.yaml.
    # Группируем в Python, а не в SQL: причины содержат конкретные суммы
    # («бюджет 5000 ₽ ниже…»), и SQL считал бы каждую отдельной строкой.
    raw_reasons = store.conn.execute(
        f"""SELECT b.reason FROM bids b JOIN orders o ON o.pk = b.order_pk
            WHERE b.created_at >= ? AND b.status IN (?, ?){where_source}""",
        [since, BidStatus.REJECTED.value, BidStatus.SKIPPED.value]
        + ([source.value] if source else []),
    ).fetchall()
    grouped: dict[str, int] = {}
    ours: dict[str, int] = {}
    for row in raw_reasons:
        key = _short_reason(row["reason"])
        target = ours if is_our_failure(key) else grouped
        target[key] = target.get(key, 0) + 1
    funnel.reasons = sorted(grouped.items(), key=lambda kv: -kv[1])[:8]
    funnel.our_failures = sorted(ours.items(), key=lambda kv: -kv[1])[:5]

    # Скорость отклика: от момента, когда заказ впервые попал к нам, до
    # отправки. Считаем в Python, а не в SQL: даты лежат строками в разных
    # форматах (у ленты свой, у страницы заказа свой).
    #
    # Берём момент отправки, а не `created_at`. Строка в `bids` заводится на
    # первом же проходе по заказу (решением «отложить», «мимо»), и при отправке
    # она не переписывается. По `created_at` отложенный на вечер заказ выглядел
    # бы отвеченным за минуту, хотя отклик ушёл спустя часы — метрика скорости
    # показывала бы не нашу скорость, а скорость чтения ленты.
    #
    # Порядок именно такой. `message_sent_at` ставится при обычной отправке —
    # то есть всегда: второй фазы больше нет ни на одной площадке, и `bid_at`
    # в боевой базе пустой. Раньше он стоял первым, COALESCE проваливался
    # сразу в `created_at`, и «отложенный до вечера» заказ показывал скорость
    # в минуту вместо девятнадцати часов.
    speeds = store.conn.execute(
        f"""SELECT o.first_seen, {SENT_AT} AS sent_at
            FROM bids b JOIN orders o ON o.pk = b.order_pk
            WHERE b.status = ? AND {SENT_AT} >= ?{where_source}""",
        [BidStatus.SENT.value, since] + ([source.value] if source else []),
    ).fetchall()
    for row in speeds:
        minutes = _minutes_between(row["first_seen"], row["sent_at"])
        if minutes is not None:
            funnel.speed_minutes.append(minutes)

    # Ответы клиентов берём по подтверждённой отметке, а не по счётчику
    # сообщений. Счётчик врёт: Profi.ru дописывает под нашим откликом свой
    # блок «Стоимость от … Контакты: …», и он выглядел как ответ клиента.
    # Окно и площадка — те же, что у остальных цифр воронки. Раньше ответы
    # считались за всё время и по всем площадкам разом, и reply_rate с
    # cost_per_reply в отчёте «за сутки по Profi» делились на чужие ответы.
    # В значении отметки — дата первого ответа; старые отметки «1» даты не
    # имеют, их считаем во всех окнах: потерять ответ хуже, чем посчитать
    # старый лишний раз (таких отметок конечное число, и они вымрут сами).
    prefix = f"clientreplied:{source.value}:%" if source else "clientreplied:%"
    replies = store.conn.execute(
        """SELECT COUNT(*) AS n FROM state
           WHERE key LIKE ? AND value != '' AND (value = '1' OR value >= ?)""",
        (prefix, since),
    ).fetchone()
    funnel.replies = int(replies["n"])

    # Сколько из «мимо без решения» ещё стоит в очереди. Считаем той же
    # функцией, что рисует экран очереди: другой счёт разъедется с ним, и
    # отчёт будет спорить с кнопкой «Очередь». Ошибку глотаем — отчёт нужен
    # владельцу и без этой строки.
    with suppress(Exception):
        from . import pipeline

        funnel.waiting = min(
            len(pipeline.waiting_queue(store, limit=None)), funnel.dropped_early
        )

    return funnel


def _spent(store: Store, days: int, source: Source | None) -> float:
    """Сколько площадки списали за период — по книге расхода, а не по откликам.

    Денежных книг в базе две, и это была ошибка: суточный предохранитель
    смотрел в `spend`, а отчёт владельцу считал по `bids.cost_rub`. Разойтись
    они могут — и разошлись: 7 августа в стоимость одного отклика на FL попала
    цена нашей работы (28 000 ₽ вместо нуля), и с тех пор сводка сообщала
    «потрачено 28 491 ₽» там, где площадки списали 491 ₽.

    Правильная книга — `spend`: по ней считается суточный бюджет, то есть
    именно она решает, тратить дальше или нет. Отчёт обязан говорить о тех же
    деньгах, что и предохранитель.

    Границу суток берём по Москве, потому что по Москве же пишется столбец
    `day` при списании. Считать её по UTC — значит ночью, с полуночи до трёх,
    захватывать лишние сутки: сводка «за сегодня» показывала вчерашний расход
    вместе с сегодняшним, то есть до двух раз больше настоящего.

    И считаем `days` целыми днями, а не «столько-то суток назад». Расход
    лежит в базе по дням, отдельного часа у него нет. «За сутки» — это
    сегодняшний день, один; отсчёт на день назад прибавлял к нему вчерашний
    целиком, и вечерняя сводка показывала двойные деньги.
    """
    where = " AND source = ?" if source else ""
    back = max(0, days - 1)
    since_day = (datetime.now(MSK) - timedelta(days=back)).date().isoformat()
    params = [since_day] + ([source.value] if source else [])
    row = store.conn.execute(
        f"SELECT COALESCE(SUM(amount_rub), 0) AS total FROM spend WHERE day >= ?{where}",
        params,
    ).fetchone()
    return float(row["total"] or 0)


def _minutes_between(first: str | None, second: str | None) -> float | None:
    """Сколько минут между двумя отметками времени. None — если не разобрать."""
    if not first or not second:
        return None
    try:
        start = datetime.fromisoformat(first)
        end = datetime.fromisoformat(second)
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    minutes = (end - start).total_seconds() / 60
    return minutes if minutes >= 0 else None


# Начала причин, которые схлопываются в одну строку отчёта. Без этого
# «бюджет 5000 ₽ ниже…» и «бюджет 10000 ₽ ниже…» считались бы разными
# причинами, и в отчёте была бы каша из полусотни строк по одной штуке.
# Приметы, по которым причину узнают ПО ВСЕЙ строке, а не по началу. Нужны
# там, где два разных отказа начинаются одинаково: «бюджет 5000 ₽ ниже
# минимума» и «бюджет 900000 ₽ выше потолка» оба начинаются с «бюджет», и
# второй попадал в строку «низкий бюджет» — причина называлась ровно
# наоборот, и владелец, читая отчёт, подкручивал не тот порог.
_REASON_MARKS = (
    ("выше потолка", "бюджет выше потолка — тендер"),
    ("оценить работу не вышло", "оценить работу не вышло"),
)

_REASON_GROUPS = (
    ("бюджет", "низкий бюджет"),
    ("категория", "не та категория"),
    ("стоп-слово", "стоп-слово"),
    ("не наш профиль", "не наш профиль"),
    ("отклик стоит", "дорогой отклик"),
    ("регламент", "регламент: не берём"),
    ("вердикт", "вердикт «уточнить»"),
    ("рейтинг", "низкий рейтинг — ждёт вечера"),
    ("этому заказчику", "этому заказчику уже писали"),
    ("почти такой же", "дубль заказа"),
    ("дневной лимит", "суточный лимит откликов"),
    ("суточный бюджет", "суточный бюджет исчерпан"),
    ("баланс", "не хватает баланса"),
    ("цена отклика", "цена отклика не прочиталась"),
    ("заход слишком короткий", "модель выдала обрубок"),
    ("в заходе оказалось", "запрещено правилами площадки"),
    ("задание не удалённое", "не удалённое задание"),
    ("заказу", "заказ устарел"),
    ("заход сгенерирован", "заход готов, решение не принято"),
)


# Причины, которые говорят о НАС, а не о заказе. Их нельзя показывать в одном
# списке с «не наш профиль» и «низкий бюджет»: там владелец читает «фильтр
# сработал правильно», а здесь на самом деле «мы не смогли и потеряли заказ».
# Живой пример: шестьдесят заказов за неделю с причиной «не удалось
# сгенерировать заход» — это молчащая модель, а не шестьдесят плохих заказов.
OUR_FAILURES = frozenset({
    "не удалось сгенерировать заход",
    "модель выдала обрубок",
    "заход готов, решение не принято",
    "цена отклика не прочиталась",
    "не хватает баланса",
    "вердикт «уточнить»",
})


def is_our_failure(title: str) -> bool:
    """Это наш сбой, а не свойство заказа."""
    lowered = (title or "").lower()
    return any(mark in lowered for mark in ("не смог", "не удалось", "не прочитал")) or (
        title in OUR_FAILURES
    )


def _short_reason(reason: str) -> str:
    """Схлопывает похожие причины в одну строку отчёта.

    Сначала приметы по всей строке, потом начала. Порядок именно такой: два
    отказа могут начинаться одинаково и значить противоположное, и различает
    их только середина.
    """
    text = (reason or "—").strip()
    lowered = text.lower()
    for mark, title in _REASON_MARKS:
        if mark in lowered:
            return title
    for prefix, title in _REASON_GROUPS:
        if lowered.startswith(prefix):
            return title
    return text[:40]


def render(funnel: Funnel, days: int) -> str:
    """Человекочитаемый отчёт для терминала и Telegram."""
    lines = [
        f"📊 За последние {days} сут.",
        "",
        f"Заказов просмотрено: {funnel.seen}",
        f"  отсеяно окончательно: {funnel.rejected}",
        f"  отложено до следующего раза: {funnel.skipped}",
        f"  биржа не дала откликнуться: {funnel.unavailable}",
    ]
    if funnel.dry_run:
        lines.append(f"  готово к отправке (DRY_RUN): {funnel.dry_run}")
    lines += [
        "",
        f"Откликов отправлено: {funnel.sent}",
    ]
    if funnel.failed:
        lines.append(f"  не удалось отправить: {funnel.failed}")
    lines.append(f"Ответов от клиентов: {funnel.replies}")
    if funnel.sent:
        lines.append(f"Отвечают на {funnel.reply_rate:.0f}% откликов")
    if funnel.median_speed is not None:
        lines.append(
            f"Откликаемся за {funnel.median_speed:.0f} мин "
            f"(в первые 5 минут — {funnel.fast_share:.0f}% откликов)"
        )
    if funnel.spent:
        lines.append(f"Потрачено на отклики: {funnel.spent:.0f} ₽")
        if funnel.cost_per_reply:
            lines.append(f"Один ответ обошёлся в {funnel.cost_per_reply:.0f} ₽")

    if funnel.dropped_early:
        lines.append(
            f"Мимо без решения: {funnel.dropped_early} — "
            f"дешёвый фильтр и то, до чего проход не дошёл"
        )
        if funnel.waiting:
            lines.append(f"  ещё ждут своей очереди: {funnel.waiting}")

    if funnel.reasons:
        lines += ["", "Почему отсеивались:"]
        lines += [f"  {count:3} — {reason}" for reason, count in funnel.reasons]
    if funnel.our_failures:
        # Отдельным блоком и другими словами: это не отработавший фильтр, а
        # заказы, которые мы упустили по своей вине.
        lines += ["", "Упустили по нашей вине:"]
        lines += [f"  {count:3} — {reason}" for reason, count in funnel.our_failures]
    return "\n".join(lines)

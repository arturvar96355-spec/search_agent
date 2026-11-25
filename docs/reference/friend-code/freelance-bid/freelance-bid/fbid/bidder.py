"""Автоотклик: все предохранители в одном месте.

Порядок проверок важен — каждая следующая дороже предыдущей, и до отправки
дело доходит последним. Ни один путь не приводит к отправке отклика без:
  1) снятого стоп-крана;
  2) отсутствия отклика по этому заказу раньше;
  3) прохождения фильтров;
  4) свободного суточного лимита по количеству;
  5) свободного суточного бюджета и достаточного баланса (Profi.ru);
  6) непустого текста;
  7) выключенного DRY_RUN;
  8) повторной проверки цены прямо на кнопке отправки.

Пункт 8 — не паранойя: цена в карточке могла устареть, а платит владелец.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from . import limits, notify, policy
from .config import get_settings
from .filters import FilterSet
from .llm import (
    VERDICT_ASK,
    VERDICT_TAKE,
    BidDraft,
    draft_from_json,
    draft_to_json,
    estimate_days,
    generate_draft,
    looks_generic,
)
from .models import BidStatus, BidUnavailable, Order, Source
from .scoring import Score, newcomer, order_budget, score_order
from .sources.fl import NotAuthorized as FlNotAuthorized
from .sources.profi import NotAuthorized as ProfiNotAuthorized
from .store import Store, as_utc, order_fingerprint

# У каждой площадки свой класс «сессия слетела» — ловим оба одинаково.
SESSION_LOST = (FlNotAuthorized, ProfiNotAuthorized)

# Под этим ключом в базе лежит правда о модели: её пишет проход, а читают
# экраны бота — они живут в другом процессе, и память прохода им недоступна.
MODEL_STATE_KEY = "model:down"

PAUSED_KEY = "paused"


def paused(store) -> bool:
    """Включён ли стоп-кран. Функцией, а не только методом Bidder.

    Спрашивать надо из переписки с клиентом, а поднимать ради одного вопроса
    весь Bidder (он читает настройки и файл фильтров) — лишнее.
    """
    return store.get_state(PAUSED_KEY) == "1"

# Короче этого заход не бывает: по регламенту в нём приветствие, разбор задачи,
# цена со сроком и экспертный вопрос в конце.
MIN_REPLY_CHARS = 100

# Насколько заголовки должны совпасть, чтобы считать заказы близнецами.
# 0.75 подобрано так, чтобы «Нужен лендинг для стоматологии» и «Нужен лендинг
# для стоматологии срочно» слиплись, а «Нужен лендинг» и «Нужен интернет-магазин»
# остались разными заказами.
TWIN_SIMILARITY = 0.75
# До скольких букв огрубляем слово, чтобы падежи не мешали сравнению.
STEM_LENGTH = 6
# Окончания, которые отрезаем у коротких слов: «бота» и «бот» — одно и то же.
_TAIL_LETTERS = "аеиоуыэюяйь"
# Слова, которые есть в каждом втором заголовке и о сходстве ничего не говорят.
_NOISE_WORDS = [
    "нужен", "нужна", "нужно", "требуется", "ищу", "ищем",
    "сделать", "создать", "разработать", "доработать",
    "срочно", "под", "ключ", "для", "заказ", "работа", "проект",
    "специалист", "человек",
]


def _stem(word: str) -> str:
    """Грубая основа слова: обрезка по длине плюс снятие окончания."""
    stem = word[:STEM_LENGTH]
    if len(stem) >= 4 and stem[-1] in _TAIL_LETTERS:
        stem = stem[:-1]
    return stem


_NOISE_STEMS = frozenset(_stem(word) for word in _NOISE_WORDS)


def title_tokens(title: str) -> set[str]:
    """Значимые слова заголовка, огрублённые до основы.

    Основа — первые 6 букв: так «стоматологии» и «стоматология» становятся одним
    словом, и перевыложенное задание находится, даже если заказчик поменял падеж.
    Полноценный стеммер сюда тащить незачем — заголовки короткие.
    """
    words = re.findall(r"[a-zа-яё0-9]{3,}", (title or "").lower().replace("ё", "е"))
    return {_stem(word) for word in words} - _NOISE_STEMS


def _title_is_category(order: Order) -> bool:
    """Заголовок заказа совпадает с его категорией — значит заголовка нет.

    Profi.ru не даёт заказу собственного названия: в карточке стоит имя
    категории («Программисты», «Создание сайтов»), и оно одинаково у десятков
    заказов. Отличать заказы по такому «заголовку» нельзя.
    """
    title = (order.title or "").strip().lower()
    return bool(title) and title == (order.category or "").strip().lower()


def find_twin(title: str, seen_titles: list[str]) -> str | None:
    """Ищет среди отработанных заказов почти такой же. Возвращает его заголовок."""
    tokens = title_tokens(title)
    if len(tokens) < 2:
        # По одному значимому слову близнецов не ищем: «Сайт» совпал бы со всем.
        return None

    for other in seen_titles:
        other_tokens = title_tokens(other)
        if not other_tokens:
            continue
        overlap = len(tokens & other_tokens) / len(tokens | other_tokens)
        if overlap >= TWIN_SIMILARITY:
            return other
    return None


@dataclass
class BidOutcome:
    status: BidStatus
    reason: str
    cost: float = 0.0
    text: str = ""
    draft: BidDraft | None = None
    # Рейтинг заказа на момент решения — едет в лог и в карточку Telegram.
    score: Score | None = None
    # Подтвердила ли биржа отправку. False бывает при статусе SENT: деньги
    # списаны, а нашего отклика на странице нет. Повторять нельзя (заплатим
    # второй раз), но и рапортовать зелёной галочкой тоже — владелец решит,
    # что отклик стоит на заказе, а его там может не быть.
    confirmed: bool = True
    # Попал ли расход в суточный счётчик прямо в момент списания. Ставится
    # только тем, кто действительно заплатил, и нужен, чтобы record() не
    # записал те же деньги во второй раз.
    spend_booked: bool = False

    @property
    def sent(self) -> bool:
        return self.status is BidStatus.SENT


class Bidder:
    """Принимает решение по заказу и, если всё сошлось, отправляет отклик."""

    def __init__(self, store: Store, filters: FilterSet | None = None) -> None:
        self.store = store
        self.filters = filters or FilterSet.load()
        self.settings = get_settings()
        # Почему последний заход не годится. Пусто — вопрос не вставал.
        self._draft_problem = ""

    # --- Стоп-кран --------------------------------------------------------

    def is_paused(self) -> bool:
        return paused(self.store)

    def pause(self) -> None:
        self.store.set_state(PAUSED_KEY, "1")

    def resume(self) -> None:
        self.store.set_state(PAUSED_KEY, "0")

    # --- Решение ----------------------------------------------------------

    def decide(
        self,
        order: Order,
        order_pk: int,
        *,
        balance: float | None = None,
        min_score: float | None = None,
    ) -> BidOutcome:
        """Все проверки до отправки. Ничего не отправляет и денег не тратит.

        min_score — порог рейтинга. По умолчанию берётся из лимитов (7 из 10):
        отклик уходит сразу только на заказы, которые этого стоят. Вечерний
        добор передаёт 0 и разбирает всё, что осталось, если деньги ещё есть.
        """
        if self.is_paused():
            return BidOutcome(BidStatus.SKIPPED, "включён стоп-кран из Telegram")

        if self.store.has_bid(order_pk):
            return BidOutcome(BidStatus.SKIPPED, "по этому заказу уже откликались")

        decision = self.filters.decide(order)
        if not decision.passed:
            # Постоянные причины (категория, текст, цена выше потолка) больше не
            # трогаем. Временные (не прочиталась цена) оставляем на следующий раз.
            status = BidStatus.REJECTED if decision.permanent else BidStatus.SKIPPED
            return BidOutcome(status, decision.reason)

        twin = self._check_same_client(order, order_pk)
        if twin is not None:
            return twin

        limit = self._daily_limit(order.source)
        sent_today, spent_today = self.store.spend_today(order.source)
        if sent_today >= limit:
            return BidOutcome(BidStatus.SKIPPED, f"дневной лимит откликов исчерпан ({limit})")

        money = self._check_money(order, spent_today, balance)
        if money is not None:
            return money

        threshold = self._score_threshold(min_score)
        score = self._score(order)
        # Заказ с понятной суммой судим до генерации захода: холодный откладываем,
        # не потратив ни рубля на модель. Договорной судить пока нечем — сумму
        # мы узнаем только после того, как модель оценит работу.
        fresh_client = newcomer(order)
        if not score.money_is_estimate:
            cold = self._check_score(score, threshold, fresh_client=fresh_client)
            if cold is not None:
                logger.debug("[{}] откладываю: {}", order.key, score.explain())
                return cold

        self._draft_problem = ""
        draft = self._draft(order, order_pk)
        if draft is None:
            if self._draft_problem:
                # Не «не смогли», а «получилось плохо, и мы это увидели».
                # Окончательно: если про этот заказ нечего сказать по существу
                # с двух попыток, завтра будет то же самое, а каждая попытка —
                # деньги. Причина видна в отчёте, среди прочих отказов.
                return BidOutcome(
                    BidStatus.REJECTED,
                    f"заход выходит шаблонным: {self._draft_problem}",
                    score=score,
                )
            return BidOutcome(BidStatus.SKIPPED, "не удалось сгенерировать заход", score=score)

        # Второй фильтр — вердикт самой модели по регламенту. «Уточнить» никогда
        # не уходит в автоотклик: неоднозначный заказ решает человек.
        if draft.verdict == VERDICT_ASK:
            # Окончательный: заход уже ушёл владельцу в Telegram, решает он.
            return BidOutcome(
                BidStatus.REJECTED,
                f"вердикт «уточнить»: {draft.reason or 'заявка неоднозначная'}",
                text=draft.reply,
                draft=draft,
                score=score,
            )
        if draft.verdict != VERDICT_TAKE:
            return BidOutcome(
                BidStatus.REJECTED,
                f"регламент: не берём ({draft.reason or 'не наш профиль'})",
                draft=draft,
                score=score,
            )
        # Именно strip(): текст из одних пробелов «истинный», и без этой проверки
        # на Profi.ru спишутся деньги за пустой отклик.
        reply = draft.reply.strip()
        if not reply:
            return BidOutcome(
                BidStatus.REJECTED, "модель вернула пустой заход", draft=draft, score=score
            )
        # Обрубок вместо захода — это тоже потраченный платный отклик. Живой
        # заход по регламенту (приветствие, конкретика, цена, вопрос)
        # короче сотни символов не бывает.
        if len(reply) < MIN_REPLY_CHARS:
            return BidOutcome(
                BidStatus.REJECTED,
                f"заход слишком короткий ({len(reply)} симв.) — на такой отклик не тратимся",
                draft=draft,
                score=score,
            )

        # Заказ без указанного бюджета: считаем нашей оценкой работы. Иначе
        # «договорные» проезжали бы мимо порога — а это больше половины ленты.
        estimated = self._check_estimated_budget(order, draft)
        if estimated is not None:
            return estimated

        # Появилась оценка — пересчитываем рейтинг уже по цифре. Договорной заказ
        # до этого момента считался пороговым, и только сейчас видно, горячий он
        # или ждёт вечера.
        score = self._score(order)
        cold = self._check_score(score, threshold, draft=draft, fresh_client=fresh_client)
        if cold is not None:
            logger.debug("[{}] откладываю: {}", order.key, score.explain())
            return cold

        # Оценка появилась — перепроверяем денежный потолок уже с ней:
        # процентная доля от бюджета теперь считается и для договорных.
        money = self._check_money(order, spent_today, balance)
        if money is not None:
            return money

        # Правила площадки применяем последними, к готовому тексту: на Юду и
        # Кворке за ссылку или контакт в переписке блокируют аккаунт.
        text = policy.sanitize(draft.reply, order.source)
        if not policy.is_usable(draft.reply, text, order.source):
            return BidOutcome(
                BidStatus.REJECTED,
                "в заходе оказалось запрещённое правилами площадки (ссылка или контакт)",
                draft=draft,
                score=score,
            )

        cost = order.bid_price or 0.0
        if self.settings.dry_run:
            return BidOutcome(
                BidStatus.DRY_RUN,
                f"прошёл все проверки ({decision.reason}), но включён DRY_RUN",
                cost=cost,
                text=text,
                draft=draft,
                score=score,
            )

        return BidOutcome(
            BidStatus.PLANNED,
            decision.reason,
            cost=cost,
            text=text,
            draft=draft,
            score=score,
        )

    # --- Рейтинг и очередь ------------------------------------------------

    def _score(self, order: Order) -> Score:
        return score_order(order, budget_floor=self.filters.common.min_budget)

    def _score_threshold(self, min_score: float | None) -> float:
        if min_score is not None:
            return float(min_score)
        return limits.get("hot_score_min", self.store)

    @staticmethod
    def _check_score(
        score: Score, threshold: float, *, draft: BidDraft | None = None, fresh_client: str = ""
    ) -> BidOutcome | None:
        """Холодный заказ не отбрасывается, а откладывается.

        Именно SKIPPED, а не REJECTED: вечером порог опускается до нуля, и заказ
        получает свой шанс, если лимиты и деньги ещё остались.

        fresh_client — заказчик зарегистрировался на бирже только что. Такому
        мгновенный отклик не покупаем ни при каком рейтинге: истории найма у
        него нет, и отклик ему — ставка вслепую. Вечером, когда порог опущен до
        нуля, он идёт в общем порядке: «никогда никого не нанимал» и «не успел
        никого нанять» снаружи выглядят одинаково, и хоронить заказ насовсем
        из-за этого нельзя.

        Ничего не логирует специально: по договорному заказу этот метод
        вызывается дважды (до и после оценки работы), и запись «откладываю»
        на первом вызове врала бы — заказ в этот момент едет дальше.
        """
        if fresh_client and threshold > 0:
            return BidOutcome(
                BidStatus.SKIPPED,
                f"заказчик на бирже {fresh_client}, истории найма нет — ждёт вечернего добора",
                draft=draft,
                score=score,
            )
        if score.is_hot(threshold):
            return None
        return BidOutcome(
            BidStatus.SKIPPED,
            f"рейтинг {score.total:.1f} из 10 ниже порога {threshold:.1f} — ждёт вечернего добора",
            draft=draft,
            score=score,
        )

    def _check_same_client(self, order: Order, order_pk: int) -> BidOutcome | None:
        """Второй отклик тому же заказчику выглядит как рассылка, а не как студия.

        Заказчика ловим двумя способами. Если площадка его раскрывает (Юду даёт
        ссылку на профиль) — по ней, это точно. Если нет (FL.ru прячет заказчика
        от гостей) — по тексту заявки: почти одинаковые заголовки означают, что
        один человек развесил задание несколько раз.
        """
        days = self.settings.client_dedup_days

        if order.client_key:
            twin = self.store.bid_to_client(
                order.source, order.client_key, within_days=days, exclude_pk=order_pk
            )
            if twin is not None:
                return BidOutcome(
                    BidStatus.REJECTED,
                    f"этому заказчику уже писали по заказу «{twin[:60]}»",
                )

        # Заголовка у заказа может не быть вовсе: площадка подставляет вместо
        # него название категории, одинаковое у десятков заказов подряд. Судить
        # по нему нельзя — «Программисты нейросетей» и «Создание сайтов на
        # битрикс» отбрасывались как дубли самих себя. Тогда сравниваем начало
        # ОПИСАНИЯ: его пишет заказчик, и два одинаковых означают одно задание,
        # развешенное дважды.
        #
        # Раньше на этом месте стоял просто выход, и на Profi.ru защита не
        # работала ни одним из двух способов: заказчика площадка не раскрывает,
        # а заголовок у неё всегда категория. Комментарий при этом утверждал,
        # что заказчик «раскрыт другими способами».
        mine = order_fingerprint(order.title, order.category, order.description)
        if not mine:
            return None

        seen = self.store.recent_bid_titles(order.source, within_days=days)
        twin = find_twin(mine, seen)
        if twin is not None:
            return BidOutcome(
                BidStatus.REJECTED,
                f"почти такой же заказ уже отработан: «{twin[:60]}»",
            )
        return None

    def _draft(self, order: Order, order_pk: int) -> BidDraft | None:
        """Заход по заказу, с кэшем.

        Отложенный до вечера заказ иначе уходил бы к модели заново на каждом
        проходе цикла — это прямые деньги за один и тот же текст.
        """
        cached = self.store.load_draft(order_pk)
        if cached:
            draft = draft_from_json(cached)
            if draft is not None:
                logger.debug("[{}] беру заход из кэша", order.key)
                return draft

        draft = generate_draft(order)
        self._remember_model_state()
        if draft is None:
            return None

        # Заход, в котором нет ни слова из заявки, читается как «нам всё равно»
        # — и стоит тех же денег, что и живой. Один переспрос: модель обычно
        # исправляется, если сказать прямо. Второй раз не платим.
        #
        # Судим ТОЛЬКО то, что действительно уйдёт клиенту. У вердиктов «не
        # берём» и «уточнить» текст пустой по замыслу, и «пустой заход» там —
        # не беда, а норма: решение по заказу принимается раньше и по другой
        # причине, её и надо показать владельцу.
        if draft.verdict != VERDICT_TAKE:
            self.store.save_draft(order_pk, draft_to_json(draft))
            return draft

        problem = looks_generic(draft.reply, order)
        if problem:
            logger.warning("[{}] заход похож на шаблон ({}) — переспрашиваю", order.key, problem)
            second = generate_draft(order, strict=True)
            self._remember_model_state()
            if second is not None:
                draft = second
                problem = looks_generic(draft.reply, order)
        if problem:
            logger.error("[{}] заход всё равно шаблонный ({}) — не отправляю", order.key, problem)
            self._draft_problem = problem
            return None

        self.store.save_draft(order_pk, draft_to_json(draft))
        return draft

    def _check_estimated_budget(self, order: Order, draft: BidDraft) -> BidOutcome | None:
        """Порог бюджета для заказов, где сумма не указана.

        Модель оценивает работу по ТЗ (price_from), и эта оценка проходит тот же
        порог, что и явный бюджет. Заодно оценка кладётся в order.raw, чтобы
        процентный потолок цены отклика считался и по договорным заказам.
        """
        if order.budget_value is not None:
            return None

        minimum = self.filters.common.min_budget
        ceiling = self.filters.common.max_budget
        estimate = float(draft.price_from or 0)
        if estimate > 0:
            order.raw["estimated_budget"] = estimate

        if minimum is None and ceiling is None:
            return None
        if estimate <= 0:
            if minimum is None:
                return None  # нижнего порога нет — оценка и не требовалась
            return BidOutcome(
                BidStatus.REJECTED,
                "бюджет не указан и оценить работу не вышло",
                draft=draft,
            )
        if minimum is not None and estimate < minimum:
            return BidOutcome(
                BidStatus.REJECTED,
                f"бюджет не указан, наша оценка {estimate:.0f} ₽ ниже минимума {minimum:.0f} ₽",
                draft=draft,
            )
        # Верхний порог тоже. Он не применялся здесь вовсе, хотя строка
        # документации обещает «тот же порог, что и явный бюджет»: на
        # договорных заказах мы платили за отклики на тендеры, где наш отклик
        # заведомо теряется среди полусотни чужих.
        if ceiling is not None and estimate > ceiling:
            return BidOutcome(
                BidStatus.REJECTED,
                f"бюджет не указан, наша оценка {estimate:.0f} ₽ выше потолка "
                f"{ceiling:.0f} ₽ — это тендер, отклик уйдёт в никуда",
                draft=draft,
            )
        return None

    def _daily_limit(self, source: Source) -> int:
        key = "fl_daily_bids" if source is Source.FL else "profi_daily_bids"
        return int(limits.get(key, self.store))

    def max_bid_price(self, order: Order) -> float:
        """Сколько максимум готовы отдать за отклик по этому конкретному заказу.

        Не просто потолок из лимитов, а тот же расчёт, что делают фильтры:
        минимум из абсолютного потолка и доли от бюджета заказа. Раньше на
        кнопку оплаты уходил голый абсолютный потолок, и отклик за 700 ₽ на
        заказе за 20 000 ₽ (3.5% вместо разрешённых 2%) прошёл бы последнюю
        проверку, хотя фильтр его не пускал.
        """
        rules = self.filters.per_source.get(order.source.value)
        if rules is None:
            return limits.get("profi_max_bid_price", self.store)
        return self.filters.effective_max_bid_price(rules, order_budget(order))

    def _check_money(
        self, order: Order, spent_today: float, balance: float | None
    ) -> BidOutcome | None:
        """Денежные предохранители Profi.ru.

        На FL.ru цена отклика на странице не показывается, но отклики там тоже
        расходуемые (в меню есть «Купить отклики», /payed/#buy-offers). Защита там
        другая: суточный лимит FL_DAILY_BIDS плюс проверка в submit_bid — если пакет
        кончился, форма отклика на странице просто не отрисуется, и отправки не будет.
        """
        if order.source is not Source.PROFI:
            return None

        price = order.bid_price
        if price is None:
            # Временный: цена не прочиталась из-за сбоя связи, а не потому,
            # что её нет. Окончательный отказ терял бы нормальные заказы.
            return BidOutcome(BidStatus.SKIPPED, "цена отклика не прочиталась")

        max_price = self.max_bid_price(order)
        if price > max_price:
            return BidOutcome(
                BidStatus.REJECTED,
                f"отклик стоит {price:.0f} ₽, потолок {max_price:.0f} ₽",
            )

        daily_budget = limits.get("profi_daily_budget", self.store)
        if spent_today + price > daily_budget:
            return BidOutcome(
                BidStatus.SKIPPED,
                f"суточный бюджет исчерпан: потрачено {spent_today:.0f} ₽ "
                f"из {daily_budget:.0f} ₽",
            )

        # Баланс не прочитали — считаем, что денег может не быть. Fail-closed.
        if balance is None:
            return BidOutcome(BidStatus.SKIPPED, "не смог прочитать баланс Profi.ru")

        min_balance = limits.get("profi_min_balance", self.store)
        if balance - price < min_balance:
            return BidOutcome(
                BidStatus.SKIPPED,
                f"баланс {balance:.0f} ₽ упрётся в минимум {min_balance:.0f} ₽",
            )
        return None

    # --- Отправка ---------------------------------------------------------

    def execute(
        self, source_adapter, context, order: Order, outcome: BidOutcome, order_pk: int
    ) -> BidOutcome:
        """Отправляет отклик. Вызывать только при status == PLANNED.

        order_pk нужен, чтобы записать факт списания сразу после него, а не
        через несколько шагов: между отправкой и `record()` процесс может
        умереть, база — оказаться занятой, диск — кончиться. Тогда заказ
        остаётся неотработанным, и на следующем проходе за него платят второй
        раз. Деньги уже ушли — значит и запись должна лечь немедленно.

        Значения по умолчанию у него нет намеренно. Оно было, и ручная команда
        `fbid bid` вызывала отправку без номера заказа — то есть без защиты от
        повторной оплаты, ради которой номер и передаётся. Забыть обязательный
        довод нельзя: Python не даст.
        """
        if outcome.status is not BidStatus.PLANNED:
            return outcome

        kwargs: dict = {"max_price": self.max_bid_price(order)}
        if self.two_phase(order.source):
            # Фаза 1: занимаем очередь как можно быстрее, развёрнутый заход — следом.
            kwargs["two_phase"] = True
            if order.source is Source.FL:
                kwargs["opener_chars"] = self.settings.fl_opener_chars

        draft = outcome.draft
        if order.source is Source.PROFI and draft is not None:
            # Поля стоимости на Profi обязательные — без них отклик не уходит.
            kwargs["price_from"] = draft.price_from
            kwargs["price_to"] = draft.price_to
        if order.source is Source.FL and draft is not None:
            # На FL цена и срок — обязательные поля формы: без них кнопка
            # отправки молча не срабатывает. Берём цену модели, а если её нет —
            # бюджет заказчика; пустыми не оставляем никогда.
            price = draft.price_from or draft.price_to or order.budget_value or 0
            kwargs["client_price"] = int(price) if price else None
            kwargs["days"] = draft.days or (estimate_days(price) if price else None)

        try:
            result = source_adapter.submit_bid(context, order, outcome.text, **kwargs)
        except SESSION_LOST as exc:
            # Сессия слетела. Это не сбой отправки: пока владелец не войдёт
            # руками, все следующие заказы упрутся в то же самое. Помечаем
            # заказ как временно недоступный и зовём человека — тревога здесь
            # обязательна, без неё «слетела сессия» неотличимо от «нет заказов»,
            # и отклики молча стоят до случайной проверки. Раз в несколько
            # часов, не на каждый заказ: за проход сюда попадают все подряд.
            logger.warning("[{}] сессия площадки слетела: {}", order.key, exc)
            self._alert_session_lost(order, exc)
            return BidOutcome(
                BidStatus.SKIPPED,
                f"сессия площадки слетела: {exc}",
                text=outcome.text,
                draft=outcome.draft,
            )
        except BidUnavailable as exc:
            # Причина не изменится сама: помечаем заказ, чтобы не долбиться в него
            # каждый проход и не слать владельцу одну и ту же тревогу.
            logger.info("[{}] откликнуться нельзя: {}", order.key, exc)
            return BidOutcome(
                BidStatus.UNAVAILABLE, str(exc), text=outcome.text, draft=outcome.draft
            )
        except Exception as exc:  # noqa: BLE001 — отправка упала — фиксируем и живём дальше
            logger.exception("[{}] отклик не отправился: {}", order.key, exc)
            return BidOutcome(
                BidStatus.FAILED, f"ошибка отправки: {exc}", text=outcome.text, draft=outcome.draft
            )

        # Сколько списали на самом деле. Цена в базе может быть суточной
        # давности, а списывают по той, что была на кнопке в момент клика.
        cost = self._charged(order, result, outcome)

        booked = False
        if result.charged and order_pk:
            # Отметку «за этот заказ уже заплачено» кладём здесь, до всего
            # остального. Подробности допишет record() чуть позже; если до
            # него дело не дойдёт, повторной оплаты всё равно не будет.
            booked = self._mark_charged(order_pk, order, cost)

        if not result.sent:
            if result.charged:
                # Деньги ушли, а подтверждения нет. Считаем отклик отправленным:
                # вернуть заказ в очередь значит заплатить за него второй раз.
                # Лучше один потерянный отклик, чем двойное списание.
                logger.warning(
                    "[{}] {} — деньги списаны, повторять не буду", order.key, result.reason
                )
                return BidOutcome(
                    BidStatus.SENT,
                    result.reason or "биржа не подтвердила отправку, но деньги списаны",
                    cost=cost,
                    text=outcome.text,
                    draft=outcome.draft,
                    score=outcome.score,
                    confirmed=False,
                    spend_booked=booked,
                )
            return BidOutcome(
                BidStatus.FAILED,
                result.reason or "биржа не подтвердила отправку",
                text=outcome.text,
                draft=outcome.draft,
                score=outcome.score,
            )
        return BidOutcome(
            BidStatus.SENT,
            outcome.reason,
            cost=cost,
            text=outcome.text,
            draft=outcome.draft,
            score=outcome.score,
            spend_booked=booked,
        )

    def _charged(self, order: Order, result, outcome: BidOutcome) -> float:
        """Сколько с нас списала площадка за этот отклик.

        Площадка называет цену на кнопке — она вернее той, что лежит в базе с
        прошлого прохода. Но принимаем мы её только в разумных пределах: адаптер,
        вернувший вместо списания цену нашей работы, однажды записал в суточный
        бюджет 28 000 ₽ и закрыл площадку на весь день. Сомнительное число не
        отбрасываем молча — берём план и зовём владельца.
        """
        told = result.price
        if told is None:
            return outcome.cost
        # Потолок берём тот же, с которым шли на кнопку оплаты: и правку из
        # бота, и долю от бюджета заказа. Раньше здесь стояло голое значение из
        # .env — владелец опускал потолок кнопкой, а проверка списания
        # продолжала считать нормой прежнюю, большую сумму.
        ceiling = max(self.max_bid_price(order), outcome.cost)
        if told > ceiling:
            logger.error(
                "[{}] площадка назвала списание {:.0f} ₽ при потолке {:.0f} ₽ — беру плановые "
                "{:.0f} ₽ и зову владельца",
                order.key, told, ceiling, outcome.cost,
            )
            notify.send(
                f"⚠️ <b>Странное списание на {order.source.value}</b>\n"
                f"Площадка сообщила {told:.0f} ₽ при потолке {ceiling:.0f} ₽.\n"
                f"В расход записал плановые {outcome.cost:.0f} ₽ — проверь счёт руками."
            )
            return outcome.cost
        return told

    def _mark_charged(self, order_pk: int, order: Order, cost: float) -> bool:
        """Пишет в базу, что за заказ уже заплачено. Падать здесь нельзя.

        Две записи, и обе про одни и те же деньги. `record_bid` закрывает заказ
        от повторной оплаты, `add_spend` двигает суточный счётчик, по которому
        софт решает, можно ли тратить дальше. Раньше вторая делалась позже, в
        record(), — и всё, что случалось между ними (упавший процесс, занятая
        база), оставляло деньги потраченными, а счётчик нетронутым: суточный
        бюджет считал, что рублей больше, чем есть на самом деле.

        Возвращает True, если счётчик суток уже увеличен — тогда record() эти
        же деньги второй раз не запишет.
        """
        try:
            self.store.record_bid(
                order_pk, BidStatus.SENT, reason="списание зафиксировано", cost_rub=cost
            )
            self.store.add_spend(order.source, cost)
            return True
        except Exception as exc:  # noqa: BLE001 — деньги уже ушли, паниковать поздно
            logger.error("[{}] НЕ ЗАПИСАЛ списание в базу: {}", order.key, exc)
            notify.send_alert(
                f"Заплатили за отклик на {order.url}, но записать это в базу не вышло: {exc}\n"
                f"Пока не проверишь — останови отклики кнопкой, иначе заказ оплатится ещё раз."
            )
            return False

    # Раз в сколько часов повторять тревогу о слетевшей сессии.
    SESSION_ALERT_HOURS = 4

    def _alert_session_lost(self, order: Order, exc: Exception) -> None:
        """Зовёт владельца входить на площадку. Не чаще раза в несколько часов."""
        from datetime import datetime, timedelta, timezone

        key = f"sessionlost:{order.source.value}"
        when = as_utc(self.store.get_state(key))
        quiet = timedelta(hours=self.SESSION_ALERT_HOURS)
        if when and datetime.now(timezone.utc) - when < quiet:
            return
        # Отметку — только после доставленной тревоги, тем же правилом, что
        # и остальные: сбой Телеграма не должен глушить её на четыре часа.
        if notify.send_alert(
            f"Сессия {order.source.value} слетела — отклики стоят. "
            f"Войди руками: fbid login {order.source.value}"
        ):
            self.store.set_state(key, datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # --- Учёт и уведомление ----------------------------------------------

    def record(self, order: Order, order_pk: int, outcome: BidOutcome) -> None:
        self.store.record_bid(
            order_pk,
            outcome.status,
            reason=outcome.reason,
            text=outcome.text,
            cost_rub=outcome.cost if outcome.sent else 0.0,
        )
        if not outcome.sent:
            return

        # Расход мог уже лечь в суточный счётчик — в момент списания, внутри
        # execute(). Второй раз те же деньги записывать нельзя: счётчик уйдёт
        # вдвое вперёд, и площадка закроется на день раньше срока.
        if not outcome.spend_booked:
            self.store.add_spend(order.source, outcome.cost)
        # От этой отметки считается срок напоминания: клиент, который молчит
        # почти сутки после отклика, получит одно короткое сообщение вдогонку.
        from . import dialog

        dialog.touch_last_message(self.store, order)

        if self.two_phase(order.source):
            # Отмечаем момент отклика: через message_delay уйдёт развёрнутый заход.
            self.store.mark_bid_placed(order_pk)
        else:
            self.store.mark_message_sent(order_pk)

    def _remember_model_state(self) -> None:
        """Кладёт правду о модели в базу. Пишет проход, читают экраны бота.

        Проход и бот — разные процессы, и предохранители внутри llm живут в
        памяти прохода. Без этой записи экран «Что сейчас происходит» отвечал
        «🟢 работает, отклики уходят» ровно тогда, когда счёт модели пуст и не
        пишется ни один заход, — то есть врал в том самом месте, ради которого
        его и открывают.
        """
        from . import llm

        trouble = llm.model_trouble()
        if trouble:
            self.store.set_state(MODEL_STATE_KEY, trouble)
        else:
            self.store.delete_state(MODEL_STATE_KEY)

    def two_phase(self, source: Source) -> bool:
        """Второй фазы больше нет ни на одной площадке. Всегда False.

        Две причины, и каждой хватило бы отдельно.

        Первая — правило владельца: на бирже софт отправляет один отклик и
        замолкает, любое следующее сообщение клиенту пишет человек. Досыл в
        чат — это ровно второе сообщение.

        Вторая — досыл и не работал. На FL он целился в поле `#noteTextarea`,
        а это личная заметка по заказу, которую видим только мы. Текст уходил
        туда, функция возвращала «отправлено», владельцу приходила карточка
        «текст ушёл клиенту» — и никто бы не узнал, что клиент его не видел.

        Настройки FL_TWO_PHASE и PROFI_TWO_PHASE намеренно больше ни на что не
        влияют: включить сломанное одним словом в .env быть не должно.
        """
        return False

    def message_delay(self, source: Source) -> float:
        return {
            Source.FL: self.settings.fl_message_delay,
            Source.PROFI: self.settings.profi_message_delay,
        }.get(source, self.settings.profi_message_delay)

    # --- Прежняя вторая фаза: только уборка ------------------------------

    def flush_pending_messages(self, source_adapter=None, context=None) -> int:
        """Ничего не досылает: второй фазы больше нет — см. two_phase().

        Площадка и браузер не нужны и не используются: уборка вся в базе.
        Аргументы остались, чтобы прежние вызовы не сломались.

        Очередь всё же разбираем: в базе могли остаться отклики, помеченные к
        досылу прежней версией. Отправлять их нельзя, а держать в очереди
        вечно — значит каждый проход заново открывать страницы.
        """
        stale = self.store.pending_messages(0)
        for _order, order_pk, _text in stale:
            self.store.mark_message_sent(order_pk)
        if stale:
            logger.info("Досыл отменён: {} откликов из прежней очереди снял", len(stale))
        return 0

    def announce(self, order: Order, outcome: BidOutcome) -> None:
        """Рассказывает про отклик — сразу или строкой в вечерней сводке.

        Отправленный отклик приходил карточкой в ту же секунду, и в день с
        двумя десятками откликов пульт превращался в ленту. Дёргать им незачем:
        отклик уже ушёл, делать с ним нечего, а посмотреть их все можно кнопкой
        «Отклики за сегодня». Поэтому карточка не исчезает, а ждёт вечера.

        Срочным здесь остаётся только то, где нужен человек: деньги, сессия,
        площадка. Их шлёт не эта функция.
        """
        if outcome.status is BidStatus.SENT:
            self.store.add_quiet(notify.bid_card(order, outcome, sent=True))
        elif outcome.status is BidStatus.DRY_RUN:
            self.store.add_quiet(notify.bid_card(order, outcome, sent=False))
        elif outcome.status is BidStatus.FAILED:
            # Один несостоявшийся отклик — не поломка: заказ остаётся в очереди
            # и попадёт в следующий проход. Поломка — это когда не получается
            # раз за разом, и о ней говорит отдельная тревога про площадку.
            self.store.add_quiet(
                f"⚠️ Не смог откликнуться на {order.url}\n{outcome.reason}"
            )
        elif outcome.status is BidStatus.UNAVAILABLE:
            # Штатная ситуация, тревогу не шлём — просто пишем в лог и больше не пробуем.
            logger.info("[{}] пропускаю навсегда: {}", order.key, outcome.reason)
        elif (
            outcome.draft
            and outcome.draft.verdict == VERDICT_ASK
            and self.settings.verdict_ask_action == "notify"
        ):
            # Модель не уверена — заход и ссылка ждут владельца в сводке.
            # Решает он, но решение это не срочное: заказ никуда не денется.
            self.store.add_quiet(notify.bid_card(order, outcome, sent=False))

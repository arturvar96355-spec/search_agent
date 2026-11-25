"""Обходчик бота: жмёт каждую кнопку по-настоящему и проверяет, что вышло.

Ручная проверка интерфейса не масштабируется: экранов под сотню, у каждого по
шесть кнопок, и половина ведёт вглубь. Поэтому здесь обход в ширину от главного
меню: экран → все его кнопки → все их экраны, пока не кончатся новые.

Работает на КОПИЯХ боевых баз, поэтому жать можно всё, включая «удалить» и
«отправить». Наружу ничего не уходит: токен, ключ модели и почта в копии
настроек стёрты, а браузер биржи не поднимается.

Что проверяется на каждом экране:
  · нажатие вообще обработалось (не «кнопка устарела», не «экран не открылся»);
  · текст непустой и влезает в лимит Telegram;
  · html-разметка закрыта — иначе Telegram отвергнет сообщение целиком;
  · callback_data кнопки влезает в 64 байта;
  · с экрана есть путь назад — и он ведёт ближе к главному меню;
  · «назад» возвращает туда, откуда пришли, а не в другое место;
  · повторное нажатие той же кнопки не даёт ровно тот же экран
    (иначе Telegram отказывает в правке, и в чате появляется дубль);
  · ряды кнопок не переполнены — на телефоне подписи схлопываются в «…».

Запуск:  .venv/bin/python tools/walk_bot.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from collections import deque
from pathlib import Path

ORBIT = Path(__file__).resolve().parent.parent
BIDS = ORBIT.parent / "freelance-bid"
sys.path.insert(0, str(ORBIT))
sys.path.insert(0, str(BIDS))

# --- Песочница: копии баз и обезвреженные настройки -------------------------

SANDBOX = Path(tempfile.mkdtemp(prefix="walkbot-"))


def _copy_db(source: Path, name: str) -> Path:
    target = SANDBOX / name
    if source.exists():
        shutil.copy2(source, target)
        for tail in ("-wal", "-shm"):
            extra = source.with_name(source.name + tail)
            if extra.exists():
                shutil.copy2(extra, target.with_name(target.name + tail))
    return target


def _sandbox() -> None:
    from fbid.config import get_settings as bids_settings

    from orbit.config import get_settings as orbit_settings

    orbit_conf = orbit_settings()
    orbit_conf.db_path = _copy_db(orbit_conf.db_path, "orbit.db")
    # Ни одного канала наружу: ни телеграма, ни почты, ни модели.
    orbit_conf.telegram_bot_token = None
    orbit_conf.smtp_host = orbit_conf.smtp_user = orbit_conf.smtp_password = None
    orbit_conf.anthropic_api_key = None
    # Каталог выгрузок парсера — тоже в песочницу, и пустой. Кнопка «Загрузить
    # из парсера» жмётся вместе со всеми, и в боевом каталоге она перезаписывала
    # карточки песочницы содержимым живых файлов: подложенные неудобные имена
    # стирались прямо посреди обхода, а сам обход зависел от того, какие файлы
    # сегодня лежат в output.
    leads = SANDBOX / "leads"
    (leads / "output").mkdir(parents=True, exist_ok=True)
    orbit_conf.leads_path = leads

    bids_conf = bids_settings()
    bids_conf.db_path = _copy_db(bids_conf.db_path, "fbid.db")
    bids_conf.telegram_bot_token = None
    bids_conf.anthropic_api_key = None
    bids_conf.dry_run = True


_sandbox()


def _refuse_if_live() -> None:
    """Останавливает прогон, если песочница не подставилась.

    Обходчик жмёт ВСЕ кнопки подряд, включая «удалить», «очистить очередь» и
    «отправить». На боевой базе это разрушительно. Подмена настроек тихая: одна
    неверная правка выше — и она просто не сработает, а прогон пойдёт по живым
    данным как ни в чём не бывало. Поэтому проверяем явно и падаем громко.
    """
    from fbid.config import PROJECT_ROOT as BIDS_ROOT
    from fbid.config import get_settings as bids_settings

    from orbit.config import PROJECT_ROOT as ORBIT_ROOT
    from orbit.config import get_settings as orbit_settings

    for path, root, name in (
        (orbit_settings().db_path, ORBIT_ROOT, "orbit"),
        (bids_settings().db_path, BIDS_ROOT, "freelance-bid"),
        (orbit_settings().leads_path, ORBIT_ROOT.parent / "leadgen", "выгрузки парсера"),
    ):
        if path.resolve().is_relative_to(root.resolve()):
            raise SystemExit(
                f"ОТКАЗ: обходчик смотрит в боевую базу {name} ({path}). "
                f"Он жмёт все кнопки подряд, включая удаление. Проверь _sandbox()."
            )


_refuse_if_live()

from orbit import bot  # noqa: E402
from orbit.store import Store  # noqa: E402
from orbit.ui import URL_PREFIX, Screen, bad_html  # noqa: E402

HOME = "home"
# Сколько кнопок с одним префиксом обходим. Списки заказов дают сотни карточек,
# и все они устроены одинаково: три штуки покажут ровно те же ошибки.
SAME_KIND_LIMIT = 3
# Предел ряда: подписи длиннее схлопываются в «…» и кнопку не прочитать.
ROW_CHARS = 48

problems: list[tuple[str, str]] = []


def flag(where: str, text: str) -> None:
    problems.append((where, text))


# --- Проверки одного экрана -------------------------------------------------

def _unbalanced(text: str) -> str | None:
    """Что в тексте не понравится Telegram. Разбор общий — см. orbit/ui.py.

    Своя копия здесь была, и она знала только про незакрытые теги: имя вида
    «<script>alert(1)</script>» она объявляла разметкой в порядке, хотя
    Telegram отвергает такое сообщение целиком.
    """
    return bad_html(text)


def _check_screen(code: str, screen: Screen) -> None:
    where = code
    text = screen.text or ""

    if not text.strip():
        flag(where, "пустой текст экрана")
    if len(text) > 4096:
        flag(where, f"текст длиннее лимита Telegram: {len(text)}")
    broken = _unbalanced(text)
    if broken:
        flag(where, f"html-разметка: {broken}")
    if "Кнопка устарела" in text:
        flag(where, "мёртвая кнопка: обработчика нет")
    if "Экран не открылся" in text:
        flag(where, f"экран упал: {text[-120:]}")
    if "None" in re.sub(r"<[^>]+>", "", text):
        flag(where, "в тексте пролезло None")

    seen: set[str] = set()
    for row in screen.rows:
        if not row:
            flag(where, "пустой ряд кнопок")
            continue
        width = sum(len(label) for label, _ in row)
        if len(row) > 3 and width > 24:
            flag(where, f"в ряду {len(row)} кнопок — на телефоне это нечитаемо")
        if len(row) > 1 and width > ROW_CHARS:
            labels = " | ".join(label for label, _ in row)
            flag(where, f"ряд не влезет в экран телефона ({width} знаков): {labels}")
        for label, data in row:
            if not label.strip():
                flag(where, f"кнопка без подписи: {data}")
            if len(data.encode()) > 64 and not data.startswith(URL_PREFIX):
                flag(where, f"callback_data длиннее 64 байт: {data}")
            if data in seen:
                flag(where, f"две одинаковые кнопки на экране: {data}")
            seen.add(data)


def _codes(screen: Screen) -> list[str]:
    out = []
    for row in screen.rows:
        for _label, data in row:
            if not data.startswith(URL_PREFIX) and data != "noop":
                out.append(data)
    return out


def _shape(screen: Screen) -> tuple[str, str]:
    """Что Telegram сравнивает при правке сообщения: текст и клавиатура."""
    return screen.text, str(screen.markup)


def _family(code: str) -> str:
    """Ключ «однотипности»: карточки заказов отличаются только номером."""
    return ":".join(part for part in code.split(":") if not part.isdigit())


# Кнопки-действия. Экран после них — не место, куда «перешли», а отчёт о
# сделанном, поэтому «назад» с него ведёт туда же, куда с исходного экрана,
# и сверять её с тем, откуда пришли, бессмысленно.
ACTIONS = {
    "mark", "dead", "send", "sendall", "drop", "gen", "prep", "imp", "lim",
    "auto", "clear", "clearyes", "manual", "res", "called", "later", "skip",
    "tip", "limit_reset", "limit_cancel", "queue_clear", "queue_clear_yes",
    "pause", "resume", "limit",
}


def _is_action(code: str) -> bool:
    return any(part in ACTIONS for part in code.split(":"))


# --- Обход ------------------------------------------------------------------

def walk(store: Store) -> dict[str, Screen]:
    registry = bot.registry_for(store)
    screens: dict[str, Screen] = {}
    depth: dict[str, int] = {HOME: 0}
    parent: dict[str, str] = {}
    family_count: dict[str, int] = {}

    queue = deque([HOME])
    while queue:
        code = queue.popleft()
        if code in screens:
            continue
        try:
            screen = bot.route(registry, store, code)
        except Exception as exc:  # noqa: BLE001 — падение роутера это находка
            flag(code, f"роутер упал: {type(exc).__name__}: {exc}")
            continue
        screens[code] = screen
        _check_screen(code, screen)

        # Кнопка-действие, после которой на экране не изменилось ничего и
        # всплывашки нет, выглядит сломанной: владелец жмёт её снова и снова.
        # (Сам дубль сообщения тут больше не возникает — правку «ничего не
        # изменилось» бот считает успешной, см. bot.edit.)
        came_from = parent.get(code)
        if (
            _is_action(code)
            and came_from in screens
            and not screen.alert
            and _shape(screens[came_from]) == _shape(screen)
        ):
            flag(code, "нажатие ничего не меняет и молчит — кнопка выглядит сломанной")

        for target in _codes(screen):
            if target in screens:
                continue
            family = _family(target)
            family_count[family] = family_count.get(family, 0) + 1
            if family_count[family] > SAME_KIND_LIMIT:
                continue
            depth.setdefault(target, depth[code] + 1)
            parent.setdefault(target, code)
            queue.append(target)

    _check_back(screens, depth, parent)
    return screens


BACK_WORDS = (
    "назад", "в меню", "главное меню", "к фрилансу", "к очереди", "к карточке",
    "оставить как есть", "к обзвону", "домой",
)


def _siblings(one: str, other: str) -> bool:
    """Соседние экраны одного списка: переключатель стадий, страницы, плоское меню.

    Между ними ходят вбок, а не вглубь, поэтому «назад» у них общая — она
    уводит из списка целиком, а не к предыдущему соседу.
    """
    return one.split(":")[:2] == other.split(":")[:2]


def _looks_back(label: str) -> bool:
    low = label.lower()
    return any(word in low for word in BACK_WORDS)


def _check_back(screens: dict[str, Screen], depth: dict[str, int], parent: dict[str, str]) -> None:
    for code, screen in screens.items():
        if code == HOME:
            continue
        targets = _codes(screen)
        if not targets:
            flag(code, "тупик: с экрана нет ни одной кнопки")
            continue

        back = [
            (label, data)
            for row in screen.rows
            for label, data in row
            if _looks_back(label) and not data.startswith(URL_PREFIX)
        ]
        if not back:
            flag(code, "нет кнопки «назад» — уйти можно только через меню Telegram")
            continue

        if _is_action(code):
            continue

        # Куда ведёт «назад». Должно быть ближе к главному меню, иначе кнопка
        # уводит вглубь, а не наружу.
        for label, data in back:
            if data not in depth:
                continue
            if depth[data] >= depth[code] and data != HOME:
                flag(code, f"«{label}» ведёт не назад, а вбок: {data}")

        came_from = parent.get(code)
        # Соседняя страница того же списка — не «откуда пришли»: листалка
        # ходит вбок, а «назад» обязана уводить из списка целиком.
        if came_from and came_from != HOME and not _siblings(came_from, code):
            if _is_action(came_from):
                continue
            data_set = {data for _label, data in back}
            if came_from not in data_set and HOME not in data_set:
                flag(
                    code,
                    f"«назад» ведёт в {', '.join(sorted(data_set))}, "
                    f"а пришли сюда из {came_from}",
                )


# Метка, по которой видно, что подложенная запись дошла до экрана.
NASTY_MARK = "ЖЖ"


def _seed_nasty(store: Store) -> int:
    """Портит имена у первых контактов песочницы — тех, что точно попадут на экран.

    Боевая база вежлива: имена как имена, тексты как тексты. А приходит в неё
    то, что лежало на чужом сайте, и однажды придёт имя с угловой скобкой —
    Telegram отвергнет сообщение целиком, и владелец не увидит ни карточки, ни
    причины. Проверить это на боевых данных нельзя: там таких имён нет, и
    обход честно отвечает «проблем не найдено», ничего про этот случай не зная.

    Именно ПЕРЕИМЕНОВЫВАЕМ, а не добавляем: списки идут по номеру в базе, и
    новая запись оказывается в самом хвосте, куда обход не доходит. Первая
    попытка так и сделала — записи легли в базу и не показались нигде.
    """
    nasty = [
        f"{NASTY_MARK}<script>alert(1)</script>",
        f"{NASTY_MARK}Фирма & Ко <ООО>",
        NASTY_MARK + "Очень длинное имя " + "и ещё " * 60,
        f'{NASTY_MARK}Кавычки «ёлочки» и "лапки"',
        NASTY_MARK,
    ]
    # По паре записей в КАЖДОЙ паре «вкладка + стадия»: экраны у них разные, и
    # неудобное имя должно попасть и в список, и в карточку, и в обзвон.
    rows = []
    pairs = store.conn.execute(
        "SELECT DISTINCT kind, stage FROM contacts ORDER BY kind, stage"
    ).fetchall()
    for pair in pairs:
        rows += store.conn.execute(
            "SELECT pk FROM contacts WHERE kind = ? AND stage = ? ORDER BY pk LIMIT 2",
            (pair["kind"], pair["stage"]),
        ).fetchall()
    for index, row in enumerate(rows):
        name = nasty[index % len(nasty)]
        store.conn.execute(
            "UPDATE contacts SET name = ?, meta = ? WHERE pk = ?",
            (name, '{"note": "<b>не закрытый тег", "city": "Москва & область"}', row["pk"]),
        )
    store.conn.commit()
    return len(rows)


def main() -> int:
    store = Store(bot.get_settings().db_path)
    print("═" * 78)
    print(" ОБХОД БОТА · жмём каждую кнопку на копии боевой базы")
    print("═" * 78)
    print(f"  подложено неудобных записей: {_seed_nasty(store)}")
    screens = walk(store)
    store.close()

    # Проверяем, что подложенное ДОШЛО до экранов. Иначе «проблем не найдено»
    # означает «мы их не показали»: списки режутся по три карточки на вид, и
    # неудобная запись легко не попадает ни на один экран вовсе.
    shown = sum(1 for screen in screens.values() if NASTY_MARK in (screen.text or ""))
    if not shown:
        flag("песочница", "неудобные записи не попали ни на один экран — проверка не состоялась")
    else:
        where = [code for code, screen in screens.items() if NASTY_MARK in (screen.text or "")]
        print(f"  экранов с неудобными записями: {shown} — {', '.join(where[:6])}")

    print(f"  экранов пройдено: {len(screens)}")
    buttons = sum(len(_codes(s)) for s in screens.values())
    print(f"  нажатий проверено: {buttons}")

    print("\n" + "═" * 78)
    if problems:
        print(f" НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for where, text in problems:
            print(f"  · [{where}] {text}")
    else:
        print(" ПРОБЛЕМ НЕ НАЙДЕНО")
    print("═" * 78)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

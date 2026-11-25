"""Реестр разделов: что подключено к Orbit и как это звать.

Orbit ничего не переписывает. Каждая система — отдельный проект со своей базой
и своей логикой; Orbit даёт им общий вход в Telegram и общую воронку.

Главное правило: **отсутствующая система не роняет бота**. Проект не установлен,
переехал, сломался импорт — раздел показывает «не подключено» и объясняет, что
сделать. Владелец в этот момент пользуется остальными разделами, а не смотрит
на упавшего бота.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

from .ui import Screen


@dataclass(frozen=True)
class Health:
    """Состояние системы для экрана «Сводка»."""

    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)

    @property
    def mark(self) -> str:
        return "🟢" if self.ok else "🔴"


@dataclass
class Section:
    """Раздел Orbit — одна вкладка бота.

    code     — префикс кнопок раздела, только латиница: «p», «c», «f»
    title    — как называется в главном меню
    icon     — эмодзи вкладки
    handle   — обработчик: (store, действие, аргумент) → экран
    health   — короткая проверка «жива ли система»
    commands — команды для кнопки «Меню» слева: (команда, описание, действие)
    knows    — «мой ли это чужой код кнопки». Есть только у разделов, которые
               принимают нажатия не со своим префиксом: карточки биржи бот
               шлёт сам, своими кодами. None — раздел чужих кодов не берёт.
    """

    code: str
    title: str
    icon: str
    handle: Callable[..., Screen]
    health: Callable[..., Health]
    commands: list[tuple[str, str, str]] = field(default_factory=list)
    knows: Callable[[str], bool] | None = None

    @property
    def button(self) -> tuple[str, str]:
        """Кнопка раздела на главном экране."""
        return f"{self.icon} {self.title}", f"{self.code}:home"


class Registry:
    """Все разделы Orbit. Собирается при старте, переживает отсутствие систем."""

    def __init__(self) -> None:
        self._sections: dict[str, Section] = {}
        self._broken: dict[str, str] = {}

    def add(self, loader: Callable[[], Section]) -> None:
        """Подключает раздел. Не смог загрузиться — записываем причину и живём дальше."""
        name = getattr(loader, "__module__", str(loader)).rsplit(".", 1)[-1]
        try:
            section = loader()
        except Exception as exc:  # noqa: BLE001 — одна система не гасит весь пульт
            logger.warning("Раздел «{}» не подключился: {}", name, exc)
            self._broken[name] = str(exc)
            return
        self._sections[section.code] = section
        logger.info("Раздел «{}» подключён", section.title)

    def get(self, code: str) -> Section | None:
        return self._sections.get(code)

    @property
    def sections(self) -> list[Section]:
        return list(self._sections.values())

    @property
    def broken(self) -> dict[str, str]:
        """Системы, которые не поднялись, и почему."""
        return dict(self._broken)

    def health(self, store) -> list[tuple[Section, Health]]:
        """Состояние всех разделов. Упавшая проверка — это тоже состояние."""
        result = []
        for section in self._sections.values():
            try:
                state = section.health(store)
            except Exception as exc:  # noqa: BLE001 — проверка не должна ронять экран
                state = Health(False, f"проверка не прошла: {exc}")
            result.append((section, state))
        return result


def build() -> Registry:
    """Собирает реестр по настройкам. Отключённый раздел просто не подключается."""
    from .config import get_settings

    settings = get_settings()
    registry = Registry()

    if settings.partners_enabled:
        from .sections import partners

        registry.add(partners.load)
    if settings.clients_enabled:
        from .sections import clients

        registry.add(clients.load)
    if settings.freelance_enabled:
        from .sections import freelance

        registry.add(freelance.load)
    return registry

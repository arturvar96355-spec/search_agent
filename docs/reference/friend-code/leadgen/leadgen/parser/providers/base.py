"""Абстрактный провайдер источника организаций.

Все источники (2ГИС, Яндекс, browser) взаимозаменяемы за счёт единого интерфейса:
search(query, city) -> list[Organization].
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Settings
from ..models import Organization


class Provider(ABC):
    """Базовый класс провайдера. Источники наследуются и реализуют search()."""

    name: str = "base"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        """Готов ли провайдер к работе (есть ключ/зависимости). По умолчанию — да."""
        return True

    @abstractmethod
    async def search(
        self,
        query: str,
        city: str,
        max_results: int | None = None,
    ) -> list[Organization]:
        """Поиск организаций по рубрике (query) в городе (city).

        max_results — верхний предел на запрос; None -> берём из настроек.
        """
        raise NotImplementedError

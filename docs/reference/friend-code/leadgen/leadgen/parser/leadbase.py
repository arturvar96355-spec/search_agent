"""Персистентная база собранных ГОРЯЧИХ лидов для непрерывного фонового сбора.

Модель работы: фоновый коллектор постоянно парсит ниши×города и складывает найденные
проблемные лиды сюда, а пользователь в боте жмёт «Посмотреть базу» и мгновенно видит
накопленное — без ожидания парса. Хранится JSONL-файлом, переживает рестарт бота.
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from .models import Organization
from .score import lead_score


def base_key(org: Organization) -> str:
    """Ключ дедупа лида в базе. ИНН — самый надёжный (одна фирма из разных ниш/городов = один лид)."""
    return org.inn or org.source_url or f"{org.name}|{org.address or ''}"


class LeadBase:
    """Накопленные горячие лиды. add() дедуплицирует и дописывает в JSONL; all_sorted() — по приоритету."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.leads: dict[str, Organization] = {}
        # Кого не удалось записать на диск. В памяти они есть и в этом запуске
        # доедут до человека, но после перезапуска пропадут — об этом надо
        # сказать вслух, а не оставлять в логе.
        self.unsaved: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                org = Organization(**json.loads(line))
                self.leads[base_key(org)] = org  # последняя запись по ключу побеждает
            except Exception as exc:  # noqa: BLE001 — битая строка не роняет загрузку базы
                logger.warning("LeadBase: пропускаю битую строку ({}).", exc)
        logger.info("LeadBase: загружено {} лидов из {}", len(self.leads), self.path)

    def add(self, org: Organization) -> bool:
        """Добавить лид. True — если новый (иначе уже был). Дописывает в файл сразу.

        Незаписанные лиды считаем отдельно. Раньше сбой записи проглатывался
        одной строкой в логе, а метод возвращал True — «лид добавлен». Дальше
        всё шло как обычно, и пропажу замечал бы только тот, кто открыл файл и
        пересчитал строки: диск кончился или каталог стал недоступен, а сбор
        честно рапортует «нашёл 40 компаний», которых на диске нет.
        """
        key = base_key(org)
        if key in self.leads:
            return False
        self.leads[key] = org
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(org.model_dump(), ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 — сбой записи учитывается в unsaved
            self.unsaved.append(org.name)
            logger.error("LeadBase: лид «{}» НЕ записан на диск ({}).", org.name, exc)
        return True

    def take_unsaved(self) -> list[str]:
        """Имена лидов, не легших на диск, — и сразу забываем их.

        Забирать надо ровно один раз: список копился без очистки, и
        предупреждение «не записалось на диск» повторялось после КАЖДОГО
        следующего сбора, хотя новых сбоев не было. Повторяющаяся тревога
        перестаёт читаться, а вместе с ней теряется настоящая.
        """
        names, self.unsaved = self.unsaved, []
        return names

    def all_sorted(self) -> list[Organization]:
        """Все лиды по убыванию приоритета (платёжеспособность + боль + контакт)."""
        return sorted(self.leads.values(), key=lead_score, reverse=True)

    def count(self) -> int:
        return len(self.leads)

    def clear(self) -> int:
        """Очистить базу (файл и память). Возвращает, сколько было.

        Порядок важен: СНАЧАЛА файл, потом память. Не удалился файл — ничего
        не трогаем и поднимаем ошибку. Бот на такую ошибку отвечает «ничего не
        трогал», и это должно быть правдой: если память очистить раньше, лиды
        пропадут до перезапуска, а после него вернутся из файла — то есть
        сообщение соврёт дважды подряд.

        Молча продолжать тоже нельзя: память пуста, бот рапортует «база
        очищена», а после рестарта всё возвращается — и владелец собирает
        поверх старого, не понимая, откуда дубли.
        """
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError as exc:
            logger.error("LeadBase: файл {} не удалился ({}) — базу не трогаю",
                         self.path, exc)
            raise
        n = len(self.leads)
        self.leads.clear()
        return n

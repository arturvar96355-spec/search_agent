"""Промежуточное состояние прогона в .jsonl — докачка (resume) без потери данных.

После каждого запроса дописываем строки org + строку-маркер done. При повторном
запуске уже собранные (source, рубрика, город) пропускаем, ранее собранные
организации подхватываем. Прерывание (Ctrl+C, падение) не теряет то, что уже собрано.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from loguru import logger

from .models import Organization

QueryKey = tuple[str, str, str]  # (source, rubric, city)


class JsonlStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def completed_queries(self) -> set[QueryKey]:
        """Запросы с маркером done — их пропускаем при resume."""
        done: set[QueryKey] = set()
        for line in self._iter_lines():
            query = line.get("query")
            if query and line.get("done"):
                done.add((query["source"], query["rubric"], query["city"]))
        return done

    def load_orgs(self) -> list[Organization]:
        """Ранее собранные организации из .jsonl."""
        orgs: list[Organization] = []
        for line in self._iter_lines():
            raw_org = line.get("org")
            if raw_org is None:
                continue
            try:
                orgs.append(Organization(**raw_org))
            except Exception as exc:  # noqa: BLE001 — битую строку не роняем весь прогон
                logger.warning("Пропускаю повреждённую строку в {}: {}", self.path, exc)
        return orgs

    def append_query(self, source: str, rubric: str, city: str, orgs: list[Organization]) -> None:
        """Дописываем результат одного запроса: строки org + маркер done."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        query = {"source": source, "rubric": rubric, "city": city}
        with self.path.open("a", encoding="utf-8") as fh:
            for org in orgs:
                fh.write(json.dumps({"query": query, "org": org.model_dump()}, ensure_ascii=False) + "\n")
            fh.write(json.dumps({"query": query, "done": True}, ensure_ascii=False) + "\n")

    def _iter_lines(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue  # обрезанная последняя строка после падения — просто пропускаем

"""Разбор robots.txt.

Своя реализация вместо RobotFileParser из стандартной библиотеки — из-за
одной конкретной беды. Строку «Disallow: /?» (запретить адреса с
параметрами, она есть почти в каждом robots на WordPress) стандартный
разборщик приводит через urlparse к пути «/» и запрещает весь сайт.
Проверено на живом сайте агентства: обход возвращал «сайт не открылся»,
хотя robots разрешает всё, кроме админки.

Поддерживаем то же, что и поисковики: * — любая последовательность,
$ — конец адреса, при споре правил побеждает более длинное, при равной
длине — разрешение.
"""
from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class Rules:
    """Правила для «User-agent: *» с одного хоста."""

    lines: list[tuple[bool, str]] = field(default_factory=list)  # (разрешено, шаблон)
    delay: float | None = None

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        best: tuple[int, bool] | None = None
        for allowed, pattern in self.lines:
            if not _matches(pattern, path):
                continue
            weight = len(pattern)
            if best is None or weight > best[0] or (weight == best[0] and allowed):
                best = (weight, allowed)
        return best[1] if best else True


def _matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(piece) for piece in body.split("*"))
    return bool(re.match(regex + ("$" if anchored else ""), path))


def parse(text: str) -> Rules:
    """robots.txt → правила для нас. Чужие группы User-agent пропускаем."""
    rules = Rules()
    for_us = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name, value = field_name.strip().lower(), value.strip()

        if field_name == "user-agent":
            for_us = value == "*"
        elif not for_us:
            continue
        elif field_name in ("allow", "disallow") and value:
            rules.lines.append((field_name == "allow", value))
        elif field_name == "crawl-delay":
            with suppress(ValueError):
                rules.delay = float(value.replace(",", "."))
    return rules

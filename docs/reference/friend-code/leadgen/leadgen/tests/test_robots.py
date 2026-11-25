"""Разбор robots.txt.

История такая: обход сайта агентства возвращал «сайт не открылся», хотя
robots разрешает всё, кроме админки. Виновата была строка «Disallow: /?» —
стандартный разборщик Python приводит её к «/» и закрывает весь сайт.
"""
from __future__ import annotations

from parser.robots import parse

WORDPRESS = """
User-agent: *
Disallow: /cgi-bin
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php
Disallow: /?
Disallow: *?s=
Disallow: /search
Disallow: */embed$
Crawl-delay: 2
"""


def test_query_rule_does_not_close_the_site():
    rules = parse(WORDPRESS)
    assert rules.allows("https://firma.ru/") is True
    assert rules.allows("https://firma.ru/contacts") is True
    assert rules.allows("https://firma.ru/o-kompanii") is True


def test_real_disallows_still_work():
    rules = parse(WORDPRESS)
    assert rules.allows("https://firma.ru/wp-admin/") is False
    assert rules.allows("https://firma.ru/search") is False
    assert rules.allows("https://firma.ru/?s=запрос") is False


def test_allow_wins_over_longer_disallow_prefix():
    rules = parse(WORDPRESS)
    assert rules.allows("https://firma.ru/wp-admin/admin-ajax.php") is True


def test_end_anchor():
    rules = parse(WORDPRESS)
    assert rules.allows("https://firma.ru/post/embed") is False
    assert rules.allows("https://firma.ru/post/embedded") is True


def test_crawl_delay_is_read():
    assert parse(WORDPRESS).delay == 2.0


def test_rules_for_other_bots_are_not_ours():
    """Запрет для GPTBot нас не касается — у нас своя группа."""
    rules = parse("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /admin")
    assert rules.allows("https://firma.ru/contacts") is True
    assert rules.allows("https://firma.ru/admin") is False


def test_full_ban_is_respected():
    rules = parse("User-agent: *\nDisallow: /")
    assert rules.allows("https://firma.ru/") is False
    assert rules.allows("https://firma.ru/contacts") is False


def test_empty_robots_allows_everything():
    assert parse("").allows("https://firma.ru/contacts") is True


def test_comments_are_ignored():
    rules = parse("User-agent: *  # все\nDisallow: /admin  # админка")
    assert rules.allows("https://firma.ru/admin") is False
    assert rules.allows("https://firma.ru/") is True

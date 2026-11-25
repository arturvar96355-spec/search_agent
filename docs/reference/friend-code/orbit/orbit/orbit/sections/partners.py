"""Вкладка «Партнёры» — агентства, которые могут отдавать нам работу.

SMM-студии, таргетологи и digital-агентства приводят клиенту трафик, но сайты
и системы сами не делают. Клиент спрашивает «а сайт вы сделаете?» — агентство
либо теряет деньги, либо ищет исполнителя. Мы заходим белым субподрядом.

Механика вкладки общая с «Клиентами» (см. base.py), отличается адресат и текст
письма: за него отвечает knowledge/partners.md.
"""
from __future__ import annotations

from ..models import Kind
from ..registry import Health, Section
from ..store import Store
from ..ui import Screen
from . import base

TAB = base.Tab(
    code="p",
    kind=Kind.PARTNER,
    title="Партнёры",
    icon="👥",
    unit_one="агентство",
    unit_few="агентства",
    unit_many="агентств",
    hint="Агентства без своей разработки: отдают нам сайты и CRM на субподряд.",
)


def handle(store: Store, action: str, arg: str) -> Screen:
    return base.route(TAB, store, action, arg)


def health(store: Store) -> Health:
    return base.health(TAB, store)


def summary(store: Store) -> str:
    return base.summary_line(TAB, store)


def load() -> Section:
    return Section(
        code=TAB.code,
        title=TAB.title,
        icon=TAB.icon,
        handle=handle,
        health=health,
        commands=[
            ("partners", "Партнёры: агентства на субподряд", "p:home"),
            ("pqueue", "Партнёры: очередь писем", "p:queue:0"),
        ],
    )

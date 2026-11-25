"""Присмотр за пультом: туннель, его смерть и готовность.

Пульт открывался «1 раз из 20». Каждый тест здесь — одна из причин.
"""
from __future__ import annotations

import subprocess

from orbit.webapp import serve


class FakeProcess:
    def __init__(self, alive: bool = True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 1


def tunnel(alive: bool = True) -> serve.Tunnel:
    return serve.Tunnel(process=FakeProcess(alive))  # type: ignore[arg-type]


def test_the_address_is_taken_from_the_log():
    t = tunnel()
    serve.read_line(t, "INF |  https://literally-double-shorts-quote.trycloudflare.com  |")
    assert t.url == "https://literally-double-shorts-quote.trycloudflare.com"


def test_a_deleted_tunnel_is_recognised_as_dead():
    """«Tunnel not found» — Cloudflare удалил туннель, ждать бесполезно.

    Живой случай: переподключения отбивались этой строкой бесконечно, а
    старый скрипт считал туннель живым и держал в боте мёртвую кнопку.
    """
    t = tunnel()
    serve.read_line(t, 'ERR Register tunnel error error="Unauthorized: Tunnel not found"')
    assert t.dead


def test_one_lucky_answer_is_not_ready():
    """Одиночная удача ничего не доказывает — на переподключении адрес
    отвечает через раз. Кнопку в боте даём только после устойчивых ответов."""
    t = tunnel()
    t.url = "https://x.trycloudflare.com"
    answers = iter([True, False, True, False, True, False])
    assert not serve.wait_ready(t, check=lambda _url: next(answers, False),
                                timeout=0.5, pause=0.01)


def test_steady_answers_are_ready():
    t = tunnel()
    t.url = "https://x.trycloudflare.com"
    assert serve.wait_ready(t, check=lambda _url: True, timeout=1, pause=0.01)


def test_a_dead_tunnel_stops_the_wait_at_once():
    t = tunnel()
    t.url = "https://x.trycloudflare.com"
    t.dead = "Tunnel not found"
    assert not serve.wait_ready(t, check=lambda _url: True, timeout=5, pause=0.01)


def test_the_router_is_never_asked(monkeypatch):
    """Проверка идёт только через публичный DNS.

    Спроси мы роутер про адрес, которого ещё нет, — он запомнит «сайта нет»
    и минутами будет отдавать это телефону в той же Wi-Fi. Ровно так и
    получалось «1 раз из 20».
    """
    import dns.resolver

    seen: dict = {}

    class Resolver:
        def __init__(self, configure=True):
            seen["configure"] = configure

        def resolve(self, host, kind):
            seen["servers"] = list(self.nameservers)
            raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "Resolver", Resolver)
    assert serve.public_ip("x.trycloudflare.com") is None
    assert seen["configure"] is False, "системные настройки DNS (роутер) брать нельзя"
    assert seen["servers"] == list(serve.PUBLIC_DNS)


def test_the_tunnel_keeps_several_connections():
    """На одном подключении любой обрыв — белый экран."""
    command = serve.cloudflared_command(8137)
    assert "--ha-connections" in command
    assert command[command.index("--ha-connections") + 1] != "1"
    assert command[-1] == "http://127.0.0.1:8137"


def test_leftovers_touch_only_our_processes(monkeypatch):
    """Уборка гасит только своё: приложение без .serve, туннель на наш порт."""
    asked: list[str] = []

    def fake_pgrep(args, **_kw):
        asked.append(args[-1])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_pgrep)
    assert serve.clear_leftovers() == 0
    assert any("orbit\\.webapp$" in pattern for pattern in asked)
    assert any(str(serve.PORT) in pattern for pattern in asked)


def test_stopping_does_not_wait_for_the_tunnel():
    """Ctrl+C срабатывает сразу, а не через две минуты ожидания туннеля."""
    import threading
    import time

    t = tunnel()
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    assert not serve.wait_ready(t, check=lambda _url: False, timeout=60, pause=5, stop=stop)
    assert time.monotonic() - started < 1


# --- Свой сервер -------------------------------------------------------------

def test_own_server_wins_when_configured(monkeypatch):
    monkeypatch.setenv("ORBIT_VPS_HOST", "185.1.2.3")
    monkeypatch.setenv("ORBIT_PUBLIC_URL", "https://pult.uniqore.ru")
    monkeypatch.delenv("ORBIT_TUNNEL", raising=False)
    assert serve.provider() == "vps"


def test_half_configured_server_is_not_used(monkeypatch):
    """IP без адреса пульта — не настройка, а полдела: такой туннель некуда
    было бы опубликовать, и владелец получил бы кнопку в никуда."""
    monkeypatch.setenv("ORBIT_VPS_HOST", "185.1.2.3")
    monkeypatch.setattr(serve, "ENV_FILE", "/nonexistent")
    monkeypatch.delenv("ORBIT_PUBLIC_URL", raising=False)
    monkeypatch.delenv("ORBIT_TUNNEL", raising=False)
    monkeypatch.delenv("ORBIT_PINGGY_TOKEN", raising=False)
    assert serve.provider() == "cloudflare"


def test_the_channel_forwards_only_to_the_server_loopback():
    """Порт на сервере слушает 127.0.0.1: снаружи его не видно, только nginx."""
    command = serve.vps_command("185.1.2.3")
    forward = command[command.index("-R") + 1]
    assert forward.startswith(f"127.0.0.1:{serve.VPS_PORT}:")
    assert command[-1] == "tunnel@185.1.2.3", "заходим урезанным пользователем, не root"
    assert "-N" in command

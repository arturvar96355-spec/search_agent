"""Пульт целиком: приложение, туннель к нему и присмотр за обоими.

Запуск: python -m orbit.webapp.serve   (или tools/miniapp.sh — он зовёт это)

Раньше это был bash-скрипт, и пульт открывался в лучшем случае через раз.
Причин было три, и каждая здесь закрыта отдельно:

1. Скрипт запускали дважды, и два экземпляра дрались: каждый при старте
   убивал приложение соседа. Теперь замок — второй запуск просто говорит
   «уже работает» и выходит.

2. Быстрый туннель Cloudflare без гарантий: при переподключении его удаляют
   («Tunnel not found»), и адрес умирает навсегда. Теперь присмотр видит это
   сам, поднимает новый туннель и переставляет кнопку в боте — владельцу
   ничего делать не надо.

3. Проверка отравляла роутер. Новый адрес проверяли сразу, через DNS
   роутера, пока в интернете его ещё не было. Роутер запоминал «такого сайта
   нет» и минутами отдавал это телефону в той же Wi-Fi — отсюда «1 раз из
   20». Теперь адрес проверяется только через публичный DNS, а кнопка в боте
   появляется лишь после того, как он устойчиво ответил.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

PORT = int(os.getenv("ORBIT_WEBAPP_PORT", "8137"))
HOST = "127.0.0.1"

# Два способа пробросить пульт наружу, и выбор между ними не вкусовой.
#
# Cloudflare бесплатен и без регистрации, но на сети владельца почти не
# работает: туннель регистрируется, а на первом же настоящем запросе
# соединение рвётся («context canceled»). Похоже на фильтрацию трафика к
# Cloudflare — предварительная проверка проходит, живые данные нет.
#
# Pinggy идёт по SSH на 443-й порт, и на той же сети отвечал десять раз из
# десяти. Но бесплатный Pinggy показывает телефонному браузеру свою страницу-
# предупреждение вместо сайта, то есть в Telegram открылась бы она, а не
# пульт. Поэтому он включается только с токеном платного тарифа.
CLOUDFLARE_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
PINGGY_URL = re.compile(
    r"https://[a-z0-9.-]+\.(?:pinggy\.link|pinggy\.online|pinggy\.net|pinggy-free\.link)"
)
URL_RE = CLOUDFLARE_URL  # для совместимости: лог cloudflared
# Строки, после которых туннель мёртв окончательно и ждать бесполезно:
# Cloudflare удалил его у себя, переподключения будут отбиваться вечно.
DEAD_MARKS = (
    "Tunnel not found", "Unauthorized", "no more connections active",
    "Connection closed by remote host", "Permission denied",
)
SSH_KEY = os.path.expanduser("~/.ssh/orbit_tunnel")

# Метка нашей страницы. По ней отличаем пульт от чужой заглушки: код ответа
# у заглушки тоже 200, и проверка по коду пропустила бы её в бота.
PAGE_MARK = "loaders.home"
PHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 Telegram-iOS"
)

# Публичные DNS. Роутер не спрашиваем НИКОГДА: он запоминает «адреса нет»
# и потом минутами отдаёт это телефону, даже когда адрес уже живой.
PUBLIC_DNS = ("1.1.1.1", "8.8.8.8")

# Сколько удачных ответов подряд считаем «работает». Одиночная удача ничего
# не доказывает: на переподключении адрес отвечает через раз.
READY_STREAK = 3
# Сколько неудач подряд считаем «умер». Одна неудача — моргнула сеть.
DEAD_STREAK = 3
CHECK_EVERY = 20.0
READY_TIMEOUT = 120.0


@dataclass
class Tunnel:
    """Один запущенный cloudflared и то, что мы про него узнали из лога."""

    process: subprocess.Popen
    url: str | None = None
    dead: str | None = None
    lines: list[str] = field(default_factory=list)


# Третий способ и основной: свой сервер. Мак держит до него SSH-канал, сервер
# отдаёт пульт по постоянному адресу со своим сертификатом. SSH на сети
# владельца работает стабильно (Pinggy по нему давал десять из десяти), а
# чужого сервиса с заглушками посередине нет. Адрес не меняется, поэтому
# кнопку в боте достаточно поставить один раз.
VPS_PORT = 18137
VPS_KEY = os.path.expanduser("~/.ssh/orbit_vps_tunnel")

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", "..", ".env")


def env_value(name: str) -> str:
    """Настройка из окружения или из orbit/.env.

    pydantic читает .env только для своих полей, а эти ему не объявлены:
    они нужны одному смотрителю. Поэтому читаем файл сами.
    """
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        with open(ENV_FILE, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def provider() -> str:
    """Каким туннелем пользоваться.

    Свой сервер, если он настроен; Pinggy — только с платным токеном (у
    бесплатного заглушка); иначе Cloudflare как последний вариант.
    """
    wanted = env_value("ORBIT_TUNNEL").lower()
    if wanted in ("vps", "pinggy", "cloudflare"):
        return wanted
    if env_value("ORBIT_VPS_HOST") and env_value("ORBIT_PUBLIC_URL"):
        return "vps"
    return "pinggy" if pinggy_token() else "cloudflare"


def pinggy_token() -> str:
    return env_value("ORBIT_PINGGY_TOKEN")


def vps_command(host: str, port: int = PORT) -> list[str]:
    """Канал с мака на свой сервер: порт сервера 127.0.0.1:VPS_PORT → пульт.

    -N — никаких команд, только проброс: ключ на сервере это и так разрешает
    одно-единственное, но пусть и мак не просит лишнего.
    """
    return [
        "ssh", "-N",
        "-i", VPS_KEY, "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=20",
        "-R", f"127.0.0.1:{VPS_PORT}:{HOST}:{port}",
        f"tunnel@{host}",
    ]


def pinggy_command(port: int = PORT, token: str = "") -> list[str]:
    who = f"{token}@pro.pinggy.io" if token else "a.pinggy.io"
    command = [
        "ssh", "-p", "443", "-tt",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=20",
        "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=20",
    ]
    if os.path.exists(SSH_KEY):
        command += ["-i", SSH_KEY, "-o", "IdentitiesOnly=yes"]
    return command + ["-R", f"0:{HOST}:{port}", who]


def cloudflared_command(port: int = PORT) -> list[str]:
    # Четыре подключения и IPv4: на одном любой обрыв — белый экран, пока
    # cloudflared переподключается. С четырьмя разрыв одного не виден.
    return [
        "cloudflared", "tunnel", "--no-autoupdate",
        "--protocol", "http2", "--edge-ip-version", "4", "--ha-connections", "4",
        "--url", f"http://{HOST}:{port}",
    ]


def read_line(tunnel: Tunnel, line: str) -> None:
    """Разбирает строку лога туннеля: адрес и признаки смерти."""
    tunnel.lines.append(line)
    del tunnel.lines[:-200]
    if tunnel.url is None:
        found = CLOUDFLARE_URL.search(line) or PINGGY_URL.search(line)
        if found:
            tunnel.url = found.group(0)
    for mark in DEAD_MARKS:
        if mark in line:
            tunnel.dead = mark


# Сюда пишется всё, что говорит cloudflared. Без этого файла причина «туннель
# не поднялся» оставалась внутри процесса, и чинить было вслепую.
TUNNEL_LOG = "/tmp/orbit_tunnel.log"


def start_tunnel() -> Tunnel:
    kind = provider()
    if kind == "vps":
        command = vps_command(env_value("ORBIT_VPS_HOST"))
    elif kind == "pinggy":
        command = pinggy_command(token=pinggy_token())
    else:
        command = cloudflared_command()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
    )
    tunnel = Tunnel(process)
    if kind == "vps":
        # Адрес постоянный и известен заранее — из лога его не выуживаем.
        tunnel.url = env_value("ORBIT_PUBLIC_URL").rstrip("/")

    def pump() -> None:
        with open(TUNNEL_LOG, "a", encoding="utf-8") as log:
            for line in process.stdout or ():
                log.write(line)
                log.flush()
                read_line(tunnel, line.rstrip())

    threading.Thread(target=pump, daemon=True).start()
    return tunnel


def stop_tunnel(tunnel: Tunnel | None) -> None:
    if tunnel is None or tunnel.process.poll() is not None:
        return
    tunnel.process.terminate()
    try:
        tunnel.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        tunnel.process.kill()


def public_ip(host: str) -> str | None:
    """Адрес хоста по публичному DNS. Роутер в обход — см. PUBLIC_DNS."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = list(PUBLIC_DNS)
    resolver.lifetime = 8.0
    try:
        answer = resolver.resolve(host, "A")
    except (dns.exception.DNSException, OSError):
        return None
    for record in answer:
        return record.to_text()
    return None


def answers(url: str) -> bool:
    """Отвечает ли адрес снаружи. Идём через публичный DNS, не через роутер."""
    host = url.removeprefix("https://").split("/")[0]
    ip = public_ip(host)
    if not ip:
        return False
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "12",
             "--resolve", f"{host}:443:{ip}", f"{url}/api/ping"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "200"


def serves_our_page(url: str) -> bool:
    """Получит ли телефон именно пульт, а не чужую страницу-заглушку.

    Проверяем браузером телефона и по содержимому, а не по коду: бесплатный
    Pinggy отдаёт телефону свою заглушку с кодом 200, и по коду она неотличима
    от пульта. Опубликуй мы такой адрес — в Telegram открылась бы она.
    """
    host = url.removeprefix("https://").split("/")[0]
    ip = public_ip(host)
    if not ip:
        return False
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "15", "-A", PHONE_UA,
             "--resolve", f"{host}:443:{ip}", f"{url}/"],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return PAGE_MARK in result.stdout


def wait_ready(tunnel: Tunnel, *, check=answers, timeout: float = READY_TIMEOUT,
               pause: float = 3.0, stop: threading.Event | None = None) -> bool:
    """Ждёт, пока туннель устойчиво отвечает снаружи. False — не дождались.

    stop — просьба остановиться. Паузу ждём на нём, а не через sleep: иначе
    Ctrl+C, нажатый в эти две минуты, срабатывал только по их истечении, и
    казалось, что пульт завис и не выключается.
    """
    stop = stop or threading.Event()
    deadline = time.monotonic() + timeout
    streak = 0
    while time.monotonic() < deadline and not stop.is_set():
        if tunnel.dead or tunnel.process.poll() is not None:
            return False
        if tunnel.url and check(tunnel.url):
            streak += 1
            if streak >= READY_STREAK:
                return True
        else:
            streak = 0
        stop.wait(pause)
    return False


def publish(url: str | None) -> None:
    """Записывает адрес в базу и переставляет кнопку в боте."""
    from ..config import get_settings
    from ..store import Store
    from . import link

    store = Store(get_settings().db_path)
    try:
        if url:
            link.remember(store, url)
        else:
            link.forget(store)
            from ..bot import publish_menu_button

            publish_menu_button(None)
    finally:
        store.close()


def start_app() -> threading.Thread:
    import uvicorn

    config = uvicorn.Config(
        "orbit.webapp.api:app", host=HOST, port=PORT,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def app_is_up() -> bool:
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5",
             f"http://{HOST}:{PORT}/api/ping"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "200"


def say(text: str) -> None:
    print(text, flush=True)


def clear_leftovers() -> int:
    """Гасит то, что оставил прежний bash-скрипт. Возвращает, сколько погасил.

    Его процессы замок не держат: они появились раньше замка. Оставь их — и
    новый пульт не сможет занять порт, а старый туннель будет и дальше
    раздавать мёртвый адрес. Трогаем строго своё: приложение, запущенное
    как `-m orbit.webapp` (не `.serve`), туннели на наш порт и сам скрипт.
    """
    patterns = (
        r"miniapp\.sh",
        r"-m orbit\.webapp$",
        rf"cloudflared tunnel.*127\.0\.0\.1:{PORT}",
    )
    killed = 0
    for pattern in patterns:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        for raw in result.stdout.split():
            pid = int(raw)
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except ProcessLookupError:
                pass
    if killed:
        time.sleep(2)
    return killed


def main() -> int:
    from ..bot import single_instance

    with single_instance("webapp") as mine:
        if not mine:
            say("Пульт уже запущен в другом окне. Второй не нужен — закрой это.")
            return 1
        leftovers = clear_leftovers()
        if leftovers:
            say(f"Погасил остатки старого запуска: {leftovers}")
        return _supervise()


def _supervise() -> int:
    stop = threading.Event()

    def on_signal(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    start_app()
    for _ in range(30):
        if app_is_up():
            break
        time.sleep(0.5)
    else:
        say("❌ Приложение не поднялось. Посмотри /tmp/orbit_webapp.log")
        return 1
    say(f"✅ Приложение работает на http://{HOST}:{PORT}")

    tunnel: Tunnel | None = None
    try:
        while not stop.is_set():
            say(f"Поднимаю туннель ({provider()})…")
            tunnel = start_tunnel()
            if not wait_ready(tunnel, stop=stop):
                if stop.is_set():
                    break
                reason = tunnel.dead or "не ответил за две минуты"
                say(f"⚠️  Туннель не поднялся ({reason}) — пробую новый")
                stop_tunnel(tunnel)
                stop.wait(5)
                continue

            if not serves_our_page(tunnel.url or ""):
                # Адрес отвечает, но телефону показывают не пульт. Ждать
                # бесполезно: это не сбой, а так устроен бесплатный тариф.
                say("❌ Туннель отвечает, но телефону показывает чужую страницу-заглушку.")
                say("   Так работает бесплатный Pinggy. Нужен токен Pro в orbit/.env:")
                say("   ORBIT_PINGGY_TOKEN=…   (или ORBIT_TUNNEL=cloudflare)")
                stop_tunnel(tunnel)
                tunnel = None
                return 1

            publish(tunnel.url)
            say("")
            say("════════════════════════════════════════════════")
            say(f"  Пульт открыт: {tunnel.url}")
            say("  В боте: кнопка «Пульт» слева от поля ввода")
            say("════════════════════════════════════════════════")
            say("Окно не закрывай. Упадёт туннель — подниму новый сам.")
            say("Остановить: Ctrl+C")

            misses = 0
            while not stop.is_set():
                stop.wait(CHECK_EVERY)
                if stop.is_set():
                    break
                if tunnel.dead or tunnel.process.poll() is not None:
                    say(f"⚠️  Туннель умер ({tunnel.dead or 'процесс вышел'}) — поднимаю новый")
                    break
                if answers(tunnel.url or ""):
                    misses = 0
                    continue
                misses += 1
                if misses >= DEAD_STREAK:
                    say("⚠️  Адрес перестал отвечать — поднимаю новый туннель")
                    break
            # Туннель мёртв или нас останавливают: кнопку в боте убираем
            # сразу, чтобы никто не нажал её и не получил белый экран.
            publish(None)
            stop_tunnel(tunnel)
            tunnel = None
    finally:
        stop_tunnel(tunnel)
        publish(None)
        say("Пульт остановлен, кнопка в боте снова показывает команды.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

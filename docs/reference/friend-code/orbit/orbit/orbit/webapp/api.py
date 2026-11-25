"""Мини-апп Orbit: цифры и управление в одном окне.

Зачем он рядом с ботом, а не вместо. Кнопки хороши для действий — «отправить»,
«закрыть», «позвонил»: одно нажатие, один результат. Для цифр они плохи:
таблицу в сообщение не положишь, график тем более, а листать воронку кнопками
мучительно. Бот дёргает, когда нужен человек; мини-апп открывают сами, когда
захотели посмотреть.

Ничего своего он не считает. Каждая цифра берётся той же функцией, что и в
боте, — иначе два экрана одной системы начнут расходиться, и владелец не
поймёт, какому верить.

База одна на всех, а запросов сюда приходит много и параллельно: SQLite
разрешает работать с соединением только из того потока, где оно создано,
поэтому на каждый запрос открывается своё и закрывается в конце.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger

from .. import alerts, digest, outreach, readiness
from ..config import get_settings
from ..models import STAGE_TITLES, Contact, Kind, Stage
from ..store import Store, today_msk
from . import auth

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Orbit", docs_url=None, redoc_url=None, openapi_url=None)


@contextmanager
def opened() -> Iterator[Store]:
    store = Store(get_settings().db_path)
    try:
        yield store
    finally:
        store.close()


def visitor(request: Request) -> auth.Visitor:
    """Проверяет подпись Telegram. Без неё дальше ничего не отдаём.

    Причину отказа пишем в лог вместе с путём. Телефон владельца отсюда не
    видно никак, и без этой строки «ничего не работает» не отличить от
    «подпись не сошлась» — а чинится это совершенно по-разному.
    """
    init_data = request.headers.get("X-Init-Data", "")
    host = request.client.host if request.client else ""
    try:
        return auth.check(init_data, client_host=host)
    except auth.NotAllowed as exc:
        logger.warning(
            "Мини-апп: отказ на {} — {} (подпись {})",
            request.url.path, exc, "есть" if init_data else "не пришла",
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc


Guard = Depends(visitor)

# Telegram держит страницы мини-аппа в своём кэше и после обновления кода
# показывает старую версию — со старыми кнопками и старыми ошибками. Для
# приложения, которое правится каждый день, это хуже медленной загрузки.
NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


def _page() -> str:
    """Вся страница одним ответом: стили, скрипт и знак внутри.

    Раньше страница тянула за собой ещё три файла. На рваном туннеле хоть
    один из них терялся — и владелец видел вёрстку без скрипта: всё на месте,
    ничего не нажимается. Одним запросом страница либо приходит целиком,
    либо не приходит вовсе, и тогда это хотя бы видно.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    logo = (STATIC / "logo.svg").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/app.css">', f"<style>{css}</style>")
    boot = (
        f"<script>window.ORBIT_LOGO={_js_string(logo)};"
        f"window.ORBIT_BUILD={_js_string(build_id())};</script>"
    )
    html = html.replace('<script src="/app.js"></script>', f"{boot}\n<script>{js}</script>")
    return html


def _js_string(text: str) -> str:
    import json

    # «</» внутри строки закрыл бы тег script раньше времени.
    return json.dumps(text).replace("</", "<\\/")


def build_id() -> str:
    """Короткий отпечаток версии: видно, какая сборка открыта на телефоне."""
    import hashlib

    digest = hashlib.sha1()
    for name in ("index.html", "app.css", "app.js", "logo.svg"):
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:7]


# --- Страница ---------------------------------------------------------------

@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(_page(), headers=NO_CACHE)


@app.get("/app.js")
def script() -> FileResponse:
    return FileResponse(STATIC / "app.js", media_type="application/javascript",
                        headers=NO_CACHE)


@app.get("/app.css")
def styles() -> FileResponse:
    return FileResponse(STATIC / "app.css", media_type="text/css", headers=NO_CACHE)


@app.get("/logo.svg")
def logo() -> FileResponse:
    return FileResponse(STATIC / "logo.svg", media_type="image/svg+xml")


@app.get("/api/ping")
def ping() -> dict:
    """Жив ли пульт. Без подписи: этим проверяют туннель, данных тут нет."""
    return {"ok": True, "build": build_id()}


# Сколько сообщений об ошибках принимаем с одного адреса в минуту. Приём
# открыт без подписи — иначе не узнать про ошибку, из-за которой подпись и
# не дошла, — поэтому держим его узким: короткий текст и не чаще раза в пару
# секунд.
CLIENT_LOG_PER_MINUTE = 30
_client_log_hits: dict[str, list[float]] = {}


@app.post("/api/clientlog")
async def client_log(request: Request) -> dict:
    """Ошибка со страницы на телефоне владельца — в наш лог."""
    import time

    host = request.client.host if request.client else "?"
    now = time.monotonic()
    hits = [at for at in _client_log_hits.get(host, []) if now - at < 60]
    if len(hits) >= CLIENT_LOG_PER_MINUTE:
        return {"ok": False}
    hits.append(now)
    _client_log_hits[host] = hits
    body = (await request.body())[:600].decode("utf-8", "replace")
    logger.warning("Мини-апп, ошибка на телефоне: {}", body)
    return {"ok": True}


# --- Главное ----------------------------------------------------------------

@app.get("/api/overview")
def overview(who: auth.Visitor = Guard) -> dict:
    """Первый экран: что требует человека и как шли дела сегодня."""
    with opened() as store:
        from ..bot import attention_lines

        exchange = digest.exchange_funnel(days=1)
        return {
            "who": who.name,
            "dev": who.dev,
            "day": today_msk(),
            "attention": [_plain(line) for line in attention_lines(store)],
            "today": {
                "letters": store.sent_today(Kind.PARTNER) + store.sent_today(Kind.CLIENT),
                "mailed": store.mailed_today(),
                "warmup": outreach.warmup_limit(store, Kind.PARTNER),
                "bids": exchange.sent if exchange else 0,
                "spent": round(exchange.spent, 0) if exchange else 0,
                "seen": exchange.seen if exchange else 0,
                "replies": exchange.replies if exchange else 0,
            },
            "queues": {
                "partner": store.queue_size(Kind.PARTNER),
                "client": store.queue_size(Kind.CLIENT),
            },
        }


@app.get("/api/funnel")
def funnel(days: int = Query(7, ge=1, le=90), who: auth.Visitor = Guard) -> dict:
    """Воронка по всем системам плюс ряд по дням для графика."""
    with opened() as store:
        sides = []
        for kind, title in (
            (Kind.PARTNER, "Партнёры"), (Kind.CLIENT, "Клиенты"), (Kind.ORDER, "Фриланс"),
        ):
            counts = store.stage_counts(kind)
            sides.append({
                "title": title,
                "kind": kind.value,
                "stages": [
                    {"code": stage.value, "title": STAGE_TITLES[stage],
                     "count": counts.get(stage, 0)}
                    for stage in digest.FUNNEL_STAGES
                ],
            })
        exchange = digest.exchange_funnel(days=days)
        return {
            "days": days,
            "sides": sides,
            "series": _series(store, days),
            "exchange": None if exchange is None else {
                "seen": exchange.seen,
                "sent": exchange.sent,
                "spent": round(exchange.spent, 0),
                "replies": exchange.replies,
                "per_reply": round(exchange.spent / exchange.replies, 0)
                if exchange.replies else None,
            },
        }


def _series(store: Store, days: int) -> list[dict]:
    """События по дням — то, из чего рисуется график.

    Считаем ровно из журнала событий: он и так ведётся по каждому письму,
    ответу и звонку, а отдельный счётчик разошёлся бы с ним на первой же
    правке базы руками.
    """
    today = date.fromisoformat(today_msk())
    out = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        counts = store.event_counts(day)
        out.append({
            "day": day,
            "sent": counts.get("sent", 0),
            "reply": counts.get("reply", 0),
            "bounce": counts.get("bounce", 0),
            "call": counts.get("call", 0),
        })
    return out


# --- Биржи ------------------------------------------------------------------

@app.get("/api/bids")
def bids(
    days: int = Query(60, ge=1, le=365),
    source: str = Query("", max_length=16),
    kind: str = Query("sent", pattern="^(sent|replied|all)$"),
    who: auth.Visitor = Guard,
) -> dict:
    """Лента откликов с текстами. Читает базу бирж напрямую, только на чтение.

    По умолчанию — только ОТПРАВЛЕННЫЕ. Отсеянных заказов на порядки больше
    (фильтры пропускают примерно один из десяти), и в общей ленте они
    вытесняли отправленные за край окна: экран «Отклики» показывал сплошной
    список того, куда мы не пошли.
    """
    from ..sections.freelance import _open_fbid_store

    try:
        store = _open_fbid_store()
    except Exception as exc:  # noqa: BLE001 — биржа не подключена
        logger.warning("Мини-апп: база бирж недоступна: {}", exc)
        return {"rows": [], "why": "Система бирж не отвечает"}

    since = (date.fromisoformat(today_msk()) - timedelta(days=days)).isoformat()
    try:
        # Отметку «клиент ответил» подтягиваем тем же запросом. Отдельным
        # проходом по каждому отклику это было бы триста запросов на экран,
        # а ответ клиента — главное, что на этом экране ищут.
        sql = """SELECT b.status, b.reason, b.text, b.cost_rub, b.bid_at, b.created_at,
                        o.title, o.url, o.source, o.budget_raw,
                        (SELECT value FROM state
                          WHERE key = 'clientreplied:' || o.source || ':' || o.ext_id
                        ) AS replied_at
                 FROM bids b JOIN orders o ON o.pk = b.order_pk
                 WHERE COALESCE(b.bid_at, b.created_at) >= ?"""
        params: list = [since]
        if source:
            sql += " AND o.source = ?"
            params.append(source)
        if kind == "sent":
            sql += " AND b.status = 'sent'"
        elif kind == "replied":
            sql += " AND b.status = 'sent' AND replied_at IS NOT NULL"
        sql += " ORDER BY COALESCE(b.bid_at, b.created_at) DESC LIMIT 300"
        rows = store.conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Мини-апп: отклики не прочитались: {}", exc)
        return {"rows": [], "why": "Отклики не прочитались"}
    finally:
        store.close()

    return {"rows": [
        {
            "title": row["title"],
            "url": row["url"],
            "source": row["source"],
            "budget": row["budget_raw"] or "",
            "status": row["status"],
            "reason": row["reason"] or "",
            "text": row["text"] or "",
            "cost": row["cost_rub"] or 0,
            "at": row["bid_at"] or row["created_at"] or "",
            # Дата первого ответа клиента. У старых записей там «1» — отметка
            # прежнего формата, датой она не была; такую отдавать нельзя,
            # иначе на экране появится «01.01.1970».
            "replied_at": _as_time(row["replied_at"]),
        }
        for row in rows
    ]}


# --- Контакты ---------------------------------------------------------------

def _as_time(raw: str | None) -> str:
    """Отметка времени или пусто. Защита от старого формата отметок.

    Раньше «клиент ответил» писалось единицей, без времени. Отдать её как
    дату значит показать владельцу «01.01.1970» и заставить гадать, что это.
    """
    value = (raw or "").split("|")[0].strip()
    if len(value) < 10 or value[4] != "-":
        return ""
    return value


@app.get("/api/contacts")
def contacts(
    kind: str = Query("partner"),
    stage: str = Query(""),
    q: str = Query("", max_length=80),
    who: auth.Visitor = Guard,
) -> dict:
    with opened() as store:
        if q:
            found = store.search_contacts(q, limit=50)
        else:
            found = store.contacts(_kind(kind), _stage(stage), limit=200)
        return {"rows": [_card(store, contact, short=True) for contact in found]}


@app.get("/api/contact/{pk}")
def contact(pk: int, who: auth.Visitor = Guard) -> dict:
    with opened() as store:
        found = store.get_contact(pk)
        if found is None:
            raise HTTPException(status_code=404, detail="Контакт не найден")
        return _card(store, found, short=False)


def _card(store: Store, contact: Contact, *, short: bool) -> dict:
    card = {
        "pk": contact.pk,
        "name": contact.name,
        "kind": contact.kind.value,
        "stage": contact.stage.value,
        "stage_title": STAGE_TITLES[contact.stage],
        "city": contact.city or "",
        "site": contact.site or "",
        "emails": contact.emails,
        "phones": contact.phones,
        "telegram": contact.telegram,
        "note": contact.note or "",
    }
    if short:
        return card
    card["history"] = [
        {"kind": row["kind"], "text": row["text"], "at": row["at"]}
        for row in store.events(contact.pk or 0, limit=40)
    ]
    draft = store.draft_for(contact.pk or 0)
    card["draft"] = None if draft is None else {
        "pk": draft.pk,
        "channel": draft.channel.value,
        "subject": draft.subject,
        "body": draft.body,
        "approved": draft.approved,
    }
    return card


# --- Очередь писем ----------------------------------------------------------

@app.get("/api/queue")
def queue(kind: str = Query("partner"), who: auth.Visitor = Guard) -> dict:
    with opened() as store:
        target = _kind(kind)
        rows = []
        for draft, contact in store.queue(target, limit=200):
            verdict = outreach.can_send(store, draft, contact)
            rows.append({
                "draft_pk": draft.pk,
                "contact_pk": contact.pk,
                "name": contact.name,
                "channel": draft.channel.value,
                "subject": draft.subject,
                "body": draft.body,
                "can_send": verdict.ok,
                "why": verdict.reason,
            })
        return {"rows": rows, "autosend": outreach.autosend_on(store)}


# --- Состояние и сводки -----------------------------------------------------

@app.get("/api/health")
def health(who: auth.Visitor = Guard) -> dict:
    with opened() as store:
        groups = []
        for title, checks in readiness.collect(store):
            groups.append({
                "title": title,
                "checks": [
                    {"title": check.title, "ok": check.ok,
                     "note": check.note, "who": check.who}
                    for check in checks
                ],
            })
        return {"groups": groups}


@app.get("/api/digest")
def digest_of_day(day: str = Query(""), who: auth.Visitor = Guard) -> dict:
    """Сводка за любой день. Тот же текст, что уходит вечером в телеграм."""
    when = day or today_msk()
    try:
        date.fromisoformat(when)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Непонятная дата") from exc
    with opened() as store:
        text = digest.build(store, day=when)
        return {"day": when, "text": _plain(text or "")}


# --- Управление -------------------------------------------------------------

@app.post("/api/send")
def send_letter(payload: dict = Body(...), who: auth.Visitor = Guard) -> dict:
    """Отправляет одно письмо из очереди. Дальше проверки те же, что в боте."""
    draft_pk = int(payload.get("draft_pk") or 0)
    if not draft_pk:
        raise HTTPException(status_code=400, detail="Не сказано, какое письмо")
    with opened() as store:
        store.approve_draft(draft_pk)
        result = outreach.send(store, draft_pk)
        return {"ok": result.ok, "why": result.reason, "manual": result.manual}


@app.post("/api/drop")
def drop_letter(payload: dict = Body(...), who: auth.Visitor = Guard) -> dict:
    draft_pk = int(payload.get("draft_pk") or 0)
    with opened() as store:
        return {"ok": store.drop_draft(draft_pk)}


@app.post("/api/stage")
def set_stage(payload: dict = Body(...), who: auth.Visitor = Guard) -> dict:
    pk = int(payload.get("pk") or 0)
    stage = _stage(str(payload.get("stage") or ""))
    if stage is None:
        raise HTTPException(status_code=400, detail="Непонятная стадия")
    with opened() as store:
        store.set_stage(pk, stage, str(payload.get("note") or "")[:200])
        return {"ok": True, "stage": stage.value}


@app.post("/api/autosend")
def toggle_autosend(payload: dict = Body(...), who: auth.Visitor = Guard) -> dict:
    want = bool(payload.get("on"))
    with opened() as store:
        from ..models import Channel

        outreach.set_autosend(store, Channel.EMAIL, want)
        return {"ok": True, "on": outreach.autosend_on(store)}


@app.get("/api/alerts")
def alert_switches(who: auth.Visitor = Guard) -> dict:
    """Что дёргает сразу, а что ждёт сводки. С переключателями."""
    with opened() as store:
        return {"rows": [
            {
                "code": alert.code,
                "title": alert.title,
                "why": alert.why,
                "instant": alerts.is_instant(store, alert),
                "default": alert.instant,
            }
            for alert in alerts.ALL
        ]}


@app.post("/api/alerts")
def set_alert(payload: dict = Body(...), who: auth.Visitor = Guard) -> dict:
    alert = alerts.BY_CODE.get(str(payload.get("code") or ""))
    if alert is None:
        raise HTTPException(status_code=400, detail="Неизвестное сообщение")
    with opened() as store:
        alerts.set_instant(store, alert, bool(payload.get("instant")))
        return {"ok": True, "instant": alerts.is_instant(store, alert)}


# --- Мелочи -----------------------------------------------------------------

def _kind(raw: str) -> Kind:
    try:
        return Kind(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Непонятный раздел") from exc


def _stage(raw: str) -> Stage | None:
    if not raw or raw == "all":
        return None
    try:
        return Stage(raw)
    except ValueError:
        return None


def _plain(text: str) -> str:
    """Убирает разметку телеграма: в вебе она не нужна и лезет в глаза.

    Текст один и тот же для бота и для мини-аппа намеренно — так формулировки
    не расходятся. Разметка при этом телеграмная, и показывать её как есть
    значит показать владельцу «<b>» посреди фразы.
    """
    import re
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


@app.exception_handler(HTTPException)
def tidy_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

"""HTTPS-reverse-proxied Telegram Mini App for the attendance journal.

Every API call authenticates Telegram WebApp initData and checks the existing
college link, student identity and starosta permissions on the server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import config
from moduls import attendance as store
from moduls import attendance_handlers as handlers
from moduls import db as dbm

WEB_DIR = Path(__file__).resolve().parent.parent / "webapp"
MAX_INIT_DATA_AGE = 3600


def verify_init_data(raw: str, token: str, *, now: int | None = None) -> dict:
    """Validate Telegram Mini App signed initData; never trust initDataUnsafe."""
    if not raw or len(raw) > 8192 or not token:
        raise ValueError("Откройте приложение из Telegram")
    try:
        fields = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        values = dict(fields)
        if len(fields) != len(values):
            raise ValueError("Дублирующиеся параметры")
        expected = values.pop("hash")
        timestamp = int(values["auth_date"])
        user = json.loads(values["user"])
        telegram_id = user["id"]
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Недопустимый пользователь")
        current = int(time.time()) if now is None else now
        if not 0 <= current - timestamp <= MAX_INIT_DATA_AGE:
            raise ValueError("Сессия Telegram устарела — откройте приложение снова")
        key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
        check = "\n".join(f"{name}={value}" for name, value in sorted(values.items()))
        actual = hmac.new(key, check.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise ValueError("Неверная подпись Telegram")
        return user
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError, OverflowError) as exc:
        raise ValueError("Некорректные данные Telegram") from exc


@dataclass(frozen=True)
class Actor:
    telegram_id: int
    user_id: int
    group: str
    login: str
    monitor: bool
    person_id: str | None


async def _actor(request: web.Request) -> Actor:
    telegram_id = request["telegram_id"]
    linked = await store.user_by_telegram(telegram_id)
    if linked is None:
        raise PermissionError("Сначала привяжите личный аккаунт ЛК в профиле бота")
    user_id, login, group = linked
    settings = await dbm.check_tg_id(telegram_id)
    is_admin = (len(settings) > 2 and bool(settings[2])) or telegram_id in request.app["get_settings"]().get("admins", [])
    monitor = is_admin or await store.is_starosta(user_id, group)
    person_id = await store.identity(user_id, login, group)
    return Actor(telegram_id, user_id, group, login, monitor, person_id)


def _requested_date(value: str | None) -> date:
    if not value:
        return handlers._today()
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректная дата") from exc
    if abs((day - handlers._today()).days) > 180:
        raise ValueError("Доступны даты в пределах 180 дней")
    return day


def _number(value: object) -> int:
    try:
        n = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Некорректный номер пары") from exc
    if not 1 <= n <= 20:
        raise ValueError("Некорректный номер пары")
    return n


async def _lesson(request: web.Request, actor: Actor, day: date, number: int):
    lessons = await handlers._lessons(
        actor.group, day, request.app["get_schedule"], request.app["get_reference"]
    )
    if number not in lessons:
        raise ValueError("Такой пары нет в расписании")
    title, starts, ends = lessons[number]
    sid, closed = await store.ensure_session(actor.group, day, number, title, starts, ends)
    return sid, closed, title, starts, ends


async def _body(request: web.Request) -> dict:
    if request.content_type != "application/json" or (request.content_length or 0) > 4096:
        raise ValueError("Ожидается небольшой JSON-запрос")
    try:
        value = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Некорректный JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Ожидается JSON-объект")
    return value


def _summary(marks: dict[str, int]) -> dict:
    attended = marks.get("present", 0) + marks.get("late", 0)
    missed = marks.get("absent", 0)
    counted = attended + missed
    return {
        "present": marks.get("present", 0), "late": marks.get("late", 0),
        "absent": missed, "excused": marks.get("excused", 0),
        "attended": attended, "total": counted,
        "percent": round(attended * 100 / counted) if counted else None,
    }


async def me(request: web.Request) -> web.Response:
    linked = await store.user_by_telegram(request["telegram_id"])
    if linked is None:
        return web.json_response({"linked": False, "role": "student"})
    actor = await _actor(request)
    return web.json_response({
        "linked": True, "group": actor.group,
        "role": "starosta" if actor.monitor else "student",
        "verified": bool(actor.person_id),
    })


async def day_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    day = _requested_date(request.query.get("date"))
    lessons = await handlers._lessons(
        actor.group, day, request.app["get_schedule"], request.app["get_reference"]
    )
    return web.json_response({
        "group": actor.group, "date": day.isoformat(),
        "lessons": [
            {"number": number, "subject": info[0], "start": info[1], "end": info[2]}
            for number, info in sorted(lessons.items())
        ],
    })


async def lesson_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    day = _requested_date(request.query.get("date"))
    number = _number(request.query.get("number"))
    sid, closed, title, starts, ends = await _lesson(request, actor, day, number)
    present = await store.marks(sid)
    members = await store.roster(actor.group)
    counts = {status: list(present.values()).count(status) for status in store.VALID_STATUSES}
    return web.json_response({
        "date": day.isoformat(), "number": number, "subject": title,
        "start": starts, "end": ends, "closed": closed,
        "can_self_mark": bool(actor.person_id) and not closed and handlers._can_self_mark(day, starts, ends)
            and actor.person_id not in present,
        "self_status": present.get(actor.person_id or ""),
        "verified": bool(actor.person_id), "monitor": actor.monitor,
        "counts": {**counts, "unmarked": max(len(members) - len(present), 0)},
        "students": [
            {"person_id": student.person_id, "name": student.full_name, "status": present.get(student.person_id)}
            for student in members
        ] if actor.monitor else [],
    })


async def students_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    if actor.person_id:
        raise ValueError("Ваша личность уже подтверждена")
    members = await store.roster(actor.group)
    return web.json_response({"students": [
        {"person_id": student.person_id, "name": student.full_name}
        for student in members
    ]})


async def claim_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    if actor.person_id:
        raise ValueError("Ваша личность уже подтверждена")
    payload = await _body(request)
    person_id = str(payload.get("person_id", ""))
    if not store.PERSON_ID_RE.fullmatch(person_id):
        raise ValueError("Некорректный ID студента")
    await store.claim_identity(actor.user_id, person_id, actor.login, actor.group)
    return web.json_response({"ok": True})


async def mark_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    payload = await _body(request)
    day = _requested_date(payload.get("date"))
    number = _number(payload.get("number"))
    sid, closed, _, starts, ends = await _lesson(request, actor, day, number)
    if bool(payload.get("self")):
        if not actor.person_id:
            raise PermissionError("Сначала подтвердите личность")
        if closed or not handlers._can_self_mark(day, starts, ends):
            raise ValueError("Сейчас нельзя самостоятельно отметиться на этой паре")
        await store.set_mark(sid, actor.person_id, "present", actor.telegram_id, only_if_unmarked=True)
    else:
        if not actor.monitor:
            raise PermissionError("Отмечать других может только староста")
        person_id = str(payload.get("person_id", ""))
        status = str(payload.get("status", ""))
        if not store.PERSON_ID_RE.fullmatch(person_id) or status not in store.VALID_STATUSES:
            raise ValueError("Некорректный студент или статус")
        await store.set_mark(sid, person_id, status, actor.telegram_id)
    return web.json_response({"ok": True})


async def close_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    if not actor.monitor:
        raise PermissionError("Закрывать журнал может только староста")
    payload = await _body(request)
    day = _requested_date(payload.get("date"))
    number = _number(payload.get("number"))
    sid, _, _, _, _ = await _lesson(request, actor, day, number)
    changed = await store.close_session(sid, actor.telegram_id)
    return web.json_response({"ok": True, "marked_absent": changed})


async def stats_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    group_stats = request.query.get("scope") == "group"
    if group_stats and not actor.monitor:
        raise PermissionError("Статистика группы доступна только старосте")
    if not group_stats and not actor.person_id:
        raise ValueError("Сначала подтвердите личность")
    students = await store.roster(actor.group)
    records = await store.statistics(actor.group)
    return web.json_response({"students": [
        {"name": student.full_name, **_summary(records.get(student.person_id, {}))}
        for student in students
        if group_stats or student.person_id == actor.person_id
    ]})


async def pending_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    if not actor.monitor:
        raise PermissionError("Недостаточно прав")
    data = await store.pending_claims(actor.group)
    return web.json_response({"claims": [
        {"user_id": uid, "telegram_id": telegram_id, "name": name}
        for uid, telegram_id, name, _ in data if uid != actor.user_id
    ]})


async def approve_view(request: web.Request) -> web.Response:
    actor = await _actor(request)
    if not actor.monitor:
        raise PermissionError("Недостаточно прав")
    payload = await _body(request)
    target = _number_user_id(payload.get("user_id"))
    if target == actor.user_id:
        raise PermissionError("Нельзя подтверждать себя")
    name = await store.approve_claim(target, actor.telegram_id, actor.group)
    return web.json_response({"ok": True, "name": name})


def _number_user_id(value: object) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Неверный ID пользователя") from exc
    if v < 1:
        raise ValueError("Неверный ID пользователя")
    return v


@web.middleware
async def guard(request: web.Request, handler):
    try:
        if request.path.startswith("/api/"):
            if request.method not in ("GET", "POST"):
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            init_data = request.headers.get("X-Telegram-Init-Data", "")
            user = verify_init_data(init_data, config.TOKEN)
            request["telegram_id"] = user["id"]
        response = await handler(request)
    except PermissionError as exc:
        response = web.json_response({"error": str(exc)}, status=403)
    except ValueError as exc:
        response = web.json_response({"error": str(exc)}, status=400)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.reason}, status=exc.status)
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def create_app(*, get_schedule, get_reference, get_settings) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=8192)
    app["get_schedule"] = get_schedule
    app["get_reference"] = get_reference
    app["get_settings"] = get_settings
    app.add_routes([
        web.get("/api/me", me),
        web.get("/api/day", day_view),
        web.get("/api/lesson", lesson_view),
        web.get("/api/students", students_view),
        web.get("/api/stats", stats_view),
        web.get("/api/pending", pending_view),
        web.post("/api/claim", claim_view),
        web.post("/api/mark", mark_view),
        web.post("/api/close", close_view),
        web.post("/api/approve", approve_view),
        web.get("/", lambda _: web.FileResponse(WEB_DIR / "index.html")),
        web.get("/app.js", lambda _: web.FileResponse(WEB_DIR / "app.js")),
        web.get("/styles.css", lambda _: web.FileResponse(WEB_DIR / "styles.css")),
    ])
    return app

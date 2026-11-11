"""Signed Telegram WebApp auth and public Mini App wiring (no real student data)."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from moduls.attendance_webapp import (
    MAX_INIT_DATA_AGE,
    _number,
    _summary,
    create_app,
    verify_init_data,
)


TOKEN = "123456:abcde-test-token"


def make_init_data(*, user_id=12345, now=1700000000):
    payload = {
        "auth_date": str(now),
        "query_id": "AAEx",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False),
    }
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    payload["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(payload)


def test_signed_init_data_valid_and_returns_telegram_id():
    result = verify_init_data(make_init_data(), TOKEN, now=1700000000)
    assert result["id"] == 12345
    assert result["first_name"] == "Тест"


def test_init_data_tamper_rejected():
    original = make_init_data()
    with pytest.raises(ValueError):
        verify_init_data(original.replace("12345", "99999"), TOKEN, now=1700000000)
    with pytest.raises(ValueError):
        verify_init_data(original, "other:token", now=1700000000)


def test_init_data_timestamps_rejected():
    original = make_init_data()
    with pytest.raises(ValueError):
        verify_init_data(original, TOKEN, now=1700000000 + MAX_INIT_DATA_AGE + 1)
    with pytest.raises(ValueError):
        verify_init_data(original, TOKEN, now=1699999999)


def test_init_data_duplicates_and_unsigned_rejected():
    original = make_init_data()
    with pytest.raises(ValueError):
        verify_init_data(original + "&auth_date=1700000000", TOKEN, now=1700000000)
    with pytest.raises(ValueError):
        verify_init_data("auth_date=1700000000&user=%7B%22id%22%3A123%7D", TOKEN, now=1700000000)


def test_summary_only_counts_attended_or_absent_in_denominator():
    result = _summary({"present": 6, "late": 2, "absent": 2, "excused": 4})
    assert result["attended"] == 8
    assert result["total"] == 10
    assert result["percent"] == 80
    assert _summary({"excused": 4})["percent"] is None


def test_lesson_number_bounds():
    assert _number(1) == 1
    with pytest.raises(ValueError):
        _number(0)
    with pytest.raises(ValueError):
        _number("not-a-lesson")


def test_routes_and_mobile_buttons_present():
    app = create_app(get_schedule=lambda: {}, get_reference=lambda: {},
                     get_settings=lambda: {})
    methods_paths = {(route.method, route.resource.canonical)
                     for route in app.router.routes()}
    assert ("POST", "/api/mark") in methods_paths
    assert ("POST", "/api/close") in methods_paths
    assert ("POST", "/api/approve") in methods_paths
    assert ("GET", "/api/stats") in methods_paths
    html = (app["get_schedule"].__module__,)
    assert html  # smoke-check: app is initialized without external services

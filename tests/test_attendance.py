"""Roster parser and calendar validation tests (synthetic students only)."""
from datetime import timedelta

import pytest

from moduls.attendance import parse_roster_html, same_group
from moduls.attendance_handlers import _key, _parse_day, _today


def test_parser_extracts_ids_and_names_only():
    markup = """
    <div class="chat-dialog person" data-person="5:1001" data-student>
      <img src="data:image/png;base64,NOT_NEEDED">
      <div class="chat-dialog-header-name">
        <a href="/user/5:1001/">Иванов Иван Иванович</a>
      </div>
      <div class="chat-dialog-message-text">
        <span class="own"></span>Группа т9-ИП-24-1
      </div>
    </div>
    <div class="chat-dialog person" data-person="5:1002" data-student>
      <div class="chat-dialog-header-name"><a>Петров Пётр Петрович</a></div>
      <div class="chat-dialog-message-text">Группа Т9-ИП-24-2</div>
    </div>
    <div class="chat-dialog person" data-person="5:2000">
      <div class="chat-dialog-header-name"><a>Преподаватель</a></div>
      <div class="chat-dialog-message-text">Группа Т9-ИП-24-1</div>
    </div>
    """
    students = parse_roster_html(markup, "Т9-ИП-24-1")
    assert len(students) == 1
    assert students[0].person_id == "5:1001"
    assert students[0].full_name == "Иванов Иван Иванович"


def test_parser_deduplicates_and_rejects_empty_group():
    card = """
    <div class="chat-dialog" data-student data-person="5:1001">
      <div class="chat-dialog-header-name"><a>Сидорова Анна Сергеевна</a></div>
      <div class="chat-dialog-message-text">Группа Т9-ИП-24-1</div>
    </div>
    """
    assert len(parse_roster_html(card * 2, "Т9-ИП-24-1")) == 1
    with pytest.raises(ValueError):
        parse_roster_html(card, "Т9-ИП-24-2")
    assert same_group("т9 - ИП -24-1", "Т9-ИП-24-1")


def test_invalid_person_ids_are_discarded():
    card = """
    <div class="chat-dialog" data-student data-person="not-an-id">
      <div class="chat-dialog-header-name"><a>Сидоров Иван Иванович</a></div>
      <div class="chat-dialog-message-text">Группа Т9-ИП-24-1</div>
    </div>
    """
    with pytest.raises(ValueError):
        parse_roster_html(card, "Т9-ИП-24-1")


def test_callback_dates_are_strict():
    today = _today()
    assert _parse_day(_key(today)) == today
    with pytest.raises(ValueError):
        _parse_day("20261111z")
    with pytest.raises(ValueError):
        _parse_day(_key(today + timedelta(days=181)))


def test_lk_roster_sync_posts_expected_form(monkeypatch):
    import asyncio

    from moduls import schedule as schedule_source
    from moduls.attendance import fetch_roster_from_lk

    markup = """
    <div class="chat-dialog person" data-student data-person="5:1001">
      <div class="chat-dialog-header-name"><a>Иванов Иван Иванович</a></div>
      <div class="chat-dialog-message-text">Группа Т9-ИП-24-1</div>
    </div>
    """

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def read(self):
            return self.body.encode("utf-8")

    class Session:
        def __init__(self):
            self.posts = []
            self.closed = False

        def get(self, url, **kwargs):
            assert url == "http://lk.stu.lipetsk.ru/chat/"
            return Response("<html>Chat</html>")

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response(markup)

        async def close(self):
            self.closed = True

    session = Session()

    async def authenticate(credentials):
        assert credentials["login"] == "fake-user"
        return session, "<html></html>", "http://lk.stu.lipetsk.ru/"

    monkeypatch.setattr(schedule_source, "_authenticate_recovery_session", authenticate)
    result = asyncio.run(fetch_roster_from_lk(
        "Т9-ИП-24-1", {"login": "fake-user", "password": "not-used"}
    ))
    assert result == markup
    assert session.closed
    assert len(session.posts) == 1
    url, kwargs = session.posts[0]
    assert url == "http://lk.stu.lipetsk.ru/ajax.handler.php"
    assert kwargs["data"] == {
        "search_person": "1", "type": "", "value": "Т9-ИП-24-1"
    }
    assert kwargs["allow_redirects"] is False

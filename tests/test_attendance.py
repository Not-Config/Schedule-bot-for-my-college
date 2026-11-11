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

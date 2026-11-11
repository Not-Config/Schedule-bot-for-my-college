"""Private-chat attendance controls for the existing aiogram dispatcher."""
from __future__ import annotations

import html
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import arrow
from aiogram import F, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from moduls import attendance as store
from moduls import db as dbm
from moduls import time as tm

STATUS_LABEL = {
    "present": "✅ Присутствует",
    "late": "🕒 Опоздал",
    "absent": "❌ Отсутствует",
    "excused": "📄 Уважительная причина",
}
STATUS_ICON = {
    "present": "✅", "late": "🕒", "absent": "❌", "excused": "📄",
}
PER_PAGE = 8


def _today() -> date:
    return arrow.now(config.BOT_TIMEZONE).date()


def _parse_day(value: str) -> date:
    if len(value) != 8 or not value.isascii() or not value.isdigit():
        raise ValueError("Неверная дата")
    day = date(int(value[:4]), int(value[4:6]), int(value[6:]))
    if abs((day - _today()).days) > 180:
        raise ValueError("Можно открывать журнал только в пределах 180 дней")
    return day


def _key(day: date) -> str:
    return day.strftime("%Y%m%d")


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _reply(event, text: str, buttons: InlineKeyboardBuilder | None = None):
    markup = buttons.as_markup() if buttons is not None else None
    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(
                text, reply_markup=markup, parse_mode="HTML"
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                raise
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


async def _current_user(event, is_admin):
    if (
        isinstance(event, types.Message)
        and event.chat.type != ChatType.PRIVATE
    ) or (
        isinstance(event, types.CallbackQuery)
        and event.message.chat.type != ChatType.PRIVATE
    ):
        if isinstance(event, types.CallbackQuery):
            await event.answer("Посещаемость доступна только в личном чате", show_alert=True)
        else:
            await event.answer("Напишите боту в личные сообщения")
        return None
    data = await dbm.check_tg_id(event.from_user.id)
    group = str(data[1]) if len(data) > 1 and data[1] else ""
    link = await dbm.get_college_account_link(int(data[0]))
    if not group or link is None or not store.same_group(group, link.group_name):
        await _reply(
            event,
            "Для журнала посещаемости сначала привяжите свой аккаунт ЛК "
            "в разделе «Профиль». Простого выбора группы недостаточно.",
        )
        return None
    monitor = is_admin(data, event.from_user.id) or await store.is_starosta(
        int(data[0]), group
    )
    return int(data[0]), group, link, monitor


async def _lessons(group, day, get_schedule, get_reference):
    ref = get_reference()
    weekday, color = await tm.get_this_weekday(
        ref["date"], ref["color"], date=day.strftime("%d.%m.%Y")
    )
    entries = get_schedule().get(group, {}).get(weekday, {})
    result = {}
    for number, lesson in entries.items():
        if not str(number).isdigit():
            continue
        info = lesson.get(color) or {}
        if not any(info.get(field) for field in ("title", "teacher", "room")):
            continue
        result[int(number)] = (
            str(info.get("title") or "Пара"),
            str(lesson.get("time", {}).get("start") or ""),
            str(lesson.get("time", {}).get("end") or ""),
        )
    return result


def _can_self_mark(day: date, start: str, end: str) -> bool:
    if day != _today():
        return False
    try:
        timezone = ZoneInfo(config.BOT_TIMEZONE)
        begins = datetime.combine(day, time.fromisoformat(start), timezone)
        finishes = datetime.combine(day, time.fromisoformat(end), timezone)
        now = datetime.now(timezone)
        return begins - timedelta(minutes=15) <= now <= finishes + timedelta(minutes=15)
    except ValueError:
        return False


def install(dp, *, get_schedule, get_reference, is_admin) -> None:
    """Register before the main module's catch-all message handler."""

    async def day_view(event, day: date):
        context = await _current_user(event, is_admin)
        if context is None:
            return
        _, group, _, monitor = context
        lessons = await _lessons(group, day, get_schedule, get_reference)
        k = InlineKeyboardBuilder()
        for number, (subject, starts, _) in lessons.items():
            k.row(_button(f"{number}. {subject[:35]} · {starts}",
                          f"at:lesson:{_key(day)}:{number}"))
        k.row(
            _button("◀️", f"at:day:{_key(day - timedelta(days=1))}"),
            _button("Сегодня", "at:home"),
            _button("▶️", f"at:day:{_key(day + timedelta(days=1))}"),
        )
        k.row(_button("Моя статистика", "at:my"))
        if monitor:
            k.row(_button("Статистика группы", "at:stats"),
                  _button("Заявки", "at:pending"))
        k.row(_button("Главное меню", "back"))
        line = "\n".join(
            ["<b>Журнал посещаемости</b>",
             f"Группа: {html.escape(group)}",
             f"Дата: {day.strftime('%d.%m.%Y')}", "",
             "Выберите пару:" if lessons else "На этот день пары не найдены."]
        )
        await _reply(event, line, k)

    async def session_view(event, day: date, number: int):
        context = await _current_user(event, is_admin)
        if context is None:
            return
        user_id, group, link, monitor = context
        lessons = await _lessons(group, day, get_schedule, get_reference)
        if number not in lessons:
            if isinstance(event, types.CallbackQuery):
                await event.answer("Этой пары нет в расписании", show_alert=True)
            return
        subject, starts, ends = lessons[number]
        session_id, closed = await store.ensure_session(
            group, day, number, subject, starts, ends
        )
        members = await store.roster(group)
        saved = await store.marks(session_id)
        counts = {status: list(saved.values()).count(status)
                  for status in STATUS_LABEL}
        counts["unknown"] = max(0, len(members) - len(saved))
        person = await store.identity(user_id, link.login, group)
        k = InlineKeyboardBuilder()
        if person and not closed and _can_self_mark(day, starts, ends):
            k.row(_button("✅ Я на паре", f"at:self:{_key(day)}:{number}"))
        if not person:
            k.row(_button("👤 Выбрать себя в списке", "at:claim:0"))
        if monitor:
            k.row(_button("Список группы", f"at:roster:{_key(day)}:{number}:0"))
            if not closed:
                k.row(_button("🔒 Закрыть журнал", f"at:confirm:{_key(day)}:{number}"))
        k.row(_button("Назад", f"at:day:{_key(day)}"))
        text = (
            f"<b>{number}-я пара · {html.escape(subject)}</b>\n"
            f"{day.strftime('%d.%m.%Y')} · {html.escape(starts)}–{html.escape(ends)}\n"
            f"Журнал: {'закрыт' if closed else 'открыт'}\n\n"
            f"✅ {counts['present']} · 🕒 {counts['late']} · "
            f"❌ {counts['absent']} · 📄 {counts['excused']}\n"
            f"Не отмечены: {counts['unknown']}\n\n"
            "Самостоятельная отметка доступна только во время пары "
            "и не доказывает присутствие."
        )
        await _reply(event, text, k)

    async def roster_view(event, day: date, number: int, page: int):
        context = await _current_user(event, is_admin)
        if context is None:
            return
        _, group, _, monitor = context
        if not monitor:
            await event.answer("Только староста или администратор", show_alert=True)
            return
        lessons = await _lessons(group, day, get_schedule, get_reference)
        if number not in lessons:
            await event.answer("Пара не найдена", show_alert=True)
            return
        subject, start, end = lessons[number]
        session_id, closed = await store.ensure_session(
            group, day, number, subject, start, end
        )
        students = await store.roster(group)
        existing = await store.marks(session_id)
        if page < 0 or page * PER_PAGE >= max(len(students), 1):
            await event.answer("Страница не найдена", show_alert=True)
            return
        k = InlineKeyboardBuilder()
        for s in students[page * PER_PAGE:(page + 1) * PER_PAGE]:
            state = STATUS_ICON.get(existing.get(s.person_id), "➖")
            k.row(_button(
                f"{state} {s.full_name[:36]}",
                f"at:edit:{_key(day)}:{number}:{s.person_id}"
            ))
        pages = (len(students) + PER_PAGE - 1) // PER_PAGE
        if pages > 1:
            k.row(
                _button("◀️", f"at:roster:{_key(day)}:{number}:{max(page-1, 0)}"),
                _button(f"{page+1}/{pages}", f"at:roster:{_key(day)}:{number}:{page}"),
                _button("▶️", f"at:roster:{_key(day)}:{number}:{min(page+1, pages-1)}"),
            )
        k.row(_button("Назад к паре", f"at:lesson:{_key(day)}:{number}"))
        await _reply(event, f"<b>Список группы · {number}-я пара</b>\n"
                     f"Журнал: {'закрыт' if closed else 'открыт'}\n"
                     "Нажмите на студента для отметки.", k)

    async def edit_view(event, day: date, number: int, person_id: str):
        context = await _current_user(event, is_admin)
        if context is None:
            return
        _, group, _, monitor = context
        if not monitor:
            await event.answer("Недостаточно прав", show_alert=True)
            return
        students = await store.roster(group)
        student = next((s for s in students if s.person_id == person_id), None)
        if student is None:
            await event.answer("Студент не найден", show_alert=True)
            return
        lessons = await _lessons(group, day, get_schedule, get_reference)
        if number not in lessons:
            await event.answer("Пара не найдена", show_alert=True)
            return
        k = InlineKeyboardBuilder()
        for code, title in STATUS_LABEL.items():
            k.row(_button(title, f"at:set:{_key(day)}:{number}:{person_id}:{code}"))
        k.row(_button("Назад к списку", f"at:roster:{_key(day)}:{number}:0"))
        await _reply(event, f"<b>{html.escape(student.full_name)}</b>\n"
                     f"{number}-я пара · {day.strftime('%d.%m.%Y')}\n"
                     "Как отметить посещаемость?", k)

    async def claim_view(event, page: int):
        context = await _current_user(event, is_admin)
        if context is None:
            return
        user_id, group, link, _ = context
        if await store.identity(user_id, link.login, group):
            await event.answer("Ваша личность уже подтверждена", show_alert=True)
            return
        students = await store.roster(group)
        if page < 0 or page * PER_PAGE >= max(len(students), 1):
            await event.answer("Неверная страница", show_alert=True)
            return
        k = InlineKeyboardBuilder()
        for student in students[page*PER_PAGE:(page+1)*PER_PAGE]:
            k.row(_button(student.full_name[:50], f"at:pick:{student.person_id}"))
        if len(students) > PER_PAGE:
            pages = (len(students) + PER_PAGE - 1) // PER_PAGE
            k.row(
                _button("◀️", f"at:claim:{max(0, page-1)}"),
                _button(f"{page+1}/{pages}", f"at:claim:{page}"),
                _button("▶️", f"at:claim:{min(pages-1, page+1)}"),
            )
        k.row(_button("Назад", "at:home"))
        await _reply(event,
                     "<b>Привязка к списку группы</b>\n"
                     "Выберите своё ФИО. После этого староста или "
                     "администратор подтвердит заявку.", k)

    async def pending_view(event):
        context = await _current_user(event, is_admin)
        if context is None:
            return
        _, group, _, monitor = context
        if not monitor:
            await event.answer("Недостаточно прав", show_alert=True)
            return
        pending = await store.pending_claims(group)
        k = InlineKeyboardBuilder()
        for internal_id, tg_id, full_name, _ in pending:
            if tg_id == event.from_user.id:
                continue
            k.row(_button(f"Подтвердить · {full_name[:29]}",
                          f"at:approve:{internal_id}"))
        k.row(_button("Назад", "at:home"))
        info = "\n".join(
            f"• {html.escape(name)} — TG <code>{tg_id}</code>"
            for _, tg_id, name, _ in pending
        )
        await _reply(event, "<b>Заявки на привязку</b>\n"
                     + (info or "Заявок нет") + "\n\n"
                     "Подтверждайте только тех, чью личность вы проверили.", k)

    async def stats_view(event, personal: bool):
        context = await _current_user(event, is_admin)
        if context is None:
            return
        user_id, group, link, monitor = context
        if not personal and not monitor:
            await event.answer("Статистика группы доступна старосте", show_alert=True)
            return
        own_id = await store.identity(user_id, link.login, group)
        if personal and not own_id:
            await _reply(event, "Сначала выберите себя в списке и получите подтверждение.")
            return
        students = await store.roster(group)
        stats = await store.statistics(group)
        lines = ["<b>Статистика посещаемости</b>",
                 f"Группа: {html.escape(group)}", ""]
        for student in students:
            if personal and student.person_id != own_id:
                continue
            marks = stats.get(student.person_id, {})
            attended = marks.get("present", 0) + marks.get("late", 0)
            missed = marks.get("absent", 0)
            considered = attended + missed
            percentage = round(100 * attended / considered) if considered else 0
            percent_display = f"{percentage}%" if considered else "н/д"
            lines.append(
                f"{html.escape(student.full_name)}: {percent_display} "
                f"({attended}/{considered}, уваж. {marks.get('excused', 0)})"
            )
        if len(lines) == 3:
            lines.append("Пока нет данных")
        k = InlineKeyboardBuilder()
        k.row(_button("Назад", "at:home"))
        await _reply(event, "\n".join(lines)[:4000], k)

    async def attendance_cmd(message: types.Message):
        await day_view(message, _today())

    async def import_cmd(message: types.Message):
        data = await dbm.check_tg_id(message.from_user.id)
        if message.chat.type != ChatType.PRIVATE or not is_admin(data, message.from_user.id):
            await message.answer("Импорт доступен только администратору в личном чате")
            return
        args = (message.text or "").split(maxsplit=1)
        reply = message.reply_to_message
        if len(args) != 2 or not reply or not reply.document:
            await message.answer(
                "Ответьте на HTML/TXT-файл из Network командой "
                "<code>/attendance_import Т9-ИП-24-1</code>", parse_mode="HTML"
            )
            return
        group = args[1].strip()
        if group not in get_schedule():
            await message.answer("Такой группы нет в расписании бота")
            return
        if (reply.document.file_size or 0) > 2_000_000:
            await message.answer("Файл больше 2 МБ")
            return
        try:
            content = (await message.bot.download(reply.document)).getvalue()
            count = await store.import_roster(content.decode("utf-8-sig"), group)
        except (ValueError, UnicodeError) as exc:
            await message.answer(f"Не удалось импортировать: {html.escape(str(exc))}",
                                 parse_mode="HTML")
            return
        await message.answer(f"Импортировано {count} записей группы {group}. "
                             "Не найденные в этом ответе студенты не удалялись.")

    async def starosta_cmd(message: types.Message):
        data = await dbm.check_tg_id(message.from_user.id)
        if message.chat.type != ChatType.PRIVATE or not is_admin(data, message.from_user.id):
            await message.answer("Недостаточно прав")
            return
        args = (message.text or "").split()
        if len(args) != 2 or not args[1].isdigit():
            await message.answer("Использование: /attendance_starosta TELEGRAM_ID")
            return
        try:
            group = await store.grant_starosta(int(args[1]))
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"Староста назначен для группы {group}.")

    async def attendance_callbacks(callback: types.CallbackQuery):
        try:
            bits = (callback.data or "").split(":")
            action = bits[1]
            if action == "home":
                await day_view(callback, _today())
            elif action == "day":
                await day_view(callback, _parse_day(bits[2]))
            elif action == "lesson":
                await session_view(callback, _parse_day(bits[2]), int(bits[3]))
            elif action == "roster":
                await roster_view(callback, _parse_day(bits[2]), int(bits[3]), int(bits[4]))
            elif action == "edit":
                await edit_view(callback, _parse_day(bits[2]), int(bits[3]),
                                ":".join(bits[4:6]))
            elif action == "claim":
                await claim_view(callback, int(bits[2]))
            elif action == "pick":
                context = await _current_user(callback, is_admin)
                if context is None:
                    return
                user_id, group, link, _ = context
                if await store.identity(user_id, link.login, group):
                    await callback.answer("Личность уже подтверждена", show_alert=True)
                    return
                await store.claim_identity(user_id, ":".join(bits[2:]), link.login, group)
                await _reply(callback, "Заявка отправлена. Дождитесь подтверждения старосты.")
            elif action == "pending":
                await pending_view(callback)
            elif action == "approve":
                context = await _current_user(callback, is_admin)
                if context is None:
                    return
                user_id, group, _, monitor = context
                if not monitor or user_id == int(bits[2]):
                    await callback.answer("Недостаточно прав", show_alert=True)
                    return
                name = await store.approve_claim(
                    int(bits[2]), callback.from_user.id, group
                )
                await callback.answer(f"Подтверждено: {name}")
                await pending_view(callback)
            elif action in ("self", "set", "confirm", "close"):
                context = await _current_user(callback, is_admin)
                if context is None:
                    return
                user_id, group, link, monitor = context
                day = _parse_day(bits[2])
                number = int(bits[3])
                lessons = await _lessons(group, day, get_schedule, get_reference)
                if number not in lessons:
                    raise ValueError("Пара не найдена")
                title, starts, ends = lessons[number]
                if action == "self":
                    person = await store.identity(user_id, link.login, group)
                    if person is None:
                        raise ValueError("Ваша личность ещё не подтверждена")
                    if not _can_self_mark(day, starts, ends):
                        raise ValueError("Отметка доступна только во время этой пары")
                    sid, closed = await store.ensure_session(
                        group, day, number, title, starts, ends
                    )
                    if closed:
                        raise ValueError("Журнал закрыт")
                    await store.set_mark(
                        sid, person, "present", callback.from_user.id,
                        only_if_unmarked=True
                    )
                    await session_view(callback, day, number)
                elif action == "set":
                    if not monitor:
                        raise ValueError("Недостаточно прав")
                    person = ":".join(bits[4:6])
                    status = bits[6]
                    sid, _ = await store.ensure_session(
                        group, day, number, title, starts, ends
                    )
                    await store.set_mark(sid, person, status, callback.from_user.id)
                    await edit_view(callback, day, number, person)
                elif action == "confirm":
                    if not monitor:
                        raise ValueError("Недостаточно прав")
                    k = InlineKeyboardBuilder()
                    k.row(_button("Да, закрыть и отметить пропуски",
                                  f"at:close:{_key(day)}:{number}"))
                    k.row(_button("Отмена", f"at:lesson:{_key(day)}:{number}"))
                    await _reply(callback, "Все оставшиеся без отметки студенты "
                                 "будут записаны отсутствующими. Продолжить?", k)
                elif action == "close":
                    if not monitor:
                        raise ValueError("Недостаточно прав")
                    sid, _ = await store.ensure_session(
                        group, day, number, title, starts, ends
                    )
                    await store.close_session(sid, callback.from_user.id)
                    await session_view(callback, day, number)
            elif action == "stats":
                await stats_view(callback, False)
            elif action == "my":
                await stats_view(callback, True)
            else:
                await callback.answer("Неизвестное действие", show_alert=True)
        except (ValueError, IndexError) as exc:
            await callback.answer(str(exc)[:180], show_alert=True)

    dp.message.register(attendance_cmd, Command("attendance"))
    dp.message.register(import_cmd, Command("attendance_import"))
    dp.message.register(starosta_cmd, Command("attendance_starosta"))
    dp.callback_query.register(attendance_callbacks, F.data.startswith("at:"))

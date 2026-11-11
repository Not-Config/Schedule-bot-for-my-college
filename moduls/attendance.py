"""Attendance roster, identity verification, marking and audit storage.

All roster data stays in MariaDB, never in the public repository.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from bs4 import BeautifulSoup

from moduls import db as dbm

Status = Literal["present", "late", "absent", "excused"]
VALID_STATUSES = {"present", "late", "absent", "excused"}
PERSON_ID_RE = re.compile(r"^\d+:\d+$")


@dataclass(frozen=True)
class Student:
    person_id: str
    full_name: str
    group_name: str


def same_group(a: str, b: str) -> bool:
    return "".join(a.split()).casefold() == "".join(b.split()).casefold()


def parse_roster_html(source: str, group_name: str) -> list[Student]:
    """Parse only student cards; never decode untrusted inline avatar data."""
    if len(source.encode("utf-8")) > 2_000_000:
        raise ValueError("Файл списка слишком большой (не более 2 МБ)")
    soup = BeautifulSoup(source, "html.parser")
    students: dict[str, Student] = {}
    for card in soup.select(".chat-dialog[data-student][data-person]"):
        person_id = str(card.get("data-person", "")).strip()
        name_link = card.select_one(".chat-dialog-header-name a")
        group_text = card.select_one(".chat-dialog-message-text")
        if not PERSON_ID_RE.fullmatch(person_id) or not name_link or not group_text:
            continue
        name = " ".join(name_link.get_text(" ", strip=True).split())
        label = " ".join(group_text.get_text(" ", strip=True).split())
        match = re.search(r"(?:^|\s)Группа\s+(\S+)", label, re.IGNORECASE)
        if not match or not same_group(match.group(1), group_name):
            continue
        if not 3 <= len(name) <= 256:
            continue
        students[person_id] = Student(person_id, name, group_name)
    if not students:
        raise ValueError("В файле нет карточек студентов указанной группы")
    return sorted(students.values(), key=lambda item: item.full_name.casefold())


async def ensure_schema() -> None:
    conn = await dbm.connect()
    statements = (
        """CREATE TABLE IF NOT EXISTS attendance_roster (
            person_id VARCHAR(64) NOT NULL PRIMARY KEY,
            group_name VARCHAR(64) NOT NULL,
            full_name VARCHAR(256) NOT NULL,
            active TINYINT(1) NOT NULL DEFAULT 1,
            INDEX ix_roster_group (group_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS attendance_roles (
            user_id BIGINT NOT NULL,
            group_name VARCHAR(64) NOT NULL,
            role VARCHAR(20) NOT NULL,
            PRIMARY KEY (user_id, group_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS attendance_claims (
            user_id BIGINT NOT NULL PRIMARY KEY,
            person_id VARCHAR(64) NOT NULL,
            group_name VARCHAR(64) NOT NULL,
            login_snapshot VARCHAR(256) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS attendance_identities (
            user_id BIGINT NOT NULL PRIMARY KEY,
            person_id VARCHAR(64) NOT NULL,
            group_name VARCHAR(64) NOT NULL,
            login_snapshot VARCHAR(256) NOT NULL,
            approved_by BIGINT NOT NULL,
            approved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_identity_person (person_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS attendance_sessions (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            group_name VARCHAR(64) NOT NULL,
            lesson_date DATE NOT NULL,
            lesson_no TINYINT UNSIGNED NOT NULL,
            subject VARCHAR(256) NOT NULL,
            starts_at VARCHAR(5) NOT NULL,
            ends_at VARCHAR(5) NOT NULL,
            closed_at DATETIME NULL,
            UNIQUE KEY uq_session (group_name, lesson_date, lesson_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS attendance_marks (
            session_id BIGINT UNSIGNED NOT NULL,
            person_id VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL,
            actor_tg_id BIGINT NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, person_id),
            INDEX ix_marks_person (person_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS attendance_events (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            session_id BIGINT UNSIGNED NOT NULL,
            person_id VARCHAR(64) NOT NULL,
            old_status VARCHAR(16) NULL,
            new_status VARCHAR(16) NOT NULL,
            actor_tg_id BIGINT NOT NULL,
            changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_events_session (session_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    )
    try:
        async with conn.cursor() as cur:
            for stmt in statements:
                await cur.execute(stmt)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def import_roster(source: str, group_name: str) -> int:
    students = parse_roster_html(source, group_name)
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            for student in students:
                await cur.execute(
                    """INSERT INTO attendance_roster (person_id, group_name, full_name)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE group_name=VALUES(group_name),
                        full_name=VALUES(full_name), active=1""",
                    (student.person_id, student.group_name, student.full_name),
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()
    # A search response may be incomplete; absent students are never deleted.
    return len(students)


async def roster(group_name: str) -> list[Student]:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT person_id, full_name, group_name
                FROM attendance_roster WHERE group_name=%s AND active=1
                ORDER BY full_name, person_id""",
                (group_name,),
            )
            return [Student(*map(str, row)) for row in await cur.fetchall()]
    finally:
        conn.close()


async def is_starosta(user_id: int, group_name: str) -> bool:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT 1 FROM attendance_roles
                WHERE user_id=%s AND group_name=%s AND role='starosta'""",
                (user_id, group_name),
            )
            return await cur.fetchone() is not None
    finally:
        conn.close()


async def user_by_telegram(tg_id: int) -> tuple[int, str, str] | None:
    """(internal ID, verified LK login, verified group)."""
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT u.id, l.login, l.group_name
                FROM users u JOIN college_account_links l ON l.user_id=u.id
                JOIN settings s ON s.id=u.id AND s.group_name=l.group_name
                WHERE u.tg_id=%s""",
                (tg_id,),
            )
            row = await cur.fetchone()
            return (int(row[0]), str(row[1]), str(row[2])) if row else None
    finally:
        conn.close()


async def grant_starosta(target_tg_id: int) -> str:
    target = await user_by_telegram(target_tg_id)
    if target is None:
        raise ValueError("У пользователя нет подтверждённой привязки ЛК и группы")
    user_id, _, group = target
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO attendance_roles (user_id, group_name, role)
                VALUES (%s, %s, 'starosta')
                ON DUPLICATE KEY UPDATE role='starosta'""",
                (user_id, group),
            )
        await conn.commit()
        return group
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()



async def revoke_starosta(target_tg_id: int) -> int:
    """Revoke starosta access for this Telegram user in every group."""
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """DELETE r FROM attendance_roles r JOIN users u ON u.id=r.user_id
                WHERE u.tg_id=%s AND r.role='starosta'""",
                (target_tg_id,),
            )
            count = int(cur.rowcount)
        await conn.commit()
        return count
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def unlink_identity(target_tg_id: int) -> int:
    """Clear an incorrect approved identity and pending claim (admin only)."""
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM users WHERE tg_id=%s", (target_tg_id,))
            row = await cur.fetchone()
            if row is None:
                return 0
            internal_id = int(row[0])
            await cur.execute(
                "DELETE FROM attendance_identities WHERE user_id=%s",
                (internal_id,),
            )
            deleted = int(cur.rowcount)
            await cur.execute(
                "DELETE FROM attendance_claims WHERE user_id=%s",
                (internal_id,),
            )
        await conn.commit()
        return deleted
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def claim_identity(user_id: int, person_id: str, login: str, group: str) -> None:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT 1 FROM attendance_roster
                WHERE person_id=%s AND group_name=%s AND active=1""",
                (person_id, group),
            )
            if await cur.fetchone() is None:
                raise ValueError("Студент не найден в списке этой группы")
            await cur.execute(
                """INSERT INTO attendance_claims (user_id, person_id, group_name, login_snapshot)
                VALUES (%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE person_id=VALUES(person_id),
                    group_name=VALUES(group_name),
                    login_snapshot=VALUES(login_snapshot), created_at=CURRENT_TIMESTAMP""",
                (user_id, person_id, group, login),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def pending_claims(group: str) -> list[tuple[int, int, str, str]]:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT c.user_id, u.tg_id, r.full_name, c.person_id
                FROM attendance_claims c
                JOIN users u ON u.id=c.user_id
                JOIN attendance_roster r ON r.person_id=c.person_id
                    AND r.group_name=c.group_name
                WHERE c.group_name=%s
                ORDER BY c.created_at LIMIT 40""",
                (group,),
            )
            return [(int(a), int(b), str(c), str(d)) for a,b,c,d in await cur.fetchall()]
    finally:
        conn.close()


async def approve_claim(target_user_id: int, actor_tg_id: int, group: str) -> str:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT c.person_id, c.login_snapshot, r.full_name
                FROM attendance_claims c
                JOIN attendance_roster r ON r.person_id=c.person_id
                    AND r.group_name=c.group_name AND r.active=1
                JOIN college_account_links l ON l.user_id=c.user_id
                    AND l.login=c.login_snapshot AND l.group_name=c.group_name
                JOIN settings s ON s.id=c.user_id AND s.group_name=c.group_name
                WHERE c.user_id=%s AND c.group_name=%s FOR UPDATE""",
                (target_user_id, group),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError("Заявка устарела или привязка ЛК изменилась")
            person_id, login, name = map(str, row)
            await cur.execute(
                """SELECT user_id FROM attendance_identities
                WHERE person_id=%s FOR UPDATE""", (person_id,),
            )
            occupied = await cur.fetchone()
            if occupied and int(occupied[0]) != target_user_id:
                raise ValueError("Этот студент уже привязан к другому Telegram")
            await cur.execute(
                """INSERT INTO attendance_identities
                    (user_id, person_id, group_name, login_snapshot, approved_by)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE person_id=VALUES(person_id),
                    group_name=VALUES(group_name), login_snapshot=VALUES(login_snapshot),
                    approved_by=VALUES(approved_by), approved_at=CURRENT_TIMESTAMP""",
                (target_user_id, person_id, group, login, actor_tg_id),
            )
            await cur.execute("DELETE FROM attendance_claims WHERE user_id=%s",
                              (target_user_id,))
        await conn.commit()
        return name
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def identity(user_id: int, login: str, group: str) -> str | None:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT i.person_id FROM attendance_identities i
                JOIN attendance_roster r ON r.person_id=i.person_id
                WHERE i.user_id=%s AND i.login_snapshot=%s
                    AND i.group_name=%s AND r.group_name=%s AND r.active=1""",
                (user_id, login, group, group),
            )
            row = await cur.fetchone()
            return str(row[0]) if row else None
    finally:
        conn.close()


async def ensure_session(group: str, day: date, lesson_no: int,
                         subject: str, starts: str, ends: str) -> tuple[int, bool]:
    if not 1 <= lesson_no <= 20:
        raise ValueError("Неверный номер пары")
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO attendance_sessions
                (group_name, lesson_date, lesson_no, subject, starts_at, ends_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)""",
                (group, day, lesson_no, subject[:256], starts[:5], ends[:5]),
            )
            session_id = int(cur.lastrowid)
            await cur.execute("SELECT closed_at FROM attendance_sessions WHERE id=%s",
                              (session_id,))
            row = await cur.fetchone()
        await conn.commit()
        return session_id, row[0] is not None
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def marks(session_id: int) -> dict[str, str]:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT person_id, status FROM attendance_marks WHERE session_id=%s",
                (session_id,),
            )
            return {str(a): str(b) for a,b in await cur.fetchall()}
    finally:
        conn.close()


async def set_mark(session_id: int, person_id: str, status: Status,
                   actor_tg_id: int, only_if_unmarked: bool = False) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError("Неизвестный статус")
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_name, closed_at FROM attendance_sessions WHERE id=%s FOR UPDATE",
                (session_id,),
            )
            session = await cur.fetchone()
            if not session:
                raise ValueError("Пара не найдена")
            if only_if_unmarked and session[1] is not None:
                raise ValueError("Журнал пары уже закрыт")
            await cur.execute(
                """SELECT 1 FROM attendance_roster
                WHERE group_name=%s AND person_id=%s AND active=1""",
                (session[0], person_id),
            )
            if not await cur.fetchone():
                raise ValueError("Студент не состоит в группе")
            await cur.execute(
                """SELECT status FROM attendance_marks
                WHERE session_id=%s AND person_id=%s FOR UPDATE""",
                (session_id, person_id),
            )
            previous = await cur.fetchone()
            old = str(previous[0]) if previous else None
            if only_if_unmarked and previous:
                raise ValueError("Для вас уже есть отметка; обратитесь к старосте")
            if old == status:
                return False
            await cur.execute(
                """INSERT INTO attendance_marks
                (session_id, person_id, status, actor_tg_id)
                VALUES (%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status=VALUES(status),
                    actor_tg_id=VALUES(actor_tg_id),
                    updated_at=CURRENT_TIMESTAMP""",
                (session_id, person_id, status, actor_tg_id),
            )
            await cur.execute(
                """INSERT INTO attendance_events
                (session_id, person_id, old_status, new_status, actor_tg_id)
                VALUES (%s,%s,%s,%s,%s)""",
                (session_id, person_id, old, status, actor_tg_id),
            )
        await conn.commit()
        return True
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def close_session(session_id: int, actor_tg_id: int) -> int:
    """Snapshot all unmarked students as absent, with audit entries."""
    conn = await dbm.connect()
    count = 0
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_name, closed_at FROM attendance_sessions WHERE id=%s FOR UPDATE",
                (session_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError("Пара не найдена")
            if row[1] is not None:
                return 0
            await cur.execute(
                """SELECT r.person_id FROM attendance_roster r
                LEFT JOIN attendance_marks m ON m.person_id=r.person_id
                    AND m.session_id=%s
                WHERE r.group_name=%s AND r.active=1 AND m.person_id IS NULL""",
                (session_id, row[0]),
            )
            missing = [str(r[0]) for r in await cur.fetchall()]
            for person_id in missing:
                await cur.execute(
                    """INSERT INTO attendance_marks
                    (session_id, person_id, status, actor_tg_id)
                    VALUES (%s,%s,'absent',%s)""",
                    (session_id, person_id, actor_tg_id),
                )
                await cur.execute(
                    """INSERT INTO attendance_events
                    (session_id, person_id, old_status, new_status, actor_tg_id)
                    VALUES (%s,%s,NULL,'absent',%s)""",
                    (session_id, person_id, actor_tg_id),
                )
                count += 1
            await cur.execute(
                "UPDATE attendance_sessions SET closed_at=NOW() WHERE id=%s",
                (session_id,),
            )
        await conn.commit()
        return count
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


async def statistics(group: str) -> dict[str, dict[str, int]]:
    conn = await dbm.connect()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT m.person_id, m.status, COUNT(*)
                FROM attendance_marks m
                JOIN attendance_sessions s ON s.id=m.session_id
                JOIN attendance_roster r ON r.person_id=m.person_id
                    AND r.group_name=s.group_name
                WHERE s.group_name=%s
                GROUP BY m.person_id, m.status""",
                (group,),
            )
            result: dict[str, dict[str, int]] = {}
            for person_id, status, amount in await cur.fetchall():
                result.setdefault(str(person_id), {})[str(status)] = int(amount)
            return result
    finally:
        conn.close()

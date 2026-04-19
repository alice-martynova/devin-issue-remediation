import logging
import os
from datetime import datetime
from typing import Optional

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "/data/sessions.db")
logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"finished", "expired"}


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id           TEXT PRIMARY KEY,
                issue_number         INTEGER NOT NULL,
                issue_title          TEXT NOT NULL,
                issue_user           TEXT,
                repo_full_name       TEXT NOT NULL,
                devin_status         TEXT DEFAULT 'working',
                last_notified_status TEXT,
                pr_url               TEXT,
                pr_merged            INTEGER DEFAULT 0,
                issue_closed         INTEGER DEFAULT 0,
                devin_session_url    TEXT,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            )
        """)
        for col, definition in [
            ("last_notified_status", "TEXT"),
            ("pr_merged", "INTEGER DEFAULT 0"),
            ("issue_user", "TEXT"),
            ("issue_closed", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        await db.commit()
    logger.info(f"Database initialised at {DB_PATH}")


async def record_session(
    session_id: str,
    issue_number: int,
    issue_title: str,
    issue_user: str,
    repo_full_name: str,
    devin_session_url: str,
) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO sessions
                (session_id, issue_number, issue_title, issue_user, repo_full_name,
                 devin_status, last_notified_status, devin_session_url,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'working', NULL, ?, ?, ?)
            """,
            (session_id, issue_number, issue_title, issue_user, repo_full_name,
             devin_session_url, now, now),
        )
        await db.commit()


async def update_session(
    session_id: str,
    devin_status: str,
    pr_url: Optional[str] = None,
) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE sessions
            SET devin_status = ?, pr_url = COALESCE(?, pr_url), updated_at = ?
            WHERE session_id = ?
            """,
            (devin_status, pr_url, now, session_id),
        )
        await db.commit()


async def update_notified_status(session_id: str, status: str) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET last_notified_status = ?, updated_at = ? WHERE session_id = ?",
            (status, now, session_id),
        )
        await db.commit()


async def get_active_session_by_issue(
    issue_number: int, repo_full_name: str
) -> Optional[dict]:
    """Return the active (non-terminal) session for an issue, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        cursor = await db.execute(
            f"""
            SELECT * FROM sessions
            WHERE issue_number = ? AND repo_full_name = ?
              AND devin_status NOT IN ({placeholders})
            LIMIT 1
            """,
            (issue_number, repo_full_name, *TERMINAL_STATUSES),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_session_by_pr_number(pr_number: int, repo_full_name: str) -> Optional[dict]:
    """Return the most recent session whose pr_url contains the given PR number."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM sessions
            WHERE repo_full_name = ? AND pr_url LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (repo_full_name, f"%/pull/{pr_number}"),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def session_exists_for_issue(issue_number: int, repo_full_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM sessions WHERE issue_number = ? AND repo_full_name = ? LIMIT 1",
            (issue_number, repo_full_name),
        )
        return await cursor.fetchone() is not None


async def get_all_sessions() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_pr_merged(session_id: str) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET pr_merged = 1, updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        await db.commit()


async def update_issue_closed(session_id: str) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET issue_closed = 1, updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        await db.commit()


async def get_active_sessions() -> list[dict]:
    """Returns sessions that still need Devin polling or PR merge checking."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        cursor = await db.execute(
            f"""
            SELECT * FROM sessions
            WHERE devin_status NOT IN ({placeholders})
               OR (pr_url IS NOT NULL AND pr_merged = 0)
            ORDER BY created_at DESC
            """,
            tuple(TERMINAL_STATUSES),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_metrics() -> dict:
    sessions = await get_all_sessions()
    total = len(sessions)
    by_status: dict[str, int] = {}
    for s in sessions:
        status = s["devin_status"] or "unknown"
        by_status[status] = by_status.get(status, 0) + 1

    finished = by_status.get("finished", 0)
    success_rate = round((finished / total * 100), 1) if total > 0 else 0.0

    return {
        "total": total,
        "by_status": by_status,
        "success_rate": success_rate,
        "prs_created": sum(1 for s in sessions if s.get("pr_url")),
        "prs_merged": sum(1 for s in sessions if s.get("pr_merged")),
        "blocked_count": by_status.get("blocked", 0),
        "expired_count": by_status.get("expired", 0),
    }

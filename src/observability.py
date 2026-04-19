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
                session_id         TEXT PRIMARY KEY,
                issue_number       INTEGER NOT NULL,
                issue_title        TEXT NOT NULL,
                repo_full_name     TEXT NOT NULL,
                devin_status       TEXT DEFAULT 'working',
                pr_url             TEXT,
                devin_session_url  TEXT,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            )
        """)
        await db.commit()
    logger.info(f"Database initialised at {DB_PATH}")


async def record_session(
    session_id: str,
    issue_number: int,
    issue_title: str,
    repo_full_name: str,
    devin_session_url: str,
) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO sessions
                (session_id, issue_number, issue_title, repo_full_name,
                 devin_status, devin_session_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'working', ?, ?, ?)
            """,
            (session_id, issue_number, issue_title, repo_full_name,
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


async def get_active_sessions() -> list[dict]:
    all_sessions = await get_all_sessions()
    return [s for s in all_sessions if s["devin_status"] not in TERMINAL_STATUSES]


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
    }

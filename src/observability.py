import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "/data/sessions.db")
logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"finished", "expired", "error"}


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id           TEXT PRIMARY KEY,
                issue_number         INTEGER NOT NULL,
                issue_title          TEXT NOT NULL,
                issue_user           TEXT,
                repo_full_name       TEXT NOT NULL,
                devin_status         TEXT DEFAULT 'pending',
                last_notified_status TEXT,
                pr_url               TEXT,
                pr_merged            INTEGER DEFAULT 0,
                issue_closed         INTEGER DEFAULT 0,
                devin_session_url    TEXT,
                devin_commented      INTEGER DEFAULT 0,
                error_message        TEXT,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            )
        """)
        for col, definition in [
            ("last_notified_status", "TEXT"),
            ("pr_merged", "INTEGER DEFAULT 0"),
            ("issue_user", "TEXT"),
            ("issue_closed", "INTEGER DEFAULT 0"),
            ("devin_commented", "INTEGER DEFAULT 0"),
            ("error_message", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists

        # Prevent duplicate active sessions for the same issue. Allow new
        # sessions to be created once a prior one terminates.
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_session_per_issue
            ON sessions (issue_number, repo_full_name)
            WHERE devin_status NOT IN ('finished', 'expired', 'error')
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS failed_webhooks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                handler    TEXT NOT NULL,
                context    TEXT NOT NULL,
                error      TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()
    logger.info(f"Database initialised at {DB_PATH}")


async def record_pending_session(
    placeholder_id: str,
    issue_number: int,
    issue_title: str,
    issue_user: str,
    repo_full_name: str,
) -> bool:
    """Insert a new session row in pending state before the Devin API call.

    Returns True if inserted (this caller claimed the issue), False if an
    active session for this (issue_number, repo_full_name) already exists
    (concurrent webhook delivery or pre-existing session).
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO sessions
                (session_id, issue_number, issue_title, issue_user, repo_full_name,
                 devin_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (placeholder_id, issue_number, issue_title, issue_user,
             repo_full_name, now, now),
        )
        await db.commit()
        return cursor.rowcount == 1


async def activate_pending_session(
    placeholder_id: str,
    session_id: str,
    devin_session_url: str,
) -> None:
    """Transition a pending row to working once Devin has accepted the session."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE sessions
            SET session_id = ?, devin_status = 'working',
                devin_session_url = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (session_id, devin_session_url, now, placeholder_id),
        )
        await db.commit()


async def set_session_error(session_id: str, error_message: str) -> None:
    """Mark a session as errored (e.g. Devin API rejected the request)."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE sessions
            SET devin_status = 'error', error_message = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (error_message, now, session_id),
        )
        await db.commit()


async def update_devin_commented(session_id: str) -> None:
    """Record that Devin has posted its first comment on the GitHub issue."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET devin_commented = 1, updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        await db.commit()


async def get_stale_pending_sessions(threshold_minutes: int = 15) -> list[dict]:
    """Return pending sessions older than threshold_minutes with no real session_id yet."""
    cutoff = (datetime.utcnow() - timedelta(minutes=threshold_minutes)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE devin_status = 'pending' AND created_at < ?",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Legacy helper kept for backward compatibility and direct test use.
# Production flow should use record_pending_session + activate_pending_session.
# ---------------------------------------------------------------------------
async def record_session(
    session_id: str,
    issue_number: int,
    issue_title: str,
    issue_user: str,
    repo_full_name: str,
    devin_session_url: str,
) -> bool:
    """Insert a session row directly in working state.

    Returns True if inserted, False if a row for this (issue, repo) already
    exists (same session_id re-recorded or concurrent webhook delivery).
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
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
        return cursor.rowcount == 1


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
    """Return the active (non-terminal, non-pending) session for an issue, or None.

    Excludes pending sessions since they have no real Devin session_id yet
    and cannot receive relayed comments.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        terminal = tuple(TERMINAL_STATUSES | {"pending"})
        placeholders = ",".join("?" * len(terminal))
        cursor = await db.execute(
            f"""
            SELECT * FROM sessions
            WHERE issue_number = ? AND repo_full_name = ?
              AND devin_status NOT IN ({placeholders})
            LIMIT 1
            """,
            (issue_number, repo_full_name, *terminal),
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
    """Return True only if there is a non-terminal active session for this issue.

    Terminal sessions (finished, expired, error) are excluded so that a
    reopened issue can spawn a new Devin session rather than being silently
    skipped.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        skip = tuple(TERMINAL_STATUSES)
        placeholders = ",".join("?" * len(skip))
        cursor = await db.execute(
            f"SELECT 1 FROM sessions WHERE issue_number = ? AND repo_full_name = ?"
            f" AND devin_status NOT IN ({placeholders}) LIMIT 1",
            (issue_number, repo_full_name, *skip),
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
    """Return sessions that need Devin polling or PR merge checking.

    Includes:
    - Non-terminal, non-pending sessions (active Devin sessions to poll).
    - Expired sessions — Devin can resume after a quota increase.
    - Finished sessions with no PR URL yet — keep retrying URL extraction.
    - Terminal sessions with an unmerged PR (need merge status checked).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # error and pending are hard stops: error is unrecoverable,
        # pending has no real session_id to poll against.
        hard_skip = tuple({"error", "pending"})
        hard_skip_placeholders = ",".join("?" * len(hard_skip))
        terminal = tuple(TERMINAL_STATUSES)
        terminal_placeholders = ",".join("?" * len(terminal))
        cursor = await db.execute(
            f"""
            SELECT * FROM sessions
            WHERE devin_status NOT IN ({hard_skip_placeholders})
              AND NOT (devin_status = 'finished' AND pr_url IS NOT NULL AND pr_merged = 1)
               OR (pr_url IS NOT NULL AND pr_merged = 0
                   AND devin_status IN ({terminal_placeholders}))
            ORDER BY created_at DESC
            """,
            (*hard_skip, *terminal),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def record_failed_webhook(handler: str, context: dict, error: str) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO failed_webhooks (handler, context, error, created_at) VALUES (?, ?, ?, ?)",
            (handler, json.dumps(context, default=str), error, now),
        )
        await db.commit()


async def get_failed_webhooks(limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM failed_webhooks ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["context"] = json.loads(d["context"])
            except (TypeError, ValueError):
                pass
            result.append(d)
        return result


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
        "error_count": by_status.get("error", 0),
    }

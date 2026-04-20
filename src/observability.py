import json
import logging
import os
from datetime import datetime
from typing import Optional
from uuid import uuid4

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "/data/sessions.db")
logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"finished", "expired"}
PLACEHOLDER_SESSION_PREFIX = "pending-"


def _new_placeholder_id() -> str:
    return f"{PLACEHOLDER_SESSION_PREFIX}{uuid4()}"


def is_placeholder_session_id(session_id: str) -> bool:
    return bool(session_id) and session_id.startswith(PLACEHOLDER_SESSION_PREFIX)


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
            ("error_message", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists

        # Prevent duplicate active sessions for the same issue while still
        # allowing retries once a prior row is effectively "done":
        #   - finished / expired: natural Devin terminal states
        #   - devin-stopped:      Devin could not work (token / 5xx / network)
        #   - issue_closed=1:     GitHub issue closed (covers the close → reopen
        #                         retry path even if the row's devin_status is
        #                         still 'working' or 'blocked')
        # The `issue-opened` placeholder is intentionally still covered by the
        # index so two concurrent webhook deliveries for the same issue can't
        # both spawn a Devin session.
        #
        # The index predicate changed from the original version, so drop any
        # older copy first — CREATE INDEX IF NOT EXISTS would silently keep
        # the stale predicate on existing deployments.
        await db.execute("DROP INDEX IF EXISTS idx_active_session_per_issue")
        await db.execute("""
            CREATE UNIQUE INDEX idx_active_session_per_issue
            ON sessions (issue_number, repo_full_name)
            WHERE devin_status NOT IN ('finished', 'expired', 'devin-stopped')
              AND issue_closed = 0
        """)

        # Dead-letter log for webhook handlers that threw before completing.
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


async def record_issue_opened(
    issue_number: int,
    issue_title: str,
    issue_user: str,
    repo_full_name: str,
) -> Optional[str]:
    """Insert a placeholder row for a freshly-opened issue.

    We want the dashboard to show an "Issue Opened" row the moment GitHub
    delivers the webhook — even if creating the Devin session later fails
    (e.g. token-limit exhaustion). The placeholder carries a synthetic
    ``pending-<uuid>`` session_id that `session_manager.handle_issue_opened`
    rewrites with the real Devin session id on success.

    Returns the placeholder session_id on insert, or None if a row already
    exists for this (issue_number, repo_full_name) — either a duplicate
    webhook delivery or an earlier active session still in flight.
    """
    placeholder_id = _new_placeholder_id()
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO sessions
                (session_id, issue_number, issue_title, issue_user, repo_full_name,
                 devin_status, last_notified_status, devin_session_url,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'issue-opened', NULL, NULL, ?, ?)
            """,
            (placeholder_id, issue_number, issue_title, issue_user,
             repo_full_name, now, now),
        )
        await db.commit()
        if cursor.rowcount != 1:
            return None
    return placeholder_id


async def promote_to_working(
    placeholder_id: str,
    real_session_id: str,
    devin_session_url: str,
) -> bool:
    """Replace a placeholder's synthetic session_id with the real one and
    flip it to `working`. Returns False if the placeholder is gone (e.g. the
    issue was closed & archived before Devin finished starting) or if the
    swap would collide with an existing row.
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute(
                """
                UPDATE sessions
                SET session_id = ?, devin_status = 'working',
                    devin_session_url = ?, error_message = NULL,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (real_session_id, devin_session_url, now, placeholder_id),
            )
            await db.commit()
        except Exception as exc:
            logger.warning(
                "promote_to_working(%s -> %s) failed: %s",
                placeholder_id, real_session_id, exc,
            )
            return False
        return cursor.rowcount == 1


async def mark_devin_stopped(
    placeholder_id: str,
    error: str,
) -> None:
    """Flip a session to `devin-stopped` so the dashboard surfaces that Devin
    could not work on the issue (typically a token-limit or API error).
    Works on both placeholder and real session rows.
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE sessions
            SET devin_status = 'devin-stopped',
                error_message = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (error[:500], now, placeholder_id),
        )
        await db.commit()


async def mark_user_action_from_github(
    session_id: str,
    reason: str,
    pr_url: Optional[str] = None,
) -> None:
    """Force a session to `blocked` (User Action) from a GitHub signal —
    Devin bot commenting on an issue, Devin bot opening a PR, etc. — so the
    dashboard does not depend solely on Devin-API polling to surface the
    "waiting on user" state.

    No-ops if the session is already terminal or already blocked, so repeated
    GitHub echoes don't overwrite the recorded reason.
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE sessions
            SET devin_status = 'blocked',
                pr_url = COALESCE(?, pr_url),
                error_message = ?,
                updated_at = ?
            WHERE session_id = ?
              AND devin_status NOT IN ('finished', 'expired', 'blocked')
            """,
            (pr_url, reason, now, session_id),
        )
        await db.commit()


async def record_session(
    session_id: str,
    issue_number: int,
    issue_title: str,
    issue_user: str,
    repo_full_name: str,
    devin_session_url: str,
) -> bool:
    """Insert a new session row.

    Returns True if a row was inserted (this caller claimed the issue), False
    if an equivalent session already existed — either because the same
    session_id was re-recorded (Devin idempotency) or because another active
    session for this (issue_number, repo_full_name) tuple blocked the insert
    via the partial unique index. Callers should use the return value to
    decide whether to post first-touch side effects (e.g. the initial GitHub
    comment) exactly once.
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
    }

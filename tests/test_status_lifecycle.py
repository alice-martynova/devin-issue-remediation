"""Tests for the GitHub-driven status lifecycle.

These tests cover the statuses introduced so the dashboard can surface
operational problems (token/system failures) and "User Action" signals
that originate from GitHub, not only from Devin API polling:

    - `issue-opened`  — placeholder written on webhook arrival
    - `devin-stopped` — flipped when `create_session` raises
    - `blocked` via Devin-bot issue comment
    - `blocked` via Devin-bot `pull_request.opened`
"""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src import observability


@pytest.fixture
def client(monkeypatch, tmp_path):
    from src import main as main_module

    monkeypatch.setattr(main_module, "_background_poller", AsyncMock())
    monkeypatch.setattr(main_module, "_print_ngrok_url", AsyncMock())
    monkeypatch.setattr(observability, "DB_PATH", str(tmp_path / "sessions.db"))

    with TestClient(main_module.app) as c:
        yield c


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def _post(client, event: str, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": _sign(body)},
    )


class TestIssueOpenedPlaceholder:
    def test_webhook_writes_issue_opened_row_before_devin_call(self, client):
        """The dashboard should see the issue the moment GitHub pings us —
        before (and independent of) the Devin session creation succeeding."""
        payload = {
            "action": "opened",
            "issue": {
                "number": 101,
                "title": "bug",
                "body": "",
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "alice/repo", "default_branch": "main"},
        }

        # Block the background handler so we can observe the placeholder before
        # it is promoted / failed.
        async def _never_finish(**kwargs):
            import asyncio
            await asyncio.sleep(3600)

        with patch(
            "src.main.session_manager.handle_issue_opened",
            side_effect=_never_finish,
        ):
            resp = _post(client, "issues", payload)

        assert resp.status_code == 202
        resp2 = client.get("/sessions")
        sessions = resp2.json()
        assert len(sessions) == 1
        row = sessions[0]
        assert row["devin_status"] == "issue-opened"
        assert row["issue_number"] == 101
        assert row["session_id"].startswith("pending-")

    def test_duplicate_webhook_delivery_is_deduped(self, client):
        payload = {
            "action": "opened",
            "issue": {
                "number": 200,
                "title": "bug",
                "body": "",
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "alice/repo", "default_branch": "main"},
        }

        async def _never_finish(**kwargs):
            import asyncio
            await asyncio.sleep(3600)

        with patch(
            "src.main.session_manager.handle_issue_opened",
            side_effect=_never_finish,
        ):
            r1 = _post(client, "issues", payload)
            r2 = _post(client, "issues", payload)

        assert r1.status_code == 202
        assert r2.status_code == 202
        sessions = client.get("/sessions").json()
        assert len(sessions) == 1, "second delivery must not create a second row"


class TestDevinStopped:
    async def test_create_session_failure_marks_devin_stopped(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(observability, "DB_PATH", str(tmp_path / "s.db"))
        await observability.init_db()

        placeholder_id = await observability.record_issue_opened(
            issue_number=7,
            issue_title="title",
            issue_user="alice",
            repo_full_name="alice/repo",
        )
        assert placeholder_id is not None

        from src.session_manager import SessionManager

        devin = AsyncMock()
        devin.create_session.side_effect = RuntimeError(
            "402 Payment Required: token limit exceeded"
        )
        github = AsyncMock()
        sm = SessionManager(devin_client=devin, github_client=github)

        result = await sm.handle_issue_opened(
            placeholder_id=placeholder_id,
            issue_number=7,
            issue_title="title",
            issue_body="body",
            repo_full_name="alice/repo",
            default_branch="main",
        )
        assert result is None

        row = await observability.get_active_session_by_issue(7, "alice/repo")
        assert row is not None
        assert row["devin_status"] == "devin-stopped"
        assert "token limit exceeded" in row["error_message"]

    async def test_create_session_success_promotes_to_working(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(observability, "DB_PATH", str(tmp_path / "s.db"))
        await observability.init_db()

        placeholder_id = await observability.record_issue_opened(
            issue_number=8,
            issue_title="title",
            issue_user="alice",
            repo_full_name="alice/repo",
        )

        from src.session_manager import SessionManager

        devin = AsyncMock()
        devin.create_session.return_value = {
            "session_id": "real-sess-1",
            "url": "https://app.devin.ai/sessions/real-sess-1",
        }
        github = AsyncMock()
        sm = SessionManager(devin_client=devin, github_client=github)

        session_id = await sm.handle_issue_opened(
            placeholder_id=placeholder_id,
            issue_number=8,
            issue_title="title",
            issue_body="body",
            repo_full_name="alice/repo",
            default_branch="main",
        )
        assert session_id == "real-sess-1"

        row = await observability.get_active_session_by_issue(8, "alice/repo")
        assert row is not None
        assert row["session_id"] == "real-sess-1"
        assert row["devin_status"] == "working"
        assert row["devin_session_url"] == "https://app.devin.ai/sessions/real-sess-1"

    async def test_poller_skips_placeholder_and_stopped_rows(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(observability, "DB_PATH", str(tmp_path / "s.db"))
        await observability.init_db()

        placeholder_id = await observability.record_issue_opened(
            issue_number=9,
            issue_title="t",
            issue_user="u",
            repo_full_name="r/r",
        )
        assert placeholder_id is not None

        from src.session_manager import SessionManager

        devin = AsyncMock()
        github = AsyncMock()
        sm = SessionManager(devin_client=devin, github_client=github)

        await sm.poll_and_update()
        devin.get_session.assert_not_called()

        # Once stopped, still should not be polled.
        await observability.mark_devin_stopped(placeholder_id, "boom")
        await sm.poll_and_update()
        devin.get_session.assert_not_called()


class TestDevinBotCommentFlipsToUserAction:
    def test_non_marker_devin_comment_flips_session_to_blocked(self, client):
        async def _never_finish(**kwargs):
            import asyncio
            await asyncio.sleep(3600)

        open_payload = {
            "action": "opened",
            "issue": {
                "number": 300,
                "title": "t",
                "body": "",
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "alice/repo", "default_branch": "main"},
        }
        with patch(
            "src.main.session_manager.handle_issue_opened",
            side_effect=_never_finish,
        ):
            _post(client, "issues", open_payload)

        # Simulate the promotion that the (mocked-out) background handler
        # would have performed.
        import asyncio
        async def _promote():
            row = await observability.get_active_session_by_issue(300, "alice/repo")
            await observability.promote_to_working(
                placeholder_id=row["session_id"],
                real_session_id="real-300",
                devin_session_url="https://app.devin.ai/sessions/real-300",
            )
        asyncio.get_event_loop().run_until_complete(_promote())

        comment_payload = {
            "action": "created",
            "comment": {
                "user": {"login": "devin-ai-integration[bot]"},
                "body": "Is this a duplicate of #42? I couldn't reproduce locally.",
            },
            "issue": {"number": 300},
            "repository": {"full_name": "alice/repo"},
        }
        resp = _post(client, "issue_comment", comment_payload)
        assert resp.status_code == 202
        assert resp.json()["reason"] == "bot_comment"

        # Give the spawned task a chance to run.
        import time
        for _ in range(20):
            sessions = client.get("/sessions").json()
            row = next(s for s in sessions if s["issue_number"] == 300)
            if row["devin_status"] == "blocked":
                break
            time.sleep(0.05)
        assert row["devin_status"] == "blocked"
        assert row["error_message"] and "commented on issue" in row["error_message"]

    def test_initial_working_on_this_marker_does_not_flip(self, client):
        async def _never_finish(**kwargs):
            import asyncio
            await asyncio.sleep(3600)

        open_payload = {
            "action": "opened",
            "issue": {
                "number": 301,
                "title": "t",
                "body": "",
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "alice/repo", "default_branch": "main"},
        }
        with patch(
            "src.main.session_manager.handle_issue_opened",
            side_effect=_never_finish,
        ):
            _post(client, "issues", open_payload)

        import asyncio
        async def _promote():
            row = await observability.get_active_session_by_issue(301, "alice/repo")
            await observability.promote_to_working(
                placeholder_id=row["session_id"],
                real_session_id="real-301",
                devin_session_url="https://app.devin.ai/sessions/real-301",
            )
        asyncio.get_event_loop().run_until_complete(_promote())

        marker_payload = {
            "action": "created",
            "comment": {
                "user": {"login": "devin-ai-integration[bot]"},
                "body": "**Working on this.** I'll comment here again when a PR is ready.",
            },
            "issue": {"number": 301},
            "repository": {"full_name": "alice/repo"},
        }
        _post(client, "issue_comment", marker_payload)

        import time
        time.sleep(0.2)
        sessions = client.get("/sessions").json()
        row = next(s for s in sessions if s["issue_number"] == 301)
        assert row["devin_status"] == "working"


class TestDevinPrOpenedFlipsToUserAction:
    def test_pr_opened_by_devin_bot_flips_active_session(self, client):
        async def _never_finish(**kwargs):
            import asyncio
            await asyncio.sleep(3600)

        open_payload = {
            "action": "opened",
            "issue": {
                "number": 400,
                "title": "t",
                "body": "",
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "alice/repo", "default_branch": "main"},
        }
        with patch(
            "src.main.session_manager.handle_issue_opened",
            side_effect=_never_finish,
        ):
            _post(client, "issues", open_payload)

        import asyncio
        async def _promote():
            row = await observability.get_active_session_by_issue(400, "alice/repo")
            await observability.promote_to_working(
                placeholder_id=row["session_id"],
                real_session_id="real-400",
                devin_session_url="https://app.devin.ai/sessions/real-400",
            )
        asyncio.get_event_loop().run_until_complete(_promote())

        pr_payload = {
            "action": "opened",
            "pull_request": {
                "number": 77,
                "html_url": "https://github.com/alice/repo/pull/77",
                "body": "Fixes #400\n\nDoes the thing.",
                "user": {"login": "devin-ai-integration[bot]"},
                "head": {"ref": "fix/issue-400-something"},
            },
            "repository": {"full_name": "alice/repo"},
        }
        resp = _post(client, "pull_request", pr_payload)
        assert resp.status_code == 202

        import time
        for _ in range(20):
            sessions = client.get("/sessions").json()
            row = next(s for s in sessions if s["issue_number"] == 400)
            if row["devin_status"] == "blocked":
                break
            time.sleep(0.05)
        assert row["devin_status"] == "blocked"
        assert row["pr_url"] == "https://github.com/alice/repo/pull/77"

    def test_pr_opened_by_non_devin_is_ignored(self, client):
        pr_payload = {
            "action": "opened",
            "pull_request": {
                "number": 78,
                "html_url": "https://github.com/alice/repo/pull/78",
                "body": "unrelated",
                "user": {"login": "alice"},
                "head": {"ref": "feature"},
            },
            "repository": {"full_name": "alice/repo"},
        }
        resp = _post(client, "pull_request", pr_payload)
        assert resp.status_code == 202
        assert resp.json()["reason"] == "pr_not_by_devin"

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src import observability


@pytest.fixture
def client(monkeypatch):
    # Prevent the lifespan from hitting the real GitHub API / starting the poller.
    from src import main as main_module

    monkeypatch.setattr(main_module.github_client, "get_authenticated_user", AsyncMock(return_value="bot"))
    monkeypatch.setattr(main_module, "_background_poller", AsyncMock())
    monkeypatch.setattr(main_module, "_print_ngrok_url", AsyncMock())
    monkeypatch.setattr(main_module.observability, "init_db", AsyncMock())

    with TestClient(main_module.app) as c:
        yield c


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerifySignature:
    def test_valid_signature_accepted(self):
        from src.main import _verify_signature

        body = b'{"hello": "world"}'
        sig = _sign(body, "s3cret")
        assert _verify_signature(body, sig, "s3cret") is True

    def test_invalid_signature_rejected(self):
        from src.main import _verify_signature

        body = b'{"hello": "world"}'
        assert _verify_signature(body, "sha256=deadbeef", "s3cret") is False

    def test_wrong_secret_rejected(self):
        from src.main import _verify_signature

        body = b'{"hello": "world"}'
        sig = _sign(body, "right")
        assert _verify_signature(body, sig, "wrong") is False


class TestWebhookRouting:
    def test_rejects_invalid_signature(self, client):
        resp = client.post(
            "/webhook/github",
            content=b"{}",
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=bad"},
        )
        assert resp.status_code == 401

    def test_issues_opened_schedules_handler_with_default_branch(self, client):
        payload = {
            "action": "opened",
            "issue": {
                "number": 7,
                "title": "Bug",
                "body": "Details",
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "alice/repo", "default_branch": "develop"},
        }
        body = json.dumps(payload).encode()

        with patch("src.main.session_manager.handle_issue_opened", new_callable=AsyncMock) as h:
            resp = client.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": _sign(body, "test-secret"),
                },
            )

        assert resp.status_code == 202
        assert resp.json()["issue_number"] == 7
        h.assert_called_once()
        kwargs = h.call_args.kwargs
        assert kwargs["issue_number"] == 7
        assert kwargs["repo_full_name"] == "alice/repo"
        assert kwargs["default_branch"] == "develop"

    def test_issues_opened_default_branch_falls_back_to_main(self, client):
        payload = {
            "action": "opened",
            "issue": {"number": 8, "title": "t", "body": "b", "user": {"login": "u"}},
            "repository": {"full_name": "alice/repo"},  # no default_branch
        }
        body = json.dumps(payload).encode()

        with patch("src.main.session_manager.handle_issue_opened", new_callable=AsyncMock) as h:
            resp = client.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": _sign(body, "test-secret"),
                },
            )

        assert resp.status_code == 202
        assert h.call_args.kwargs["default_branch"] == "main"

    def test_issue_ignored_when_trigger_label_filter_excludes_it(self, client, monkeypatch):
        from src import main as main_module

        monkeypatch.setattr(main_module.trigger, "TRIGGER_LABELS", {"devin"})
        payload = {
            "action": "opened",
            "issue": {
                "number": 99,
                "title": "no label match",
                "body": "",
                "user": {"login": "u"},
                "labels": [{"name": "bug"}],
            },
            "repository": {"full_name": "alice/repo", "default_branch": "main"},
        }
        body = json.dumps(payload).encode()

        with patch("src.main.session_manager.handle_issue_opened", new_callable=AsyncMock) as h:
            resp = client.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": _sign(body, "test-secret"),
                },
            )

        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"
        assert "no matching trigger label" in resp.json()["reason"]
        h.assert_not_called()

    def test_issue_accepted_when_label_filter_matches(self, client, monkeypatch):
        from src import main as main_module

        monkeypatch.setattr(main_module.trigger, "TRIGGER_LABELS", {"devin"})
        monkeypatch.setattr(main_module.trigger, "DRAFT_PR", True)
        monkeypatch.setattr(main_module.trigger, "PR_REVIEWERS", ["alice"])

        payload = {
            "action": "opened",
            "issue": {
                "number": 100,
                "title": "fixme",
                "body": "",
                "user": {"login": "u"},
                "labels": [{"name": "devin"}],
            },
            "repository": {"full_name": "alice/repo", "default_branch": "main"},
        }
        body = json.dumps(payload).encode()

        with patch("src.main.session_manager.handle_issue_opened", new_callable=AsyncMock) as h:
            resp = client.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": _sign(body, "test-secret"),
                },
            )

        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        h.assert_called_once()
        assert h.call_args.kwargs["draft_pr"] is True
        assert h.call_args.kwargs["pr_reviewers"] == ["alice"]

    def test_bot_comments_are_ignored(self, client):
        payload = {
            "action": "created",
            "comment": {"user": {"login": "devin-ai-integration[bot]"}, "body": "hi"},
            "issue": {"number": 1},
            "repository": {"full_name": "alice/repo"},
        }
        body = json.dumps(payload).encode()

        with patch("src.main.session_manager.handle_issue_comment", new_callable=AsyncMock) as h:
            resp = client.post(
                "/webhook/github",
                content=body,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-Hub-Signature-256": _sign(body, "test-secret"),
                },
            )

        assert resp.status_code == 202
        assert resp.json()["reason"] == "bot_comment"
        h.assert_not_called()

    def test_unknown_event_is_ignored(self, client):
        body = b"{}"
        resp = client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": _sign(body, "test-secret"),
            },
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"


class TestSafeRun:
    async def test_records_failed_webhook_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(observability, "DB_PATH", str(tmp_path / "s.db"))
        await observability.init_db()

        from src.main import _safe_run

        async def boom():
            raise RuntimeError("kaboom")

        await _safe_run("h", boom(), {"issue_number": 1})

        rows = await observability.get_failed_webhooks()
        assert len(rows) == 1
        assert rows[0]["handler"] == "h"
        assert rows[0]["context"] == {"issue_number": 1}
        assert "kaboom" in rows[0]["error"]

    async def test_no_record_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(observability, "DB_PATH", str(tmp_path / "s.db"))
        await observability.init_db()

        from src.main import _safe_run

        async def ok():
            return None

        await _safe_run("h", ok(), {})
        assert await observability.get_failed_webhooks() == []


class TestFailedWebhooksEndpoint:
    async def test_returns_failed_webhooks(self, tmp_path, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from src import main as main_module

        monkeypatch.setattr(observability, "DB_PATH", str(tmp_path / "s.db"))
        await observability.init_db()
        await observability.record_failed_webhook(
            "handle_issue_opened", {"issue_number": 5}, "boom"
        )

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/failed_webhooks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["handler"] == "handle_issue_opened"
        assert data[0]["context"] == {"issue_number": 5}

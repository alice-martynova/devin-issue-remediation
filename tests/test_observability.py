import pytest

from src import observability


@pytest.fixture
async def db(tmp_path, monkeypatch):
    path = tmp_path / "sessions.db"
    monkeypatch.setattr(observability, "DB_PATH", str(path))
    await observability.init_db()
    yield path


async def test_record_and_get_session(db):
    await observability.record_session(
        session_id="sess-1",
        issue_number=42,
        issue_title="Broken thing",
        issue_user="alice",
        repo_full_name="alice/repo",
        devin_session_url="https://app.devin.ai/sessions/sess-1",
    )

    assert await observability.session_exists_for_issue(42, "alice/repo") is True
    assert await observability.session_exists_for_issue(99, "alice/repo") is False

    active = await observability.get_active_session_by_issue(42, "alice/repo")
    assert active is not None
    assert active["session_id"] == "sess-1"
    assert active["devin_status"] == "working"
    assert active["pr_merged"] == 0


async def test_pending_session_two_step_flow(db):
    """record_pending_session + activate_pending_session produces a working session."""
    inserted = await observability.record_pending_session(
        placeholder_id="pending:issue-1-r/r",
        issue_number=1,
        issue_title="Fix me",
        issue_user="alice",
        repo_full_name="r/r",
    )
    assert inserted is True

    # Pending session is visible but not returned as active (no real session_id yet)
    assert await observability.session_exists_for_issue(1, "r/r") is True
    assert await observability.get_active_session_by_issue(1, "r/r") is None

    await observability.activate_pending_session(
        placeholder_id="pending:issue-1-r/r",
        session_id="sess-real",
        devin_session_url="https://app.devin.ai/sessions/sess-real",
    )

    active = await observability.get_active_session_by_issue(1, "r/r")
    assert active is not None
    assert active["session_id"] == "sess-real"
    assert active["devin_status"] == "working"


async def test_pending_session_blocks_duplicate(db):
    """A second pending insert for the same issue is silently ignored."""
    await observability.record_pending_session(
        placeholder_id="pending:issue-2-r/r",
        issue_number=2, issue_title="t", issue_user="u", repo_full_name="r/r",
    )
    inserted = await observability.record_pending_session(
        placeholder_id="pending:issue-2-r/r-dup",
        issue_number=2, issue_title="t", issue_user="u", repo_full_name="r/r",
    )
    assert inserted is False


async def test_set_session_error_stores_message(db):
    await observability.record_pending_session(
        placeholder_id="pending:issue-3-r/r",
        issue_number=3, issue_title="t", issue_user="u", repo_full_name="r/r",
    )
    await observability.set_session_error("pending:issue-3-r/r", "402 Payment Required")

    sessions = await observability.get_all_sessions()
    s = next(s for s in sessions if s["issue_number"] == 3)
    assert s["devin_status"] == "error"
    assert "402" in s["error_message"]
    # Error sessions must not appear in get_active_sessions (terminal)
    active = await observability.get_active_sessions()
    assert not any(s["issue_number"] == 3 for s in active)


async def test_update_devin_commented(db):
    await observability.record_session(
        session_id="sess-c", issue_number=10, issue_title="t",
        issue_user="u", repo_full_name="r/r",
        devin_session_url="https://example",
    )
    s = await observability.get_active_session_by_issue(10, "r/r")
    assert s["devin_commented"] == 0

    await observability.update_devin_commented("sess-c")

    s = await observability.get_active_session_by_issue(10, "r/r")
    assert s["devin_commented"] == 1


async def test_record_session_returns_true_on_insert_false_on_duplicate(db):
    kwargs = dict(
        session_id="sess-1",
        issue_number=42,
        issue_title="x",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    assert await observability.record_session(**kwargs) is True
    # Second call with same session_id is a no-op
    assert await observability.record_session(**kwargs) is False

    all_sessions = await observability.get_all_sessions()
    assert len([s for s in all_sessions if s["session_id"] == "sess-1"]) == 1


async def test_partial_unique_index_blocks_duplicate_active_session(db):
    """A second active session for the same issue (different session_id)
    must be rejected by the partial unique index."""
    await observability.record_session(
        session_id="sess-A",
        issue_number=1,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    # Racing webhook: different session_id, same issue — must NOT insert.
    inserted = await observability.record_session(
        session_id="sess-B",
        issue_number=1,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    assert inserted is False
    all_sessions = await observability.get_all_sessions()
    assert {s["session_id"] for s in all_sessions} == {"sess-A"}


async def test_new_session_allowed_after_previous_one_terminated(db):
    """Once a session reaches a terminal status, a new active session for
    the same issue should be creatable (e.g. issue reopened)."""
    await observability.record_session(
        session_id="sess-A",
        issue_number=1,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    await observability.update_session("sess-A", "finished")

    inserted = await observability.record_session(
        session_id="sess-B",
        issue_number=1,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    assert inserted is True


async def test_session_exists_ignores_terminal_sessions(db):
    """session_exists_for_issue must return False for terminal sessions so that
    reopened issues are not silently skipped by the early-exit check."""
    for terminal_status in ("finished", "expired", "error"):
        await observability.record_session(
            session_id=f"sess-{terminal_status}",
            issue_number=100,
            issue_title="t",
            issue_user="u",
            repo_full_name="r/r",
            devin_session_url="https://example",
        )
        await observability.update_session(f"sess-{terminal_status}", terminal_status)
        # After reaching a terminal state the issue should appear as "open
        # for a new session" from the early-exit check's perspective.
        assert await observability.session_exists_for_issue(100, "r/r") is False
        # Clean up for next iteration
        await observability.update_session(f"sess-{terminal_status}", "finished")


async def test_update_session_sets_status_and_pr(db):
    await observability.record_session(
        session_id="sess-2",
        issue_number=1,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    await observability.update_session(
        "sess-2", "blocked", pr_url="https://github.com/r/r/pull/9"
    )

    active = await observability.get_active_session_by_issue(1, "r/r")
    assert active["devin_status"] == "blocked"
    assert active["pr_url"] == "https://github.com/r/r/pull/9"


async def test_terminal_status_excludes_from_active(db):
    await observability.record_session(
        session_id="sess-3",
        issue_number=2,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    await observability.update_session("sess-3", "finished")

    assert await observability.get_active_session_by_issue(2, "r/r") is None


async def test_get_session_by_pr_number(db):
    await observability.record_session(
        session_id="sess-4",
        issue_number=3,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    await observability.update_session(
        "sess-4", "working", pr_url="https://github.com/r/r/pull/17"
    )

    found = await observability.get_session_by_pr_number(17, "r/r")
    assert found is not None
    assert found["session_id"] == "sess-4"

    missing = await observability.get_session_by_pr_number(999, "r/r")
    assert missing is None


async def test_failed_webhook_round_trip(db):
    await observability.record_failed_webhook(
        handler="handle_issue_opened",
        context={"issue_number": 42, "repo_full_name": "r/r"},
        error="httpx.HTTPError: boom",
    )
    rows = await observability.get_failed_webhooks()
    assert len(rows) == 1
    row = rows[0]
    assert row["handler"] == "handle_issue_opened"
    assert row["context"] == {"issue_number": 42, "repo_full_name": "r/r"}
    assert "boom" in row["error"]


async def test_failed_webhooks_returned_newest_first(db):
    for i in range(3):
        await observability.record_failed_webhook(
            handler=f"h{i}", context={"i": i}, error="x"
        )
    rows = await observability.get_failed_webhooks()
    assert [r["handler"] for r in rows] == ["h2", "h1", "h0"]

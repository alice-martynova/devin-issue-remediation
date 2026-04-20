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


async def test_devin_stopped_does_not_block_future_placeholder(db):
    """A `devin-stopped` row must not block a subsequent record_issue_opened
    for the same issue — otherwise a token-limit failure would permanently
    prevent retries on close+reopen.
    """
    ph1 = await observability.record_issue_opened(
        issue_number=42,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
    )
    assert ph1 is not None
    await observability.mark_devin_stopped(ph1, "token limit")

    ph2 = await observability.record_issue_opened(
        issue_number=42,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
    )
    assert ph2 is not None
    assert ph2 != ph1


async def test_issue_closed_row_does_not_block_future_placeholder(db):
    """Closing an issue (issue_closed=1) should release the partial unique
    index so a subsequent reopen can create a fresh placeholder even if the
    prior session was still `working` / `blocked` when it closed.
    """
    await observability.record_session(
        session_id="sess-old",
        issue_number=55,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    # Simulate a `blocked` session that the user then closed without waiting
    # for Devin to finish.
    await observability.update_session("sess-old", "blocked")
    await observability.update_issue_closed("sess-old")

    ph = await observability.record_issue_opened(
        issue_number=55,
        issue_title="t",
        issue_user="u",
        repo_full_name="r/r",
    )
    assert ph is not None


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

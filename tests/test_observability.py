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


async def test_record_session_is_idempotent(db):
    kwargs = dict(
        session_id="sess-1",
        issue_number=42,
        issue_title="x",
        issue_user="u",
        repo_full_name="r/r",
        devin_session_url="https://example",
    )
    await observability.record_session(**kwargs)
    # Second call with same session_id must not raise
    await observability.record_session(**kwargs)

    all_sessions = await observability.get_all_sessions()
    assert len([s for s in all_sessions if s["session_id"] == "sess-1"]) == 1


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

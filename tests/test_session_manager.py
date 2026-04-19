from src.session_manager import (
    _extract_pr_number,
    _extract_pr_url,
    _extract_status,
    _normalize_status,
    _slugify,
    build_devin_prompt,
)


class TestExtractStatus:
    def test_prefers_status_enum_over_status(self):
        assert _extract_status({"status_enum": "blocked", "status": "running"}) == "blocked"

    def test_falls_back_to_status_when_enum_missing(self):
        assert _extract_status({"status": "running"}) == "running"

    def test_falls_back_to_status_when_enum_is_null(self):
        assert _extract_status({"status_enum": None, "status": "running"}) == "running"

    def test_defaults_to_working_when_both_missing(self):
        assert _extract_status({}) == "working"


class TestNormalizeStatus:
    def test_running_maps_to_working(self):
        assert _normalize_status("running") == "working"

    def test_stopped_maps_to_finished(self):
        assert _normalize_status("stopped") == "finished"

    def test_suspended_maps_to_expired(self):
        assert _normalize_status("suspended") == "expired"

    def test_blocked_passes_through(self):
        assert _normalize_status("blocked") == "blocked"

    def test_working_passes_through(self):
        assert _normalize_status("working") == "working"

    def test_finished_passes_through(self):
        assert _normalize_status("finished") == "finished"

    def test_transient_enum_values_map_to_working(self):
        for transient in [
            "suspend_requested",
            "suspend_requested_frontend",
            "resume_requested",
            "resume_requested_frontend",
            "resumed",
        ]:
            assert _normalize_status(transient) == "working", transient


class TestExtractPrUrl:
    def test_pulls_html_url_from_pull_request_object(self):
        details = {"pull_request": {"html_url": "https://github.com/x/y/pull/1"}}
        assert _extract_pr_url(details) == "https://github.com/x/y/pull/1"

    def test_falls_back_to_pull_request_url(self):
        details = {"pull_request": {"url": "https://api.github.com/x/y/pulls/1"}}
        assert _extract_pr_url(details) == "https://api.github.com/x/y/pulls/1"

    def test_reads_from_structured_output_pr_url(self):
        details = {"structured_output": {"pr_url": "https://github.com/x/y/pull/5"}}
        assert _extract_pr_url(details) == "https://github.com/x/y/pull/5"

    def test_returns_none_when_no_pr_fields(self):
        assert _extract_pr_url({}) is None
        assert _extract_pr_url({"pull_request": None}) is None


class TestExtractPrNumber:
    def test_parses_number_from_standard_url(self):
        assert _extract_pr_number("https://github.com/x/y/pull/123") == 123

    def test_returns_none_on_malformed_url(self):
        assert _extract_pr_number("https://github.com/x/y") is None


class TestSlugify:
    def test_lowercases_and_replaces_non_alnum(self):
        assert _slugify("Fix: Weird Bug!") == "fix--weird-bug"

    def test_truncates_to_40_chars_and_strips_trailing_dashes(self):
        result = _slugify("A " * 30)
        assert len(result) <= 40
        assert not result.endswith("-")


class TestBuildDevinPrompt:
    def test_uses_supplied_default_branch(self):
        prompt = build_devin_prompt(1, "t", "b", "owner/repo", "develop")
        assert "Branch: develop" in prompt
        assert "Check out branch: develop" in prompt
        assert "pull request against develop" in prompt
        assert "master" not in prompt

    def test_embeds_issue_details(self):
        prompt = build_devin_prompt(42, "Title", "Body text", "owner/repo", "main")
        assert "#42" in prompt
        assert "Title" in prompt
        assert "Body text" in prompt
        assert "owner/repo" in prompt

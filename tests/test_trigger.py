from src.trigger import should_trigger


def _issue(title="fix a bug", labels=None):
    return {
        "title": title,
        "labels": [{"name": name} for name in (labels or [])],
    }


class TestShouldTriggerDefaults:
    def test_empty_config_accepts_everything(self):
        accept, reason = should_trigger(_issue(), trigger_labels=set(), trigger_title_prefix="")
        assert accept is True
        assert reason == "accepted"

    def test_empty_config_accepts_even_without_labels(self):
        accept, _ = should_trigger(
            _issue(title="whatever", labels=[]),
            trigger_labels=set(),
            trigger_title_prefix="",
        )
        assert accept is True


class TestLabelFilter:
    def test_accepts_issue_with_matching_label(self):
        accept, _ = should_trigger(
            _issue(labels=["devin"]),
            trigger_labels={"devin"},
            trigger_title_prefix="",
        )
        assert accept is True

    def test_rejects_issue_without_any_matching_label(self):
        accept, reason = should_trigger(
            _issue(labels=["bug"]),
            trigger_labels={"devin"},
            trigger_title_prefix="",
        )
        assert accept is False
        assert "no matching trigger label" in reason

    def test_rejects_issue_with_no_labels_when_filter_set(self):
        accept, _ = should_trigger(
            _issue(labels=[]),
            trigger_labels={"devin"},
            trigger_title_prefix="",
        )
        assert accept is False

    def test_label_match_is_case_insensitive(self):
        accept, _ = should_trigger(
            _issue(labels=["Devin"]),
            trigger_labels={"devin"},
            trigger_title_prefix="",
        )
        assert accept is True

    def test_any_matching_label_triggers(self):
        accept, _ = should_trigger(
            _issue(labels=["bug", "auto-fix"]),
            trigger_labels={"devin", "auto-fix"},
            trigger_title_prefix="",
        )
        assert accept is True


class TestTitlePrefixFilter:
    def test_accepts_matching_prefix(self):
        accept, _ = should_trigger(
            _issue(title="[devin] broken thing"),
            trigger_labels=set(),
            trigger_title_prefix="[devin]",
        )
        assert accept is True

    def test_rejects_non_matching_prefix(self):
        accept, reason = should_trigger(
            _issue(title="broken thing"),
            trigger_labels=set(),
            trigger_title_prefix="[devin]",
        )
        assert accept is False
        assert "title does not start" in reason

    def test_prefix_check_is_case_insensitive(self):
        accept, _ = should_trigger(
            _issue(title="[DEVIN] yo"),
            trigger_labels=set(),
            trigger_title_prefix="[devin]",
        )
        assert accept is True


class TestCombinedFilters:
    def test_must_satisfy_both_filters(self):
        # title matches but label doesn't
        accept, _ = should_trigger(
            _issue(title="[devin] x", labels=["bug"]),
            trigger_labels={"devin"},
            trigger_title_prefix="[devin]",
        )
        assert accept is False

        # label matches but title doesn't
        accept, _ = should_trigger(
            _issue(title="x", labels=["devin"]),
            trigger_labels={"devin"},
            trigger_title_prefix="[devin]",
        )
        assert accept is False

        # both match
        accept, _ = should_trigger(
            _issue(title="[devin] x", labels=["devin"]),
            trigger_labels={"devin"},
            trigger_title_prefix="[devin]",
        )
        assert accept is True

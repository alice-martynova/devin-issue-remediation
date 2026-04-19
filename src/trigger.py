"""Product-configurable filters controlling which GitHub issues Devin responds to.

Both filters are opt-in:

- TRIGGER_LABELS: comma-separated list of labels. If set, an issue must have
  at least one matching label to fire. Empty means accept every issue.
- TRIGGER_TITLE_PREFIX: case-insensitive prefix the issue title must start
  with. Empty means no prefix check.

Leaving both unset preserves the original "every issue fires" behavior.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


TRIGGER_LABELS: set[str] = {
    label.lower() for label in _parse_csv(os.getenv("TRIGGER_LABELS", ""))
}
TRIGGER_TITLE_PREFIX: str = os.getenv("TRIGGER_TITLE_PREFIX", "").strip()
DRAFT_PR: bool = _parse_bool(os.getenv("DRAFT_PR", ""))
PR_REVIEWERS: list[str] = _parse_csv(os.getenv("PR_REVIEWERS", ""))


def should_trigger(
    issue_payload: dict,
    *,
    trigger_labels: set[str] | None = None,
    trigger_title_prefix: str | None = None,
) -> tuple[bool, str]:
    """Return (accept, reason) for a GitHub issues webhook payload.

    Filters are evaluated against the canonical module-level config by default;
    the kwargs exist so tests can inject overrides without touching os.environ.
    """
    labels_cfg = TRIGGER_LABELS if trigger_labels is None else trigger_labels
    prefix_cfg = TRIGGER_TITLE_PREFIX if trigger_title_prefix is None else trigger_title_prefix

    title = (issue_payload.get("title") or "").strip()
    if prefix_cfg and not title.lower().startswith(prefix_cfg.lower()):
        return False, f"title does not start with {prefix_cfg!r}"

    if labels_cfg:
        issue_labels = {
            (lbl.get("name") or "").lower()
            for lbl in (issue_payload.get("labels") or [])
            if isinstance(lbl, dict)
        }
        if not (labels_cfg & issue_labels):
            return False, f"no matching trigger label (need one of {sorted(labels_cfg)})"

    return True, "accepted"

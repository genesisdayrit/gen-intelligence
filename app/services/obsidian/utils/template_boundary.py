"""Detection for the Daily Action template boundary.

Webhook-driven inserts walk a Daily Action note looking for the user's
template section so they don't write into it.

Legacy notes begin the template with `Vision Objective 1` (optionally
with a parenthetical, e.g. `Vision Objective 1 (...):`). Current notes
are body-only leverage questions ending in a standalone `---` divider,
with no Vision Objective and no Daily Review.

All inserters call `is_template_boundary` / `template_end_insert_index`
rather than comparing against a constant directly, so any future
template change only needs a fix here.
"""

import re

# Canonical literal form of the legacy boundary — used by tests and
# fixtures that need a concrete string. Production code should call
# `is_template_boundary` rather than comparing against this constant.
TEMPLATE_BOUNDARY = "Vision Objective 1:"

_TEMPLATE_BOUNDARY_RE = re.compile(r"^Vision Objective\s+\d")


def is_template_boundary(line: str) -> bool:
    """Return True if a line marks the start of the user's template section.

    Matches `Vision Objective 1:`, `Vision Objective 1 (...):`, and any
    later-numbered `Vision Objective N` label — with or without a
    parenthetical context. Current Daily Action templates have no such
    line; callers then fall back to `template_end_insert_index`.
    """
    return _TEMPLATE_BOUNDARY_RE.match(line.strip()) is not None


def template_end_insert_index(lines: list[str]) -> int:
    """Line index after the template's trailing `---`, or EOF if none.

    New Daily Action notes end with the leverage questions and a
    standalone `---` divider. Webhook sections belong after that
    divider so they do not sit above the prompts.
    """
    last_divider = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            last_divider = i
    if last_divider is None:
        return len(lines)
    return last_divider + 1


def fallback_webhook_insert_index(
    lines: list[str],
    daily_review_end_line: int,
    template_boundary_line: int | None = None,
) -> int:
    """Insert index when no later sibling section exists.

    Prefer a Vision Objective boundary (legacy notes), then the line
    after Daily Review (legacy notes), then after the template `---` /
    EOF (current notes with neither marker).
    """
    if template_boundary_line is not None:
        return template_boundary_line
    if daily_review_end_line:
        return daily_review_end_line
    return template_end_insert_index(lines)

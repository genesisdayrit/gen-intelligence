"""Detection for the Daily Action template boundary.

Webhook-driven inserts walk a Daily Action note looking for the user's
template section so they don't write into it.

Legacy notes begin the template with `Vision Objective 1` (optionally
with a parenthetical, e.g. `Vision Objective 1 (...):`). Current notes
are body-only leverage questions ending in a standalone `---` divider,
with no Vision Objective and no Daily Review.

New notes seed HTML comment markers so later writers can place content
without scanning the whole file:

- `<!-- mindfold-slot:daily-review -->` — mindfold inserts that date's
  review here, above everything.
- `<!-- hub-slot:updates -->` — Linear Initiative/Project Updates,
  Todoist, and Issues Touched land immediately above this marker
  (near the top, above the Main Thread snapshot). Manus is not seeded.
- `<!-- mindfold-slot:daily-action-plan -->` — mindfold inserts today's
  objectives above the leverage questions.
- `<!-- appended from mindfold ...` — anything after this comment is
  ignored by placement logic.

All inserters call `is_template_boundary` / `template_end_insert_index`
/ `fallback_webhook_insert_index` rather than comparing against a
constant directly, so any future template change only needs a fix here.

Current notes may also have `##### <initiative>` proposed-work headers
immediately after the template `---`. Those stay at the bottom;
webhook sections do not.
"""

import re

# Canonical literal form of the legacy boundary — used by tests and
# fixtures that need a concrete string. Production code should call
# `is_template_boundary` rather than comparing against this constant.
TEMPLATE_BOUNDARY = "Vision Objective 1:"

MINDFOLD_DAILY_REVIEW_SLOT = "<!-- mindfold-slot:daily-review -->"
MINDFOLD_DAILY_PLAN_SLOT = "<!-- mindfold-slot:daily-action-plan -->"
HUB_UPDATES_SLOT = "<!-- hub-slot:updates -->"
MINDFOLD_APPENDED_MARKER_PREFIX = "<!-- appended from mindfold"

_TEMPLATE_BOUNDARY_RE = re.compile(r"^Vision Objective\s+\d")
# Exactly five hashes: `##### Hub OS`, not `### Initiative Updates:` or `######`.
_INITIATIVE_WORK_HEADER_RE = re.compile(r"^#{5}\s+\S")
_MAIN_THREAD_HEADER_RE = re.compile(r"^####\s+Main Thread\b")


def is_template_boundary(line: str) -> bool:
    """Return True if a line marks the start of the user's template section.

    Matches `Vision Objective 1:`, `Vision Objective 1 (...):`, and any
    later-numbered `Vision Objective N` label — with or without a
    parenthetical context. Current Daily Action templates have no such
    line; callers then fall back to `template_end_insert_index`.
    """
    return _TEMPLATE_BOUNDARY_RE.match(line.strip()) is not None


def is_initiative_work_header(line: str) -> bool:
    """True for Daily Action `##### <initiative name>` proposed-work headers."""
    stripped = line.strip()
    if stripped.startswith("######"):
        return False
    return _INITIATIVE_WORK_HEADER_RE.match(stripped) is not None


def is_mindfold_appended_marker(line: str) -> bool:
    """True for the start of a mindfold-appended dump (ignored by placement)."""
    return line.strip().startswith(MINDFOLD_APPENDED_MARKER_PREFIX)


def is_hub_updates_slot(line: str) -> bool:
    """True for the seeded webhook-section anchor above Main Thread."""
    return line.strip() == HUB_UPDATES_SLOT


def is_daily_plan_slot(line: str) -> bool:
    """True for the mindfold daily-action-plan marker above the leverage questions."""
    return line.strip() == MINDFOLD_DAILY_PLAN_SLOT


def is_main_thread_header(line: str) -> bool:
    """True for `#### Main Thread (<when PT>)` snapshot headers."""
    return _MAIN_THREAD_HEADER_RE.match(line.strip()) is not None


def is_webhook_scan_stop(line: str) -> bool:
    """True if a webhook section walk should stop at this line.

    Covers the hub-slot anchor, Main Thread snapshot, Vision Objective
    (legacy), and the mindfold-appended dump marker.
    """
    return (
        is_hub_updates_slot(line)
        or is_mindfold_appended_marker(line)
        or is_main_thread_header(line)
        or is_daily_plan_slot(line)
        or is_template_boundary(line)
    )


def placement_limit(lines: list[str]) -> int:
    """Index at which placement scans stop (start of mindfold-appended dump)."""
    for i, line in enumerate(lines):
        if is_mindfold_appended_marker(line):
            return i
    return len(lines)


def hub_updates_insert_index(lines: list[str]) -> int | None:
    """Line index of `<!-- hub-slot:updates -->`, or None on legacy notes."""
    limit = placement_limit(lines)
    for i in range(limit):
        if is_hub_updates_slot(lines[i]):
            return i
    return None


def url_in_main_thread_snapshot(lines: list[str], url: str) -> bool:
    """True if `url` already appears inside the `#### Main Thread` snapshot.

    Used to skip webhook Initiative Updates that would duplicate the
    snapshot seeded at Daily Action creation.
    """
    if not url:
        return False
    in_block = False
    for line in lines[: placement_limit(lines)]:
        if is_main_thread_header(line):
            in_block = True
            if url in line:
                return True
            continue
        if in_block:
            if (
                is_daily_plan_slot(line)
                or is_hub_updates_slot(line)
                or is_template_boundary(line)
                or is_initiative_work_header(line)
                or is_mindfold_appended_marker(line)
                or (line.strip().startswith("#") and not is_main_thread_header(line))
                or line.strip() == "---"
            ):
                return False
            if url in line:
                return True
    return False


def template_end_insert_index(lines: list[str]) -> int:
    """Line index after the template `---` and any ##### headers.

    New Daily Action notes end the leverage-question template with a
    standalone `---` divider, then one `##### <initiative>` block per
    Active Linear initiative. The plan slot now sits *above* the
    leverage questions, so it is not part of this region.

    Used to append initiative-work headers (and as the legacy webhook
    fallback when no hub-slot exists). Content after a mindfold-appended
    marker is ignored so a later `---` in that dump cannot steal the
    divider. Slot markers and `#### Main Thread` are not treated as
    initiative headers.
    """
    scan = lines[: placement_limit(lines)]
    last_divider = None
    for i, line in enumerate(scan):
        if line.strip() == "---":
            last_divider = i
    if last_divider is None:
        return len(scan)

    insert_at = last_divider + 1
    i = insert_at
    while i < len(scan) and not scan[i].strip():
        i += 1
    if i >= len(scan) or not is_initiative_work_header(scan[i]):
        return insert_at

    while i < len(scan):
        stripped = scan[i].strip()
        if not stripped:
            i += 1
            continue
        if is_initiative_work_header(scan[i]):
            i += 1
            continue
        if (
            stripped.startswith("#")
            or is_template_boundary(scan[i])
            or is_hub_updates_slot(scan[i])
            or is_main_thread_header(scan[i])
            or is_daily_plan_slot(scan[i])
            or stripped.startswith("Daily Review")
        ):
            break
        i += 1
    return i


def fallback_webhook_insert_index(
    lines: list[str],
    daily_review_end_line: int,
    template_boundary_line: int | None = None,
) -> int:
    """Insert index when no later sibling section exists.

    Prefer the seeded `<!-- hub-slot:updates -->` (new notes: near the
    top, above Main Thread). Otherwise Vision Objective (legacy), then
    the line after Daily Review (legacy), then after the template `---`
    and any `#####` initiative-work headers / EOF (PR 216/217 notes).
    """
    hub = hub_updates_insert_index(lines)
    if hub is not None:
        return hub
    if template_boundary_line is not None:
        return template_boundary_line
    if daily_review_end_line:
        return daily_review_end_line
    return template_end_insert_index(lines)

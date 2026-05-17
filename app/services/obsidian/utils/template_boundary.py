"""Detection for the Daily Action template boundary.

Webhook-driven inserts walk a Daily Action note looking for the user's
template section so they don't write into it. The template begins with
`Vision Objective 1` and continues with `Vision Objective 2`, etc. Each
label may or may not include a parenthetical context (e.g.
`Vision Objective 1 (...):`), so a literal equality check misses the
parenthetical form and lets entry-end loops run past the template start,
wiping user content.

All inserters call `is_template_boundary` rather than comparing against a
constant directly, so any future template change only needs a fix here.
"""

import re

_TEMPLATE_BOUNDARY_RE = re.compile(r"^Vision Objective\s+\d")


def is_template_boundary(line: str) -> bool:
    """Return True if a line marks the start of the user's template section.

    Matches `Vision Objective 1:`, `Vision Objective 1 (...):`, and any
    later-numbered `Vision Objective N` label — with or without a
    parenthetical context.
    """
    return _TEMPLATE_BOUNDARY_RE.match(line.strip()) is not None

#!/usr/bin/env python3
"""Hourly reconcile of missed/deferred Obsidian hub writes.

Usage:
    python -m scripts.obsidian.workflows.reconcile_missed_writes
    python -m scripts.obsidian.workflows.reconcile_missed_writes --since 2026-09-19T00:00:00Z
"""

import argparse
import logging
import sys

from services.obsidian.reconcile.runner import reconcile_missed_obsidian_writes


def reconcile_missed_writes(since: str | None = None) -> dict:
    """Scheduler / CLI entry. See ``reconcile_missed_obsidian_writes``."""
    return reconcile_missed_obsidian_writes(since=since)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile missed/deferred Obsidian hub writes since last check"
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "UTC ISO last_reconcile_check_at override "
            "(default: Redis watermark, or now-1h on first run only)"
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = reconcile_missed_writes(since=args.since)
    errors = result.get("errors") or []
    sys.exit(1 if errors and not result.get("watermark_advanced") else 0)


if __name__ == "__main__":
    main()

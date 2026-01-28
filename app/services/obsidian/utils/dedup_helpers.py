"""Helpers for deduplicating completed tasks in Obsidian notes."""

import re

LOG_ENTRY_PATTERN = re.compile(r"^\[\d{2}:\d{2}\s*(?:AM|PM)?\]\s*(.+)$")


def extract_task_contents_from_section(content: str, section_header: str) -> set[str]:
    """Extract task content strings from a section of an Obsidian note.

    Parses log entries like '[10:30 AM] Buy groceries' and returns {'Buy groceries'}.
    Only looks within the specified section (stops at next header or non-log content).

    Args:
        content: The full note content (or a slice of it)
        section_header: The section header to look for (e.g. '### Completed Tasks on Todoist:')

    Returns:
        Set of task content strings found in the section
    """
    lines = content.split("\n")
    in_section = False
    tasks: set[str] = set()

    for line in lines:
        if line.strip() == section_header:
            in_section = True
            continue
        if in_section:
            match = LOG_ENTRY_PATTERN.match(line)
            if match:
                tasks.add(match.group(1).strip())
            elif line.strip() == "":
                continue
            else:
                break

    return tasks


def is_task_duplicate(task_content: str, existing_tasks: set[str]) -> bool:
    """Check if a task content string already exists in the set."""
    return task_content.strip() in existing_tasks

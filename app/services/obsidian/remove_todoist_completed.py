"""Dropbox helper for removing uncompleted Todoist tasks from Daily Action notes."""

import os
import re
from datetime import datetime

import dropbox
import pytz
from dotenv import load_dotenv

from services.obsidian.utils.dropbox_rev_safe import update_with_retry
from services.dropbox.client import get_dropbox_client as _get_dropbox_client

load_dotenv()


# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")

TODOIST_COMPLETED_HEADER = "### Completed Tasks on Todoist:"



def _find_daily_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Daily' in the vault."""
    result = dbx.files_list_folder(vault_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find '_Daily' folder in Dropbox")


def _find_daily_action_folder(dbx: dropbox.Dropbox, daily_folder_path: str) -> str:
    """Find folder ending with '_Daily-Action' in the daily folder."""
    result = dbx.files_list_folder(daily_folder_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily-Action"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find '_Daily-Action' folder in Dropbox")


def _get_today_daily_action_path(daily_action_folder_path: str) -> str:
    """Get file path for today's Daily Action."""
    system_tz = pytz.timezone(timezone_str)
    now = datetime.now(system_tz)
    formatted_date = now.strftime('%Y-%m-%d')
    return f"{daily_action_folder_path}/DA {formatted_date}.md"


def _get_daily_action_content(dbx: dropbox.Dropbox, file_path: str) -> str:
    """Fetch Daily Action content from Dropbox."""
    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Daily Action not found: {file_path}")
        raise


def remove_todoist_completed(task_content: str) -> bool:
    """Remove an uncompleted task from today's Todoist section in Daily Action.

    Searches for any line containing the task content (ignoring timestamp) and removes it.
    Returns True if a task was removed, False if task was not found.
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    daily_action_folder = _find_daily_action_folder(dbx, daily_folder)
    file_path = _get_today_daily_action_path(daily_action_folder)

    def apply_remove(content: str):
        if TODOIST_COMPLETED_HEADER not in content:
            return None, False

        lines = content.split('\n')
        updated_lines = []
        task_removed = False
        in_todoist_section = False

        for line in lines:
            if line.strip() == TODOIST_COMPLETED_HEADER:
                in_todoist_section = True
                updated_lines.append(line)
                continue

            if in_todoist_section:
                if re.match(r'^\[\d{2}:\d{2}', line) and task_content in line:
                    task_removed = True
                    continue
                if line.strip() and not re.match(r'^\[\d{2}:\d{2}', line) and line.strip() != '':
                    in_todoist_section = False

            updated_lines.append(line)

        if not task_removed:
            return None, False

        final_lines = []
        i = 0
        while i < len(updated_lines):
            line = updated_lines[i]
            if line.strip() == TODOIST_COMPLETED_HEADER:
                section_has_entries = False
                for j in range(i + 1, len(updated_lines)):
                    next_line = updated_lines[j]
                    if re.match(r'^\[\d{2}:\d{2}', next_line):
                        section_has_entries = True
                        break
                    if next_line.strip() and not next_line.strip() == '':
                        break

                if not section_has_entries:
                    i += 1
                    while i < len(updated_lines) and updated_lines[i].strip() == '':
                        i += 1
                    continue

            final_lines.append(line)
            i += 1

        return '\n'.join(final_lines), True

    status, removed, _updated = update_with_retry(
        dbx,
        file_path,
        apply_remove,
        defer={
            "source": "todoist",
            "kind": "uncompleted",
            "payload_ref": task_content,
            "target": file_path,
            "payload": {"task_content": task_content},
        },
    )
    if status in {"missing", "error", "skipped"}:
        return False
    return status in {"updated", "deferred"} and bool(removed)

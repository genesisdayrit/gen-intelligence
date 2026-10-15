#!/usr/bin/env python3
"""Create daily action file in Obsidian vault via Dropbox.

Creates tomorrow's (or today's) daily action file from the vault template
`*_Templates/daily-templates/daily_action_properties.md` (sibling of the
journal template `daily_note_properties.md`). The `_Templates` folder is
resolved the same way as `_find_daily_action_folder` — via
`_find_folder_in_path`, so a numbered prefix like `15_Templates` matches.

Frontmatter is the script-generated `_Journal` / `_Weekly-Cycle` /
`_Long-Cycle` / `_Weekly-Map` block. The template is body-only and is
appended verbatim. If a template ever includes YAML frontmatter, that
block is stripped and ignored (logged) so it cannot duplicate or override
the generated keys.

After the template's trailing `---`, one `##### <initiative name>`
header is added for each Active (non-archived) Linear initiative,
excluding those labelled `main-thread` (override with
`DAILY_ACTION_EXCLUDE_INITIATIVE_LABEL`). Each header is followed by
`- ` and a blank line so proposed work can be filled in. Linear
failures/timeouts are logged and the note is still created without
this section. Webhook jobs (Linear updates, Todoist, Manus, issues
touched) append after these headers.

If the template is missing or cannot be downloaded, logs a warning and
falls back to the hardcoded body so the daily note is still created.
Skips creation if the file already exists.

Usage:
    python -m scripts.obsidian.workflows.file_creation.create_daily_action
    python -m scripts.obsidian.workflows.file_creation.create_daily_action --today
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta

import dropbox
import pytz
import redis
import requests
import yaml
from dotenv import load_dotenv

from scripts.linear.sync_utils import LINEAR_API_KEY, fetch_all_pages
from services.obsidian.utils.template_boundary import (
    is_initiative_work_header,
    template_end_insert_index,
)

load_dotenv()

logger = logging.getLogger(__name__)

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles")

# Vault path pieces — resolved through `_find_folder_in_path` /
# `_lookup_file_in_folder`, never concatenated onto DROPBOX_OBSIDIAN_VAULT_PATH.
DAILY_ACTION_TEMPLATES_SUFFIX = "_Templates"
DAILY_ACTION_TEMPLATE_SUBFOLDER = "daily-templates"
DAILY_ACTION_TEMPLATE_STEM = "daily_action_properties"

# Linear's `initiative.status` for an in-flight initiative. Matches
# `fetch_active_initiatives` in scripts.linear.test_find_main_thread_initiative.
ACTIVE_STATUS = "Active"
DEFAULT_EXCLUDE_INITIATIVE_LABEL = "main-thread"

# Same GraphQL shape as test_find_main_thread_initiative / send_main_thread_rollup:
# Active, non-archived, with labels so `main-thread` can be excluded.
ACTIVE_INITIATIVES_QUERY = """
query ActiveInitiatives($first: Int!, $after: String) {
  initiatives(first: $first, after: $after, includeArchived: false, orderBy: updatedAt) {
    nodes {
      id
      name
      slugId
      url
      status
      labels { nodes { id name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Used when the vault template cannot be found or downloaded.
# Matches the live `daily_action_properties.md` body: two leverage
# questions, a blank line, then a trailing `---` divider.
FALLBACK_MAIN_CONTENT = (
    "What is the highest leverage thing that you can do today to move the ball forward on what you need to?\n"
    "If you only had 2 hours to work today, what would you need to get done to move forward towards your goals or master vision?\n"
    "\n"
    "---\n"
)


# ===== Dropbox Client =====

def _refresh_access_token() -> str:
    """Refresh the Dropbox access token using the refresh token."""
    client_id = os.getenv('DROPBOX_ACCESS_KEY')
    client_secret = os.getenv('DROPBOX_ACCESS_SECRET')
    refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN')

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError("Missing Dropbox credentials in .env file")

    response = requests.post(
        'https://api.dropbox.com/oauth2/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': client_id,
            'client_secret': client_secret
        }
    )

    if response.status_code == 200:
        data = response.json()
        access_token = data.get('access_token')
        expires_in = data.get('expires_in')
        redis_client.set('DROPBOX_ACCESS_TOKEN', access_token, ex=expires_in)
        return access_token
    else:
        raise EnvironmentError(f"Failed to refresh token: {response.status_code}")


def _get_dropbox_client() -> dropbox.Dropbox:
    """Get authenticated Dropbox client."""
    access_token = redis_client.get('DROPBOX_ACCESS_TOKEN')
    if not access_token:
        access_token = _refresh_access_token()
    return dropbox.Dropbox(access_token)


# ===== Date Helper Functions =====

def _get_target_day(use_today: bool = False) -> datetime:
    """Return target day's datetime object in the configured timezone."""
    days_offset = 0 if use_today else 1
    return datetime.now(pytz.timezone(timezone_str)) + timedelta(days=days_offset)


def _get_journal_filename(use_today: bool = False) -> str:
    """Format target day's date to match journal filename format (e.g. 'Mar 6, 2026')."""
    target_day = _get_target_day(use_today)
    try:
        return target_day.strftime('%b %-d, %Y')
    except ValueError:
        return target_day.strftime('%b %#d, %Y')


def _get_cycle_date_range(use_today: bool = False) -> str:
    """Get weekly cycle date range for the target day's date."""
    target_day = _get_target_day(use_today)
    days_since_wednesday = (target_day.weekday() - 2) % 7
    cycle_start = target_day - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)
    return f"{cycle_start.strftime('%b. %d')} - {cycle_end.strftime('%b. %d, %Y')}"


def _get_week_ending_sunday(use_today: bool = False) -> str:
    """Get the date of the Sunday ending the week that contains the target day."""
    target_day = _get_target_day(use_today)
    days_until_sunday = (6 - target_day.weekday()) % 7
    week_ending = target_day + timedelta(days=days_until_sunday)
    return week_ending.strftime('%Y-%m-%d')


def _get_week_ending_filenames(use_today: bool = False) -> dict[str, str]:
    week_ending = _get_week_ending_sunday(use_today)
    return {
        "week_ending": f"Week-Ending-{week_ending}",
        "weekly_map": f"Weekly Map {week_ending}"
    }


# ===== Dropbox File/Folder Helpers =====

def _list_all_entries(dbx: dropbox.Dropbox, base_path: str) -> list:
    """List all entries in a Dropbox folder, handling pagination."""
    entries = []
    try:
        response = dbx.files_list_folder(base_path)
        entries.extend(response.entries)
        while response.has_more:
            response = dbx.files_list_folder_continue(response.cursor)
            entries.extend(response.entries)
    except dropbox.exceptions.ApiError as e:
        logger.error(f"Error fetching folder list from {base_path}: {e}")
    return entries


def _find_folder_in_path(dbx: dropbox.Dropbox, base_path: str, search_term: str) -> str | None:
    """Search for a folder whose name contains search_term (case-insensitive)."""
    entries = _list_all_entries(dbx, base_path)
    for entry in entries:
        if isinstance(entry, dropbox.files.FolderMetadata):
            if search_term.lower() in entry.name.lower():
                return entry.path_lower
    logger.warning(f"No folder containing '{search_term}' found in '{base_path}'.")
    return None


def _lookup_file_in_folder(dbx: dropbox.Dropbox, folder_path: str, file_template: str) -> tuple[str | None, str | None]:
    """Search for a file whose name contains file_template (case-insensitive)."""
    entries = _list_all_entries(dbx, folder_path)
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            if file_template.lower() in entry.name.lower():
                return entry.path_lower, entry.name
    logger.warning(f"No file containing '{file_template}' found in '{folder_path}'.")
    return None, None


def _parse_date_range_from_filename(filename: str, target_date) -> bool:
    """Check if target_date falls within a date range in the filename."""
    pattern = r'\((\d{4}\.\d{2}\.\d{2}) - (\d{4}\.\d{2}\.\d{2})\)'
    match = re.search(pattern, filename)
    if not match:
        return False

    start_str, end_str = match.groups()
    try:
        start_date = datetime.strptime(start_str, '%Y.%m.%d').date()
        end_date = datetime.strptime(end_str, '%Y.%m.%d').date()
        return start_date <= target_date <= end_date
    except ValueError as e:
        logger.warning(f"Could not parse dates from filename '{filename}': {e}")
        return False


# ===== Relationship Discovery =====

def _find_weekly_cycle_link(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> str:
    """Find weekly cycle file link for the target day's date."""
    try:
        cycles_folder = _find_folder_in_path(dbx, vault_path, "_Cycles")
        if not cycles_folder:
            return ""

        weekly_cycles_folder = _find_folder_in_path(dbx, cycles_folder, "_Weekly-Cycles")
        if not weekly_cycles_folder:
            return ""

        cycle_date_range = _get_cycle_date_range(use_today)
        file_path, file_name = _lookup_file_in_folder(dbx, weekly_cycles_folder, cycle_date_range)

        if file_path and file_name:
            base_name = file_name
            if base_name.lower().endswith('.md'):
                base_name = base_name[:-3]
            return f"[[{base_name}]]"

        logger.warning(f"No weekly cycle file found for date range: {cycle_date_range}")
        return ""

    except Exception as e:
        logger.error(f"Error finding weekly cycle link: {e}")
        return ""


def _find_long_cycle_link(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> str:
    """Find long cycle file link for the target day's date."""
    try:
        target_day = _get_target_day(use_today).date()

        cycles_folder = _find_folder_in_path(dbx, vault_path, "_Cycles")
        if not cycles_folder:
            return ""

        six_week_folder = _find_folder_in_path(dbx, cycles_folder, "_6-Week-Cycles")
        if not six_week_folder:
            return ""

        entries = _list_all_entries(dbx, six_week_folder)
        for entry in entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                if _parse_date_range_from_filename(entry.name, target_day):
                    filename = entry.name
                    if filename.lower().endswith('.md'):
                        filename = filename[:-3]
                    logger.info(f"Found matching long cycle file: {filename}")
                    return f"[[{filename}]]"

        logger.warning(f"No long cycle file found containing target day's date: {target_day}")
        return ""

    except Exception as e:
        logger.error(f"Error finding long cycle link: {e}")
        return ""


def _find_weekly_map_link(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> str:
    """Find weekly map file link for the target day's date."""
    try:
        weekly_folder = _find_folder_in_path(dbx, vault_path, "_Weekly")
        if not weekly_folder:
            return ""

        weekly_maps_folder = _find_folder_in_path(dbx, weekly_folder, "_Weekly-Maps")
        if not weekly_maps_folder:
            return ""

        filenames = _get_week_ending_filenames(use_today)
        weekly_map_filename = filenames["weekly_map"]
        file_path, file_name = _lookup_file_in_folder(dbx, weekly_maps_folder, weekly_map_filename)

        if file_path and file_name:
            base_name = file_name
            if base_name.lower().endswith('.md'):
                base_name = base_name[:-3]
            return f"[[{base_name}]]"

        logger.warning(f"No weekly map file found for: {weekly_map_filename}")
        return ""

    except Exception as e:
        logger.error(f"Error finding weekly map link: {e}")
        return ""


# ===== File Creation =====

def _generate_yaml_properties(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> dict:
    """Generate the YAML properties with relationship links."""
    journal_filename = _get_journal_filename(use_today)
    journal_link = f"[[{journal_filename}]]"

    weekly_cycle_link = _find_weekly_cycle_link(dbx, vault_path, use_today)
    weekly_cycle_list = [weekly_cycle_link] if weekly_cycle_link else []

    long_cycle_link = _find_long_cycle_link(dbx, vault_path, use_today)
    long_cycle_list = [long_cycle_link] if long_cycle_link else []

    weekly_map_link = _find_weekly_map_link(dbx, vault_path, use_today)
    weekly_map_list = [weekly_map_link] if weekly_map_link else []

    return {
        'journal': journal_link,
        'weekly_cycle': weekly_cycle_list,
        'long_cycle': long_cycle_list,
        'weekly_map': weekly_map_list
    }


def _find_daily_action_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find the _Daily-Action folder inside the vault's _Daily folder."""
    daily_folder = _find_folder_in_path(dbx, vault_path, "_Daily")
    if not daily_folder:
        raise FileNotFoundError("Could not find a folder ending with '_Daily' in Dropbox")

    action_folder = _find_folder_in_path(dbx, daily_folder, "_Daily-Action")
    if not action_folder:
        raise FileNotFoundError("Could not find a folder ending with '_Daily-Action' in Dropbox")

    return action_folder


def _find_daily_action_template_path(dbx: dropbox.Dropbox, vault_path: str) -> str | None:
    """Resolve `*_Templates/daily-templates/daily_action_properties.md` via folder helpers."""
    templates_folder = _find_folder_in_path(dbx, vault_path, DAILY_ACTION_TEMPLATES_SUFFIX)
    if not templates_folder:
        logger.warning(
            "Daily Action template folder (*%s) not found; using hardcoded fallback body.",
            DAILY_ACTION_TEMPLATES_SUFFIX,
        )
        return None

    daily_templates_folder = _find_folder_in_path(
        dbx, templates_folder, DAILY_ACTION_TEMPLATE_SUBFOLDER
    )
    if not daily_templates_folder:
        logger.warning(
            "Daily Action template subfolder '%s' not found under %s; "
            "using hardcoded fallback body.",
            DAILY_ACTION_TEMPLATE_SUBFOLDER,
            templates_folder,
        )
        return None

    template_path, _name = _lookup_file_in_folder(
        dbx, daily_templates_folder, DAILY_ACTION_TEMPLATE_STEM
    )
    if not template_path:
        logger.warning(
            "Daily Action template '%s' not found in %s; using hardcoded fallback body.",
            DAILY_ACTION_TEMPLATE_STEM,
            daily_templates_folder,
        )
        return None
    return template_path


def _load_daily_action_template(dbx: dropbox.Dropbox, vault_path: str) -> str | None:
    """Download the vault Daily Action template, or None on failure."""
    template_path = _find_daily_action_template_path(dbx, vault_path)
    if not template_path:
        return None
    try:
        _, response = dbx.files_download(template_path)
        raw = response.content.decode("utf-8")
    except (dropbox.exceptions.ApiError, dropbox.exceptions.HttpError) as e:
        logger.warning(
            "Could not download Daily Action template from %s; "
            "using hardcoded fallback body: %s",
            template_path,
            e,
        )
        return None
    except Exception as e:
        logger.warning(
            "Failed reading Daily Action template from %s; "
            "using hardcoded fallback body: %s",
            template_path,
            e,
        )
        return None

    logger.info("Using Daily Action template from %s", template_path)
    return raw


def _strip_template_frontmatter(content: str) -> str:
    """Return the template body, ignoring YAML frontmatter if present.

    The live template is body-only. If frontmatter reappears, strip it and
    log so generated `_Journal` / cycle keys are never duplicated or overridden.
    A later `---` divider in the body is left intact.
    """
    if not content.startswith("---"):
        return content

    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return content

    yaml_end_index = -1
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            yaml_end_index = i
            break

    if yaml_end_index == -1:
        return content

    yaml_block = "\n".join(lines[1:yaml_end_index])
    try:
        parsed = yaml.safe_load(yaml_block) if yaml_block.strip() else {}
    except yaml.YAMLError:
        return content

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        return content

    keys = list(parsed.keys())
    logger.warning(
        "Ignoring YAML frontmatter on Daily Action template (%s); using generated frontmatter only.",
        keys,
    )
    return "\n".join(lines[yaml_end_index + 1:]).lstrip("\n")


def _resolve_daily_action_body(template_content: str | None) -> str:
    """Template body, or the hardcoded fallback when the template is missing."""
    if template_content is None:
        return FALLBACK_MAIN_CONTENT
    return _strip_template_frontmatter(template_content)


def _exclude_initiative_label() -> str:
    """Label to skip when listing Active initiatives (default: main-thread)."""
    if "DAILY_ACTION_EXCLUDE_INITIATIVE_LABEL" not in os.environ:
        return DEFAULT_EXCLUDE_INITIATIVE_LABEL
    return os.environ["DAILY_ACTION_EXCLUDE_INITIATIVE_LABEL"].strip()


def _initiative_label_names(initiative: dict) -> list[str]:
    """Lowercased label names from a Linear initiative node.

    Accepts the GraphQL shape `{labels: {nodes: [{name}]}}` used by
    `fetch_all_pages` / `INITIATIVES_QUERY`, or a flattened `labels: ["a"]`.
    """
    labels = initiative.get("labels")
    if not labels:
        return []
    if isinstance(labels, list):
        names = []
        for item in labels:
            if isinstance(item, str):
                names.append(item.lower())
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]).lower())
        return names
    nodes = labels.get("nodes") or []
    return [str(n.get("name", "")).lower() for n in nodes if n.get("name")]


def _select_active_initiatives(
    nodes: list[dict],
    exclude_label: str | None = None,
) -> list[dict]:
    """Active, non-empty-name initiatives, excluding `exclude_label`, sorted by name."""
    if exclude_label is None:
        exclude_label = _exclude_initiative_label()
    exclude = exclude_label.strip().lower()
    selected: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        if node.get("status") != ACTIVE_STATUS:
            continue
        name = (node.get("name") or "").strip()
        if not name:
            continue
        labels = _initiative_label_names(node)
        if exclude and exclude in labels:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(node)
    selected.sort(key=lambda item: item["name"].casefold())
    return selected


def fetch_active_initiatives() -> list[dict]:
    """Active (non-archived, status == Active) initiatives via the hub Linear client.

    Same query/client as `scripts.linear.test_find_main_thread_initiative` and
    `scripts.linear.sync_utils.fetch_all_pages`. Returns [] if Linear is
    unconfigured, times out, or otherwise fails — callers still create the note.
    """
    if not LINEAR_API_KEY and not os.getenv("LINEAR_API_KEY"):
        logger.warning(
            "LINEAR_API_KEY not set; creating Daily Action without initiative headers"
        )
        return []

    try:
        nodes = fetch_all_pages(
            ACTIVE_INITIATIVES_QUERY,
            {"first": 50},
            ["initiatives"],
        )
    except Exception as e:
        logger.warning(
            "Failed to fetch Linear initiatives for Daily Action; "
            "creating the note without initiative headers: %s",
            e,
        )
        return []

    return _select_active_initiatives(nodes)


def _existing_initiative_header_names(body: str) -> set[str]:
    """Initiative names that already have a `##### <name>` header in `body`."""
    names: set[str] = set()
    for line in body.splitlines():
        if is_initiative_work_header(line):
            names.add(line.strip()[5:].strip())
    return names


def _format_initiative_work_block(name: str) -> list[str]:
    """One `##### <name>` header, a `- ` prompt, and a trailing blank line."""
    return [f"##### {name}", "- ", ""]


def _append_initiative_headers(body: str, names: list[str]) -> str:
    """Insert missing initiative-work headers after the template `---`.

    Idempotent: existing `##### <name>` headers are left in place and not
    duplicated. New headers go after the template divider (and any
    headers already present) and before webhook sections other jobs append.
    """
    existing = {name.casefold() for name in _existing_initiative_header_names(body)}
    to_add = [name for name in names if name.casefold() not in existing]
    if not to_add:
        return body

    lines = body.split("\n")
    insert_at = template_end_insert_index(lines)
    new_lines: list[str] = []
    if insert_at > 0 and lines[insert_at - 1].strip() != "":
        new_lines.append("")
    for name in to_add:
        new_lines.extend(_format_initiative_work_block(name))
    result = lines[:insert_at] + new_lines + lines[insert_at:]
    return "\n".join(result)


def create_daily_action(use_today: bool = False) -> bool:
    """Create a daily action file in the Obsidian vault.

    Args:
        use_today: If True, create action file for today. Otherwise, create for tomorrow.

    Returns:
        True if the file was created or already exists, False on error.
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()
        daily_action_folder = _find_daily_action_folder(dbx, vault_path)

        # Determine target date
        target_day = _get_target_day(use_today)
        file_name = f"DA {target_day.strftime('%Y-%m-%d')}.md"
        dropbox_file_path = f"{daily_action_folder}/{file_name}"

        # Check if file already exists
        try:
            dbx.files_get_metadata(dropbox_file_path)
            logger.info(f"Daily action file for '{target_day.strftime('%Y-%m-%d')}' already exists. No new file created.")
            return True
        except dropbox.exceptions.ApiError as e:
            if not isinstance(e.error, dropbox.files.GetMetadataError):
                raise

        # File doesn't exist — create it
        logger.info(f"File '{file_name}' does not exist in Dropbox. Creating it now.")

        # Generate YAML frontmatter
        yaml_props = _generate_yaml_properties(dbx, vault_path, use_today)
        yaml_metadata = {
            '_Journal': yaml_props['journal'],
            '_Weekly-Cycle': yaml_props['weekly_cycle'],
            '_Long-Cycle': yaml_props['long_cycle'],
            '_Weekly-Map': yaml_props['weekly_map']
        }
        yaml_str = yaml.safe_dump(yaml_metadata, default_flow_style=False, sort_keys=False)
        yaml_section = f"---\n{yaml_str}---\n\n"

        template_content = _load_daily_action_template(dbx, vault_path)
        main_content = _resolve_daily_action_body(template_content)
        initiative_names = [item["name"] for item in fetch_active_initiatives()]
        main_content = _append_initiative_headers(main_content, initiative_names)
        content = yaml_section + main_content
        dbx.files_upload(content.encode('utf-8'), dropbox_file_path)
        logger.info(f"Successfully created daily action file '{file_name}' in Dropbox.")
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create daily action file in Obsidian vault")
    parser.add_argument("--today", action="store_true",
                        help="Create action file for today instead of tomorrow")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_daily_action(use_today=args.today)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

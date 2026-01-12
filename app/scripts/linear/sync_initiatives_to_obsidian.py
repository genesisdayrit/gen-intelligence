#!/usr/bin/env python3
"""Sync Linear Initiatives and Projects to Obsidian vault via Dropbox.

Syncs:
- Initiatives (organized by status folders)
- Initiative Updates
- Initiative Documents
- Projects under initiatives
- Project Updates
- Project Documents
- Project Issues

Usage:
    python -m app.scripts.linear.sync_initiatives_to_obsidian
    python -m app.scripts.linear.sync_initiatives_to_obsidian --include-archived
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime

import dropbox
import redis
import requests
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration & Constants
# =============================================================================

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(
    host=redis_host, port=redis_port, password=redis_password, decode_responses=True
)

# Workspace configuration
WORKSPACE_NAME = os.getenv('OBSIDIAN_LINEAR_WORKSPACE_NAME', '_Chapters-Technology')

# Status to folder mapping
STATUS_FOLDER_MAP = {
    "Active": "00_Active",
    "Planned": "01_Planned",
    "Completed": "02_Completed",
}
ARCHIVED_FOLDER = "03_Archived"

# =============================================================================
# GraphQL Queries
# =============================================================================

INITIATIVES_QUERY = """
query Initiatives($first: Int!, $after: String, $includeArchived: Boolean) {
  initiatives(first: $first, after: $after, includeArchived: $includeArchived, orderBy: updatedAt) {
    nodes {
      id
      name
      slugId
      url
      status
      description
      content
      health
      healthUpdatedAt
      startedAt
      completedAt
      targetDate
      targetDateResolution
      owner { id name email }
      creator { id name email }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

INITIATIVE_UPDATES_QUERY = """
query InitiativeUpdates($initiativeId: String!, $first: Int!, $after: String) {
  initiative(id: $initiativeId) {
    initiativeUpdates(first: $first, after: $after) {
      nodes {
        id
        body
        health
        createdAt
        updatedAt
        url
        user { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

INITIATIVE_DOCUMENTS_QUERY = """
query InitiativeDocuments($initiativeId: String!, $first: Int!, $after: String) {
  initiative(id: $initiativeId) {
    documents(first: $first, after: $after) {
      nodes {
        id
        title
        content
        createdAt
        updatedAt
        url
        creator { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

INITIATIVE_PROJECTS_QUERY = """
query InitiativeProjects($initiativeId: String!, $first: Int!, $after: String) {
  initiative(id: $initiativeId) {
    projects(first: $first, after: $after) {
      nodes {
        id
        name
        slugId
        url
        state
        description
        content
        health
        progress
        startDate
        targetDate
        createdAt
        updatedAt
        lead { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_UPDATES_QUERY = """
query ProjectUpdates($projectId: String!, $first: Int!, $after: String) {
  project(id: $projectId) {
    projectUpdates(first: $first, after: $after) {
      nodes {
        id
        body
        health
        createdAt
        updatedAt
        url
        user { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_DOCUMENTS_QUERY = """
query ProjectDocuments($projectId: String!, $first: Int!, $after: String) {
  project(id: $projectId) {
    documents(first: $first, after: $after) {
      nodes {
        id
        title
        content
        createdAt
        updatedAt
        url
        creator { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_ISSUES_QUERY = """
query ProjectIssues($projectId: String!, $first: Int!, $after: String) {
  project(id: $projectId) {
    issues(first: $first, after: $after) {
      nodes {
        id
        identifier
        title
        description
        priority
        estimate
        createdAt
        updatedAt
        completedAt
        dueDate
        url
        state { id name type }
        assignee { id name email }
        creator { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# =============================================================================
# Linear API Functions
# =============================================================================


def execute_query(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against the Linear API."""
    if not LINEAR_API_KEY:
        logger.error("LINEAR_API_KEY not set in environment")
        sys.exit(1)

    headers = {
        "Content-Type": "application/json",
        "Authorization": LINEAR_API_KEY,
    }

    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    response = requests.post(
        LINEAR_API_URL,
        headers=headers,
        json=payload,
        timeout=30,
    )

    if response.status_code != 200:
        logger.error(f"HTTP {response.status_code}: {response.text}")
        raise Exception(f"Linear API error: {response.status_code}")

    data = response.json()

    if "errors" in data:
        logger.error(f"GraphQL errors: {data['errors']}")
        raise Exception(f"GraphQL errors: {data['errors']}")

    return data


def fetch_all_pages(query: str, variables: dict, data_path: list[str]) -> list[dict]:
    """Fetch all pages of a paginated query."""
    all_nodes = []
    after = variables.get("after")

    while True:
        vars_with_cursor = {**variables, "after": after}
        data = execute_query(query, vars_with_cursor)

        result = data["data"]
        for key in data_path:
            result = result[key]

        all_nodes.extend(result["nodes"])

        page_info = result["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]

    return all_nodes


def fetch_initiatives(include_archived: bool = False) -> list[dict]:
    """Fetch all initiatives (base data only)."""
    return fetch_all_pages(
        INITIATIVES_QUERY,
        {"first": 50, "includeArchived": include_archived},
        ["initiatives"],
    )


def fetch_initiative_updates(initiative_id: str) -> list[dict]:
    """Fetch all updates for an initiative."""
    return fetch_all_pages(
        INITIATIVE_UPDATES_QUERY,
        {"initiativeId": initiative_id, "first": 50},
        ["initiative", "initiativeUpdates"],
    )


def fetch_initiative_documents(initiative_id: str) -> list[dict]:
    """Fetch all documents for an initiative."""
    return fetch_all_pages(
        INITIATIVE_DOCUMENTS_QUERY,
        {"initiativeId": initiative_id, "first": 50},
        ["initiative", "documents"],
    )


def fetch_initiative_projects(initiative_id: str) -> list[dict]:
    """Fetch all projects under an initiative."""
    return fetch_all_pages(
        INITIATIVE_PROJECTS_QUERY,
        {"initiativeId": initiative_id, "first": 50},
        ["initiative", "projects"],
    )


def fetch_project_updates(project_id: str) -> list[dict]:
    """Fetch all updates for a project."""
    return fetch_all_pages(
        PROJECT_UPDATES_QUERY,
        {"projectId": project_id, "first": 50},
        ["project", "projectUpdates"],
    )


def fetch_project_documents(project_id: str) -> list[dict]:
    """Fetch all documents for a project."""
    return fetch_all_pages(
        PROJECT_DOCUMENTS_QUERY,
        {"projectId": project_id, "first": 50},
        ["project", "documents"],
    )


def fetch_project_issues(project_id: str) -> list[dict]:
    """Fetch all issues for a project."""
    return fetch_all_pages(
        PROJECT_ISSUES_QUERY,
        {"projectId": project_id, "first": 50},
        ["project", "issues"],
    )


def fetch_all_initiative_data(include_archived: bool = False) -> list[dict]:
    """Fetch all initiatives with their related objects."""
    logger.info("Fetching initiatives from Linear...")
    initiatives = fetch_initiatives(include_archived)
    logger.info(f"Found {len(initiatives)} initiatives")

    for i, initiative in enumerate(initiatives):
        initiative_id = initiative["id"]
        initiative_name = initiative["name"]
        logger.info(f"Processing initiative {i + 1}/{len(initiatives)}: {initiative_name}")

        # Fetch initiative updates
        initiative["initiativeUpdates"] = fetch_initiative_updates(initiative_id)
        logger.debug(f"  Updates: {len(initiative['initiativeUpdates'])}")

        # Fetch initiative documents
        initiative["documents"] = fetch_initiative_documents(initiative_id)
        logger.debug(f"  Documents: {len(initiative['documents'])}")

        # Fetch projects
        projects = fetch_initiative_projects(initiative_id)
        logger.debug(f"  Projects: {len(projects)}")

        # For each project, fetch its nested objects
        for project in projects:
            project_id = project["id"]

            project["projectUpdates"] = fetch_project_updates(project_id)
            project["documents"] = fetch_project_documents(project_id)
            project["issues"] = fetch_project_issues(project_id)

        initiative["projects"] = projects

    return initiatives


# =============================================================================
# Dropbox Utilities
# =============================================================================


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


def _create_folder_if_not_exists(dbx: dropbox.Dropbox, path: str) -> bool:
    """Create a folder in Dropbox if it doesn't exist.

    Returns True if folder was created, False if it already existed.
    """
    try:
        dbx.files_create_folder_v2(path)
        logger.debug(f"Created folder: {path}")
        return True
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.CreateFolderError):
            if e.error.is_path() and e.error.get_path().is_conflict():
                return False
        raise


def _download_file_content(dbx: dropbox.Dropbox, path: str) -> str | None:
    """Download file content as string. Returns None if not found."""
    try:
        _, response = dbx.files_download(path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError:
        return None


def _upload_file(dbx: dropbox.Dropbox, path: str, content: str) -> None:
    """Upload file content to Dropbox, overwriting if exists."""
    dbx.files_upload(
        content.encode('utf-8'),
        path,
        mode=dropbox.files.WriteMode.overwrite
    )


def _folder_exists(dbx: dropbox.Dropbox, path: str) -> bool:
    """Check if a folder exists in Dropbox."""
    try:
        metadata = dbx.files_get_metadata(path)
        return isinstance(metadata, dropbox.files.FolderMetadata)
    except dropbox.exceptions.ApiError:
        return False


def _move_folder(dbx: dropbox.Dropbox, from_path: str, to_path: str) -> None:
    """Move a folder in Dropbox."""
    dbx.files_move_v2(from_path, to_path)
    logger.info(f"Moved folder: {from_path} -> {to_path}")


# =============================================================================
# Markdown Generation Functions
# =============================================================================


def sanitize_filename(name: str) -> str:
    """Sanitize a name for use as a filename."""
    sanitized = name.replace('/', '-')
    sanitized = re.sub(r'[:\*\?"<>|]', '', sanitized)
    return sanitized.strip()


def generate_yaml_frontmatter(data: dict, fields: list[tuple[str, str]]) -> str:
    """Generate YAML frontmatter from data dict.

    Args:
        data: Source data dictionary
        fields: List of (yaml_key, data_key) tuples
    """
    lines = ["---"]
    for yaml_key, data_key in fields:
        value = data.get(data_key)
        if isinstance(value, dict):
            # Handle nested objects like owner, lead
            value = value.get("name", "")
        if value is None:
            value = "null"
        elif isinstance(value, bool):
            value = str(value).lower()
        elif isinstance(value, (int, float)):
            value = str(value)
        else:
            # Escape quotes in strings
            value = str(value).replace('"', '\\"')
            if '\n' in value or ':' in value:
                value = f'"{value}"'
        lines.append(f"{yaml_key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def format_updates_section(updates: list[dict]) -> str:
    """Format updates as markdown, sorted descending by createdAt."""
    if not updates:
        return "_No updates yet._"

    # Sort by createdAt descending
    sorted_updates = sorted(
        updates,
        key=lambda u: u.get("createdAt", ""),
        reverse=True
    )

    lines = []
    for update in sorted_updates:
        created_at = update.get("createdAt", "")
        if created_at:
            # Parse ISO format and format nicely
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                timestamp = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                timestamp = created_at[:16]
        else:
            timestamp = "Unknown"

        user = update.get("user", {})
        user_name = user.get("name", "Unknown") if user else "Unknown"
        url = update.get("url", "")
        body = update.get("body", "").strip()

        lines.append(f"[{timestamp}] - [{user_name}]({url}):")
        if body:
            lines.append(body)
        lines.append("")  # Blank line between updates

    return "\n".join(lines).strip()


def format_issues_section(issues: list[dict]) -> str:
    """Format issues grouped by state."""
    if not issues:
        return "_No issues._"

    # Group by state name
    by_state: dict[str, list[dict]] = {}
    for issue in issues:
        state = issue.get("state", {})
        state_name = state.get("name", "Unknown") if state else "Unknown"
        if state_name not in by_state:
            by_state[state_name] = []
        by_state[state_name].append(issue)

    lines = []
    for state_name, state_issues in sorted(by_state.items()):
        lines.append(f"#### {state_name}")
        for issue in state_issues:
            identifier = issue.get("identifier", "")
            title = issue.get("title", "")
            url = issue.get("url", "")
            lines.append(f"- [[{identifier}]]({url}) {title}")
        lines.append("")

    return "\n".join(lines).strip()


def format_documents_links(documents: list[dict], parent_name: str) -> str:
    """Format document links as Obsidian wikilinks."""
    if not documents:
        return "_No documents._"

    lines = []
    for doc in documents:
        title = doc.get("title", "Untitled")
        url = doc.get("url", "")
        # Wikilink format: [[filename]](url)
        filename = f"{sanitize_filename(title)} - ({sanitize_filename(parent_name)})"
        lines.append(f"- [[{filename}]]({url})")

    return "\n".join(lines)


def format_projects_links(projects: list[dict]) -> str:
    """Format project links as Obsidian wikilinks."""
    if not projects:
        return "_No projects._"

    lines = []
    for project in projects:
        name = project.get("name", "Untitled")
        url = project.get("url", "")
        filename = f"(Project) {sanitize_filename(name)}"
        lines.append(f"- [[{filename}]]({url})")

    return "\n".join(lines)


def generate_initiative_markdown(initiative: dict) -> str:
    """Generate full markdown for an initiative file."""
    # YAML frontmatter
    frontmatter = generate_yaml_frontmatter(initiative, [
        ("id", "id"),
        ("name", "name"),
        ("url", "url"),
        ("status", "status"),
        ("health", "health"),
        ("startedAt", "startedAt"),
        ("completedAt", "completedAt"),
        ("targetDate", "targetDate"),
        ("owner", "owner"),
    ])

    # Description and content
    description = initiative.get("description", "") or ""
    content = initiative.get("content", "") or ""

    # Sections
    documents = initiative.get("documents", [])
    updates = initiative.get("initiativeUpdates", [])
    projects = initiative.get("projects", [])

    name = initiative.get("name", "")

    sections = [
        frontmatter,
        "",
        description,
        "",
        content,
        "",
        "### Related Linear Documents:",
        format_documents_links(documents, name),
        "",
        "### Updates:",
        format_updates_section(updates),
        "",
        "### Related Projects:",
        format_projects_links(projects),
    ]

    return "\n".join(sections)


def generate_project_markdown(project: dict, initiative_name: str) -> str:
    """Generate full markdown for a project file."""
    # YAML frontmatter
    frontmatter = generate_yaml_frontmatter(project, [
        ("id", "id"),
        ("name", "name"),
        ("url", "url"),
        ("state", "state"),
        ("health", "health"),
        ("progress", "progress"),
        ("startDate", "startDate"),
        ("targetDate", "targetDate"),
        ("lead", "lead"),
    ])

    # Description and content
    description = project.get("description", "") or ""
    content = project.get("content", "") or ""

    # Sections
    documents = project.get("documents", [])
    updates = project.get("projectUpdates", [])
    issues = project.get("issues", [])

    name = project.get("name", "")

    sections = [
        frontmatter,
        "",
        description,
        "",
        content,
        "",
        "### Related Linear Documents:",
        format_documents_links(documents, name),
        "",
        "### Updates:",
        format_updates_section(updates),
        "",
        "### Related Issues:",
        format_issues_section(issues),
    ]

    return "\n".join(sections)


def generate_document_markdown(document: dict, parent_name: str) -> str:
    """Generate full markdown for a document file."""
    creator = document.get("creator", {})
    creator_name = creator.get("name", "") if creator else ""

    # YAML frontmatter
    frontmatter_lines = [
        "---",
        f"id: {document.get('id', '')}",
        f"title: {document.get('title', '')}",
        f"url: {document.get('url', '')}",
        f"createdAt: {document.get('createdAt', '')}",
        f"updatedAt: {document.get('updatedAt', '')}",
        f"creator: {creator_name}",
        f"parent: {parent_name}",
        "---",
    ]
    frontmatter = "\n".join(frontmatter_lines)

    content = document.get("content", "") or ""

    return f"{frontmatter}\n\n{content}"


# =============================================================================
# Content Preservation
# =============================================================================


def parse_existing_file(content: str) -> tuple[str, str, str]:
    """Parse existing file to extract YAML, user content, and generated sections.

    Returns:
        (yaml_frontmatter, user_content, generated_sections)

    user_content = everything between frontmatter and first ### heading
    """
    # Check for YAML frontmatter
    if not content.startswith("---\n"):
        return "", content, ""

    # Find end of frontmatter
    lines = content.split("\n")
    yaml_end_index = -1
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            yaml_end_index = i
            break

    if yaml_end_index == -1:
        return "", content, ""

    yaml_section = "\n".join(lines[:yaml_end_index + 1])

    # Find first ### heading
    remaining = "\n".join(lines[yaml_end_index + 1:])
    heading_match = re.search(r'^### ', remaining, re.MULTILINE)

    if heading_match:
        user_content = remaining[:heading_match.start()].strip()
        generated_sections = remaining[heading_match.start():]
    else:
        user_content = remaining.strip()
        generated_sections = ""

    return yaml_section, user_content, generated_sections


def merge_with_user_content(new_markdown: str, existing_content: str | None) -> str:
    """Merge new markdown with user content from existing file.

    Preserves user content between frontmatter and first ### heading.
    """
    if not existing_content:
        return new_markdown

    _, user_content, _ = parse_existing_file(existing_content)

    if not user_content:
        return new_markdown

    # Find where to insert user content in new markdown
    # (after frontmatter, before first ### heading)
    new_yaml, _, new_generated = parse_existing_file(new_markdown)

    if not new_yaml:
        return new_markdown

    # Rebuild with user content preserved
    parts = [new_yaml, "", user_content, ""]

    # Find first ### in new markdown and append from there
    heading_match = re.search(r'^### ', new_markdown, re.MULTILINE)
    if heading_match:
        parts.append(new_markdown[heading_match.start():])

    return "\n".join(parts)


# =============================================================================
# Sync Operations
# =============================================================================


def find_initiatives_base_path(dbx: dropbox.Dropbox, vault_path: str) -> str | None:
    """Find the _Initiatives folder path in the vault.

    Looks for: {vault_path}/{XX}_Workspaces/{WORKSPACE_NAME}/_Initiatives
    """
    try:
        result = dbx.files_list_folder(vault_path)
        workspaces_folder = None

        while True:
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FolderMetadata):
                    if entry.name.endswith("_Workspaces"):
                        workspaces_folder = entry.path_lower
                        break

            if workspaces_folder or not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)

        if not workspaces_folder:
            logger.error("Could not find _Workspaces folder in vault")
            return None

        # Find workspace under workspaces folder
        workspace_path = f"{workspaces_folder}/{WORKSPACE_NAME}"
        initiatives_path = f"{workspace_path}/_Initiatives"

        if _folder_exists(dbx, initiatives_path):
            return initiatives_path

        logger.error(f"Could not find _Initiatives folder at {initiatives_path}")
        return None

    except dropbox.exceptions.ApiError as e:
        logger.error(f"Error finding initiatives path: {e}")
        return None


def find_existing_initiative_folder(
    dbx: dropbox.Dropbox,
    base_path: str,
    initiative_name: str
) -> tuple[str | None, str | None]:
    """Search all status folders for an existing initiative folder.

    Returns:
        (folder_path, status_folder) if found, (None, None) otherwise
    """
    sanitized_name = sanitize_filename(initiative_name)

    for status_folder in list(STATUS_FOLDER_MAP.values()) + [ARCHIVED_FOLDER]:
        folder_path = f"{base_path}/{status_folder}/{sanitized_name}"
        if _folder_exists(dbx, folder_path):
            return folder_path, status_folder

    return None, None


def is_manually_archived(
    dbx: dropbox.Dropbox,
    base_path: str,
    initiative_name: str
) -> bool:
    """Check if an initiative exists in 03_Archived (manually archived)."""
    sanitized_name = sanitize_filename(initiative_name)
    archived_path = f"{base_path}/{ARCHIVED_FOLDER}/{sanitized_name}"
    return _folder_exists(dbx, archived_path)


def get_target_status_folder(initiative: dict) -> str:
    """Get the target status folder for an initiative."""
    status = initiative.get("status", "Planned")
    return STATUS_FOLDER_MAP.get(status, "01_Planned")


def sync_document(
    dbx: dropbox.Dropbox,
    docs_folder: str,
    document: dict,
    parent_name: str,
    stats: dict
) -> None:
    """Sync a single document."""
    title = document.get("title", "Untitled")
    filename = f"{sanitize_filename(title)} - ({sanitize_filename(parent_name)}).md"
    file_path = f"{docs_folder}/{filename}"

    content = generate_document_markdown(document, parent_name)

    # Check existing and merge
    existing = _download_file_content(dbx, file_path)
    if existing:
        content = merge_with_user_content(content, existing)
        stats["documents_updated"] += 1
    else:
        stats["documents_created"] += 1

    _upload_file(dbx, file_path, content)
    logger.debug(f"Synced document: {filename}")


def sync_project(
    dbx: dropbox.Dropbox,
    projects_folder: str,
    project: dict,
    initiative_name: str,
    stats: dict
) -> None:
    """Sync a single project and its documents."""
    project_name = project.get("name", "Untitled")
    sanitized_name = sanitize_filename(project_name)

    # Create project folder
    project_folder = f"{projects_folder}/{sanitized_name}"
    _create_folder_if_not_exists(dbx, project_folder)

    # Sync project markdown file
    filename = f"(Project) {sanitized_name}.md"
    file_path = f"{project_folder}/{filename}"

    content = generate_project_markdown(project, initiative_name)

    existing = _download_file_content(dbx, file_path)
    if existing:
        content = merge_with_user_content(content, existing)
        stats["projects_updated"] += 1
    else:
        stats["projects_created"] += 1

    _upload_file(dbx, file_path, content)
    logger.debug(f"Synced project: {project_name}")

    # Sync project documents
    documents = project.get("documents", [])
    if documents:
        docs_folder = f"{project_folder}/_Docs"
        _create_folder_if_not_exists(dbx, docs_folder)

        for doc in documents:
            sync_document(dbx, docs_folder, doc, project_name, stats)


def sync_initiative(
    dbx: dropbox.Dropbox,
    base_path: str,
    initiative: dict,
    stats: dict
) -> None:
    """Sync a single initiative and all its children."""
    initiative_name = initiative.get("name", "Untitled")
    sanitized_name = sanitize_filename(initiative_name)

    # Check if manually archived - skip if so
    if is_manually_archived(dbx, base_path, initiative_name):
        logger.info(f"Skipping manually archived initiative: {initiative_name}")
        stats["skipped_archived"] += 1
        return

    # Determine target folder
    target_status = get_target_status_folder(initiative)
    target_folder = f"{base_path}/{target_status}/{sanitized_name}"

    # Check if exists in a different folder (status changed)
    existing_path, existing_status = find_existing_initiative_folder(
        dbx, base_path, initiative_name
    )

    if existing_path and existing_status != target_status:
        # Move to new status folder
        logger.info(f"Moving initiative '{initiative_name}' from {existing_status} to {target_status}")
        _move_folder(dbx, existing_path, target_folder)
        stats["initiatives_moved"] += 1
    elif not existing_path:
        # Create new folder
        _create_folder_if_not_exists(dbx, f"{base_path}/{target_status}")
        _create_folder_if_not_exists(dbx, target_folder)

    # Sync initiative markdown file
    filename = f"(Initiative) - {sanitized_name}.md"
    file_path = f"{target_folder}/{filename}"

    content = generate_initiative_markdown(initiative)

    existing_content = _download_file_content(dbx, file_path)
    if existing_content:
        content = merge_with_user_content(content, existing_content)
        stats["initiatives_updated"] += 1
    else:
        stats["initiatives_created"] += 1

    _upload_file(dbx, file_path, content)
    logger.info(f"Synced initiative: {initiative_name}")

    # Sync initiative documents
    documents = initiative.get("documents", [])
    if documents:
        docs_folder = f"{target_folder}/_Docs"
        _create_folder_if_not_exists(dbx, docs_folder)

        for doc in documents:
            sync_document(dbx, docs_folder, doc, initiative_name, stats)

    # Sync projects
    projects = initiative.get("projects", [])
    if projects:
        projects_folder = f"{target_folder}/_Projects"
        _create_folder_if_not_exists(dbx, projects_folder)

        for project in projects:
            sync_project(dbx, projects_folder, project, initiative_name, stats)


def sync_all(include_archived: bool = False) -> dict:
    """Main sync function.

    Returns summary statistics.
    """
    stats = {
        "initiatives_created": 0,
        "initiatives_updated": 0,
        "initiatives_moved": 0,
        "skipped_archived": 0,
        "projects_created": 0,
        "projects_updated": 0,
        "documents_created": 0,
        "documents_updated": 0,
        "errors": [],
    }

    # Fetch all initiative data from Linear
    try:
        initiatives = fetch_all_initiative_data(include_archived)
    except Exception as e:
        logger.error(f"Failed to fetch initiatives from Linear: {e}")
        stats["errors"].append(f"Linear API error: {e}")
        return stats

    if not initiatives:
        logger.info("No initiatives found")
        return stats

    # Get Dropbox client
    try:
        dbx = _get_dropbox_client()
    except Exception as e:
        logger.error(f"Failed to get Dropbox client: {e}")
        stats["errors"].append(f"Dropbox error: {e}")
        return stats

    # Find _Initiatives base path
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH not set")
        stats["errors"].append("DROPBOX_OBSIDIAN_VAULT_PATH not set")
        return stats

    base_path = find_initiatives_base_path(dbx, vault_path)
    if not base_path:
        stats["errors"].append("Could not find _Initiatives folder")
        return stats

    logger.info(f"Syncing to: {base_path}")

    # Sync each initiative
    for initiative in initiatives:
        try:
            sync_initiative(dbx, base_path, initiative, stats)
        except Exception as e:
            initiative_name = initiative.get("name", "Unknown")
            logger.error(f"Error syncing initiative '{initiative_name}': {e}")
            stats["errors"].append(f"Initiative '{initiative_name}': {e}")

    return stats


def print_summary(stats: dict) -> None:
    """Print sync summary."""
    print("\n" + "=" * 50)
    print("SYNC SUMMARY")
    print("=" * 50)
    print(f"Initiatives created: {stats['initiatives_created']}")
    print(f"Initiatives updated: {stats['initiatives_updated']}")
    print(f"Initiatives moved:   {stats['initiatives_moved']}")
    print(f"Skipped (archived):  {stats['skipped_archived']}")
    print(f"Projects created:    {stats['projects_created']}")
    print(f"Projects updated:    {stats['projects_updated']}")
    print(f"Documents created:   {stats['documents_created']}")
    print(f"Documents updated:   {stats['documents_updated']}")

    if stats["errors"]:
        print(f"\nErrors: {len(stats['errors'])}")
        for error in stats["errors"]:
            print(f"  - {error}")
    else:
        print("\nNo errors!")
    print("=" * 50)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Sync Linear Initiatives and Projects to Obsidian"
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived initiatives from Linear"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("Starting Linear to Obsidian sync...")

    stats = sync_all(include_archived=args.include_archived)

    print_summary(stats)

    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()

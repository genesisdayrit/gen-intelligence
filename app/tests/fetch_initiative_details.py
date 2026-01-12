"""
Fetch all Linear initiatives with their related objects.

Fetches:
- Initiatives (with full pagination)
- Initiative Updates
- Initiative Documents
- Projects under initiatives
- Project Updates
- Project Documents
- Project Issues

Usage:
    python app/tests/fetch_initiative_details.py

Requires LINEAR_API_KEY in .env file.
Output: app/tests/data/YYYYMMDD_HHMMSS_initiative_details.json
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")

# Simpler query to fetch initiatives (without deep nesting to avoid complexity limits)
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
        state { id name type }
        assignee { id name email }
        creator { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def execute_query(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against the Linear API."""
    if not LINEAR_API_KEY:
        print("ERROR: LINEAR_API_KEY not set in environment", file=sys.stderr)
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
        print(f"ERROR: HTTP {response.status_code}: {response.text}", file=sys.stderr)
        sys.exit(1)

    data = response.json()

    if "errors" in data:
        print(f"ERROR: GraphQL errors: {data['errors']}", file=sys.stderr)
        sys.exit(1)

    return data


def fetch_all_pages(query: str, variables: dict, data_path: list[str]) -> list[dict]:
    """
    Fetch all pages of a paginated query.

    Args:
        query: GraphQL query string
        variables: Query variables (must include 'first', optionally 'after')
        data_path: Path to the connection in the response (e.g., ['initiative', 'documents'])

    Returns:
        List of all nodes across all pages
    """
    all_nodes = []
    after = variables.get("after")

    while True:
        vars_with_cursor = {**variables, "after": after}
        data = execute_query(query, vars_with_cursor)

        # Navigate to the connection data
        result = data["data"]
        for key in data_path:
            result = result[key]

        all_nodes.extend(result["nodes"])

        page_info = result["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]

    return all_nodes


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


def fetch_initiatives(include_archived: bool = False) -> list[dict]:
    """Fetch all initiatives (base data only)."""
    return fetch_all_pages(
        INITIATIVES_QUERY,
        {"first": 50, "includeArchived": include_archived},
        ["initiatives"],
    )


def fetch_initiative_details(include_archived: bool = False) -> list[dict]:
    """Fetch all initiatives with their related objects."""
    print("Fetching initiatives...", file=sys.stderr)
    initiatives = fetch_initiatives(include_archived)
    print(f"  Found {len(initiatives)} initiatives", file=sys.stderr)

    for i, initiative in enumerate(initiatives):
        initiative_id = initiative["id"]
        initiative_name = initiative["name"]
        print(f"\nProcessing initiative {i + 1}/{len(initiatives)}: {initiative_name}", file=sys.stderr)

        # Fetch initiative updates
        print("  Fetching initiative updates...", file=sys.stderr)
        initiative["initiativeUpdates"] = fetch_initiative_updates(initiative_id)
        print(f"    Found {len(initiative['initiativeUpdates'])} updates", file=sys.stderr)

        # Fetch initiative documents
        print("  Fetching initiative documents...", file=sys.stderr)
        initiative["documents"] = fetch_initiative_documents(initiative_id)
        print(f"    Found {len(initiative['documents'])} documents", file=sys.stderr)

        # Fetch projects
        print("  Fetching projects...", file=sys.stderr)
        projects = fetch_initiative_projects(initiative_id)
        print(f"    Found {len(projects)} projects", file=sys.stderr)

        # For each project, fetch its nested objects
        for j, project in enumerate(projects):
            project_id = project["id"]
            project_name = project["name"]
            print(f"    Processing project {j + 1}/{len(projects)}: {project_name}", file=sys.stderr)

            # Fetch project updates
            project["projectUpdates"] = fetch_project_updates(project_id)
            print(f"      Updates: {len(project['projectUpdates'])}", file=sys.stderr)

            # Fetch project documents
            project["documents"] = fetch_project_documents(project_id)
            print(f"      Documents: {len(project['documents'])}", file=sys.stderr)

            # Fetch project issues
            project["issues"] = fetch_project_issues(project_id)
            print(f"      Issues: {len(project['issues'])}", file=sys.stderr)

        initiative["projects"] = projects

    return initiatives


def print_summary(initiatives: list[dict], output_file: Path) -> None:
    """Print a summary of fetched objects."""
    total_initiative_updates = 0
    total_initiative_docs = 0
    total_projects = 0
    total_project_updates = 0
    total_project_docs = 0
    total_issues = 0

    for initiative in initiatives:
        total_initiative_updates += len(initiative.get("initiativeUpdates", []))
        total_initiative_docs += len(initiative.get("documents", []))

        projects = initiative.get("projects", [])
        total_projects += len(projects)

        for project in projects:
            total_project_updates += len(project.get("projectUpdates", []))
            total_project_docs += len(project.get("documents", []))
            total_issues += len(project.get("issues", []))

    total_objects = (
        len(initiatives)
        + total_initiative_updates
        + total_initiative_docs
        + total_projects
        + total_project_updates
        + total_project_docs
        + total_issues
    )

    print("\n=== Fetch Summary ===", file=sys.stderr)
    print(f"Initiatives: {len(initiatives)}", file=sys.stderr)
    print(f"  - Initiative Updates: {total_initiative_updates}", file=sys.stderr)
    print(f"  - Initiative Documents: {total_initiative_docs}", file=sys.stderr)
    print(f"Projects: {total_projects}", file=sys.stderr)
    print(f"  - Project Updates: {total_project_updates}", file=sys.stderr)
    print(f"  - Project Documents: {total_project_docs}", file=sys.stderr)
    print(f"  - Issues: {total_issues}", file=sys.stderr)
    print(f"\nTotal objects fetched: {total_objects}", file=sys.stderr)
    print(f"Output: {output_file}", file=sys.stderr)


def main():
    """Fetch initiative details and save to timestamped JSON file."""
    print("Fetching Linear initiative details...", file=sys.stderr)

    initiatives = fetch_initiative_details()

    # Create data directory if needed
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)

    # Write to timestamped file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = data_dir / f"{timestamp}_initiative_details.json"

    with open(output_file, "w") as f:
        json.dump(initiatives, f, indent=2)

    print_summary(initiatives, output_file)


if __name__ == "__main__":
    main()

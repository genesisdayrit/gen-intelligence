#!/usr/bin/env python3
"""Find the active Linear initiative labeled 'main-thread'.

A small test/probe script: lists the currently active initiatives and
identifies the one carrying a given label (default: 'main-thread'). Handy
for verifying LINEAR_API_KEY works and for wiring the "main thread"
initiative into other automations.

Usage:
    python -m scripts.linear.test_find_main_thread_initiative
    python -m scripts.linear.test_find_main_thread_initiative --label main-thread
    python -m scripts.linear.test_find_main_thread_initiative --json
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")

# Linear's `initiative.status` for an in-flight initiative.
ACTIVE_STATUS = "Active"

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


def execute_query(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against the Linear API."""
    if not LINEAR_API_KEY:
        print("❌ LINEAR_API_KEY not set in environment (app/.env)")
        sys.exit(1)

    response = requests.post(
        LINEAR_API_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": LINEAR_API_KEY,
        },
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )

    if response.status_code != 200:
        raise Exception(f"Linear API error {response.status_code}: {response.text}")

    data = response.json()
    if "errors" in data:
        raise Exception(f"GraphQL errors: {data['errors']}")
    return data


def fetch_active_initiatives() -> list[dict]:
    """Return all active (non-archived, status == Active) initiatives.

    Each initiative is flattened to {id, name, slugId, url, status, labels}
    where labels is a list of label-name strings.
    """
    initiatives: list[dict] = []
    after = None

    while True:
        data = execute_query(ACTIVE_INITIATIVES_QUERY, {"first": 50, "after": after})
        conn = data["data"]["initiatives"]

        for node in conn["nodes"]:
            if node.get("status") != ACTIVE_STATUS:
                continue
            initiatives.append(
                {
                    "id": node["id"],
                    "name": node["name"],
                    "slugId": node.get("slugId"),
                    "url": node.get("url"),
                    "status": node.get("status"),
                    "labels": [l["name"] for l in node.get("labels", {}).get("nodes", [])],
                }
            )

        page_info = conn["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]

    return initiatives


def find_by_label(initiatives: list[dict], label: str) -> list[dict]:
    """Return initiatives whose labels contain `label` (case-insensitive)."""
    target = label.strip().lower()
    return [i for i in initiatives if target in [l.lower() for l in i["labels"]]]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find the active Linear initiative with a given label."
    )
    parser.add_argument(
        "--label",
        default="main-thread",
        help="Label to search for (default: main-thread).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the matching initiative(s) as JSON.",
    )
    args = parser.parse_args()

    initiatives = fetch_active_initiatives()
    matches = find_by_label(initiatives, args.label)

    if args.json:
        print(json.dumps(matches, indent=2))
        sys.exit(0 if matches else 1)

    print(f"Active initiatives: {len(initiatives)}")
    for i in initiatives:
        marker = "→" if args.label.lower() in [l.lower() for l in i["labels"]] else " "
        labels = ", ".join(i["labels"]) or "-"
        print(f"  {marker} {i['name']}  [labels: {labels}]")

    print()
    if not matches:
        print(f"❌ No active initiative found with label '{args.label}'.")
        sys.exit(1)

    if len(matches) > 1:
        print(f"⚠️  {len(matches)} active initiatives carry label '{args.label}':")

    for m in matches:
        print(f"✅ '{args.label}' initiative: {m['name']}")
        print(f"   id:  {m['id']}")
        print(f"   url: {m['url']}")


if __name__ == "__main__":
    main()

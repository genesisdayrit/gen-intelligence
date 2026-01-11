"""
Fetch all Linear initiatives from the workspace.

Usage:
    python app/tests/fetch_linear_initiatives.py

Requires LINEAR_API_KEY in .env file.
Output: app/tests/data/YYYYMMDD_HHMMSS_initiatives.json
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


def fetch_initiatives(include_archived: bool = False) -> list[dict]:
    """Fetch all initiatives from Linear with pagination."""
    if not LINEAR_API_KEY:
        print("ERROR: LINEAR_API_KEY not set in environment", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Content-Type": "application/json",
        "Authorization": LINEAR_API_KEY,
    }

    all_initiatives = []
    after = None
    page = 1

    while True:
        variables = {
            "first": 50,
            "after": after,
            "includeArchived": include_archived,
        }

        response = requests.post(
            LINEAR_API_URL,
            headers=headers,
            json={"query": INITIATIVES_QUERY, "variables": variables},
            timeout=30,
        )

        if response.status_code != 200:
            print(f"ERROR: HTTP {response.status_code}: {response.text}", file=sys.stderr)
            sys.exit(1)

        data = response.json()

        if "errors" in data:
            print(f"ERROR: GraphQL errors: {data['errors']}", file=sys.stderr)
            sys.exit(1)

        initiatives_data = data["data"]["initiatives"]
        nodes = initiatives_data["nodes"]
        all_initiatives.extend(nodes)

        print(f"Page {page}: fetched {len(nodes)} initiatives (total: {len(all_initiatives)})", file=sys.stderr)

        page_info = initiatives_data["pageInfo"]
        if not page_info["hasNextPage"]:
            break

        after = page_info["endCursor"]
        page += 1

    return all_initiatives


def main():
    """Fetch initiatives and save to timestamped JSON file."""
    print("Fetching Linear initiatives...", file=sys.stderr)

    initiatives = fetch_initiatives()

    # Create data directory if needed
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)

    # Write to timestamped file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = data_dir / f"{timestamp}_initiatives.json"

    with open(output_file, "w") as f:
        json.dump(initiatives, f, indent=2)

    print(f"Saved {len(initiatives)} initiatives to {output_file}", file=sys.stderr)


if __name__ == "__main__":
    main()

# Cycle Context Gathering Guide

Reference guide for AI agents to understand and use the weekly cycle data collection infrastructure. This document explains what data is available, how to generate it, and how to interpret it for cycle context gathering.

## Overview

The system collects data from two primary sources for weekly cycle analysis:

1. **Linear** - Project management data (initiatives, projects, issues, updates)
2. **Todoist** - Personal task completions

All data is scoped to weekly cycles running **Wednesday through Tuesday**.

---

## Cycle Date Logic

### Weekly Boundaries

- **Start**: Wednesday 00:00:00
- **End**: Tuesday 23:59:59
- **Day rollover buffer**: 3am (times between midnight-3am count as previous day)

### Date Calculation

```python
from datetime import datetime, timedelta

def get_cycle_bounds(tz, previous: bool = False):
    """Calculate Wednesday-Tuesday cycle bounds."""
    now = datetime.now(tz)

    # Apply 3am rollover buffer
    if now.hour < 3:
        effective_now = now - timedelta(days=1)
    else:
        effective_now = now

    # Wednesday is weekday 2 (Monday=0)
    days_since_wednesday = (effective_now.weekday() - 2) % 7

    cycle_start = effective_now - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)

    if previous:
        cycle_start -= timedelta(days=7)
        cycle_end -= timedelta(days=7)

    return cycle_start, cycle_end
```

### Relevant Code

- Date helpers: `app/services/obsidian/utils/date_helpers.py`
- `DAY_ROLLOVER_HOUR = 3` constant
- `get_effective_date()` function for 3am buffer

---

## Data Sources

### 1. Linear Cycle Summary

**Script**: `app/scripts/generate_cycle_summary_data.py`

**Usage**:
```bash
cd app

# Current cycle (default)
uv run python -m scripts.generate_cycle_summary_data

# Previous cycle
uv run python -m scripts.generate_cycle_summary_data --previous-cycle

# With debug logging
uv run python -m scripts.generate_cycle_summary_data --debug
```

**Output location**: `app/tests/data/{timestamp}_cycle_summary_{start}-{end}.json`

**Output structure**:
```json
{
  "cycle_start_date": "2026-01-07",
  "cycle_end_date": "2026-01-13",
  "latest_initiative_updates": [...],
  "active_initiatives": [...],
  "other_completed_issues": [...],
  "newly_created_initiatives": [...],
  "newly_created_projects": [...]
}
```

### 2. Todoist Completed Tasks

**Script**: `app/tests/test_todoist_cycle_completions.py`

**Usage**:
```bash
cd app

# Current cycle
uv run python tests/test_todoist_cycle_completions.py

# Previous cycle
uv run python tests/test_todoist_cycle_completions.py --previous
```

**Output location**: `app/tests/data/{timestamp}_todoist_completed_{current|previous}_cycle.json`

**Output structure**:
```json
{
  "metadata": {
    "fetched_at": "2026-01-13T19:19:53.077296",
    "cycle_type": "current",
    "cycle_start": "2026-01-07T00:00:00-08:00",
    "cycle_end": "2026-01-13T23:59:59-08:00",
    "cycle_range": "(Jan. 07 - Jan. 13, 2026)",
    "total_tasks": 135
  },
  "tasks": [...]
}
```

---

## Linear Data Structure

### Entity Hierarchy

```
Initiative (strategic objective)
├── Initiative Updates (progress reports)
├── Initiative Documents (specs, plans)
└── Projects (execution containers)
    ├── Project Updates (progress reports)
    ├── Project Documents (technical docs)
    └── Issues (individual work items)
```

### Key Fields in Cycle Summary

#### `latest_initiative_updates`

Array of the most recent update for each active initiative. Use this to get a quick overview of what's happening across all initiatives.

```json
{
  "initiative_id": "uuid",
  "initiative_name": "Product Engineering Processes",
  "update": {
    "body": "What did you get done yesterday...",
    "health": "onTrack",
    "createdAt": "2026-01-12T15:17:28.750Z",
    "user": { "name": "Genesis Dayrit" }
  }
}
```

#### `active_initiatives`

Full enriched data for each active initiative including:

| Field | Description |
|-------|-------------|
| `updates_in_cycle` | All updates posted during this cycle |
| `latest_update` | Most recent update (may be outside cycle) |
| `projects` | Array of projects under this initiative |

Each project contains:

| Field | Description |
|-------|-------------|
| `completed_issues` | Issues completed during cycle |
| `created_issues` | Issues created during cycle |
| `modified_issues` | Issues modified during cycle |
| `updates_in_cycle` | Project updates posted during cycle |

#### `other_completed_issues`

Issues completed during the cycle that don't belong to any active initiative's projects. Useful for tracking ad-hoc or maintenance work.

#### `newly_created_initiatives`

Initiatives created during the cycle period.

#### `newly_created_projects`

Projects created during the cycle period.

---

## Todoist Data Structure

### Task Object

```json
{
  "content": "GD-272: Create Linear Obsidian Workspaces Path",
  "completed_at": "2026-01-14T02:45:08.685233Z",
  "added_at": "2026-01-14T02:45:08.317821Z",
  "project_id": "6ffRM9cQmCmjRg2f",
  "priority": 1,
  "due": {
    "date": "2026-01-14",
    "is_recurring": false
  }
}
```

### Common Task Patterns

Tasks often follow naming conventions:
- `GD-XXX: Task name` - Linked to Linear issue
- `gen-intelligence: Description` - Commits/work on this project
- Plain task names - General tasks

---

## Common AI Use Cases

### 1. Generate Weekly Summary

Combine data from both sources to create a comprehensive cycle summary:

```python
# Load both data files
with open("tests/data/latest_cycle_summary.json") as f:
    linear_data = json.load(f)

with open("tests/data/latest_todoist_completed.json") as f:
    todoist_data = json.load(f)

# Extract key metrics
initiatives = linear_data["active_initiatives"]
total_issues_completed = sum(
    len(p["completed_issues"])
    for i in initiatives
    for p in i.get("projects", [])
)
total_tasks = todoist_data["metadata"]["total_tasks"]
```

### 2. Find Work on Specific Initiative

```python
def get_initiative_summary(data, initiative_name):
    """Get all work done on a specific initiative."""
    for init in data["active_initiatives"]:
        if initiative_name.lower() in init["name"].lower():
            return {
                "updates": init["updates_in_cycle"],
                "projects": [
                    {
                        "name": p["name"],
                        "completed": len(p["completed_issues"]),
                        "created": len(p["created_issues"]),
                    }
                    for p in init.get("projects", [])
                ]
            }
    return None
```

### 3. Identify Completed Linear Issues

```python
def get_all_completed_issues(data):
    """Get all issues completed across all initiatives."""
    completed = []
    for init in data["active_initiatives"]:
        for project in init.get("projects", []):
            for issue in project.get("completed_issues", []):
                completed.append({
                    "identifier": issue["identifier"],
                    "title": issue["title"],
                    "project": project["name"],
                    "initiative": init["name"],
                })

    # Add "other" completed issues
    for issue in data.get("other_completed_issues", []):
        completed.append({
            "identifier": issue["identifier"],
            "title": issue["title"],
            "project": issue.get("project", {}).get("name", "Unknown"),
            "initiative": None,
        })

    return completed
```

### 4. Match Todoist Tasks to Linear Issues

```python
import re

def match_todoist_to_linear(todoist_tasks, linear_issues):
    """Match Todoist tasks to Linear issues by identifier."""
    matches = []

    for task in todoist_tasks:
        content = task["content"]
        # Look for GD-XXX pattern
        match = re.match(r"(GD-\d+)", content)
        if match:
            identifier = match.group(1)
            for issue in linear_issues:
                if issue["identifier"] == identifier:
                    matches.append({
                        "todoist_task": content,
                        "linear_issue": issue["title"],
                        "identifier": identifier,
                    })
                    break

    return matches
```

---

## Data File Naming Convention

Files are saved with timestamps for versioning:

```
{YYYYMMDD}_{HHMMSS}_cycle_summary_{startYYYYMMDD}-{endYYYYMMDD}.json
{YYYYMMDD}_{HHMMSS}_todoist_completed_{current|previous}_cycle.json
```

Example:
```
20260113_192722_cycle_summary_20260107-20260113.json
20260113_191953_todoist_completed_current_cycle.json
```

To find the most recent file:
```bash
ls -t app/tests/data/*cycle_summary*.json | head -1
ls -t app/tests/data/*todoist_completed*.json | head -1
```

---

## Environment Variables

Required for data collection:

```bash
# Linear API
LINEAR_API_KEY=your_linear_api_key

# Todoist API
TODOIST_ACCESS_TOKEN=your_todoist_token

# Timezone (affects cycle boundaries)
SYSTEM_TIMEZONE=US/Pacific
```

---

## Related Files

| File | Purpose |
|------|---------|
| `app/scripts/generate_cycle_summary_data.py` | Linear cycle data collection |
| `app/tests/test_todoist_cycle_completions.py` | Todoist completed tasks |
| `app/scripts/linear/sync_utils.py` | Linear API utilities |
| `app/services/obsidian/utils/date_helpers.py` | Date/cycle utilities |
| `docs/linear-api.md` | Linear API reference |
| `docs/guides/weekly-cycle-sync.md` | Weekly cycle Obsidian sync |

---

## Tips for AI Agents

1. **Always check data freshness** - Look at `metadata.fetched_at` or file timestamps to ensure data is current.

2. **Use `latest_initiative_updates` for quick context** - This gives you one update per active initiative without parsing the full structure.

3. **Cross-reference sources** - Todoist tasks often reference Linear issue IDs (e.g., `GD-123`), enabling correlation between personal task tracking and project management.

4. **Handle empty data gracefully** - Not all initiatives have projects, not all projects have issues completed in a given cycle.

5. **Consider the 3am rollover** - Tasks completed between midnight and 3am count as the previous day's work.

6. **Regenerate data when needed** - If data seems stale, run the scripts again to get fresh information.

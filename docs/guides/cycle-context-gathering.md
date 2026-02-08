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

**Script**: `app/tests/test_generate_cycle_summary_data.py`

**Usage**:
```bash
cd app

# Current cycle (default)
uv run python tests/test_generate_cycle_summary_data.py

# Previous cycle
uv run python tests/test_generate_cycle_summary_data.py --previous-cycle

# With debug logging
uv run python tests/test_generate_cycle_summary_data.py --debug
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

### 3. Latest Headlines (Past Cycle)

**Script**: `app/tests/generate_latest_headlines.py`

Generates summary headlines from completed cycle data using a two-stage LLM process:
- **Stage 1**: GPT-4o-mini generates 1-3 headlines per initiative
- **Stage 2**: GPT-4o synthesizes all headlines into a cohesive markdown section

**Usage**:
```bash
cd app

# Current cycle (default)
uv run python -m tests.generate_latest_headlines

# Previous cycle
uv run python -m tests.generate_latest_headlines --previous

# With debug logging
uv run python -m tests.generate_latest_headlines --debug
```

**Output location**: `app/tests/data/{timestamp}_latest_headlines_{start}-{end}.json`

**Output structure**:
```json
{
  "metadata": {
    "generated_at": "2026-01-13T19:30:00.000000",
    "cycle_type": "current",
    "cycle_start": "2026-01-07",
    "cycle_end": "2026-01-13",
    "active_initiative_count": 5
  },
  "initiative_headlines": [...],
  "other_headlines": {...},
  "final_synthesis": {...},
  "final_markdown_section": "### Latest Headlines\n\n1. [Initiative Name]\n   1. Headline..."
}
```

**Extracting headlines**:
```python
# Load the latest headlines file
with open("tests/data/latest_headlines.json") as f:
    data = json.load(f)

# Get the final formatted markdown section
markdown_headlines = data["final_markdown_section"]

# Or get raw headlines per initiative
for init in data["initiative_headlines"]:
    print(f"{init['initiative_name']}: {init['parsed_headlines']}")
```

### 4. Next Cycle Headlines (Projected)

**Script**: `app/scripts/generate_next_cycle_headlines.py`

Generates projected headlines for the upcoming cycle by extracting "This Week" sections from the latest initiative updates and processing them with an LLM.

**Usage**:
```bash
# Generate headlines
python app/scripts/generate_next_cycle_headlines.py

# Show what would be sent to LLM (no API calls)
python app/scripts/generate_next_cycle_headlines.py --dry-run

# Use specific model
python app/scripts/generate_next_cycle_headlines.py --model gpt-4o

# With debug logging
python app/scripts/generate_next_cycle_headlines.py --debug
```

### 5. Cycle Summary Email

**Script**: `app/scripts/send_cycle_summary_email.py`

Generates and sends a comprehensive weekly cycle summary email combining headlines from the past cycle, projected headlines for the upcoming cycle, and initiative completions with completed issues.

**Usage**:
```bash
cd app

# Send email for previous cycle (default)
python -m scripts.send_cycle_summary_email

# Send for current cycle
python -m scripts.send_cycle_summary_email --current

# Generate without sending (dry run)
python -m scripts.send_cycle_summary_email --dry-run

# Save HTML output to file
python -m scripts.send_cycle_summary_email --output email.html

# Include all initiatives (not just active ones)
python -m scripts.send_cycle_summary_email --all-initiatives

# With debug logging
python -m scripts.send_cycle_summary_email --debug
```

**Note**: The `--all-initiatives` flag removes the "Active" status filter, including all non-archived initiatives in the report. Initiatives with no cycle activity will still be excluded from the output.

**Automated schedule**: The email is sent automatically via an in-app cron job configured in `app/scheduler.py`. It runs every **Wednesday at 3:30 AM** (in the `SYSTEM_TIMEZONE`, defaulting to `America/Los_Angeles`). This is intentionally after the 3:00 AM day-rollover buffer, so the previous cycle (Wednesday–Tuesday) is fully closed before the summary is generated. The job sends the **previous** cycle summary (`current=False`, `all_initiatives=False`).

The scheduler uses APScheduler's `BackgroundScheduler` running inside the FastAPI process. It includes a 1-hour misfire grace time — if the server is down at 3:30 AM, the job will still fire once the server comes back up within that window. You can also trigger it manually via the API:

```bash
# Trigger the job immediately
curl -X POST http://localhost:8000/scheduler/jobs/send_cycle_summary_email/run
```

**Output location**: `app/tests/data/{timestamp}_next_cycle_headlines.json`

**Output structure**:
```json
{
  "generated_at": "2026-01-13T19:30:00.000000Z",
  "model": "gpt-4o-mini",
  "total_initiatives": 5,
  "headlines_generated": 4,
  "headlines": [
    {
      "initiative_id": "uuid",
      "initiative_name": "Product Engineering Processes",
      "llm_input": {
        "raw_update_body": "Full update text...",
        "this_week_section": "Extracted 'This Week' content..."
      },
      "llm_output": {
        "projected_headline": "Ship automated deployment pipeline for staging",
        "raw_response": "..."
      }
    }
  ]
}
```

**Extracting projected headlines**:
```python
# Load the latest next cycle headlines file
with open("tests/data/latest_next_cycle_headlines.json") as f:
    data = json.load(f)

# Get all projected headlines
for h in data["headlines"]:
    if h["llm_output"] and h["llm_output"].get("projected_headline"):
        print(f"{h['initiative_name']}: {h['llm_output']['projected_headline']}")
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

### 5. Get Latest Headlines for Weekly Summary

```python
import glob
import json

def get_latest_headlines() -> str:
    """Get the most recent cycle headlines markdown section."""
    data_dir = "app/tests/data"

    # Find the most recent headlines file
    pattern = f"{data_dir}/*_latest_headlines_*.json"
    files = sorted(glob.glob(pattern), reverse=True)

    if not files:
        return "No headlines found"

    with open(files[0]) as f:
        data = json.load(f)

    return data.get("final_markdown_section", "")


def get_projected_headlines() -> list[dict]:
    """Get projected headlines for the upcoming cycle."""
    data_dir = "app/tests/data"

    # Find the most recent next cycle headlines file
    pattern = f"{data_dir}/*_next_cycle_headlines.json"
    files = sorted(glob.glob(pattern), reverse=True)

    if not files:
        return []

    with open(files[0]) as f:
        data = json.load(f)

    return [
        {
            "initiative": h["initiative_name"],
            "headline": h["llm_output"]["projected_headline"],
        }
        for h in data.get("headlines", [])
        if h.get("llm_output", {}).get("projected_headline")
    ]
```

### 6. Format Headlines for Obsidian Sections

The headline scripts store parsed data separately from any synthesized output, allowing you to reconstruct custom formats. Use these helpers to generate the exact Obsidian section formats:

```python
def format_last_cycle_headlines(data: dict) -> str:
    """Format headlines for 'Headlines from Last Cycle:' Obsidian section.

    Args:
        data: Loaded JSON from *_latest_headlines_*.json file
    """
    lines = ["### Headlines from Last Cycle:", ""]

    idx = 1
    for init in data["initiative_headlines"]:
        headlines = init.get("parsed_headlines", [])
        if not headlines:
            continue
        lines.append(f"{idx}. {init['initiative_name']}")
        for i, h in enumerate(headlines, 1):
            lines.append(f"   {i}. {h}")
        idx += 1

    # Add other headlines if present
    other = data.get("other_headlines", {}).get("parsed_headlines", [])
    if other:
        lines.append(f"{idx}. Other")
        for i, h in enumerate(other, 1):
            lines.append(f"   {i}. {h}")

    return "\n".join(lines)


def format_projected_headlines(data: dict) -> str:
    """Format headlines for 'New Projected Headlines' Obsidian section.

    Args:
        data: Loaded JSON from *_next_cycle_headlines.json file
    """
    lines = ["### New Projected Headlines (anticipate how you're going to win this week):", ""]

    for idx, h in enumerate(data.get("headlines", []), 1):
        headline = h.get("llm_output", {}).get("projected_headline")
        if headline:
            lines.append(f"{idx}. **{h['initiative_name']}**: {headline}")

    return "\n".join(lines)
```

**Note:** The `final_markdown_section` from `generate_latest_headlines.py` uses a different header ("### Latest Headlines"). Use the parsed data with these formatters if you need the exact Obsidian section titles.

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
| `app/tests/test_generate_cycle_summary_data.py` | Linear cycle data collection |
| `app/tests/test_todoist_cycle_completions.py` | Todoist completed tasks |
| `app/tests/generate_latest_headlines.py` | Generate headlines from past cycle data |
| `app/scripts/generate_next_cycle_headlines.py` | Generate projected headlines for next cycle |
| `app/scripts/send_cycle_summary_email.py` | Generate and send weekly cycle summary email |
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

7. **Use headlines for summaries** - The `final_markdown_section` from latest headlines provides a ready-to-use summary. For projected work, use the next cycle headlines.

8. **Headlines vs raw data** - Use headline scripts for human-readable summaries; use cycle summary data for detailed analysis or custom processing.

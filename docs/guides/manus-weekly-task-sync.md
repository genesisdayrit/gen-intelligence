# Manus Weekly Task Sync

Automatically append your Manus AI task history to your Obsidian weekly notes via Dropbox.

## Overview

A standalone script that fetches tasks from the Manus API for the current week (Monday-Sunday) and appends them to the bottom of your `Week-Ending` note in Obsidian. Designed to run as a Sunday cron job.

1. Fetches all Manus tasks created since Monday
2. Finds your `_Weekly` folder in the Obsidian vault
3. Locates the `Week-Ending-YYYY-MM-DD.md` file for this Sunday
4. Appends a `## Manus Tasks` section to the bottom of the file
5. Deduplicates by URL so re-running is safe

## Prerequisites

- Dropbox configured with Obsidian vault access
- Redis running for access token caching
- A Manus API key (get from [manus.im](https://manus.im))
- Weekly notes following the expected folder structure

## Folder Structure

The script expects this structure in your Dropbox-synced Obsidian vault:

```
Your_Vault/
├── *_Weekly/                         # Folder ending with "_Weekly"
│   └── _Weeks/                       # Subfolder for weekly notes
│       ├── Week-Ending-2026-02-08.md
│       ├── Week-Ending-2026-02-01.md
│       └── ...
```

### File Naming Convention

Weekly note files must follow the format: `Week-Ending-YYYY-MM-DD.md` where the date is always a Sunday.

## Environment Variables

Required variables in your `.env`:

```bash
# Manus AI
MANUS_API_KEY=your_manus_api_key

# Dropbox OAuth credentials
DROPBOX_ACCESS_KEY=your_app_key
DROPBOX_ACCESS_SECRET=your_app_secret
DROPBOX_REFRESH_TOKEN=your_refresh_token

# Path to Obsidian vault in Dropbox
DROPBOX_OBSIDIAN_VAULT_PATH=/Your_Vault

# Timezone for date formatting (default: US/Eastern)
SYSTEM_TIMEZONE=US/Pacific

# Redis configuration
REDIS_HOST=localhost
REDIS_PORT=6379
```

## Usage

```bash
cd app

# Preview tasks without writing to Obsidian
uv run python -m scripts.manus.append_weekly_tasks_to_obsidian --dry-run

# Write tasks to the weekly note
uv run python -m scripts.manus.append_weekly_tasks_to_obsidian
```

### Dry Run Output

```
============================================================
DRY RUN - 27 Manus tasks found (Week ending 2026-02-08)
============================================================
  2026-02-02  How to Prevent Gmail Accounts From Being Flagged
              https://manus.im/app/cFtEhxKubEPfwsqL9rD6DV
  2026-02-03  Planning Products with AI Using Desired Outcomes in 2026
              https://manus.im/app/oZM6AUI397kOR4tjhk35BK
  ...
============================================================
```

## Output Format

The script appends a section to the bottom of the weekly note:

```markdown
## Manus Tasks
- [How to Prevent Gmail Accounts From Being Flagged](https://manus.im/app/cFtEhxKubEPfwsqL9rD6DV) - 2026-02-02
- [Planning Products with AI Using Desired Outcomes in 2026](https://manus.im/app/oZM6AUI397kOR4tjhk35BK) - 2026-02-03
- [Best Local Pizza in Fontana or Rancho](https://manus.im/app/5TLocvkjZKxhx4jQNXThjG) - 2026-02-08
```

Tasks are sorted chronologically (oldest first) and each entry links to the Manus task page.

## How It Works

### Week Boundaries

The script uses Monday-Sunday weeks:
- **Week start**: Monday 00:00 (in your configured timezone)
- **Week end**: Sunday (the date used in the filename)

For example, if today is Sunday Feb 8, 2026:
- Tasks fetched from: Monday Feb 2 onward
- File searched: `Week-Ending-2026-02-08.md`

### Manus API

The script calls `GET https://api.manus.ai/v1/tasks` with:
- `createdAfter`: Monday midnight as a Unix timestamp
- `limit`: 100 per page (API maximum)
- Pagination handled automatically via `has_more` / `last_id` cursor

### Deduplication

Re-running the script on the same file is safe. It extracts existing Manus task URLs from the `## Manus Tasks` section and only appends tasks whose URL is not already present.

## Cron Setup

To run automatically every Sunday evening:

```bash
crontab -e
```

Add:

```
0 21 * * 0 cd /path/to/casablanca-v1/app && uv run python -m scripts.manus.append_weekly_tasks_to_obsidian >> /var/log/manus-sync.log 2>&1
```

This runs at 9 PM every Sunday (day 0).

## Troubleshooting

### "MANUS_API_KEY not set"

- Ensure `MANUS_API_KEY` is set in your `.env` file
- Verify the key is valid at [manus.im](https://manus.im)

### "Could not find '_Weekly' folder"

- Ensure your Obsidian vault has a folder ending with `_Weekly`
- Check `DROPBOX_OBSIDIAN_VAULT_PATH` is correct
- Verify Dropbox credentials have access to the vault

### "Weekly note not found: Week-Ending-YYYY-MM-DD.md"

- The script does not auto-create weekly notes — the file must already exist
- Verify the file name matches exactly: `Week-Ending-2026-02-08.md`
- Check the file is in the `_Weeks` subfolder

### Token Refresh Issues

The system automatically refreshes Dropbox tokens using Redis caching. If you see token errors:

```bash
# Check Redis is running
docker compose logs redis

# Clear cached token to force refresh
redis-cli DEL DROPBOX_ACCESS_TOKEN
```

## Code Location

- Script: `app/scripts/manus/append_weekly_tasks_to_obsidian.py`

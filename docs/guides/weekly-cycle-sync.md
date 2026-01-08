# Weekly Cycle Sync

Automatically sync completed Todoist tasks to your Obsidian Weekly Cycle notes via Dropbox.

## Overview

The Weekly Cycle sync feature writes completed tasks from Todoist to the appropriate day section in your current Weekly Cycle note. It:

1. Finds your `_Cycles` folder in the Obsidian vault
2. Locates the current week's cycle file (Wednesday-Tuesday)
3. Appends completed tasks to the correct day's "Completed Tasks" section

## Prerequisites

- Dropbox configured with Obsidian vault access (see environment setup)
- Redis running for access token caching
- Todoist webhook configured (see [Todoist Webhook Setup](./todoist-webhook-setup.md))
- Weekly Cycle notes following the expected folder structure

## Folder Structure

The sync expects this structure in your Dropbox-synced Obsidian vault:

```
Your_Vault/
├── *_Cycles/                          # Folder ending with "_Cycles"
│   └── _Weekly-Cycles/                # Subfolder for weekly notes
│       ├── Weekly Cycle (Jan. 01 - Jan. 07, 2026).md
│       ├── Weekly Cycle (Jan. 08 - Jan. 14, 2026).md
│       └── ...
```

### File Naming Convention

Weekly cycle files must include the date range in this format:
- `(Jan. 07 - Jan. 13, 2026)` - Month abbreviated with period, zero-padded day, year at end

### Weekly Cycle File Structure

Each weekly cycle file should have day sections like:

```markdown
### Wednesday -

[Your content for Wednesday]

---

### Thursday -

[Your content for Thursday]

---
```

When tasks are completed, they're added under a `##### Completed Tasks:` header within the appropriate day section:

```markdown
### Wednesday -

[Your content for Wednesday]

##### Completed Tasks:
[14:30 PM] Buy groceries
[16:45 PM] Review PR

---
```

## Environment Variables

Required variables in your `.env`:

```bash
# Dropbox OAuth credentials
DROPBOX_ACCESS_KEY=your_app_key
DROPBOX_ACCESS_SECRET=your_app_secret
DROPBOX_REFRESH_TOKEN=your_refresh_token

# Path to Obsidian vault in Dropbox
DROPBOX_OBSIDIAN_VAULT_PATH=/Your_Vault

# Timezone for timestamps (default: US/Eastern)
SYSTEM_TIMEZONE=US/Eastern

# Redis configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your_password  # optional
```

## How It Works

### Week Boundaries

The system uses Wednesday-Tuesday weeks:
- **Cycle start**: Wednesday
- **Cycle end**: Tuesday

For example, if today is Friday Jan 10, 2026:
- Current cycle: Wednesday Jan 08 - Tuesday Jan 14
- File searched: `*Weekly Cycle (Jan. 08 - Jan. 14, 2026)*`

### Task Insertion Logic

When a task is completed:

1. **Find the cycles folder**: Searches for a folder ending with `_Cycles`
2. **Locate weekly cycles**: Looks in the `_Weekly-Cycles` subfolder
3. **Match date range**: Finds the file containing the current week's date range
4. **Find day section**: Locates `### {DayName} -` (e.g., `### Friday -`)
5. **Insert task**:
   - If `##### Completed Tasks:` exists, append after existing entries
   - If not, create the header and add the task before the section separator

### Timestamp Format

Tasks are logged with timestamps: `[HH:MM AM/PM] Task content`

Example: `[14:30 PM] Review pull request #123`

## Testing

### Manual Test Script

Run the test script to verify the weekly cycle fetch works:

```bash
cd app
uv run python tests/test_get_weekly_cycle.py
```

This will:
1. Connect to Dropbox using your credentials
2. Find your `_Cycles` folder
3. Locate the current week's cycle file
4. Print the file contents

### Expected Output

```
2026-01-07 10:00:00 - INFO - Using timezone: US/Eastern
2026-01-07 10:00:00 - INFO - Found weekly cycles folder: /your_vault/*_cycles/_weekly-cycles
2026-01-07 10:00:00 - INFO - Looking for weekly cycle with date range: (Jan. 08 - Jan. 14, 2026)
2026-01-07 10:00:00 - INFO - Cycle period: Wednesday, Jan 08 to Tuesday, Jan 14, 2026
2026-01-07 10:00:00 - INFO - Found weekly cycle file: Weekly Cycle (Jan. 08 - Jan. 14, 2026).md

==================================================
THIS WEEK'S CYCLE
==================================================

[File contents displayed here]
```

## Troubleshooting

### "Could not find '_Cycles' folder"

- Ensure your Obsidian vault has a folder ending with `_Cycles`
- Check `DROPBOX_OBSIDIAN_VAULT_PATH` is correct
- Verify Dropbox credentials have access to the vault

### "Could not find weekly cycle file for date range"

- Verify the file naming matches the expected format: `(Mon. DD - Mon. DD, YYYY)`
- Check the file exists for the current week (Wednesday-Tuesday)
- Note: The system looks for the date range string anywhere in the filename

### "Could not find day section"

- Ensure day headers follow format: `### Wednesday -` (day name, space, dash)
- The day name must match exactly (e.g., `Wednesday`, not `Wed`)

### Token Refresh Issues

The system automatically refreshes Dropbox tokens using Redis caching. If you see token errors:

```bash
# Check Redis is running
docker compose logs redis

# Clear cached token to force refresh
redis-cli DEL DROPBOX_ACCESS_TOKEN
```

### Timezone Issues

If tasks appear on the wrong day:

1. Check `SYSTEM_TIMEZONE` in `.env`
2. Common values: `US/Eastern`, `US/Pacific`, `UTC`
3. Restart the app after changing:
   ```bash
   docker compose up -d --force-recreate app
   ```

## Integration with Todoist

When configured with the Todoist webhook, the flow is:

1. Complete a task in Todoist
2. Webhook sends `item:completed` event to your API
3. API extracts task content
4. Task is written to the Weekly Cycle note under today's section

See [Todoist Webhook Setup](./todoist-webhook-setup.md) for webhook configuration.

## Code Location

- Main sync logic: `app/services/obsidian/add_weekly_cycle_completed.py`
- Test script: `app/tests/test_get_weekly_cycle.py`
- API endpoint integration: `app/main.py`

# Linear to Obsidian Sync

This guide covers the scripts that sync Linear Initiatives, Projects, and related data to your Obsidian vault via Dropbox.

## Overview

The sync system creates a structured representation of your Linear workspace in Obsidian:

```
_Initiatives/
├── 00_Active/
│   └── {Initiative Name}/
│       ├── (Initiative) - {Initiative Name}.md
│       ├── _Docs/
│       │   └── {Doc Title} - ({Initiative Name}).md
│       └── _Projects/
│           └── {Project Name}/
│               ├── (Project) {Project Name}.md
│               └── _Docs/
│                   └── {Doc Title} - ({Project Name}).md
├── 01_Planned/
├── 02_Completed/
└── 03_Archived/
```

## Scripts

### Full Sync: `sync_initiatives_to_obsidian.py`

Syncs all initiatives and their related objects from Linear to Obsidian.

```bash
# Basic sync
python -m app.scripts.linear.sync_initiatives_to_obsidian

# Include archived initiatives from Linear
python -m app.scripts.linear.sync_initiatives_to_obsidian --include-archived

# Enable debug logging
python -m app.scripts.linear.sync_initiatives_to_obsidian --debug
```

### Single Sync: `sync_single_initiative.py`

Syncs a single initiative (useful for webhook-triggered updates).

```bash
# Sync by initiative ID
python -m app.scripts.linear.sync_single_initiative --initiative-id <id>

# Sync by project ID (syncs the parent initiative)
python -m app.scripts.linear.sync_single_initiative --project-id <id>
```

## What Gets Synced

| Linear Entity | Obsidian Location | File Name |
|--------------|-------------------|-----------|
| Initiative | `_Initiatives/{status}/{name}/` | `(Initiative) - {name}.md` |
| Initiative Document | `_Initiatives/{status}/{name}/_Docs/` | `{title} - ({initiative}).md` |
| Project | `_Initiatives/{status}/{initiative}/_Projects/{name}/` | `(Project) {name}.md` |
| Project Document | `.../_Projects/{name}/_Docs/` | `{title} - ({project}).md` |

### Initiative Files Include

- YAML frontmatter (id, name, url, status, health, dates, owner)
- Description and content from Linear
- `### Related Linear Documents:` - Obsidian wikilinks to docs
- `### Updates:` - Status reports sorted by date (newest first)
- `### Related Projects:` - Obsidian wikilinks to projects

### Project Files Include

- YAML frontmatter (id, name, url, state, health, progress, dates, lead)
- Description and content from Linear
- `### Related Linear Documents:` - Obsidian wikilinks to docs
- `### Updates:` - Status reports sorted by date (newest first)
- `### Related Issues:` - Issues grouped by state

## Status Folder Mapping

| Linear Status | Obsidian Folder |
|--------------|-----------------|
| Active | `00_Active/` |
| Planned | `01_Planned/` |
| Completed | `02_Completed/` |
| (manual) | `03_Archived/` |

When an initiative's status changes in Linear, the sync will automatically move its folder to the correct status directory.

## Manual Archival

If you manually move an initiative folder to `03_Archived/`, the sync will skip that initiative and not overwrite or move it. This allows you to archive initiatives in Obsidian without affecting Linear.

## User Content Preservation

You can add your own notes to initiative and project files. The sync preserves any content you write between the YAML frontmatter and the first `###` heading.

Example:
```markdown
---
id: abc123
name: My Initiative
...
---

My personal notes here will be preserved across syncs.
I can write anything I want in this section.

### Related Linear Documents:
(this section and below will be replaced on each sync)
```

## Webhook Integration

The sync integrates with Linear webhooks. When a `ProjectUpdate` or `InitiativeUpdate` event is received:

1. **ProjectUpdate**: Syncs the parent initiative (including all its projects)
2. **InitiativeUpdate**: Syncs that initiative

This happens automatically via the webhook handler in `app/main.py`.

## Environment Variables

Required in `.env`:

```bash
# Linear API
LINEAR_API_KEY=lin_api_...

# Dropbox OAuth
DROPBOX_ACCESS_KEY=...
DROPBOX_ACCESS_SECRET=...
DROPBOX_REFRESH_TOKEN=...
DROPBOX_OBSIDIAN_VAULT_PATH=/obsidian/personal

# Workspace name (folder under _Workspaces)
OBSIDIAN_LINEAR_WORKSPACE_NAME=_Chapters-Technology

# Redis (for Dropbox token caching)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=...
```

## File Structure

```
app/scripts/linear/
├── sync_utils.py                    # Shared utilities
├── sync_initiatives_to_obsidian.py  # Full sync script
└── sync_single_initiative.py        # Single initiative sync
```

### `sync_utils.py`

Contains all shared functionality:
- Linear API functions (GraphQL queries, pagination)
- Dropbox utilities (auth, file operations)
- Markdown generation (YAML frontmatter, sections)
- Content preservation (merge user notes)
- Sync operations (initiative, project, document)

## Troubleshooting

### "Could not find _Initiatives folder"

Ensure your Obsidian vault has the expected structure:
```
{vault}/_Workspaces/{WORKSPACE_NAME}/_Initiatives/
```

Run `create_base_workspace_directory.py` first if needed.

### "LINEAR_API_KEY not set"

Add your Linear API key to `.env`. Get one from Linear Settings > API.

### Webhook not triggering sync

1. Check webhook is configured in Linear (Settings > Webhooks)
2. Verify the server is receiving webhooks (check logs)
3. Ensure `ProjectUpdate` and `InitiativeUpdate` events are enabled

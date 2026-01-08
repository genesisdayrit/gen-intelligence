# Linear Initiative & Project Updates to Weekly Cycle

## Goal
Route Linear Initiative Updates and Project Updates **directly to Obsidian** (Weekly Cycle file), bypassing Todoist. This is different from the existing Linear Issue completion flow which routes through Todoist first.

**Flow comparison:**
- Existing: Linear Issue completed → Todoist task created/completed → Todoist webhook → Obsidian
- New: Linear Initiative/Project Update → **Direct to Obsidian** (no Todoist involvement)

## User Decisions

- **Section Order**: Initiative Updates → Project Updates → Completed Tasks
- **Actions**: Both `create` (append new) and `update` (edit existing entry in place)
- **Entry Format**: `[HH:MM AM/PM] [link](url) Parent Name: Update content`
  - Link frontloaded for pattern detection (URL serves as unique identifier)
  - No truncation limit
- **Update Detection**: Use the Linear URL in the entry to find and update existing entries

## Weekly Cycle Day Section Structure
```markdown
### Wednesday -

##### Initiative Updates:
[03:15 PM] [link](https://linear.app/.../projectUpdate/abc123) Initiative Name: Update content here

##### Project Updates:
[04:00 PM] [link](https://linear.app/.../projectUpdate/def456) Project Name: Update content here

##### Completed Tasks:
[10:30 AM] Task content

---
```

## Implementation Approach

### Entry Format Pattern
```
[HH:MM AM/PM] [link](URL) Parent Name: Content body
```
Timestamp always reflects the last modified time (updates overwrite with new timestamp).

### Update Logic (for `action: "update"`)
1. Search current day's section for a line containing the update's URL
2. If found → replace entire line with new content and new timestamp
3. If not found → append as new entry

### Files to Modify

1. **`app/main.py`** (lines 244-314)
   - Add handlers for `ProjectUpdate` and `Initiative` (or `InitiativeUpdate`) webhook types
   - Extract: parent name (initiative/project name), update body/content, URL
   - Determine action (create vs update) and call service function

2. **`app/services/obsidian/add_weekly_cycle_updates.py`** (NEW FILE)
   - Generic function: `upsert_weekly_cycle_update(section_type, url, parent_name, content)`
   - `section_type`: "initiative" or "project" → determines header
   - Handles both insert (new) and update (edit existing) logic
   - Section ordering: Initiative Updates → Project Updates → Completed Tasks
   - Reuses Dropbox client pattern from `add_weekly_cycle_completed.py`

3. **`docs/guides/linear-webhook-setup.md`**
   - Document enabling ProjectUpdate and Initiative webhook types in Linear
   - Update payload examples for new event types
   - Add troubleshooting section for updates

## Implementation Steps

### Step 0: Create test script to capture webhook payloads
- Add a temporary endpoint or logging to capture raw ProjectUpdate and Initiative webhook payloads
- Trigger test updates in Linear to see exact field structure
- Use this to confirm field names before full implementation

### Remaining Steps
1. Create `app/services/obsidian/add_weekly_cycle_updates.py`
2. Update `app/main.py` with handlers
3. Update documentation

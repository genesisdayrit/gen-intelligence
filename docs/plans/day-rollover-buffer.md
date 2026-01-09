# Day Rollover Buffer

## Overview

Tasks completed between midnight and 3am are treated as belonging to the previous day. This accounts for late-night work sessions that logically belong to "yesterday" even though the calendar has rolled over.

## Current Implementation

Implemented in `app/services/obsidian/add_todoist_completed.py`:
- Uses `DAY_ROLLOVER_HOUR = 3` constant
- `_get_effective_date()` helper subtracts 1 day if current hour < 3

## Other Opportunities to Add This Buffer

The following files use similar date logic and could benefit from the same buffer:

### High Priority

1. **`app/services/obsidian/add_daily_action_updates.py`**
   - `_get_today_daily_action_path()` (line ~97)
   - Linear updates logged late at night should go to previous day's note

2. **`app/services/obsidian/add_telegram_log.py`**
   - `_get_today_journal_path()` (line ~81)
   - Late-night Telegram messages should log to previous day's journal

### Lower Priority

3. **`app/services/obsidian/add_weekly_cycle_completed.py`**
   - Uses weekly bounds (Wed-Tue), not daily files
   - Buffer less relevant but could be considered

4. **`app/services/obsidian/add_weekly_cycle_updates.py`**
   - Same as above - weekly cycle logic

## Consideration: Reusable Function

Currently the `_get_effective_date()` helper is defined in `add_todoist_completed.py`. If applying this buffer to multiple files, consider:

1. **Create a shared utility module** (e.g., `app/services/obsidian/utils/date_helpers.py`):
   ```python
   from datetime import datetime, timedelta

   DAY_ROLLOVER_HOUR = 3

   def get_effective_date(now: datetime) -> datetime:
       """Get the effective date, treating midnight-3am as the previous day."""
       if now.hour < DAY_ROLLOVER_HOUR:
           return now - timedelta(days=1)
       return now
   ```

2. **Import and use across all Obsidian services** that need day-boundary logic

3. **Benefits**:
   - Single source of truth for buffer logic
   - Easy to adjust buffer hours in one place
   - Consistent behavior across all services

4. **Trade-off**: Adds indirection for a simple function. May not be worth it if only 2-3 files use it. Evaluate after implementing in the high-priority files.

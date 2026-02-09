# Config Module Migration Guide

## Background

`app/config.py` provides a single source of truth for timezone and Redis configuration. Currently, each file reads `SYSTEM_TIMEZONE` and Redis env vars independently with its own defaults, leading to inconsistency:

| Default | Count | Used in |
|---------|-------|---------|
| `US/Eastern` | 16 files | main.py, obsidian services, most scripts/tests |
| `US/Pacific` | 6 files | cycle email, headlines, linear sync |
| `America/Los_Angeles` | 1 file | scheduler.py |

Redis boilerplate (4 identical lines) is copy-pasted across 15 files.

## How to migrate a file

Replace the boilerplate:

```python
# DELETE these lines:
import redis
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")

# ADD this import:
from config import redis_client, SYSTEM_TZ
```

- Files that only need timezone: `from config import SYSTEM_TZ`
- Files that only need Redis: `from config import redis_client`
- Files that need the timezone string (not pytz object): `from config import SYSTEM_TIMEZONE_STR`

Also remove unused imports (`redis`, `pytz`, etc.) after migrating.

## Files to migrate

### Services — timezone + Redis (9 files)

- [ ] `services/obsidian/add_telegram_log.py` — redis block (L18-21) + timezone_str (L24)
- [ ] `services/obsidian/add_shared_link.py` — redis block (L22-25) + timezone_str (L28)
- [ ] `services/obsidian/add_daily_action_updates.py` — redis block (L18-21) + timezone_str (L24)
- [ ] `services/obsidian/add_weekly_cycle_updates.py` — redis block (L18-21) + timezone_str (L24)
- [ ] `services/obsidian/add_weekly_cycle_completed.py` — redis block (L19-22) + timezone_str (L25)
- [ ] `services/obsidian/add_todoist_completed.py` — redis block (L19-22) + timezone_str (L25)
- [ ] `services/obsidian/update_telegram_log.py` — redis block (L15-18) + timezone_str (L21)
- [ ] `services/obsidian/remove_todoist_completed.py` — redis block (L16-19) + timezone_str (L22)
- [ ] `services/obsidian/add_youtube_link.py` — timezone_str only (L218)

### Scripts — timezone and/or Redis (8 files)

- [ ] `scripts/send_cycle_summary_email.py` — inline pytz.timezone(os.getenv(...)) (L444)
- [ ] `scripts/generate_cycle_summary_data.py` — inline pytz.timezone(os.getenv(...)) (L478)
- [ ] `scripts/generate_latest_headlines.py` — inline pytz.timezone(os.getenv(...)) (L569)
- [ ] `scripts/todoist/backfill_completions.py` — timezone_str (L87)
- [ ] `scripts/manus/append_weekly_tasks_to_obsidian.py` — redis block (L40-43) + timezone_str (L262)
- [ ] `scripts/linear/sync_today_completed_to_todoist.py` — timezone_str (L99, L153)
- [ ] `scripts/linear/sync_utils.py` — redis block (L34-37)
- [ ] `scripts/linear/create_base_workspace_directory.py` — redis block (L19-22)

### Core app (2 files)

- [ ] `main.py` — `SYSTEM_TIMEZONE = os.getenv(...)` (L47)
- [ ] `scheduler.py` — `os.getenv("SYSTEM_TIMEZONE", ...)` in CronTrigger (L46)

### Tests (7 files)

- [ ] `tests/test_telegram_logs.py` — redis block (L26-27) + timezone_str (L22)
- [ ] `tests/test_todoist_completed.py` — redis block (L28-29) + timezone_str (L24)
- [ ] `tests/test_todoist_cycle_completions.py` — timezone_str (L227)
- [ ] `tests/test_get_today_journal.py` — redis block (L25-26) + timezone_str (L21)
- [ ] `tests/test_get_today_daily_action.py` — redis block (L25-26) + timezone_str (L21)
- [ ] `tests/test_get_weekly_cycle.py` — redis block (L25-26) + timezone_str (L21)
- [ ] `tests/test_dropbox_redis.py` — redis block (L15-16)

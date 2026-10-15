# Daily Journal, Action, and Properties Creation

Evening-before jobs that create tomorrow's Obsidian Daily Journal and Daily Action notes, then fill the journal's YAML properties — plus a morning catch-up that creates **today's** files if last night's run was skipped. Migrated from `gd-second-brain-os` crontab onto APScheduler.

## Overview

Three staggered jobs run every evening in `SYSTEM_TIMEZONE` (default `America/Los_Angeles`):

1. **18:00** `create_daily_journal` — copy `_Templates/daily-templates/daily_note_properties.md` into `_Daily/_Journal/{Mon D, YYYY}.md`
2. **18:05** `create_daily_action` — create `DA YYYY-MM-DD.md` with YAML links to journal, weekly cycle, long cycle, and weekly map
3. **18:10** `update_daily_journal_properties` — rewrite tomorrow's journal frontmatter with those same relationship links, plus Day of Week, Date, Daily Action, On this Day, Previous Day, and Next Day

A fourth job runs every morning:

4. **05:00** `ensure_todays_daily_files` — if last night's 18:00 cluster never ran (hub deploy/restart past APScheduler `misfire_grace_time`, which is 1 hour), create **today's** journal, daily action, and journal properties. Calls the same helpers with `use_today=True`, in journal → action → properties order. Does not remove the evening-before jobs.

Default evening mode creates **tomorrow's** files, so they exist before midnight. The morning job creates **today's**. All three helpers skip work if the target file already exists (journal/action) or if the journal is missing (properties). Safe to re-run.

`update_daily_journal_properties` writes adjacent-day journal wikilinks relative to the target day (tomorrow by default, or today with `--today` / `use_today=true`):

```yaml
Previous Day:
- '[[Sep 16, 2026]]'
Next Day:
- '[[Sep 18, 2026]]'
```

Journals created after the Hub port but before this restore may still have `Previous Day: null` / `Next Day: null`. Re-running the nightly job only fills the next target day. Historical nulls need a one-shot backfill — the reference script is `gd-second-brain-os/dropbox-api/tests/backfill_adjacent_day_properties.py` (date-range, `--only-missing`, `--dry-run`). After deploy, ask Gen Intelligence to run that (or a Hub port) for the affected range.

This is the DST-aware equivalent of the old UTC crontab (`01:00` / `01:05` / `01:10` UTC, commented as 9:00pm ET). 6:00pm Pacific is 9:00pm Eastern year-round.

## Prerequisites

- Dropbox configured with Obsidian vault access
- Redis running for Dropbox access-token caching
- Vault folders ending in `_Daily`, `_Templates`, `_Daily-Action`

## Environment Variables

```bash
DROPBOX_ACCESS_KEY=your_app_key
DROPBOX_ACCESS_SECRET=your_app_secret
DROPBOX_REFRESH_TOKEN=your_refresh_token
DROPBOX_OBSIDIAN_VAULT_PATH=/Your_Vault
SYSTEM_TIMEZONE=America/Los_Angeles
REDIS_HOST=localhost
REDIS_PORT=6379
```

## Manual Trigger

```bash
# Tomorrow's files (same as the scheduled run)
curl -X POST http://localhost:8000/scheduler/jobs/create_daily_journal/run
curl -X POST http://localhost:8000/scheduler/jobs/create_daily_action/run
curl -X POST http://localhost:8000/scheduler/jobs/update_daily_journal_properties/run

# Morning catch-up (same as the 05:00 scheduled job): today's files
curl -X POST http://localhost:8000/scheduler/jobs/ensure_todays_daily_files/run

# Manual morning recovery via the evening job ids (use_today=true)
curl -X POST 'http://localhost:8000/scheduler/jobs/create_daily_journal/run?use_today=true'
curl -X POST 'http://localhost:8000/scheduler/jobs/create_daily_action/run?use_today=true'
curl -X POST 'http://localhost:8000/scheduler/jobs/update_daily_journal_properties/run?use_today=true'

# Check next run times
curl http://localhost:8000/scheduler/jobs
```

CLI equivalents (from `app/`):

```bash
python -m scripts.obsidian.workflows.file_creation.create_daily_journal
python -m scripts.obsidian.workflows.file_creation.create_daily_journal --today
python -m scripts.obsidian.workflows.file_creation.create_daily_action --today
python -m scripts.obsidian.workflows.file_updates.update_daily_journal_properties --today
```

## Troubleshooting

### Job not appearing in scheduler

Restart the app so `start_scheduler()` re-registers jobs from `SCHEDULED_JOBS`.

### Today's journal is missing in the morning

The 18:00 cluster creates **tomorrow's** files. If it misses (for example a hub deploy after 18:00 PT, past the 1-hour `misfire_grace_time`), `next_run` jumps to the following evening and today's journal is never created.

`ensure_todays_daily_files` at **05:00** Pacific is the automatic catch-up: it calls the same helpers with `use_today=True`. If that job also has not run yet, trigger it (or the evening ids with `use_today=true`, same as the old `run_daily_creation_jobs.sh --today`):

```bash
curl -X POST http://localhost:8000/scheduler/jobs/ensure_todays_daily_files/run
```

### Properties job skipped

`update_daily_journal_properties` no-ops if the target journal is missing. Run journal creation first, wait for Dropbox to settle, then run properties.

## Related

- Scheduler entry: `app/scheduler.py`
- Scripts: `app/scripts/obsidian/workflows/file_creation/` and `file_updates/`
- Remaining migrated crontab jobs (weekly pages, folder-journal relations; `daily_prep` and `daily_reflection` are intentionally not scheduled): [obsidian-scheduled-jobs.md](obsidian-scheduled-jobs.md)

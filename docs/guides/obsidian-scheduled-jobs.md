# Obsidian Scheduled Jobs (APScheduler)

`gd-second-brain-os` crontab jobs whose scripts already live in this repo are registered on APScheduler in `app/scheduler.py`. Hours are fixed in `SYSTEM_TIMEZONE` (default `America/Los_Angeles`) so they stay DST-stable, matching the evening-before daily-creation cluster.

List jobs and fire any of them immediately:

```bash
curl http://localhost:8000/scheduler/jobs
curl -X POST http://localhost:8000/scheduler/jobs/add_daily_review_section/run
```

## Already migrated (do not re-add)

| Job id | Local schedule | Notes |
|---|---|---|
| `create_daily_journal` | Daily 18:00 | Tomorrow's journal. See [daily-journal-creation.md](daily-journal-creation.md). |
| `create_daily_action` | Daily 18:05 | Tomorrow's DA file. |
| `update_daily_journal_properties` | Daily 18:10 | Tomorrow's journal YAML (relationship links, Daily Action, On this Day, Previous/Next Day). |
| `send_essay_ideas_from_journal` | Daily 04:30 | Already on APScheduler; remapped from old `25 1 * * *` UTC. |

Daily creation jobs accept `?use_today=true` for morning recovery. Other jobs ignore that flag.

## Migrated in this follow-up

| Job id | Local (`SYSTEM_TZ`) | Callable | What it does |
|---|---|---|---|
| `add_daily_review_section` | Daily 13:00 | `scripts.obsidian.workflows.file_updates.add_daily_review_section.add_daily_review_section` | Inserts a Daily Review section into today's DA file if missing. **This port is not a no-op** (the original second-brain copy may have been). |
| `update_modified_files_today` | Every 15 minutes | `scripts.obsidian.workflows.file_updates.update_modified_files_today.update_modified_files_today` | Folder-journal relations: set `Journal:` YAML on files modified since last run. |
| `create_weeks` | Sun 23:00 | `scripts.obsidian.workflows.file_creation.create_weeks.create_weeks` | Next Week-Ending Sunday page (skips if it exists). |
| `create_newsletter_page` | Thu 23:30 | `scripts.obsidian.workflows.file_creation.create_newsletter_page.create_newsletter_page` | Newsletter for the Sunday after next. |
| `create_new_cycle_page` | Tue 01:30 | `scripts.obsidian.workflows.file_creation.create_new_cycle_page.create_new_cycle_page` | Next Wed–Tue weekly cycle page. |
| `create_weekly_health_review_page` | Tue 02:00 | `scripts.obsidian.workflows.file_creation.create_weekly_health_review_page.create_weekly_health_review_page` | Next Wed–Tue health review page. |
| `create_weekly_map` | Wed 23:00 | `scripts.obsidian.workflows.file_creation.create_weekly_map.create_weekly_map` | Weekly map for the Sunday after next. |
| `create_cycle_and_cooling_period_pages` | Sat 04:00 | `scripts.obsidian.workflows.file_creation.create_cycle_and_cooling_period_pages.create_cycle_and_cooling_period_pages` | 6-week cycle + 2-week cooling files. Never in `crontab_generation.py`; live host still ran it. |

## Intentionally omitted

| Job id | Why it is not scheduled |
|---|---|
| `daily_prep` | AM Vision check-in. Script still lives at `scripts.obsidian.workflows.daily_prep` and can be re-registered later. Turned off after `daily_reflection` so neither Vision check-in runs on a cadence. |
| `daily_reflection` | PM Vision check-in. Script still lives at `scripts.obsidian.workflows.daily_reflection` and can be re-registered later. Turned off because the old crontab was failing to find files and the current setup does not want it on a cadence. |

## Schedule choices

- **Fixed local hours**, not a UTC-cron translation. Fire times stay aligned with Pacific (and Eastern, which stays 3 hours ahead year-round) and drift ±1h vs the old UTC crontab across DST.
- **`add_daily_review_section` 13:00** matches the generation comment of 3:00pm ET (EST). The original UTC line was `0 20 * * *`.
- **`update_modified_files_today` every 15 minutes** instead of the live-host `*/10`. `paths_to_check.txt` lists 13 Dropbox folders (non-recursive `list_folder`). Redis `last_run_folder_journal_relations_at` keeps each pass incremental. 15 minutes is less chatty than every 10 minutes and still near-real-time for `Journal:` links. Generation used a once-daily `5 0 * * *` UTC that did not match its "12:05am Eastern" comment.
- **Weekly page jobs** use the PR #198 suggested local times (Sun 23:00, Thu 23:30, Tue 01:30, Tue 02:00, Wed 23:00).
- **`create_cycle_and_cooling_period_pages` Sat 04:00** is the PDT equivalent of the live-host `0 11 * * 6` UTC (Sat 11:00 UTC = Sat 04:00 PDT / Sat 03:00 PST, described as Friday-evening-ish). Included because the live host still runs it, even though generation never emitted that line.

## Still crontab-only (do not migrate)

These remain on the personal-ec2 crontab. Scripts either do not exist in this repo or were intentionally skipped in PR #198:

| Crontab entry | Why it stays on crontab |
|---|---|
| `refresh_token_to_redis` (Dropbox) | Each Dropbox script already refreshes when Redis is empty. |
| `sync_yt_and_knowledge_hub` | Old Notion path; replaced by Readwise / Knowledge Hub buffet. |
| Gmail `refresh_redis_token` | This repo sends mail with an app password, not that OAuth token. |
| `daily_writing_randomizer` | No script in this repo. |
| `weekly_map_prayer` | No script in this repo. |

## Safe to remove from personal-ec2 crontab after deploy

After this app is deployed and `GET /scheduler/jobs` shows the new ids with a `next_run_time`, delete the matching crontab lines so they do not double-fire.

Checklist:

- [ ] `daily_prep` crontab only (live `30 14 * * *`; generation `30 17 * * *`) — not replaced on APScheduler; delete so the old line does not keep running
- [ ] `daily_reflection` crontab only (live `30 0 * * *`; generation `30 3 * * *`) — not replaced on APScheduler; delete so the old failing line does not keep running
- [ ] `add_daily_review_section` (`0 20 * * *`)
- [ ] `update_modified_files_today` / folder-journal relations (live `*/10 * * * *`; generation `5 0 * * *`)
- [ ] `create_weeks` (`0 6 * * 1`)
- [ ] `create_newsletter_page` (`30 6 * * 5`)
- [ ] `create_new_cycle_page` (`30 8 * * 2`)
- [ ] `create_weekly_health_review_page` (`0 9 * * 2`)
- [ ] `create_weekly_map` (`0 6 * * 4`)
- [ ] `create_cycle_and_cooling_period_pages` (live `0 11 * * 6`; not in generation)
- [ ] Daily creation cluster if not already removed: `create_daily_journal` / `create_daily_action` / `update_daily_journal_properties` (`01:00` / `01:05` / `01:10` UTC) and `essay_ideas_from_journal` (`25 1 * * *`)

Leave the "still crontab-only" rows above on the host.

## Prerequisites

Same as daily journal creation: Dropbox vault access, Redis for the access-token cache, `SYSTEM_TIMEZONE`, plus `GMAIL_ACCOUNT` / `GMAIL_PASSWORD` and OpenAI credentials if `daily_prep` or `daily_reflection` is ever re-enabled.

## Related

- Scheduler: `app/scheduler.py`
- Scripts: `app/scripts/obsidian/workflows/`
- [Daily journal creation](daily-journal-creation.md)
- [PR #198](https://github.com/genesisdayrit/gen-intelligence/pull/198)

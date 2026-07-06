# Main-Thread Rollup Guide

Reference for AI agents and humans to understand the **main-thread rollup loop** — a scheduled job that keeps a single, always-current, high-level overview of everything happening across your other active Linear initiatives.

## The idea

You run many active initiatives at once (consulting engagements, build efforts, personal systems). Each has its own status updates and scratch-pad documents where the real, context-specific work lives. That detail is valuable but scattered — there's no single place to glance at "what's the shape of everything right now."

The **main thread** is that single place: one Linear initiative, tagged with the `main-thread` label, that receives a periodic *rollup* update synthesizing recent activity from all the **other** active initiatives. You keep the high-level overview in the main thread while the context-specific stuff stays where it belongs.

```
Other active initiatives (sources)          Main-thread initiative (target)
──────────────────────────────────          ───────────────────────────────
Everlywell Consulting                         (LC12) Main Thread
  ├─ status updates ─────┐                     label: main-thread
  ├─ documents (today) ──┤                          ▲
HMW Build Personal Loops │   ── LLM synth ──►  new initiative update
  ├─ status updates ─────┤                     ## Summary
  └─ documents (today) ──┘                     ## By initiative
```

---

## The loop, end to end

Every 6 hours the job (`send_main_thread_rollup.py`) does the following:

1. **Resolve the target.** Fetch active initiatives (with labels) and find the single one carrying the `main-thread` label. Abort if zero or more than one match.
2. **Pick the sources.** Every *other* active initiative (`status == "Active"`, not archived, does not carry `main-thread`).
3. **Gather recent activity** in the last N hours (default 6) for each source, at both the initiative and project level:
   - **Status updates** → contribute their **full body**.
   - **Documents** → contribute **only today's dated section** (see below). Documents with no section dated today are skipped.
4. **Synthesize.** Hand the assembled context to an LLM, which writes a concise `## Summary` + `## By initiative` markdown update.
5. **Post** it as a new initiative update on the main-thread initiative. If no source had any activity in the window, it skips posting.

---

## Recency window

An item counts as "recent" if `max(createdAt, updatedAt) >= now - hours`. Both freshly-created and re-edited items are caught. Timestamps from Linear are UTC ISO-8601 (`2026-07-06T18:24:34.353Z`); they're parsed and compared against `now - hours` in `SYSTEM_TZ`.

The scheduled job uses a **6-hour look-back to match its 6-hour cadence**, so the windows tile contiguously — no gaps, no double-counting. If you change the schedule, change the look-back to match (see `_send_main_thread_rollup` in `scheduler.py`).

---

## Document "today section" parsing

The scratch-pad documents follow a **date-header convention**, newest day at the top (reverse-chronological). Rather than dumping the whole document, the rollup extracts only **today's** section — the block from today's date header down to the next date header (the "following date" break point).

### Header format

A date-header line is: optional leading `#`s, a month name (abbreviated or full), a day number with an optional ordinal suffix, and nothing else on the line. Both of these match:

```
# Jul 4th
Jul 6th
```

The regex (`_DATE_HEADER_RE` in `send_main_thread_rollup.py`):

```
^\s*#*\s*(Jan|Feb|…|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*$
```

Separators *inside* a day (em-dashes like `—`, `—-`) are **not** headers and are correctly left in place.

### Extraction

```python
extract_today_section(content, today=(month, day))
```

- Finds every date-header line and its `(month, day)`.
- Locates the header matching today.
- Returns the lines from that header up to (but not including) the next date header.
- Returns `None` if there is no section dated today → the document is skipped.

### Worked example

Given a document `content` of:

```
Jul 6th

Hey Rhea, reviewing the immutable backup setup...
* setup GitLab
—
Postgres Upgrades

# Jul 4th
older notes...
```

`extract_today_section(content, (7, 6))` returns everything from `Jul 6th` down to just before `# Jul 4th`. If today were Jul 5th, it would return `None` (no Jul 5th header present).

> **Caveat — no year in headers.** Headers carry only month + day, so the parser assumes the current year. This is only ambiguous across a Jan 1 boundary (a stale "Dec 31st" header the day after New Year's would not be treated as today, which is the desired behavior anyway).

---

## LLM synthesis

- **Model:** `gpt-4o-mini` (OpenAI), matching `send_daily_initiative_update.py`. Requires `OPENAI_API_KEY`.
- **Prompt intent:** produce a compact overview that stays high-level but keeps concrete specifics (names, systems, decisions, numbers). Synthesize and de-duplicate — merge a status update and a document section that cover the same thing.
- **Output shape:**

```markdown
## Summary
One or two sentences on the overall shape of the last few hours.

## By initiative

### <Initiative name>
- <tight bullet>
- **<Project name>**
  - <project-level bullet>
```

The system/user prompts live in `SUMMARY_SYSTEM_PROMPT` / `SUMMARY_USER_PROMPT_TEMPLATE`. The raw context handed to the model is built by `build_activity_block()` — plain text, one block per initiative, status-update bodies and document today-sections indented under each.

---

## Schedule

Wired into `app/scheduler.py` (APScheduler `BackgroundScheduler`, runs inside the FastAPI process):

- **Job id:** `send_main_thread_rollup`
- **Times:** 05:30, 11:30, 17:30, 23:30 **America/Los_Angeles** (`CronTrigger(hour="5,11,17,23", minute=30)`)
- **Look-back:** 6 hours (set in the `_send_main_thread_rollup` wrapper)
- **Misfire grace:** 1 hour (shared scheduler default) — if the server is down at fire time, it still runs once it comes back within the hour.

The job only starts firing after the app process is **deployed/restarted**, since the scheduler registers jobs on FastAPI startup.

Trigger it manually via the scheduler API:

```bash
curl -X POST http://localhost:8000/scheduler/jobs/send_main_thread_rollup/run
```

---

## Running it directly (CLI)

```bash
cd app

# Post the rollup for the last 6h (default 4h look-back when run ad hoc)
python -m scripts.send_main_thread_rollup

# Preview without posting
python -m scripts.send_main_thread_rollup --dry-run

# Custom window
python -m scripts.send_main_thread_rollup --hours 6 --dry-run

# Save the generated body to a file
python -m scripts.send_main_thread_rollup --output rollup.md

# Verbose logging
python -m scripts.send_main_thread_rollup --debug
```

> **Note:** the CLI default look-back is 4 hours (`DEFAULT_WINDOW_HOURS`); the *scheduled* job overrides this to 6. It **posts by default** — use `--dry-run` to preview.

### Probe scripts (read-only)

Used to design the parser; handy for debugging:

```bash
# Which active initiative carries the main-thread label?
python -m scripts.linear.test_find_main_thread_initiative

# What activity is in the window? (--json includes full doc content + update bodies)
python -m scripts.linear.test_recent_initiative_activity --hours 6 --json
```

---

## Behavior notes & caveats

- **No idempotency guard.** Every fire posts if there is *any* activity in the window (empty windows are skipped). At the 6h cadence that's the intent, but re-running manually will create duplicate updates. If you need dedup, add an "already posted within window" check modeled on `_already_posted_today` in `send_daily_initiative_update.py`.
- **Exactly one `main-thread` initiative.** The job aborts loudly if zero or multiple active initiatives carry the label. To move the main thread, re-label — don't create a second.
- **Sources are `status == "Active"` only.** Planned/Completed initiatives are ignored.
- **Documents need a today section.** A document edited in the window but whose latest content sits under yesterday's header contributes nothing. This is deliberate ("just the today section").

---

## Environment variables

```bash
LINEAR_API_KEY=your_linear_api_key      # read initiatives/projects/updates/docs + post the update
OPENAI_API_KEY=your_openai_key          # LLM synthesis
SYSTEM_TIMEZONE=America/Los_Angeles      # window + today-date resolution (defaults to America/Los_Angeles)
```

---

## Related files

| File | Purpose |
|------|---------|
| `app/scripts/send_main_thread_rollup.py` | The rollup job: gather → parse → synthesize → post |
| `app/scheduler.py` | Registers the 6-hourly `send_main_thread_rollup` job |
| `app/scripts/linear/sync_utils.py` | Linear GraphQL helpers (`fetch_*`, `create_initiative_update`) |
| `app/scripts/linear/test_find_main_thread_initiative.py` | Probe: find the main-thread initiative by label |
| `app/scripts/linear/test_recent_initiative_activity.py` | Probe: dump recent activity (JSON includes full content) |
| `app/scripts/send_daily_initiative_update.py` | Sibling job; template for the LLM + posting pattern (and idempotency) |
| `docs/linear-api.md` | Linear API reference |
| `docs/guides/cycle-context-gathering.md` | Related weekly cycle data infrastructure |

---

## Tips for future agents

1. **The `main-thread` label is the contract.** Everything keys off it — target resolution and source exclusion. Don't hardcode the initiative id; resolve by label.
2. **Keep the look-back == the cadence.** If you change the cron interval, change `hours=` in the wrapper so windows stay contiguous.
3. **The date-header parser is the fragile part.** If documents adopt a new date format (e.g. `2026-07-06` or `July 6, 2026`), extend `_DATE_HEADER_RE` and re-test with `test_recent_initiative_activity.py --json`.
4. **Deduplicate shared helpers if you extend this.** `extract_today_section`, the recency helpers, and the active-initiatives-with-labels query are currently duplicated across the rollup and the probe scripts. If this grows, promote them into `sync_utils.py`.
5. **Watch for duplicate posts** until an idempotency guard exists — especially when testing against the live workspace. Use `--dry-run`.

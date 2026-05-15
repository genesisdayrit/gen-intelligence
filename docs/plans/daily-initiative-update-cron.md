# Issue Card: Daily Initiative Update Cron

## Problem

> No automatic narrative of "what got done" exists at the initiative level; daily action notes capture raw activity but no rolling summary surfaces wins or incomplete items.

## Desired Outcome

A daily cron job (04:00 America/Los_Angeles) generates an LLM-summarized initiative update from the last three days of context and posts it as a real Linear Initiative Update on the single active initiative. Because Linear initiative updates already flow into Obsidian via the existing webhook handler, the summary lands in that day's `DA YYYY-MM-DD.md` automatically — no additional write paths required.

Success looks like:

- Each morning, a new Linear initiative update appears on the active initiative summarizing the prior three days of work.
- The update emphasizes wins (so the user feels reinforcement) and flags items that appeared incomplete (so they can be followed up).
- Over weeks, the user can scroll back through these updates to reason about how a project cycle is going.

## Summary/Context/Current State

The repo already has the building blocks: an APScheduler-based job registry in [app/scheduler.py](app/scheduler.py), a Linear GraphQL client in [app/scripts/linear/sync_utils.py](app/scripts/linear/sync_utils.py) that reads `initiativeUpdates`, a precedent for OpenAI summarization in [app/scripts/generate_latest_headlines.py](app/scripts/generate_latest_headlines.py), Dropbox-backed Obsidian helpers that locate `DA YYYY-MM-DD.md` files (e.g., [app/services/obsidian/add_todoist_completed.py:92](app/services/obsidian/add_todoist_completed.py:92)), and a webhook handler that already mirrors `InitiativeUpdate` events into the current day's daily action note ([app/main.py:457](app/main.py:457)).

What's missing:
- A **Linear mutation** to *create* an initiative update (the existing GraphQL surface only reads them).
- A **summarizer** that consumes the last 3 days of `DA *.md` plus the last 3 initiative updates and produces a structured post.
- A **scheduler entry** that wires it up at 04:00 LA.

GitHub commits and direct Todoist queries are explicitly out of scope: commits already flow into Todoist, and Todoist completions already flow into the daily action notes — the DA notes are treated as the spine.

## User Story

As **the sole user of gen-intelligence**, I want **an automatic daily initiative update posted to my active Linear initiative summarizing the last three days of activity** so that **I get continuous reinforcement of recent wins and a flagged list of incomplete items, and so the cadence of updates builds a longitudinal record I can later summarize across cycles**.

## Core Workflows

1. **Cron fires at 04:00 LA.** Scheduler invokes the new job.
2. **Resolve the active initiative.** Fetch initiatives via existing `fetch_initiatives`, select the single one whose status indicates active. Abort with a logged error if zero or more than one match.
3. **Idempotency check.** Query the active initiative's recent `initiativeUpdates`. If any was created today (LA local date), skip and log.
4. **Gather inputs.**
   - Load the last 3 `DA YYYY-MM-DD.md` files from Dropbox/Obsidian (today minus 1, 2, 3). Missing files are skipped, not fatal.
   - Pull the last 3 initiative updates on the active initiative (for continuity / dedup reference).
5. **Summarize with LLM (OpenAI, same client pattern as `generate_latest_headlines.py`).** Prompt explicitly instructs:
   - Do not repeat items already covered by the last 3 initiative updates.
   - Emphasize completed wins.
   - Surface items that look incomplete or carried-over across the 3 days as a follow-up list.
   - Output in the markdown structure below.
6. **Post via Linear mutation** to create an initiative update on the active initiative with the generated markdown body. Do not set `health` — leave default/null.
7. **Log result.** On success, log the new update's id/url; existing webhook flow takes over and mirrors it into today's `DA YYYY-MM-DD.md`.

### Output structure (first pass — to iterate on)

```markdown
## Wins
- bullet of completed/shipped work

## In-progress
- bullet of work clearly underway but not done

## Follow-ups
- items from the last 3 days that look incomplete and worth revisiting
```

## Assumptions

- Exactly one initiative is active at job run time. If multiple match, the job aborts rather than guessing.
- "Active" is determined by Linear's initiative status; precise filter (e.g., status name "Active" or a state flag) to be confirmed when implementing — see Follow Up.
- The Linear API exposes a mutation to create an initiative update. The likely name is `initiativeUpdateCreate` but this needs to be confirmed against the current Linear GraphQL schema — see Follow Up.
- Daily action notes are partial; the summarizer is expected to tolerate gaps and infer from what's present.
- 04:00 LA gives enough buffer after a typical end-of-day so yesterday's note is settled.
- Skipping the day (when an update already exists) is preferable to posting duplicates; manual reruns via the existing `run_job_now` helper at [app/scheduler.py](app/scheduler.py) are how a re-post would be forced.
- `health` is left unset on each update — user can edit in Linear if they want to assign one.

## Implementation Details

- **New script**: `app/scripts/send_daily_initiative_update.py` exposing a `run_daily_initiative_update(dry_run: bool = False)` callable plus a CLI entrypoint that mirrors `send_linear_digest_email.py` (argparse, `--dry-run`, `--output` to dump the would-be body to stdout/file without posting).
- **Linear mutation helper**: extend [app/scripts/linear/sync_utils.py](app/scripts/linear/sync_utils.py) with a `create_initiative_update(initiative_id, body, health=None)` wrapper around the create mutation, returning the new update id/url. Reuse `execute_query` and the existing auth.
- **Daily action loader**: small helper that walks back N days, reusing the Dropbox client and `_find_daily_action_folder` pattern from [app/services/obsidian/add_todoist_completed.py:92](app/services/obsidian/add_todoist_completed.py:92). Factor the folder-lookup helper if it isn't already shared.
- **Summarizer**: reuse `get_openai_client` from [app/scripts/generate_latest_headlines.py](app/scripts/generate_latest_headlines.py). One prompt, structured user message containing the three DA bodies and last 3 initiative-update bodies, system message describing the output schema above.
- **Scheduler wiring**: in [app/scheduler.py](app/scheduler.py), add `_send_daily_initiative_update` wrapper and a `SCHEDULED_JOBS` entry triggering daily at hour=4, minute=0, LA tz.
- **Dry-run**: when set, logs the rendered body and exits without calling the mutation or hitting OpenAI in any destructive way (the OpenAI call itself runs — that's the part being tested).
- **Idempotency**: check `initiativeUpdates` filtered to `createdAt >= start_of_today_LA`. If any exist, log and skip.
- **Error handling**: missing DA files → skip those individual files, proceed. Zero-or-multiple active initiatives → abort with logged error. Linear mutation failure → log and surface via the existing `_job_listener` error path.

## Data Model

Entities involved (all already present except the new write):

- **Initiative** (Linear) — read via `fetch_initiatives`; selected by active status.
- **InitiativeUpdate** (Linear) — read for context (last 3); written (new) as the cron's output. Key fields: `id`, `body`, `createdAt`, `health`, parent `initiative.id`.
- **DailyActionNote** (Obsidian/Dropbox) — file at `{daily_action_folder_path}/DA YYYY-MM-DD.md`. Read-only here.

No new persistent storage is introduced.

## Data Sources

- Linear GraphQL API (`https://api.linear.app/graphql`) using existing `LINEAR_API_KEY` env var.
- OpenAI API using existing `OPENAI_API_KEY` env var and the client pattern in [app/scripts/generate_latest_headlines.py](app/scripts/generate_latest_headlines.py).
- Dropbox API for the Obsidian vault using the existing Dropbox client in [app/scripts/linear/sync_utils.py](app/scripts/linear/sync_utils.py).
- All three inputs are pulled fresh at job run time; no caching.

## API Integrations

- **Linear**
  - Read: `initiatives` (filter for active), `initiativeUpdates` (last 3 on the active initiative, plus today's idempotency check).
  - Write: `initiativeUpdateCreate` (or whatever the current mutation name is — confirm during implementation).
- **OpenAI**
  - Single chat completion call per run, model TBD (match what `generate_latest_headlines.py` uses unless the user wants to bump it).
- **Dropbox**
  - List + download for the 3 DA files.

## System Design Diagram

```
[APScheduler 04:00 LA]
        │
        ▼
[run_daily_initiative_update()]
        │
        ├──► Linear: fetch active initiative
        ├──► Linear: fetch last 3 initiative updates (context + today's idempotency check)
        ├──► Dropbox/Obsidian: read DA notes for D-1, D-2, D-3
        ├──► OpenAI: summarize → markdown body
        ├──► Linear: initiativeUpdateCreate(initiative_id, body)
        │           │
        │           ▼
        │   [Linear webhook → existing handler in app/main.py:457]
        │           │
        │           ▼
        │   [DA YYYY-MM-DD.md (today) — auto-appended]
        ▼
   [log success / failure via _job_listener]
```

## Resources/Attachments

- Existing daily/weekly email scripts as reference: [app/scripts/send_linear_digest_email.py](app/scripts/send_linear_digest_email.py), [app/scripts/send_cycle_summary_email.py](app/scripts/send_cycle_summary_email.py).
- Existing summarization precedent: [app/scripts/generate_latest_headlines.py](app/scripts/generate_latest_headlines.py).
- Linear GraphQL client and existing queries: [app/scripts/linear/sync_utils.py](app/scripts/linear/sync_utils.py).
- Webhook handler that auto-mirrors `InitiativeUpdate` events into Obsidian: [app/main.py:457](app/main.py:457).
- Daily action file naming + folder lookup: [app/services/obsidian/add_todoist_completed.py:92](app/services/obsidian/add_todoist_completed.py:92).

## Follow Up

- Confirm the exact Linear mutation name and required input shape for creating an initiative update (likely `initiativeUpdateCreate` with `IntiativeUpdateCreateInput`-style payload). Smoke-test against the API before wiring the scheduler.
- Confirm the rule for "active initiative" (status name vs. a flag) so the selector is correct. Likely a single `status` value such as `"Active"` — verify with a one-off `fetch_initiatives` call.
- Decide the final output structure after a few real runs — current `## Wins / ## In-progress / ## Follow-ups` is a v1 starting point and explicitly meant to iterate.
- Decide whether to ever set `health` automatically based on the LLM's read of the days (out of scope for v1).
- Long-horizon goal: a separate cycle-level script that consumes the accumulating daily initiative updates and produces a cycle retrospective. Not in scope here, but the data shape produced by this job should make that easy.

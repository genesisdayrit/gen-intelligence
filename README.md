# gen-intelligence

Personal services hub that integrates Obsidian, Linear, Todoist, Telegram, GitHub, and Gmail into a single FastAPI backend for productivity automation.

## Project Structure

```
app/              # FastAPI backend (webhooks, services, scripts)
extensions/       # Chrome extension for saving links to Obsidian
docs/
  guides/         # Setup and usage guides
  plans/          # Implementation plans
```

## Getting Started

### Prerequisites

- Python 3.12+
- Redis
- [uv](https://github.com/astral-sh/uv) (Python package manager)

### Setup

```bash
# Install dependencies
uv sync

# Configure environment variables
cp .env.example .env
# Edit .env with your API keys and config

# Run the server
cd app && uv run uvicorn main:app --reload
```

For containerized deployment, use Docker Compose:

```bash
docker compose up
```

## Guides

Setup and usage guides live in [`docs/guides/`](docs/guides/):

- [Share Link Endpoint](docs/guides/share-link-endpoint.md)
- [Share YouTube Endpoint](docs/guides/share-youtube-endpoint.md)
- [iOS Shortcut: Save Link](docs/guides/ios-shortcut-save-link.md)
- [iOS Shortcut: Save YouTube](docs/guides/ios-shortcut-save-youtube.md)
- [Telegram Webhook Setup](docs/guides/todoist-webhook-setup.md)
- [Todoist Webhook Setup](docs/guides/todoist-webhook-setup.md)
- [Linear Webhook Setup](docs/guides/linear-webhook-setup.md)
- [Readwise Webhook Setup](docs/guides/readwise-webhook-setup.md) — point Readwise at `POST {WEBHOOK_BASE_URL}/readwise/webhook` with `READWISE_WEBHOOK_SECRET`
- [Granola Webhook Setup](docs/guides/granola-webhook-setup.md) — point Granola at `POST {WEBHOOK_BASE_URL}/granola/webhook` with `GRANOLA_WEBHOOK_SECRET`
- [Granola Journal Sync](docs/guides/granola-journal-sync.md) — webhook-driven `### Transcript Notes` writes; manual `sync_granola_notes` / `backfill_granola_notes` safety nets
- [GitHub Webhook Setup](docs/guides/github-webhook-setup.md)
- [Linear-Obsidian Sync](docs/guides/linear-obsidian-sync.md)
- [Manus Daily Task Fetch](docs/guides/manus-daily-task-fetch.md)
- [Weekly Cycle Sync](docs/guides/weekly-cycle-sync.md)
- [Daily Journal Creation](docs/guides/daily-journal-creation.md) — evening-before journal, daily action, and journal properties jobs
- [Ngrok Setup](docs/guides/ngrok-setup.md)
- [Server Restart](docs/guides/server-restart.md)
- [EC2 Docker Setup](docs/guides/ec2-docker-setup.md)
- [EC2 Disk Cleanup](docs/guides/ec2-disk-cleanup.md)

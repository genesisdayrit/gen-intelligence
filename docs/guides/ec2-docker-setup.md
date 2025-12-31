# EC2 Docker Setup

Deploy Gen Intelligence API on EC2 using Docker.

## Prerequisites

- EC2 instance with port 8000 open in security group
- SSH access to the instance

## 1. Install Docker

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sudo sh

# Add user to docker group
sudo usermod -aG docker $USER

# Log out and back in, then verify
docker --version
```

## 2. Install Docker Compose

```bash
sudo apt install docker-compose-plugin -y

# Verify
docker compose version
```

## 3. Clone and Configure

```bash
# Clone repo
git clone https://github.com/your-username/gen-intelligence.git
cd gen-intelligence

# Create .env from template
cp .env.example .env

# Edit with your credentials
nano .env
```

Update these values in `.env`:
- `DROPBOX_ACCESS_KEY`, `DROPBOX_ACCESS_SECRET`, `DROPBOX_REFRESH_TOKEN`
- `BOT_TOKEN`, `TG_WEBHOOK_SECRET`
- `WEBHOOK_URL` - set to `http://<your-ec2-public-ip>:8000/telegram/webhook`

## 4. Start Services

```bash
# Build and start
docker compose up -d

# Check logs
docker compose logs -f app
```

## 5. Register Telegram Webhook

```bash
# Enter the app container
docker compose exec app bash

# Set webhook
uv run python scripts/set_webhook.py set

# Verify
uv run python scripts/set_webhook.py info

# Exit container
exit
```

## 6. Verify

```bash
# Health check
curl http://localhost:8000/health

# Should return: {"status":"healthy"}
```

Send a message to your Telegram channel - it should appear in your Obsidian journal.

## Management Commands

```bash
# Stop
docker compose down

# Restart
docker compose restart

# View logs
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build
```

## Troubleshooting

**Container won't start:**
```bash
docker compose logs app
```

**Redis connection issues:**
```bash
docker compose exec redis redis-cli ping
```

**Webhook not receiving:**
- Check EC2 security group allows inbound on port 8000
- Verify `WEBHOOK_URL` in `.env` uses your public IP
- Re-run `set_webhook.py set`

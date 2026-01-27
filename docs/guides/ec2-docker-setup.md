# EC2 Docker Setup

Deploy Gen Intelligence API on EC2 using Docker.

## Prerequisites

- EC2 instance with ports 8000 and 4040 open in security group
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
- `NGROK_AUTHTOKEN` - get from https://dashboard.ngrok.com/get-started/your-authtoken
- `WEBHOOK_URL` - leave blank for now, will update after starting services

## 4. Start Services

```bash
# Build and start (includes ngrok for HTTPS)
docker compose up -d

# Check logs
docker compose logs -f app
```

## 5. Get ngrok URL and Register Webhook

ngrok provides an HTTPS tunnel required by Telegram.

```bash
# Get the ngrok HTTPS URL
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
```

Or visit `http://<your-ec2-ip>:4040` in your browser.

Update `WEBHOOK_URL` in `.env` with the ngrok URL + `/telegram/webhook`:
```
WEBHOOK_URL=https://abc123.ngrok-free.app/telegram/webhook
```

Then recreate the app container and register the webhook:
```bash
docker compose up -d --force-recreate app
docker compose exec app uv run python scripts/set_webhook.py set

# Verify
docker compose exec app uv run python scripts/set_webhook.py info
```

## 6. Verify

```bash
# Health check
curl http://localhost:8000/health

# Should return: {"status":"healthy"}
```

Send a message to your Telegram channel - it should appear in your Obsidian journal.

## Server Restart After Reboot

If your EC2 instance goes down and you need to bring services back up manually (without CI), see [Server Restart Guide](server-restart.md). That guide also covers optional systemd auto-start on boot.

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
- Check EC2 security group allows inbound on ports 8000 and 4040
- Verify ngrok is running: `docker compose logs ngrok`
- Check the ngrok URL matches `WEBHOOK_URL` in `.env`
- Recreate app after `.env` changes: `docker compose up -d --force-recreate app`
- Re-run `docker compose exec app uv run python scripts/set_webhook.py set`

**ngrok URL changed after restart:**
The free ngrok tier gives a new URL on each restart. When this happens:
1. Get new URL: `curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'`
2. Update `WEBHOOK_URL` in `.env`
3. Recreate and re-register: `docker compose up -d --force-recreate app && docker compose exec app uv run python scripts/set_webhook.py set`

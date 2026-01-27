# Server Restart After EC2 Reboot

How to bring the server back up manually after an EC2 instance reboot, without triggering CI/CD.

## Quick Start

SSH into your EC2 instance and run:

```bash
cd /path/to/gen-intelligence

# Start all services
docker compose up -d

# Wait for app to be ready
sleep 10

# Health check
curl http://localhost:8000/health
```

If health check returns `{"status":"healthy"}`, the app is running.

## Re-register Webhooks (ngrok free tier)

The free ngrok tier assigns a new URL on every restart. After bringing services back up, you need to update the webhook URL for **all services and clients**.

### 1. Get the new ngrok URL

```bash
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
```

### 2. Update `.env`

Update `WEBHOOK_BASE_URL` in `app/.env` with the new ngrok URL, then recreate the app container:

```bash
docker compose up -d --force-recreate app
```

### 3. Re-register Telegram (scripted)

```bash
docker compose exec app uv run python scripts/set_webhook.py set

# Verify
docker compose exec app uv run python scripts/set_webhook.py info
```

### 4. Update external service dashboards (manual)

These services require manually updating the webhook URL in their web UIs:

| Service | Where to Update | Webhook URL to Set |
|---------|----------------|--------------------|
| **Todoist** | [App Console](https://developer.todoist.com/appconsole.html) → Your App → Webhooks | `https://<ngrok-url>/todoist/webhook` |
| **Linear** | Workspace Settings → API → Webhooks → Edit | `https://<ngrok-url>/linear/webhook` |
| **GitHub** | [GitHub App Settings](https://github.com/settings/apps/gen-intelligence) → Webhook | `https://<ngrok-url>/github/webhook` |

See the individual setup guides for more detail:
- [Todoist Webhook Setup](todoist-webhook-setup.md)
- [Linear Webhook Setup](linear-webhook-setup.md)
- [GitHub Webhook Setup](github-webhook-setup.md)

### 5. Update iOS Shortcuts

The iOS Shortcuts for saving links and YouTube videos have the server URL hardcoded. After a URL change, update both shortcuts on your iPhone:

| Shortcut | Action to Edit | New URL |
|----------|---------------|---------|
| **Save Link** | "Get Contents of URL" step | `https://<ngrok-url>/share/link` |
| **Save YouTube** | "Get Contents of URL" step | `https://<ngrok-url>/share/youtube` |

Open each shortcut in the iOS Shortcuts app, find the "Get Contents of URL" action, and replace the old URL with the new ngrok URL.

See the individual setup guides for more detail:
- [iOS Shortcut: Save Link](ios-shortcut-save-link.md)
- [iOS Shortcut: Save YouTube](ios-shortcut-save-youtube.md)

## Verify Everything

```bash
# Check all containers are running
docker compose ps

# Check app health
curl http://localhost:8000/health

# Tail logs to confirm no errors
docker compose logs -f --tail=50
```

## Auto-Start on Boot with systemd

To have the server start automatically whenever the EC2 instance boots, create a systemd service.

### 1. Create the service file

```bash
sudo nano /etc/systemd/system/gen-intelligence.service
```

Paste the following (replace `<your-user>` and `/path/to/gen-intelligence` with your values):

```ini
[Unit]
Description=Gen Intelligence API
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=<your-user>
WorkingDirectory=/path/to/gen-intelligence
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
```

### 2. Enable and start

```bash
# Reload systemd to pick up the new service
sudo systemctl daemon-reload

# Enable auto-start on boot
sudo systemctl enable gen-intelligence

# Start the service now (or it will start on next reboot)
sudo systemctl start gen-intelligence
```

### 3. Verify

```bash
# Check service status
sudo systemctl status gen-intelligence

# Confirm containers are up
docker compose ps
```

### 4. Manage the service

```bash
# Stop the server
sudo systemctl stop gen-intelligence

# Restart the server
sudo systemctl restart gen-intelligence

# Disable auto-start
sudo systemctl disable gen-intelligence

# View service logs
journalctl -u gen-intelligence -f
```

> **Note:** Even with systemd auto-start, if you're on the free ngrok tier you'll still need to re-register all webhooks and update iOS shortcuts after each reboot since ngrok assigns a new URL each time. See the [Re-register Webhooks](#re-register-webhooks-ngrok-free-tier) section above.

### Ensure Docker starts on boot

Docker should start automatically on most EC2 Ubuntu instances, but verify:

```bash
sudo systemctl enable docker
```

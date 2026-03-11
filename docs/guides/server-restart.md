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

## Webhooks (ngrok static domain)

The project uses an ngrok static domain, so the public URL stays the same across restarts. No need to re-register webhooks, update iOS Shortcuts, or reconfigure the Chrome extension after a reboot.

Verify the tunnel is working:

```bash
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
```

The URL should match the `NGROK_DOMAIN` in your `app/.env`.

> **If you ever need to change the domain**, update `NGROK_DOMAIN` and `WEBHOOK_BASE_URL` in `app/.env`, then re-register all webhooks:
>
> ```bash
> # Re-register Telegram
> docker compose exec app uv run python scripts/set_webhook.py set
>
> # Verify
> docker compose exec app uv run python scripts/set_webhook.py info
> ```
>
> Then manually update Todoist, Linear, GitHub, iOS Shortcuts, and the Chrome extension with the new URL. See the individual setup guides for details.

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

> **Note:** With a static ngrok domain configured, reboots preserve the same public URL. No webhook re-registration needed.

### Ensure Docker starts on boot

Docker should start automatically on most EC2 Ubuntu instances, but verify:

```bash
sudo systemctl enable docker
```

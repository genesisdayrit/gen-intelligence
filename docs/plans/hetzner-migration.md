# Hetzner Server Migration Plan

This guide covers migrating the Daegu application from its current infrastructure to a Hetzner server.

## Prerequisites

- Hetzner Cloud account
- SSH key pair for server access
- Domain name (optional, for custom webhook URLs)
- All environment variables from current deployment

## Phase 1: Server Provisioning

### 1.1 Create Hetzner Server

1. Log into [Hetzner Cloud Console](https://console.hetzner.cloud)
2. Create new project or select existing one
3. Add server with these specs:
   - **Location:** Choose closest to your location (Falkenstein, Nuremberg, or Helsinki for EU; Ashburn/Hillsboro for US)
   - **Image:** Ubuntu 24.04 LTS
   - **Type:** CX22 (2 vCPU, 4GB RAM) - sufficient for this workload
   - **Networking:** Public IPv4 + IPv6
   - **SSH Key:** Add your public key
   - **Name:** `daegu-prod` or similar

### 1.2 Initial Server Setup

SSH into the server:

```bash
ssh root@<server-ip>
```

Update system and install essentials:

```bash
apt update && apt upgrade -y
apt install -y curl git ufw fail2ban
```

### 1.3 Configure Firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

### 1.4 Create Non-Root User

```bash
adduser deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

## Phase 2: Docker Installation

SSH as deploy user:

```bash
ssh deploy@<server-ip>
```

Install Docker:

```bash
# Add Docker's official GPG key
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add deploy user to docker group
sudo usermod -aG docker deploy
newgrp docker
```

Verify installation:

```bash
docker --version
docker compose version
```

## Phase 3: Application Deployment

### 3.1 Clone Repository

```bash
mkdir -p ~/apps
cd ~/apps
git clone <repository-url> daegu
cd daegu
```

### 3.2 Configure Environment

Create `.env` file with all required variables:

```bash
cat > .env << 'EOF'
# Dropbox
DROPBOX_ACCESS_KEY=<your-value>
DROPBOX_ACCESS_SECRET=<your-value>
DROPBOX_REFRESH_TOKEN=<your-value>
DROPBOX_OBSIDIAN_VAULT_PATH=<your-vault-path>

# Webhook Secrets
TG_WEBHOOK_SECRET=<your-value>
TODOIST_CLIENT_SECRET=<your-value>
TODOIST_ACCESS_TOKEN=<your-value>
LINEAR_WEBHOOK_SECRET=<your-value>
GITHUB_WEBHOOK_SECRET=<your-value>
GITHUB_USERNAME=<your-username>

# System
SYSTEM_TIMEZONE=US/Eastern

# Redis (using container name)
REDIS_HOST=redis
REDIS_PORT=6379
EOF
```

### 3.3 Start Services

```bash
docker compose up -d
```

Verify all containers are running:

```bash
docker compose ps
docker compose logs -f
```

Test health endpoint:

```bash
curl http://localhost:8000/health
```

## Phase 4: HTTPS & Domain Setup

### Option A: Using Caddy (Recommended)

Caddy provides automatic HTTPS with Let's Encrypt.

Create `Caddyfile`:

```bash
cat > ~/apps/Caddyfile << 'EOF'
daegu.yourdomain.com {
    reverse_proxy localhost:8000
}
EOF
```

Install and run Caddy:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy

sudo cp ~/apps/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

### Option B: Keep Using Ngrok

If you prefer ngrok, keep the existing docker-compose setup. The ngrok container will create a tunnel automatically.

Get your public URL:

```bash
curl http://localhost:4040/api/tunnels | jq '.tunnels[0].public_url'
```

**Note:** Ngrok URLs change on restart unless you have a paid plan with reserved domains.

### Option C: Cloudflare Tunnel

For a stable free tunnel without ngrok:

```bash
# Install cloudflared
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# Login and create tunnel
cloudflared tunnel login
cloudflared tunnel create daegu
cloudflared tunnel route dns daegu daegu.yourdomain.com

# Run tunnel
cloudflared tunnel run --url http://localhost:8000 daegu
```

## Phase 5: Update Webhook URLs

After obtaining your public HTTPS URL, update webhook registrations:

### Telegram

Use the Bot API to set webhook:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://daegu.yourdomain.com/telegram/webhook&secret_token=<TG_WEBHOOK_SECRET>"
```

### Todoist

1. Go to [Todoist App Console](https://developer.todoist.com/appconsole.html)
2. Select your app
3. Update webhook URL to `https://daegu.yourdomain.com/todoist/webhook`

### Linear

1. Go to Linear Settings > API > Webhooks
2. Update webhook URL to `https://daegu.yourdomain.com/linear/webhook`

### GitHub

1. Go to your repository Settings > Webhooks
2. Update Payload URL to `https://daegu.yourdomain.com/github/webhook`

## Phase 6: Update CI/CD

Update GitHub Actions secrets for the new server:

1. Go to repository Settings > Secrets and variables > Actions
2. Update these secrets:
   - `SSH_HOST`: New Hetzner server IP
   - `SSH_USER`: `deploy`
   - `SSH_PRIVATE_KEY`: Private key for deploy user

The existing `.github/workflows/deploy.yml` should work with these updated secrets.

## Phase 7: DNS Configuration (if using custom domain)

Add an A record pointing to your Hetzner server:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | daegu | `<server-ip>` | 3600 |

Or use Cloudflare/other DNS provider with proxy enabled for DDoS protection.

## Phase 8: Monitoring & Maintenance

### Set Up Log Rotation

Docker logs can grow large. Configure log rotation:

```bash
sudo cat > /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
sudo systemctl restart docker
```

### Health Check Script

Create a simple health check cron:

```bash
cat > ~/apps/daegu/health-check.sh << 'EOF'
#!/bin/bash
if ! curl -sf http://localhost:8000/health > /dev/null; then
    cd ~/apps/daegu && docker compose restart app
fi
EOF
chmod +x ~/apps/daegu/health-check.sh

# Add to crontab (every 5 minutes)
(crontab -l 2>/dev/null; echo "*/5 * * * * ~/apps/daegu/health-check.sh") | crontab -
```

### Backup Redis Data

The Redis data is persisted in a Docker volume. To backup:

```bash
# Create backup
docker compose exec redis redis-cli BGSAVE
docker cp $(docker compose ps -q redis):/data/dump.rdb ~/backups/redis-$(date +%Y%m%d).rdb
```

## Rollback Plan

If migration fails:

1. Re-point DNS to old server (if changed)
2. Update webhook URLs back to original
3. Restore GitHub Actions secrets to old values

## Post-Migration Checklist

- [ ] Server provisioned and hardened
- [ ] Docker and Docker Compose installed
- [ ] Application cloned and configured
- [ ] All containers running (`docker compose ps`)
- [ ] Health endpoint responding
- [ ] HTTPS configured and working
- [ ] Telegram webhook updated and verified
- [ ] Todoist webhook updated and verified
- [ ] Linear webhook updated and verified
- [ ] GitHub webhook updated and verified
- [ ] CI/CD pipeline updated and tested
- [ ] Old infrastructure decommissioned

## Cost Estimate

| Resource | Monthly Cost |
|----------|--------------|
| Hetzner CX22 | ~€4.35/month |
| Domain (optional) | ~€10-15/year |
| **Total** | ~€5/month |

## Troubleshooting

### Container won't start

```bash
docker compose logs app
docker compose logs redis
```

### Webhook not receiving events

1. Verify public URL is accessible: `curl https://daegu.yourdomain.com/health`
2. Check webhook secret matches in `.env`
3. Review app logs for signature verification errors

### Redis connection issues

```bash
# Test Redis connectivity
docker compose exec app python -c "import redis; r = redis.Redis(host='redis'); print(r.ping())"
```

### Permission denied errors

Ensure deploy user is in docker group:

```bash
groups deploy  # Should include 'docker'
```

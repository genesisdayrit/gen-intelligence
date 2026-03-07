# CI/CD Deployment

Automated deployment pipeline via GitHub Actions. Pushes to `main` trigger tests and deploy.

## How It Works

The workflow (`.github/workflows/deploy.yml`) has two jobs:

1. **Test** — runs `pytest` on a GitHub-hosted runner with a Redis service
2. **Deploy** — SSHs into the EC2 instance and updates only the `app` container

### What happens on deploy

1. Pulls latest code from `main`
2. Cleans Docker build cache and unused images (prevents disk exhaustion)
3. Stops, rebuilds, and restarts only the `app` container
4. Runs a health check to verify the deploy succeeded

Importantly, **ngrok and redis are not restarted** during a deploy. This preserves the ngrok URL so you don't have to re-register webhooks on every code push.

## GitHub Secrets

These secrets must be configured in the repo settings (Settings → Secrets and variables → Actions):

| Secret | Description |
|--------|-------------|
| `SSH_HOST` | EC2 public IP or hostname |
| `SSH_USERNAME` | SSH user (e.g., `ubuntu`) |
| `SSH_PRIVATE_KEY` | Private key for SSH access |
| `SSH_PORT` | SSH port (usually `22`) |
| `DEPLOY_PATH` | Path to the repo on the server (e.g., `~/repos/gen-intelligence`) |

## Triggering a Deploy

Push to `main`:

```bash
git push origin main
```

Or merge a pull request into `main`. The workflow runs automatically.

## Monitoring a Deploy

Watch the workflow in the GitHub Actions tab, or check the server directly:

```bash
# Check container status
docker compose ps

# Check app health
curl http://localhost:8000/health

# Tail logs
docker compose logs -f --tail=50 app
```

## Disk Space Management

The EC2 instance has limited disk space. The deploy pipeline automatically cleans up before each build:

- `docker builder prune -af` — clears build cache (biggest space saver)
- `docker container prune -f` — removes stopped containers
- `docker image prune -af` — removes unused images

If a deploy fails with "no space left on device" despite the automatic cleanup, SSH in and run a manual cleanup. See [EC2 Disk Cleanup](ec2-disk-cleanup.md) for the full guide.

## Deploy vs Server Restart

| Scenario | What to do |
|----------|------------|
| Pushed code changes | Push to `main` — CI/CD handles it |
| EC2 instance rebooted | Follow [Server Restart](server-restart.md) (all containers need starting, ngrok URL changes) |
| Need to update `.env` | SSH in, edit `app/.env`, run `docker compose up -d --force-recreate app` |
| App is unhealthy | SSH in, check logs with `docker compose logs app`, restart with `docker compose restart app` |

## Troubleshooting

**Deploy fails with "no space left on device":**

SSH into the instance and free space manually:

```bash
docker system prune -a --volumes -f
sudo apt-get clean
sudo journalctl --vacuum-time=3d
```

Then re-trigger the deploy by pushing an empty commit:

```bash
git commit --allow-empty -m "retry deploy"
git push origin main
```

Consider expanding the EBS volume if this happens repeatedly. See [EC2 Disk Cleanup](ec2-disk-cleanup.md#expand-ebs-volume-permanent-fix).

**Deploy succeeds but app is broken:**

```bash
# SSH in and check logs
docker compose logs --tail=100 app

# Roll back to previous commit
cd ~/repos/gen-intelligence
git log --oneline -5           # find the last good commit
git reset --hard <commit-sha>
docker compose stop app
docker compose build --no-cache app
docker compose up -d app
```

**Health check fails after deploy:**

The health check runs 10 seconds after starting the container. If the app needs more startup time, check logs:

```bash
docker compose logs --tail=50 app
```

Common causes: missing env variables, Redis not reachable, port conflict.

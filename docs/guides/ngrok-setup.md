# ngrok Setup Guide

Expose local services to the internet with secure HTTPS tunnels using ngrok.

## What is ngrok?

ngrok creates secure tunnels from public URLs to your local machine. This is essential for:

- Receiving webhooks from external services (Telegram, GitHub, Linear, Todoist)
- Testing APIs that require HTTPS
- Sharing local development servers with others

## 1. Create an ngrok Account

1. Go to [https://ngrok.com/signup](https://ngrok.com/signup)
2. Create a free account
3. Navigate to [https://dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken)
4. Copy your authtoken

## 2. Installation

### macOS

```bash
# Using Homebrew
brew install ngrok

# Authenticate
ngrok config add-authtoken YOUR_AUTHTOKEN_HERE
```

### Linux

```bash
# Download (amd64)
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | \
  sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && \
  echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | \
  sudo tee /etc/apt/sources.list.d/ngrok.list && \
  sudo apt update && sudo apt install ngrok

# Authenticate
ngrok config add-authtoken YOUR_AUTHTOKEN_HERE
```

### Docker (Recommended for Production)

No separate installation needed - use the official Docker image:

```yaml
# docker-compose.yml
services:
  ngrok:
    image: ngrok/ngrok:latest
    restart: unless-stopped
    command:
      - "start"
      - "--all"
      - "--config"
      - "/etc/ngrok.yml"
    volumes:
      - ./ngrok.yml:/etc/ngrok.yml:ro
    ports:
      - "4040:4040"
```

Create `ngrok.yml`:

```yaml
version: "2"
authtoken: YOUR_AUTHTOKEN_HERE
tunnels:
  app:
    addr: app:8000
    proto: http
```

## 3. Basic Usage

### Start a Tunnel

```bash
# Expose local port 8000
ngrok http 8000

# Expose with custom subdomain (paid plans only)
ngrok http --subdomain=myapp 8000
```

### Get the Public URL

**From the terminal**: The URL is displayed in the ngrok interface when running.

**From the API**:
```bash
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
```

**From the web interface**: Visit `http://localhost:4040` in your browser.

### Example Output

```
ngrok

Session Status                online
Account                       you@example.com (Plan: Free)
Version                       3.x.x
Region                        United States (us)
Latency                       -
Web Interface                 http://127.0.0.1:4040
Forwarding                    https://abc123.ngrok-free.app -> http://localhost:8000
```

The `https://abc123.ngrok-free.app` URL is your public endpoint.

## 4. Configuration File

For complex setups, use a configuration file at `~/.config/ngrok/ngrok.yml`:

```yaml
version: "2"
authtoken: YOUR_AUTHTOKEN_HERE

tunnels:
  # Main API
  api:
    addr: 8000
    proto: http

  # Separate tunnel for another service
  frontend:
    addr: 3000
    proto: http
```

Start all tunnels:
```bash
ngrok start --all
```

Start a specific tunnel:
```bash
ngrok start api
```

## 5. Using with Docker Compose

This project uses ngrok in Docker Compose for production deployments:

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - WEBHOOK_URL=${WEBHOOK_URL}

  ngrok:
    image: ngrok/ngrok:latest
    restart: unless-stopped
    command:
      - "start"
      - "--all"
      - "--config"
      - "/etc/ngrok.yml"
    volumes:
      - ./ngrok.yml:/etc/ngrok.yml:ro
    ports:
      - "4040:4040"
    depends_on:
      - app
```

## 6. Free vs Paid Plans

### Free Plan Limitations

- **Random subdomain**: URL changes every restart (e.g., `abc123.ngrok-free.app`)
- **Interstitial page**: First-time visitors see an ngrok warning page
- **1 online tunnel** at a time
- **Rate limits**: Connections per minute are limited

### Paid Plans

- **Static domains**: Keep the same URL across restarts
- **No interstitial page**: Direct access to your service
- **Multiple tunnels**: Run several tunnels simultaneously
- **Custom domains**: Use your own domain names

## 7. Handling URL Changes (Free Plan)

Since the free plan generates a new URL on each restart, you need to:

1. **Get the new URL** after restarting ngrok:
   ```bash
   curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
   ```

2. **Update webhook registrations** with external services:
   - Telegram: Run `scripts/set_webhook.py set`
   - GitHub: Update webhook URL in repository settings
   - Linear: Update webhook URL in settings
   - Todoist: Update webhook URL in app console

3. **Consider upgrading** to a paid plan if frequent restarts are disruptive.

## 8. Web Interface and Inspection

ngrok provides a web interface at `http://localhost:4040` for:

- **Viewing active tunnels**: See all running tunnels and their URLs
- **Request inspection**: View all HTTP requests passing through the tunnel
- **Request replay**: Re-send previous requests for debugging
- **Traffic metrics**: Monitor request counts and response times

### API Endpoints

```bash
# List all tunnels
curl http://localhost:4040/api/tunnels

# Get specific tunnel info
curl http://localhost:4040/api/tunnels/app
```

## 9. Security Considerations

### Authentication

For sensitive endpoints, add HTTP Basic Auth:

```bash
ngrok http 8000 --basic-auth="user:password"
```

Or in config:

```yaml
tunnels:
  api:
    addr: 8000
    proto: http
    basic_auth:
      - "user:password"
```

### IP Restrictions (Paid)

Limit access to specific IP addresses:

```yaml
tunnels:
  api:
    addr: 8000
    proto: http
    ip_restriction:
      allow_cidrs:
        - "192.168.1.0/24"
```

### Webhook Signatures

Always verify webhook signatures at the application level:

- GitHub: HMAC-SHA256 in `X-Hub-Signature-256`
- Telegram: Secret token verification
- Linear/Todoist: Signature headers

## 10. Troubleshooting

### Tunnel Won't Start

**Error: authentication failed**
```bash
# Re-add your authtoken
ngrok config add-authtoken YOUR_AUTHTOKEN_HERE
```

**Error: tunnel session limit exceeded**
- Free accounts allow 1 tunnel at a time
- Kill other ngrok processes: `pkill ngrok`

### Connection Refused

```bash
# Ensure your local service is running
curl http://localhost:8000/health

# Check if the correct port is exposed
docker compose ps
```

### Tunnel Disconnects Frequently

- Check your internet connection stability
- Review ngrok status page: https://status.ngrok.com
- Consider using `restart: unless-stopped` in Docker Compose

### Docker Container Can't Reach App

Ensure you use the Docker service name, not `localhost`:

```yaml
# ngrok.yml for Docker
tunnels:
  app:
    addr: app:8000  # Use service name, not localhost
    proto: http
```

### Interstitial Page Blocking Webhooks

Some services don't follow redirects through the ngrok interstitial page. Solutions:

1. **Add ngrok-skip-browser-warning header** in webhook requests (if the service supports custom headers)
2. **Upgrade to paid plan** to remove the interstitial
3. **Use a static domain** (paid feature)

## 11. Environment Variable Reference

| Variable | Description | Example |
|----------|-------------|---------|
| `NGROK_AUTHTOKEN` | Your ngrok authentication token | `2abc123...` |
| `WEBHOOK_URL` | Full public URL including path | `https://abc.ngrok-free.app/telegram/webhook` |

## 12. Quick Reference

```bash
# Start tunnel on port 8000
ngrok http 8000

# Get public URL
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'

# Start all configured tunnels
ngrok start --all

# View web interface
open http://localhost:4040

# Kill all ngrok processes
pkill ngrok
```

## Related Guides

- [EC2 Docker Setup](./ec2-docker-setup.md) - Deploy with ngrok on AWS
- [GitHub Webhook Setup](./github-webhook-setup.md) - Configure GitHub webhooks
- [Linear Webhook Setup](./linear-webhook-setup.md) - Configure Linear webhooks
- [Todoist Webhook Setup](./todoist-webhook-setup.md) - Configure Todoist webhooks

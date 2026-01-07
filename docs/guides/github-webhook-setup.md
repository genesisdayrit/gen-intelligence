# GitHub Webhook Setup

Configure GitHub webhooks to receive real-time notifications when PRs are merged and commits are pushed to main.

## Prerequisites

- GitHub repository owner permissions
- A publicly accessible server with HTTPS (e.g., EC2 with ngrok)
- The Gen Intelligence API running (see [EC2 Docker Setup](./ec2-docker-setup.md))

## 1. Generate a Webhook Secret

Generate a secure random string for webhook verification:

```bash
# Using openssl
openssl rand -hex 32

# Or using Python
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Save this value - you'll use it in both GitHub and your `.env` file.

## 2. Update Environment Variables

Add to your `.env` file:

```bash
# GitHub Webhooks
GITHUB_WEBHOOK_SECRET=your_generated_secret_here
GITHUB_USERNAME=your_github_username
```

Restart the app after updating:

```bash
docker compose up -d --force-recreate app
```

## 3. Create a GitHub Webhook

1. Go to your GitHub repository
2. Click **Settings** (gear icon)
3. In the left sidebar, click **Webhooks**
4. Click **Add webhook**
5. Configure the webhook:
   - **Payload URL**: `https://your-ngrok-url.ngrok-free.app/github/webhook`
   - **Content type**: `application/json`
   - **Secret**: Paste your generated secret from step 1
   - **SSL verification**: Enable SSL verification (recommended)
6. Under "Which events would you like to trigger this webhook?":
   - Select **Let me select individual events**
   - Check **Pull requests**
   - Check **Pushes**
7. Ensure **Active** is checked
8. Click **Add webhook**

GitHub will send a `ping` event to verify the connection.

## 4. Get Your Public URL

If using ngrok (as in the EC2 setup):

```bash
# Get ngrok URL
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
```

Your webhook URL will be: `https://your-ngrok-url.ngrok-free.app/github/webhook`

## 5. Verify Setup

### Check Webhook Endpoint Health

```bash
curl https://your-ngrok-url.ngrok-free.app/health
# Should return: {"status":"healthy"}
```

### Test with a PR

1. Create and merge a PR in your repository
2. Check the API logs:
   ```bash
   docker compose logs -f app
   ```
3. Look for: `🔀 GitHub PR merged | repo-name#123 | PR title`

### Test with a Commit

1. Push a commit directly to `main` or `master`
2. Check the API logs for: `📝 GitHub commit | repo-name | abc1234 | Commit message`

### Verify in Daily Action

The activity should appear in your Obsidian Daily Action note under:

```
### GitHub Activity:
[HH:MM AM/PM] [repo-name#123: PR title](https://github.com/user/repo/pull/123)
[HH:MM AM/PM] [repo-name: Commit message](https://github.com/user/repo/commit/abc1234)
```

## How It Works

### PR Merged Events

1. You merge a PR in GitHub
2. GitHub sends a POST request to `/github/webhook` with `X-GitHub-Event: pull_request`
3. The API verifies the HMAC-SHA256 signature using `X-Hub-Signature-256` header
4. If `action=closed` and `merged=true`, and you are the PR author, it's logged
5. Entry is appended to today's Daily Action under "GitHub Activity"

### Push Events (Commits to Main)

1. You push commits to `main` or `master`
2. GitHub sends a POST request with `X-GitHub-Event: push`
3. The API verifies the signature and checks `ref` is `refs/heads/main` or `refs/heads/master`
4. If you are the pusher, each non-merge commit is logged
5. Entries are appended to today's Daily Action

### Filtering Logic

- **PRs**: Only tracks PRs where you are the **author** (not PRs you merged that were authored by others)
- **Commits**: Only tracks pushes to `main`/`master` where you are the **pusher**
- **Merge commits**: Automatically skipped (commits starting with "Merge pull request" or "Merge branch")

### Webhook Payload Examples

**Pull Request (merged):**
```json
{
  "action": "closed",
  "pull_request": {
    "number": 42,
    "title": "Add new feature",
    "merged": true,
    "html_url": "https://github.com/user/repo/pull/42",
    "user": {
      "login": "your_username"
    }
  },
  "repository": {
    "name": "repo-name"
  }
}
```

**Push (commit to main):**
```json
{
  "ref": "refs/heads/main",
  "pusher": {
    "name": "your_username"
  },
  "commits": [
    {
      "id": "abc1234567890",
      "message": "Fix bug in authentication",
      "url": "https://github.com/user/repo/commit/abc1234567890"
    }
  ],
  "repository": {
    "name": "repo-name"
  }
}
```

### Signature Verification

GitHub signs webhooks with HMAC-SHA256 using your secret. The signature is sent in the `X-Hub-Signature-256` header as `sha256=<hex_digest>`. The API verifies this signature before processing events.

## Setting Up Multiple Repositories

You can configure the same webhook URL for multiple repositories. Each repository needs its own webhook configured with the same secret.

To add to another repository:
1. Go to that repository's Settings → Webhooks
2. Add webhook with the same URL and secret
3. Select "Pull requests" and "Pushes" events

All events from all configured repositories will be logged to your Daily Action.

## Troubleshooting

### Webhook Not Receiving Events

1. **Check webhook delivery status in GitHub**:
   - Go to Settings → Webhooks → Click your webhook
   - Scroll to "Recent Deliveries"
   - Check for failures (red X) and view response details

2. **Check ngrok is running**:
   ```bash
   docker compose logs ngrok
   ```

3. **Verify webhook URL**:
   - Ensure URL matches your current ngrok URL
   - Ensure it ends with `/github/webhook`

4. **Check API logs for errors**:
   ```bash
   docker compose logs -f app
   ```

### Signature Verification Failing (401 Error)

- Ensure `GITHUB_WEBHOOK_SECRET` in `.env` **exactly** matches the secret in GitHub webhook settings
- No extra whitespace or quotes around the value
- Restart the app after updating `.env`:
  ```bash
  docker compose up -d --force-recreate app
  ```

### Events Being Ignored

Check logs for "ignoring" messages:

- **"GitHub PR by X (not target user Y), ignoring"**: PR was authored by someone else
- **"GitHub push by X (not target user Y), ignoring"**: Push was made by someone else
- **"GitHub push to refs/heads/feature (not main/master), ignoring"**: Push was to a non-main branch

Ensure `GITHUB_USERNAME` matches your exact GitHub username (case-sensitive).

### ngrok URL Changed

Free ngrok gives a new URL on restart. When this happens:

1. Get the new URL:
   ```bash
   curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
   ```

2. Update the webhook URL in each repository:
   - GitHub Settings → Webhooks → Edit → Update Payload URL

### Activity Not Appearing in Daily Action

1. Ensure you are the PR author (for PRs) or pusher (for commits)
2. Check the Daily Action file exists for today
3. Verify Dropbox credentials are valid
4. Check logs for Dropbox errors:
   ```bash
   docker compose logs app | grep -i dropbox
   ```

### Duplicate Entries

If you're seeing duplicate entries, it may be because:
- The webhook was triggered twice (check GitHub Recent Deliveries)
- You have webhooks configured at both repository and organization level

## Security Notes

- **Never commit** your `.env` file or expose your webhook secret
- Webhook signature verification prevents spoofed requests
- Use a strong, randomly generated secret (at least 32 characters)
- If you suspect your secret is compromised:
  1. Generate a new secret
  2. Update it in your `.env` file
  3. Update it in GitHub webhook settings
  4. Restart the app

# GitHub PRs to Obsidian Integration

> **Status:** Planned  
> **Created:** January 3, 2026  
> **Goal:** Automatically log merged GitHub PRs to Obsidian Daily Action notes in real-time

---

## Overview

Create an integration that captures all merged Pull Requests authored by you and writes them to your Obsidian Daily Action note, similar to how Todoist completed tasks are logged.

### End Result

When you merge a PR on any of your repos, an entry appears in your Daily Action:

```markdown
### Merged PRs:
[10:00 AM] feat: Add dark mode toggle - [repo-name#123](https://github.com/user/repo/pull/123)
[02:30 PM] fix: Resolve login bug - [another-repo#456](https://github.com/user/another-repo/pull/456)
```

---

## Chosen Approach: GitHub App

A personal GitHub App that receives webhook events for all your repositories automatically.

### Why This Approach

| Criteria | Benefit |
|----------|---------|
| **Instant** | Webhook fires within seconds of PR merge |
| **Automatic** | New repos are included without manual setup |
| **Scalable** | Single configuration covers all current and future repos |
| **Secure** | Webhook signature verification (same pattern as Todoist) |
| **Minimal permissions** | Only needs read access to PR metadata |

### Alternatives Considered

| Option | Verdict |
|--------|---------|
| Per-repo webhooks | Too much manual setup for each repo |
| GitHub Actions | Requires workflow file in every repo |
| Scheduled polling | Delays, unnecessary API calls, not real-time |

---

## Implementation Plan

### Phase 1: GitHub App Setup (GitHub.com)

1. **Create GitHub App**
   - Go to: `https://github.com/settings/apps/new`
   - Fill in required fields:

   | Field | Value |
   |-------|-------|
   | App name | `Obsidian PR Sync` (or similar unique name) |
   | Homepage URL | Your server URL or GitHub profile |
   | Webhook URL | `https://your-server.com/github/webhook` |
   | Webhook secret | Generate secure random string |

2. **Set Permissions**
   - Repository permissions:
     - **Pull requests**: Read-only
     - **Metadata**: Read-only (required)

3. **Subscribe to Events**
   - Check: **Pull request**

4. **Create & Install**
   - Set to "Only on this account"
   - Click "Create GitHub App"
   - Go to "Install App" → Select your account
   - Choose "All repositories" (or select specific ones)

### Phase 2: Server Implementation

#### New Environment Variables

Add to `.env`:

```bash
# GitHub App Webhook
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here
GITHUB_USERNAME=your_github_username
```

#### New Files to Create

```
app/
├── services/
│   └── obsidian/
│       └── add_github_pr.py      # NEW: Write PR to Daily Action
└── main.py                        # UPDATE: Add /github/webhook endpoint
```

#### Endpoint: `/github/webhook`

```python
@app.post("/github/webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
):
    """Receive GitHub App webhook events."""
    # 1. Verify HMAC-SHA256 signature
    # 2. Parse payload
    # 3. Check event type (X-GitHub-Event header)
    # 4. For pull_request events:
    #    - Check action == "closed" and merged == true
    #    - Check PR author matches GITHUB_USERNAME
    # 5. Write to Obsidian Daily Action
```

#### Service: `add_github_pr.py`

Follow the pattern from `add_todoist_completed.py`:

- Reuse Dropbox client helpers
- Find Daily Action folder and today's file
- Create/append to "### Merged PRs:" section
- Format: `[HH:MM AM/PM] {title} - [{repo}#{number}]({url})`

### Phase 3: Testing

1. **Create test file**: `tests/test_github_webhook.py`
   - Test signature verification
   - Test payload parsing
   - Test filtering (only merged PRs, only your username)
   - Mock Dropbox interactions

2. **Manual testing**
   - Create a test PR on a repo
   - Merge it
   - Verify entry appears in Daily Action

### Phase 4: Deployment

1. Update `.env.example` with new variables
2. Deploy to server (automatic via existing GitHub Actions workflow)
3. Verify webhook is receiving events in GitHub App settings

---

## Technical Details

### Webhook Payload (PR Merged)

GitHub sends this when a PR is merged:

```json
{
  "action": "closed",
  "pull_request": {
    "merged": true,
    "merged_at": "2026-01-03T15:30:00Z",
    "title": "feat: Add user authentication",
    "number": 42,
    "html_url": "https://github.com/you/repo/pull/42",
    "user": {
      "login": "your-username"
    }
  },
  "repository": {
    "full_name": "you/repo-name"
  }
}
```

### Webhook Headers

| Header | Purpose |
|--------|---------|
| `X-GitHub-Event` | Event type (`pull_request`) |
| `X-Hub-Signature-256` | HMAC-SHA256 signature for verification |
| `X-GitHub-Delivery` | Unique delivery ID |

### Signature Verification

Same pattern as Todoist, but using SHA256 with `sha256=` prefix:

```python
def verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### Filtering Logic

Only write to Obsidian when ALL conditions are true:

1. Event is `pull_request`
2. Action is `closed`
3. `merged` is `true`
4. `pull_request.user.login` matches your GitHub username

---

## Obsidian Output Format

### Section Header

```markdown
### Merged PRs:
```

### Entry Format

```markdown
[HH:MM AM/PM] {pr_title} - [{repo_name}#{pr_number}]({pr_url})
```

### Example

```markdown
### Merged PRs:
[10:15 AM] feat: Add dark mode support - [my-app#123](https://github.com/user/my-app/pull/123)
[03:42 PM] fix: Handle null pointer exception - [api-service#456](https://github.com/user/api-service/pull/456)
```

### Placement in Daily Action

Same logic as Todoist completed tasks:
- After "Daily Review" section if present
- After YAML frontmatter otherwise

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Invalid signature | Return 401, log warning |
| Malformed payload | Return 400, log warning |
| Dropbox API error | Log error, return 200 (don't retry) |
| Daily Action not found | Log error, return 200 |
| Non-merge PR event | Ignore, return 200 |
| PR by different author | Ignore, return 200 |

**Note:** Always return 200 for valid webhooks to prevent GitHub from retrying. Log errors for debugging.

---

## Webhook Reliability

GitHub automatically retries failed webhook deliveries:

- Retries if your server returns 4xx/5xx or times out
- Up to 3 retries over several hours
- View delivery history in GitHub App settings

If your server is temporarily down, GitHub will retry and the PR will still be logged (though possibly with a delay).

---

## Files Changed Summary

| File | Change |
|------|--------|
| `app/.env.example` | Add `GITHUB_WEBHOOK_SECRET`, `GITHUB_USERNAME` |
| `app/main.py` | Add `/github/webhook` endpoint |
| `app/services/obsidian/add_github_pr.py` | New file |
| `app/tests/test_github_webhook.py` | New file |
| `docs/guides/github-prs-to-obsidian.md` | This document |

---

## Checklist

- [ ] Create GitHub App at github.com/settings/apps/new
- [ ] Configure permissions (Pull requests: read)
- [ ] Subscribe to Pull request events
- [ ] Generate and save webhook secret
- [ ] Install app on your account
- [ ] Add environment variables to server
- [ ] Implement `/github/webhook` endpoint
- [ ] Implement `add_github_pr.py` service
- [ ] Write tests
- [ ] Deploy and verify with a test PR

---

## Future Enhancements (Optional)

- **Include PR description snippet** in the log entry
- **Track reviewed PRs** (not just authored)
- **Add labels/tags** based on PR labels
- **Link to related issues** if PR closes an issue

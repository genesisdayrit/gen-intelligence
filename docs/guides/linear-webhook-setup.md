# Linear Webhook Setup

Configure Linear webhooks to receive real-time notifications when issues are completed.

## Prerequisites

- A Linear workspace with admin permissions
- A publicly accessible server with HTTPS (e.g., EC2 with ngrok)
- The Gen Intelligence API running (see [EC2 Docker Setup](./ec2-docker-setup.md))

## 1. Create a Linear Webhook

1. Go to your Linear workspace
2. Open **Settings** (gear icon in sidebar)
3. Navigate to **API** section
4. Click **New webhook**
5. Configure the webhook:
   - **Label**: e.g., "Gen Intelligence - Completed Issues"
   - **URL**: `https://your-ngrok-url.ngrok-free.app/linear/webhook`
   - **Data change events**: Enable **Issues**
6. Click **Create webhook**

After creation, note down the **Signing secret** from the webhook details page.

## 2. Update Environment Variables

Add the signing secret to your `.env` file:

```bash
# Linear Webhooks
LINEAR_WEBHOOK_SECRET=your_linear_webhook_signing_secret
```

## 3. Get Your Public URL

If using ngrok (as in the EC2 setup):

```bash
# Get ngrok URL
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
```

Your webhook URL will be: `https://your-ngrok-url.ngrok-free.app/linear/webhook`

## 4. Verify Setup

### Check Webhook Endpoint Health

```bash
curl https://your-ngrok-url.ngrok-free.app/health
# Should return: {"status":"healthy"}
```

### Test with an Issue

1. Open Linear
2. Move an issue to a "Done" state (or any completed state)
3. Check the API logs:
   ```bash
   docker compose logs -f app
   ```
4. Look for: `✅ Linear issue completed | ENG-123: Issue title | update`

### Verify in Daily Action

The completed issue should appear in your Obsidian Daily Action note under:
```
### Completed Tasks on Todoist:
[HH:MM AM/PM] ENG-123: Issue title here
```

## How It Works

### Issue Completion
1. You move an issue to a completed state in Linear
2. Linear sends a POST request to `/linear/webhook` with issue data
3. The API verifies the HMAC-SHA256 signature using your Signing Secret
4. If the issue has a `completedAt` timestamp, it's treated as completed
5. The issue is formatted as `TEAM-123: Title` and appended to today's Daily Action

### Webhook Payload Example

```json
{
  "action": "update",
  "type": "Issue",
  "data": {
    "id": "abc123",
    "number": 123,
    "title": "Fix login bug",
    "completedAt": "2024-01-15T14:30:00.000Z",
    "team": {
      "id": "team123",
      "key": "ENG",
      "name": "Engineering"
    }
  },
  "updatedFrom": {
    "completedAt": null
  }
}
```

### Signature Verification

Linear signs webhooks with HMAC-SHA256 using your Signing Secret. The signature is sent in the `Linear-Signature` header as a hex-encoded string. The API verifies this signature before processing events.

## Troubleshooting

### Webhook Not Receiving Events

1. **Check ngrok is running**:
   ```bash
   docker compose logs ngrok
   ```

2. **Verify webhook URL in Linear**:
   - Go to Settings → API → Webhooks
   - Ensure URL matches your current ngrok URL

3. **Check API logs for errors**:
   ```bash
   docker compose logs -f app
   ```

### Signature Verification Failing

- Ensure `LINEAR_WEBHOOK_SECRET` in `.env` matches the Signing Secret from Linear
- The secret is shown on the webhook details page in Linear Settings
- Restart the app after updating `.env`:
  ```bash
  docker compose up -d --force-recreate app
  ```

### ngrok URL Changed

Free ngrok gives a new URL on restart. When this happens:

1. Get the new URL:
   ```bash
   curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
   ```

2. Update the webhook URL in Linear Settings → API → Webhooks

### Issue Not Appearing in Daily Action

1. Ensure the issue was moved to a **completed** state (not just any state change)
2. Check the Daily Action file exists for today
3. Verify Dropbox credentials are valid
4. Check logs for Dropbox errors:
   ```bash
   docker compose logs app | grep -i dropbox
   ```

## Security Notes

- **Never commit** your `.env` file or expose your Signing Secret
- Webhook signature verification prevents spoofed requests
- If you suspect your secret is compromised, delete the webhook in Linear and create a new one

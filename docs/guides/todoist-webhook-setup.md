# Todoist Webhook Setup

Configure Todoist webhooks to receive real-time notifications when tasks are completed.

## Prerequisites

- A Todoist account
- A publicly accessible server with HTTPS (e.g., EC2 with ngrok)
- The Gen Intelligence API running (see [EC2 Docker Setup](./ec2-docker-setup.md))

## 1. Create a Todoist App

1. Go to the [Todoist App Console](https://developer.todoist.com/appconsole.html)
2. Click **Create a new app**
3. Fill in the details:
   - **App name**: e.g., "Gen Intelligence Webhook"
   - **App service URL**: Your public URL (can be updated later)
4. Click **Create app**

After creation, note down:
- **Client ID** - You'll need this for `.env`
- **Client Secret** - You'll need this for `.env` and webhook signature verification

## 2. Configure OAuth (Optional but Recommended)

If you need to make API calls to Todoist (e.g., fetching task details), you'll need an access token.

### Add OAuth Redirect URL

1. In the App Console, find your app
2. Under **OAuth Redirect URL**, add: `http://localhost:8888/callback`
3. Save changes

### Get Access Token

Run the OAuth helper script:

```bash
# From the app directory
cd app

# Set credentials in .env first
echo "TODOIST_CLIENT_ID=your_client_id" >> .env
echo "TODOIST_CLIENT_SECRET=your_client_secret" >> .env

# Run OAuth flow
uv run python tests/todoist_oauth.py
```

The script will:
1. Open your browser for authorization
2. Capture the callback on localhost:8888
3. Exchange the code for an access token
4. Print the token to add to your `.env`

## 3. Set Up Webhook

### Get Your Public URL

If using ngrok (as in the EC2 setup):

```bash
# Get ngrok URL
curl -s http://localhost:4040/api/tunnels | grep -o 'https://[^"]*'
```

Your webhook URL will be: `https://your-ngrok-url.ngrok-free.app/todoist/webhook`

### Configure Webhook in Todoist

1. Go to the [Todoist App Console](https://developer.todoist.com/appconsole.html)
2. Select your app
3. Find the **Webhooks** section
4. Click **Add webhook** or configure the existing one:
   - **Webhook URL**: `https://your-ngrok-url.ngrok-free.app/todoist/webhook`
   - **Events**: Select `item:completed` and `item:uncompleted`
5. Save the webhook configuration

### Watched Events

Common events to watch:
- `item:completed` - When a task is marked complete
- `item:uncompleted` - When a completed task is marked incomplete (reopened)
- `item:added` - When a new task is created
- `item:updated` - When a task is modified
- `item:deleted` - When a task is deleted

For this setup, we use `item:completed` and `item:uncompleted` to track task completions and remove them when uncompleted.

## 4. Update Environment Variables

Add these to your `.env` file:

```bash
# Todoist credentials
TODOIST_CLIENT_ID=your_client_id_here
TODOIST_CLIENT_SECRET=your_client_secret_here

# Optional: Only needed if making API calls
TODOIST_ACCESS_TOKEN=your_access_token_here

# For reference (the webhook URL you configured in Todoist console)
TODOIST_WEBHOOK_URL=https://your-ngrok-url.ngrok-free.app/todoist/webhook
```

## 5. Verify Setup

### Check Webhook Endpoint Health

```bash
curl https://your-ngrok-url.ngrok-free.app/health
# Should return: {"status":"healthy"}
```

### Test with a Task

1. Open Todoist (app or web)
2. Complete a task
3. Check the API logs:
   ```bash
   docker compose logs -f app
   ```
4. Look for: `✅ Todoist task completed | user=... | task_id=... | content=...`

### Verify in Daily Action

The completed task should appear in your Obsidian Daily Action note under:
```
### Completed Tasks on Todoist:
[HH:MM AM/PM] Task content here
```

## How It Works

### Task Completion
1. You complete a task in Todoist
2. Todoist sends a POST request to `/todoist/webhook` with event data
3. The API verifies the HMAC-SHA256 signature using your Client Secret
4. For `item:completed` events, the task content is extracted
5. The task is appended to today's Daily Action note in Dropbox

### Task Uncompletion
1. You mark a completed task as incomplete (reopen it) in Todoist
2. Todoist sends a POST request to `/todoist/webhook` with `item:uncompleted` event
3. The API verifies the signature and extracts the task content
4. The task entry is removed from today's Daily Action note (if it exists)

### Webhook Payload Example

```json
{
  "event_name": "item:completed",
  "user_id": "12345678",
  "event_data": {
    "id": "7654321",
    "content": "Buy groceries",
    "project_id": "2345678901",
    "completed_at": "2024-01-15T14:30:00Z"
  }
}
```

### Signature Verification

Todoist signs webhooks with HMAC-SHA256 using your Client Secret. The signature is sent in the `X-Todoist-Hmac-SHA256` header. The API verifies this signature before processing events.

## Troubleshooting

### Webhook Not Receiving Events

1. **Check ngrok is running**:
   ```bash
   docker compose logs ngrok
   ```

2. **Verify webhook URL in Todoist**:
   - Go to App Console → Your App → Webhooks
   - Ensure URL matches your current ngrok URL

3. **Check API logs for errors**:
   ```bash
   docker compose logs -f app
   ```

4. **Test endpoint manually**:
   ```bash
   curl -X POST https://your-ngrok-url.ngrok-free.app/todoist/webhook \
     -H "Content-Type: application/json" \
     -d '{"event_name": "test"}'
   ```
   Note: This will fail signature verification but confirms the endpoint is reachable.

### Signature Verification Failing

- Ensure `TODOIST_CLIENT_SECRET` in `.env` matches the secret in App Console
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

2. Update the webhook URL in [Todoist App Console](https://developer.todoist.com/appconsole.html)

3. Update `TODOIST_WEBHOOK_URL` in `.env` (for reference)

### Task Not Appearing in Daily Action

1. Check the Daily Action file exists for today
2. Verify Dropbox credentials are valid
3. Check logs for Dropbox errors:
   ```bash
   docker compose logs app | grep -i dropbox
   ```

## Security Notes

- **Never commit** your `.env` file or expose your Client Secret
- The Client Secret is used for signature verification, keeping your webhook secure
- Webhook signature verification prevents spoofed requests
- If you suspect your Client Secret is compromised, regenerate it in the App Console and update your `.env`

# Linear Webhook Setup

Configure Linear webhooks to receive real-time notifications when issues are completed and when initiative/project updates are posted.

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
   - **Label**: e.g., "Gen Intelligence - Issues & Updates"
   - **URL**: `https://your-ngrok-url.ngrok-free.app/linear/webhook`
   - **Data change events**: Enable:
     - **Issues** (for completed issue tracking)
     - **Project updates** (for project status updates)
     - **Initiatives** (for initiative updates)
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

### Issue Completion (via Todoist)

1. You move an issue to a completed state in Linear
2. Linear sends a POST request to `/linear/webhook` with issue data
3. The API verifies the HMAC-SHA256 signature using your Signing Secret
4. If the issue has a `completedAt` timestamp, it's treated as completed
5. The issue is formatted as `TEAM-123: Title` and routed through Todoist to Daily Action

### Initiative & Project Updates (Direct to Weekly Cycle)

1. You post an update to an Initiative or Project in Linear
2. Linear sends a POST request to `/linear/webhook` with the update data
3. The API extracts: parent name, update body, and Linear URL
4. The update is written directly to the Weekly Cycle file (bypasses Todoist)
5. Format: `[HH:MM AM/PM] [link](url) Parent Name: Update content`

Section ordering in Weekly Cycle:

```markdown
### Wednesday -

##### Initiative Updates:
[03:15 PM] [link](https://linear.app/.../initiative-update-abc123) Initiative Name: Update content

##### Project Updates:
[04:00 PM] [link](https://linear.app/.../project-update-def456) Project Name: Update content

##### Completed Tasks:
[10:30 AM] Task content
```

**Upsert behavior**: If you edit an existing update on the same day, the entry is replaced (not duplicated). The Linear URL serves as the unique identifier.

## Webhook Payload Examples

### Issue Completion

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

### ProjectUpdate (create)

```json
{
  "action": "create",
  "type": "ProjectUpdate",
  "createdAt": "2026-01-08T06:02:43.368Z",
  "data": {
    "id": "3d719b6d-ed6a-4716-9313-6dd183c09049",
    "createdAt": "2026-01-08T06:02:43.368Z",
    "updatedAt": "2026-01-08T06:02:43.368Z",
    "body": "Worked on update to more easily calculate cost value ownership.\n\nUp next is better QA between sources",
    "slugId": "3d719b6d",
    "projectId": "6e07ea6f-7d75-452b-a814-7140eb9bd94f",
    "health": "onTrack",
    "userId": "404b5d03-7076-480b-a4c7-2bfc1f3f2f15",
    "project": {
      "id": "6e07ea6f-7d75-452b-a814-7140eb9bd94f",
      "name": "Accel - Cost Value Ownership",
      "url": "https://linear.app/chapters/project/accel-cost-value-ownership-b3b4aff0af85"
    },
    "user": {
      "id": "404b5d03-7076-480b-a4c7-2bfc1f3f2f15",
      "name": "Genesis Dayrit"
    }
  },
  "url": "https://linear.app/chapters/project/accel-cost-value-ownership-b3b4aff0af85/updates#project-update-3d719b6d",
  "organizationId": "a5959960-a447-49a8-a637-cb9312bff53b"
}
```

Key fields extracted:
- `data.project.name` → Parent name
- `data.body` → Update content
- `url` → Unique identifier for upsert

### ProjectUpdate (update - editing existing)

```json
{
  "action": "update",
  "type": "ProjectUpdate",
  "createdAt": "2026-01-08T06:09:48.084Z",
  "data": {
    "id": "1cd9338c-78bd-4991-b650-370e2a2fb835",
    "createdAt": "2026-01-08T06:09:33.102Z",
    "updatedAt": "2026-01-08T06:09:48.103Z",
    "body": "* Need to get access to training modules\n* Need to start getting access to database systems\n* Need to study Tonic Data Refresh Pipeline",
    "editedAt": "2026-01-08T06:09:48.088Z",
    "slugId": "1cd9338c",
    "projectId": "e5c43ade-46de-4236-9314-d63cda35b036",
    "health": "onTrack",
    "project": {
      "id": "e5c43ade-46de-4236-9314-d63cda35b036",
      "name": "Everlyhealth - Onboarding",
      "url": "https://linear.app/chapters/project/everlyhealth-onboarding-f0a97bf0d420"
    }
  },
  "updatedFrom": {
    "updatedAt": "2026-01-08T06:09:35.125Z",
    "body": "* Need to get access to training modules\n* Need to start getting access to database systems",
    "editedAt": null
  },
  "url": "https://linear.app/chapters/project/everlyhealth-onboarding-f0a97bf0d420/updates#project-update-1cd9338c"
}
```

Note: The `url` field stays the same between create and update, allowing upsert detection.

### InitiativeUpdate (create)

```json
{
  "action": "create",
  "type": "InitiativeUpdate",
  "createdAt": "2026-01-08T06:04:31.871Z",
  "data": {
    "id": "f4d3c4db-ef11-4fb9-9c58-fcd3ff9c596e",
    "createdAt": "2026-01-08T06:04:31.871Z",
    "updatedAt": "2026-01-08T06:04:31.871Z",
    "body": "Working on updates for capturing Weekly Cycle Updates for better tracking\n\n* now have todoist completions going to coresponding day of week in cycle\n* now working on initiative and project updates going into corresponding day in weekly cycle",
    "slugId": "f4d3c4db",
    "initiativeId": "4a94ab25-2d97-4180-be8f-0cdfa93fd0b4",
    "health": "onTrack",
    "initiative": {
      "id": "4a94ab25-2d97-4180-be8f-0cdfa93fd0b4",
      "name": "Centralizing Personal OS Systems",
      "url": "https://linear.app/chapters/initiative/centralizing-personal-os-systems-6f4162e2888f"
    },
    "user": {
      "id": "404b5d03-7076-480b-a4c7-2bfc1f3f2f15",
      "name": "Genesis Dayrit"
    }
  },
  "url": "https://linear.app/chapters/initiative/centralizing-personal-os-systems-6f4162e2888f/updates#initiative-update-f4d3c4db",
  "organizationId": "a5959960-a447-49a8-a637-cb9312bff53b"
}
```

Key fields extracted:
- `data.initiative.name` → Parent name
- `data.body` → Update content
- `url` → Unique identifier for upsert

### InitiativeUpdate (update - editing existing)

```json
{
  "action": "update",
  "type": "InitiativeUpdate",
  "createdAt": "2026-01-08T06:07:17.622Z",
  "data": {
    "id": "f4d3c4db-ef11-4fb9-9c58-fcd3ff9c596e",
    "createdAt": "2026-01-08T06:04:31.871Z",
    "updatedAt": "2026-01-08T06:07:17.637Z",
    "body": "Working on updates for capturing Weekly Cycle Updates for better tracking\n\n* now have todoist completions going to coresponding day of week in cycle\n* now working on initiative and project updates going into corresponding day in weekly cycle\n\nNext up:\n\n* can summarize Headlines for the next cycle",
    "editedAt": "2026-01-08T06:07:17.628Z",
    "slugId": "f4d3c4db",
    "initiativeId": "4a94ab25-2d97-4180-be8f-0cdfa93fd0b4",
    "health": "onTrack",
    "initiative": {
      "id": "4a94ab25-2d97-4180-be8f-0cdfa93fd0b4",
      "name": "Centralizing Personal OS Systems",
      "url": "https://linear.app/chapters/initiative/centralizing-personal-os-systems-6f4162e2888f"
    }
  },
  "updatedFrom": {
    "updatedAt": "2026-01-08T06:04:34.075Z",
    "body": "Working on updates for capturing Weekly Cycle Updates for better tracking\n\n* now have todoist completions going to coresponding day of week in cycle\n* now working on initiative and project updates going into corresponding day in weekly cycle",
    "editedAt": null
  },
  "url": "https://linear.app/chapters/initiative/centralizing-personal-os-systems-6f4162e2888f/updates#initiative-update-f4d3c4db"
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

### Initiative/Project Updates Not Appearing in Weekly Cycle

1. **Verify webhook events are enabled** in Linear Settings → API → Webhooks:
   - "Project updates" must be enabled for ProjectUpdate events
   - "Initiatives" must be enabled for InitiativeUpdate events

2. **Check logs for the event type**:
   ```bash
   docker compose logs app | grep -E "(ProjectUpdate|InitiativeUpdate)"
   ```

3. **Verify Weekly Cycle file exists** for the current week (Wednesday-Tuesday cycle)

4. **Check the day section exists** in the Weekly Cycle file:
   - Must have `### Wednesday -` (or current day) header
   - The file structure must match expected format

5. **Check for Dropbox errors**:
   ```bash
   docker compose logs app | grep -i "Failed to write to Weekly Cycle"
   ```

## Security Notes

- **Never commit** your `.env` file or expose your Signing Secret
- Webhook signature verification prevents spoofed requests
- If you suspect your secret is compromised, delete the webhook in Linear and create a new one

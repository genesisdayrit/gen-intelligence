# Linear API Reference

Documentation for Linear GraphQL API objects used in this project.

## API Endpoint

```
https://api.linear.app/graphql
```

## Authentication

```
Authorization: <LINEAR_API_KEY>
Content-Type: application/json
```

Note: No `Bearer` prefix needed for Linear API keys.

---

## Initiative

Initiatives are high-level strategic objectives that group related projects.

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Unique identifier |
| `name` | String | Initiative name |
| `slugId` | String | URL-friendly identifier |
| `url` | String | Full Linear URL |
| `status` | String | `Planned`, `Active`, or `Completed` |
| `description` | String | Initiative description |
| `health` | String | `onTrack`, `atRisk`, or `offTrack` |
| `healthUpdatedAt` | DateTime | When health was last updated |
| `startedAt` | DateTime | When initiative started |
| `completedAt` | DateTime | When initiative completed |
| `targetDate` | Date | Target completion date |
| `targetDateResolution` | String | Resolution of target date |
| `owner` | User | Initiative owner |
| `creator` | User | Who created the initiative |

### Query

```graphql
query Initiatives($first: Int!, $after: String, $includeArchived: Boolean) {
  initiatives(first: $first, after: $after, includeArchived: $includeArchived, orderBy: updatedAt) {
    nodes {
      id
      name
      slugId
      url
      status
      description
      health
      healthUpdatedAt
      startedAt
      completedAt
      targetDate
      targetDateResolution
      owner { id name email }
      creator { id name email }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

### Pagination

- Use `first: 50` for page size
- Use `after: <endCursor>` for next page
- Check `pageInfo.hasNextPage` to continue

---

## User

Represents a Linear user.

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Unique identifier |
| `name` | String | Display name |
| `email` | String | Email address |

---

## Project

Projects are containers for issues, linked to initiatives.

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Unique identifier |
| `name` | String | Project name |
| `slugId` | String | URL-friendly identifier |
| `url` | String | Full Linear URL |
| `state` | String | Project state |
| `description` | String | Project description |
| `health` | String | `onTrack`, `atRisk`, or `offTrack` |
| `startDate` | Date | Project start date |
| `targetDate` | Date | Target completion date |

---

## ProjectUpdate

Updates posted to projects.

### Webhook Payload Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Update ID |
| `body` | String | Update content (markdown) |
| `url` | String | Link to the update |
| `health` | String | Health status at time of update |
| `project.name` | String | Parent project name |
| `project.url` | String | Parent project URL |
| `user.name` | String | Author name |

---

## InitiativeUpdate

Updates posted to initiatives.

### Webhook Payload Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Update ID |
| `body` | String | Update content (markdown) |
| `url` | String | Link to the update |
| `health` | String | Health status at time of update |
| `initiative.name` | String | Parent initiative name |
| `initiative.url` | String | Parent initiative URL |
| `user.name` | String | Author name |

---

## Issue

Individual work items.

### Webhook Payload Fields (on completion)

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Issue ID |
| `identifier` | String | Human-readable ID (e.g., `GD-123`) |
| `title` | String | Issue title |
| `url` | String | Link to the issue |
| `completedAt` | DateTime | When issue was completed |
| `project.name` | String | Parent project name |

---

## Test Scripts

| Script | Description |
|--------|-------------|
| `app/tests/fetch_linear_initiatives.py` | Fetches all initiatives to JSON |
| `app/tests/capture_linear_webhooks.py` | Captures webhook payloads for debugging |

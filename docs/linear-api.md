# Linear API Reference

Documentation for Linear GraphQL API objects used in this project. This document is designed to help AI generate robust Python code for traversing Linear's hierarchical data structures.

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

## 1. Entity Hierarchy Overview

Linear's data model follows a nested structure where Initiatives act as high-level containers:

```
Initiative
├── Initiative Updates (direct child)
├── Initiative Documents (direct child)
└── Projects (direct child)
    ├── Project Updates (grandchild via Project)
    ├── Project Documents (grandchild via Project)
    └── Issues (grandchild via Project)
```

---

## 2. Core Entities

### Initiative

Initiatives are high-level strategic objectives that group related projects.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `ID!` | Unique identifier |
| `name` | `String!` | Initiative name |
| `slugId` | `String` | URL-friendly identifier |
| `url` | `String` | Full Linear URL |
| `description` | `String` | Short description |
| `content` | `String` | Full markdown content |
| `status` | `InitiativeStatus!` | `Planned`, `Active`, or `Completed` |
| `health` | `InitiativeUpdateHealthType` | `onTrack`, `atRisk`, or `offTrack` |
| `healthUpdatedAt` | `DateTime` | When health was last updated |
| `targetDate` | `TimelessDate` | Target completion date |
| `targetDateResolution` | `String` | Resolution of target date |
| `startedAt` | `DateTime` | When initiative started |
| `completedAt` | `DateTime` | When initiative completed |
| `owner` | `User` | Initiative owner |
| `creator` | `User` | Who created the initiative |

**Relationship Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `initiativeUpdates` | `InitiativeUpdateConnection!` | Status updates for this initiative |
| `documents` | `DocumentConnection!` | Documents linked to this initiative |
| `projects` | `ProjectConnection!` | Projects grouped under this initiative |

---

### Project

Projects are containers for issues, often linked to initiatives.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `ID!` | Unique identifier |
| `name` | `String!` | Project name |
| `slugId` | `String` | URL-friendly identifier |
| `url` | `String` | Full Linear URL |
| `description` | `String` | Project description |
| `content` | `String` | Full markdown content |
| `state` | `String` | Project state |
| `status` | `ProjectStatus!` | Current project status |
| `progress` | `Float!` | Completion percentage (0.0 to 1.0) |
| `health` | `String` | `onTrack`, `atRisk`, or `offTrack` |
| `startDate` | `Date` | Project start date |
| `targetDate` | `Date` | Target completion date |
| `lead` | `User` | Project lead |

**Relationship Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `projectUpdates` | `ProjectUpdateConnection!` | Status updates for this project |
| `documents` | `DocumentConnection!` | Documents linked to this project |
| `issues` | `IssueConnection!` | Issues within this project |

---

### InitiativeUpdate

Status reports posted to initiatives.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `ID!` | Unique identifier |
| `body` | `String!` | Markdown content of the update |
| `health` | `InitiativeUpdateHealthType!` | Health status at time of update |
| `createdAt` | `DateTime!` | Creation timestamp |
| `updatedAt` | `DateTime` | Last update timestamp |
| `url` | `String` | Link to the update |
| `user` | `User!` | Author of the update |

**Webhook Payload Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `data.initiative.name` | `String` | Parent initiative name |
| `data.initiative.url` | `String` | Parent initiative URL |
| `data.user.name` | `String` | Author name |

---

### ProjectUpdate

Status reports posted to projects.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `ID!` | Unique identifier |
| `body` | `String!` | Markdown content of the update |
| `health` | `ProjectUpdateHealthType!` | Health status at time of update |
| `createdAt` | `DateTime!` | Creation timestamp |
| `updatedAt` | `DateTime` | Last update timestamp |
| `url` | `String` | Link to the update |
| `user` | `User` | Author of the update |

**Webhook Payload Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `data.project.name` | `String` | Parent project name |
| `data.project.url` | `String` | Parent project URL |
| `data.user.name` | `String` | Author name |

---

### Document

Text-based resources linked to initiatives or projects.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `ID!` | Unique identifier |
| `title` | `String!` | Document title |
| `content` | `String` | Full markdown content |
| `url` | `String!` | Canonical URL to the document |
| `createdAt` | `DateTime!` | Creation timestamp |
| `updatedAt` | `DateTime` | Last update timestamp |

**Relationship Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `initiative` | `Initiative` | Parent initiative (if any) |
| `project` | `Project` | Parent project (if any) |

---

### Issue

Individual work items within a project.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `ID!` | Unique identifier |
| `identifier` | `String!` | Human-readable ID (e.g., `ENG-123`) |
| `title` | `String!` | Issue title |
| `description` | `String` | Markdown description |
| `priority` | `Int!` | Priority level |
| `state` | `WorkflowState!` | Current workflow state |
| `url` | `String` | Link to the issue |
| `completedAt` | `DateTime` | When issue was completed |
| `assignee` | `User` | User assigned to the issue |

**Webhook Payload Fields (on completion):**

| Field | Type | Description |
|-------|------|-------------|
| `data.team.key` | `String` | Team key (e.g., `ENG`) |
| `data.number` | `Int` | Issue number |
| `data.project.name` | `String` | Parent project name |

---

### User

Represents a Linear user.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `ID!` | Unique identifier |
| `name` | `String!` | Display name |
| `email` | `String` | Email address |

---

## 3. Traversal Logic

### Relationship Paths (GraphQL)

#### Path A: Initiative to Updates & Documents

```graphql
query GetInitiativeWithUpdatesAndDocs($id: String!) {
  initiative(id: $id) {
    id
    name
    initiativeUpdates {
      nodes {
        id
        body
        health
        createdAt
        user { name }
      }
    }
    documents {
      nodes {
        id
        title
        content
        url
      }
    }
  }
}
```

#### Path B: Initiative to Projects and Their Children

```graphql
query GetInitiativeWithProjects($id: String!) {
  initiative(id: $id) {
    id
    name
    projects {
      nodes {
        id
        name
        progress
        projectUpdates {
          nodes {
            id
            body
            health
            createdAt
          }
        }
        documents {
          nodes {
            id
            title
            content
          }
        }
        issues {
          nodes {
            id
            identifier
            title
            state { name }
          }
        }
      }
    }
  }
}
```

#### Path C: Full Initiative Traversal

Fetch an initiative with all related entities in a single query:

```graphql
query GetInitiativeDetails($initiativeId: String!) {
  initiative(id: $initiativeId) {
    id
    name
    description
    content
    status
    health
    targetDate
    owner { id name email }
    
    initiativeUpdates(first: 50) {
      nodes {
        id
        body
        health
        createdAt
        user { name }
      }
      pageInfo { hasNextPage endCursor }
    }
    
    documents(first: 50) {
      nodes {
        id
        title
        content
        url
      }
      pageInfo { hasNextPage endCursor }
    }
    
    projects(first: 50) {
      nodes {
        id
        name
        description
        status
        progress
        health
        
        projectUpdates(first: 50) {
          nodes {
            id
            body
            health
            createdAt
          }
          pageInfo { hasNextPage endCursor }
        }
        
        documents(first: 50) {
          nodes {
            id
            title
            content
          }
          pageInfo { hasNextPage endCursor }
        }
        
        issues(first: 100) {
          nodes {
            id
            identifier
            title
            description
            priority
            state { name type }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
```

### List All Initiatives

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

---

## 4. Traversal Constraints

### Pagination

All connection fields use the Relay pagination pattern:

- **Page size**: Use `first: 50` (or up to `first: 100` for issues)
- **Cursor**: Use `after: <endCursor>` for subsequent pages
- **Termination**: Check `pageInfo.hasNextPage` to continue

Example pagination loop pattern:

```python
def fetch_all_pages(query, variables, connection_path):
    """Fetch all pages of a paginated connection."""
    all_nodes = []
    has_next = True
    cursor = None
    
    while has_next:
        vars = {**variables}
        if cursor:
            vars['after'] = cursor
        
        result = execute_query(query, vars)
        connection = get_nested(result, connection_path)
        
        all_nodes.extend(connection['nodes'])
        has_next = connection['pageInfo']['hasNextPage']
        cursor = connection['pageInfo']['endCursor']
    
    return all_nodes
```

### Filtering Notes

- Projects can belong to multiple initiatives, but typically have a primary association
- Documents can exist without being attached to a project or initiative
- When traversing, focus on documents with a defined parent link

### Nullability

- `description`, `content`, and `targetDate` fields may be `null`
- Relationship fields (`owner`, `lead`, `assignee`) may be `null`
- Always handle optional fields gracefully

---

## 5. Python Code Generation Guidelines

When generating Python code for the Linear API:

### HTTP Client

Use the `requests` library for GraphQL POST requests:

```python
import requests
import os

LINEAR_API_URL = "https://api.linear.app/graphql"

def execute_query(query: str, variables: dict = None) -> dict:
    """Execute a GraphQL query against Linear API."""
    headers = {
        "Authorization": os.environ["LINEAR_API_KEY"],
        "Content-Type": "application/json",
    }
    
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    
    response = requests.post(LINEAR_API_URL, json=payload, headers=headers)
    response.raise_for_status()
    
    result = response.json()
    if "errors" in result:
        raise Exception(f"GraphQL errors: {result['errors']}")
    
    return result["data"]
```

### Query Construction

Define GraphQL queries as multi-line strings:

```python
INITIATIVE_QUERY = """
query GetInitiative($id: String!) {
  initiative(id: $id) {
    id
    name
    status
    projects {
      nodes {
        id
        name
      }
    }
  }
}
"""

result = execute_query(INITIATIVE_QUERY, {"id": "abc123"})
```

### Error Handling

Implement proper error handling:

```python
try:
    result = execute_query(query, variables)
except requests.exceptions.RequestException as e:
    logger.error(f"Network error: {e}")
    raise
except Exception as e:
    logger.error(f"API error: {e}")
    raise
```

### Data Extraction

Use helper functions for nested data access:

```python
def get_nested(data: dict, path: str, default=None):
    """Safely get nested dictionary values."""
    keys = path.split('.')
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key, default)
        else:
            return default
    return data

# Usage
initiative_name = get_nested(result, 'initiative.name')
projects = get_nested(result, 'initiative.projects.nodes', [])
```

### Output Structure

Return structured data objects (dataclasses or TypedDicts):

```python
from dataclasses import dataclass
from typing import Optional, List

@dataclass
class Initiative:
    id: str
    name: str
    status: str
    description: Optional[str] = None
    projects: List['Project'] = None

@dataclass
class Project:
    id: str
    name: str
    progress: float
    issues: List['Issue'] = None
```

---

## 6. Test Scripts

| Script | Description |
|--------|-------------|
| `app/tests/fetch_linear_initiatives.py` | Fetches all initiatives to JSON |
| `app/tests/fetch_initiative_details.py` | Fetches single initiative with full details |
| `app/tests/capture_linear_webhooks.py` | Captures webhook payloads for debugging |

---

## 7. References

- [Linear API Documentation](https://linear.app/docs/api)
- [Linear GraphQL Schema Reference](https://studio.apollographql.com/public/Linear-API/variant/current/schema/reference)

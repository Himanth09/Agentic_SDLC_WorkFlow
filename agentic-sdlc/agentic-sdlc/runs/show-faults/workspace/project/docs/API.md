# API reference

Generated from `openapi.yaml` (1.0.0). Errors use `application/problem+json`.

## `GET /healthz` - Liveness and analytics backlog
operationId: `health`


| Status | Meaning |
|---|---|
| 200 | Healthy |

## `POST /api/v1/links` - Create a short link
operationId: `createLink`  |  requires `X-API-Key`

- body fields: `url`*, `ttl_seconds`, `alias`

| Status | Meaning |
|---|---|
| 201 | Created |
| 400 | Malformed JSON, unknown fields or wrong content type |
| 413 | Body too large |
| 422 | Invalid URL, alias or TTL |
| 429 | Rate limited |
| 503 | Could not allocate a unique code |
| 401 | Missing or invalid API key |
| 409 | Alias already in use |

## `GET /api/v1/links/{code}` - Link metadata
operationId: `getLink`

- path parameter `code` (required): `{'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,32}$'}`

| Status | Meaning |
|---|---|
| 200 | Link |
| 404 | Unknown link |

## `DELETE /api/v1/links/{code}` - Disable a link (owner only)
operationId: `deleteLink`  |  requires `X-API-Key`

- path parameter `code` (required): `{'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,32}$'}`

| Status | Meaning |
|---|---|
| 204 | Disabled |
| 404 | Unknown link or not owned by caller |
| 401 | Missing or invalid API key |

## `GET /api/v1/links/{code}/stats` - Click analytics for a link
operationId: `getLinkStats`

- path parameter `code` (required): `{'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,32}$'}`
- query parameter `days`: `{'type': 'integer', 'minimum': 1, 'maximum': 90, 'default': 30}`

| Status | Meaning |
|---|---|
| 200 | Stats |
| 400 | Invalid query parameter |
| 404 | Unknown link |

## `GET /{code}` - Redirect to the target URL
operationId: `redirect`

- path parameter `code` (required): `{'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,32}$'}`

| Status | Meaning |
|---|---|
| 302 | Redirect |
| 404 | Unknown or disabled link |
| 410 | Link expired |

"""Contract-first design artefacts for the URL shortener blueprint.

`build_openapi()` produces the OpenAPI 3.1 contract from the *resolved*
requirement settings (auth on/off, alias support, TTL bounds), so answers given
at the requirements gate change the contract, and the contract in turn drives
code generation, test generation and documentation.
"""
# Store annotations as strings.
from __future__ import annotations

# Any: the contract is a nested dict of JSON values.
from typing import Any


# Standard error response: RFC 7807 application/problem+json using the Problem schema.
def _problem(desc: str) -> dict[str, Any]:
    return {"description": desc,
            "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}}


# Build the OpenAPI 3.1 document for the given resolved settings.
def build_openapi(settings: dict[str, Any]) -> dict[str, Any]:
    # Security requirement for write endpoints (empty list = public when API keys are off).
    secured: list[dict[str, list[str]]] = [{"ApiKeyAuth": []}] if settings["require_api_key"] else []
    # Shared {code} path parameter definition.
    code_param = {"name": "code", "in": "path", "required": True,
                  "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,32}$"}}
    # Request-body properties for POST /api/v1/links.
    create_props: dict[str, Any] = {
        # Target URL (required).
        "url": {"type": "string", "format": "uri", "maxLength": 2048,
                "description": "Absolute http(s) URL to a public host"},
        # Optional lifetime of at least one minute.
        "ttl_seconds": {"type": "integer", "minimum": 60,
                        "description": "Optional lifetime; expired links return 410"},
    }
    # Only expose "alias" when custom aliases are allowed.
    if settings["allow_custom_alias"]:
        create_props["alias"] = {"type": "string", "pattern": "^[A-Za-z0-9_-]{4,32}$",
                                 "description": "Optional custom code (vanity alias)"}

    # Helper to build an operation object (extra keys: parameters, requestBody, security...).
    def op(op_id: str, summary: str, responses: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"operationId": op_id, "summary": summary, "responses": responses, **extra}

    # Responses for link creation.
    create_responses = {
        # Success: the new Link plus a Location header.
        "201": {"description": "Created",
                "headers": {"Location": {"schema": {"type": "string"}}},
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Link"}}}},
        "400": _problem("Malformed JSON, unknown fields or wrong content type"),
        "413": _problem("Body too large"),
        "422": _problem("Invalid URL, alias or TTL"),
        # Rate limited, with Retry-After.
        "429": {**_problem("Rate limited"),
                "headers": {"Retry-After": {"schema": {"type": "integer"}}}},
        # Repeated code collisions (CodeSpaceExhausted); signals the code length is too short.
        "503": _problem("Could not allocate a unique code"),
    }
    # 401 only exists when API keys are required.
    if settings["require_api_key"]:
        create_responses["401"] = _problem("Missing or invalid API key")
    # 409 only exists when aliases are allowed.
    if settings["allow_custom_alias"]:
        create_responses["409"] = _problem("Alias already in use")

    # Responses for disabling a link.
    # 401 is always possible: only an authenticated owner may disable a link, so with
    # API keys switched off every delete is refused.
    delete_responses = {"204": {"description": "Disabled"},
                        "404": _problem("Unknown link or not owned by caller"),
                        "401": _problem("Missing or invalid API key"
                                        if settings["require_api_key"]
                                        else "Deletion requires API keys, which are disabled")}

    # Reusable JSON content block for a Link.
    link_json = {"application/json": {"schema": {"$ref": "#/components/schemas/Link"}}}
    # The complete contract.
    return {
        "openapi": "3.1.0",
        "info": {"title": "URL Shortener API", "version": "1.0.0",
                 "description": "Create short links, redirect, and read click analytics."},
        "paths": {
            # Liveness probe.
            "/healthz": {"get": op("health", "Liveness and analytics backlog", {
                "200": {"description": "Healthy"}})},
            # Create a link (secured when API keys are required).
            "/api/v1/links": {"post": op(
                "createLink", "Create a short link", create_responses,
                requestBody={"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "additionalProperties": False, "required": ["url"],
                    "properties": create_props}}}},
                **({"security": secured} if secured else {}))},
            # Read link metadata / disable a link.
            "/api/v1/links/{code}": {
                "get": op("getLink", "Link metadata", {
                    "200": {"description": "Link", "content": link_json},
                    "404": _problem("Unknown link")}, parameters=[code_param]),
                "delete": op("deleteLink", "Disable a link (owner only)", delete_responses,
                             parameters=[code_param], **({"security": secured} if secured else {})),
            },
            # Click analytics; the "days" window is capped by the retention setting.
            "/api/v1/links/{code}/stats": {"get": op(
                "getLinkStats", "Click analytics for a link", {
                    "200": {"description": "Stats", "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/Stats"}}}},
                    "400": _problem("Invalid query parameter"),
                    "404": _problem("Unknown link")},
                parameters=[code_param, {"name": "days", "in": "query", "required": False,
                                         "schema": {"type": "integer", "minimum": 1,
                                                    "maximum": settings["analytics_retention_days"],
                                                    "default": 30}}])},
            # Public redirect endpoint.
            "/{code}": {"get": op("redirect", "Redirect to the target URL", {
                "302": {"description": "Redirect", "headers": {"Location": {"schema": {"type": "string"}}}},
                "404": _problem("Unknown or disabled link"),
                "410": _problem("Link expired")}, parameters=[code_param])},
        },
        "components": {
            # API key passed in the X-API-Key header.
            "securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
            "schemas": {
                # A short link.
                "Link": {"type": "object", "required": ["code", "short_url", "target_url", "created_at"],
                         "properties": {
                             "code": {"type": "string"}, "short_url": {"type": "string"},
                             "target_url": {"type": "string"}, "created_at": {"type": "number"},
                             "expires_at": {"type": ["number", "null"]},
                             "is_custom": {"type": "boolean"}, "disabled": {"type": "boolean"}}},
                # Analytics response.
                "Stats": {"type": "object", "properties": {
                    "code": {"type": "string"}, "window_days": {"type": "integer"},
                    "total_clicks": {"type": "integer"},
                    "unique_visitors": {"type": "integer", "description":
                                        "Sum of daily unique visitors in the window (visitor ids "
                                        "rotate daily, so a returning visitor counts once per day)"},
                    "clicks_by_day": {"type": "array", "items": {"type": "object", "properties": {
                        "day": {"type": "string", "format": "date"}, "clicks": {"type": "integer"}}}},
                    "top_referrers": {"type": "array", "items": {"type": "object", "properties": {
                        "host": {"type": "string"}, "clicks": {"type": "integer"}}}},
                    "user_agents": {"type": "object", "additionalProperties": {"type": "integer"}}}},
                # RFC 7807 error body.
                "Problem": {"type": "object", "properties": {
                    "type": {"type": "string"}, "title": {"type": "string"},
                    "status": {"type": "integer"}, "detail": {"type": "string"}}},
            },
        },
    }

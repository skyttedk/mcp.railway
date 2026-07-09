"""Railway MCP Server — direct GraphQL API (no CLI needed).

Authenticates via RAILWAY_API_TOKEN env var.
"""

from __future__ import annotations

import os
import json
import requests
from mcp.server.fastmcp import FastMCP

TOKEN = os.getenv("RAILWAY_API_TOKEN", "")
API = "https://backboard.railway.com/graphql/v2"

mcp = FastMCP("railway")

def _query(query: str, variables: dict | None = None) -> dict:
    r = requests.post(API, json={"query": query, "variables": variables or {}},
                       headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"][0]["message"])
    return data["data"]

# ── tools ──────────────────────────────────────────────────────────

@mcp.tool()
def whoami() -> str:
    """Return authenticated Railway user email."""
    data = _query("query { me { email } }")
    return json.dumps(data["me"])

@mcp.tool()
def list_projects() -> str:
    """List all Railway projects."""
    data = _query("query { projects { edges { node { id name } } } }")
    projects = [e["node"] for e in data["projects"]["edges"]]
    return json.dumps(projects)

@mcp.tool()
def list_services(project_id: str) -> str:
    """List services in a Railway project."""
    data = _query("""query($id: String!) {
      project(id: $id) { services { edges { node { id name } } } }
    }""", {"id": project_id})
    services = [e["node"] for e in data["project"]["services"]["edges"]]
    return json.dumps(services)

@mcp.tool()
def list_environments(project_id: str) -> str:
    """List environments in a Railway project."""
    data = _query("""query($id: String!) {
      project(id: $id) { environments { edges { node { id name } } } }
    }""", {"id": project_id})
    envs = [e["node"] for e in data["project"]["environments"]["edges"]]
    return json.dumps(envs)

@mcp.tool()
def list_variables(project_id: str, environment_id: str, service_id: str = "") -> str:
    """List variables for a project/environment/service."""
    data = _query("""query($pid: String!, $eid: String!, $sid: String!) {
      variables(projectId: $pid, environmentId: $eid, serviceId: $sid)
    }""", {"pid": project_id, "eid": environment_id, "sid": service_id})
    return json.dumps(data["variables"])

@mcp.tool()
def set_variables(project_id: str, environment_id: str,
                  service_id: str, variables: dict[str, str]) -> str:
    """Set variables on a Railway service."""
    result = _query("""mutation($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }""", {"input": {
        "projectId": project_id, "environmentId": environment_id,
        "serviceId": service_id, "variables": variables
    }})
    return json.dumps(result)

@mcp.tool()
def get_logs(project_id: str, environment_id: str, service_id: str,
             limit: int = 50) -> str:
    """Get recent deployment logs for a service."""
    data = _query("""query($pid: String!, $eid: String!, $sid: String!, $limit: Int!) {
      deploymentLogs(projectId: $pid, environmentId: $eid, serviceId: $sid, limit: $limit) {
        timestamp message
      }
    }""", {"pid": project_id, "eid": environment_id, "sid": service_id, "limit": limit})
    return json.dumps(data.get("deploymentLogs", []))

@mcp.tool()
def deploy(project_id: str, environment_id: str, service_id: str) -> str:
    """Trigger a deploy for a service (via restart)."""
    data = _query("""mutation($sid: String!) {
      deploymentRestart(id: $sid)
    }""", {"sid": service_id})
    return json.dumps(data)

# ── run ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import anyio, uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport in ("sse", "streamable-http"):
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.getenv("PORT", "8080"))
        mcp.settings.transport_security = None

        app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()

        auth_token = os.getenv("MCP_AUTH_TOKEN")
        if auth_token:
            class _BearerAuth(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    if request.headers.get("Authorization") != f"Bearer {auth_token}":
                        return JSONResponse({"error": "Unauthorized"}, status_code=401)
                    return await call_next(request)
            app.add_middleware(_BearerAuth)

        config = uvicorn.Config(app, host=mcp.settings.host, port=mcp.settings.port,
                                log_level=mcp.settings.log_level.lower())
        anyio.run(uvicorn.Server(config).serve)
    else:
        mcp.run(transport=transport)
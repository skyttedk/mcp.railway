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
DEFAULT_PROJECT = os.getenv("RAILWAY_PROJECT_ID", "")

mcp = FastMCP("railway")

def _pid(project_id: str = "") -> str:
    """Return project_id or default from env."""
    return project_id or DEFAULT_PROJECT

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
    """List all Railway projects the token can access."""
    # Try direct projects query
    data = _query("query { projects { edges { node { id name } } } }")
    projects = [e["node"] for e in data["projects"]["edges"]]
    if projects:
        return json.dumps(projects)

    # Fallback: try via workspaces
    try:
        me = _query("query { me { workspaces { id name } } }")
        result = []
        for ws in me["me"]["workspaces"]:
            try:
                wp = _query("""query($wid: String!) {
                  workspace(workspaceId: $wid) { projects { edges { node { id name } } } }
                }""", {"wid": ws["id"]})
                for e in wp["workspace"]["projects"]["edges"]:
                    result.append({**e["node"], "workspace": ws["name"]})
            except Exception:
                pass
        if result:
            return json.dumps(result)
    except Exception:
        pass

    # Nothing found — tell user to set project ID
    if DEFAULT_PROJECT:
        return json.dumps([{"id": DEFAULT_PROJECT, "name": "(from RAILWAY_PROJECT_ID)"}])
    return json.dumps({"error": "Token cannot list projects. Set RAILWAY_PROJECT_ID or use a less-scoped token.", "workspaces": me.get("me", {}).get("workspaces", []) if 'me' in dir() else []})

@mcp.tool()
def list_services(project_id: str = "") -> str:
    """List services in a Railway project (uses RAILWAY_PROJECT_ID if empty)."""
    pid = _pid(project_id)
    if not pid:
        return json.dumps({"error": "No project_id provided and RAILWAY_PROJECT_ID not set"})
    data = _query("""query($id: String!) {
      project(id: $id) { services { edges { node { id name } } } }
    }""", {"id": pid})
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


# ── domain tools ────────────────────────────────────────────────────

@mcp.tool()
def list_service_domains(project_id: str, environment_id: str,
                         service_id: str) -> str:
    """List all domains (service + custom) for a Railway service.

    For custom domains, includes DNS verification info (TXT host/token),
    verification status, DNS records (CNAME target etc.), and SSL cert status.
    """
    data = _query("""query($pid: String!, $eid: String!, $sid: String!) {
      domains(projectId: $pid, environmentId: $eid, serviceId: $sid) {
        serviceDomains { id domain targetPort syncStatus createdAt }
        customDomains {
          id
          domain
          targetPort
          syncStatus
          createdAt
          status {
            verified
            verificationDnsHost
            verificationToken
            certificateStatus
            certificateErrorType
            certificateErrorMessage
            certificateStatusDetailed
            cdnProvider
            dnsRecords {
              hostlabel
              fqdn
              recordType
              requiredValue
              currentValue
              status
              zone
              purpose
            }
          }
        }
      }
    }""", {"pid": project_id, "eid": environment_id, "sid": service_id})
    return json.dumps(data["domains"])


@mcp.tool()
def get_custom_domain_details(project_id: str, environment_id: str,
                              service_id: str, domain: str) -> str:
    """Get full DNS details for a specific custom domain, including
    verification TXT records, CNAME targets, and SSL certificate status.

    Use this after create_custom_domain to get the verification values
    you need to set at your DNS provider (e.g. Simply.com).
    """
    data = _query("""query($pid: String!, $eid: String!, $sid: String!) {
      domains(projectId: $pid, environmentId: $eid, serviceId: $sid) {
        customDomains {
          id
          domain
          targetPort
          syncStatus
          status {
            verified
            verificationDnsHost
            verificationToken
            certificateStatus
            certificateErrorType
            certificateErrorMessage
            certificateStatusDetailed
            cdnProvider
            dnsRecords {
              hostlabel
              fqdn
              recordType
              requiredValue
              currentValue
              status
              zone
              purpose
            }
          }
        }
      }
    }""", {"pid": project_id, "eid": environment_id, "sid": service_id})

    # Filter to the requested domain
    domains = data.get("domains", {}).get("customDomains", [])
    target = [d for d in domains if d["domain"] == domain]
    if not target:
        # Try case-insensitive match
        target = [d for d in domains if d["domain"].lower() == domain.lower()]
    if not target:
        return json.dumps({"error": f"Custom domain '{domain}' not found on this service",
                           "available_domains": [d["domain"] for d in domains]})
    return json.dumps(target[0])


@mcp.tool()
def create_service_domain(project_id: str, environment_id: str,
                          service_id: str, target_port: int = 0) -> str:
    """Create a new Railway-generated domain for a service.
    Optionally set target_port (omit or set 0 for auto)."""
    inp: dict = {
        "environmentId": environment_id,
        "serviceId": service_id,
    }
    if target_port and target_port > 0:
        inp["targetPort"] = target_port
    data = _query("""mutation($input: ServiceDomainCreateInput!) {
      serviceDomainCreate(input: $input) {
        domain
        id
        targetPort
        syncStatus
      }
    }""", {"input": inp})
    return json.dumps(data["serviceDomainCreate"])


@mcp.tool()
def create_custom_domain(project_id: str, environment_id: str,
                         service_id: str, domain: str,
                         target_port: int = 0) -> str:
    """Add a custom domain (e.g. 'api.example.com') to a Railway service.
    After this, Railway provides a CNAME target — set that at your DNS provider."""
    inp: dict = {
        "domain": domain,
        "environmentId": environment_id,
        "projectId": project_id,
        "serviceId": service_id,
    }
    if target_port and target_port > 0:
        inp["targetPort"] = target_port
    data = _query("""mutation($input: CustomDomainCreateInput!) {
      customDomainCreate(input: $input) {
        id
        domain
        targetPort
        syncStatus
      }
    }""", {"input": inp})
    return json.dumps(data["customDomainCreate"])


@mcp.tool()
def delete_service_domain(domain_id: str) -> str:
    """Delete a domain from a service (pass the domain ID from list_service_domains)."""
    data = _query("""mutation($id: String!) {
      serviceDomainDelete(id: $id)
    }""", {"id": domain_id})
    return json.dumps(data)


@mcp.tool()
def update_service_domain(environment_id: str, service_id: str,
                          service_domain_id: str, domain: str,
                          target_port: int = 0) -> str:
    """Update a service domain (e.g. change target port).
    domain must match the existing domain string."""
    inp: dict = {
        "domain": domain,
        "environmentId": environment_id,
        "serviceDomainId": service_domain_id,
        "serviceId": service_id,
    }
    if target_port and target_port > 0:
        inp["targetPort"] = target_port
    data = _query("""mutation($input: ServiceDomainUpdateInput!) {
      serviceDomainUpdate(input: $input) {
        id
        domain
        targetPort
        syncStatus
      }
    }""", {"input": inp})
    return json.dumps(data["serviceDomainUpdate"])

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
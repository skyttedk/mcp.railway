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
    """Return the authenticated Railway user plus the workspaces the token can
    create projects in.

    Each workspace is {id, name}; pass a workspace id to create_project's
    workspace_id (Railway's ProjectCreateInput requires a workspaceId — there is
    no implicit "personal" default at the API level)."""
    data = _query("query { me { email name workspaces { id name } } }")
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
def create_project(name: str, description: str = "", workspace_id: str = "") -> str:
    """Create a new Railway project.

    name is required. description is optional. workspace_id is the target
    workspace (from whoami's `workspaces`); Railway's ProjectCreateInput requires
    a workspaceId, so if workspace_id is omitted this auto-selects the workspace
    when the token owns exactly one, and otherwise returns an error listing the
    available workspaces to choose from. Railway auto-creates a "production"
    environment — this returns the new project's id plus its environments
    (id + name), so the returned environment id can be passed straight to
    create_service without a separate list_environments call.
    """
    wid = workspace_id
    if not wid:
        workspaces = _query("query { me { workspaces { id name } } }")["me"]["workspaces"]
        if len(workspaces) == 1:
            wid = workspaces[0]["id"]
        elif not workspaces:
            return json.dumps({"error": "Token has no workspaces; cannot create a project."})
        else:
            return json.dumps({"error": "Multiple workspaces — pass workspace_id.",
                               "workspaces": workspaces})
    inp: dict = {"name": name, "workspaceId": wid}
    if description:
        inp["description"] = description
    data = _query("""mutation($input: ProjectCreateInput!) {
      projectCreate(input: $input) {
        id
        name
        environments { edges { node { id name } } }
      }
    }""", {"input": inp})
    proj = data["projectCreate"]
    proj["environments"] = [e["node"] for e in proj["environments"]["edges"]]
    return json.dumps(proj)

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
def create_service(project_id: str, environment_id: str, name: str) -> str:
    """Create a new Railway service inside a project/environment."""
    data = _query("""mutation($input: ServiceCreateInput!) {
      serviceCreate(input: $input) {
        id
        name
      }
    }""", {"input": {
        "projectId": project_id,
        "environmentId": environment_id,
        "name": name,
    }})
    return json.dumps(data["serviceCreate"])

@mcp.tool()
def connect_service(service_id: str, repo: str, branch: str = "master") -> str:
    """Connect a Railway service to a GitHub repo/branch for auto deploys."""
    data = _query("""mutation($id: String!, $input: ServiceConnectInput!) {
      serviceConnect(id: $id, input: $input) {
        id
        name
      }
    }""", {"id": service_id, "input": {
        "repo": repo,
        "branch": branch,
    }})
    return json.dumps(data["serviceConnect"])

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
def check_variable(project_id: str, environment_id: str,
                   service_id: str, key: str) -> str:
    """Check whether a single env var is configured on a Railway service.

    Returns {key, exists, length, sha256_16}. The value is not included, which
    keeps the response safe to log or share. sha256_16 is the first 16 hex
    chars of sha256(value): compare it against a locally computed hash to
    confirm a specific expected value without moving the value itself. Use when
    you only need to verify a key is set, rather than reading all variables."""
    import hashlib
    data = _query("""query($pid: String!, $eid: String!, $sid: String!) {
      variables(projectId: $pid, environmentId: $eid, serviceId: $sid)
    }""", {"pid": project_id, "eid": environment_id, "sid": service_id})
    variables = data["variables"] or {}
    value = variables.get(key)
    if value is None:
        return json.dumps({"key": key, "exists": False})
    v = str(value)
    return json.dumps({
        "key": key,
        "exists": True,
        "length": len(v),
        "sha256_16": hashlib.sha256(v.encode()).hexdigest()[:16],
    })

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
    """Get recent deployment logs for a service.

    Railway's API has no deploymentLogs(projectId/environmentId/serviceId) query —
    logs are keyed by deploymentId. This looks up the most recent deployment for
    the given project/environment/service, then fetches that deployment's logs.
    """
    deployments = _query("""query($input: DeploymentListInput!) {
      deployments(input: $input, first: 5) {
        edges { node { id createdAt status } }
      }
    }""", {"input": {
        "projectId": project_id, "environmentId": environment_id, "serviceId": service_id
    }})
    edges = deployments.get("deployments", {}).get("edges", [])
    if not edges:
        return json.dumps({"error": "No deployments found for this project/environment/service"})
    latest = sorted((e["node"] for e in edges), key=lambda d: d["createdAt"], reverse=True)[0]

    data = _query("""query($did: String!, $limit: Int!) {
      deploymentLogs(deploymentId: $did, limit: $limit) {
        timestamp message
      }
    }""", {"did": latest["id"], "limit": limit})
    return json.dumps({"deploymentId": latest["id"], "deploymentStatus": latest["status"],
                       "logs": data.get("deploymentLogs", [])})

@mcp.tool()
def get_metrics(project_id: str, environment_id: str, service_id: str,
                start_date: str, end_date: str = "",
                measurements: list[str] | None = None,
                sample_rate_seconds: int = 0) -> str:
    """Get CPU/memory/network/disk usage samples for a service over a time range.

    start_date/end_date are ISO 8601 timestamps (e.g. "2026-07-21T12:08:00Z");
    end_date defaults to now. measurements is a subset of: CPU_USAGE, CPU_USAGE_2,
    CPU_LIMIT, MEMORY_USAGE_GB, MEMORY_LIMIT_GB, NETWORK_TX_GB, NETWORK_RX_GB,
    DISK_USAGE_GB, EPHEMERAL_DISK_USAGE_GB, BACKUP_USAGE_GB — defaults to
    CPU_USAGE and MEMORY_USAGE_GB if omitted. Results are grouped by deployment,
    so each sample series carries its deploymentId tag — use that to isolate one
    deployment's window when others ran in the same project/environment/service
    during the requested range. Each value is {ts, value} (ts = unix seconds).
    """
    data = _query("""query($pid: String!, $eid: String!, $sid: String!, $start: DateTime!,
                          $end: DateTime, $measurements: [MetricMeasurement!]!, $rate: Int) {
      metrics(projectId: $pid, environmentId: $eid, serviceId: $sid,
              startDate: $start, endDate: $end, measurements: $measurements,
              sampleRateSeconds: $rate, groupBy: [DEPLOYMENT_ID]) {
        measurement
        tags { deploymentId }
        values { ts value }
      }
    }""", {
        "pid": project_id, "eid": environment_id, "sid": service_id,
        "start": start_date, "end": end_date or None,
        "measurements": measurements or ["CPU_USAGE", "MEMORY_USAGE_GB"],
        "rate": sample_rate_seconds or None,
    })
    return json.dumps(data.get("metrics", []))

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

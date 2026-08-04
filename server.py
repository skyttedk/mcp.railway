"""Railway MCP Server — direct GraphQL API (no CLI needed).

Authenticates via RAILWAY_API_TOKEN env var.
"""

from __future__ import annotations

import os
import json
import math
import threading
from datetime import datetime, timezone

import requests
from anyio import to_thread
from mcp.server.fastmcp import FastMCP

TOKEN = os.getenv("RAILWAY_API_TOKEN", "")
API = "https://backboard.railway.com/graphql/v2"
DEFAULT_PROJECT = os.getenv("RAILWAY_PROJECT_ID", "")

mcp = FastMCP("railway")

def _pid(project_id: str = "") -> str:
    """Return project_id or default from env."""
    return project_id or DEFAULT_PROJECT

_thread_state = threading.local()

def _session() -> requests.Session:
    """Return this thread's reusable HTTP session.

    Module-level `requests.post` builds and throws away a Session per call, so
    DNS + TCP + TLS is renegotiated every time — measured at 46-70 ms of pure
    handshake per request to backboard.railway.com, paid 3x by a single
    list_projects. A Session keeps the connection alive, so only the first call
    on a thread pays it.

    One session PER THREAD rather than one shared globally: _query_sync runs on
    anyio worker threads (see _query), and requests.Session is not documented as
    thread-safe — it mutates cookie-jar and adapter state per request. anyio
    reuses its worker threads, so a thread-local session is still reused across
    calls and gets the same saving without the shared-mutable-state risk.
    """
    s = getattr(_thread_state, "session", None)
    if s is None:
        s = requests.Session()
        _thread_state.session = s
    return s

def _query_sync(query: str, variables: dict | None = None) -> dict:
    # Auth stays a per-request header rather than a session default, so the
    # session change alters only the connection, never what is sent.
    r = _session().post(API, json={"query": query, "variables": variables or {}},
                        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"][0]["message"])
    return data["data"]

def _why(exc: Exception) -> str:
    """One short, credential-free line explaining why a Railway query failed.

    Exists so a caller can tell three situations apart at a glance: nothing is
    configured, Railway refused us, or Railway could not be reached. They are
    otherwise indistinguishable, and the caller then goes hunting for a
    configuration fault that does not exist.

    Never let this carry the token. requests puts it in a header, not the URL,
    so neither an HTTPError nor a ConnectionError repr contains it — but a
    GraphQL message is Railway's text, so redact and truncate it anyway rather
    than trusting that.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        code = response.status_code
        if code in (401, 403):
            return f"Railway refused the token (HTTP {code})"
        return f"Railway returned HTTP {code}"
    if isinstance(exc, requests.exceptions.RequestException):
        return f"Railway was unreachable ({type(exc).__name__})"
    msg = str(exc)
    if TOKEN:
        msg = msg.replace(TOKEN, "***")
    return f"Railway rejected the query: {msg[:200]}"

async def _query(query: str, variables: dict | None = None) -> dict:
    """Run one GraphQL call without blocking the event loop.

    requests is blocking, and the SDK calls a plain `def` tool directly on the
    event loop (func_metadata.py: `return fn(**args)` with no thread offload).
    A single slow Railway response would therefore stall the whole service for
    up to the 30 s timeout — including the streamable-http session traffic the
    gateway depends on — so it drops the backend and it looks like an outage
    with no error to explain it. Hence: every tool is `async def` and awaits
    this, which parks the request in a worker thread and leaves the loop free
    to serve other calls. Keep both halves of that pair; a plain `def` tool
    reintroduces the stall even though it still works.
    """
    return await to_thread.run_sync(_query_sync, query, variables)

# ── tools ──────────────────────────────────────────────────────────

@mcp.tool()
async def whoami() -> str:
    """Return the authenticated Railway user plus the workspaces the token can
    create projects in.

    Each workspace is {id, name}; pass a workspace id to create_project's
    workspace_id (Railway's ProjectCreateInput requires a workspaceId — there is
    no implicit "personal" default at the API level)."""
    data = await _query("query { me { email name workspaces { id name } } }")
    return json.dumps(data["me"])

@mcp.tool()
async def list_projects() -> str:
    """List all Railway projects the token can access."""
    # One round trip: workspaces AND their projects in a single query.
    #
    # This used to start with `query { projects }` and, when that came back
    # empty, ask `me { workspaces }` and then one more query PER workspace —
    # 2 + W requests, of which the first is guaranteed waste on our accounts
    # (the root `projects` field returns nothing for these tokens). Since
    # list_projects opens nearly every Railway session, that was paid
    # constantly. `Workspace.projects` is part of Railway's schema (verified
    # against the live endpoint), so the workspace path needs no fan-out at all.
    # Output shape is unchanged: each project still carries its "workspace".
    #
    # Every attempt below still swallows its own failure so the next one runs —
    # but it now records WHY, because if all of them come up empty that reason
    # is the only thing that explains it.
    workspaces: list[dict] = []
    failures: list[str] = []
    try:
        me = await _query("""query {
          me { workspaces { id name projects { edges { node { id name } } } } }
        }""")
        workspaces = me["me"]["workspaces"]
        result = [{**e["node"], "workspace": ws["name"]}
                  for ws in workspaces
                  for e in ws.get("projects", {}).get("edges", [])]
        if result:
            return json.dumps(result)
    except Exception as exc:
        failures.append(_why(exc))

    # Fallbacks, only reached when the single query yields nothing: a token that
    # sees projects but no workspaces, then the old per-workspace fan-out in
    # case some token exposes projects there but not nested. Both cost extra
    # requests, which is why they are last and not first.
    try:
        data = await _query("query { projects { edges { node { id name } } } }")
        projects = [e["node"] for e in data["projects"]["edges"]]
        if projects:
            return json.dumps(projects)
    except Exception as exc:
        failures.append(_why(exc))

    result = []
    for ws in workspaces:
        try:
            wp = await _query("""query($wid: String!) {
              workspace(workspaceId: $wid) { projects { edges { node { id name } } } }
            }""", {"wid": ws["id"]})
            for e in wp["workspace"]["projects"]["edges"]:
                result.append({**e["node"], "workspace": ws["name"]})
        except Exception as exc:
            failures.append(f"workspace {ws['name']}: {_why(exc)}")
    if result:
        return json.dumps(result)

    # Nothing found. If an attempt actually FAILED, that failure is the answer:
    # blaming a missing RAILWAY_PROJECT_ID here sends the reader after a
    # configuration problem that does not exist, while an outage or an expired
    # token never surfaces anywhere. The config message below is correct only
    # when every query succeeded and simply had nothing to return.
    if DEFAULT_PROJECT:
        return json.dumps([{"id": DEFAULT_PROJECT, "name": "(from RAILWAY_PROJECT_ID)"}])
    if failures:
        return json.dumps({"error": f"Could not list projects — {failures[0]}",
                           "attempts": failures})
    return json.dumps({"error": "Token cannot list projects. Set RAILWAY_PROJECT_ID or use a less-scoped token.",
                       "workspaces": [{"id": w["id"], "name": w["name"]} for w in workspaces]})

@mcp.tool()
async def create_project(name: str, description: str = "", workspace_id: str = "") -> str:
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
        workspaces = (await _query("query { me { workspaces { id name } } }"))["me"]["workspaces"]
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
    data = await _query("""mutation($input: ProjectCreateInput!) {
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
async def list_services(project_id: str = "") -> str:
    """List services in a Railway project (uses RAILWAY_PROJECT_ID if empty).

    Each service includes its per-environment `instances` with the region
    override; region null means the service runs in Railway's default region
    (no per-service override) — use get_service_instance / list_regions for
    details.

    Every instance also carries `latestDeployment` {id, status,
    deploymentStopped}, which is how you tell a stopped service from a running
    one: Railway has NO "stopped" deployment status, so a deployment stopped by
    stop_service keeps the status it already had (usually SUCCESS) and is
    flagged deploymentStopped=true instead. Without that flag a stopped service
    looks identical to a live one here — and a service whose domain has been
    removed shows up nowhere else — which is exactly how a service ends up
    forgotten. latestDeployment null means the service has never deployed."""
    pid = _pid(project_id)
    if not pid:
        return json.dumps({"error": "No project_id provided and RAILWAY_PROJECT_ID not set"})
    data = await _query("""query($id: String!) {
      project(id: $id) { services { edges { node {
        id
        name
        serviceInstances { edges { node {
          environmentId
          region
          numReplicas
          latestDeployment { id status deploymentStopped }
        } } }
      } } } }
    }""", {"id": pid})
    services = []
    for e in data["project"]["services"]["edges"]:
        svc = e["node"]
        svc["instances"] = [i["node"] for i in svc.pop("serviceInstances")["edges"]]
        services.append(svc)
    return json.dumps(services)


@mcp.tool()
async def list_regions() -> str:
    """List the deploy regions available to this Railway account.

    Returns [{id, name, location, country, region}]. Pass the `name` value
    (e.g. "europe-west4-drams3a", "us-west2") to set_region or create_volume —
    NOT the short `id` ("ams", "sfo"), which is just the metro code."""
    data = await _query("query { regions { id name location country region } }")
    return json.dumps(data["regions"])


@mcp.tool()
async def get_service_instance(environment_id: str, service_id: str) -> str:
    """Get one service's per-environment deploy config: region, replicas,
    builder, commands, healthcheck, sleep/cron settings.

    `region` is the per-service override; null means the service inherits
    Railway's default region (currently US West / us-west2 for new services —
    confirm with list_regions). Change it with set_region."""
    data = await _query("""query($sid: String!, $eid: String!) {
      serviceInstance(serviceId: $sid, environmentId: $eid) {
        serviceId
        serviceName
        environmentId
        region
        numReplicas
        builder
        buildCommand
        startCommand
        preDeployCommand
        rootDirectory
        healthcheckPath
        healthcheckTimeout
        sleepApplication
        cronSchedule
        restartPolicyType
        restartPolicyMaxRetries
      }
    }""", {"sid": service_id, "eid": environment_id})
    return json.dumps(data["serviceInstance"])


@mcp.tool()
async def set_region(environment_id: str, service_id: str, region: str,
               redeploy: bool = False) -> str:
    """Set the deploy region for a service in one environment.

    region is a region `name` from list_regions (e.g. "europe-west4-drams3a"),
    not the short metro `id`.
    The change only takes effect on the next deploy — pass redeploy=true to
    trigger one immediately. NB: attached volumes do NOT move with the
    service; a volume stays in its own region, so check list_volumes before
    moving a service with persistent storage."""
    await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id, "input": {"region": region}})
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "region": region, "updated": True, "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Region change takes effect on the next deploy."
    return json.dumps(result)

@mcp.tool()
async def set_start_command(environment_id: str, service_id: str, start_command: str,
                      redeploy: bool = False) -> str:
    """Set or clear the custom start command for a service in one environment.

    Pass an empty string to clear the override so the service falls back to
    its Dockerfile CMD / builder default. The change only takes effect on the
    next deploy — pass redeploy=true to trigger one immediately."""
    await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id,
           "input": {"startCommand": start_command or None}})
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "startCommand": start_command or None, "updated": True,
                    "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Start-command change takes effect on the next deploy."
    return json.dumps(result)


@mcp.tool()
async def create_service(project_id: str, environment_id: str, name: str) -> str:
    """Create a new Railway service inside a project/environment."""
    data = await _query("""mutation($input: ServiceCreateInput!) {
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
async def connect_service(service_id: str, repo: str, branch: str = "master") -> str:
    """Connect a Railway service to a GitHub repo/branch for auto deploys."""
    data = await _query("""mutation($id: String!, $input: ServiceConnectInput!) {
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
async def list_environments(project_id: str) -> str:
    """List environments in a Railway project."""
    data = await _query("""query($id: String!) {
      project(id: $id) { environments { edges { node { id name } } } }
    }""", {"id": project_id})
    envs = [e["node"] for e in data["project"]["environments"]["edges"]]
    return json.dumps(envs)

@mcp.tool()
async def list_variables(project_id: str, environment_id: str, service_id: str = "") -> str:
    """List which variables are set on a project/environment/service.

    Values are NEVER returned — only names plus safe metadata, so the response
    is safe to log or share. Returns a list of {key, length, sha256_16} sorted
    by key, the same shape check_variable reports for one key: sha256_16 is the
    first 16 hex chars of sha256(value). Compare hashes between environments to
    see where they differ, or against a locally computed hash to confirm a
    rotation landed — without the value leaving the server. To confirm a single
    expected value, use check_variable. There is no mode that returns raw
    values; read them in the Railway dashboard if a human truly needs one."""
    import hashlib
    data = await _query("""query($pid: String!, $eid: String!, $sid: String!) {
      variables(projectId: $pid, environmentId: $eid, serviceId: $sid)
    }""", {"pid": project_id, "eid": environment_id, "sid": service_id})
    variables = data["variables"] or {}
    return json.dumps([
        {
            "key": k,
            "length": len(str(v)),
            "sha256_16": hashlib.sha256(str(v).encode()).hexdigest()[:16],
        }
        for k, v in sorted(variables.items())
    ])

@mcp.tool()
async def check_variable(project_id: str, environment_id: str,
                   service_id: str, key: str) -> str:
    """Check whether a single env var is configured on a Railway service.

    Returns {key, exists, length, sha256_16}. The value is not included, which
    keeps the response safe to log or share. sha256_16 is the first 16 hex
    chars of sha256(value): compare it against a locally computed hash to
    confirm a specific expected value without moving the value itself. Use when
    you only need to verify a key is set, rather than reading all variables."""
    import hashlib
    data = await _query("""query($pid: String!, $eid: String!, $sid: String!) {
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
async def set_variables(project_id: str, environment_id: str,
                  service_id: str, variables: dict[str, str]) -> str:
    """Set variables on a Railway service."""
    result = await _query("""mutation($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }""", {"input": {
        "projectId": project_id, "environmentId": environment_id,
        "serviceId": service_id, "variables": variables
    }})
    return json.dumps(result)

@mcp.tool()
async def get_logs(project_id: str, environment_id: str, service_id: str,
             limit: int = 50) -> str:
    """Get recent deployment logs for a service.

    Railway's API has no deploymentLogs(projectId/environmentId/serviceId) query —
    logs are keyed by deploymentId. This looks up the most recent deployment for
    the given project/environment/service, then fetches that deployment's logs.
    """
    deployments = await _query("""query($input: DeploymentListInput!) {
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

    data = await _query("""query($did: String!, $limit: Int!) {
      deploymentLogs(deploymentId: $did, limit: $limit) {
        timestamp message
      }
    }""", {"did": latest["id"], "limit": limit})
    return json.dumps({"deploymentId": latest["id"], "deploymentStatus": latest["status"],
                       "logs": data.get("deploymentLogs", [])})

# ── keeping a metrics answer small enough to read ────────────────────
#
# Railway hands back every raw sample it holds, and the scarce resource at the
# other end of this server is the calling agent's context window, not Railway's
# CPU. A day across a few deployments is tens of thousands of {ts, value}
# objects, all of which land in that context and crowd out the task the caller
# was actually doing.
#
# So get_metrics condenses each series to a bounded number of points — but the
# high/low/average it reports are computed over EVERY raw sample, never over
# the points that survived. That distinction is the whole point: a one-minute
# CPU spike is exactly what someone asks for metrics to find, and it is exactly
# what an average-of-averages would erase.

# Points per series a caller can hold in mind (and Railway's own charts draw)
# without the answer becoming unreadable.
_METRIC_POINT_BUDGET = 360

# Round, human-legible intervals, coarsest first at the far end. A rate derived
# purely by division gives numbers like "every 47 s", which nobody can reason
# about; these are the intervals a person would have chosen anyway.
_SAMPLE_INTERVALS = (10, 30, 60, 300, 900, 1800, 3600, 21600, 86400)


def _sample_interval_for(window_seconds: float) -> int:
    """Finest round interval that keeps one series inside the point budget.

    The bound comes from the length of the window, not from a fixed count of
    points: an hour keeps Railway's own resolution, a day lands on 5-minute
    points, a month on 6-hourly ones. Past a year even daily points overflow
    the budget, so that tail falls back to plain division.
    """
    for interval in _SAMPLE_INTERVALS:
        if window_seconds <= interval * _METRIC_POINT_BUDGET:
            return interval
    return int(math.ceil(window_seconds / _METRIC_POINT_BUDGET))


def _epoch(value: str) -> float | None:
    """ISO 8601 timestamp -> unix seconds, or None if it is not one."""
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _metrics_window(start_date: str, end_date: str, series: list[dict]) -> float:
    """Length of the range the caller asked for, in seconds.

    Falls back to the span the returned samples actually cover, so a timestamp
    this function cannot parse costs some resolution rather than the guard.
    """
    start = _epoch(start_date)
    end = _epoch(end_date) if end_date else datetime.now(timezone.utc).timestamp()
    if start is not None and end is not None and end > start:
        return end - start
    stamps = [v["ts"] for s in series for v in (s.get("values") or [])
              if isinstance(v.get("ts"), (int, float))]
    return max(stamps) - min(stamps) if len(stamps) > 1 else 0.0


def _condense_metrics(series: list[dict], window_seconds: float) -> list[dict]:
    """Bound each series' point count, and say so — never silently truncate.

    Each returned point is the mean of the raw samples inside one interval, and
    every series carries a `summary` (high, low, their timestamps, average,
    sample count) computed over all of its raw samples. A series that was
    condensed also carries `sampleIntervalSeconds` and a `note`, so a caller can
    tell at a glance that it is reading a summary rather than the whole series.
    A window short enough to need no condensing is returned exactly as Railway
    measured it.
    """
    interval = _sample_interval_for(window_seconds)
    condensed = []
    for one in series:
        values = sorted((one.get("values") or []),
                        key=lambda v: v.get("ts") if isinstance(v.get("ts"), (int, float)) else 0)
        entry = {k: v for k, v in one.items() if k != "values"}

        numeric = [v for v in values if isinstance(v.get("value"), (int, float))]
        if numeric:
            high = max(numeric, key=lambda v: v["value"])
            low = min(numeric, key=lambda v: v["value"])
            entry["summary"] = {
                "high": high["value"], "highTs": high.get("ts"),
                "low": low["value"], "lowTs": low.get("ts"),
                "average": sum(v["value"] for v in numeric) / len(numeric),
                "samples": len(numeric),
            }

        buckets: list[list[dict]] = []
        previous_key = None
        for sample in values:
            ts = sample.get("ts")
            key = int(ts // interval) if isinstance(ts, (int, float)) else None
            if buckets and key is not None and key == previous_key:
                buckets[-1].append(sample)
            else:
                buckets.append([sample])
            previous_key = key

        points = []
        for group in buckets:
            numbers = [v["value"] for v in group if isinstance(v.get("value"), (int, float))]
            if not numbers:
                value = None
            elif len(numbers) == 1:
                value = numbers[0]          # untouched, not a one-element mean
            else:
                value = sum(numbers) / len(numbers)
            points.append({"ts": group[0].get("ts"), "value": value})
        entry["values"] = points

        if len(points) < len(values):
            entry["sampleIntervalSeconds"] = interval
            entry["note"] = (
                f"Summarised, not truncated: {len(values)} samples condensed to "
                f"{len(points)} points, one per {interval}s, each the mean of its "
                "interval. No sample was dropped from `summary` — its high, low "
                "and average cover the full range you asked for."
            )
        condensed.append(entry)
    return condensed


@mcp.tool()
async def get_metrics(project_id: str, environment_id: str, service_id: str,
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

    A wide range comes back SUMMARISED, never truncated. The sampling interval
    is chosen from the length of the range (about an hour keeps Railway's own
    resolution; a day becomes 5-minute points, a month 6-hourly ones), and each
    returned point is the mean of its interval — a series that was condensed
    says so in `sampleIntervalSeconds` and `note`. Every series also carries a
    `summary` with high, low (each with the timestamp it occurred at), average
    and sample count computed over ALL raw samples in the range, so a brief
    spike is still reported even where the point holding it was averaged away.
    For the usual "is this service healthy" question, read `summary` and ignore
    `values` entirely.
    """
    data = await _query("""query($pid: String!, $eid: String!, $sid: String!, $start: DateTime!,
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
    series = data.get("metrics") or []
    return json.dumps(_condense_metrics(
        series, _metrics_window(start_date, end_date, series)))

# Statuses whose deployment still has a container to restart. Everything else
# either never ran (FAILED, SKIPPED), is already gone (REMOVED, REMOVING) or is
# on its way up already (BUILDING, DEPLOYING, QUEUED…), and restarting it is
# either impossible or pointless.
_RESTARTABLE_STATUSES = ("SUCCESS", "SLEEPING", "CRASHED")


@mcp.tool()
async def deploy(project_id: str, environment_id: str, service_id: str) -> str:
    """Trigger a deploy for a service (via restart of its current deployment).

    Railway's deploymentRestart takes a DEPLOYMENT id, not a service id — a
    service id passed to it is simply an id that matches no deployment, and the
    API answers "Deployment not found" even while the service is running
    happily. So resolve the service's newest restartable deployment first, with
    the same deployments(DeploymentListInput) lookup get_logs uses, and restart
    that.
    """
    deployments = await _query("""query($input: DeploymentListInput!) {
      deployments(input: $input, first: 10) {
        edges { node { id createdAt status } }
      }
    }""", {"input": {
        "projectId": project_id, "environmentId": environment_id, "serviceId": service_id
    }})
    nodes = [e["node"] for e in deployments.get("deployments", {}).get("edges", [])]
    if not nodes:
        return json.dumps({"error": "No deployments found for this project/environment/service"})
    nodes.sort(key=lambda d: d["createdAt"], reverse=True)

    target = next((d for d in nodes if d["status"] in _RESTARTABLE_STATUSES), None)
    if target is None:
        return json.dumps({
            "error": "No restartable deployment for this service — its newest "
                     f"deployment is {nodes[0]['status']}. Restart needs a "
                     "deployment that is running (or crashed/sleeping); deploy "
                     "the service first.",
            "recentStatuses": [d["status"] for d in nodes[:5]],
        })

    data = await _query("""mutation($did: String!) {
      deploymentRestart(id: $did)
    }""", {"did": target["id"]})
    return json.dumps({"deploymentId": target["id"],
                       "deploymentStatus": target["status"],
                       "restarted": data.get("deploymentRestart")})


# A deployment can only be stopped while it still has a container. Same set as
# _RESTARTABLE_STATUSES and for the same reason, but kept separate because the
# two answer different questions and Railway is free to move one without the
# other. NB: there is no STOPPED status in Railway's DeploymentStatus enum — a
# stopped deployment keeps the status it had and is marked deploymentStopped,
# so status alone can never tell you whether a service is running.
_STOPPABLE_STATUSES = ("SUCCESS", "SLEEPING", "CRASHED")


@mcp.tool()
async def stop_service(project_id: str, environment_id: str, service_id: str) -> str:
    """Stop a service: tear down its running container so it stops consuming
    (and billing) compute. Reversible — start_service brings it back.

    This is NOT a delete. The service, its source, its variables, its domains
    and its volumes all survive untouched; only the running container goes away.
    There is no delete tool here on purpose.

    Railway's deploymentStop takes a DEPLOYMENT id, not a service id — the same
    trap deploy() documents — so this resolves the service's newest running
    deployment first and stops that one. A service with nothing running is
    reported as such rather than being blamed on a missing deployment.

    Afterwards the deployment KEEPS its old status (Railway has no STOPPED
    status) and carries deploymentStopped=true; list_services surfaces that per
    instance, so a stopped service stays visible instead of silently looking
    like a healthy one.
    """
    deployments = await _query("""query($input: DeploymentListInput!) {
      deployments(input: $input, first: 10) {
        edges { node { id createdAt status deploymentStopped } }
      }
    }""", {"input": {
        "projectId": project_id, "environmentId": environment_id, "serviceId": service_id
    }})
    nodes = [e["node"] for e in deployments.get("deployments", {}).get("edges", [])]
    if not nodes:
        return json.dumps({"error": "No deployments found for this project/environment/service"})
    nodes.sort(key=lambda d: d["createdAt"], reverse=True)

    with_container = [d for d in nodes if d["status"] in _STOPPABLE_STATUSES]
    target = next((d for d in with_container if not d.get("deploymentStopped")), None)
    if target is None:
        if with_container:
            already = with_container[0]
            return json.dumps({
                "error": f"Deployment {already['id']} is already stopped — it is "
                         f"flagged deploymentStopped even though its status is "
                         f"still {already['status']}, which is how Railway "
                         "represents a stopped deployment. Nothing to do; use "
                         "start_service to bring the service back up.",
                "deploymentId": already["id"],
                "alreadyStopped": True,
            })
        return json.dumps({
            "error": "No running deployment to stop for this service — its newest "
                     f"deployment is {nodes[0]['status']}, so it has no container "
                     "and is not billing compute. Stopping needs a deployment "
                     "that is running (or crashed/sleeping).",
            "recentStatuses": [d["status"] for d in nodes[:5]],
        })

    data = await _query("""mutation($did: String!) {
      deploymentStop(id: $did)
    }""", {"did": target["id"]})
    return json.dumps({"deploymentId": target["id"],
                       "deploymentStatus": target["status"],
                       "stopped": data.get("deploymentStop"),
                       "note": "Reversible: start_service(environment_id, service_id) "
                               "brings this service back up. The deployment keeps "
                               "its status and is now flagged deploymentStopped."})


@mcp.tool()
async def start_service(environment_id: str, service_id: str) -> str:
    """Start a service that stop_service stopped, by redeploying its instance.

    The undo half of stop_service. It goes through serviceInstanceRedeploy,
    which addresses the SERVICE and ENVIRONMENT directly, so it needs no
    deployment id and does not care what state the stopped deployment was left
    in. The service comes back with the same source, variables, domains and
    volumes it had before.
    """
    try:
        data = await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
    except RuntimeError as exc:
        # Railway answers a wrong or mismatched id pair with a generic
        # not-found/not-authorised message that reads as if the service were
        # gone. Say which ids were actually sent, so the next step is obvious.
        return json.dumps({
            "error": f"Railway refused to redeploy service {service_id} in "
                     f"environment {environment_id}: {exc}",
            "hint": "Both ids must belong to the same project — service ids come "
                    "from list_services, environment ids from list_environments. "
                    "Railway reports a mismatched pair the same way it reports a "
                    "missing one, so check the pair before concluding the service "
                    "is gone.",
        })
    return json.dumps({"serviceId": service_id, "environmentId": environment_id,
                       "started": data.get("serviceInstanceRedeploy")})


# ── domain tools ────────────────────────────────────────────────────

@mcp.tool()
async def list_service_domains(project_id: str, environment_id: str,
                         service_id: str) -> str:
    """List all domains (service + custom) for a Railway service.

    For custom domains, includes DNS verification info (TXT host/token),
    verification status, DNS records (CNAME target etc.), and SSL cert status.
    """
    data = await _query("""query($pid: String!, $eid: String!, $sid: String!) {
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
async def get_custom_domain_details(project_id: str, environment_id: str,
                              service_id: str, domain: str) -> str:
    """Get full DNS details for a specific custom domain, including
    verification TXT records, CNAME targets, and SSL certificate status.

    Use this after create_custom_domain to get the verification values
    you need to set at your DNS provider (e.g. Simply.com).
    """
    data = await _query("""query($pid: String!, $eid: String!, $sid: String!) {
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
async def create_service_domain(project_id: str, environment_id: str,
                          service_id: str, target_port: int = 0) -> str:
    """Create a new Railway-generated domain for a service.
    Optionally set target_port (omit or set 0 for auto)."""
    inp: dict = {
        "environmentId": environment_id,
        "serviceId": service_id,
    }
    if target_port and target_port > 0:
        inp["targetPort"] = target_port
    data = await _query("""mutation($input: ServiceDomainCreateInput!) {
      serviceDomainCreate(input: $input) {
        domain
        id
        targetPort
        syncStatus
      }
    }""", {"input": inp})
    return json.dumps(data["serviceDomainCreate"])


@mcp.tool()
async def create_custom_domain(project_id: str, environment_id: str,
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
    data = await _query("""mutation($input: CustomDomainCreateInput!) {
      customDomainCreate(input: $input) {
        id
        domain
        targetPort
        syncStatus
      }
    }""", {"input": inp})
    return json.dumps(data["customDomainCreate"])


@mcp.tool()
async def delete_service_domain(domain_id: str) -> str:
    """Delete a domain from a service (pass the domain ID from list_service_domains)."""
    data = await _query("""mutation($id: String!) {
      serviceDomainDelete(id: $id)
    }""", {"id": domain_id})
    return json.dumps(data)


@mcp.tool()
async def update_service_domain(environment_id: str, service_id: str,
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
    data = await _query("""mutation($input: ServiceDomainUpdateInput!) {
      serviceDomainUpdate(input: $input) {
        id
        domain
        targetPort
        syncStatus
      }
    }""", {"input": inp})
    return json.dumps(data["serviceDomainUpdate"])


@mcp.tool()
async def list_volumes(project_id: str = "") -> str:
    """List persistent volumes in a project (uses RAILWAY_PROJECT_ID if empty).

    A volume is project-scoped; its per-environment attachments are the
    `instances`, each carrying the mount path, size and the service it is
    attached to. Use an instance's `id` for volume backups and the parent
    volume's `id` for delete_volume/update_volume_mount."""
    pid = _pid(project_id)
    if not pid:
        return json.dumps({"error": "No project_id provided and RAILWAY_PROJECT_ID not set"})
    data = await _query("""query($id: String!) {
      project(id: $id) { volumes { edges { node {
        id
        name
        createdAt
        volumeInstances { edges { node {
          id
          mountPath
          sizeMB
          currentSizeMB
          region
          state
          serviceId
          environmentId
          service { id name }
        } } }
      } } } }
    }""", {"id": pid})
    volumes = []
    for edge in data["project"]["volumes"]["edges"]:
        vol = edge["node"]
        vol["instances"] = [e["node"] for e in vol.pop("volumeInstances")["edges"]]
        volumes.append(vol)
    return json.dumps(volumes)


@mcp.tool()
async def create_volume(project_id: str, environment_id: str, service_id: str,
                  mount_path: str, region: str = "") -> str:
    """Create a persistent volume and attach it to a service at mount_path.

    mount_path is the absolute path inside the container (e.g. "/data"); Railway
    rejects paths that collide with the image's own directories. region is
    optional and defaults to the service's region. Railway sizes the volume by
    plan (no size argument) and the service must redeploy before the mount is
    live — call deploy() afterwards."""
    inp: dict = {
        "projectId": project_id,
        "environmentId": environment_id,
        "serviceId": service_id,
        "mountPath": mount_path,
    }
    if region:
        inp["region"] = region
    data = await _query("""mutation($input: VolumeCreateInput!) {
      volumeCreate(input: $input) {
        id
        name
        createdAt
      }
    }""", {"input": inp})
    return json.dumps(data["volumeCreate"])


@mcp.tool()
async def update_volume_mount(volume_id: str, environment_id: str = "",
                        mount_path: str = "", service_id: str = "") -> str:
    """Re-mount an existing volume: change its mount path and/or move it to a
    different service, for one environment.

    volume_id is the parent volume (from list_volumes), not the instance id.
    environment_id selects which instance to update; omit it only for
    single-environment projects. Pass at least one of mount_path / service_id.
    Requires a redeploy of the affected service to take effect."""
    inp: dict = {}
    if mount_path:
        inp["mountPath"] = mount_path
    if service_id:
        inp["serviceId"] = service_id
    if not inp:
        return json.dumps({"error": "Pass at least one of mount_path / service_id."})
    variables: dict = {"volumeId": volume_id, "input": inp}
    if environment_id:
        variables["environmentId"] = environment_id
    data = await _query("""mutation($volumeId: String!, $environmentId: String,
                             $input: VolumeInstanceUpdateInput!) {
      volumeInstanceUpdate(volumeId: $volumeId, environmentId: $environmentId,
                           input: $input)
    }""", variables)
    return json.dumps(data)


@mcp.tool()
async def delete_volume(volume_id: str) -> str:
    """Delete a volume and permanently destroy its data.

    volume_id is the parent volume id from list_volumes. This removes the volume
    in every environment it is attached to and cannot be undone."""
    data = await _query("""mutation($volumeId: String!) {
      volumeDelete(volumeId: $volumeId)
    }""", {"volumeId": volume_id})
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

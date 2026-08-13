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
# RAILWAY_PROJECT_ID is a name Railway RESERVES. The platform injects it into
# every container with the id of the project the service is HOSTED in, and it
# overwrites a service-level variable of the same name on the next build — so it
# can never carry an operator's choice. On the riskwave instance the hosting
# project is `mcp servers` on the *skyttedk* account, which the riskwave token
# cannot read, so every call that omitted project_id fell back to it and failed
# with "Not Authorized". Setting the reserved name to the wanted project was
# tried and provably shadowed (verified 2026-08-06, restart AND full rebuild).
# MCP_DEFAULT_PROJECT_ID is our own, unreserved, and is therefore the only name
# that can actually pin a default. The reserved one stays as the fallback so an
# instance that never sets ours behaves exactly as before.
def _pinned_default_project(env=None) -> tuple[str, str]:
    """Return (project id, name of the variable it came from).

    Ours wins; Railway's reserved name is only the fallback, so an instance
    that sets neither, or only the reserved one, is unaffected. The variable
    name is returned rather than assumed because "the default is wrong" has
    two completely different answers depending on which of the two supplied it.
    """
    env = os.environ if env is None else env
    ours = env.get("MCP_DEFAULT_PROJECT_ID", "")
    if ours:
        return ours, "MCP_DEFAULT_PROJECT_ID"
    return env.get("RAILWAY_PROJECT_ID", ""), "RAILWAY_PROJECT_ID"

DEFAULT_PROJECT, DEFAULT_PROJECT_VAR = _pinned_default_project()

mcp = FastMCP("railway")

def _pid(project_id: str = "") -> str:
    """Return project_id or the pinned default (MCP_DEFAULT_PROJECT_ID)."""
    return project_id or DEFAULT_PROJECT


def _no_project(what: str, extra: str = "") -> str:
    """The refusal for "no project_id given and none pinned", said once.

    The tools that fall back to a default project used to answer this with a
    bare line naming the environment variable and stopping there — true, and
    useless to an agent that has no way to set a service variable and is
    holding a project id it could simply have passed. It is also the one
    failure here that is OURS, not Railway's, so it never passed through
    _query_sync and never got explained: the same request that reads clearly
    when the id is wrong read as a shrug when the id was missing.

    So: name the two ways forward and where to get the id. Written once because
    it is said in four places, and four copies drift.
    """
    message = (f"{what} needs a project, and none was given: project_id was "
               "empty and no default project is pinned on this server "
               "(MCP_DEFAULT_PROJECT_ID). Nothing was looked up. Pass "
               "project_id explicitly — list_projects returns the ids this "
               "token can see — or pin one by setting MCP_DEFAULT_PROJECT_ID "
               "on the Railway service so it can be omitted in future.")
    if extra:
        message = f"{message} {extra}"
    return json.dumps({"error": message,
                       "projectId": "",
                       "defaultProjectPinned": False})

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

class RailwayCallError(RuntimeError):
    """A failed Railway call whose message is already the explanation.

    Every tool here funnels through _query_sync, so this is the one place a
    Railway failure can be explained once on behalf of all 32 of them. Anything
    raised from that function is this type and its str() is the finished
    sentence — a caller may re-raise it, embed it or show it to a user without
    knowing which of the four failure modes it came from.

    It subclasses RuntimeError deliberately: several tools already catch
    RuntimeError to fold a Railway refusal into their own answer, and they must
    keep catching this without being edited one by one.
    """


def _query_sync(query: str, variables: dict | None = None) -> dict:
    # Every exit from here is a RailwayCallError carrying _why()'s explanation.
    #
    # Until 2026-08-07 only the GraphQL-errors branch was dressed up, and only
    # with _annotate_refusal; an HTTP 401, a connect timeout or an HTML error
    # page from Railway's edge escaped as requests' own repr. list_projects was
    # the single tool that translated those, because it is the only one that
    # catches its failures — so it said "Railway refused the token (HTTP 401)"
    # while the other 31 tools answered the same outage with a raw traceback
    # string. An agent that had learnt the good wording from list_projects read
    # a bare one from any other tool as a different, harder problem. Explaining
    # at the boundary rather than per tool is what makes the next improvement
    # to _why reach all of them at once.
    try:
        # Auth stays a per-request header rather than a session default, so the
        # session change alters only the connection, never what is sent.
        r = _session().post(API, json={"query": query, "variables": variables or {}},
                            headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        # Includes the non-JSON body: Railway's edge answers an overload or a
        # blocked request with HTML, and r.json() then raises a ValueError whose
        # text is a character offset — the least informative failure of the lot.
        raise RailwayCallError(_why(exc)) from exc
    if "errors" in data:
        raise RailwayCallError(_rejected(_annotate_refusal(data["errors"][0]["message"])))
    return data["data"]

# Railway answers a wrong id, an id belonging to someone else and a genuinely
# unpermitted one with the same flat refusal: it decides authorisation before
# it will admit whether the thing exists at all. A mistyped or stale id is far
# the commonest of the three, so the bare message points at access rights when
# the fix is almost always the identifier — and an agent then goes auditing
# permissions instead of re-reading what it typed. These markers are how a
# refusal of that shape is recognised; every other failure is left untouched,
# because a wrong explanation attached to an unrelated error is its own dead
# end.
_REFUSAL_MARKERS = ("not authorized", "not authorised", "unauthorized",
                    "unauthorised", "forbidden", "access denied",
                    "permission denied")

# Appended, never substituted. Railway is occasionally right — sometimes the
# token really is missing a permission — so this says what the refusal USUALLY
# means and leaves the platform's own words standing next to it. A message that
# confidently blamed the identifier would just mislead in the other direction.
# Kept short on purpose: _why truncates a rejection at 200 characters, and the
# explanation is worth nothing if it is the half that gets cut off.
_REFUSAL_HINT = (" — this usually means the identifier was not recognised on "
                 "this account rather than a real permissions problem, though "
                 "it can be either.")

def _annotate_refusal(message: str) -> str:
    """Return Railway's error message, with the likely cause added if it is a
    refusal of the not-authorised kind.

    Sits in the one place every tool's errors pass through, so all of them gain
    the explanation at once and none of them has to repeat it. The token is
    redacted on the way out for the same reason _why does it: this text is now
    handed to callers by us, and Railway's message is Railway's to write.
    """
    if TOKEN:
        message = message.replace(TOKEN, "***")
    lowered = message.lower()
    if any(marker in lowered for marker in _REFUSAL_MARKERS):
        return message + _REFUSAL_HINT
    return message

def _rejected(message: str) -> str:
    """The one line for "the call reached Railway and Railway said no".

    Split out of _why so _query_sync can produce exactly the same sentence when
    it raises, rather than a second wording that only looks similar. The token
    is redacted and the text truncated here, in the one place Railway's own
    words are turned into ours: this string now leaves the server both as a
    raised error and inside a JSON answer, and a credential must not ride along
    either exit.
    """
    if TOKEN:
        message = message.replace(TOKEN, "***")
    return f"Railway rejected the query: {message[:200]}"


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
    # A RailwayCallError has already been through here: _query_sync built its
    # message with this function. Re-explaining it would produce "Railway
    # rejected the query: Railway refused the token (HTTP 401)" — our own
    # sentence quoted as if it were Railway's. Returning it unchanged is also
    # what keeps list_projects' output identical to before the boundary was
    # wrapped, since every failure it catches now arrives pre-explained.
    if isinstance(exc, RailwayCallError):
        return str(exc)
    response = getattr(exc, "response", None)
    if response is not None:
        code = response.status_code
        if code in (401, 403):
            return f"Railway refused the token (HTTP {code})"
        return f"Railway returned HTTP {code}"
    # Checked before RequestException on purpose: requests' JSONDecodeError
    # subclasses both, and the RequestException branch would call an HTML error
    # page "unreachable" — sending the reader to look at the network when the
    # request arrived and came back with a proxy page. It is also the least
    # informative failure raw, since json's own text is a character offset.
    if isinstance(exc, ValueError):
        return ("Railway answered with a body that is not JSON — usually its "
                "edge rather than the API (an error or rate-limit page), so "
                "the request never reached GraphQL")
    if isinstance(exc, requests.exceptions.RequestException):
        return f"Railway was unreachable ({type(exc).__name__})"
    return _rejected(str(exc))

def _could_not_list(failures: list[str]) -> str:
    """The one sentence saying the project lookup failed, and why.

    Written once because it is now said in two places: as the whole answer when
    nothing came back at all, and alongside a pinned project when one did. Two
    hand-written copies would drift apart, and a caller that learned to
    recognise one wording would not recognise the other.
    """
    return f"Could not list projects — {failures[0]}"

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
    """List all Railway projects the token can access.

    When the lookup fails but a default project is pinned
    (MCP_DEFAULT_PROJECT_ID), that one project is still returned — carrying a
    `warning` naming what went wrong, so a dead token or a Railway outage stays
    visible instead of being masked by the pinned id.
    """
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
    # blaming a missing default project here sends the reader after a
    # configuration problem that does not exist, while an outage or an expired
    # token never surfaces anywhere. The config message below is correct only
    # when every query succeeded and simply had nothing to return.
    #
    # A pinned default project is a usable answer and stays the answer — the
    # caller gets a project rather than an error, which is the right trade. But
    # returning it alone says nothing about HOW we got here: one pinned project
    # satisfies whoever asked just as well whether the wider lookup found
    # nothing or died trying, so an expiring token or a Railway outage can sit
    # unnoticed for as long as that one id keeps being enough. The note rides
    # along on the project itself, which keeps the answer a list of projects —
    # a second element would look like a project that does not exist, and an
    # object would break every caller that iterates this.
    if DEFAULT_PROJECT:
        pinned = {"id": DEFAULT_PROJECT, "name": f"(from {DEFAULT_PROJECT_VAR})"}
        if failures:
            pinned["warning"] = _could_not_list(failures)
            pinned["attempts"] = failures
        return json.dumps([pinned])
    if failures:
        return json.dumps({"error": _could_not_list(failures),
                           "attempts": failures})
    return json.dumps({"error": "Token cannot list projects. Set MCP_DEFAULT_PROJECT_ID or use a less-scoped token.",
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

# Statuses of a deployment that has not finished being released yet: it is on
# its way up and has never had a container. Railway nevertheless answers
# `deploymentStopped: true` for such a deployment — seen 2026-08-06 on a build
# that was actively BUILDING and succeeded seconds later (card
# "list_services reports a fresh BUILDING deployment as deploymentStopped: true").
# The flag is only meaningful once a deployment HAS a container to stop, so on
# an in-flight one it is noise, and noise that reads as "this deploy is dead" —
# exactly the wrong conclusion while a build is running, and the one that makes
# an agent fire a pointless redeploy nudge.
#
# Kept separate from _LIVE_STATUSES / _RESTARTABLE_STATUSES / _STOPPABLE_STATUSES
# for the same reason those three are separate from each other: they answer
# different questions and Railway is free to move one without the others. Those
# three all exclude in-flight statuses already, so `_running_deployment` and
# `stop_service` never see the bogus flag — only the raw listing does.
_IN_FLIGHT_STATUSES = ("BUILDING", "DEPLOYING", "INITIALIZING", "QUEUED",
                       "WAITING", "NEEDS_APPROVAL")


def _correct_stopped_flag(deployment: dict | None) -> dict | None:
    """Clear `deploymentStopped` on a deployment that is still in flight.

    Genuinely stopped, crashed and removed deployments are untouched — the flag
    keeps its meaning; only a status that cannot have a container yet is
    corrected. Railway's own value is preserved as `railwayDeploymentStopped`
    beside a note, so nothing is hidden from a caller who wants to see it."""
    if not deployment or not deployment.get("deploymentStopped"):
        return deployment
    if deployment.get("status") not in _IN_FLIGHT_STATUSES:
        return deployment
    deployment["deploymentStopped"] = False
    deployment["railwayDeploymentStopped"] = True
    deployment["deploymentStoppedNote"] = (
        f"Railway reported deploymentStopped=true for this {deployment['status']} "
        "deployment, which has no container yet and so cannot have been stopped; "
        "it is reported as not stopped here. The build is still in flight — wait "
        "for it, do not read this as a dead deploy and do not redeploy on it.")
    return deployment


@mcp.tool()
async def list_services(project_id: str = "") -> str:
    """List services in a Railway project (uses the pinned default project,
    MCP_DEFAULT_PROJECT_ID, if empty).

    Each service includes its per-environment `instances` with the region
    override; region null means the service runs in Railway's default region
    (no per-service override) — use get_service_instance / list_regions for
    details.

    Every instance also carries `latestDeployment` {id, createdAt, status,
    deploymentStopped}, which is how you tell a stopped service from a running
    one: Railway has NO "stopped" deployment status, so a deployment stopped by
    stop_service keeps the status it already had (usually SUCCESS) and is
    flagged deploymentStopped=true instead. Without that flag a stopped service
    looks identical to a live one here — and a service whose domain has been
    removed shows up nowhere else — which is exactly how a service ends up
    forgotten. latestDeployment null means the service has never deployed.

    A DEPLOYMENT THAT IS STILL IN FLIGHT IS NEVER REPORTED AS STOPPED. Railway
    has been seen answering deploymentStopped=true for a deployment that was
    actively BUILDING and succeeded seconds later; the flag only means anything
    once a deployment has a container to stop, so on a BUILDING / DEPLOYING /
    INITIALIZING / QUEUED / WAITING / NEEDS_APPROVAL status it is corrected to
    false here, with Railway's raw value kept as `railwayDeploymentStopped` and
    a `deploymentStoppedNote` saying why. Uncorrected it reads as a dead deploy
    mid-build. For genuinely stopped, crashed or failed deployments the flag is
    passed through untouched.

    A SUCCESSFUL, UNSTOPPED DEPLOYMENT IS STILL NOT PROOF OF A RUNNING
    CONTAINER. These fields describe the deployment, not the process: a service
    whose container has gone away keeps latestDeployment SUCCESS and
    deploymentStopped false indefinitely, and nothing here tells it apart from a
    healthy one — a production database sat dead for five months that way. When
    the question is whether something is actually up, call get_logs (it checks
    resource usage when the running deployment has printed nothing, and reports
    `containerCheck`) or read get_metrics directly: memory flat at zero means no
    container.

    DO NOT USE THIS TO CONFIRM THAT A DEPLOY LANDED. `latestDeployment` is
    Railway's own per-instance pointer, and it has been seen naming the
    PREVIOUS deployment for the whole of a deploy — still stale well after the
    new code was provably answering live traffic. It does catch up, so it is
    not wrong so much as late, and neither the id nor the status says which of
    the two you are holding: an unchanged id reads exactly like a push that
    never landed. That is why `createdAt` is asked for and returned — a
    deployment created before the push you just made is the stale one, and
    now visibly so. To actually confirm a deploy, use the deployment id
    create_deployment returns, or call get_logs: it reads the deployments list
    directly and reports deploymentId, deploymentCreatedAt and
    deploymentIsRunning."""
    pid = _pid(project_id)
    if not pid:
        return _no_project("list_services")
    data = await _query("""query($id: String!) {
      project(id: $id) { services { edges { node {
        id
        name
        serviceInstances { edges { node {
          environmentId
          region
          numReplicas
          latestDeployment { id createdAt status deploymentStopped }
        } } }
      } } } }
    }""", {"id": pid})
    services = []
    for e in data["project"]["services"]["edges"]:
        svc = e["node"]
        svc["instances"] = [i["node"] for i in svc.pop("serviceInstances")["edges"]]
        for inst in svc["instances"]:
            _correct_stopped_flag(inst.get("latestDeployment"))
        services.append(svc)
    return json.dumps(services)


@mcp.tool()
async def list_regions() -> str:
    """List the deploy regions available to this Railway account, grouped by the
    physical metro each one sits in.

    There are far more region names than places: today 13 names across 5 metros,
    so most of the list is aliases. Railway's `id` is the metro code and it
    REPEATS across rows — `us-east4-eqdc4a`, `us-east-1`, `us-east4` and
    `us-east4-eqdc16a` are all `iad`, one datacentre. Read as a flat list that
    is easy to miss, and two services put in "different regions" can turn out to
    be in the same rack.

    So the answer is `{metros, regions, note}` rather than a bare list.
    `metros` has one entry per place — {metro_id, location, country, region,
    names} — and `names` are that metro's interchangeable region names. Each row
    in `regions` is Railway's own {id, name, location, country, region} plus
    `metro_id`, a spelled-out copy of `id` (the name `id` reads like a row key,
    which is exactly the misreading this tool exists to prevent).

    Pass a `name` (e.g. "europe-west4-drams3a", "us-west2") to set_region or
    create_volume — a metro id passed where a region name belongs is a known way
    to leave a service unable to deploy, so prefer `name` even though set_region
    took the short id when that was tried (2026-08-10). Do not group by
    `location` either: `sfo` (California) and `pdx` (Oregon) are two metros both
    labelled "US West"."""
    data = await _query("query { regions { id name location country region } }")
    return json.dumps(_group_regions_by_metro(data["regions"]))


@mcp.tool()
async def get_service_instance(environment_id: str, service_id: str) -> str:
    """Get one service's per-environment deploy config: source (repo or Docker
    image), region, replicas, builder, Dockerfile path, commands, healthcheck,
    sleep/cron settings. This is the read side of set_service_config.

    `region` is the per-service override; null means the service inherits
    Railway's default region (currently US West / us-west2 for new services —
    confirm with list_regions). Change it with set_region, and remove it again
    with set_region and an empty region. Most of what this reports has a
    standalone setter too — set_build_command, set_start_command,
    set_dockerfile_path, set_root_directory, set_healthcheck, set_num_replicas
    — each clearing a string override again on an empty string; the rest
    (builder, watch patterns, pre-deploy command, restart policy, cron
    schedule, sleep) is written with set_service_config."""
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
        dockerfilePath
        watchPatterns
        source { image repo }
        healthcheckPath
        healthcheckTimeout
        sleepApplication
        cronSchedule
        restartPolicyType
        restartPolicyMaxRetries
      }
    }""", {"sid": service_id, "eid": environment_id})
    return json.dumps(data["serviceInstance"])


# A service belongs to a project, but its deploy CONFIG belongs to a
# service *instance* — one per environment. A service can therefore exist in
# the project and have no instance in the environment being configured, and
# `serviceInstanceUpdate` does not object: it answers without an error, the
# settings are written nowhere, and the tool used to report `updated: true`
# with the settings echoed back. The write is not merely lost, it is reported
# as done — the caller only finds out at the next deploy, which refuses with
# "Service Instance not found". So: confirm the instance first and refuse if
# it is not there, exactly as delete_service confirms a service before firing.
async def _instance_missing(environment_id: str, service_id: str,
                            action: str) -> str | None:
    """Return the refusal to send back, or None when the instance exists.

    Two different failures are reported apart, because they need different
    answers: Railway would not tell us (bad token, wrong account, outage) and
    Railway told us there is nothing there. Both refuse — neither is a reason
    to fire a write we cannot confirm landed.
    """
    try:
        data = await _query("""query($sid: String!, $eid: String!) {
          serviceInstance(serviceId: $sid, environmentId: $eid) {
            serviceId serviceName
          }
        }""", {"sid": service_id, "eid": environment_id})
    except RuntimeError as exc:
        return json.dumps({
            "error": f"Railway would not confirm service {service_id} in "
                     f"environment {environment_id}: {exc}. Nothing was "
                     f"changed — {action} looks the service instance up first "
                     "and will not write settings it cannot read back."})
    if not data.get("serviceInstance"):
        return json.dumps({
            "error": f"Service {service_id} has no instance in environment "
                     f"{environment_id}, so there is nothing to configure "
                     f"there. Nothing was changed. The service may exist in "
                     "the project and only in OTHER environments — check "
                     "list_services, whose `instances` name the environments "
                     "it is actually in, or create it in this one with "
                     "create_service."})
    return None


# Railway's serviceInstanceUpdate is a Boolean field. It is only inspected for
# an explicit `false`, never for absence: a null or missing value is left to
# behave exactly as it did when the result was discarded, so this can only add
# a refusal, never invent one.
def _update_rejected(data: dict, environment_id: str, service_id: str) -> str | None:
    if data.get("serviceInstanceUpdate") is False:
        return json.dumps({
            "error": f"Railway rejected the update for service {service_id} in "
                     f"environment {environment_id} (serviceInstanceUpdate "
                     "returned false). Nothing was changed."})
    return None


# `serviceInstanceUpdate` answers `true` for a write it accepted and then
# ignored, so its boolean is not evidence that anything changed — which is how
# set_region reported success for two months while changing nothing. Reading
# the field back is the only evidence available, and it is immediate: a
# healthcheckPath, a buildCommand and a numReplicas written through this same
# mutation all read back correctly within the same call sequence, so there is
# no lag to tolerate and no retry to add.
#
# Generic on purpose, and now used by three tools: `set_region`, where Railway
# drops the write whatever is sent, and `set_build_command`/`set_healthcheck`,
# where it keeps a value and drops an explicit null — so the clear is the half
# that used to report a success nobody had checked. Both call it for every
# write, not only the clear: a tool that echoes back a value it did not read is
# guessing regardless of which value it is, and the extra read is one query.
async def _write_unconfirmed(environment_id: str, service_id: str, field: str,
                             expected, action: str, hint: str = "") -> str | None:
    """Re-read one service-instance field after a write. None when it landed.

    Compares against `expected` — the value the caller asked for, with None
    meaning "no override". Returns the refusal to hand back otherwise, naming
    what was sent and what Railway actually reports, because "it did not work"
    without those two values sends the next reader to the wrong layer.

    A verify query that fails is reported as its own outcome rather than as
    success: the write was already sent, so the honest answer is that nobody
    knows whether it landed, not that it did.
    """
    try:
        data = await _query(f"""query($sid: String!, $eid: String!) {{
          serviceInstance(serviceId: $sid, environmentId: $eid) {{ {field} }}
        }}""", {"sid": service_id, "eid": environment_id})
    except RuntimeError as exc:
        return json.dumps({
            "error": f"{action} sent the change for service {service_id} in "
                     f"environment {environment_id}, but Railway would not "
                     f"confirm it afterwards: {exc}. The write may or may not "
                     f"have landed — read {field} with get_service_instance "
                     "before assuming either.",
            "sent": expected, "verified": False})
    observed = (data.get("serviceInstance") or {}).get(field)
    if observed == expected:
        return None
    return json.dumps({
        "error": f"{action} was accepted by Railway and then silently dropped: "
                 f"it asked for {field}={expected!r} on service {service_id} in "
                 f"environment {environment_id}, Railway's mutation answered "
                 f"success, but reading the service back immediately reports "
                 f"{field}={observed!r}. Nothing was changed." + (f" {hint}" if hint else ""),
        "sent": expected, "observed": observed, "verified": False})


@mcp.tool()
async def set_region(environment_id: str, service_id: str, region: str,
               redeploy: bool = False) -> str:
    """Set or clear the deploy region for a service in one environment.

    KNOWN BROKEN ON RAILWAY'S SIDE — this tool currently cannot move a service.
    Railway accepts the region write, answers success, and ignores it. That is
    not a claim about some past API version: verified live 2026-08-10 against
    a decommissioned service, with "europe-west4-drams3a" and with "us-west2"
    (one of the four names Railway's own staff call valid), each answered
    `updated: true` and each reading back as no override at all. Nor is it this
    mutation being broken generally — `numReplicas`, `healthcheckPath` and
    `buildCommand` written through the SAME mutation on the SAME instance read
    back correctly. The drop is specific to `region`. Setting a region
    therefore now RETURNS AN ERROR naming what was asked for and what Railway
    actually reports, instead of the success it used to invent; that error is
    the tool working as intended, and there is nothing to retry. Move a service
    from the Railway dashboard until this changes.

    region is a region `name` from list_regions (e.g. "europe-west4-drams3a");
    the short metro `id` ("ams", "sfo", …) passes Railway's validation too.
    Both are moot while the write is dropped, but the validation is real, so a
    rejection still means a bad value rather than this defect.
    Pass an empty string to CLEAR the override. A clear still reports success,
    and honestly so — it verifies the END STATE, and every service in both
    accounts already reads back as "no override" (45 of 45 checked). It does
    NOT prove a stuck region was removed: see the multiRegionConfig note below.
    The change would only take effect on the next deploy — pass redeploy=true
    to trigger one immediately; a redeploy is skipped when verification fails,
    so a dropped write never costs a pointless deployment. NB: attached volumes
    do NOT move with the service; a volume stays in its own region, so check
    list_volumes before moving a service with persistent storage — clearing the
    override is a move too, back to the default region.

    Why this is not simply pointed at a different field: region control has
    moved to `multiRegionConfig`, a JSON map of region name to replica count on
    the same input, and Railway's own rejection text points there ("clear it by
    setting its key to null in multiRegionConfig"). This server does not write
    it, for two reasons that both have to be fixed before it can. It is
    WRITE-ONLY — introspecting all 609 types in Railway's schema, nothing
    exposes multiRegionConfig for reading, so a write to it could never be
    confirmed and this tool would be back to guessing. And a wrong key in it
    BRICKS the service: a metro id instead of a region name makes every later
    deployment fail with "configured with an invalid region", a state a
    Railway employee had to clear by hand for the user who hit it. An
    unverifiable write whose failure mode is unrecoverable is worse than an
    honest refusal. That is also why a "cleared" result cannot promise a region
    stuck in multiRegionConfig is gone — nothing here can see that map.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    refusal = await _instance_missing(environment_id, service_id, "set_region")
    if refusal:
        return refusal
    # "" clears the override, sent as an explicit null — the same convention
    # set_start_command and set_service_config use on this input, and the write
    # side of the null get_service_instance reads back for "no override".
    # `region` is a nullable String on ServiceInstanceUpdateInput, so the null
    # is the reset; omitting the key would leave the override in place instead.
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id,
           "input": {"region": region or None}})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
    # The mutation's `true` means "accepted", not "applied", so it is not
    # evidence on its own — see _write_unconfirmed. Verified BEFORE any
    # redeploy: triggering a deployment to pick up a change that was never
    # stored just moves the surprise later.
    unconfirmed = await _write_unconfirmed(
        environment_id, service_id, "region", region or None, "set_region",
        hint="Railway ignores the flat `region` field on serviceInstanceUpdate; "
             "region control has moved to the write-only `multiRegionConfig`, "
             "which this server cannot safely drive. Move the service from the "
             "Railway dashboard.")
    if unconfirmed:
        return unconfirmed
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "region": region or None, "cleared": not region,
                    "updated": True, "verified": True, "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    elif region:
        result["note"] = "Region change takes effect on the next deploy."
    else:
        result["note"] = ("No region override is set; the service runs in the "
                          "default region. Read back and confirmed — but that "
                          "confirms only the readable `region` field, which is "
                          "also what a service that never had an override "
                          "reports. A region stuck in Railway's write-only "
                          "multiRegionConfig cannot be seen or cleared here.")
    return json.dumps(result)

@mcp.tool()
async def set_start_command(environment_id: str, service_id: str, start_command: str,
                      redeploy: bool = False) -> str:
    """Set or clear the custom start command for a service in one environment.

    Pass an empty string to clear the override so the service falls back to
    its Dockerfile CMD / builder default. The change only takes effect on the
    next deploy — pass redeploy=true to trigger one immediately.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    refusal = await _instance_missing(environment_id, service_id, "set_start_command")
    if refusal:
        return refusal
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id,
           "input": {"startCommand": start_command or None}})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
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
async def set_build_command(environment_id: str, service_id: str, build_command: str,
                      redeploy: bool = False) -> str:
    """Set or clear the custom build command for a service in one environment.

    The counterpart to set_start_command for the other half of the deploy:
    get_service_instance reports `buildCommand`, and this is how it is changed.
    Pass an empty string to clear the override so the service falls back to
    whatever its builder (Railpack/Nixpacks/Dockerfile) works out on its own.
    The change only takes effect on the next deploy — pass redeploy=true to
    trigger one immediately.

    CLEARING IS CURRENTLY BROKEN ON RAILWAY'S SIDE, and this tool now says so
    instead of reporting success: Railway accepts the explicit null, answers
    `true`, and leaves the stored build command exactly where it was — verified
    live 2026-08-10, while a `set_num_replicas` through the identical mutation
    seconds later landed. Setting a value works. So a clear returns an error
    naming what was asked for and what Railway still reports, there is nothing
    to retry, and the way to remove a build command today is the Railway
    dashboard. Clearing a service that has no build command to begin with is
    reported as success, honestly — the end state asked for is the end state
    read back.
    Every write is read back, not only the clear, so the value echoed in the
    answer is one this tool has seen rather than one it assumed; a verification
    that fails also skips the redeploy, so a dropped write never costs a
    pointless deployment.

    set_service_config also carries build_command, for changing it together
    with other build settings in a single write; this tool is the standalone
    one, and the two write the same field.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    refusal = await _instance_missing(environment_id, service_id, "set_build_command")
    if refusal:
        return refusal
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id,
           "input": {"buildCommand": build_command or None}})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
    # The mutation's `true` means "accepted", not "applied" — see
    # _write_unconfirmed. Checked BEFORE any redeploy: triggering a deployment
    # to pick up a change that was never stored just moves the surprise later.
    unconfirmed = await _write_unconfirmed(
        environment_id, service_id, "buildCommand", build_command or None,
        "set_build_command",
        hint="Railway drops an explicit null on this field while keeping a "
             "value, so clearing a build command through the API does nothing. "
             "Clear it from the Railway dashboard.")
    if unconfirmed:
        return unconfirmed
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "buildCommand": build_command or None, "updated": True,
                    "verified": True, "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Build-command change takes effect on the next deploy."
    return json.dumps(result)


@mcp.tool()
async def set_dockerfile_path(environment_id: str, service_id: str,
                              dockerfile_path: str, redeploy: bool = False) -> str:
    """Set or clear the Dockerfile path for a service in one environment.

    Use it to build from a Dockerfile that is not `./Dockerfile`, e.g.
    "docker/Dockerfile.web". This is the same setting as the
    `RAILWAY_DOCKERFILE_PATH` service variable; set it here rather than as a
    variable, so it does not read as application config. NB: a Dockerfile is
    selected by its presence or by this path — there is no DOCKERFILE builder
    to choose.
    Pass an empty string to clear the override, so Railway looks for
    `./Dockerfile` again. The change only takes effect on the next deploy —
    pass redeploy=true to trigger one immediately.

    set_service_config carries dockerfile_path too, for changing it together
    with other build settings in a single write; this tool is the standalone
    one, and the two write the same field.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    refusal = await _instance_missing(environment_id, service_id, "set_dockerfile_path")
    if refusal:
        return refusal
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id,
           "input": {"dockerfilePath": dockerfile_path or None}})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "dockerfilePath": dockerfile_path or None, "updated": True,
                    "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Dockerfile-path change takes effect on the next deploy."
    return json.dumps(result)


@mcp.tool()
async def set_root_directory(environment_id: str, service_id: str,
                             root_directory: str, redeploy: bool = False) -> str:
    """Set or clear the build root directory for a service in one environment.

    The monorepo setting: point the service at a subdirectory of the repo, e.g.
    "apps/api", and everything else (builder detection, Dockerfile lookup,
    watch patterns) is resolved from there.
    Pass an empty string to clear the override, so the service builds from the
    repository root again. The change only takes effect on the next deploy —
    pass redeploy=true to trigger one immediately.

    set_service_config carries root_directory too, for changing it together
    with other build settings in a single write; this tool is the standalone
    one, and the two write the same field.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    refusal = await _instance_missing(environment_id, service_id, "set_root_directory")
    if refusal:
        return refusal
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id,
           "input": {"rootDirectory": root_directory or None}})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "rootDirectory": root_directory or None, "updated": True,
                    "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Root-directory change takes effect on the next deploy."
    return json.dumps(result)


@mcp.tool()
async def set_healthcheck(environment_id: str, service_id: str,
                          healthcheck_path: str,
                          healthcheck_timeout: int | None = None,
                          redeploy: bool = False) -> str:
    """Set or clear the healthcheck for a service in one environment.

    healthcheck_path is the path Railway polls before a new deployment takes
    over traffic, e.g. "/healthz"; without one, a deployment goes live as soon
    as the container starts. Pass an empty string to clear the healthcheck
    again.
    healthcheck_timeout is how many seconds Railway keeps trying before giving
    up on the deployment. Omitting it leaves the stored timeout untouched — so
    changing only the timeout means passing the CURRENT path alongside it (read
    it with get_service_instance), and clearing the path leaves the timeout
    where it is, harmlessly, since nothing polls.
    The change only takes effect on the next deploy — pass redeploy=true to
    trigger one immediately.

    CLEARING THE PATH IS CURRENTLY BROKEN ON RAILWAY'S SIDE, and this tool now
    says so instead of reporting success: Railway accepts the explicit null,
    answers `true`, and leaves the stored path exactly where it was — verified
    live 2026-08-10, the same defect set_build_command has, while a value
    written through the identical mutation lands. So a clear returns an error
    naming what was asked for and what Railway still reports, there is nothing
    to retry, and the way to remove a healthcheck today is the Railway
    dashboard. Clearing a service that has no healthcheck to begin with is
    reported as success, honestly — the end state asked for is the end state
    read back.
    The path is read back on every write, not only the clear. The TIMEOUT is
    not: it has no clear, an omitted key is untouched rather than null, and
    nothing suggests Railway drops it — so `healthcheckTimeout` in the answer
    is still only what was sent. Read it with get_service_instance if it
    matters.

    set_service_config carries healthcheck_path and healthcheck_timeout too,
    for changing them together with other deploy settings in a single write;
    this tool is the standalone one, and the two write the same fields.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    refusal = await _instance_missing(environment_id, service_id, "set_healthcheck")
    if refusal:
        return refusal
    # "" clears the path as an explicit null; the timeout is only sent when the
    # caller passed one, because an omitted key means "untouched" here — the
    # same split set_service_config makes between None and "".
    payload: dict = {"healthcheckPath": healthcheck_path or None}
    if healthcheck_timeout is not None:
        payload["healthcheckTimeout"] = healthcheck_timeout
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id, "input": payload})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
    # Same read-back as set_build_command, and for the same reason: the clear
    # is dropped while a value is kept, and the mutation says `true` either
    # way. Only the path is verified — see the docstring on the timeout.
    unconfirmed = await _write_unconfirmed(
        environment_id, service_id, "healthcheckPath", healthcheck_path or None,
        "set_healthcheck",
        hint="Railway drops an explicit null on this field while keeping a "
             "value, so clearing a healthcheck through the API does nothing. "
             "Clear it from the Railway dashboard." + (
                 " A healthcheck_timeout was sent in the same write and is not "
                 "read back, so it may have been stored even though the path "
                 "was not — check get_service_instance."
                 if healthcheck_timeout is not None else ""))
    if unconfirmed:
        return unconfirmed
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "healthcheckPath": healthcheck_path or None, "updated": True,
                    "verified": True, "redeployed": False}
    if healthcheck_timeout is not None:
        result["healthcheckTimeout"] = healthcheck_timeout
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Healthcheck change takes effect on the next deploy."
    return json.dumps(result)


@mcp.tool()
async def set_num_replicas(environment_id: str, service_id: str,
                           num_replicas: int, redeploy: bool = False) -> str:
    """Set how many replicas of a service run in one environment.

    Horizontal scaling: Railway runs this many identical containers of the
    service behind its load balancer. Read the current count back as
    `numReplicas` from get_service_instance.
    Unlike the string settings there is no "clear" — a replica count is always
    a number, and 1 is Railway's default. The change only takes effect on the
    next deploy — pass redeploy=true to trigger one immediately.

    set_service_config carries num_replicas too, for changing it together with
    other deploy settings in a single write; this tool is the standalone one,
    and the two write the same field.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    refusal = await _instance_missing(environment_id, service_id, "set_num_replicas")
    if refusal:
        return refusal
    # Sent as-is, never through the `or None` the string setters use: 0 is a
    # value Railway can be given, and `0 or None` would silently turn it into
    # "leave the replica count alone".
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id,
           "input": {"numReplicas": num_replicas}})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "numReplicas": num_replicas, "updated": True,
                    "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Replica-count change takes effect on the next deploy."
    return json.dumps(result)


# Railway's own enums. Sent as GraphQL enum values, so a typo is rejected by
# the API with a parse error that names neither the tool nor the argument —
# checking here turns that into a message the caller can act on.
_BUILDERS = ("RAILPACK", "NIXPACKS", "PAKETO", "HEROKU")
_RESTART_POLICIES = ("ALWAYS", "NEVER", "ON_FAILURE")


@mcp.tool()
async def set_service_config(environment_id: str, service_id: str,
                             dockerfile_path: str | None = None,
                             root_directory: str | None = None,
                             builder: str | None = None,
                             build_command: str | None = None,
                             watch_patterns: list[str] | None = None,
                             railway_config_file: str | None = None,
                             pre_deploy_command: list[str] | None = None,
                             healthcheck_path: str | None = None,
                             healthcheck_timeout: int | None = None,
                             num_replicas: int | None = None,
                             restart_policy_type: str | None = None,
                             restart_policy_max_retries: int | None = None,
                             cron_schedule: str | None = None,
                             sleep_application: bool | None = None,
                             redeploy: bool = False) -> str:
    """Set several build and deploy settings for a service in one write —
    builder, Dockerfile path, root directory, build/pre-deploy commands, watch
    patterns, railway config file, healthcheck, replicas, restart policy, cron
    schedule and sleep — each also readable with get_service_instance; the
    commonly-changed ones have standalone tools (set_build_command,
    set_dockerfile_path, set_root_directory, set_healthcheck,
    set_num_replicas, set_start_command, set_region, set_variables) that write
    the same fields. This is the dashboard's Settings tab in one call; reach
    for it when several settings change together, or for the ones with no tool
    of their own.

    Every setting is optional and **omitting one leaves it untouched**; only
    the arguments actually passed are sent to Railway. For the string
    settings, passing an empty string CLEARS the override instead, so the
    service falls back to Railway's default. Read the current values back with
    get_service_instance.

    - dockerfile_path — build from a Dockerfile that is not `./Dockerfile`,
      e.g. "docker/Dockerfile.web". This is the same setting as the
      `RAILWAY_DOCKERFILE_PATH` service variable; set it here rather than as a
      variable, so it does not read as application config.
    - root_directory — build from a subdirectory of the repo (monorepos).
    - builder — one of RAILPACK, NIXPACKS, PAKETO, HEROKU. NB: there is no
      DOCKERFILE builder; a Dockerfile is picked up by its presence (or by
      dockerfile_path), not by choosing a builder.
    - watch_patterns — only redeploy when these paths change, e.g.
      ["apps/api/**"]. An empty list clears the filter (redeploy on any change).
    - pre_deploy_command — a LIST of strings, run before the new deployment
      goes live (migrations); an empty list clears it.
    - restart_policy_type — one of ALWAYS, NEVER, ON_FAILURE.
    - sleep_application — serverless-style sleep when idle.

    Like the other setters, changes take effect on the NEXT deploy — pass
    redeploy=true to trigger one immediately.

    Refuses, and writes nothing, when the service has no instance in that
    environment — Railway accepts the write silently in that case, so the
    instance is confirmed first."""
    if builder and builder.upper() not in _BUILDERS:
        return json.dumps({"error": (
            f"builder must be one of {', '.join(_BUILDERS)} (got {builder!r}). "
            "There is no DOCKERFILE builder — a Dockerfile is picked up by its "
            "presence, or by dockerfile_path.")})
    if restart_policy_type and restart_policy_type.upper() not in _RESTART_POLICIES:
        return json.dumps({"error": (
            f"restart_policy_type must be one of {', '.join(_RESTART_POLICIES)} "
            f"(got {restart_policy_type!r})")})

    # An empty string clears a string setting (sent as null); an empty list
    # clears a list setting (sent as []) — an empty list is a meaningful value
    # to Railway, so it must not be collapsed to null the way "" is.
    strings = {
        "dockerfilePath": dockerfile_path,
        "rootDirectory": root_directory,
        "buildCommand": build_command,
        "railwayConfigFile": railway_config_file,
        "healthcheckPath": healthcheck_path,
        "cronSchedule": cron_schedule,
    }
    enums = {
        "builder": builder.upper() if builder else builder,
        "restartPolicyType": (restart_policy_type.upper() if restart_policy_type
                              else restart_policy_type),
    }
    others = {
        "watchPatterns": watch_patterns,
        "preDeployCommand": pre_deploy_command,
        "healthcheckTimeout": healthcheck_timeout,
        "numReplicas": num_replicas,
        "restartPolicyMaxRetries": restart_policy_max_retries,
        "sleepApplication": sleep_application,
    }
    payload: dict = {k: (v or None) for k, v in {**strings, **enums}.items()
                     if v is not None}
    payload.update({k: v for k, v in others.items() if v is not None})

    if not payload:
        return json.dumps({"error": (
            "No settings given, so nothing was changed. Pass at least one of: "
            "dockerfile_path, root_directory, builder, build_command, "
            "watch_patterns, railway_config_file, pre_deploy_command, "
            "healthcheck_path, healthcheck_timeout, num_replicas, "
            "restart_policy_type, restart_policy_max_retries, cron_schedule, "
            "sleep_application.")})

    # After the local checks above, so a malformed or empty call is still
    # answered without a round trip, and before the write, so a missing
    # instance is refused instead of reported as applied.
    refusal = await _instance_missing(environment_id, service_id, "set_service_config")
    if refusal:
        return refusal
    data = await _query("""mutation($sid: String!, $eid: String!, $input: ServiceInstanceUpdateInput!) {
      serviceInstanceUpdate(serviceId: $sid, environmentId: $eid, input: $input)
    }""", {"sid": service_id, "eid": environment_id, "input": payload})
    rejected = _update_rejected(data, environment_id, service_id)
    if rejected:
        return rejected
    result: dict = {"serviceId": service_id, "environmentId": environment_id,
                    "applied": payload, "updated": True, "redeployed": False}
    if redeploy:
        await _query("""mutation($sid: String!, $eid: String!) {
          serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
        }""", {"sid": service_id, "eid": environment_id})
        result["redeployed"] = True
    else:
        result["note"] = "Settings take effect on the next deploy."
    return json.dumps(result)


@mcp.tool()
async def create_service(project_id: str, environment_id: str, name: str,
                         repo: str = "", image: str = "", branch: str = "") -> str:
    """Create a new Railway service inside a project/environment.

    Optionally give it a source in the same call, so the service is deployable
    immediately instead of being an empty shell that has to be finished in the
    dashboard:

    - repo — a GitHub repo as "owner/name" (the account's GitHub App must
      already have access to it), optionally with `branch`.
    - image — a public Docker image as "gotenberg/gotenberg:8". No repo, no
      Dockerfile and no build: Railway pulls and runs it.

    repo and image are mutually exclusive. Passing neither creates the empty
    service the old signature did; attach a source later with connect_service.
    Build settings (Dockerfile path, root directory, healthcheck) are not part
    of this mutation — set them with set_service_config afterwards."""
    if repo and image:
        return json.dumps({"error": "Pass repo OR image, not both — a Railway "
                                    "service has one source. Nothing was created."})
    if branch and not repo:
        return json.dumps({"error": "branch applies to a repo source only. Pass "
                                    "repo as well, or drop branch. Nothing was "
                                    "created."})
    # project_id is a required argument, so it cannot be absent — but it can be
    # empty, which is what an agent sends when it expected the same pinned
    # default the listings accept. Railway then answers the mutation with a
    # refusal about the input, and the reader hunts through the source and the
    # name before noticing the empty id. Refuse first, in the shared wording,
    # rather than paying a round trip to be told something less clear.
    if not project_id:
        return _no_project("create_service", extra="Nothing was created. "
                           "create_service takes no pinned default: the "
                           "project has to be named in the call.")
    payload: dict = {
        "projectId": project_id,
        "environmentId": environment_id,
        "name": name,
    }
    if repo or image:
        payload["source"] = {"repo": repo} if repo else {"image": image}
    if branch:
        payload["branch"] = branch
    data = await _query("""mutation($input: ServiceCreateInput!) {
      serviceCreate(input: $input) {
        id
        name
      }
    }""", {"input": payload})
    return json.dumps(data["serviceCreate"])

@mcp.tool()
async def connect_service(service_id: str, repo: str = "", branch: str = "master",
                          image: str = "") -> str:
    """Point an existing Railway service at a source.

    Either a GitHub repo/branch for auto deploys (`repo` as "owner/name"), or a
    Docker image (`image` as "gotenberg/gotenberg:8") that Railway pulls and
    runs with no build step. The two are mutually exclusive, and `branch` is
    ignored for an image source.

    This REPLACES the service's current source."""
    if repo and image:
        return json.dumps({"error": "Pass repo OR image, not both — a Railway "
                                    "service has one source. Nothing was changed."})
    if not repo and not image:
        return json.dumps({"error": "Nothing to connect: pass repo (a GitHub "
                                    "'owner/name') or image (a Docker image "
                                    "reference)."})
    payload = {"image": image} if image else {"repo": repo, "branch": branch}
    data = await _query("""mutation($id: String!, $input: ServiceConnectInput!) {
      serviceConnect(id: $id, input: $input) {
        id
        name
      }
    }""", {"id": service_id, "input": payload})
    return json.dumps(data["serviceConnect"])

@mcp.tool()
async def list_environments(project_id: str) -> str:
    """List environments in a Railway project."""
    data = await _query("""query($id: String!) {
      project(id: $id) { environments { edges { node { id name } } } }
    }""", {"id": project_id})
    envs = [e["node"] for e in data["project"]["environments"]["edges"]]
    return json.dumps(envs)

# ── environment lifecycle ────────────────────────────────────────────
#
# An ephemeral preview environment is created and destroyed by a script, with
# no human between the request and the mutation — so environmentDelete is the
# second irreversible operation in this server, next to serviceDelete, and it
# takes MORE with it: every service instance in the environment, its variables,
# its deployments and its volumes' data. Railway's own mutation is a bare
# `environmentDelete(id:)` that asks nothing and confirms nothing.
#
# Hence two layers before it fires. The environment is read back first, so an
# id Railway will not confirm deletes nothing (same guard as delete_service).
# Then the name is judged: an environment whose name carries a throwaway prefix
# — or that Railway itself marks isEphemeral — is what this tool exists for and
# goes without ceremony, while anything else needs confirm_permanent_delete
# spelled out. `production` and `test`, and whichever environment the project
# names as its base or primary one, are refused whatever the flag says: those
# are the environments a preview script has no business touching, and a flag
# that can unlock them is a flag that will be passed by a retry loop.
_EPHEMERAL_PREFIXES = ("pr-", "preview-", "ephemeral-")

# Names that are never deletable through this tool, regardless of arguments.
# Compared case-insensitively against the environment's own name.
_PROTECTED_ENVIRONMENT_NAMES = ("production", "test")


@mcp.tool()
async def create_environment(name: str, project_id: str = "",
                             source_environment_id: str = "",
                             ephemeral: bool = False) -> str:
    """Create a Railway environment, optionally as a copy of an existing one.

    With source_environment_id (an id from list_environments) Railway DUPLICATES
    that environment: its services, their configuration and their variables are
    copied into the new one. Without it the environment starts empty. This is
    the create half of an ephemeral-preview workflow — one environment per pull
    request or per card, deleted again with delete_environment.

    `ephemeral` marks the environment as throwaway in Railway's own model
    (readable as isEphemeral, and how Railway's PR environments are tagged). It
    changes nothing about how services run; it is a label, and delete_environment
    reads it.

    Creation is ASYNCHRONOUS on Railway's side: this returns as soon as the
    environment exists, which is BEFORE the copied services and their
    deployments necessarily do. Poll list_services (with the new environment's
    id) or list_environments until what you need is there — do not assume the
    clone is complete because this call succeeded.

    project_id may be omitted only if this server pins a default project."""
    pid = _pid(project_id)
    if not pid:
        return _no_project(f"Creating the environment {name!r}",
                           extra="Nothing was created.")
    if not name.strip():
        return json.dumps({"error": "An environment needs a name: pass a "
                                    "non-empty `name`. Nothing was created."})

    payload: dict = {"projectId": pid, "name": name}
    if source_environment_id:
        payload["sourceEnvironmentId"] = source_environment_id
    if ephemeral:
        payload["ephemeral"] = True

    data = await _query("""mutation($input: EnvironmentCreateInput!) {
      environmentCreate(input: $input) {
        id
        name
        projectId
        isEphemeral
        createdAt
      }
    }""", {"input": payload})
    created = data["environmentCreate"]
    return json.dumps({
        **created,
        "clonedFrom": source_environment_id or None,
        "note": "The environment exists. Railway copies services and starts "
                "their deployments in the background, so it may still be empty "
                "for a while — confirm with list_services / list_environments "
                "rather than assuming the clone finished."
        if source_environment_id else
        "The environment exists and is empty — it has no services until one is "
        "created in it.",
    })


@mcp.tool()
async def delete_environment(environment_id: str,
                             confirm_permanent_delete: bool = False) -> str:
    """PERMANENTLY DELETE one Railway environment. Irreversible — no undo, and
    nothing is moved to a trash first.

    What goes with it: every service instance in that environment, all of their
    VARIABLES, all of their deployments and logs, and every VOLUME attached to a
    service in it together with the data on it. Services themselves survive in
    the project's other environments; anything that existed only here does not.

    Intended for the ephemeral environments create_environment makes. An
    environment whose name starts with pr-, preview- or ephemeral-, or that
    Railway marks isEphemeral, is deleted directly. Any OTHER environment is
    refused unless confirm_permanent_delete=True is passed as well — deleting a
    long-lived environment is a decision, not a cleanup step.

    `production` and `test`, and whichever environment the project names as its
    base or primary one, are REFUSED in every case, confirm_permanent_delete
    included. The environment is looked up before the delete fires, so an id
    Railway cannot confirm deletes nothing."""
    if not environment_id:
        return json.dumps({"error": "Name the environment to delete: pass "
                                    "environment_id (from list_environments). "
                                    "delete_environment never chooses one for you."})

    try:
        data = await _query("""query($id: String!) {
          environment(id: $id) { id name projectId isEphemeral }
        }""", {"id": environment_id})
    except RuntimeError as exc:
        return json.dumps({
            "error": f"Railway would not confirm environment {environment_id}: "
                     f"{exc}. Nothing was deleted — delete_environment looks the "
                     "environment up first and will not fire at an id it could "
                     "not read back."})
    target = data.get("environment")
    if not target:
        return json.dumps({
            "error": f"No environment with id {environment_id}. Nothing was deleted."})

    env_name = (target.get("name") or "").strip()
    if env_name.lower() in _PROTECTED_ENVIRONMENT_NAMES:
        return json.dumps({
            "error": f"Refusing to delete the environment named {env_name!r} "
                     f"({environment_id}). Nothing was deleted. "
                     f"{', '.join(_PROTECTED_ENVIRONMENT_NAMES)} are protected "
                     "here whatever confirm_permanent_delete says — delete such "
                     "an environment in the Railway dashboard, where a human "
                     "sees the request."})

    # The project's own idea of its default environment, which is not always
    # called "production". A refused or unreadable project lookup must not
    # unlock the delete, so it is treated as "cannot rule this out".
    try:
        project = await _query("""query($id: String!) {
          project(id: $id) { id name baseEnvironmentId primaryEnvironmentId }
        }""", {"id": target.get("projectId") or ""})
        proj = project.get("project") or {}
    except RuntimeError as exc:
        return json.dumps({
            "error": f"Railway would not confirm the project environment "
                     f"{environment_id} belongs to: {exc}. Nothing was deleted — "
                     "without the project this cannot tell a throwaway "
                     "environment from the project's default one."})
    defaults = {proj.get("baseEnvironmentId"), proj.get("primaryEnvironmentId")}
    if environment_id in defaults:
        return json.dumps({
            "error": f"Environment {env_name!r} ({environment_id}) is the default "
                     f"environment of project {proj.get('name') or proj.get('id')}. "
                     "Nothing was deleted — the project's own environment is "
                     "protected here whatever confirm_permanent_delete says."})

    looks_ephemeral = (bool(target.get("isEphemeral"))
                       or env_name.lower().startswith(_EPHEMERAL_PREFIXES))
    if not looks_ephemeral and not confirm_permanent_delete:
        return json.dumps({
            "error": f"Environment {env_name!r} ({environment_id}) does not look "
                     "like a throwaway one: its name starts with none of "
                     f"{', '.join(_EPHEMERAL_PREFIXES)} and Railway does not mark "
                     "it ephemeral. Nothing was deleted. If deleting it really is "
                     "intended — with all of its variables, deployments and "
                     "volume data — call again with "
                     "confirm_permanent_delete=true.",
            "environmentName": env_name,
            "isEphemeral": bool(target.get("isEphemeral"))})

    result = await _query("""mutation($id: String!) {
      environmentDelete(id: $id)
    }""", {"id": environment_id})
    return json.dumps({
        "environmentId": environment_id,
        "environmentName": env_name,
        "projectId": target.get("projectId"),
        "deleted": result.get("environmentDelete"),
        "note": f"Environment {env_name} ({environment_id}) was permanently "
                "deleted, with the service instances in it, their variables, "
                "deployments and volume data. This cannot be undone."})


@mcp.tool()
async def update_deployment_trigger(environment_id: str, service_id: str,
                                    branch: str, project_id: str = "") -> str:
    """Point a service's auto-deploy trigger at a different GIT BRANCH, in ONE
    environment.

    This is what makes a cloned environment build something other than what it
    was cloned from: the copy inherits the source environment's branch, and this
    repoints it. A deployment trigger belongs to one service in one environment,
    so this changes nothing for that service anywhere else.

    It rewrites configuration only — no build is started here. Use
    create_deployment to deploy the new branch now; otherwise the next push to
    it is what deploys.

    The trigger is resolved from service_id + environment_id first. If the
    service has no trigger there (a service deployed from an image, or one never
    connected to a repo) or somehow more than one, this REFUSES and changes
    nothing rather than guessing; creating a trigger where none exists is a
    different operation and is deliberately not exposed here.

    project_id may be omitted only if this server pins a default project."""
    pid = _pid(project_id)
    if not pid:
        return _no_project("Finding the deployment trigger to update",
                           extra="Nothing was changed.")
    if not branch.strip():
        return json.dumps({"error": "Pass the branch to deploy from — "
                                    "update_deployment_trigger writes `branch` "
                                    "and an empty one means nothing. Nothing "
                                    "was changed."})

    data = await _query("""query($pid: String!, $eid: String!, $sid: String!) {
      deploymentTriggers(projectId: $pid, environmentId: $eid, serviceId: $sid) {
        edges { node { id branch repository serviceId environmentId } }
      }
    }""", {"pid": pid, "eid": environment_id, "sid": service_id})
    triggers = [e["node"] for e in
                (data.get("deploymentTriggers") or {}).get("edges", [])]

    if not triggers:
        return json.dumps({
            "error": f"Service {service_id} has no deployment trigger in "
                     f"environment {environment_id}, so there is no branch to "
                     "change. Nothing was changed — this happens when the "
                     "service deploys from a Docker image, or was never "
                     "connected to a GitHub repo (connect_service does that).",
            "projectId": pid})
    if len(triggers) > 1:
        return json.dumps({
            "error": f"Service {service_id} has {len(triggers)} deployment "
                     f"triggers in environment {environment_id}, so this does "
                     "not identify one. Nothing was changed.",
            "triggers": triggers})

    trigger = triggers[0]
    updated = await _query("""mutation($id: String!, $input: DeploymentTriggerUpdateInput!) {
      deploymentTriggerUpdate(id: $id, input: $input) {
        id
        branch
        repository
        serviceId
        environmentId
      }
    }""", {"id": trigger["id"], "input": {"branch": branch}})
    return json.dumps({
        "trigger": updated["deploymentTriggerUpdate"],
        "previousBranch": trigger.get("branch"),
        "note": "Configuration only — nothing was deployed. Use "
                "create_deployment to build this branch now, or let the next "
                "push to it deploy."})


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

async def _variable_names(project_id: str, environment_id: str,
                          service_id: str) -> set[str]:
    """The keys currently in one variable collection, values discarded here.

    The same `variables` query list_variables and check_variable read, kept to
    the same rule: a value is never returned to the caller and never logged.
    """
    data = await _query("""query($pid: String!, $eid: String!, $sid: String!) {
      variables(projectId: $pid, environmentId: $eid, serviceId: $sid)
    }""", {"pid": project_id, "eid": environment_id, "sid": service_id})
    return set(data.get("variables") or {})


# `variableCollectionUpsert` writes a whole COLLECTION, and its `replace` field
# decides whether the dict handed over is merged into the stored one or becomes
# it. The field used not to be sent at all, so the outcome rested entirely on
# Railway's default — invisible from here, and the difference between setting
# one variable and deleting every secret on the service. A project's deploy
# routine that nudges a rebuild by writing a dummy `DEPLOY_NUDGE` is one
# `replace` default away from wiping the service it is nudging.
#
# Introspection settles what that default is (Railway answers introspection
# without a token, which is how the region and environment notes above were
# established too): on `VariableCollectionUpsertInput`, `replace` is a nullable
# Boolean whose `defaultValue` is `"false"`, described as "When set to true,
# removes all existing variables before upserting the new collection". The old
# silence was therefore a merge — the safe half — but silence is not the same
# as a decision, so the field is now sent explicitly in BOTH states: a
# server-side default is the vendor's to change, and this one turns a write
# into a wipe.
@mcp.tool()
async def set_variables(project_id: str, environment_id: str,
                  service_id: str, variables: dict[str, str],
                  replace: bool = False) -> str:
    """Add or update variables on a Railway service, keeping the rest.

    `variables` addresses the service's WHOLE variable collection, not only the
    keys named in it, so what happens to the keys left out is this tool's most
    important behaviour. With `replace=False` (the default) they are kept and
    the given keys are merged in, so passing one key sets one key. With
    `replace=True` the given dict BECOMES the collection and every key not
    listed is deleted — credentials included, with no undo and no copy kept by
    Railway. Ask for that by name only when wiping the collection is the point.

    Values are never returned. The answer is names only: `keysSet` (what was
    sent), `keysNow` (the whole collection read back afterwards) and
    `keysRemoved` (present before the write, gone after — empty for a merge).
    For lengths and truncated hashes per key, use list_variables, or
    check_variable for one key; neither moves a value either.

    The collection is read before and after the write, so a key that failed to
    land, and a Railway that will not answer, are reported as themselves rather
    than as a success nobody checked.
    """
    try:
        before = await _variable_names(project_id, environment_id, service_id)
    except RuntimeError as exc:
        return json.dumps({
            "error": f"Railway would not list the variables already on service "
                     f"{service_id} in environment {environment_id}: {exc}. "
                     "Nothing was changed — set_variables reads the collection "
                     "first, because the write addresses all of it and a "
                     "replace deletes whatever it could not see.",
            "verified": False})

    result = await _query("""mutation($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }""", {"input": {
        "projectId": project_id, "environmentId": environment_id,
        "serviceId": service_id, "variables": variables,
        "replace": replace,
    }})
    if result.get("variableCollectionUpsert") is False:
        return json.dumps({
            "error": f"Railway rejected the variable write for service "
                     f"{service_id} in environment {environment_id} "
                     "(variableCollectionUpsert returned false). Nothing was "
                     "changed.",
            "keysSet": sorted(variables), "verified": False})

    try:
        after = await _variable_names(project_id, environment_id, service_id)
    except RuntimeError as exc:
        return json.dumps({
            "error": f"set_variables sent {len(variables)} variable(s) to "
                     f"service {service_id} in environment {environment_id} "
                     f"with replace={replace}, but Railway would not read the "
                     f"collection back afterwards: {exc}. The write may or may "
                     "not have landed — list the keys with list_variables "
                     "before assuming either.",
            "keysSet": sorted(variables), "verified": False})

    missing = sorted(set(variables) - after)
    if missing:
        return json.dumps({
            "error": f"set_variables was accepted by Railway and then silently "
                     f"dropped: it set {sorted(variables)} on service "
                     f"{service_id} in environment {environment_id}, Railway's "
                     "mutation answered success, but reading the collection "
                     f"back immediately does not report {missing}. Treat the "
                     "whole write as unlanded and check the collection in the "
                     "Railway dashboard.",
            "keysSet": sorted(variables), "keysMissing": missing,
            "keysNow": sorted(after), "verified": False})

    return json.dumps({
        "updated": True,
        "replace": replace,
        "keysSet": sorted(variables),
        "keysNow": sorted(after),
        "keysRemoved": sorted(before - after),
        "verified": True,
    })

# Statuses of a deployment that has been RELEASED and still holds the service's
# container — i.e. the one a request to the service reaches right now. Anything
# else either never got that far (FAILED, SKIPPED, BUILDING, DEPLOYING, QUEUED…)
# or has already handed over (REMOVED, REMOVING): Railway supersedes the
# previous deployment only once a new one succeeds, which is precisely why a
# failed build leaves the OLD deployment serving traffic.
#
# Same three values as _RESTARTABLE_STATUSES / _STOPPABLE_STATUSES, kept
# separate for the reason those two are: they answer different questions and
# Railway is free to move one without the others. CRASHED counts here because
# the service is still ON that deployment — it is the release whose logs matter,
# even though it is failing to stay up.
#
# Status alone is never enough: a deployment stopped by stop_service keeps the
# status it had (Railway has no STOPPED status) and is flagged deploymentStopped,
# so that flag has to be read too or a stopped service reads as a running one.
_LIVE_STATUSES = ("SUCCESS", "SLEEPING", "CRASHED")


def _running_deployment(nodes: list[dict]) -> dict | None:
    """The deployment the service is actually running, newest first, or None."""
    return next((d for d in nodes
                 if d["status"] in _LIVE_STATUSES and not d.get("deploymentStopped")),
                None)


# ── a deployment status is not a container ───────────────────────────
#
# _running_deployment answers "which of these deployments is the one the
# service is on" — a question about the DEPLOYMENT LIST, and the only question
# Railway's deployment fields can answer. It cannot answer "does a container
# exist", and for five months in 2026 a production Postgres proved the two are
# different: latestDeployment SUCCESS, deploymentStopped false, get_logs
# reporting deploymentIsRunning true, and no container anywhere. Every status
# field agreed, the dependent API had been unable to connect for three days,
# and the outage read as healthy through the whole tool surface.
#
# What did reveal it was resource usage: CPU and memory flat at zero across
# every sample. A live container cannot use zero memory — a process that exists
# occupies some — so a window of samples that are all zero is positive evidence
# of absence, and it is the cheapest such evidence Railway's API offers (one
# metrics query, the same one get_metrics uses). A TCP/protocol probe settles it
# harder but needs a public address, a protocol implementation per service type
# and a real connection attempt; that does not belong in a general read tool.
#
# The probe is deliberately three-valued, and only one value is an accusation:
#   resource-use-seen — a non-zero sample. The container is up. Certain.
#   no-resource-use   — samples exist for the window and every one is zero.
#                       Nothing is running. This is the incident's signature.
#   not-checked       — no samples at all, a metrics query Railway refused, a
#                       deployment too young to have reported yet, or a
#                       SLEEPING one (which has no container BY DESIGN and says
#                       so in its status). Nothing is claimed either way.
# "No samples" stays not-checked on purpose: it is also what an unavailable
# metrics backend looks like, and a health check that cries wolf gets ignored
# exactly like one that never fires.
_CONTAINER_PROBE_WINDOW_SECONDS = 1800

# A container that has only just started has not reported a sample yet, so a
# fresh deployment would probe as zero and be accused of not running. Below
# this age the probe declines to answer instead.
_CONTAINER_PROBE_MIN_AGE_SECONDS = 600

# Only measurements a CONTAINER produces belong here. CPU and memory stop dead
# when the process is gone, which is the whole basis of the probe; DISK_USAGE_GB
# and BACKUP_USAGE_GB describe the volume and keep reporting the same non-zero
# number with no container anywhere (and come back tagged deploymentId: null —
# see the note above get_metrics). Adding either would turn a stored byte into
# "resource-use-seen" and silence the accusation this probe exists to make.
_CONTAINER_PROBE_MEASUREMENTS = ("CPU_USAGE", "MEMORY_USAGE_GB")


async def _container_probe(project_id: str, environment_id: str, service_id: str,
                           deployment: dict) -> dict:
    """Does this service actually have a container? Evidence, not status.

    Reads the last half hour of CPU and memory samples and reports what it
    found. Never raises: a probe that cannot run returns "not-checked", because
    it exists to add certainty to an answer and must never be able to take the
    answer away.
    """
    if deployment.get("status") == "SLEEPING":
        return {"verdict": "not-checked",
                "reason": "The deployment is SLEEPING, which means Railway has "
                          "removed its container on purpose. Zero resource use "
                          "is the correct state and proves nothing."}

    now = datetime.now(timezone.utc)
    created = _epoch(deployment.get("createdAt") or "")
    if created is not None and now.timestamp() - created < _CONTAINER_PROBE_MIN_AGE_SECONDS:
        return {"verdict": "not-checked",
                "reason": f"The deployment was created less than "
                          f"{_CONTAINER_PROBE_MIN_AGE_SECONDS // 60} minutes ago; a "
                          "container that has just started has not reported usage "
                          "yet, so zero samples would mean nothing."}

    start = datetime.fromtimestamp(
        now.timestamp() - _CONTAINER_PROBE_WINDOW_SECONDS, timezone.utc)
    try:
        data = await _query("""query($pid: String!, $eid: String!, $sid: String!,
                              $start: DateTime!, $measurements: [MetricMeasurement!]!) {
          metrics(projectId: $pid, environmentId: $eid, serviceId: $sid,
                  startDate: $start, measurements: $measurements,
                  groupBy: [DEPLOYMENT_ID]) {
            measurement
            values { ts value }
          }
        }""", {
            "pid": project_id, "eid": environment_id, "sid": service_id,
            "start": start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "measurements": list(_CONTAINER_PROBE_MEASUREMENTS),
        })
    except RuntimeError as exc:
        return {"verdict": "not-checked",
                "reason": f"Railway would not answer the metrics query: {exc}"}

    # Aggregated across every returned series on purpose, never attributed to a
    # deployment: `groupBy: [DEPLOYMENT_ID]` splits the answer per deployment AND
    # can add a series tagged deploymentId: null (see the note above get_metrics),
    # so picking "the running deployment's series" is a guess. The question here
    # is only "did ANY container burn CPU or memory in this window", which the
    # union answers correctly whatever the tags say — hence `tags` is not even
    # selected in the query above.
    maxima: dict[str, float] = {}
    samples = 0
    for one in data.get("metrics") or []:
        numbers = [v["value"] for v in (one.get("values") or [])
                   if isinstance(v.get("value"), (int, float))]
        if not numbers:
            continue
        samples += len(numbers)
        name = one.get("measurement") or "UNKNOWN"
        maxima[name] = max(maxima.get(name, 0.0), max(numbers))

    if not samples:
        return {"verdict": "not-checked",
                "windowSeconds": _CONTAINER_PROBE_WINDOW_SECONDS,
                "samples": 0,
                "reason": "Railway returned no CPU or memory samples for this "
                          "service over the window. That is also what an "
                          "unavailable metrics backend looks like, so it is not "
                          "taken as proof either way."}

    verdict = "resource-use-seen" if any(v > 0 for v in maxima.values()) else "no-resource-use"
    probe = {"verdict": verdict,
             "windowSeconds": _CONTAINER_PROBE_WINDOW_SECONDS,
             "samples": samples,
             "maxima": maxima}
    if verdict == "no-resource-use":
        probe["reason"] = (
            f"Every one of the {samples} CPU and memory samples Railway has for "
            f"the last {_CONTAINER_PROBE_WINDOW_SECONDS // 60} minutes is zero. A "
            "running container cannot use zero memory, so nothing is running — "
            "whatever the deployment status says.")
    return probe


# ── build logs are a different query, and the one that explains a failed build ──
#
# Railway keeps a deployment's output in two places: buildLogs(deploymentId) is
# the builder's own output (dependency install, compiler, image export) and
# deploymentLogs(deploymentId) is what the CONTAINER printed. They are separate
# queries with identical arguments, and the dashboard shows them as two tabs.
#
# A build that fails never starts a container, so deploymentLogs is empty and
# the reason it failed exists only in buildLogs. Reading just deploymentLogs —
# which is all this tool did until 2026-08-06 — therefore answers a failed
# deploy with an empty list and no way to learn why, while the same tool works
# perfectly on a CRASHED deployment, whose container did run. That is the whole
# shape of the bug: it is invisible until the deploy you need to diagnose is the
# one that never got that far.
#
# Statuses of a deployment whose container is up and simply has nothing to say
# yet — a freshly deployed quiet service. For those, empty runtime logs are the
# honest answer and pulling the build output on top would only be noise; for
# every other status an empty answer means the output is in the other query.
_QUIET_IS_NORMAL_STATUSES = ("SUCCESS", "SLEEPING")


@mcp.tool()
async def get_logs(project_id: str, environment_id: str, service_id: str,
             limit: int = 50, source: str = "latest",
             build_logs: str = "auto") -> str:
    """Get recent deployment logs for a service, and say which deployment they
    came from.

    Railway's API has no deploymentLogs(projectId/environmentId/serviceId) query —
    logs are keyed by deploymentId. This looks up the service's deployments for
    the given project/environment, picks one, then fetches that deployment's logs.

    A DEPLOYMENT HAS TWO SETS OF LOGS. `logs` is what the container printed;
    `buildLogs` is what the builder printed. A build that fails never starts a
    container, so `logs` is empty and the reason for the failure is in
    `buildLogs` only — which is why an answer with no container output fetches
    the build output as well, and says so in `buildLogsNote`.

    THE NEWEST DEPLOYMENT IS NOT ALWAYS THE ONE SERVING TRAFFIC. A build that
    fails is still the newest deployment, while the service keeps running the
    previous, working one — so by default these logs can describe a version that
    no request ever reaches. The answer therefore always carries
    `deploymentIsRunning`, and when it is false it leads with a `warning` naming
    both deployments. When the logs ARE the running deployment's, there is no
    warning and nothing extra to read past.

    A DEPLOYMENT STATUS IS NOT A CONTAINER, so `deploymentIsRunning` is not
    decided by status alone. When the running deployment has printed nothing,
    the last half hour of CPU and memory samples is checked as well, and the
    result is reported in `containerCheck`. All-zero usage means no container
    exists — a running process cannot use zero memory — so `deploymentIsRunning`
    comes back FALSE with a warning, however green the deployment looks. That is
    the case a SUCCESS status hides completely: a service dead for months reads
    as healthy through every status field. No samples at all, a refused metrics
    query, a deployment younger than ten minutes or a SLEEPING one claim
    nothing either way.

    source:
      "latest"  (default, unchanged behaviour) — the most recent deployment,
                whether or not it succeeded. What you want after a deploy: the
                failed build's own output is the reason it failed.
      "running" — the deployment currently holding the service's container
                (newest one that is SUCCESS/SLEEPING/CRASHED and not stopped).
                What you want when investigating live behaviour. Refuses,
                naming the recent statuses, if nothing is running.

    build_logs:
      "auto"    (default) — also fetch the build output when the container
                printed nothing and the deployment is not simply a healthy,
                quiet one, i.e. exactly when the empty answer would otherwise
                be unexplained.
      "always"  — fetch it regardless. For a build that SUCCEEDED but is slow
                or produced something unexpected.
      "never"   — container logs only, the behaviour before 2026-08-06.
    """
    if source not in ("latest", "running"):
        return json.dumps({"error": f"Unknown source {source!r} — use \"latest\" "
                                    "(the most recent deployment, the default) or "
                                    "\"running\" (the one serving traffic)."})
    if build_logs not in ("auto", "always", "never"):
        return json.dumps({"error": f"Unknown build_logs {build_logs!r} — use "
                                    "\"auto\" (the default: the build's output is "
                                    "added when the container printed nothing), "
                                    "\"always\" or \"never\"."})

    deployments = await _query("""query($input: DeploymentListInput!) {
      deployments(input: $input, first: 10) {
        edges { node { id createdAt status deploymentStopped } }
      }
    }""", {"input": {
        "projectId": project_id, "environmentId": environment_id, "serviceId": service_id
    }})
    edges = deployments.get("deployments", {}).get("edges", [])
    if not edges:
        return json.dumps({"error": "No deployments found for this project/environment/service"})
    nodes = sorted((e["node"] for e in edges), key=lambda d: d["createdAt"], reverse=True)

    latest = nodes[0]
    running = _running_deployment(nodes)

    if source == "running":
        if running is None:
            return json.dumps({
                "error": "No running deployment for this service — its newest "
                         f"deployment is {latest['status']} and nothing older is "
                         "still holding a container, so there are no logs from a "
                         "running version. Call again without source=\"running\" "
                         "to read the newest deployment's logs instead.",
                "recentStatuses": [d["status"] for d in nodes[:5]],
            })
        target = running
    else:
        target = latest

    data = await _query("""query($did: String!, $limit: Int!) {
      deploymentLogs(deploymentId: $did, limit: $limit) {
        timestamp message
      }
    }""", {"did": target["id"], "limit": limit})

    container_logs = data.get("deploymentLogs", [])
    is_running = running is not None and running["id"] == target["id"]

    # A deployment that is printing has a container by definition, so the probe
    # is only worth its round trip on the answer that would otherwise be an
    # unexplained silence: the running deployment, saying nothing. That is
    # precisely the shape the dead Postgres had.
    probe = None
    if is_running and not container_logs:
        probe = await _container_probe(project_id, environment_id, service_id, target)
        if probe["verdict"] == "no-resource-use":
            is_running = False

    result: dict = {}
    if probe is not None and probe["verdict"] == "no-resource-use":
        result["warning"] = (
            f"This service has NO RUNNING CONTAINER, even though deployment "
            f"{target['id']} is {target['status']} and is not flagged stopped. "
            f"{probe['reason']} `logs` is empty for the same reason: there is "
            "nothing running to print anything. Nothing is serving traffic — "
            "use start_service to bring it back up, and get_metrics for the "
            "full picture.")
    elif not is_running:
        if running is None:
            result["warning"] = (
                f"These logs are NOT from a running deployment. They come from "
                f"deployment {target['id']} ({target['status']}, created "
                f"{target['createdAt']}), and this service has NO deployment "
                f"holding a container right now — it is stopped or has never "
                f"deployed successfully, so nothing is serving traffic.")
        else:
            result["warning"] = (
                f"These logs are NOT from the deployment serving traffic. They "
                f"come from deployment {target['id']} ({target['status']}, created "
                f"{target['createdAt']}), which never took over; the service is "
                f"still running deployment {running['id']} ({running['status']}, "
                f"created {running['createdAt']}). Call again with "
                f"source=\"running\" to read the logs of the version actually "
                f"serving traffic.")
    result.update({
        "deploymentId": target["id"],
        "deploymentStatus": target["status"],
        "deploymentCreatedAt": target["createdAt"],
        "deploymentIsRunning": is_running,
    })
    if probe is not None:
        result["containerCheck"] = probe
    elif not is_running:
        result["runningDeploymentId"] = running["id"] if running else None
        result["runningDeploymentStatus"] = running["status"] if running else None
    result["logs"] = container_logs

    want_build = build_logs == "always" or (
        build_logs == "auto" and not container_logs
        and target["status"] not in _QUIET_IS_NORMAL_STATUSES)
    if want_build:
        try:
            build = await _query("""query($did: String!, $limit: Int!) {
              buildLogs(deploymentId: $did, limit: $limit) {
                timestamp message
              }
            }""", {"did": target["id"], "limit": limit})
        except RuntimeError as exc:
            # Never turn an answer into an error over the extra query: the
            # container logs above are still worth returning.
            result["buildLogsNote"] = (
                f"The build output could not be read — Railway answered: {exc}")
        else:
            lines = build.get("buildLogs", [])
            result["buildLogs"] = lines
            if not container_logs:
                result["buildLogsNote"] = (
                    f"`logs` is empty because deployment {target['id']} "
                    f"({target['status']}) never reached the container stage — "
                    "read `buildLogs` instead, which is the builder's own output "
                    "and where the reason lives."
                    if lines else
                    f"Deployment {target['id']} ({target['status']}) has neither "
                    "container output nor build output. Railway kept no lines for "
                    "it — a build that was cancelled or skipped before it ran, or "
                    "one old enough that its logs have been dropped.")
    return json.dumps(result)

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


# ── a metrics series is not always a deployment's ────────────────────
#
# `groupBy: [DEPLOYMENT_ID]` reads as "one series per deployment", and for
# CPU_USAGE / MEMORY_USAGE_GB it is: each series carries the id of the
# deployment that produced it. But not every measurement Railway exposes is
# produced by a deployment, and those come back in the SAME list with
# `tags: {deploymentId: null}` — an extra, unattributed series alongside the
# real ones. Observed 2026-08-10 on the mcp.google service: one MEMORY_USAGE_GB
# series tagged with the running deployment's id, and one DISK_USAGE_GB series
# tagged null, in a single answer. The volume is service-scoped and outlives
# every deployment, so there is no id to attribute it to; the null is Railway
# being accurate, not Railway losing the tag.
#
# The consequence for anything reading this data: DO NOT ATTRIBUTE BY DEFAULT.
# Filtering to one deploymentId silently drops the untagged series, and treating
# the untagged series as "the deployment's" credits a volume's stored bytes to a
# process. Aggregate across all series unless a question genuinely needs one
# deployment's window — and when it does, filter on a deploymentId you already
# know rather than assuming the tag is populated, and expect the untagged series
# to be unusable for that question at all. `_container_probe` above is the
# working example: it unions every series and never looks at the tags.
#
# Railway has not documented the null tag; if a later API version starts
# attributing volume metrics, this note is what to re-check, not silently trust.
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
    CPU_USAGE and MEMORY_USAGE_GB if omitted. Each value is {ts, value}
    (ts = unix seconds).

    RESULTS ARE GROUPED BY DEPLOYMENT, BUT NOT EVERY SERIES HAS A DEPLOYMENT.
    CPU and memory series carry the deploymentId that produced them, so those
    can be isolated per deployment when several ran during the range. Volume-
    scoped measurements (DISK_USAGE_GB, BACKUP_USAGE_GB) belong to no deployment
    and arrive in the same list tagged `deploymentId: null`. So AGGREGATE ACROSS
    SERIES BY DEFAULT: filtering to one deploymentId quietly drops the untagged
    series, and reading the untagged series as a deployment's own usage credits a
    volume's stored bytes to a process. Attribute only when the question really
    is per-deployment, and then match on an id you already know.

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
    """RESTART the deployment a service is already running. It does NOT build,
    and it does NOT pick up new code.

    Despite the name, nothing is deployed: the container is torn down and
    started again from the SAME image that is running now. A service left
    running old code is still running old code afterwards. Use it to clear a
    wedged process, re-read a changed environment variable, or bring a crashed
    container back.

    To make new code go live, use create_deployment — that one builds the
    service's current source and releases a NEW deployment. This tool keeps its
    misleading name only because callers already use it.

    Railway's deploymentRestart takes a DEPLOYMENT id, not a service id — a
    service id passed to it is simply an id that matches no deployment, and the
    API answers "Deployment not found" even while the service is running
    happily. So resolve the service's newest restartable deployment first, with
    the same deployments(DeploymentListInput) lookup get_logs uses, and restart
    that.

    A CONTAINER THAT IS ALREADY GONE CANNOT BE RESTARTED. deploymentRestart
    answers true for such a deployment and starts nothing, which is how a dead
    service was "restarted" repeatedly while staying dead. So when the target
    deployment shows no CPU or memory use at all, this falls back to
    serviceInstanceRedeploy — the mutation start_service uses, which addresses
    the service rather than a deployment and does bring it back. It redeploys
    the same commit the service is already on, so still no new code; the answer
    says which mutation ran, in `method`, and carries the evidence in
    `containerCheck`.
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

    probe = await _container_probe(project_id, environment_id, service_id, target)
    if probe["verdict"] == "no-resource-use":
        try:
            data = await _query("""mutation($sid: String!, $eid: String!) {
              serviceInstanceRedeploy(serviceId: $sid, environmentId: $eid)
            }""", {"sid": service_id, "eid": environment_id})
        except RuntimeError as exc:
            return json.dumps({
                "error": f"This service has no running container to restart, and "
                         f"Railway refused to redeploy service {service_id} in "
                         f"environment {environment_id}: {exc}",
                "containerCheck": probe,
                "hint": "Both ids must belong to the same project — service ids "
                        "come from list_services, environment ids from "
                        "list_environments.",
            })
        return json.dumps({
            "deploymentId": target["id"],
            "deploymentStatus": target["status"],
            "restarted": data.get("serviceInstanceRedeploy"),
            "method": "serviceInstanceRedeploy",
            "containerCheck": probe,
            "note": "Restarted the SERVICE, not the deployment: the deployment "
                    "is flagged as running but has no container, and "
                    "deploymentRestart answers true for such a deployment "
                    "while starting nothing. Same commit, no build. Confirm "
                    "with get_metrics that usage has left zero.",
        })

    data = await _query("""mutation($did: String!) {
      deploymentRestart(id: $did)
    }""", {"did": target["id"]})
    return json.dumps({"deploymentId": target["id"],
                       "deploymentStatus": target["status"],
                       "restarted": data.get("deploymentRestart")})


@mcp.tool()
async def create_deployment(environment_id: str = "", service_id: str = "",
                      commit_sha: str = "") -> str:
    """DEPLOY FOR REAL: build the service's current source and release the
    result as a NEW deployment. This is the tool that makes new code go live.

    DISRUPTIVE. The service is rebuilt and its running container is replaced,
    so a live service is interrupted for the length of the build and the
    changeover, and a broken commit reaches production the moment it builds.
    There is no dry run. If the goal is merely to restart a wedged process or
    re-read a variable, deploy() is the cheaper and safer call — it restarts
    what is already running and never rebuilds.

    Railway's serviceInstanceDeployV2 addresses the SERVICE and ENVIRONMENT
    directly and returns the id of the deployment it created. It is a different
    mutation from deploymentRestart, which deploy() uses and which needs a
    DEPLOYMENT id. By default it deploys the commit currently associated with
    the service; pass commit_sha to deploy a specific one — for example the
    HEAD of the connected GitHub branch, because a plain call does NOT go and
    look for newer commits. Railway validates the sha against the connected
    repo and creates no deployment for one it does not recognise.

    ONE service per call, identified explicitly. Both ids are required and are
    never guessed: an omitted id, or an id that reads as a list or a pattern,
    is refused before anything is built.
    """
    missing = [n for n, v in (("environment_id", environment_id),
                              ("service_id", service_id)) if not v.strip()]
    if missing:
        return json.dumps({
            "error": f"Cannot deploy: {' and '.join(missing)} not given. "
                     "create_deployment never chooses a service or an "
                     "environment for you — service ids come from "
                     "list_services, environment ids from list_environments. "
                     "Nothing was built."})

    # Same guard, and the same constant, as delete_service (defined below): an
    # id carrying a wildcard or a separator means the caller had a SET of
    # services in mind. Deploying several at once is not offered, and guessing
    # which one was meant would rebuild production on a hunch.
    set_like = [c for c in _SET_LIKE_NAME_CHARS if c in service_id]
    if set_like:
        return json.dumps({
            "error": f"Refusing service_id {service_id!r}: it contains "
                     f"{''.join(set_like)!r} and so reads as a pattern or a "
                     "list of ids, not one service. create_deployment deploys "
                     "exactly one service per call. Nothing was built — deploy "
                     "them one at a time, each named in its own call."})

    variables = {"sid": service_id.strip(), "eid": environment_id.strip()}
    if commit_sha.strip():
        variables["sha"] = commit_sha.strip()
        mutation = """mutation($sid: String!, $eid: String!, $sha: String!) {
          serviceInstanceDeployV2(serviceId: $sid, environmentId: $eid, commitSha: $sha)
        }"""
    else:
        mutation = """mutation($sid: String!, $eid: String!) {
          serviceInstanceDeployV2(serviceId: $sid, environmentId: $eid)
        }"""

    try:
        data = await _query(mutation, variables)
    except RuntimeError as exc:
        # Railway reports a wrong id, a mismatched pair and an unknown commit
        # through the same generic channel, so say which of the three was
        # actually being attempted rather than passing the message through.
        result = {
            "error": f"Railway refused to deploy service {service_id} in "
                     f"environment {environment_id}: {exc}",
            "hint": "Both ids must belong to the same project — a mismatched "
                    "pair is reported exactly like a missing service, so check "
                    "the pair before concluding the service is gone. Nothing "
                    "was built.",
        }
        if commit_sha.strip():
            result["hint"] += (f" A commit_sha ({commit_sha}) Railway cannot "
                               "find in the connected repo fails the same way.")
        return json.dumps(result)

    return json.dumps({
        "serviceId": service_id.strip(),
        "environmentId": environment_id.strip(),
        "commitSha": commit_sha.strip() or None,
        "deploymentId": data.get("serviceInstanceDeployV2"),
        "note": "A NEW deployment was created and is building now; the running "
                "container is replaced when it succeeds. Follow it with "
                "get_logs, and list_services for its status."})


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
    Prefer this over delete_service whenever the goal is to stop paying for
    something: it costs nothing to undo, and delete_service cannot be undone.

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


# Characters that make a name stand for a SET rather than a service: glob
# metacharacters and the separators a caller would use to pass several names at
# once. delete_service refuses them outright instead of resolving them, because
# the whole risk here is a caller that meant one thing and got several — and the
# mistake cannot be undone afterwards.
_SET_LIKE_NAME_CHARS = "*?%,;|"


@mcp.tool()
async def delete_service(service_id: str = "", name: str = "",
                   project_id: str = "") -> str:
    """PERMANENTLY DELETE one Railway service. Irreversible — there is no undo,
    and nothing is moved to a trash first.

    What goes with it: the service in EVERY environment, all of its deployments
    and their logs, all of its environment VARIABLES, all of its DOMAINS
    (Railway-generated and custom) and every VOLUME attached to it together with
    the data on it. None of that can be recovered afterwards — a domain has to be
    created and re-verified at the DNS provider again, secrets have to be set
    again from wherever they are kept, and volume contents are simply gone.

    If the goal is to stop the service costing money, use stop_service instead:
    it frees the container, keeps all of the above, and start_service brings the
    service back exactly as it was.

    ONE service per call, named explicitly. Pass service_id (from
    list_services), or name together with project_id. There is deliberately no
    way to delete several services, a whole project, or everything matching a
    pattern. A name that matches no service — or more than one — is REFUSED and
    nothing is deleted; it is never resolved to the closest candidate. Passing
    both service_id and name checks them against each other and refuses a
    mismatch. The id is looked up before the delete fires, so an id Railway
    cannot confirm deletes nothing.
    """
    if not service_id and not name:
        return json.dumps({"error": "Name the service to delete: pass service_id "
                                    "(from list_services), or name plus project_id. "
                                    "delete_service never chooses a service for you."})

    set_like = [c for c in _SET_LIKE_NAME_CHARS if c in name]
    if set_like:
        return json.dumps({
            "error": f"Refusing the name {name!r}: it contains {''.join(set_like)!r} "
                     "and so reads as a pattern or a list of names. delete_service "
                     "deletes exactly one service and takes one literal name — "
                     "there is no bulk or wildcard delete. Delete them one at a "
                     "time, each named in its own call."})

    if name:
        pid = _pid(project_id)
        if not pid:
            # Same shared sentence as the listings, plus the way out that is
            # specific to this tool: an id needs no project lookup at all.
            return _no_project(f"Looking up the service named {name!r} for "
                               "delete_service", extra="Nothing was deleted. "
                               "Passing service_id instead skips the name "
                               "lookup, and the project, entirely.")
        data = await _query("""query($id: String!) {
          project(id: $id) { services { edges { node { id name } } } }
        }""", {"id": pid})
        services = [e["node"] for e in data["project"]["services"]["edges"]]
        matches = [s for s in services if s["name"].strip().lower() == name.strip().lower()]
        if not matches:
            return json.dumps({
                "error": f"No service named {name!r} in project {pid}. Nothing was "
                         "deleted — delete_service refuses a name it cannot resolve "
                         "rather than picking the nearest one.",
                "servicesInProject": [s["name"] for s in services]})
        if len(matches) > 1:
            return json.dumps({
                "error": f"The name {name!r} matches {len(matches)} services in "
                         f"project {pid}, so it does not identify one of them. "
                         "Nothing was deleted. Pass service_id to say which.",
                "matched": matches})
        target = matches[0]
        if service_id and target["id"] != service_id:
            return json.dumps({
                "error": f"service_id {service_id} and name {name!r} disagree — that "
                         f"name is service {target['id']} ({target['name']}). Nothing "
                         "was deleted; check which of the two you meant."})
    else:
        try:
            data = await _query("""query($id: String!) {
              service(id: $id) { id name projectId }
            }""", {"id": service_id})
        except RuntimeError as exc:
            return json.dumps({
                "error": f"Railway would not confirm service {service_id}: {exc}. "
                         "Nothing was deleted — delete_service looks the service up "
                         "first and will not fire at an id it could not read back."})
        target = data.get("service")
        if not target:
            return json.dumps({
                "error": f"No service with id {service_id}. Nothing was deleted."})

    result = await _query("""mutation($id: String!) {
      serviceDelete(id: $id)
    }""", {"id": target["id"]})
    return json.dumps({
        "serviceId": target["id"],
        "serviceName": target["name"],
        "deleted": result.get("serviceDelete"),
        "note": f"Service {target['name']} ({target['id']}) was permanently deleted, "
                "with its deployments, variables, domains and volumes. This cannot "
                "be undone."})


# ── a region name is not a place ─────────────────────────────────────

def _group_regions_by_metro(rows: list[dict]) -> dict:
    """Turn Railway's flat region list into {metros, regions, note}.

    Railway's `id` is the metro code, shared by every name in that metro, so
    grouping on it is a plain fold — nothing here is inferred from the region
    name. Railway's order is preserved (metros by first appearance, names within
    a metro as listed) so the answer does not reshuffle between calls, and each
    original row is passed through untouched apart from the added `metro_id`.
    """
    metros: dict = {}
    regions = []
    for row in rows:
        metro_id = row.get("id")
        regions.append({**row, "metro_id": metro_id})
        metro = metros.get(metro_id)
        if metro is None:
            metro = metros[metro_id] = {
                "metro_id": metro_id,
                "location": row.get("location"),
                "country": row.get("country"),
                "region": row.get("region"),
                "names": [],
            }
        metro["names"].append(row.get("name"))

    return {
        "metros": list(metros.values()),
        "regions": regions,
        "note": (
            f"{len(regions)} region names across {len(metros)} physical metros — "
            "every name under one `metro_id` is the same datacentre, so choosing "
            "between them does not move a service. `metro_id` repeats across "
            "`regions` rows on purpose; it is not a row key. Group by it, never "
            "by `location`, which two different metros can share."
        ),
    }


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
    """List persistent volumes in a project (uses the pinned default project,
    MCP_DEFAULT_PROJECT_ID, if empty).

    A volume is project-scoped; its per-environment attachments are the
    `instances`, each carrying the mount path, size and the service it is
    attached to. Use an instance's `id` for volume backups and the parent
    volume's `id` for delete_volume/update_volume_mount."""
    pid = _pid(project_id)
    if not pid:
        return _no_project("list_volumes")
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

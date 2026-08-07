"""The permanent test suite for the Railway MCP server.

Deliberately small. It does not chase coverage: it locks the two properties
that would have caught the last two real defects, plus a regression test for
each of those defects by name.

  1. The tools are still listed under the same names, with the same arguments.
     A change that silently alters a tool's schema breaks every client that
     calls it, and nothing else in this repo would notice.
  2. A slow Railway response does not block the server. Every tool must be
     `async def` and must reach the API through a worker thread; a plain `def`
     tool re-freezes the whole service for up to the 30 s HTTP timeout.

Stdlib `unittest` only — no new dependency, and `IsolatedAsyncioTestCase`
covers the async half. The Railway API is never contacted: `server._session`
is replaced with a fake whose `.post` returns canned GraphQL. That is the true
HTTP boundary, so `_query`, the thread offload, and the error handling in
`_query_sync` all still run for real. The suite therefore passes on a clean
checkout with no RAILWAY_API_TOKEN present, which is the point — a suite that
needs credentials is a suite nobody runs.

Run:   python -m unittest discover -s tests -v
Refresh the tool-contract snapshot after an INTENDED schema change:
       python tests/test_server.py --refresh
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402  (needs REPO_ROOT on sys.path first)

CONTRACT_FILE = Path(__file__).parent / "tool_contract.json"

_REAL_SESSION = server._session


def setUpModule():
    """Cut the whole suite off from the real Railway API for its entire run.

    Belt and braces: the tests below install their own fakes, but a test added
    later that forgets to stub would otherwise fire at production Railway with
    whatever token happens to be in the developer's environment. This makes
    that mistake fail loudly instead.
    """
    def _refuse():
        raise AssertionError(
            "a test tried to reach the real Railway API — stub it with "
            "_StubbedServer.install() instead")
    server._session = _refuse


def tearDownModule():
    server._session = _REAL_SESSION


# ── the tool contract ───────────────────────────────────────────────

def _arg_type(schema: dict) -> str:
    """One stable token per argument type.

    Deliberately coarser than the raw JSON schema: titles and defaults are
    cosmetic and shift with pydantic/SDK versions, so snapshotting them would
    make this test fail on an unrelated dependency bump. Names, requiredness
    and type are what a caller actually depends on.
    """
    if "type" in schema:
        return str(schema["type"])
    if "anyOf" in schema:
        return "|".join(sorted(str(s.get("type", "null")) for s in schema["anyOf"]))
    return "unknown"


async def _current_contract() -> dict:
    tools = await server.mcp.list_tools()
    return {
        t.name: {
            "required": sorted(t.inputSchema.get("required", [])),
            "args": {k: _arg_type(v)
                     for k, v in sorted(t.inputSchema.get("properties", {}).items())},
        }
        for t in sorted(tools, key=lambda t: t.name)
    }


class ToolContractTest(unittest.IsolatedAsyncioTestCase):
    """Check 1 — the advertised tool surface has not moved."""

    async def test_tool_contract_unchanged(self):
        expected = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
        actual = await _current_contract()

        self.assertEqual(
            sorted(expected), sorted(actual),
            "the set of tool NAMES changed — clients call these by name. If "
            "intended, refresh: python tests/test_server.py --refresh",
        )
        for name in sorted(expected):
            self.assertEqual(
                expected[name], actual[name],
                f"tool '{name}' changed its arguments. If intended, refresh: "
                f"python tests/test_server.py --refresh",
            )


# ── the non-blocking guarantee ──────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Stands in for requests.Session at the one place server.py uses it.

    Records every call, answers from `routes` (first substring of the GraphQL
    query that matches wins), and optionally blocks for `delay` seconds so a
    slow Railway can be simulated without a slow Railway.

    A route value is the GraphQL `data` payload, unless it already carries an
    "errors" key — then it is sent as-is, which is how a test asks for a real
    GraphQL error (the kind `_query_sync` turns into a RuntimeError). A route
    value that is an Exception is raised instead, which is how a test asks for
    the failures that never reach GraphQL at all: an HTTP 401, a timeout.
    """

    def __init__(self, routes: dict[str, dict], delay: float = 0.0):
        self.routes = routes
        self.delay = delay
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        body = json or {}
        query = body.get("query", "")
        self.calls.append({"query": query, "variables": body.get("variables", {})})
        if self.delay:
            time.sleep(self.delay)
        for marker, data in self.routes.items():
            if marker in query:
                if isinstance(data, Exception):
                    raise data
                return _FakeResponse(data if "errors" in data else {"data": data})
        return _FakeResponse({"data": {}})


class _StubbedServer(unittest.IsolatedAsyncioTestCase):
    """Base class: swaps server._session for a fake, restores it afterwards."""

    def install(self, routes: dict[str, dict], delay: float = 0.0) -> _FakeSession:
        session = _FakeSession(routes, delay)
        original = server._session
        server._session = lambda: session
        self.addCleanup(setattr, server, "_session", original)
        return session


class NonBlockingTest(_StubbedServer):
    """Check 2 — a slow response must not stall the event loop."""

    def test_every_tool_is_async(self):
        """Structural half: the SDK calls a plain `def` tool straight on the
        event loop (func_metadata: `return fn(**args)` with no offload), so one
        synchronous tool re-freezes the service. This is the cheap check that
        catches it at the moment it is written."""
        sync_tools = [t.name for t in server.mcp._tool_manager.list_tools()
                      if not (t.is_async and inspect.iscoroutinefunction(t.fn))]
        self.assertEqual(
            [], sync_tools,
            "these tools are not `async def`, so a slow Railway response will "
            "block the whole server for up to the 30 s timeout: "
            f"{sync_tools}",
        )

    async def test_slow_response_does_not_block_the_loop(self):
        """Behavioural half: with the backend taking 300 ms, the event loop must
        still be free to run other work. Proves the tool actually reaches the
        API through a worker thread, not just that it is spelled `async`."""
        slow = 0.3
        self.install({"me {": {"me": {"email": "a@b.c", "name": "t", "workspaces": []}}},
                     delay=slow)

        ticks = 0
        done = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not done.is_set():
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(ticker())
        started = time.perf_counter()
        await server.mcp.call_tool("whoami", {})
        elapsed = time.perf_counter() - started
        done.set()
        await beat

        self.assertGreaterEqual(elapsed, slow, "the stub did not actually delay")
        self.assertGreaterEqual(
            ticks, 10,
            f"the event loop only ran {ticks} times during a {slow}s backend "
            "call — it was blocked. Every tool must await _query(), which "
            "parks the blocking request in a worker thread.",
        )


# ── regressions: the two defects this suite exists to catch ─────────

class RegressionTest(_StubbedServer):

    async def test_create_project_auto_selects_a_single_workspace(self):
        """`await _query(...)["me"]` subscripts the coroutine, not the result.
        It raises only on the path where workspace_id is omitted, so it survived
        review and every manual check that passed one."""
        session = self.install({
            "me { workspaces": {"me": {"workspaces": [{"id": "ws1", "name": "Solo"}]}},
            "projectCreate": {"projectCreate": {
                "id": "p1", "name": "demo",
                "environments": {"edges": [{"node": {"id": "e1", "name": "production"}}]},
            }},
        })

        result = json.loads(await _text(server.mcp.call_tool("create_project", {"name": "demo"})))

        self.assertEqual("p1", result.get("id"))
        self.assertEqual([{"id": "e1", "name": "production"}], result["environments"])
        self.assertEqual("ws1", session.calls[1]["variables"]["input"]["workspaceId"],
                         "the workspace found by the lookup was not the one used")

    async def test_create_project_asks_which_workspace_when_ambiguous(self):
        """The other branch of the same lookup: two workspaces must produce a
        choice, not a guess and not a crash."""
        self.install({"me { workspaces": {"me": {"workspaces": [
            {"id": "ws1", "name": "A"}, {"id": "ws2", "name": "B"}]}}})

        result = json.loads(await _text(server.mcp.call_tool("create_project", {"name": "demo"})))

        self.assertIn("error", result)
        self.assertEqual(2, len(result["workspaces"]))

    async def test_list_projects_makes_one_request(self):
        """list_projects opens nearly every Railway session and used to cost
        2 + one-per-workspace round trips. The single-query path must stay the
        one that answers."""
        session = self.install({"me { workspaces": {"me": {"workspaces": [
            {"id": "ws1", "name": "Solo",
             "projects": {"edges": [{"node": {"id": "p1", "name": "demo"}}]}}]}}})

        result = json.loads(await _text(server.mcp.call_tool("list_projects", {})))

        self.assertEqual([{"id": "p1", "name": "demo", "workspace": "Solo"}], result)
        self.assertEqual(1, len(session.calls),
                         f"expected one round trip, made {len(session.calls)}")


    async def test_deploy_restarts_the_deployment_not_the_service(self):
        """`deploymentRestart(id:)` takes a DEPLOYMENT id, but deploy passed the
        SERVICE id. Railway then answered "Deployment not found" on a service
        with a live, healthy deployment — true of the id it was given, and
        completely misleading about the service. deploy must resolve the current
        deployment first, exactly as get_logs already does."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-old", "createdAt": "2026-08-01T10:00:00Z",
                          "status": "SUCCESS"}},
                {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "SUCCESS"}},
            ]}},
            "deploymentRestart": {"deploymentRestart": True},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "deploy", {"project_id": "p1", "environment_id": "e1", "service_id": "svc1"})))

        self.assertNotIn("error", result)
        self.assertEqual("dep-live", result["deploymentId"],
                         "restarted the wrong deployment — it must be the newest")
        restart = next(c for c in session.calls if "deploymentRestart" in c["query"])
        self.assertEqual("dep-live", restart["variables"]["did"],
                         "the service id was sent to deploymentRestart again — "
                         "that is the original 'Deployment not found' defect")
        self.assertNotIn("svc1", restart["variables"].values())

    async def test_deploy_reports_why_when_nothing_is_restartable(self):
        """The other half of the defect: when there genuinely is nothing to
        restart, say so specifically instead of leaving Railway to blame a
        missing deployment. And do not fire the mutation blindly."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-dead", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "FAILED"}},
            ]}},
            "deploymentRestart": {"deploymentRestart": True},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "deploy", {"project_id": "p1", "environment_id": "e1", "service_id": "svc1"})))

        self.assertIn("FAILED", result.get("error", ""))
        self.assertEqual(["FAILED"], result["recentStatuses"])
        self.assertFalse([c for c in session.calls if "deploymentRestart" in c["query"]],
                         "restarted anyway despite having no restartable deployment")


# ── deploying for real, next to a tool that only restarts ───────────

class CreateDeploymentTest(_StubbedServer):
    """`deploy` restarts the container already running; it builds nothing. The
    failure guarded here is silent and one level up from the code: an agent
    reads the name, calls it, sees it succeed and reports that new code is
    live. `create_deployment` is the tool that actually builds and releases,
    so the two must reach DIFFERENT Railway mutations and must be readable
    apart from their descriptions alone.

    The mutation is `serviceInstanceDeployV2(serviceId, environmentId,
    commitSha)`, which addresses the SERVICE directly and returns the id of the
    deployment it created — not `deploymentRestart`, which needs a DEPLOYMENT
    id and is the trap `deploy` already carries a regression test for.
    """

    _DEPLOYS = {"serviceInstanceDeployV2": {"serviceInstanceDeployV2": "dep-new"}}

    async def test_it_builds_a_new_deployment_for_the_service(self):
        session = self.install(self._DEPLOYS)

        result = json.loads(await _text(server.mcp.call_tool(
            "create_deployment", {"environment_id": "e1", "service_id": "svc1"})))

        self.assertNotIn("error", result)
        self.assertEqual("dep-new", result["deploymentId"],
                         "the id Railway returned for the new deployment was lost")
        call = next(c for c in session.calls
                    if "serviceInstanceDeployV2" in c["query"])
        self.assertEqual({"sid": "svc1", "eid": "e1"}, call["variables"],
                         "the service and environment ids were not the ones sent")

    async def test_it_does_not_go_through_the_restart_mutation(self):
        """The whole point of the tool. If it restarted like `deploy`, it would
        report success without building anything — the same wrong mental model
        one name further along."""
        session = self.install(self._DEPLOYS)

        await server.mcp.call_tool(
            "create_deployment", {"environment_id": "e1", "service_id": "svc1"})

        self.assertFalse([c for c in session.calls if "deploymentRestart" in c["query"]],
                         "create_deployment restarted instead of deploying")
        self.assertFalse([c for c in session.calls if "deployments(input:" in c["query"]],
                         "resolved a deployment id — serviceInstanceDeployV2 "
                         "takes the SERVICE, so that lookup is the old trap "
                         "being repeated in reverse")

    async def test_a_commit_sha_is_passed_through_when_given(self):
        """A plain call deploys the commit already associated with the service
        and does NOT look for newer ones, so deploying the HEAD of a branch
        means naming it."""
        session = self.install(self._DEPLOYS)

        result = json.loads(await _text(server.mcp.call_tool(
            "create_deployment", {"environment_id": "e1", "service_id": "svc1",
                                  "commit_sha": "abc123"})))

        call = next(c for c in session.calls
                    if "serviceInstanceDeployV2" in c["query"])
        self.assertIn("commitSha", call["query"],
                      "commit_sha was given but never reached the mutation")
        self.assertEqual("abc123", call["variables"]["sha"])
        self.assertEqual("abc123", result["commitSha"])

    async def test_a_missing_service_or_environment_builds_nothing(self):
        """Both ids are refused when absent rather than defaulted. Guessing here
        rebuilds a production service that nobody named."""
        for args in ({"environment_id": "e1"},
                     {"service_id": "svc1"},
                     {},
                     {"environment_id": "  ", "service_id": "svc1"}):
            with self.subTest(args=args):
                session = self.install(self._DEPLOYS)
                result = json.loads(await _text(
                    server.mcp.call_tool("create_deployment", args)))
                self.assertIn("error", result)
                self.assertEqual([], session.calls,
                                 f"called Railway anyway for {args}")

    async def test_an_id_that_reads_as_a_list_or_pattern_builds_nothing(self):
        """Same refusal delete_service makes: an id carrying a separator or a
        wildcard means a SET of services was meant, and there is no bulk
        deploy. Resolving it to the nearest one would rebuild the wrong live
        service."""
        for sid in ("svc1,svc2", "svc*", "svc1;svc2", "svc1|svc2"):
            with self.subTest(service_id=sid):
                session = self.install(self._DEPLOYS)
                result = json.loads(await _text(server.mcp.call_tool(
                    "create_deployment", {"environment_id": "e1", "service_id": sid})))
                self.assertIn("error", result)
                self.assertEqual([], session.calls,
                                 f"deployed anyway for the set-like id {sid!r}")

    async def test_a_railway_refusal_names_both_ids_instead_of_echoing_it(self):
        """Railway answers a wrong id, a mismatched project/environment pair and
        an unknown commit through the same generic message, which reads as if
        the service were gone. Say what was attempted."""
        self.install({"serviceInstanceDeployV2": {
            "errors": [{"message": "Not Authorized"}]}})

        result = json.loads(await _text(server.mcp.call_tool(
            "create_deployment", {"environment_id": "e1", "service_id": "svc1",
                                  "commit_sha": "abc123"})))

        self.assertIn("svc1", result["error"])
        self.assertIn("e1", result["error"])
        self.assertIn("abc123", result["hint"],
                      "a named commit is the third way this call fails and the "
                      "message does not mention it")

    async def test_deploy_still_only_restarts(self):
        """The old tool is unchanged. It is not quietly upgraded into a real
        deploy: callers that rely on it restarting must keep getting a restart,
        and nothing may be built behind their backs."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "SUCCESS"}},
            ]}},
            "deploymentRestart": {"deploymentRestart": True},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "deploy", {"project_id": "p1", "environment_id": "e1", "service_id": "svc1"})))

        self.assertEqual({"deploymentId": "dep-live", "deploymentStatus": "SUCCESS",
                          "restarted": True}, result,
                         "deploy's answer changed — callers parse this")
        self.assertFalse([c for c in session.calls if "DeployV2" in c["query"]],
                         "deploy now builds — that is a behaviour change, not a "
                         "clarification")

    async def test_the_two_descriptions_cannot_be_confused(self):
        """The card was about a name, and the fix is carried by the two
        descriptions: whoever reads only the tool list must be able to pick
        correctly. Lock the words that make that possible, so a later tidy-up
        of the docstrings cannot quietly undo the fix."""
        tools = {t.name: (t.description or "").lower()
                 for t in await server.mcp.list_tools()}

        self.assertIn("restart", tools["deploy"],
                      "deploy's description does not say that it restarts")
        self.assertIn("does not", tools["deploy"],
                      "deploy's description does not deny building")
        self.assertIn("create_deployment", tools["deploy"],
                      "deploy does not point at the tool that really deploys")

        self.assertIn("build", tools["create_deployment"],
                      "create_deployment's description does not say it builds")
        self.assertIn("deploy", tools["create_deployment"])


# ── stopping a service, and finding one that is stopped ─────────────

class StopStartTest(_StubbedServer):
    """Halting a service must be reversible, honest about what it did, and
    visible afterwards. The failure this guards is not a crash: it is a stopped
    service that still looks like a running one (Railway has no STOPPED status),
    or a stop that reports the platform's own misleading "not found" because it
    was handed a service id where a deployment id was expected."""

    _RUNNING = {
        "deployments(input:": {"deployments": {"edges": [
            {"node": {"id": "dep-old", "createdAt": "2026-08-01T10:00:00Z",
                      "status": "SUCCESS", "deploymentStopped": False}},
            {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                      "status": "SUCCESS", "deploymentStopped": False}},
        ]}},
        # "deploymentStop(id:" and not "deploymentStop": the LIST query selects
        # the field deploymentStopped, which contains that substring.
        "deploymentStop(id:": {"deploymentStop": True},
    }

    async def test_stop_service_stops_the_deployment_not_the_service(self):
        """deploymentStop takes a DEPLOYMENT id, exactly like deploymentRestart.
        Passing the service id would produce "Deployment not found" about a
        service that is running perfectly well — the defect deploy() already
        carries a regression test for."""
        session = self.install(self._RUNNING)

        result = json.loads(await _text(server.mcp.call_tool(
            "stop_service", {"project_id": "p1", "environment_id": "e1",
                             "service_id": "svc1"})))

        self.assertNotIn("error", result)
        self.assertEqual("dep-live", result["deploymentId"],
                         "stopped the wrong deployment — it must be the newest")
        self.assertIs(True, result["stopped"])
        stop = next(c for c in session.calls if "deploymentStop(id:" in c["query"])
        self.assertEqual("dep-live", stop["variables"]["did"],
                         "a service id was sent to deploymentStop")
        self.assertNotIn("svc1", stop["variables"].values())

    async def test_stop_service_says_when_it_is_already_stopped(self):
        """A stopped deployment keeps its old status, so 'SUCCESS' proves
        nothing. Read deploymentStopped, and say so plainly instead of stopping
        it a second time."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "SUCCESS", "deploymentStopped": True}},
            ]}},
            # "deploymentStop(id:" and not "deploymentStop": the LIST query selects
        # the field deploymentStopped, which contains that substring.
        "deploymentStop(id:": {"deploymentStop": True},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "stop_service", {"project_id": "p1", "environment_id": "e1",
                             "service_id": "svc1"})))

        self.assertTrue(result["alreadyStopped"])
        self.assertIn("already stopped", result["error"])
        self.assertIn("start_service", result["error"],
                      "an already-stopped service must be told how to come back")
        self.assertFalse([c for c in session.calls if "deploymentStop(id:" in c["query"]],
                         "stopped a deployment that was already stopped")

    async def test_stop_service_reports_why_when_nothing_is_running(self):
        """Nothing to stop is a legitimate answer. It must name the actual
        status rather than letting Railway blame a missing deployment, and it
        must not fire the mutation on a hope."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-dead", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "FAILED", "deploymentStopped": False}},
            ]}},
            # "deploymentStop(id:" and not "deploymentStop": the LIST query selects
        # the field deploymentStopped, which contains that substring.
        "deploymentStop(id:": {"deploymentStop": True},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "stop_service", {"project_id": "p1", "environment_id": "e1",
                             "service_id": "svc1"})))

        self.assertIn("FAILED", result.get("error", ""))
        self.assertEqual(["FAILED"], result["recentStatuses"])
        self.assertFalse([c for c in session.calls if "deploymentStop(id:" in c["query"]],
                         "stopped something despite having nothing to stop")

    async def test_stop_service_reports_a_service_that_never_deployed(self):
        """No deployments at all is a different answer from none running."""
        self.install({"deployments(input:": {"deployments": {"edges": []}}})

        result = json.loads(await _text(server.mcp.call_tool(
            "stop_service", {"project_id": "p1", "environment_id": "e1",
                             "service_id": "svc1"})))

        self.assertIn("No deployments found", result["error"])

    async def test_start_service_addresses_the_service_not_a_deployment(self):
        """The undo path deliberately avoids deployment ids: it must work even
        when the stopped deployment was left in a state nothing can restart."""
        session = self.install({
            "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}})

        result = json.loads(await _text(server.mcp.call_tool(
            "start_service", {"environment_id": "e1", "service_id": "svc1"})))

        self.assertIs(True, result["started"])
        call = session.calls[0]
        self.assertEqual({"sid": "svc1", "eid": "e1"}, call["variables"])
        self.assertNotIn("deploymentRestart", call["query"])

    async def test_start_service_explains_a_refusal_instead_of_echoing_it(self):
        """Railway answers a mismatched service/environment pair the same way it
        answers a deleted service. Echoing that verbatim would tell an agent its
        service is gone when the ids were simply crossed."""
        self.install({"serviceInstanceRedeploy": {
            "errors": [{"message": "Not Authorized"}]}})

        result = json.loads(await _text(server.mcp.call_tool(
            "start_service", {"environment_id": "e1", "service_id": "svc1"})))

        self.assertIn("svc1", result["error"])
        self.assertIn("e1", result["error"])
        self.assertIn("Not Authorized", result["error"],
                      "the platform's own message must survive, not be swallowed")
        self.assertIn("list_environments", result["hint"])

    async def test_list_services_shows_that_a_service_is_stopped(self):
        """The invisible-orphan half of the card: a stopped service keeps status
        SUCCESS, and one stripped of its domain appears nowhere else. If the
        listing does not carry deploymentStopped, nothing an agent can see says
        the service is down."""
        self.install({"project(id:": {"project": {"services": {"edges": [
            {"node": {"id": "svc1", "name": "halted", "serviceInstances": {"edges": [
                {"node": {"environmentId": "e1", "region": None, "numReplicas": 1,
                          "latestDeployment": {"id": "dep-live", "status": "SUCCESS",
                                               "deploymentStopped": True}}},
            ]}}},
        ]}}}})

        result = json.loads(await _text(server.mcp.call_tool(
            "list_services", {"project_id": "p1"})))

        instance = result[0]["instances"][0]
        self.assertTrue(instance["latestDeployment"]["deploymentStopped"],
                        "a stopped service is indistinguishable from a running "
                        "one in this listing")
        self.assertEqual("SUCCESS", instance["latestDeployment"]["status"])


class DeploymentFreshnessTest(_StubbedServer):
    """`latestDeployment` cannot answer "did my deploy land?", and must not
    look as though it can.

    Railway's per-instance pointer lags: during a real deploy it kept naming
    the previous deployment across three checks, still stale well after the new
    code was answering live traffic. It is the newest deployment the API knows
    about — checked against the deployments list on 24 service instances across
    both accounts, including a CRASHED one, it agreed every time — so the field
    is right and merely late, and there is nothing here to correct.

    What was wrong is that the lateness was invisible. The old answer carried
    an opaque id and a status that reads SUCCESS before and after, so a stale
    value is indistinguishable from a push that never happened. `createdAt`
    makes it visible, and the description sends the reader to the tools that
    can actually confirm a deploy."""

    _ONE_SERVICE = {"project(id:": {"project": {"services": {"edges": [
        {"node": {"id": "svc1", "name": "api", "serviceInstances": {"edges": [
            {"node": {"environmentId": "e1", "region": None, "numReplicas": 1,
                      "latestDeployment": {"id": "dep-old",
                                           "createdAt": "2026-08-01T10:00:00Z",
                                           "status": "SUCCESS",
                                           "deploymentStopped": False}}},
        ]}}},
    ]}}}}

    async def test_the_listing_says_when_the_deployment_it_names_was_created(self):
        """Without a timestamp the caller has only an id it has to have
        memorised beforehand to spot that nothing moved."""
        session = self.install(self._ONE_SERVICE)

        result = json.loads(await _text(server.mcp.call_tool(
            "list_services", {"project_id": "p1"})))

        self.assertIn("createdAt", session.calls[0]["query"],
                      "the query does not ask Railway when the deployment was made")
        self.assertEqual("2026-08-01T10:00:00Z",
                         result[0]["instances"][0]["latestDeployment"]["createdAt"],
                         "the age of the named deployment never reaches the caller")

    async def test_the_description_denies_being_a_deploy_check(self):
        """The fix is carried by the words: an agent reading the tool list must
        learn here that this value cannot confirm a deploy, and which tool can.
        A later tidy-up of the docstring must fail this, not pass quietly."""
        tools = {t.name: (t.description or "").lower()
                 for t in await server.mcp.list_tools()}
        listing = tools["list_services"]

        self.assertIn("confirm", listing,
                      "nothing in the description warns about confirming a deploy")
        self.assertIn("stale", listing)
        self.assertIn("get_logs", listing,
                      "the description does not point at a tool that can answer it")
        self.assertIn("create_deployment", listing)


class DeleteServiceTest(_StubbedServer):
    """Deleting a service is the one operation here with no undo.

    Everything else in this server can be repaired by calling something else:
    a stopped service starts, a wrong region is set again, a deleted domain is
    re-created. serviceDelete takes the service, its deployments, its variables,
    its domains and its volumes' data, and none of it comes back. These tools
    also run with permissions pre-granted, so no human sees the call before it
    happens.

    So the property under test is not "deletion works" — that is one line of
    GraphQL. It is that delete_service destroys exactly the service it was
    given and refuses everything else: a name it cannot resolve to one service,
    a name that reads as a pattern or a list, an id that disagrees with the
    name beside it, an id Railway would not confirm. Every refusal must leave
    the account untouched, and both refusals and successes must name the
    service, so a transcript says exactly what was destroyed.
    """

    _PROJECT = {"project(id:": {"project": {"services": {"edges": [
        {"node": {"id": "svc-api", "name": "api"}},
        {"node": {"id": "svc-web", "name": "web"}},
    ]}}}}
    _DELETE = {"serviceDelete": {"serviceDelete": True}}

    @staticmethod
    def _deletions(session: _FakeSession) -> list[dict]:
        return [c for c in session.calls if "serviceDelete" in c["query"]]

    async def _delete(self, routes: dict, args: dict) -> tuple[dict, _FakeSession]:
        session = self.install(routes)
        result = json.loads(await _text(server.mcp.call_tool("delete_service", args)))
        return result, session

    async def test_deleting_by_id_deletes_that_service_and_names_it(self):
        """The happy path, and the only path that may reach the mutation: an id
        the caller supplied, confirmed against Railway first so the answer can
        say which service went."""
        result, session = await self._delete(
            {"service(id:": {"service": {"id": "svc-api", "name": "api",
                                         "projectId": "p1"}},
             **self._DELETE},
            {"service_id": "svc-api"})

        self.assertNotIn("error", result)
        self.assertIs(True, result["deleted"])
        self.assertEqual("svc-api", result["serviceId"])
        self.assertEqual("api", result["serviceName"],
                         "a transcript of this call must say WHICH service was "
                         "destroyed, not just that one was")
        self.assertIn("cannot be undone", result["note"])
        deletions = self._deletions(session)
        self.assertEqual(1, len(deletions), "exactly one service may be deleted per call")
        self.assertEqual("svc-api", deletions[0]["variables"]["id"])

    async def test_a_unique_name_resolves_to_exactly_that_service(self):
        """A name is allowed only because it is checked. It must delete the
        service that actually carries the name, never a neighbour."""
        result, session = await self._delete(
            {**self._PROJECT, **self._DELETE},
            {"name": "web", "project_id": "p1"})

        self.assertNotIn("error", result)
        self.assertEqual("svc-web", result["serviceId"])
        self.assertEqual("web", result["serviceName"])
        self.assertEqual(["svc-web"],
                         [d["variables"]["id"] for d in self._deletions(session)])

    async def test_a_name_matching_more_than_one_service_is_refused(self):
        """Two services share a name. There is no defensible way to pick one,
        so nothing may be deleted — and the answer must show both, or the
        caller cannot tell what it nearly destroyed."""
        result, session = await self._delete(
            {"project(id:": {"project": {"services": {"edges": [
                {"node": {"id": "svc-1", "name": "worker"}},
                {"node": {"id": "svc-2", "name": "worker"}},
            ]}}}, **self._DELETE},
            {"name": "worker", "project_id": "p1"})

        self.assertIn("worker", result["error"])
        self.assertIn("does not identify one", result["error"])
        self.assertEqual({"svc-1", "svc-2"}, {m["id"] for m in result["matched"]},
                         "the refusal does not say which services matched")
        self.assertEqual([], self._deletions(session),
                         "an ambiguous name deleted something anyway")

    async def test_a_name_matching_nothing_is_refused_not_guessed(self):
        """The dangerous shape of a typo: 'ap' is not 'api', and resolving it to
        the closest candidate would delete a service nobody asked for."""
        result, session = await self._delete({**self._PROJECT, **self._DELETE},
                                             {"name": "ap", "project_id": "p1"})

        self.assertIn("'ap'", result["error"])
        self.assertIn("Nothing was deleted", result["error"])
        self.assertIn("api", result["servicesInProject"])
        self.assertEqual([], self._deletions(session),
                         "a name that matched no service deleted one anyway")

    async def test_a_pattern_or_a_list_is_refused_before_any_lookup(self):
        """'delete everything matching this' must not be expressible. Refuse the
        shape of the request itself, without even resolving it — a glob that
        happened to match exactly one service today would teach a caller that
        globs work here, and tomorrow it matches three."""
        for hostile in ("mcp.*", "api,web", "svc-?", "api;web", "api|web", "%"):
            with self.subTest(name=hostile):
                result, session = await self._delete(
                    {**self._PROJECT, **self._DELETE}, {"name": hostile,
                                                        "project_id": "p1"})

                self.assertIn("pattern or a list", result["error"])
                self.assertIn(hostile, result["error"],
                              "the refusal does not quote what was rejected")
                self.assertEqual([], session.calls,
                                 "a pattern was resolved against Railway instead "
                                 "of being refused outright")

    async def test_an_id_that_disagrees_with_the_name_deletes_neither(self):
        """Passing both is a cross-check, not two chances to match. A stale id
        beside a fresh name is exactly the situation where guessing is worst."""
        result, session = await self._delete({**self._PROJECT, **self._DELETE},
                                             {"service_id": "svc-api", "name": "web",
                                              "project_id": "p1"})

        self.assertIn("disagree", result["error"])
        self.assertIn("svc-api", result["error"])
        self.assertIn("svc-web", result["error"],
                      "the refusal must name both candidates, or the caller "
                      "cannot tell which one was stale")
        self.assertEqual([], self._deletions(session))

    async def test_an_unconfirmable_id_deletes_nothing(self):
        """serviceDelete on an unknown id is harmless, but on a MISTYPED id that
        happens to exist it is not. The lookup is the guard: if Railway will not
        read the service back, the mutation must not fire at all."""
        result, session = await self._delete(
            {"service(id:": {"errors": [{"message": "Not Authorized"}]},
             **self._DELETE},
            {"service_id": "svc-typo"})

        self.assertIn("svc-typo", result["error"])
        self.assertIn("Not Authorized", result["error"],
                      "the platform's own message must survive, not be swallowed")
        self.assertEqual([], self._deletions(session),
                         "deleted an id that could not be looked up first")

    async def test_an_empty_request_deletes_nothing_and_asks_for_a_name(self):
        """No id and no name is not 'delete the default service'."""
        result, session = await self._delete({**self._PROJECT, **self._DELETE}, {})

        self.assertIn("never chooses a service for you", result["error"])
        self.assertEqual([], session.calls)


class ListProjectsFailureTest(_StubbedServer):
    """list_projects tries a fast path and two fallbacks. Each swallows its own
    exception so the next one runs — correct — but when all of them come up
    empty the reader used to be told to pin a default project, whatever had
    actually gone wrong. An outage, an expired token and a genuinely unscoped
    token all looked identical, and the real cause appeared nowhere.

    The round-trip test above cannot see this: it counts requests on the happy
    path, and a swallowed failure changes no count.
    """

    def setUp(self):
        # These tests are about the no-project-id branch, and a developer
        # machine may well have a default project pinned.
        original = server.DEFAULT_PROJECT
        server.DEFAULT_PROJECT = ""
        self.addCleanup(setattr, server, "DEFAULT_PROJECT", original)

    @staticmethod
    def _http(status: int) -> requests.exceptions.HTTPError:
        response = requests.Response()
        response.status_code = status
        return requests.exceptions.HTTPError(f"{status} Client Error", response=response)

    async def test_a_rejected_token_is_not_reported_as_missing_configuration(self):
        """The defect itself: Railway refuses the token, and the answer blames
        a configuration setting that is not the problem."""
        self.install({"me { workspaces": self._http(401),
                      "query { projects": self._http(401)})

        result = json.loads(await _text(server.mcp.call_tool("list_projects", {})))

        self.assertIn("refused", result["error"],
                      "the reason the queries failed was thrown away")
        self.assertIn("401", result["error"])
        self.assertNotIn("RAILWAY_PROJECT_ID", result["error"],
                         "an auth failure was reported as a missing setting — "
                         "that is the whole defect")

    async def test_an_unreachable_railway_reads_differently_from_a_refusal(self):
        """The three cases must be distinguishable at a glance, not merely
        'something failed'."""
        self.install({"me { workspaces": requests.exceptions.ConnectionError("no route"),
                      "query { projects": requests.exceptions.ConnectionError("no route")})

        result = json.loads(await _text(server.mcp.call_tool("list_projects", {})))

        self.assertIn("unreachable", result["error"])
        self.assertNotIn("refused", result["error"])
        self.assertNotIn("RAILWAY_PROJECT_ID", result["error"])

    async def test_the_fallbacks_own_failure_is_reported_too(self):
        """The fast path can succeed and simply hold no projects; then it is a
        fallback that fails, and that silence hides just as much."""
        self.install({
            "me { workspaces": {"me": {"workspaces": [
                {"id": "ws1", "name": "Solo", "projects": {"edges": []}}]}},
            "query { projects": {"errors": [{"message": "Problem processing request"}]},
            "workspace(workspaceId:": {"errors": [{"message": "Not Authorized"}]},
        })

        result = json.loads(await _text(server.mcp.call_tool("list_projects", {})))

        self.assertIn("Problem processing request", result["error"])
        self.assertTrue(any("Not Authorized" in a for a in result["attempts"]),
                        "the per-workspace fan-out failed silently")
        self.assertNotIn("RAILWAY_PROJECT_ID", result["error"])

    async def test_a_genuinely_unconfigured_token_still_gets_the_old_advice(self):
        """The other half: when nothing failed and there is simply nothing to
        list, the configuration message is the right answer and must survive."""
        self.install({"me { workspaces": {"me": {"workspaces": []}},
                      "query { projects": {"projects": {"edges": []}}})

        result = json.loads(await _text(server.mcp.call_tool("list_projects", {})))

        self.assertIn("MCP_DEFAULT_PROJECT_ID", result["error"])
        self.assertEqual([], result["workspaces"])
        self.assertNotIn("attempts", result,
                         "nothing failed, so there is no failure to report")

    def test_a_failure_reason_cannot_carry_the_token(self):
        """Error text is exactly where a credential leaks. _why must not pass
        one through even if Railway echoes it back to us."""
        original = server.TOKEN
        server.TOKEN = "super-secret-token"
        self.addCleanup(setattr, server, "TOKEN", original)

        leaked = server._why(RuntimeError("bad auth: Bearer super-secret-token"))

        self.assertNotIn("super-secret-token", leaked)
        self.assertIn("***", leaked)


class ListProjectsPinnedProjectTest(_StubbedServer):
    """The failure reason above only reaches a caller who got nothing back.

    With a default project pinned, that project satisfies the request and
    the recorded failures were dropped — so a caller could not tell "the
    account holds one project" from "everything failed and one id was
    configured". An expiring token then goes unnoticed for as long as that one
    project keeps answering whoever asks, and the first sign of trouble turns
    up somewhere unrelated.

    The rule these tests hold in place: keep giving the usable answer, and say
    beside it that the wider lookup failed.
    """

    PINNED = "proj-pinned-id"

    def setUp(self):
        original = server.DEFAULT_PROJECT
        server.DEFAULT_PROJECT = self.PINNED
        self.addCleanup(setattr, server, "DEFAULT_PROJECT", original)
        # The name is reported, not assumed, so pin it too — otherwise this
        # test reads whatever the developer's own environment happens to hold.
        original_var = server.DEFAULT_PROJECT_VAR
        server.DEFAULT_PROJECT_VAR = "MCP_DEFAULT_PROJECT_ID"
        self.addCleanup(setattr, server, "DEFAULT_PROJECT_VAR", original_var)

    @staticmethod
    def _http(status: int) -> requests.exceptions.HTTPError:
        response = requests.Response()
        response.status_code = status
        return requests.exceptions.HTTPError(f"{status} Client Error", response=response)

    def _all_fail(self, exc: Exception) -> None:
        self.install({"me { workspaces": exc, "query { projects": exc})

    async def _projects(self) -> list:
        return json.loads(await _text(server.mcp.call_tool("list_projects", {})))

    async def test_the_pinned_project_still_comes_back_when_the_lookup_failed(self):
        """The half that must NOT change. This passed before the warning was
        added and has to keep passing: a caller who got a working answer
        yesterday still gets one, in the same shape — a list, one project, its
        id intact. Adding a diagnosis must not cost anyone their result.
        """
        self._all_fail(self._http(401))

        result = await self._projects()

        self.assertIsInstance(result, list, "the answer stopped being a list of projects")
        self.assertEqual(1, len(result), "the pinned project gained or lost company")
        self.assertEqual(self.PINNED, result[0]["id"], "the usable answer was dropped")
        self.assertIn("MCP_DEFAULT_PROJECT_ID", result[0]["name"],
                      "the answer no longer says where the pinned id came from")

    async def test_the_pinned_project_says_the_wider_lookup_failed_and_why(self):
        """The defect: one project came back and the 401 behind it vanished."""
        self._all_fail(self._http(401))

        result = await self._projects()

        self.assertIn("warning", result[0],
                      "the lookup failed and the answer said nothing about it — "
                      "that is the whole defect")
        self.assertIn("refused", result[0]["warning"], "the reason was thrown away")
        self.assertIn("401", result[0]["warning"])
        self.assertTrue(any("401" in a for a in result[0]["attempts"]),
                        "every attempt's reason should still be readable")

    async def test_an_unreachable_railway_reads_differently_from_a_refusal(self):
        """Same wording as the no-project-id answer, because it is the same
        sentence — a caller should not have to learn two of them."""
        self._all_fail(requests.exceptions.ConnectionError("no route"))

        result = await self._projects()

        self.assertIn("Could not list projects", result[0]["warning"])
        self.assertIn("unreachable", result[0]["warning"])
        self.assertNotIn("refused", result[0]["warning"])

    async def test_a_quiet_account_gets_no_warning(self):
        """The other half: when every query succeeded and simply had nothing to
        return, there is no failure to report and inventing one would train
        readers to ignore the field. Passes before and after — it guards the
        new code against crying wolf, it does not describe a past bug.
        """
        self.install({"me { workspaces": {"me": {"workspaces": []}},
                      "query { projects": {"projects": {"edges": []}}})

        result = await self._projects()

        self.assertEqual(self.PINNED, result[0]["id"])
        self.assertNotIn("warning", result[0], "nothing failed, so nothing to warn about")
        self.assertNotIn("attempts", result[0])

    async def test_the_warning_cannot_carry_the_token(self):
        """The warning is new text handed to callers, and it is built from
        Railway's own words — which is exactly where a credential comes back at
        you. It goes through _why, so it is redacted; this pins that it stays
        that way now the text has a second way out of the server.
        """
        original = server.TOKEN
        server.TOKEN = "super-secret-token"
        self.addCleanup(setattr, server, "TOKEN", original)
        echoed = {"errors": [{"message": "bad auth: Bearer super-secret-token"}]}
        self.install({"me { workspaces": echoed, "query { projects": echoed})

        result = await self._projects()

        self.assertIn("warning", result[0])
        self.assertNotIn("super-secret-token", json.dumps(result))
        self.assertIn("***", result[0]["warning"])


class DefaultProjectSourceTest(unittest.TestCase):
    """Which variable the default project may be read from.

    RAILWAY_PROJECT_ID is a name Railway reserves: the platform injects it into
    every container with the id of the project the service is HOSTED in, and it
    overwrites a service-level variable of the same name on the next build. On
    the riskwave instance that hosting project sits on the other account, so
    every call that omitted project_id fell back to a project its own token
    cannot read and answered "Not Authorized". Pinning the reserved name to the
    wanted project was tried on the live service and provably shadowed — which
    is why the operator's default now has a name of ours that nothing else
    writes.

    Both halves matter. Ours must win where it is set, and the reserved one
    must keep working where it is not: the skyttedk instance pins nothing and
    relies on the injected value, so a change that only honoured the new name
    would quietly take its default away.
    """

    def test_our_variable_wins_over_the_reserved_one(self):
        """The fix itself — set on the same service, ours is the answer."""
        pid, source = server._pinned_default_project(
            {"MCP_DEFAULT_PROJECT_ID": "ours", "RAILWAY_PROJECT_ID": "railways"})

        self.assertEqual("ours", pid, "the platform's injected value won again")
        self.assertEqual("MCP_DEFAULT_PROJECT_ID", source)

    def test_the_reserved_one_is_still_used_when_ours_is_absent(self):
        """Today's behaviour for every deployment that sets nothing new."""
        pid, source = server._pinned_default_project({"RAILWAY_PROJECT_ID": "railways"})

        self.assertEqual("railways", pid)
        self.assertEqual("RAILWAY_PROJECT_ID", source)

    def test_an_empty_value_of_ours_does_not_shadow_the_fallback(self):
        """Railway hands an unset variable through as an empty string, so
        "" must read as absent rather than as a deliberate blank default."""
        pid, source = server._pinned_default_project(
            {"MCP_DEFAULT_PROJECT_ID": "", "RAILWAY_PROJECT_ID": "railways"})

        self.assertEqual("railways", pid)
        self.assertEqual("RAILWAY_PROJECT_ID", source)

    def test_neither_set_leaves_no_default(self):
        """No default is a supported state: the tools then ask for a project
        rather than guessing one."""
        pid, _ = server._pinned_default_project({})

        self.assertEqual("", pid)


class MetricsSizeTest(_StubbedServer):
    """get_metrics used to return every sample Railway held, unbounded.

    The cost of that lands nowhere this repo can see it: not on Railway, but in
    the context window of the agent that asked, where a day across a few
    deployments is tens of thousands of {ts, value} objects and crowds out the
    task it was doing. It only bites on a long range, so it never showed up in
    testing — which is exactly why it is pinned here.

    The dangerous half of the fix is the cure, not the disease: bounding a
    series by averaging is only safe while high/low/average still come from
    every raw sample. A peak computed from the points that survived would be
    wrong in the one case anyone reads metrics for.
    """

    START = "2026-08-01T00:00:00Z"

    @staticmethod
    def _series(samples: list[dict]) -> dict:
        return {"metrics(projectId:": {"metrics": [{
            "measurement": "CPU_USAGE",
            "tags": {"deploymentId": "dep-1"},
            "values": samples,
        }]}}

    @classmethod
    def _flat_day(cls, spike_at: int | None = None, spike: float = 95.0) -> list[dict]:
        """A day of 10-second samples — 8640 of them, as Railway really answers."""
        base = 1785542400  # 2026-08-01T00:00:00Z, matching START
        return [{"ts": base + i * 10,
                 "value": spike if i == spike_at else 1.0}
                for i in range(8640)]

    async def _metrics(self, samples: list[dict], end: str) -> dict:
        self.install(self._series(samples))
        result = json.loads(await _text(server.mcp.call_tool("get_metrics", {
            "project_id": "p1", "environment_id": "e1", "service_id": "s1",
            "start_date": self.START, "end_date": end,
        })))
        return result[0]

    async def test_a_wide_range_is_bounded_and_says_it_was_summarised(self):
        """A day must not come back as 8640 points, and must not come back as a
        silent subset either: a caller reasoning about a partial window while
        believing it holds the whole one is worse than a big answer."""
        raw = self._flat_day()
        series = await self._metrics(raw, "2026-08-02T00:00:00Z")

        self.assertLess(len(series["values"]), len(raw) // 10,
                        "a day of samples came back essentially unbounded")
        self.assertLessEqual(len(series["values"]), 360)
        self.assertEqual(300, series["sampleIntervalSeconds"],
                         "a day should land on 5-minute points")
        self.assertIn("not truncated", series["note"],
                      "the response does not tell the caller it was summarised")
        self.assertIn("300", series["note"], "the interval used is not stated")

    async def test_a_narrow_range_is_returned_exactly_as_measured(self):
        """Half an hour is already small. Condensing it would cost fidelity for
        no benefit, so the ordinary short call must behave as it always has."""
        raw = self._flat_day()[:180]  # 30 minutes at 10 s
        series = await self._metrics(raw, "2026-08-01T00:30:00Z")

        self.assertEqual(raw, series["values"],
                        "a short range was altered — it must pass through")
        self.assertNotIn("note", series)
        self.assertNotIn("sampleIntervalSeconds", series)

    async def test_a_spike_survives_being_condensed(self):
        """The defect the fix could introduce. One sample in a day is 95%; every
        neighbour is 1%. Averaged into a 5-minute point it all but vanishes, so
        the high must be read off the raw samples or the answer is a lie in the
        only case that matters."""
        spike_index = 4000
        raw = self._flat_day(spike_at=spike_index)
        series = await self._metrics(raw, "2026-08-02T00:00:00Z")

        self.assertEqual(95.0, series["summary"]["high"],
                         "the peak was computed from the surviving points, not "
                         "from every sample — a brief spike is invisible")
        self.assertEqual(raw[spike_index]["ts"], series["summary"]["highTs"])
        self.assertEqual(1.0, series["summary"]["low"])
        self.assertEqual(len(raw), series["summary"]["samples"],
                         "the summary did not cover the full range")
        self.assertAlmostEqual(
            (1.0 * (len(raw) - 1) + 95.0) / len(raw), series["summary"]["average"],
            places=9, msg="the average is not the average of the full range")

        peak_of_returned = max(v["value"] for v in series["values"])
        self.assertLess(peak_of_returned, 95.0,
                        "the spike happens to survive condensing here, so this "
                        "test would pass even with a summary computed from the "
                        "returned points — pick a spike that gets averaged away")

    async def test_the_interval_is_chosen_from_the_range_asked_for(self):
        """The bound is a function of the window, not a fixed number of points,
        so the answer stays legible as the range grows instead of silently
        changing resolution to whatever fills a quota."""
        self.assertEqual(10, server._sample_interval_for(3600))       # an hour
        self.assertEqual(300, server._sample_interval_for(86400))     # a day
        self.assertEqual(1800, server._sample_interval_for(604800))   # a week
        self.assertEqual(21600, server._sample_interval_for(2592000))  # a month
        self.assertGreater(server._sample_interval_for(86400 * 4000), 86400,
                           "a decade-long range must still be bounded")


class LogProvenanceTest(_StubbedServer):
    """Logs must say which deployment they came from, and shout when that is
    not the deployment serving traffic.

    The failure is silent and arrives at the worst moment: a deploy has just
    failed, someone asks for the logs to find out why the service is misbehaving,
    and reads the failed build's output while the service is still running the
    previous, working version. Both facts were in the old answer — the status
    was right there — but nothing distinguished them, so the reader compares
    nothing and spends half an hour debugging a version no request reaches."""

    # A failed build on top of a working release: dep-new is the NEWEST
    # deployment, dep-live is the one actually holding the container.
    _FAILED_ON_TOP = {
        "deployments(input:": {"deployments": {"edges": [
            {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                      "status": "SUCCESS", "deploymentStopped": False}},
            {"node": {"id": "dep-new", "createdAt": "2026-08-04T12:00:00Z",
                      "status": "FAILED", "deploymentStopped": False}},
        ]}},
        "deploymentLogs(deploymentId:": {"deploymentLogs": [
            {"timestamp": "2026-08-04T12:00:01Z", "message": "build failed"}]},
    }

    _HEALTHY = {
        "deployments(input:": {"deployments": {"edges": [
            {"node": {"id": "dep-old", "createdAt": "2026-08-01T10:00:00Z",
                      "status": "REMOVED", "deploymentStopped": False}},
            {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                      "status": "SUCCESS", "deploymentStopped": False}},
        ]}},
        "deploymentLogs(deploymentId:": {"deploymentLogs": [
            {"timestamp": "2026-08-04T10:00:01Z", "message": "listening on 8080"}]},
    }

    async def test_logs_from_a_failed_deployment_are_flagged(self):
        """The whole card. The newest deployment failed, so these logs describe
        a version nothing is running — and that must be the first thing in the
        answer, not a status the reader is expected to cross-check."""
        self.install(self._FAILED_ON_TOP)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertIs(False, result["deploymentIsRunning"])
        self.assertIn("warning", result,
                      "a failed deployment's logs were served with no warning")
        self.assertIn("NOT", result["warning"],
                      "the warning must say plainly that this is not the running version")
        self.assertIn("dep-live", result["warning"],
                      "the warning must name the deployment that IS running")
        self.assertEqual("dep-live", result["runningDeploymentId"])
        self.assertEqual("SUCCESS", result["runningDeploymentStatus"])
        self.assertEqual("warning", next(iter(result)),
                         "the warning must lead the answer, not trail it")

    async def test_running_logs_carry_no_warning(self):
        """The other half: a caller reading healthy logs must not have to wade
        through a caveat that does not apply. A warning that fires every time is
        a warning nobody reads when it matters."""
        self.install(self._HEALTHY)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertIs(True, result["deploymentIsRunning"])
        self.assertNotIn("warning", result)
        self.assertNotIn("runningDeploymentId", result,
                         "no second deployment to compare against — do not add fields")
        self.assertEqual("dep-live", result["deploymentId"])

    async def test_default_still_returns_the_newest_deployment(self):
        """Existing callers asked for the latest deployment's logs and must keep
        getting them: after a failed deploy its own output is the reason it
        failed. The new provenance is added ALONGSIDE, never instead."""
        session = self.install(self._FAILED_ON_TOP)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertEqual("dep-new", result["deploymentId"])
        self.assertEqual("FAILED", result["deploymentStatus"])
        self.assertEqual([{"timestamp": "2026-08-04T12:00:01Z",
                           "message": "build failed"}], result["logs"])
        logs = next(c for c in session.calls if "deploymentLogs(deploymentId:" in c["query"])
        self.assertEqual("dep-new", logs["variables"]["did"])

    async def test_source_running_reads_the_deployment_serving_traffic(self):
        """The opt-in half of the card: ask for the running one and get it,
        without having to know which id that is."""
        session = self.install(self._FAILED_ON_TOP)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1", "source": "running"})))

        self.assertEqual("dep-live", result["deploymentId"])
        self.assertIs(True, result["deploymentIsRunning"])
        self.assertNotIn("warning", result)
        logs = next(c for c in session.calls if "deploymentLogs(deploymentId:" in c["query"])
        self.assertEqual("dep-live", logs["variables"]["did"])

    async def test_logs_are_fetched_by_deployment_id_not_service_id(self):
        """deploymentLogs is keyed by a DEPLOYMENT id — the same trap deploy()
        and stop_service already carry a test for. A service id there matches no
        deployment, and Railway blames a missing deployment for a service that
        is running perfectly well."""
        session = self.install(self._HEALTHY)

        await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"}))

        logs = next(c for c in session.calls if "deploymentLogs(deploymentId:" in c["query"])
        self.assertEqual("dep-live", logs["variables"]["did"],
                         "a service id was sent where a deployment id belongs")
        self.assertNotIn("svc1", logs["variables"].values())

    async def test_a_stopped_deployment_does_not_count_as_running(self):
        """A stopped deployment keeps the status it had — Railway has no STOPPED
        status — so SUCCESS proves nothing on its own. Read deploymentStopped
        too, or a stopped service's logs are presented as the live version's."""
        self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-stopped", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "SUCCESS", "deploymentStopped": True}},
            ]}},
            "deploymentLogs(deploymentId:": {"deploymentLogs": []},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertIs(False, result["deploymentIsRunning"])
        self.assertIn("NO deployment", result["warning"])
        self.assertIsNone(result["runningDeploymentId"])

    async def test_source_running_refuses_when_nothing_is_running(self):
        """Asking for the running version when there is none must say so and
        name the statuses, rather than quietly handing back the failed build's
        logs under the label the caller asked for."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-new", "createdAt": "2026-08-04T12:00:00Z",
                          "status": "FAILED", "deploymentStopped": False}},
            ]}},
            "deploymentLogs(deploymentId:": {"deploymentLogs": []},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1", "source": "running"})))

        self.assertIn("No running deployment", result["error"])
        self.assertEqual(["FAILED"], result["recentStatuses"])
        self.assertFalse(
            [c for c in session.calls if "deploymentLogs(deploymentId:" in c["query"]],
            "fetched logs anyway and would have labelled them as the running version")

    async def test_an_unknown_source_is_refused_by_name(self):
        """A typo must not silently fall back to the default and answer a
        question that was not asked."""
        session = self.install(self._HEALTHY)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1", "source": "live"})))

        self.assertIn("live", result["error"])
        self.assertIn("running", result["error"])
        self.assertFalse(session.calls, "queried Railway despite a bad argument")

    async def test_a_service_that_never_deployed_is_reported_as_such(self):
        """Unchanged behaviour, kept locked: no deployments is its own answer."""
        self.install({"deployments(input:": {"deployments": {"edges": []}}})

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertIn("No deployments found", result["error"])


class BuildLogTest(_StubbedServer):
    """A failed BUILD must return the build's own output.

    Railway keeps a deployment's output in two queries with identical
    arguments: buildLogs(deploymentId) is the builder's, deploymentLogs is the
    container's. A build that fails never starts a container, so the second is
    empty and the reason exists only in the first — and this tool asked for the
    second alone, so it answered a failed deploy with `logs: []` and nothing
    else. The docstring promised the opposite ("the failed build's own output is
    the reason it failed"), so the emptiness read as "Railway kept nothing"
    rather than "asked the wrong query".

    What made it expensive is that the tool looked fine everywhere else: a
    CRASHED deployment, whose container did run, returns a full stack trace. The
    hole opens only at the moment it is needed most — a production build failing
    — and it cost a real investigation, which had to stop and hand over to
    someone with dashboard access.

    So both halves are pinned: the build output arrives when the container
    printed nothing, and the second query is NOT paid for when the container
    logs already answer the question."""

    # A build that failed: newest deployment, no container, nothing in the
    # container's log stream. The exact shape reported on the card.
    _BUILD_FAILED = {
        "deployments(input:": {"deployments": {"edges": [
            {"node": {"id": "dep-new", "createdAt": "2026-08-04T12:00:00Z",
                      "status": "FAILED", "deploymentStopped": False}},
            {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                      "status": "SUCCESS", "deploymentStopped": False}},
        ]}},
        "deploymentLogs(deploymentId:": {"deploymentLogs": []},
        "buildLogs(deploymentId:": {"buildLogs": [
            {"timestamp": "2026-08-04T12:00:30Z",
             "message": "ERROR: failed to solve: process \"/bin/sh -c pip "
                        "install -r requirements.txt\" did not complete"}]},
    }

    async def test_a_failed_build_returns_the_build_output(self):
        """The card. An empty `logs` must be accompanied by the build output
        that explains it, read from the same deployment."""
        session = self.install(self._BUILD_FAILED)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertEqual([], result["logs"])
        self.assertEqual(1, len(result["buildLogs"]),
                         "a failed build was answered without its build output")
        self.assertIn("pip install", result["buildLogs"][0]["message"])
        build = next(c for c in session.calls if "buildLogs(deploymentId:" in c["query"])
        self.assertEqual("dep-new", build["variables"]["did"],
                         "the build output came from a different deployment "
                         "than the one the answer names")

    async def test_the_empty_container_log_is_explained(self):
        """`logs: []` next to a populated `buildLogs` is the thing a reader has
        to interpret, so the answer says outright which one to read and why the
        other is empty — otherwise the emptiness still looks like the defect."""
        self.install(self._BUILD_FAILED)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        note = result["buildLogsNote"]
        self.assertIn("buildLogs", note, "the note must name the list to read")
        self.assertIn("dep-new", note)
        self.assertIn("FAILED", note)

    async def test_a_deployment_with_no_output_at_all_says_so(self):
        """When neither query has anything, the answer must not point at an
        empty `buildLogs` as if it held the reason. Railway keeping nothing and
        this tool asking the wrong query are exactly the two possibilities the
        card could not tell apart — so the one case where the answer really is
        "nothing was kept" has to say that in those words."""
        self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-new", "createdAt": "2026-08-04T12:00:00Z",
                          "status": "FAILED", "deploymentStopped": False}},
            ]}},
            "deploymentLogs(deploymentId:": {"deploymentLogs": []},
            "buildLogs(deploymentId:": {"buildLogs": []},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertEqual([], result["buildLogs"])
        self.assertIn("neither", result["buildLogsNote"])

    async def test_a_crashed_deployment_is_answered_by_one_query_as_before(self):
        """The case that already worked must not change or slow down. A CRASHED
        deployment ran, so its container output is the answer; fetching the
        build output on top would double the cost and bury the stack trace the
        caller came for."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "CRASHED", "deploymentStopped": False}},
            ]}},
            "deploymentLogs(deploymentId:": {"deploymentLogs": [
                {"timestamp": "2026-08-04T10:00:09Z",
                 "message": "Traceback (most recent call last):"}]},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertEqual(1, len(result["logs"]))
        self.assertNotIn("buildLogs", result)
        self.assertNotIn("buildLogsNote", result)
        self.assertFalse([c for c in session.calls
                          if "buildLogs(deploymentId:" in c["query"]])

    async def test_a_quiet_healthy_deployment_is_left_alone(self):
        """A service that deployed fine and has simply not logged yet is not a
        build problem. Empty is the honest answer there, and appending the build
        output would make every quiet service look like it needs diagnosing."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "SUCCESS", "deploymentStopped": False}},
            ]}},
            "deploymentLogs(deploymentId:": {"deploymentLogs": []},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertEqual([], result["logs"])
        self.assertNotIn("buildLogs", result)
        self.assertFalse([c for c in session.calls
                          if "buildLogs(deploymentId:" in c["query"]])

    async def test_build_output_can_be_asked_for_on_a_successful_build(self):
        """The automatic rule only fires on an empty container log, so a build
        that SUCCEEDED but was slow or produced something odd would be
        unreachable without an explicit way to ask."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "SUCCESS", "deploymentStopped": False}},
            ]}},
            "deploymentLogs(deploymentId:": {"deploymentLogs": [
                {"timestamp": "2026-08-04T10:00:01Z", "message": "listening"}]},
            "buildLogs(deploymentId:": {"buildLogs": [
                {"timestamp": "2026-08-04T09:59:00Z", "message": "exporting layers"}]},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1", "build_logs": "always"})))

        self.assertEqual(1, len(result["logs"]))
        self.assertEqual(1, len(result["buildLogs"]))
        self.assertNotIn("buildLogsNote", result,
                         "nothing to explain — the container logs are there too")
        self.assertTrue([c for c in session.calls
                         if "buildLogs(deploymentId:" in c["query"]])

    async def test_build_output_can_be_declined(self):
        """The pre-2026-08-06 behaviour stays reachable for a caller who wants
        one query and one list, and it must not smuggle the second one in."""
        session = self.install(self._BUILD_FAILED)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1", "build_logs": "never"})))

        self.assertEqual([], result["logs"])
        self.assertNotIn("buildLogs", result)
        self.assertFalse([c for c in session.calls
                          if "buildLogs(deploymentId:" in c["query"]])

    async def test_an_unknown_build_logs_value_is_refused_by_name(self):
        """Same rule as `source`: a typo must not fall back to the default and
        answer a different question than the one asked."""
        session = self.install(self._BUILD_FAILED)

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1", "build_logs": "yes"})))

        self.assertIn("yes", result["error"])
        self.assertIn("auto", result["error"])
        self.assertFalse(session.calls, "queried Railway despite a bad argument")

    async def test_a_refused_build_query_does_not_lose_the_container_logs(self):
        """The build query is an addition, so its failure must cost only itself.
        Letting the error propagate would turn answers that work today into
        exceptions — the opposite of the card."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-live", "createdAt": "2026-08-04T10:00:00Z",
                          "status": "SUCCESS", "deploymentStopped": False}},
            ]}},
            "deploymentLogs(deploymentId:": {"deploymentLogs": [
                {"timestamp": "2026-08-04T10:00:01Z", "message": "listening"}]},
            "buildLogs(deploymentId:": {"errors": [{"message": "Not Authorized"}]},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1", "build_logs": "always"})))

        self.assertEqual(1, len(result["logs"]),
                         "a failed extra query threw away the logs that worked")
        self.assertNotIn("buildLogs", result)
        self.assertIn("Not Authorized", result["buildLogsNote"])
        self.assertTrue([c for c in session.calls
                         if "buildLogs(deploymentId:" in c["query"]])


class RefusalWordingTest(_StubbedServer):
    """Railway refuses anything the account cannot see with a flat "Not
    Authorized", whether the id is mistyped, stale, someone else's or genuinely
    off limits — it will not even admit whether the thing exists. That message
    names the one cause that is usually NOT the problem, so an agent goes
    auditing permissions instead of re-reading the id it sent.

    The explanation is added in `_query_sync`, the single point every tool's
    errors pass through, so all of them gain it at once. Two properties are
    locked here: a refusal of this kind carries BOTH Railway's own words and the
    likely cause, and a failure of any other kind is not given the explanation —
    a confident wrong lead is worse than none.
    """

    _HINT = "not recognised on this account"

    async def test_a_refusal_carries_both_railways_words_and_the_likely_cause(self):
        """The reported case verbatim: listing services against a project the
        token cannot read answered with a bare "Not Authorized" and nothing
        else. Railway's wording stays — it is occasionally the real answer."""
        self.install({"project(id:": {"errors": [{"message": "Not Authorized"}]}})

        with self.assertRaises(Exception) as caught:
            await server.mcp.call_tool("list_services", {"project_id": "p-typo"})

        message = str(caught.exception)
        self.assertIn("Not Authorized", message,
                      "the platform's own message must survive, not be replaced")
        self.assertIn(self._HINT, message)
        self.assertIn("usually", message,
                      "it must read as the likely cause, not as a verdict the "
                      "code cannot actually reach")

    async def test_a_different_failure_is_not_given_this_explanation(self):
        """The other half. A refusal is recognised by its wording; anything else
        is passed through untouched, because an identifier explanation bolted
        onto an unrelated failure sends the reader somewhere there is nothing to
        find."""
        self.install({
            "me { workspaces": {"errors": [{"message": "Problem processing request"}]},
            "query { projects": {"errors": [{"message": "Problem processing request"}]},
        })

        result = json.loads(await _text(server.mcp.call_tool("list_projects", {})))

        self.assertIn("Problem processing request", result["error"])
        self.assertNotIn(self._HINT, json.dumps(result),
                         "an unrelated failure was blamed on the identifier")

    async def test_the_explanation_survives_the_tools_that_wrap_the_error(self):
        """Several tools catch the RuntimeError and fold it into their own
        message. Adding this at the shared boundary rather than per tool means
        those keep working AND gain the cause — check one, so a later refactor
        that re-raises a fresh error loses the test rather than the users."""
        self.install({"serviceInstanceRedeploy": {
            "errors": [{"message": "Not Authorized"}]}})

        result = json.loads(await _text(server.mcp.call_tool(
            "start_service", {"environment_id": "e1", "service_id": "svc1"})))

        self.assertIn("Not Authorized", result["error"])
        self.assertIn(self._HINT, result["error"])
        self.assertIn("list_environments", result["hint"],
                      "the tool's own advice must not be crowded out")

    async def test_a_refusal_cannot_carry_the_token(self):
        """An auth error is exactly the message most likely to quote the
        credential back at us. Railway's text is now handed on by our code, so
        it is redacted here as well as in _why."""
        original = server.TOKEN
        server.TOKEN = "super-secret-token"
        self.addCleanup(setattr, server, "TOKEN", original)
        self.install({"project(id:": {"errors": [
            {"message": "Not Authorized: Bearer super-secret-token"}]}})

        with self.assertRaises(Exception) as caught:
            await server.mcp.call_tool("list_services", {"project_id": "p1"})

        message = str(caught.exception)
        self.assertNotIn("super-secret-token", message)
        self.assertIn("***", message)


class ServiceSourceTest(_StubbedServer):
    """A service without a source is an empty shell that has to be finished in
    the dashboard, which is exactly what an agent cannot do. `create_service`
    and `connect_service` therefore carry Railway's `source` — a GitHub repo or
    a Docker image — and the two are mutually exclusive, because a service has
    one source and sending both leaves it ambiguous which one won.
    """

    async def test_create_service_attaches_a_docker_image(self):
        session = self.install({"serviceCreate": {"serviceCreate": {
            "id": "svc1", "name": "gotenberg"}}})

        result = json.loads(await _text(server.mcp.call_tool("create_service", {
            "project_id": "p1", "environment_id": "e1", "name": "gotenberg",
            "image": "gotenberg/gotenberg:8"})))

        self.assertEqual("svc1", result["id"])
        sent = session.calls[0]["variables"]["input"]
        self.assertEqual({"image": "gotenberg/gotenberg:8"}, sent["source"])
        self.assertNotIn("branch", sent,
                         "a branch is meaningless for an image and Railway "
                         "rejects the combination")

    async def test_create_service_attaches_a_repo_and_branch(self):
        session = self.install({"serviceCreate": {"serviceCreate": {
            "id": "svc1", "name": "api"}}})

        await _text(server.mcp.call_tool("create_service", {
            "project_id": "p1", "environment_id": "e1", "name": "api",
            "repo": "skyttedk/mcp.railway", "branch": "master"}))

        sent = session.calls[0]["variables"]["input"]
        self.assertEqual({"repo": "skyttedk/mcp.railway"}, sent["source"])
        self.assertEqual("master", sent["branch"])

    async def test_create_service_without_a_source_stays_the_old_shape(self):
        """The pre-existing three-argument call must keep working unchanged —
        both namespaces' callers use it."""
        session = self.install({"serviceCreate": {"serviceCreate": {
            "id": "svc1", "name": "empty"}}})

        await _text(server.mcp.call_tool("create_service", {
            "project_id": "p1", "environment_id": "e1", "name": "empty"}))

        sent = session.calls[0]["variables"]["input"]
        self.assertEqual({"projectId": "p1", "environmentId": "e1",
                          "name": "empty"}, sent)

    async def test_a_service_cannot_be_given_two_sources(self):
        session = self.install({"serviceCreate": {"serviceCreate": {"id": "svc1"}}})

        result = json.loads(await _text(server.mcp.call_tool("create_service", {
            "project_id": "p1", "environment_id": "e1", "name": "x",
            "repo": "a/b", "image": "nginx"})))

        self.assertIn("not both", result["error"])
        self.assertEqual([], session.calls,
                         "created the service anyway despite the ambiguity")

    async def test_connect_service_points_a_service_at_an_image(self):
        session = self.install({"serviceConnect": {"serviceConnect": {
            "id": "svc1", "name": "gotenberg"}}})

        await _text(server.mcp.call_tool("connect_service", {
            "service_id": "svc1", "image": "gotenberg/gotenberg:8"}))

        sent = session.calls[0]["variables"]["input"]
        self.assertEqual({"image": "gotenberg/gotenberg:8"}, sent)
        self.assertNotIn("branch", sent,
                         "the default branch leaked into an image connect")

    async def test_connect_service_still_defaults_the_branch_for_a_repo(self):
        session = self.install({"serviceConnect": {"serviceConnect": {"id": "svc1"}}})

        await _text(server.mcp.call_tool("connect_service", {
            "service_id": "svc1", "repo": "skyttedk/mcp.railway"}))

        self.assertEqual({"repo": "skyttedk/mcp.railway", "branch": "master"},
                         session.calls[0]["variables"]["input"])

    async def test_connect_service_refuses_to_do_nothing_quietly(self):
        session = self.install({"serviceConnect": {"serviceConnect": {"id": "svc1"}}})

        result = json.loads(await _text(server.mcp.call_tool(
            "connect_service", {"service_id": "svc1"})))

        self.assertIn("Nothing to connect", result["error"])
        self.assertEqual([], session.calls)


class ServiceConfigTest(_StubbedServer):
    """set_service_config is the one tool where an omitted argument and a
    cleared one must not mean the same thing: it sends a partial
    ServiceInstanceUpdateInput, and any key present in that payload is written.
    So a setting the caller never mentioned must not appear at all, or calling
    it to change the Dockerfile path silently wipes the healthcheck.
    """

    # The read half is answered too: both setters now confirm the service
    # instance exists before writing, so every successful call makes two
    # requests and the mutation is no longer calls[0].
    _ROUTE = {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
              "serviceInstance(serviceId": {
                  "serviceInstance": {"serviceId": "svc1", "serviceName": "web"}}}

    @staticmethod
    def _sent(session: _FakeSession) -> dict:
        """The input actually written, found by name rather than by position."""
        writes = [c for c in session.calls if "serviceInstanceUpdate" in c["query"]]
        assert len(writes) == 1, f"expected one write, got {len(writes)}"
        return writes[0]["variables"]["input"]

    async def test_only_the_settings_passed_are_sent(self):
        session = self.install(self._ROUTE)

        result = json.loads(await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1",
            "dockerfile_path": "docker/Dockerfile.web"})))

        self.assertTrue(result["updated"])
        self.assertEqual({"dockerfilePath": "docker/Dockerfile.web"},
                         self._sent(session),
                         "an untouched setting was included and would be "
                         "overwritten on the service")

    async def test_an_empty_string_clears_a_setting(self):
        """The counterpart: there has to be a way to remove an override, and it
        is the same "" convention set_start_command already uses."""
        session = self.install(self._ROUTE)

        await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1", "root_directory": ""}))

        self.assertEqual({"rootDirectory": None}, self._sent(session))

    async def test_an_empty_list_is_sent_as_a_list_not_as_null(self):
        """watchPatterns/preDeployCommand are list settings. Collapsing [] to
        null the way "" is collapsed would send the wrong clear for them."""
        session = self.install(self._ROUTE)

        await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1", "watch_patterns": []}))

        self.assertEqual({"watchPatterns": []}, self._sent(session))

    async def test_falsey_numbers_and_booleans_survive(self):
        """0 replicas and sleep_application=False are real values a caller may
        want. A truthiness filter would drop both."""
        session = self.install(self._ROUTE)

        await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1",
            "num_replicas": 0, "sleep_application": False}))

        self.assertEqual({"numReplicas": 0, "sleepApplication": False},
                         self._sent(session))

    async def test_an_unknown_builder_is_refused_before_the_call(self):
        """Railway takes `builder` as a GraphQL enum, so a bad value fails with
        a parse error naming neither the tool nor the argument. DOCKERFILE is
        the guess an agent actually makes — it is not a builder."""
        session = self.install(self._ROUTE)

        result = json.loads(await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1", "builder": "DOCKERFILE"})))

        self.assertIn("RAILPACK", result["error"])
        self.assertIn("dockerfile_path", result["error"],
                      "the refusal must point at what the caller actually wanted")
        self.assertEqual([], session.calls)

    async def test_a_call_with_no_settings_changes_nothing_and_says_so(self):
        session = self.install(self._ROUTE)

        result = json.loads(await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1"})))

        self.assertIn("nothing was changed", result["error"].lower())
        self.assertEqual([], session.calls)

    async def test_redeploy_is_opt_in(self):
        session = self.install({**self._ROUTE,
                                "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}})

        quiet = json.loads(await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1", "num_replicas": 2})))
        self.assertFalse(quiet["redeployed"])
        self.assertIn("next deploy", quiet["note"])
        self.assertEqual([], [c for c in session.calls
                              if "serviceInstanceRedeploy" in c["query"]])

        loud = json.loads(await _text(server.mcp.call_tool("set_service_config", {
            "environment_id": "e1", "service_id": "svc1", "num_replicas": 2,
            "redeploy": True})))
        self.assertTrue(loud["redeployed"])
        self.assertTrue([c for c in session.calls if "serviceInstanceRedeploy" in c["query"]])


class MissingServiceInstanceTest(_StubbedServer):
    """Regression: config writes reported success for an instance that was not
    there.

    A service lives in a project; its deploy config lives in a service
    *instance*, one per environment. Told to configure a service in an
    environment it has no instance in, Railway's serviceInstanceUpdate raised
    nothing — so both setters answered `updated: true`, set_service_config even
    echoing an `applied` block, while nothing was written anywhere. The caller
    learned the truth only at the next deploy ("Service Instance not found"),
    or never — read back, the settings were simply absent. A write that cannot
    land must be refused, not reported as done.
    """

    _WRITE = {"serviceInstanceUpdate": {"serviceInstanceUpdate": True}}
    _ABSENT = {**_WRITE, "serviceInstance(serviceId": {"serviceInstance": None}}
    # Railway's own answer for the same state, seen from get_service_instance.
    _NOT_FOUND = {**_WRITE, "serviceInstance(serviceId": {
        "errors": [{"message": "ServiceInstance not found"}]}}

    @staticmethod
    def _writes(session: _FakeSession) -> list[dict]:
        return [c for c in session.calls if "serviceInstanceUpdate" in c["query"]]

    async def _call(self, tool: str, routes: dict) -> tuple[dict, _FakeSession]:
        session = self.install(routes)
        args = {"environment_id": "env-prod", "service_id": "svc-pdf"}
        args.update({"set_start_command": {"start_command": "node serve.js"},
                     "set_service_config": {"num_replicas": 2},
                     "set_region": {"region": "europe-west4-drams3a"}}[tool])
        return json.loads(await _text(server.mcp.call_tool(tool, args))), session

    async def test_set_service_config_refuses_a_service_with_no_instance(self):
        result, session = await self._call("set_service_config", self._ABSENT)

        self.assertNotIn("updated", result,
                         "a write that never happened was reported as done")
        self.assertNotIn("applied", result,
                         "settings were echoed back as applied but went nowhere")
        self.assertIn("svc-pdf", result["error"])
        self.assertIn("env-prod", result["error"],
                      "the refusal must name the environment — the service does "
                      "exist, just not there")
        self.assertEqual([], self._writes(session))

    async def test_set_start_command_refuses_a_service_with_no_instance(self):
        result, session = await self._call("set_start_command", self._ABSENT)

        self.assertNotIn("updated", result)
        self.assertIn("svc-pdf", result["error"])
        self.assertIn("env-prod", result["error"])
        self.assertEqual([], self._writes(session))

    async def test_set_region_refuses_a_service_with_no_instance(self):
        """set_region writes through the same serviceInstanceUpdate mutation and
        had the same defect: a region change reported as applied while nothing
        was written, surfacing later as configuration that had simply vanished.
        """
        result, session = await self._call("set_region", self._ABSENT)

        self.assertNotIn("updated", result)
        self.assertNotIn("redeployed", result,
                         "a redeploy must not follow a write that never landed")
        self.assertIn("svc-pdf", result["error"])
        self.assertIn("env-prod", result["error"])
        self.assertEqual([], self._writes(session))

    async def test_railways_own_not_found_is_a_refusal_too(self):
        """Railway answers this state with a GraphQL error rather than a null,
        depending on how the pair is wrong. Both mean the same thing here."""
        for tool in ("set_service_config", "set_start_command", "set_region"):
            with self.subTest(tool=tool):
                result, session = await self._call(tool, self._NOT_FOUND)

                self.assertNotIn("updated", result)
                self.assertIn("svc-pdf", result["error"])
                self.assertEqual([], self._writes(session))

    async def test_an_existing_instance_is_still_written(self):
        """The guard must refuse the missing case only — the working path is
        the whole point of the tools."""
        present = {**self._WRITE, "serviceInstance(serviceId": {
            "serviceInstance": {"serviceId": "svc-pdf", "serviceName": "pdf"}}}

        for tool in ("set_service_config", "set_start_command", "set_region"):
            with self.subTest(tool=tool):
                result, session = await self._call(tool, present)

                self.assertTrue(result["updated"])
                self.assertEqual(1, len(self._writes(session)))

    async def test_a_rejected_mutation_is_not_reported_as_updated(self):
        """The result of the write used to be discarded outright. Only an
        explicit false is treated as a rejection, so a null or missing value
        keeps behaving exactly as it did."""
        rejected = {"serviceInstanceUpdate": {"serviceInstanceUpdate": False},
                    "serviceInstance(serviceId": {
                        "serviceInstance": {"serviceId": "svc-pdf",
                                            "serviceName": "pdf"}}}

        for tool in ("set_service_config", "set_region"):
            with self.subTest(tool=tool):
                result, _ = await self._call(tool, rejected)

                self.assertNotIn("updated", result)
                self.assertIn("svc-pdf", result["error"])


class ContainerLivenessTest(_StubbedServer):
    """A service with no container must not read as healthy.

    The defect this locks is the one that costs the most: a production Postgres
    whose container had been gone for five months, described as fine by every
    read tool. latestDeployment SUCCESS, deploymentStopped false, get_logs
    reporting deploymentIsRunning TRUE beside an empty log array — three days of
    outage that the tooling agreed was healthy, while the dependent API's
    "connection timeout" was blamed on the network.

    The status fields were not wrong so much as answering a different question:
    which deployment the service is ON, never whether a process exists. So the
    tests below are about the one signal that does answer it — resource usage —
    and about not overclaiming with it: an accusation is made ONLY from samples
    that exist and are all zero. No samples, a refused query, a young deployment
    or a SLEEPING one must claim nothing, because a check that cries wolf is
    ignored exactly like one that never fires. And a deployment that printed
    logs is alive by definition, so it must not pay for the extra query at all.
    """

    _OLD = "2026-03-01T10:00:00Z"       # comfortably older than the age guard

    def _routes(self, metrics, logs=(), status="SUCCESS", created=None):
        return {
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-1", "createdAt": created or self._OLD,
                          "status": status, "deploymentStopped": False}},
            ]}},
            "deploymentLogs(": {"deploymentLogs": list(logs)},
            "metrics(projectId:": metrics,
        }

    @staticmethod
    def _flat_zero():
        return {"metrics": [
            {"measurement": "CPU_USAGE",
             "values": [{"ts": 1, "value": 0}, {"ts": 2, "value": 0}]},
            {"measurement": "MEMORY_USAGE_GB",
             "values": [{"ts": 1, "value": 0}, {"ts": 2, "value": 0}]},
        ]}

    async def _logs(self, session_routes):
        self.install(session_routes)
        return json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

    async def test_a_dead_container_is_not_reported_as_running(self):
        """The incident itself: SUCCESS, not stopped, nothing printed, and every
        CPU/memory sample zero. A running process cannot use zero memory, so the
        answer must say the service is down instead of asserting it is up."""
        result = await self._logs(self._routes(self._flat_zero()))

        self.assertFalse(result["deploymentIsRunning"],
                         "a service with no container is still reported as "
                         "running — the field that misled the whole incident")
        self.assertEqual("no-resource-use", result["containerCheck"]["verdict"])
        self.assertIn("NO RUNNING CONTAINER", result["warning"])
        self.assertEqual(["warning"], list(result)[:1],
                         "the warning must lead the answer, not sit under the "
                         "fields that look healthy")

    async def test_a_quiet_but_live_service_is_still_running(self):
        """The other half, and the one that keeps the check usable: a healthy
        service can simply have nothing to say. Memory above zero settles it,
        and no warning may fire."""
        result = await self._logs(self._routes({"metrics": [
            {"measurement": "CPU_USAGE", "values": [{"ts": 1, "value": 0}]},
            {"measurement": "MEMORY_USAGE_GB", "values": [{"ts": 1, "value": 0.12}]},
        ]}))

        self.assertTrue(result["deploymentIsRunning"])
        self.assertEqual("resource-use-seen", result["containerCheck"]["verdict"])
        self.assertNotIn("warning", result)

    async def test_no_samples_at_all_accuses_nobody(self):
        """An empty metrics answer is also what an unavailable metrics backend
        looks like. Reporting a live service as dead on that basis would make
        the whole check something people learn to ignore."""
        result = await self._logs(self._routes({"metrics": []}))

        self.assertTrue(result["deploymentIsRunning"])
        self.assertEqual("not-checked", result["containerCheck"]["verdict"])
        self.assertNotIn("warning", result)

    async def test_a_refused_metrics_query_costs_only_itself(self):
        """Same rule as the build-log query: the extra check may never turn a
        working answer into an error."""
        routes = self._routes({"errors": [{"message": "Not Authorized"}]})
        result = await self._logs(routes)

        self.assertNotIn("error", result)
        self.assertEqual("not-checked", result["containerCheck"]["verdict"])
        self.assertTrue(result["deploymentIsRunning"])

    async def test_a_deployment_that_printed_is_not_probed(self):
        """Logs are proof of a container, so the second query would buy nothing.
        The check must cost a round trip only where it can change the answer."""
        session = self.install(self._routes(
            self._flat_zero(), logs=[{"timestamp": "t", "message": "hello"}]))

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertTrue(result["deploymentIsRunning"])
        self.assertNotIn("containerCheck", result)
        self.assertFalse([c for c in session.calls if "metrics(" in c["query"]],
                         "a service that is printing logs was probed anyway")

    async def test_a_just_created_deployment_is_not_accused(self):
        """A container that has only just started has not reported a sample yet,
        so zero usage means nothing about it — and a deploy is exactly when
        someone reads the logs."""
        fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        session = self.install(self._routes(self._flat_zero(), created=fresh))

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertTrue(result["deploymentIsRunning"])
        self.assertEqual("not-checked", result["containerCheck"]["verdict"])
        self.assertFalse([c for c in session.calls if "metrics(" in c["query"]])

    async def test_a_sleeping_deployment_is_not_accused(self):
        """Railway removes a sleeping app's container on purpose and says so in
        the status. Zero usage is the correct state, not a fault."""
        session = self.install(self._routes(self._flat_zero(), status="SLEEPING"))

        result = json.loads(await _text(server.mcp.call_tool(
            "get_logs", {"project_id": "p1", "environment_id": "e1",
                         "service_id": "svc1"})))

        self.assertEqual("not-checked", result["containerCheck"]["verdict"])
        self.assertNotIn("warning", result)
        self.assertFalse([c for c in session.calls if "metrics(" in c["query"]])

    async def test_restart_falls_back_to_redeploy_when_nothing_is_running(self):
        """Second finding of the same incident: deploymentRestart answered true
        against the dead service and started nothing, three times over, while
        start_service brought it straight back. A restart that cannot work must
        use the mutation that does, and say which one it used."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-1", "createdAt": self._OLD, "status": "SUCCESS"}},
            ]}},
            "metrics(projectId:": self._flat_zero(),
            "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True},
            "deploymentRestart": {"deploymentRestart": True},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "deploy", {"project_id": "p1", "environment_id": "e1",
                       "service_id": "svc1"})))

        self.assertTrue(result["restarted"])
        self.assertEqual("serviceInstanceRedeploy", result["method"])
        self.assertEqual("no-resource-use", result["containerCheck"]["verdict"])
        self.assertTrue([c for c in session.calls
                         if "serviceInstanceRedeploy" in c["query"]])
        self.assertFalse([c for c in session.calls
                          if "deploymentRestart" in c["query"]],
                         "restarted a deployment whose container is gone — the "
                         "call Railway confirms and does not act on")

    async def test_a_live_container_is_still_restarted_the_old_way(self):
        """The fallback is for the dead case only. A service that is up must
        keep getting the cheap in-place restart, with the answer callers already
        parse."""
        session = self.install({
            "deployments(input:": {"deployments": {"edges": [
                {"node": {"id": "dep-1", "createdAt": self._OLD, "status": "SUCCESS"}},
            ]}},
            "metrics(projectId:": {"metrics": [
                {"measurement": "MEMORY_USAGE_GB", "values": [{"ts": 1, "value": 0.4}]},
            ]},
            "deploymentRestart": {"deploymentRestart": True},
        })

        result = json.loads(await _text(server.mcp.call_tool(
            "deploy", {"project_id": "p1", "environment_id": "e1",
                       "service_id": "svc1"})))

        self.assertEqual({"deploymentId": "dep-1", "deploymentStatus": "SUCCESS",
                          "restarted": True}, result)
        self.assertFalse([c for c in session.calls
                          if "serviceInstanceRedeploy" in c["query"]])

    async def test_the_listing_admits_it_cannot_see_a_container(self):
        """list_services is where the dead service was looked at first, and its
        fields cannot answer this at any price — a per-instance metrics query
        for every service would be unaffordable. So the description has to carry
        the warning and name the tools that can."""
        tools = {t.name: (t.description or "").lower()
                 for t in await server.mcp.list_tools()}

        self.assertIn("not proof of a running", tools["list_services"])
        self.assertIn("get_metrics", tools["list_services"])
        self.assertIn("containercheck", tools["get_logs"],
                      "get_logs no longer documents the field that carries the "
                      "evidence")


async def _text(call) -> str:
    """Pull the tool's string return value out of whatever call_tool answers.

    The SDK has returned bare content, (content, structured) and dicts across
    versions; every tool here returns a JSON string, so find that.
    """
    result = await call
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        result = result.get("content", result)
    if isinstance(result, list) and result:
        return result[0].text
    raise AssertionError(f"could not read tool output from {result!r}")


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        contract = asyncio.run(_current_contract())
        CONTRACT_FILE.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {len(contract)} tools to {CONTRACT_FILE}")
    else:
        unittest.main()

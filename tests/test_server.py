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

    A route value that is a LIST is consumed one entry per matching call, and
    the last entry keeps answering once the list runs out. Substring routing
    alone cannot express "the same query answers differently the second time",
    which is exactly what a write followed by a read-back verification needs:
    both the guard read and the verify read are `serviceInstance(serviceId:`.
    Each entry is then interpreted by the rules above, so a sequence can mix
    payloads, GraphQL errors and exceptions.
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
                if isinstance(data, list):
                    data = data[0] if len(data) == 1 else data.pop(0)
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


class InFlightDeploymentTest(_StubbedServer):
    """A deployment that is still being built is not a stopped one.

    Railway answered `deploymentStopped: true` for a deployment that was
    actively BUILDING and succeeded seconds later (observed 2026-08-06 on the
    riskwave-site project; the deployment detail view said SUCCESS/running).
    The flag only means something once a deployment has a container to stop, so
    on an in-flight status it is noise — and noise that reads as "this deploy is
    dead" precisely while a build is running, which is what makes an agent give
    up on a healthy deploy or fire a redeploy nudge on top of it.

    The correction is deliberately narrow: only statuses that cannot have a
    container yet are touched. A genuinely stopped, crashed or failed deployment
    keeps the flag, or the field would stop meaning anything at all and the
    stopped-service blindness StopStartTest exists for would come straight
    back."""

    @staticmethod
    def _listing(status: str, stopped: bool) -> dict:
        return {"project(id:": {"project": {"services": {"edges": [
            {"node": {"id": "svc1", "name": "api", "serviceInstances": {"edges": [
                {"node": {"environmentId": "e1", "region": None, "numReplicas": 1,
                          "latestDeployment": {"id": "dep-new",
                                               "createdAt": "2026-08-06T08:56:00Z",
                                               "status": status,
                                               "deploymentStopped": stopped}}},
            ]}}},
        ]}}}}

    async def _latest(self, status: str, stopped: bool) -> dict:
        self.install(self._listing(status, stopped))
        result = json.loads(await _text(server.mcp.call_tool(
            "list_services", {"project_id": "p1"})))
        return result[0]["instances"][0]["latestDeployment"]

    async def test_a_building_deployment_is_not_reported_as_stopped(self):
        """The card itself: a build in progress must not read as a dead deploy."""
        latest = await self._latest("BUILDING", True)

        self.assertFalse(latest["deploymentStopped"],
                         "a BUILDING deployment has no container and cannot have "
                         "been stopped — reporting it as stopped tells an agent "
                         "the deploy is dead while it is on its way up")
        self.assertTrue(latest["railwayDeploymentStopped"],
                        "Railway's own value must still be visible, not silently "
                        "dropped")
        self.assertIn("BUILDING", latest["deploymentStoppedNote"],
                      "the note must say which status was corrected")

    async def test_every_in_flight_status_is_corrected(self):
        """BUILDING is the one that was seen, not the only one that can happen:
        a deployment is equally container-less while queued or deploying."""
        for status in server._IN_FLIGHT_STATUSES:
            with self.subTest(status=status):
                latest = await self._latest(status, True)
                self.assertFalse(latest["deploymentStopped"],
                                 f"{status} is in flight, not stopped")

    async def test_a_genuinely_stopped_deployment_still_reads_as_stopped(self):
        """Regression guard. Railway has no STOPPED status, so a service stopped
        by stop_service is SUCCESS + deploymentStopped — the flag is the only
        evidence there is, and a blanket false would hide every stopped service
        again."""
        for status in ("SUCCESS", "SLEEPING", "CRASHED", "FAILED"):
            with self.subTest(status=status):
                latest = await self._latest(status, True)
                self.assertTrue(latest["deploymentStopped"],
                                f"a {status} deployment flagged stopped must stay "
                                "stopped — this field is how a stopped service is "
                                "told apart from a running one")
                self.assertNotIn("deploymentStoppedNote", latest,
                                 "nothing was corrected, so nothing should be "
                                 "explained away")

    async def test_an_unstopped_deployment_is_left_exactly_as_it_came(self):
        """The common case must gain no extra fields to reason about."""
        latest = await self._latest("BUILDING", False)

        self.assertFalse(latest["deploymentStopped"])
        self.assertNotIn("railwayDeploymentStopped", latest)
        self.assertNotIn("deploymentStoppedNote", latest)

    async def test_the_description_says_an_in_flight_deploy_is_never_stopped(self):
        """An agent reads the tool list, not this file. If the correction is not
        described, the next reader still distrusts the field."""
        tools = {t.name: (t.description or "").lower()
                 for t in await server.mcp.list_tools()}
        listing = tools["list_services"]

        self.assertIn("building", listing,
                      "the description does not mention the in-flight case at all")
        self.assertIn("railwaydeploymentstopped", listing,
                      "the raw value is returned but never explained")


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


class EnvironmentLifecycleTest(_StubbedServer):
    """Deleting an environment is the second irreversible operation here, and it
    destroys more than serviceDelete does: every service instance in it, their
    variables, their deployments and their volumes' data.

    It exists to be driven by an ephemeral-preview script — a loop that creates
    and destroys environments with no human between the request and the
    mutation — so the property under test is that the loop can only ever reach
    the environments it made. A name that carries a throwaway prefix goes; a
    long-lived one needs the intent spelled out; `production`, `test` and the
    project's own default environment are refused whatever is passed, because a
    flag that can unlock them is a flag a retry will eventually pass. Every
    refusal must leave the account untouched and name the environment, and
    update_deployment_trigger must refuse rather than guess when the service it
    is given has no single trigger to rewrite.
    """

    _PROJECT = {"project(id:": {"project": {"id": "p1", "name": "riskwave-app",
                                            "baseEnvironmentId": "env-prod",
                                            "primaryEnvironmentId": "env-prod"}}}
    _DELETE = {"environmentDelete": {"environmentDelete": True}}

    @staticmethod
    def _env(name: str, ephemeral: bool = False, env_id: str = "env-1") -> dict:
        return {"environment(id:": {"environment": {
            "id": env_id, "name": name, "projectId": "p1",
            "isEphemeral": ephemeral}}}

    @staticmethod
    def _deletions(session: _FakeSession) -> list[dict]:
        return [c for c in session.calls if "environmentDelete" in c["query"]]

    async def _delete(self, routes: dict, args: dict) -> tuple[dict, _FakeSession]:
        session = self.install(routes)
        result = json.loads(await _text(server.mcp.call_tool("delete_environment", args)))
        return result, session

    # ── create ──────────────────────────────────────────────────────

    async def test_creating_from_a_source_environment_asks_railway_to_clone(self):
        """The whole point of the create half: sourceEnvironmentId must reach
        Railway, or every "clone" silently produces an empty environment."""
        session = self.install({"environmentCreate": {"environmentCreate": {
            "id": "env-new", "name": "pr-42", "projectId": "p1",
            "isEphemeral": True, "createdAt": "2026-08-13T10:00:00Z"}}})
        result = json.loads(await _text(server.mcp.call_tool(
            "create_environment", {"name": "pr-42", "project_id": "p1",
                                   "source_environment_id": "env-test",
                                   "ephemeral": True})))

        sent = session.calls[0]["variables"]["input"]
        self.assertEqual("env-test", sent["sourceEnvironmentId"])
        self.assertEqual({"projectId": "p1", "name": "pr-42",
                          "sourceEnvironmentId": "env-test", "ephemeral": True},
                         sent)
        self.assertEqual("env-new", result["id"])
        self.assertEqual("env-test", result["clonedFrom"])
        self.assertIn("background", result["note"],
                      "a clone that is still filling in must say so, or the "
                      "caller reads an empty list_services as a failed clone")

    async def test_creating_without_a_source_sends_no_clone_fields(self):
        """An omitted source must not travel as an empty string — Railway would
        take that for an id."""
        session = self.install({"environmentCreate": {"environmentCreate": {
            "id": "env-new", "name": "scratch", "projectId": "p1",
            "isEphemeral": False, "createdAt": "2026-08-13T10:00:00Z"}}})
        result = json.loads(await _text(server.mcp.call_tool(
            "create_environment", {"name": "scratch", "project_id": "p1"})))

        self.assertEqual({"projectId": "p1", "name": "scratch"},
                         session.calls[0]["variables"]["input"])
        self.assertIsNone(result["clonedFrom"])

    async def test_creating_without_a_project_creates_nothing(self):
        """Our own refusal, so nothing explains it for us: it must name both
        ways forward and cost no request."""
        original = server.DEFAULT_PROJECT
        server.DEFAULT_PROJECT = ""
        self.addCleanup(setattr, server, "DEFAULT_PROJECT", original)
        session = self.install({"environmentCreate": {"environmentCreate": {}}})
        result = json.loads(await _text(server.mcp.call_tool(
            "create_environment", {"name": "pr-42"})))

        self.assertIn("MCP_DEFAULT_PROJECT_ID", result["error"])
        self.assertIn("Nothing was created", result["error"])
        self.assertEqual([], session.calls)

    # ── delete ──────────────────────────────────────────────────────

    async def test_an_ephemeral_name_is_deleted_and_named(self):
        """The happy path the preview script rides: a pr- environment goes
        without a flag, and the answer says which one went."""
        result, session = await self._delete(
            {**self._env("pr-42"), **self._PROJECT, **self._DELETE},
            {"environment_id": "env-1"})

        self.assertNotIn("error", result)
        self.assertIs(True, result["deleted"])
        self.assertEqual("pr-42", result["environmentName"])
        self.assertIn("cannot be undone", result["note"])
        self.assertEqual(["env-1"],
                         [d["variables"]["id"] for d in self._deletions(session)])

    async def test_railways_own_ephemeral_flag_is_enough(self):
        """Railway tags its PR environments isEphemeral without using our
        prefixes; that is the same statement, made by the platform."""
        result, _ = await self._delete(
            {**self._env("gh-1234", ephemeral=True), **self._PROJECT, **self._DELETE},
            {"environment_id": "env-1"})

        self.assertNotIn("error", result)
        self.assertIs(True, result["deleted"])

    async def test_an_ordinary_environment_needs_the_intent_spelled_out(self):
        """'staging' is nobody's throwaway. Refuse first, and say exactly what
        would unlock it — a refusal without a way forward gets worked around."""
        result, session = await self._delete(
            {**self._env("staging"), **self._PROJECT, **self._DELETE},
            {"environment_id": "env-1"})

        self.assertIn("does not look like a throwaway", result["error"])
        self.assertIn("confirm_permanent_delete=true", result["error"])
        self.assertEqual("staging", result["environmentName"])
        self.assertEqual([], self._deletions(session),
                         "an unconfirmed delete fired anyway")

        result, session = await self._delete(
            {**self._env("staging"), **self._PROJECT, **self._DELETE},
            {"environment_id": "env-1", "confirm_permanent_delete": True})

        self.assertNotIn("error", result)
        self.assertEqual(["env-1"],
                         [d["variables"]["id"] for d in self._deletions(session)])

    async def test_production_and_test_are_refused_even_when_confirmed(self):
        """The flag must not be a master key. These two are the environments a
        preview loop has no business reaching, whatever it passes."""
        for name in ("production", "Production", "test"):
            with self.subTest(name=name):
                result, session = await self._delete(
                    {**self._env(name), **self._PROJECT, **self._DELETE},
                    {"environment_id": "env-1",
                     "confirm_permanent_delete": True})

                self.assertIn("protected", result["error"])
                self.assertIn(name, result["error"],
                              "the refusal does not name the environment")
                self.assertEqual([], self._deletions(session))

    async def test_the_projects_default_environment_is_refused_by_id(self):
        """A default environment need not be called production — the project
        says which one it is, and that answer wins over the name."""
        result, session = await self._delete(
            {**self._env("main", env_id="env-prod"), **self._PROJECT, **self._DELETE},
            {"environment_id": "env-prod", "confirm_permanent_delete": True})

        self.assertIn("default environment", result["error"])
        self.assertIn("riskwave-app", result["error"])
        self.assertEqual([], self._deletions(session))

    async def test_an_unconfirmable_id_deletes_nothing(self):
        """Same guard as delete_service: if Railway will not read the
        environment back, the mutation must not fire at all."""
        result, session = await self._delete(
            {"environment(id:": {"errors": [{"message": "Not Authorized"}]},
             **self._PROJECT, **self._DELETE},
            {"environment_id": "env-typo"})

        self.assertIn("env-typo", result["error"])
        self.assertIn("Not Authorized", result["error"],
                      "the platform's own message must survive, not be swallowed")
        self.assertEqual([], self._deletions(session))

    async def test_an_unreadable_project_does_not_unlock_the_delete(self):
        """The default-environment check is a guard, so failing to run it must
        refuse — not fall through to the delete."""
        result, session = await self._delete(
            {**self._env("pr-42"),
             "project(id:": {"errors": [{"message": "Not Authorized"}]},
             **self._DELETE},
            {"environment_id": "env-1"})

        self.assertIn("Nothing was deleted", result["error"])
        self.assertEqual([], self._deletions(session))

    async def test_an_unknown_environment_deletes_nothing(self):
        result, session = await self._delete(
            {"environment(id:": {"environment": None}, **self._DELETE},
            {"environment_id": "env-gone"})

        self.assertIn("No environment with id env-gone", result["error"])
        self.assertEqual([], self._deletions(session))

    # ── deployment trigger ──────────────────────────────────────────

    async def test_the_single_trigger_for_that_service_is_repointed(self):
        """A trigger belongs to one service in one environment; the update must
        hit that one's id, not the first trigger in the project."""
        session = self.install({
            "deploymentTriggers(": {"deploymentTriggers": {"edges": [
                {"node": {"id": "trg-1", "branch": "main", "repository": "o/r",
                          "serviceId": "svc-1", "environmentId": "env-1"}}]}},
            "deploymentTriggerUpdate": {"deploymentTriggerUpdate": {
                "id": "trg-1", "branch": "feat/x", "repository": "o/r",
                "serviceId": "svc-1", "environmentId": "env-1"}}})
        result = json.loads(await _text(server.mcp.call_tool(
            "update_deployment_trigger",
            {"environment_id": "env-1", "service_id": "svc-1",
             "branch": "feat/x", "project_id": "p1"})))

        update = [c for c in session.calls if "deploymentTriggerUpdate" in c["query"]]
        self.assertEqual(1, len(update))
        self.assertEqual("trg-1", update[0]["variables"]["id"])
        self.assertEqual({"branch": "feat/x"}, update[0]["variables"]["input"])
        self.assertEqual("main", result["previousBranch"])
        self.assertIn("nothing was deployed", result["note"].lower(),
                      "a config-only write that reads as a deploy is how an "
                      "agent reports new code live that never built")

    async def test_a_service_with_no_trigger_is_refused(self):
        """An image-sourced service has no trigger. Creating one is a different
        operation, so this must refuse and say so rather than write nothing and
        report success."""
        session = self.install({
            "deploymentTriggers(": {"deploymentTriggers": {"edges": []}},
            "deploymentTriggerUpdate": {"deploymentTriggerUpdate": {}}})
        result = json.loads(await _text(server.mcp.call_tool(
            "update_deployment_trigger",
            {"environment_id": "env-1", "service_id": "svc-1",
             "branch": "feat/x", "project_id": "p1"})))

        self.assertIn("no deployment trigger", result["error"])
        self.assertEqual([], [c for c in session.calls
                              if "deploymentTriggerUpdate" in c["query"]])

    async def test_more_than_one_trigger_is_refused_and_both_are_shown(self):
        session = self.install({
            "deploymentTriggers(": {"deploymentTriggers": {"edges": [
                {"node": {"id": "trg-1", "branch": "main", "repository": "o/r",
                          "serviceId": "svc-1", "environmentId": "env-1"}},
                {"node": {"id": "trg-2", "branch": "dev", "repository": "o/r",
                          "serviceId": "svc-1", "environmentId": "env-1"}}]}},
            "deploymentTriggerUpdate": {"deploymentTriggerUpdate": {}}})
        result = json.loads(await _text(server.mcp.call_tool(
            "update_deployment_trigger",
            {"environment_id": "env-1", "service_id": "svc-1",
             "branch": "feat/x", "project_id": "p1"})))

        self.assertIn("does not identify one", result["error"])
        self.assertEqual({"trg-1", "trg-2"}, {t["id"] for t in result["triggers"]})
        self.assertEqual([], [c for c in session.calls
                              if "deploymentTriggerUpdate" in c["query"]])

    async def test_an_empty_branch_changes_nothing(self):
        session = self.install({
            "deploymentTriggers(": {"deploymentTriggers": {"edges": []}}})
        result = json.loads(await _text(server.mcp.call_tool(
            "update_deployment_trigger",
            {"environment_id": "env-1", "service_id": "svc-1",
             "branch": "", "project_id": "p1"})))

        self.assertIn("Nothing was changed", result["error"])
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


class RegionOverrideTest(_StubbedServer):
    """A region override has to be removable, not just settable.

    set_region could only ever write a region name, so once a service had an
    override the only way back to the default region was the Railway dashboard
    — the tool that made the change could not undo it. The clear is the same ""
    convention set_start_command and set_service_config already use, and it has
    to travel as an explicit null: `region` is a nullable String on
    ServiceInstanceUpdateInput, and leaving the key out means "untouched", not
    "reset".
    """

    # The guard read, the verify read and get_service_instance are the same
    # `serviceInstance(serviceId:` query, so one route answers all three; what
    # the payload's `region` says is therefore what Railway "stored". A route
    # whose region equals the value being written is a Railway that kept it;
    # one that stays null is the live defect.
    _ROUTE = {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
              "serviceInstance(serviceId": {
                  "serviceInstance": {"serviceId": "svc1", "serviceName": "web",
                                      "region": None}}}

    @staticmethod
    def _stored(region) -> dict:
        """A Railway that actually keeps what set_region writes."""
        return {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
                "serviceInstance(serviceId": {
                    "serviceInstance": {"serviceId": "svc1", "serviceName": "web",
                                        "region": region}}}

    @staticmethod
    def _sent(session: _FakeSession) -> dict:
        writes = [c for c in session.calls if "serviceInstanceUpdate" in c["query"]]
        assert len(writes) == 1, f"expected one write, got {len(writes)}"
        return writes[0]["variables"]["input"]

    async def _set(self, session_routes: dict, region: str, **extra) -> tuple[dict, _FakeSession]:
        session = self.install(session_routes)
        args = {"environment_id": "e1", "service_id": "svc1", "region": region}
        args.update(extra)
        return json.loads(await _text(server.mcp.call_tool("set_region", args))), session

    async def test_a_region_name_is_still_written(self):
        """The existing path must be untouched by the clear — and still report
        success when Railway does keep the value, so the read-back guard is a
        check on reality rather than a blanket refusal."""
        result, session = await self._set(
            self._stored("europe-west4-drams3a"), "europe-west4-drams3a")

        self.assertEqual({"region": "europe-west4-drams3a"}, self._sent(session))
        self.assertEqual("europe-west4-drams3a", result["region"])
        self.assertFalse(result["cleared"])
        self.assertTrue(result["updated"])
        self.assertTrue(result["verified"])
        self.assertIn("next deploy", result["note"])

    async def test_an_empty_region_clears_the_override(self):
        """The key must be present and null. An omitted key leaves Railway's
        stored override exactly where it was, which is the bug this fixes."""
        result, session = await self._set(self._ROUTE, "")

        sent = self._sent(session)
        self.assertIn("region", sent,
                      "the key was dropped, so the override was left in place")
        self.assertIsNone(sent["region"])
        self.assertIsNone(result["region"])
        self.assertTrue(result["cleared"])
        self.assertTrue(result["updated"])
        self.assertIn("default region", result["note"])

    async def test_clearing_still_refuses_a_missing_service_instance(self):
        """The clear is a serviceInstanceUpdate like any other, so it inherits
        the guard — Railway accepts it silently for an absent instance too."""
        absent = {**self._ROUTE, "serviceInstance(serviceId": {"serviceInstance": None}}
        result, session = await self._set(absent, "")

        self.assertNotIn("updated", result)
        self.assertIn("svc1", result["error"])
        self.assertIn("e1", result["error"])
        self.assertEqual([], [c for c in session.calls
                              if "serviceInstanceUpdate" in c["query"]])

    async def test_clearing_can_redeploy_immediately(self):
        result, session = await self._set(
            {**self._ROUTE,
             "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}},
            "", redeploy=True)

        self.assertTrue(result["cleared"])
        self.assertTrue(result["redeployed"])
        self.assertTrue([c for c in session.calls
                         if "serviceInstanceRedeploy" in c["query"]])

    async def test_a_dropped_region_write_is_an_error_not_a_success(self):
        """The defect this class is really guarding: Railway accepts the
        region, answers true, stores nothing. `updated: true` on the strength
        of that boolean is a lie, and it is the only signal the tool used to
        have. _ROUTE keeps region null, which is exactly what the live API did
        for every value tried on 2026-08-10."""
        result, _ = await self._set(self._ROUTE, "us-west2")

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertEqual("us-west2", result["sent"])
        self.assertIsNone(result["observed"])
        # Both values have to be in the sentence: "it did not work" without
        # them sends the next reader looking in the wrong layer.
        self.assertIn("us-west2", result["error"])
        self.assertIn("multiRegionConfig", result["error"])
        self.assertIn("svc1", result["error"])
        self.assertIn("e1", result["error"])

    async def test_a_dropped_write_is_verified_before_any_redeploy(self):
        """A deployment triggered to pick up a change that was never stored
        restarts a service for nothing and moves the surprise later."""
        result, session = await self._set(
            {**self._ROUTE,
             "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}},
            "us-west2", redeploy=True)

        self.assertFalse(result["verified"])
        self.assertEqual([], [c for c in session.calls
                              if "serviceInstanceRedeploy" in c["query"]])

    async def test_the_check_reads_back_after_the_write_not_before(self):
        """A verification satisfied by the guard read the tool already made
        would pass while proving nothing, so the order is the guarantee."""
        _, session = await self._set(self._ROUTE, "us-west2")

        kinds = ["write" if "serviceInstanceUpdate" in c["query"] else "read"
                 for c in session.calls]
        self.assertEqual(["read", "write", "read"], kinds)

    async def test_railway_refusing_the_check_is_not_reported_as_success(self):
        """Third outcome, distinct from both: the write went out and then the
        read-back failed, so whether it landed is unknown. Reporting that as
        success is the original bug wearing a different hat."""
        routes = {**self._ROUTE, "serviceInstance(serviceId": [
            {"serviceInstance": {"serviceId": "svc1", "serviceName": "web",
                                 "region": None}},
            {"errors": [{"message": "Not Authorized"}]},
        ]}
        result, session = await self._set(routes, "us-west2")

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertIn("would not confirm", result["error"])
        self.assertIn("Not Authorized", result["error"])
        # The write really was attempted — this is not the pre-write guard.
        self.assertEqual(1, len([c for c in session.calls
                                 if "serviceInstanceUpdate" in c["query"]]))


class BuildCommandTest(_StubbedServer):
    """The build command has to be writable, not only readable.

    get_service_instance reported `buildCommand` and set_start_command changed
    the other half of the deploy, but the build command had no setter of its
    own — a production service left carrying a build command from an earlier
    architecture could be seen and not corrected, so the fix went to a human
    with dashboard access. set_build_command mirrors set_start_command exactly:
    `""` clears the override as an explicit null (an omitted key means
    "untouched" to ServiceInstanceUpdateInput, not "reset"), the redeploy is
    opt-in, and a missing service instance is refused rather than reported as
    written.

    And it mirrors it in the defect too: sending the null is necessary but not
    sufficient, because Railway accepts that null, answers `true` and keeps the
    stored command — verified live 2026-08-10, in the same session where a
    `set_num_replicas` through the identical mutation landed. So the write is
    read back, exactly as set_region's is, and a clear that changed nothing is
    an error rather than the success it used to invent. The read-back covers
    every write, not only the clear: a value echoed back unread is a guess
    whichever value it is.
    """

    # No buildCommand in the payload, so the field reads back null — a Railway
    # that kept whatever it had is `_stored(...)` below. The guard read, the
    # verify read and get_service_instance are all the same
    # `serviceInstance(serviceId:` query, so one route answers all three.
    _ROUTE = {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
              "serviceInstance(serviceId": {
                  "serviceInstance": {"serviceId": "svc1", "serviceName": "web"}}}

    @staticmethod
    def _stored(build_command) -> dict:
        """A Railway that actually keeps what set_build_command writes."""
        return {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
                "serviceInstance(serviceId": {
                    "serviceInstance": {"serviceId": "svc1", "serviceName": "web",
                                        "buildCommand": build_command}}}

    @staticmethod
    def _sent(session: _FakeSession) -> dict:
        writes = [c for c in session.calls if "serviceInstanceUpdate" in c["query"]]
        assert len(writes) == 1, f"expected one write, got {len(writes)}"
        return writes[0]["variables"]["input"]

    async def _set(self, session_routes: dict, build_command: str,
                   **extra) -> tuple[dict, _FakeSession]:
        session = self.install(session_routes)
        args = {"environment_id": "e1", "service_id": "svc1",
                "build_command": build_command}
        args.update(extra)
        return json.loads(await _text(
            server.mcp.call_tool("set_build_command", args))), session

    async def test_a_build_command_is_written(self):
        result, session = await self._set(self._stored("npm run build"),
                                          "npm run build")

        self.assertEqual({"buildCommand": "npm run build"}, self._sent(session),
                         "the write must touch buildCommand and nothing else")
        self.assertEqual("npm run build", result["buildCommand"])
        self.assertTrue(result["updated"])
        self.assertTrue(result["verified"])
        self.assertFalse(result["redeployed"])
        self.assertIn("next deploy", result["note"])

    async def test_an_empty_build_command_clears_the_override(self):
        """The key must be present and null, exactly as set_start_command and
        set_region send their clear — dropping it leaves Railway's stored
        override in place, which is the state this card was filed about.

        _ROUTE reads the field back as null, i.e. a service with no build
        command to remove: the end state asked for is the end state observed,
        so this is a success and honestly one."""
        result, session = await self._set(self._ROUTE, "")

        sent = self._sent(session)
        self.assertIn("buildCommand", sent,
                      "the key was dropped, so the override was left in place")
        self.assertIsNone(sent["buildCommand"])
        self.assertIsNone(result["buildCommand"])
        self.assertTrue(result["updated"])
        self.assertTrue(result["verified"])

    async def test_a_dropped_clear_is_an_error_not_a_success(self):
        """The defect this card was filed about: Railway takes the explicit
        null, answers true and keeps the command. `updated: true` on the
        strength of that boolean is a lie — and unlike set_region's, this one
        only appears when clearing, since a value written the same way lands.
        """
        result, _ = await self._set(self._stored("npm run build"), "")

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertIsNone(result["sent"])
        self.assertEqual("npm run build", result["observed"])
        # Both values belong in the sentence: "it did not work" without them
        # sends the next reader looking in the wrong layer.
        self.assertIn("npm run build", result["error"])
        self.assertIn("dashboard", result["error"])
        self.assertIn("svc1", result["error"])
        self.assertIn("e1", result["error"])

    async def test_a_dropped_clear_is_verified_before_any_redeploy(self):
        """A deployment triggered to pick up a change that was never stored
        restarts a service for nothing and moves the surprise later."""
        routes = {**self._stored("npm run build"),
                  "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}}
        result, session = await self._set(routes, "", redeploy=True)

        self.assertFalse(result["verified"])
        self.assertEqual([], [c for c in session.calls
                              if "serviceInstanceRedeploy" in c["query"]])

    async def test_the_check_reads_back_after_the_write_not_before(self):
        """A verification satisfied by the guard read the tool already made
        would pass while proving nothing, so the order is the guarantee."""
        _, session = await self._set(self._stored("npm run build"), "")

        kinds = ["write" if "serviceInstanceUpdate" in c["query"] else "read"
                 for c in session.calls]
        self.assertEqual(["read", "write", "read"], kinds)

    async def test_railway_refusing_the_check_is_not_reported_as_success(self):
        """Third outcome, distinct from both: the write went out and then the
        read-back failed, so whether it landed is unknown. Reporting that as
        success is the original bug wearing a different hat."""
        routes = {**self._ROUTE, "serviceInstance(serviceId": [
            {"serviceInstance": {"serviceId": "svc1", "serviceName": "web"}},
            {"errors": [{"message": "Not Authorized"}]},
        ]}
        result, session = await self._set(routes, "")

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertIn("would not confirm", result["error"])
        self.assertIn("Not Authorized", result["error"])
        # The write really was attempted — this is not the pre-write guard.
        self.assertEqual(1, len([c for c in session.calls
                                 if "serviceInstanceUpdate" in c["query"]]))

    async def test_redeploy_is_opt_in(self):
        stored = self._stored("npm run build")
        routes = {**stored,
                  "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}}

        quiet, session = await self._set(stored, "npm run build")
        self.assertFalse(quiet["redeployed"])
        self.assertEqual([], [c for c in session.calls
                              if "serviceInstanceRedeploy" in c["query"]])

        loud, session = await self._set(routes, "npm run build", redeploy=True)
        self.assertTrue(loud["redeployed"])
        self.assertTrue([c for c in session.calls
                         if "serviceInstanceRedeploy" in c["query"]])

    async def test_it_refuses_a_service_with_no_instance(self):
        """Same serviceInstanceUpdate, same silent acceptance for an absent
        instance, so the same guard — including on the clear."""
        absent = {**self._ROUTE, "serviceInstance(serviceId": {"serviceInstance": None}}

        for value in ("npm run build", ""):
            with self.subTest(build_command=value):
                result, session = await self._set(absent, value)

                self.assertNotIn("updated", result)
                self.assertIn("svc1", result["error"])
                self.assertIn("e1", result["error"])
                self.assertEqual([], [c for c in session.calls
                                      if "serviceInstanceUpdate" in c["query"]])

    async def test_a_rejected_mutation_is_not_reported_as_updated(self):
        rejected = {**self._ROUTE,
                    "serviceInstanceUpdate": {"serviceInstanceUpdate": False}}
        result, _ = await self._set(rejected, "npm run build")

        self.assertNotIn("updated", result)
        self.assertIn("svc1", result["error"])


class SplitOutSetterTest(_StubbedServer):
    """The four settings lifted out of set_service_config into tools of their
    own (2026-08-10).

    Dockerfile path, root directory, healthcheck and replica count are among
    the most-changed Railway settings and were reachable only through
    set_service_config — one tool, fifteen optional arguments, a name that
    says nothing about any of them. An agent reading the tool list concluded
    the API could not do it and handed the job back, exactly as it had for the
    build command before set_build_command. These four are that fix, and they
    are deliberately identical to set_build_command: the value is written with
    a single-key serviceInstanceUpdate, `""` clears a string override as an
    explicit null (an omitted key means "untouched" to
    ServiceInstanceUpdateInput), the redeploy is opt-in, and a missing service
    instance is refused rather than reported as written.

    Written as a table because the sameness IS the property: a divergence in
    one of the four is the bug worth catching, and a table makes it impossible
    to cover three of them and quietly forget the fourth.

    One deliberate divergence since 2026-08-13: `set_healthcheck` reads its
    path back after the write, because Railway drops the clear on that field
    (the set_build_command defect, confirmed on both). The other three are
    string setters on the same input and very likely share it — nothing here
    claims otherwise; they simply have no live evidence yet, which is a card
    and not a reason to guess. So the table's routes now answer the
    healthcheck read, and the clear case gets a route of its own.
    """

    # tool -> (arguments it takes, the input payload that must reach Railway)
    _SETTERS = {
        "set_dockerfile_path": ({"dockerfile_path": "docker/Dockerfile.web"},
                                {"dockerfilePath": "docker/Dockerfile.web"}),
        "set_root_directory": ({"root_directory": "apps/api"},
                               {"rootDirectory": "apps/api"}),
        "set_healthcheck": ({"healthcheck_path": "/healthz"},
                            {"healthcheckPath": "/healthz"}),
        "set_num_replicas": ({"num_replicas": 3}, {"numReplicas": 3}),
    }
    # The three string settings, and the argument whose "" clears them.
    _CLEARABLE = {"set_dockerfile_path": ("dockerfile_path", "dockerfilePath"),
                  "set_root_directory": ("root_directory", "rootDirectory"),
                  "set_healthcheck": ("healthcheck_path", "healthcheckPath")}

    # Echoes the healthcheck path set_healthcheck now reads back, so this is a
    # Railway that stored what the table writes. _CLEARED is the same route
    # with the path gone — what a successful clear reads back as.
    _ROUTE = {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
              "serviceInstance(serviceId": {
                  "serviceInstance": {"serviceId": "svc1", "serviceName": "web",
                                      "healthcheckPath": "/healthz"}}}
    _CLEARED = {**_ROUTE, "serviceInstance(serviceId": {
        "serviceInstance": {"serviceId": "svc1", "serviceName": "web"}}}

    @staticmethod
    def _sent(session: _FakeSession) -> dict:
        writes = [c for c in session.calls if "serviceInstanceUpdate" in c["query"]]
        assert len(writes) == 1, f"expected one write, got {len(writes)}"
        return writes[0]["variables"]["input"]

    async def _set(self, tool: str, routes: dict, args: dict,
                   **extra) -> tuple[dict, _FakeSession]:
        session = self.install(routes)
        call = {"environment_id": "e1", "service_id": "svc1", **args, **extra}
        return json.loads(await _text(server.mcp.call_tool(tool, call))), session

    async def test_each_setter_writes_only_its_own_field(self):
        for tool, (args, expected) in self._SETTERS.items():
            with self.subTest(tool=tool):
                _, session = await self._set(tool, self._ROUTE, args)

                self.assertEqual(expected, self._sent(session),
                                 "the write must touch that one field and no other "
                                 "— these tools exist so a caller can change one "
                                 "setting without reading the rest back first")

    async def test_each_setter_echoes_the_value_and_names_the_next_deploy(self):
        for tool, (args, expected) in self._SETTERS.items():
            with self.subTest(tool=tool):
                field, value = next(iter(expected.items()))
                result, _ = await self._set(tool, self._ROUTE, args)

                self.assertEqual(value, result[field])
                self.assertTrue(result["updated"])
                self.assertFalse(result["redeployed"])
                self.assertIn("next deploy", result["note"])

    async def test_an_empty_string_clears_a_string_setting(self):
        """The key must be present and null, exactly as set_build_command and
        set_region send their clear — dropping it leaves Railway's stored
        override in place, which reads back as a change that never happened."""
        for tool, (arg, field) in self._CLEARABLE.items():
            with self.subTest(tool=tool):
                result, session = await self._set(tool, self._CLEARED, {arg: ""})

                sent = self._sent(session)
                self.assertIn(field, sent,
                              "the key was dropped, so the override was left in place")
                self.assertIsNone(sent[field])
                self.assertIsNone(result[field])
                self.assertTrue(result["updated"])

    async def test_a_replica_count_of_zero_is_still_a_write(self):
        """set_num_replicas must not borrow the string setters' `or None`: 0 is
        a number Railway can be given, and `0 or None` would silently turn a
        scale-to-zero into "leave the replica count alone" while still
        reporting `updated: true`. The same falsey trap set_service_config
        already documents."""
        result, session = await self._set("set_num_replicas", self._ROUTE,
                                          {"num_replicas": 0})

        self.assertEqual({"numReplicas": 0}, self._sent(session))
        self.assertEqual(0, result["numReplicas"])
        self.assertTrue(result["updated"])

    async def test_redeploy_is_opt_in(self):
        loud_routes = {**self._ROUTE,
                       "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}}

        for tool, (args, _expected) in self._SETTERS.items():
            with self.subTest(tool=tool):
                quiet, session = await self._set(tool, self._ROUTE, args)
                self.assertFalse(quiet["redeployed"])
                self.assertEqual([], [c for c in session.calls
                                      if "serviceInstanceRedeploy" in c["query"]])

                loud, session = await self._set(tool, loud_routes, args,
                                                redeploy=True)
                self.assertTrue(loud["redeployed"])
                self.assertTrue([c for c in session.calls
                                 if "serviceInstanceRedeploy" in c["query"]])

    async def test_it_refuses_a_service_with_no_instance(self):
        """Same serviceInstanceUpdate, same silent acceptance for an absent
        instance, so the same guard — including on the clear."""
        absent = {**self._ROUTE, "serviceInstance(serviceId": {"serviceInstance": None}}

        for tool, (args, _expected) in self._SETTERS.items():
            variants = [args]
            if tool in self._CLEARABLE:
                variants.append({self._CLEARABLE[tool][0]: ""})
            for variant in variants:
                with self.subTest(tool=tool, args=variant):
                    result, session = await self._set(tool, absent, variant)

                    self.assertNotIn("updated", result)
                    self.assertIn("svc1", result["error"])
                    self.assertIn("e1", result["error"])
                    self.assertEqual([], [c for c in session.calls
                                          if "serviceInstanceUpdate" in c["query"]])

    async def test_a_rejected_mutation_is_not_reported_as_updated(self):
        rejected = {**self._ROUTE,
                    "serviceInstanceUpdate": {"serviceInstanceUpdate": False}}

        for tool, (args, _expected) in self._SETTERS.items():
            with self.subTest(tool=tool):
                result, _ = await self._set(tool, rejected, args)

                self.assertNotIn("updated", result)
                self.assertIn("svc1", result["error"])


class HealthcheckTimeoutTest(_StubbedServer):
    """set_healthcheck writes two fields, and only one of them is required.

    healthcheckTimeout has no "clear" and no natural default to send, so the
    same rule set_service_config uses applies inside a single tool: an omitted
    timeout must not appear in the payload at all, because a key that IS there
    is written — sending a null would wipe a timeout the caller never
    mentioned while it looked like a plain path change.
    """

    # The path is read back after the write, so the route has to answer as a
    # Railway that stored it; _CLEARED is the same one with no path, which is
    # what a clear that landed reads back as.
    _ROUTE = {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
              "serviceInstance(serviceId": {
                  "serviceInstance": {"serviceId": "svc1", "serviceName": "web",
                                      "healthcheckPath": "/healthz"}}}
    _CLEARED = {**_ROUTE, "serviceInstance(serviceId": {
        "serviceInstance": {"serviceId": "svc1", "serviceName": "web"}}}

    async def _set(self, routes: dict | None = None, **args) -> tuple[dict, dict]:
        session = self.install(routes or self._ROUTE)
        call = {"environment_id": "e1", "service_id": "svc1", **args}
        result = json.loads(await _text(server.mcp.call_tool("set_healthcheck", call)))
        sent = [c for c in session.calls
                if "serviceInstanceUpdate" in c["query"]][0]["variables"]["input"]
        return result, sent

    async def test_an_omitted_timeout_is_left_untouched(self):
        result, sent = await self._set(healthcheck_path="/healthz")

        self.assertEqual({"healthcheckPath": "/healthz"}, sent,
                         "an omitted timeout must not be sent — the stored one "
                         "would be overwritten by a call that never mentioned it")
        self.assertNotIn("healthcheckTimeout", result)

    async def test_a_given_timeout_is_sent_and_echoed(self):
        result, sent = await self._set(healthcheck_path="/healthz",
                                       healthcheck_timeout=120)

        self.assertEqual({"healthcheckPath": "/healthz", "healthcheckTimeout": 120},
                         sent)
        self.assertEqual(120, result["healthcheckTimeout"])

    async def test_clearing_the_path_does_not_touch_the_timeout(self):
        """Clearing is about the path only. Nothing polls once the path is
        gone, so the stored timeout is harmless — and inventing a write for it
        would destroy the value a caller wants back when they restore the
        path."""
        result, sent = await self._set(self._CLEARED, healthcheck_path="")

        self.assertEqual({"healthcheckPath": None}, sent)
        self.assertIsNone(result["healthcheckPath"])
        self.assertTrue(result["updated"])


class HealthcheckClearTest(_StubbedServer):
    """The other half of the dropped-clear defect (2026-08-13).

    `set_healthcheck ""` and `set_build_command ""` were confirmed live on
    2026-08-10 to report success and change nothing: Railway takes the explicit
    null on these fields, answers `true`, and keeps the stored value, while a
    value written through the identical mutation seconds later lands. Two
    distinct Railway behaviours, then, not one — `region` is dropped whatever
    is sent (RegionOverrideTest), these two only when cleared — and the same
    answer to both, `_write_unconfirmed`: read the field back and say what
    Railway actually reports.

    BuildCommandTest holds the build-command half; this class is set_healthcheck
    alone, because its clear is the one that also carries a second field the
    check does not cover.
    """

    _STORED = {"serviceInstanceUpdate": {"serviceInstanceUpdate": True},
               "serviceInstance(serviceId": {
                   "serviceInstance": {"serviceId": "svc1", "serviceName": "web",
                                       "healthcheckPath": "/healthz"}}}

    async def _set(self, routes: dict, **args) -> tuple[dict, _FakeSession]:
        session = self.install(routes)
        call = {"environment_id": "e1", "service_id": "svc1", **args}
        result = json.loads(await _text(server.mcp.call_tool("set_healthcheck", call)))
        return result, session

    async def test_a_dropped_clear_is_an_error_not_a_success(self):
        result, _ = await self._set(self._STORED, healthcheck_path="")

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertIsNone(result["sent"])
        self.assertEqual("/healthz", result["observed"])
        self.assertIn("/healthz", result["error"])
        self.assertIn("dashboard", result["error"])
        self.assertIn("svc1", result["error"])
        self.assertIn("e1", result["error"])

    async def test_a_dropped_clear_says_the_timeout_was_not_read_back(self):
        """The refusal's generic sentence ends "Nothing was changed", which is
        true of the path and unknown of a timeout sent in the same write — the
        check reads one field. Saying so is the difference between an honest
        refusal and a new small lie in place of the old one."""
        with_timeout, _ = await self._set(self._STORED, healthcheck_path="",
                                          healthcheck_timeout=120)
        self.assertIn("healthcheck_timeout", with_timeout["error"])

        alone, _ = await self._set(self._STORED, healthcheck_path="")
        self.assertNotIn("healthcheck_timeout", alone["error"],
                         "a caveat about a field the caller never sent is noise")

    async def test_a_dropped_clear_is_verified_before_any_redeploy(self):
        routes = {**self._STORED,
                  "serviceInstanceRedeploy": {"serviceInstanceRedeploy": True}}
        result, session = await self._set(routes, healthcheck_path="",
                                          redeploy=True)

        self.assertFalse(result["verified"])
        self.assertEqual([], [c for c in session.calls
                              if "serviceInstanceRedeploy" in c["query"]])

    async def test_the_check_reads_back_after_the_write_not_before(self):
        """The guard read the tool already makes would satisfy a check placed
        before the write while proving nothing."""
        _, session = await self._set(self._STORED, healthcheck_path="")

        kinds = ["write" if "serviceInstanceUpdate" in c["query"] else "read"
                 for c in session.calls]
        self.assertEqual(["read", "write", "read"], kinds)

    async def test_railway_refusing_the_check_is_not_reported_as_success(self):
        """Write sent, read-back failed: nobody knows whether it landed, and
        that is not success."""
        routes = {**self._STORED, "serviceInstance(serviceId": [
            {"serviceInstance": {"serviceId": "svc1", "serviceName": "web"}},
            {"errors": [{"message": "Not Authorized"}]},
        ]}
        result, session = await self._set(routes, healthcheck_path="/healthz")

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertIn("would not confirm", result["error"])
        self.assertIn("Not Authorized", result["error"])
        self.assertEqual(1, len([c for c in session.calls
                                 if "serviceInstanceUpdate" in c["query"]]))

    async def test_clearing_a_service_that_has_no_healthcheck_is_a_success(self):
        """The check is on the END STATE, not on movement: asked to remove a
        path that is not there, the tool reports the state the caller wanted
        rather than accusing Railway of dropping a write with nothing to do."""
        empty = {**self._STORED, "serviceInstance(serviceId": {
            "serviceInstance": {"serviceId": "svc1", "serviceName": "web"}}}
        result, _ = await self._set(empty, healthcheck_path="")

        self.assertTrue(result["updated"])
        self.assertTrue(result["verified"])
        self.assertIsNone(result["healthcheckPath"])


class SetVariablesMergeTest(_StubbedServer):
    """`set_variables` writes a COLLECTION, so what happens to the keys it was
    not given is the behaviour that matters (2026-08-13).

    The tool passed the caller's dict straight into `variableCollectionUpsert`
    and never sent the input's `replace` field, so whether one key was set or
    every secret on the service was deleted rested on Railway's default — with
    a one-line docstring that mentioned neither outcome. Introspection settles
    the default (`VariableCollectionUpsertInput.replace` is a Boolean with
    `defaultValue` "false", "When set to true, removes all existing variables
    before upserting the new collection"), and the field is now sent explicitly
    in both states so no vendor default decides it. The rest is the same rule
    the other write paths follow: read back and report names, never values.
    """

    _BEFORE = {"KEEP": "x", "OLD": "y"}

    def _routes(self, before: dict, after: dict) -> dict:
        return {"variables(projectId": [{"variables": before},
                                        {"variables": after}],
                "variableCollectionUpsert": {"variableCollectionUpsert": True}}

    async def _set(self, routes: dict, **args) -> tuple[dict, _FakeSession]:
        session = self.install(routes)
        call = {"project_id": "p1", "environment_id": "e1",
                "service_id": "svc1", "variables": {"NEW": "1"}, **args}
        result = json.loads(await _text(server.mcp.call_tool("set_variables", call)))
        return result, session

    @staticmethod
    def _sent(session: _FakeSession) -> dict:
        return next(c["variables"]["input"] for c in session.calls
                    if "variableCollectionUpsert" in c["query"])

    async def test_replace_is_sent_explicitly_and_defaults_to_merge(self):
        """The defect itself: the field absent means Railway's default decides,
        and a default that flips turns a nudge variable into a wipe."""
        _, session = await self._set(
            self._routes(self._BEFORE, {**self._BEFORE, "NEW": "1"}))

        sent = self._sent(session)
        self.assertIn("replace", sent,
                      "the mutation input omits `replace`, so the outcome is "
                      "Railway's default rather than the caller's choice")
        self.assertIs(False, sent["replace"])

    async def test_replace_true_is_passed_through(self):
        _, session = await self._set(self._routes(self._BEFORE, {"NEW": "1"}),
                                     replace=True)

        self.assertIs(True, self._sent(session)["replace"])

    async def test_a_merge_reports_the_keys_and_removes_nothing(self):
        result, _ = await self._set(
            self._routes(self._BEFORE, {**self._BEFORE, "NEW": "1"}))

        self.assertTrue(result["updated"])
        self.assertTrue(result["verified"])
        self.assertIs(False, result["replace"])
        self.assertEqual(["NEW"], result["keysSet"])
        self.assertEqual(["KEEP", "NEW", "OLD"], result["keysNow"])
        self.assertEqual([], result["keysRemoved"])

    async def test_a_replace_names_the_keys_it_deleted(self):
        """The whole point of returning the key set: what a replace took away
        is visible in the answer instead of being found weeks later."""
        result, _ = await self._set(self._routes(self._BEFORE, {"NEW": "1"}),
                                    replace=True)

        self.assertEqual(["NEW"], result["keysNow"])
        self.assertEqual(["KEEP", "OLD"], result["keysRemoved"])

    async def test_no_value_ever_reaches_the_answer(self):
        """Same rule as list_variables and check_variable — the read-back holds
        every value on the service, so the tool must hand back names alone."""
        result, _ = await self._set(
            self._routes({"TOKEN": "s3cret"}, {"TOKEN": "s3cret", "NEW": "1"}))

        self.assertNotIn("s3cret", json.dumps(result))
        self.assertNotIn("1", json.dumps(result["keysNow"]))

    async def test_a_key_that_did_not_land_is_an_error_not_a_success(self):
        result, _ = await self._set(self._routes(self._BEFORE, self._BEFORE))

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertEqual(["NEW"], result["keysMissing"])
        self.assertIn("silently dropped", result["error"])

    async def test_railway_refusing_the_read_back_leaves_the_landing_unknown(self):
        """Write sent, collection unreadable: the honest answer is that nobody
        knows whether it landed, not that it did."""
        routes = {**self._routes(self._BEFORE, {}),
                  "variables(projectId": [{"variables": self._BEFORE},
                                          {"errors": [{"message": "Not Authorized"}]}]}
        result, session = await self._set(routes)

        self.assertNotIn("updated", result)
        self.assertFalse(result["verified"])
        self.assertIn("may or may not have landed", result["error"])
        self.assertIn("Not Authorized", result["error"])
        self.assertEqual(["NEW"], result["keysSet"])
        self.assertEqual(1, len([c for c in session.calls
                                 if "variableCollectionUpsert" in c["query"]]))

    async def test_a_collection_that_cannot_be_read_first_is_not_written(self):
        """A replace deletes whatever it could not see, so the before-read is a
        guard and not only a diff — same doctrine as _instance_missing."""
        routes = {**self._routes(self._BEFORE, {}),
                  "variables(projectId": {"errors": [{"message": "Not Authorized"}]}}
        result, session = await self._set(routes, replace=True)

        self.assertFalse(result["verified"])
        self.assertIn("Nothing was changed", result["error"])
        self.assertEqual([], [c for c in session.calls
                              if "variableCollectionUpsert" in c["query"]])

    async def test_the_collection_is_read_before_and_after_the_write(self):
        _, session = await self._set(
            self._routes(self._BEFORE, {**self._BEFORE, "NEW": "1"}))

        kinds = ["write" if "variableCollectionUpsert" in c["query"] else "read"
                 for c in session.calls]
        self.assertEqual(["read", "write", "read"], kinds)

    def test_the_docstring_states_the_merge_replace_distinction(self):
        """A caller reads the description, not the mutation. The one-line
        docstring is what made the destructive reading possible at all."""
        doc = next(t.description for t in server.mcp._tool_manager.list_tools()
                   if t.name == "set_variables")

        self.assertIn("merged", doc)
        self.assertIn("deleted", doc)
        self.assertIn("replace=True", doc)
        self.assertIn("default", doc)


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
                     "set_build_command": {"build_command": "npm run build"},
                     "set_dockerfile_path": {"dockerfile_path": "docker/Dockerfile"},
                     "set_root_directory": {"root_directory": "apps/api"},
                     "set_healthcheck": {"healthcheck_path": "/healthz"},
                     "set_num_replicas": {"num_replicas": 3},
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

    async def test_set_build_command_refuses_a_service_with_no_instance(self):
        """The newest tool on this mutation, added with the guard rather than
        after it — set_region shipped the defect for a day by being left out."""
        result, session = await self._call("set_build_command", self._ABSENT)

        self.assertNotIn("updated", result)
        self.assertIn("svc-pdf", result["error"])
        self.assertIn("env-prod", result["error"])
        self.assertEqual([], self._writes(session))

    async def test_the_split_out_setters_refuse_a_service_with_no_instance(self):
        """The four settings split out of set_service_config (2026-08-10) write
        through the same mutation, so they inherit the same defect if the guard
        is left out — and they were written with it, not fitted afterwards."""
        for tool in ("set_dockerfile_path", "set_root_directory",
                     "set_healthcheck", "set_num_replicas"):
            with self.subTest(tool=tool):
                result, session = await self._call(tool, self._ABSENT)

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
        for tool in ("set_service_config", "set_start_command", "set_region",
                     "set_build_command", "set_dockerfile_path",
                     "set_root_directory", "set_healthcheck", "set_num_replicas"):
            with self.subTest(tool=tool):
                result, session = await self._call(tool, self._NOT_FOUND)

                self.assertNotIn("updated", result)
                self.assertIn("svc-pdf", result["error"])
                self.assertEqual([], self._writes(session))

    async def test_an_existing_instance_is_still_written(self):
        """The guard must refuse the missing case only — the working path is
        the whole point of the tools."""
        # `region`, `buildCommand` and `healthcheckPath` are here because those
        # three tools now read their field back and refuse when it did not
        # land — so "the working path" for them means a Railway that stores
        # what it is told, not merely one that has an instance. Each value
        # matches what the corresponding _call writes.
        present = {**self._WRITE, "serviceInstance(serviceId": {
            "serviceInstance": {"serviceId": "svc-pdf", "serviceName": "pdf",
                                "region": "europe-west4-drams3a",
                                "buildCommand": "npm run build",
                                "healthcheckPath": "/healthz"}}}

        for tool in ("set_service_config", "set_start_command", "set_region",
                     "set_build_command", "set_dockerfile_path",
                     "set_root_directory", "set_healthcheck", "set_num_replicas"):
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

    async def test_the_probe_only_ever_asks_for_container_measurements(self):
        """The whole check rests on measurements that stop when the process
        does. A volume-scoped one — DISK_USAGE_GB, BACKUP_USAGE_GB, or the
        ephemeral variant — keeps reporting the same non-zero number with no
        container anywhere, so adding one to this list would turn stored bytes
        into "resource-use-seen" and silence the accusation the probe exists to
        make. Nothing else guards that list; it is a hardcoded tuple one edit
        away from reintroducing the five-month-dead-Postgres defect."""
        self.assertEqual(("CPU_USAGE", "MEMORY_USAGE_GB"),
                         server._CONTAINER_PROBE_MEASUREMENTS,
                         "the container probe's measurement list changed — only "
                         "measurements a CONTAINER produces may be in it")

        for volume_scoped in ("DISK_USAGE_GB", "BACKUP_USAGE_GB",
                              "EPHEMERAL_DISK_USAGE_GB"):
            self.assertNotIn(volume_scoped, server._CONTAINER_PROBE_MEASUREMENTS,
                             f"{volume_scoped} describes the volume, which "
                             "outlives every deployment — a dead service would "
                             "read as healthy")


class ExplainedFailureTest(_StubbedServer):
    """Every tool explains a failed Railway call, not just list_projects.

    `_annotate_refusal` already covered the GraphQL-errors branch for all tools
    (RefusalWordingTest pins that). The gap this class exists for is everything
    that never reaches GraphQL — an HTTP 401 or 502, a connect timeout, an HTML
    page from Railway's edge. Those escaped `_query_sync` as requests' own repr,
    and only list_projects turned them into a sentence, because it is the only
    tool that catches its own failures. So the same outage read as
    "Railway refused the token (HTTP 401)" from one tool and as a raw exception
    string from the other 31 — and an agent that had learnt the first wording
    took the second for a different, harder problem.

    The fix is one wrapper at the boundary, so the properties worth locking are
    about the boundary: the explanation reaches tools that have no error
    handling of their own, it reaches every failure mode, it does not get
    applied twice, and it still cannot carry the token.
    """

    @staticmethod
    def _http(status: int) -> requests.exceptions.HTTPError:
        response = requests.Response()
        response.status_code = status
        return requests.exceptions.HTTPError(f"{status} Server Error", response=response)

    async def _failure_text(self, tool: str, args: dict, exc: Exception) -> str:
        """Run `tool` with every Railway call failing as `exc`, return the text
        the caller sees — raised or returned, since tools do both."""
        self.install({"": exc})
        try:
            return await _text(server.mcp.call_tool(tool, args))
        except Exception as raised:  # noqa: BLE001 — the answer under test
            return str(raised)

    # The tools the card names, one per family, each with the arguments it
    # needs. A family is represented rather than exhaustively listed: they all
    # reach Railway through the same _query, so one from each proves the
    # wrapper is not tool-specific — and these are the ones with no error
    # handling of their own, which is precisely why they used to fail bare.
    COVERED = {
        "list_services": {"project_id": "p1"},
        "create_service": {"project_id": "p1", "environment_id": "e1", "name": "s"},
        "create_deployment": {"environment_id": "e1", "service_id": "svc1"},
        "list_variables": {"project_id": "p1", "environment_id": "e1"},
        "set_variables": {"project_id": "p1", "environment_id": "e1",
                          "service_id": "svc1", "variables": {"A": "b"}},
        "list_service_domains": {"project_id": "p1", "environment_id": "e1",
                                 "service_id": "svc1"},
        "create_service_domain": {"project_id": "p1", "environment_id": "e1",
                                  "service_id": "svc1"},
        "list_volumes": {"project_id": "p1"},
        "create_volume": {"project_id": "p1", "environment_id": "e1",
                          "service_id": "svc1", "mount_path": "/data"},
        "delete_volume": {"volume_id": "v1"},
        "get_logs": {"project_id": "p1", "environment_id": "e1", "service_id": "svc1"},
        "get_metrics": {"project_id": "p1", "environment_id": "e1",
                        "service_id": "svc1", "start_date": "2026-08-01T00:00:00Z"},
        "list_environments": {"project_id": "p1"},
        "get_service_instance": {"environment_id": "e1", "service_id": "svc1"},
    }

    async def test_every_covered_tool_explains_a_refused_token(self):
        """The defect in one assertion, across the whole fleet of tools: an
        HTTP 401 used to arrive as requests' "401 Server Error" string from
        every one of these. Now each says which of the three situations it is.
        """
        for tool, args in self.COVERED.items():
            with self.subTest(tool=tool):
                message = await self._failure_text(tool, args, self._http(401))
                self.assertIn("Railway refused the token (HTTP 401)", message,
                              f"{tool} still reports a bare platform error")

    async def test_every_covered_tool_explains_an_unreachable_railway(self):
        """The second failure mode. It must read differently from a refusal —
        the two need opposite responses, and telling them apart was the whole
        point of the wording list_projects already had."""
        for tool, args in self.COVERED.items():
            with self.subTest(tool=tool):
                message = await self._failure_text(
                    tool, args, requests.exceptions.ConnectionError("no route"))
                self.assertIn("Railway was unreachable", message,
                              f"{tool} still reports a bare platform error")
                self.assertNotIn("refused the token", message)

    async def test_a_server_error_names_the_status(self):
        """A 502 is neither a refusal nor an outage of ours to fix, so it must
        not be dressed as either — it says the status and stops."""
        message = await self._failure_text("list_volumes", {"project_id": "p1"},
                                           self._http(502))

        self.assertIn("Railway returned HTTP 502", message)
        self.assertNotIn("refused the token", message)

    async def test_a_non_json_body_is_named_rather_than_shown_as_an_offset(self):
        """Railway's edge answers an overload or a blocked request with HTML.
        json() then raises a character-offset error, the least informative
        failure of the lot, and requests' JSONDecodeError subclasses
        RequestException — so an ordering slip here would file an error page
        under "unreachable" and send the reader to look at the network."""
        message = await self._failure_text(
            "get_metrics", self.COVERED["get_metrics"],
            requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0))

        self.assertIn("not JSON", message)
        self.assertNotIn("unreachable", message,
                         "an error page was reported as a network failure")

    async def test_the_explanation_is_not_applied_twice(self):
        """_why is still called by list_projects on failures that now arrive
        pre-explained. Without the RailwayCallError shortcut it would wrap its
        own sentence — "Railway rejected the query: Railway refused the token
        (HTTP 401)" — quoting us as if it were Railway."""
        self.install({"": self._http(401)})

        result = json.loads(await _text(server.mcp.call_tool("list_projects", {})))

        reason = result["error"] if "error" in result else result[0]["warning"]
        self.assertIn("Railway refused the token (HTTP 401)", reason)
        self.assertNotIn("rejected the query", reason,
                         "the explanation was explained a second time")

    async def test_an_explained_failure_cannot_carry_the_token(self):
        """The wrapper turns Railway's words into ours for 32 tools at once, so
        the redaction has to hold at the boundary rather than in the one tool
        that used to do it."""
        original = server.TOKEN
        server.TOKEN = "super-secret-token"
        self.addCleanup(setattr, server, "TOKEN", original)

        message = await self._failure_text(
            "list_environments", {"project_id": "p1"},
            RuntimeError("refused for Bearer super-secret-token"))

        self.assertNotIn("super-secret-token", message)
        self.assertIn("***", message)

    async def test_a_wrapping_tool_still_folds_the_failure_into_its_own_answer(self):
        """The tools that DO catch RuntimeError must keep catching. The wrapper
        raises a RailwayCallError, and it subclasses RuntimeError precisely so
        those handlers are not edited one at a time — a plain Exception here
        would turn every one of them back into an uncaught error."""
        self.install({"service(id:": self._http(401)})

        result = json.loads(await _text(server.mcp.call_tool(
            "delete_service", {"service_id": "svc1"})))

        self.assertIn("Nothing was deleted", result["error"],
                      "the tool's own refusal was replaced by a raised error")
        self.assertIn("Railway refused the token (HTTP 401)", result["error"])


class MissingDefaultProjectTest(_StubbedServer):
    """The one failure here that is ours, not Railway's — and so the one that
    never passed the explanation step at all.

    Four places need a project and may not have one. They used to answer with a
    bare line naming an environment variable: true, and useless to an agent
    that cannot set a service variable and is holding a project id it could
    simply have passed. Said once now, naming both ways forward.
    """

    def setUp(self):
        original = server.DEFAULT_PROJECT
        server.DEFAULT_PROJECT = ""
        self.addCleanup(setattr, server, "DEFAULT_PROJECT", original)

    CASES = {
        "list_services": {},
        "list_volumes": {},
        "delete_service": {"name": "some-service"},
        "create_service": {"project_id": "", "environment_id": "e1", "name": "s"},
        "create_environment": {"name": "pr-42"},
        "update_deployment_trigger": {"environment_id": "e1",
                                      "service_id": "s1", "branch": "main"},
    }

    async def test_each_one_explains_what_to_do_instead(self):
        for tool, args in self.CASES.items():
            with self.subTest(tool=tool):
                # No routes: reaching Railway at all would already be the bug.
                self.install({})
                session = server._session()

                result = json.loads(await _text(server.mcp.call_tool(tool, args)))

                self.assertIn("MCP_DEFAULT_PROJECT_ID", result["error"])
                self.assertIn("list_projects", result["error"],
                              "the reader is told to pin a variable but not "
                              "where to get a project id")
                self.assertIn("project_id", result["error"])
                self.assertEqual([], session.calls,
                                 "the refusal cost a round trip it did not need")

    async def test_the_write_says_nothing_happened(self):
        """A listing that refuses has changed nothing by definition; a create
        has to say so, because the reader's next question is whether a
        half-made service is now sitting in the project."""
        self.install({})

        result = json.loads(await _text(server.mcp.call_tool(
            "create_service", {"project_id": "", "environment_id": "e1", "name": "s"})))

        self.assertIn("Nothing was created", result["error"])

    async def test_delete_service_still_offers_its_own_way_out(self):
        """The shared sentence must not crowd out the advice that is specific
        to one tool: delete_service needs no project at all when given an id."""
        self.install({})

        result = json.loads(await _text(server.mcp.call_tool(
            "delete_service", {"name": "some-service"})))

        self.assertIn("service_id", result["error"])
        self.assertIn("Nothing was deleted", result["error"])


class RegionMetroGroupingTest(_StubbedServer):
    """list_regions lists names, and most of them are the same place twice.

    Railway returns 13 region names across 5 metros, so two thirds of the list
    are aliases: `us-east4-eqdc4a`, `us-east-1`, `us-east4` and
    `us-east4-eqdc16a` are one datacentre. The old answer was Railway's flat
    array, where the shared metro code arrives in a field called `id` — a name
    that reads like a row key — so two services deliberately put in "different
    regions" could sit in the same rack with nothing in the output to say so.

    The tests hold the answer to grouping on Railway's own `id` rather than on
    anything parsed out of a region name, and to keeping every original row
    intact beside the grouping, since the grouping is the summary and the rows
    are still the thing you pass to set_region. Two of them guard the traps:
    `location` is NOT the grouping key, because `sfo` (California) and `pdx`
    (Oregon) are two metros both labelled "US West" and folding them together
    would claim a service can move between coasts for free; and the metro code
    must never be inferred from the name, because the names are Railway's to
    change and a parser would keep answering confidently after they did.
    """

    # Railway's live answer for the skyttedk account, 2026-08-10 — 13 names,
    # 5 metros. Kept verbatim so the grouping is checked against the shape the
    # card describes rather than an invented one.
    _LIVE = [
        {"id": "sfo", "name": "us-west2", "location": "US West",
         "country": "USA", "region": "California"},
        {"id": "sfo", "name": "us-west2-aws", "location": "US West",
         "country": "USA", "region": "California"},
        {"id": "sfo", "name": "us-west2-cssv9a", "location": "US West",
         "country": "USA", "region": "California"},
        {"id": "iad", "name": "us-east4-eqdc4a", "location": "US East",
         "country": "USA", "region": "Virginia"},
        {"id": "iad", "name": "us-east-1", "location": "US East",
         "country": "USA", "region": "Virginia"},
        {"id": "iad", "name": "us-east4", "location": "US East",
         "country": "USA", "region": "Virginia"},
        {"id": "iad", "name": "us-east4-eqdc16a", "location": "US East",
         "country": "USA", "region": "Virginia"},
        {"id": "sin", "name": "asia-southeast1-eqsg3a",
         "location": "Southeast Asia", "country": "Singapore",
         "region": "Singapore"},
        {"id": "sin", "name": "asia-southeast1", "location": "Southeast Asia",
         "country": "Singapore", "region": "Singapore"},
        {"id": "pdx", "name": "us-west1", "location": "US West",
         "country": "USA", "region": "Oregon"},
        {"id": "ams", "name": "europe-west4-drams3a", "location": "EU West",
         "country": "Netherlands", "region": "Amsterdam"},
        {"id": "ams", "name": "europe-west4", "location": "EU West",
         "country": "Netherlands", "region": "Amsterdam"},
        {"id": "ams", "name": "europe-west4-drams11a", "location": "EU West",
         "country": "Netherlands", "region": "Amsterdam"},
    ]

    async def _list(self, rows: list[dict] | None = None) -> dict:
        self.install({"regions {": {"regions": self._LIVE if rows is None else rows}})
        return json.loads(await _text(server.mcp.call_tool("list_regions", {})))

    async def test_the_live_thirteen_names_collapse_to_five_metros(self):
        """The card's own numbers, end to end: whatever else the answer says,
        a caller must be able to see that there are only five places."""
        result = await self._list()

        self.assertEqual(
            [("sfo", ["us-west2", "us-west2-aws", "us-west2-cssv9a"]),
             ("iad", ["us-east4-eqdc4a", "us-east-1", "us-east4",
                      "us-east4-eqdc16a"]),
             ("sin", ["asia-southeast1-eqsg3a", "asia-southeast1"]),
             ("pdx", ["us-west1"]),
             ("ams", ["europe-west4-drams3a", "europe-west4",
                      "europe-west4-drams11a"])],
            [(m["metro_id"], m["names"]) for m in result["metros"]],
            "metros must appear in Railway's order, each carrying every one of "
            "its interchangeable names")
        self.assertEqual(13, len(result["regions"]))

    async def test_every_name_survives_the_grouping(self):
        """A summary that loses a row is worse than no summary: the names are
        what set_region and create_volume are actually given."""
        result = await self._list()

        grouped = [n for m in result["metros"] for n in m["names"]]
        self.assertEqual([r["name"] for r in self._LIVE], grouped)

    async def test_each_row_is_railways_own_plus_metro_id(self):
        """The grouping is added, not substituted. Every field Railway sent is
        still on the row, so nothing that read the old list loses data — and
        `metro_id` spells out what `id` already held, because `id` is the field
        that gets mistaken for a row key."""
        result = await self._list()

        for original, row in zip(self._LIVE, result["regions"]):
            self.assertEqual({**original, "metro_id": original["id"]}, row)

    async def test_two_metros_sharing_a_location_stay_apart(self):
        """`sfo` and `pdx` are both "US West" and are 900 km apart. Grouping by
        the human label instead of the metro code would merge California and
        Oregon and report four places where there are five."""
        result = await self._list()

        by_id = {m["metro_id"]: m for m in result["metros"]}
        self.assertEqual("California", by_id["sfo"]["region"])
        self.assertEqual("Oregon", by_id["pdx"]["region"])
        self.assertEqual(["us-west1"], by_id["pdx"]["names"])
        self.assertNotIn("us-west1", by_id["sfo"]["names"])

    async def test_the_metro_comes_from_railway_not_from_the_name(self):
        """Region names are Railway's to rename, and a name-shaped guess would
        go on answering confidently after they did. A row that Railway says is
        `ams` belongs to `ams` however much its name looks like us-east4."""
        result = await self._list([
            {"id": "ams", "name": "us-east4-lookalike", "location": "EU West",
             "country": "Netherlands", "region": "Amsterdam"},
        ])

        self.assertEqual(["ams"], [m["metro_id"] for m in result["metros"]])
        self.assertEqual(["us-east4-lookalike"], result["metros"][0]["names"])

    async def test_the_note_counts_both_names_and_places(self):
        """The gap between the two numbers is the whole finding, and an agent
        reading only the first line of the answer should still meet it."""
        result = await self._list()

        self.assertIn("13", result["note"])
        self.assertIn("5", result["note"])

    def test_the_description_warns_before_the_answer_is_read(self):
        """A caller choosing a region from the tool list may never look at a
        response. The one-to-many relationship has to be legible from the
        description alone, so these words are part of the fix, not decoration."""
        description = server.mcp._tool_manager.get_tool("list_regions").description

        self.assertIn("metro", description)
        for word in ("us-east4-eqdc4a", "us-east-1", "iad"):
            self.assertIn(word, description,
                          "the description must show the concrete aliases — "
                          "an abstract warning is one nobody applies")
        self.assertIn("location", description,
                      "and must say not to group by location, since two metros "
                      "share the label 'US West'")


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

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
    empty the reader used to be told to set RAILWAY_PROJECT_ID, whatever had
    actually gone wrong. An outage, an expired token and a genuinely unscoped
    token all looked identical, and the real cause appeared nowhere.

    The round-trip test above cannot see this: it counts requests on the happy
    path, and a swallowed failure changes no count.
    """

    def setUp(self):
        # These tests are about the no-project-id branch, and a developer
        # machine may well have RAILWAY_PROJECT_ID set.
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

        self.assertIn("RAILWAY_PROJECT_ID", result["error"])
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

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
    GraphQL error (the kind `_query_sync` turns into a RuntimeError).
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

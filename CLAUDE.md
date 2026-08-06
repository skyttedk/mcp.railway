# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server for Railway hosting, talking straight to Railway's GraphQL API
(`https://backboard.railway.com/graphql/v2`) — no Railway CLI involved. It covers
projects, services, environments, variables, deployments, logs, metrics, domains
and volumes: 32 tools, all of them in one file, `server.py`.
Repo: `skyttedk/mcp.railway`.

It runs two ways from that same file. `MCP_TRANSPORT` unset (or anything other
than `sse`/`streamable-http`) means a **local stdio** server; `sse` or
`streamable-http` means a **network server** bound to `0.0.0.0:$PORT`, wrapped in
a bearer-token middleware. The Dockerfile sets `MCP_TRANSPORT=streamable-http`
and `PORT=8080`, so a Railway deploy is always the network form.

Everything else is helpers around the tools: `_session()` (one `requests.Session`
per thread, because `anyio` reuses its worker threads and `Session` is not
thread-safe), `_query()`/`_query_sync()` (every blocking call offloaded to a
worker thread so a slow Railway response cannot block the event loop),
`_annotate_refusal()`/`_why()` (Railway's error wording is misleading often
enough that refusals are explained rather than echoed).

## The two services it deploys to

One repository, two Railway services, differing only in which Railway account
their token belongs to:

| Service | Service id | Administers the account | Gateway namespace |
| --- | --- | --- | --- |
| `mcp.railway.skyttedk` | `2f4829be-7054-4345-865a-1ae1c5b31ed3` | `sigurd.skytte@gmail.com` (workspace *Sigurd's Projects*) | `railway_skyttedk_*` |
| `mcp.railway.riskwave` | `73befa9f-4fdd-4349-b8f1-c09cc136b043` | `sigurd@riskwave.ai` (workspace *RiskWave*) | `railway_riskwave_*` |

URLs: `https://mcprailwayskyttedk-production.up.railway.app/mcp` and
`https://mcprailwayriskwave-production.up.railway.app/mcp`. `whoami` on each
namespace prints the account above, which is the quickest way to confirm a
deploy landed on the instance you meant.

Both are hosted side by side in the Railway project **`mcp servers`**
(`261c65a9-5b2d-4c90-a8a9-ce9a17d53964`, environment
`4b6e70ed-63ce-4b41-8097-be88f31af02e`) on the skyttedk account, and both are
federated by the Fortea MCP Gateway. A change here lands on **both** — there is
no way to deploy one without the other, so a tool rename breaks two namespaces
at once. That is the reason the tool-contract test exists.

Service-side env vars: `RAILWAY_API_TOKEN` (the account this instance
administers — a plain UUID token from Railway Account Settings → Tokens; the
`rw_Fe26.2**…` session token in `~/.railway/config.json` is encrypted and does
**not** work against GraphQL) and `MCP_AUTH_TOKEN` (the inbound bearer the
gateway sends). The two services carry different `RAILWAY_API_TOKEN` values and
the same `MCP_AUTH_TOKEN`. Values live only on the services, never in this repo.

## Commands

```powershell
# project-local environment — 3.12, matching the Dockerfile and CI
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe server.py        # stdio server (waits on stdin — that is correct)

# tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**Always run the tests through `.\.venv\Scripts\python.exe`, never a bare
`python`** — see the first trap below.

The suite is stdlib `unittest`, so there is no test framework to install, but it
imports `server.py` and therefore needs `requirements.txt` installed. It needs no
Railway credentials and never contacts the Railway API: it swaps `server._session`
for a fake at the HTTP boundary and refuses any call that forgets to. **83 tests,
0.57 s, verified 2026-08-06** on Python 3.12.10. `tests/README.md` explains what
each class is protecting and why.

GitHub Actions runs the same command on every push and pull request
(`.github/workflows/tests.yml`), and fails the job if a Railway credential is
present in it — the suite is only trustworthy while it has none.

After an *intended* change to a tool's name or arguments the contract test fails
on purpose. Confirm the change is wanted, then regenerate and commit the snapshot
alongside it:

```powershell
.\.venv\Scripts\python.exe tests\test_server.py --refresh   # "wrote 32 tools to …"
```

## Deploy

Commit and push to `master`; Railway builds the Dockerfile and restarts **both**
services. Verify through the gateway (`railway_skyttedk_whoami`,
`railway_riskwave_whoami`), or against the two URLs above — a request without
`Authorization: Bearer <MCP_AUTH_TOKEN>` gets a 401 by design, which is also the
cheapest liveness check.

## Traps

- **A bare `python` is not this project's interpreter.** On the workstation this
  server is developed on, `python` resolves to
  `…\hermes-agent\venv\Scripts\python.exe` — an unrelated project's environment
  that happens to be first on `PATH`. The suite passes there, because it needs
  only stdlib plus `requests`, so nothing looks wrong; the run simply proves less
  than it appears to. That has already happened once. Create `.venv` and address
  the interpreter by path. `.venv/` is gitignored, so it is per-machine setup, not
  something a clone gives you.
- **`mcp` is pinned `<2` on purpose.** The 2.x SDK removed `mcp.server.fastmcp`,
  which `server.py` imports. The requirement used to be an unpinned `>=1.2.0`; the
  first rebuild after 2.0.0 shipped resolved to it and crash-looped the service on
  `ModuleNotFoundError` at import time, with no code change to blame. Railway
  rebuilds from scratch and re-resolves every dependency, so any unrelated push is
  the one that discovers this. Moving to 2.x is a code change, not a version bump
  — do not "modernise" the pin. `requirements.txt` says so at the top; keep that
  comment with the line. (Verified working: `mcp` 1.29.0.)
- **`anyio>=4.5` is named deliberately, not by accident.** It arrives with `mcp`
  anyway, but `server.py` imports `to_thread` at module level and every tool
  depends on it, so it is pinned in its own right rather than left to a transitive
  dependency that could move. `uvicorn` and `starlette` are used only inside the
  `__main__` block and do still come from `mcp`; that is the deliberate line
  between the two.
- **`RAILWAY_PROJECT_ID` is Railway's name, not ours — pin a default with
  `MCP_DEFAULT_PROJECT_ID`.** Railway *injects* `RAILWAY_PROJECT_ID` into every
  container with the id of the project the service is hosted in — `mcp servers`
  on the **skyttedk** account for both services — and rewrites it on each build,
  so a service-level variable of that name is stored but permanently shadowed
  (tried on the live riskwave service, shadowed after both a restart and a full
  rebuild; verified 2026-08-06). The riskwave instance's `RAILWAY_API_TOKEN`
  belongs to the riskwave account and cannot read that project, so every
  `railway_riskwave_*` call that omitted `project_id` fell back to it and failed
  with `Not Authorized`. `DEFAULT_PROJECT` now reads `MCP_DEFAULT_PROJECT_ID`
  first and only falls back to the reserved name, which is what keeps the
  skyttedk service — which pins nothing — working exactly as before. Deploy-side:
  the riskwave service needs `MCP_DEFAULT_PROJECT_ID` set to the `riskwave-app`
  project id (`7b8d2d41-8854-4742-bfff-dbfd946c2202`, chosen by the owner
  2026-08-06); until it is, pass `project_id` explicitly to that namespace.
- **Railway's `Not Authorized` usually is not a permissions problem.** It is what
  the API answers when an id is not recognised *on the account the token belongs
  to* — most often a project or service id from the other account. `_annotate_refusal`
  says this next to Railway's own words rather than replacing them; keep that
  shape, because it can genuinely be either.
- **Railway has no `STOPPED` deployment status.** A service stopped by
  `stop_service` keeps whatever status it had, usually `SUCCESS`, and is
  distinguishable only by `deploymentStopped`. Any code that decides whether a
  service is running must read that flag — trusting `status` reports a stopped
  service as healthy and a failed build's logs as the live ones. Tests pin this in
  `StopStartTest` and `LogProvenanceTest`.
- **A deployment has two log queries, and the failure you care about is in the
  other one.** `buildLogs(deploymentId:)` is the builder's output and
  `deploymentLogs(deploymentId:)` is the container's. They take identical
  arguments, return the same `[Log!]!`, and the dashboard shows them as two
  tabs — so it is easy to write one and believe you covered both. A build that
  fails never starts a container, so `deploymentLogs` is empty and the reason
  exists only in `buildLogs`; until 2026-08-06 `get_logs` asked for the
  container's alone and answered a failed production deploy with `logs: []`,
  which reads as "Railway kept nothing" rather than "wrong query". Nothing else
  hinted at it, because a CRASHED deployment — whose container did run — returns
  a complete stack trace. `get_logs` now adds the build output whenever the
  container printed nothing and the deployment is not merely a quiet
  SUCCESS/SLEEPING one, with `buildLogsNote` saying which list to read;
  `build_logs="always"|"never"` overrides that. The extra query is wrapped:
  a Railway refusal on it must never cost the container logs that did arrive.
  `BuildLogTest` pins both the fetch and the deliberate non-fetch.
- **`list_services` cannot tell you whether a deploy landed.** Its
  `latestDeployment` is Railway's own per-instance pointer and it lags: during a
  real deploy it kept naming the previous deployment across three checks, still
  stale well after the new code was provably serving traffic (card 2026-08-05).
  It is not the wrong field — compared against the `deployments` list on 24
  service instances across both accounts it agreed every time, including on a
  CRASHED deployment — it is simply late, and it does catch up. Nothing can be
  read off the id or the status to tell a lagging value from a push that never
  landed, which is why the listing now also returns the deployment's `createdAt`
  and the description says outright that this is not a deploy check. Confirm a
  deploy with the id `create_deployment` returns, or with `get_logs`, which
  queries `deployments(DeploymentListInput)` directly. `DeploymentFreshnessTest`
  pins both halves.
- **A service's source and its build settings are two different mutations, and
  neither is `serviceCreate` alone.** `create_service`/`connect_service` set the
  SOURCE (`ServiceSourceInput`: a GitHub `repo` or a Docker `image` — one or the
  other, never both); everything else the dashboard's Settings tab offers is
  `serviceInstanceUpdate`, which `set_service_config` wraps. Until 2026-08-06
  only the source's repo half existed, so an agent asked to deploy anything
  needing a Dockerfile path or an image concluded — reasonably, from the tool
  list — that the API could not do it and handed the job back as manual
  dashboard work. Two specifics worth keeping written down: **`dockerfilePath`
  is a first-class field** on `ServiceInstanceUpdateInput` (it is the same
  setting as the `RAILWAY_DOCKERFILE_PATH` service variable, so it can also be
  set with `set_variables` — prefer the field, so it does not read as
  application config), and **the `Builder` enum is `RAILPACK | NIXPACKS |
  PAKETO | HEROKU` with no `DOCKERFILE` member** — a Dockerfile is selected by
  its presence or by `dockerfilePath`, never by choosing a builder.
  `ServiceConfigTest` refuses the `DOCKERFILE` guess with a message naming
  `dockerfile_path`, because that is the wrong turn an agent actually takes.
- **In `set_service_config`, an omitted setting and a cleared one must stay
  different things.** It sends a partial `ServiceInstanceUpdateInput`, and every
  key present in that payload is written — so a truthiness filter over the
  arguments would both drop legitimate falsey values (`num_replicas=0`,
  `sleep_application=False`, an empty `watch_patterns` list, which Railway reads
  as "no filter") and, in the other direction, any `None` that leaked into the
  payload would wipe a setting the caller never mentioned. Hence the split:
  `None` means untouched, `""` clears a string, `[]` clears a list and is sent
  as `[]` rather than collapsed to null. `ServiceConfigTest` pins all four.
- **`deploy` and `create_deployment` are not the same operation, and the
  confusion is silent.** `deploy` restarts the container already running and
  builds nothing, so an agent reaching for it sees a success and reports that new
  code is live. `create_deployment` is the real one (`serviceInstanceDeployV2`).
  `CreateDeploymentTest` holds both to their descriptions precisely so a reader of
  the tool list alone can tell them apart — do not blur the wording.

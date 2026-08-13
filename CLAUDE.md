# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server for Railway hosting, talking straight to Railway's GraphQL API
(`https://backboard.railway.com/graphql/v2`) — no Railway CLI involved. It covers
projects, services, environments, variables, deployments, logs, metrics, domains
and volumes: 40 tools, all of them in one file, `server.py`.
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
`_annotate_refusal()`/`_rejected()`/`_why()` (Railway's error wording is
misleading often enough that refusals are explained rather than echoed).

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
for a fake at the HTTP boundary and refuses any call that forgets to. **186 tests,
1.4 s, verified 2026-08-13** on Python 3.12.10. `tests/README.md` explains what
each class is protecting and why.

GitHub Actions runs the same command on every push and pull request
(`.github/workflows/tests.yml`), and fails the job if a Railway credential is
present in it — the suite is only trustworthy while it has none.

After an *intended* change to a tool's name or arguments the contract test fails
on purpose. Confirm the change is wanted, then regenerate and commit the snapshot
alongside it:

```powershell
.\.venv\Scripts\python.exe tests\test_server.py --refresh   # "wrote 40 tools to …"
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
  the riskwave service now has `MCP_DEFAULT_PROJECT_ID` set to the `riskwave-app`
  project id (`7b8d2d41-8854-4742-bfff-dbfd946c2202`, chosen by the owner
  2026-08-06), so a `railway_riskwave_*` call may omit `project_id` and lands in
  `riskwave-app` (re-verified 2026-08-10: `list_services` with no `project_id`
  returns that project's services). Only pass `project_id` to reach one of the
  account's *other* projects. The skyttedk service still pins nothing and keeps
  using the injected value.
- **A tool's failure is explained at the boundary, not in the tool.** Everything
  raised out of `_query_sync` is a `RailwayCallError` whose `str()` is already
  the finished sentence `_why` produces — so all 40 tools report a refused
  token, an unreachable Railway, a 5xx, a non-JSON edge page and a GraphQL
  refusal in the same words, and the next improvement to `_why` reaches all of
  them at once. Until 2026-08-07 only the GraphQL branch was dressed up and only
  `list_projects` translated the rest, because it is the one tool that catches
  its own failures; the same outage therefore read as
  `Railway refused the token (HTTP 401)` from `list_projects` and as requests'
  raw repr from the other 31, and an agent that had learnt the first wording
  took the second for a harder problem. Two things to keep: `RailwayCallError`
  subclasses `RuntimeError`, because several tools already catch that to fold a
  refusal into their own answer and must keep catching without being edited one
  by one; and `_why` returns a `RailwayCallError` unchanged, or it would quote
  our own sentence back as if Railway had said it. The `ValueError` branch sits
  **before** the `RequestException` one deliberately — requests'
  `JSONDecodeError` subclasses both, and the wrong order files an HTML error
  page under "unreachable". `ExplainedFailureTest` pins one tool per family.
- **The missing-default-project refusal is ours, so nothing explains it for
  us.** `list_services`, `list_volumes`, `delete_service` (name path) and
  `create_service` refuse before any round trip when there is no project id and
  none pinned — a failure that never passes `_query_sync` and so never met the
  explanation step. All four now go through `_no_project()`, which names both
  ways forward (pass `project_id`, whose ids `list_projects` returns, or pin
  `MCP_DEFAULT_PROJECT_ID`) and takes an `extra` for the tool-specific way out.
  The old wording named only the environment variable, which an agent cannot
  set and did not need. `MissingDefaultProjectTest` pins it, including that the
  refusal costs no request.
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
- **No deployment field can tell you a container exists.** `deploymentStopped`
  covers the deliberate case only. A container that simply went away leaves
  `latestDeployment` on `SUCCESS`, `deploymentStopped` false and every read tool
  agreeing the service is fine — a production Postgres sat dead for five months
  that way, with three days of downstream outage blamed on network timing (card
  2026-08-06). The only cheap evidence Railway offers is resource usage: a live
  container cannot use zero memory, so a window of CPU/memory samples that are
  all zero proves absence. `_container_probe` reads the last 30 minutes and is
  deliberately three-valued — `resource-use-seen`, `no-resource-use`, and
  `not-checked` for no samples at all, a refused query, a deployment under ten
  minutes old or a `SLEEPING` one. **Only all-zero-samples accuses**: an empty
  metrics answer is also what an unavailable metrics backend looks like, and a
  check that cries wolf is ignored exactly like one that never fires. `get_logs`
  runs it only when the running deployment printed nothing (logs are proof of a
  container, so probing then buys nothing) and reports `containerCheck`,
  flipping `deploymentIsRunning` to false on the evidence. Same incident, second
  half: `deploymentRestart` answers `true` for a deployment whose container is
  gone and starts nothing, so `deploy()` falls back to `serviceInstanceRedeploy`
  — what `start_service` uses, and what actually brought the service back — and
  names the mutation it used in `method`. `ContainerLivenessTest` pins the
  accusation, every non-accusation, and both restart paths.
- **A metrics series is not always a deployment's.** `metrics(groupBy:
  [DEPLOYMENT_ID])` reads as one series per deployment, and for CPU/memory it is
  — but volume-scoped measurements (`DISK_USAGE_GB`, `BACKUP_USAGE_GB`) belong to
  no deployment and arrive in the same list tagged `deploymentId: null`. Observed
  2026-08-10 on `mcp.google`: a memory series with the running deployment's id
  and a disk series with a null one, in a single answer. Nothing is broken — the
  volume outlives every deployment, so there is no id to give it. The rule for
  callers is therefore **aggregate, do not attribute**: filtering to one
  deploymentId drops the untagged series, and reading the untagged series as a
  deployment's usage credits stored bytes to a process. `_container_probe` is the
  working example (unions every series, never reads `tags`), and its measurement
  list is load-bearing for the same reason — adding a disk measurement would let
  a volume's bytes read as a live container. Documented above `get_metrics` and in
  its docstring; re-check if a later Railway API version starts attributing
  volume metrics.
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
- **Railway's `deploymentStopped` is meaningless until a deployment has a
  container, and it does not answer false there — it answers true.** Seen
  2026-08-06 on the riskwave-site project: `list_services` reported
  `deploymentStopped: true` for a deployment that was actively BUILDING and
  went to SUCCESS seconds later. The flag describes a deployment that was
  deliberately stopped, which a build on its way up cannot have been, so on the
  statuses that have no container yet it is noise — and noise that reads as
  "this deploy is dead" exactly while it is alive, which is the failure the
  lagging-`latestDeployment` note above already warns about, arriving from the
  other direction. `list_services` now clears it for
  `_IN_FLIGHT_STATUSES` (`BUILDING, DEPLOYING, INITIALIZING, QUEUED, WAITING,
  NEEDS_APPROVAL`), keeping Railway's own value as `railwayDeploymentStopped`
  next to a `deploymentStoppedNote`. The correction stops there on purpose: for
  SUCCESS/SLEEPING/CRASHED/FAILED the flag is passed through, because it is the
  only evidence a stopped service leaves (there is no STOPPED status) and a
  blanket false would bring back the invisible-stopped-service defect. The three
  other status tuples (`_LIVE_STATUSES`, `_RESTARTABLE_STATUSES`,
  `_STOPPABLE_STATUSES`) all exclude in-flight statuses already, so
  `_running_deployment`, `deploy` and `stop_service` never saw the bogus flag —
  only the raw listing did. `InFlightDeploymentTest` pins both halves.
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
  **Five settings are deliberately written by two tools each** —
  `build_command`, `dockerfile_path`, `root_directory`, the healthcheck pair
  (`healthcheck_path` + `healthcheck_timeout`) and `num_replicas`. Each has a
  standalone setter (`set_build_command` 2026-08-09, then
  `set_dockerfile_path`, `set_root_directory`, `set_healthcheck`,
  `set_num_replicas` 2026-08-10) *and* an argument on `set_service_config`,
  which changes it together with the other settings in a single write. They
  were split out because a setting reachable only through a fifteen-argument
  tool named after nothing in particular is undiscoverable: agents read the
  tool list, concluded the API could not change a Dockerfile path or a replica
  count, and handed the job back as dashboard work. The overlapping pairs send
  the identical `ServiceInstanceUpdateInput` field — **do not "de-duplicate"
  any of them out of `set_service_config`**, which would change that tool's
  published arguments for both namespaces. What is left in
  `set_service_config` alone is the rare or meaningless-alone half: `builder`,
  `watch_patterns`, `railway_config_file`, `pre_deploy_command`,
  `restart_policy_*`, `cron_schedule`, `sleep_application`.
  One asymmetry to keep: the string setters clear on `""` (sent as an explicit
  null), while `set_num_replicas` sends its int straight through — `0 or None`
  would turn a scale-to-zero into "leave it alone" and still report success —
  and `set_healthcheck` sends `healthcheck_timeout` only when it is passed, so
  a plain path change never wipes the stored timeout.
- **In `set_service_config`, an omitted setting and a cleared one must stay
  different things.** It sends a partial `ServiceInstanceUpdateInput`, and every
  key present in that payload is written — so a truthiness filter over the
  arguments would both drop legitimate falsey values (`num_replicas=0`,
  `sleep_application=False`, an empty `watch_patterns` list, which Railway reads
  as "no filter") and, in the other direction, any `None` that leaked into the
  payload would wipe a setting the caller never mentioned. Hence the split:
  `None` means untouched, `""` clears a string, `[]` clears a list and is sent
  as `[]` rather than collapsed to null. `ServiceConfigTest` pins all four.
- **`serviceInstanceUpdate` returning `true` does not mean it stored anything,
  and `region` is the field where that bites.** Railway accepts the flat
  `region` string on that input, answers success, and drops it. Verified live
  2026-08-10 against the decommissioned `mcp.reddit`: `europe-west4-drams3a`
  and `us-west2` (one of the four names Railway's own staff call valid) each
  came back `updated: true` and each read back as no override, unchanged four
  minutes later. It is not the mutation being broken — `numReplicas`,
  `healthcheckPath` and `buildCommand` written through the SAME mutation on the
  SAME instance read back correctly — and it is not a stale read: `region` is
  null on **all 45 service instances across both accounts**, so nothing in the
  fleet has ever demonstrated the field carrying a value. `set_region`
  therefore reads the value back through `_write_unconfirmed` and returns an
  error naming what it sent versus what Railway reports, rather than the
  success it invented for as long as it existed. The tool cannot move a
  service; the dashboard can. `RegionOverrideTest` pins the dropped write, the
  three-call read-write-read order, that a failed verification skips the
  redeploy, and the third outcome where the read-back itself fails (write sent,
  landing unknown — reporting that as success is the original bug in a hat).
  **Where region actually lives now is `multiRegionConfig`**, a JSON map of
  region name to replica count on the same input; Railway's own rejection text
  points there ("clear it by setting its key to null in multiRegionConfig") and
  their forum says flat `numReplicas` is deprecated in its favour. Two things
  must be solved before this server writes it, and neither is a code detail:
  it is **write-only** — introspecting all 609 types in the schema, nothing
  exposes it for reading, so `ServiceInstance.region` (the field being dropped)
  is the only readable region signal there is — and **a wrong key in it bricks
  the service**, a metro id instead of a region name making every later
  deployment fail with "configured with an invalid region", which a Railway
  employee had to clear by hand for the user who hit it. An unverifiable write
  whose failure mode needs vendor support to undo is worse than an honest
  refusal. Note the consequence for clearing: a `set_region ""` reports success
  because the readable field does say "no override", but that cannot promise a
  region stuck in multiRegionConfig is gone. Railway's GraphQL answers
  **introspection without a token**, which is how all of this was established
  from a checkout that has no credentials — reach for it before guessing at a
  field.
- **Region names outnumber places roughly three to one, and Railway's field
  naming hides it.** 13 names across 5 metros; the metro code arrives as `id`,
  which reads like a row key but repeats — `us-east4-eqdc4a`, `us-east-1`,
  `us-east4` and `us-east4-eqdc16a` are all `iad`, one datacentre. `list_regions`
  therefore no longer returns Railway's flat array: it answers
  `{metros, regions, note}`, where `metros` is one entry per place with its
  interchangeable `names` and `regions` is every original row plus an explicit
  `metro_id`. **This is a response-shape change** — a caller that indexed the
  top-level array now wants `["regions"]`. Calls reach the new code as soon as
  the service redeploys, but the gateway caches tool *descriptions*, so restart
  it too or agents keep reading the old one. Group on `metro_id`,
  never on `location`: `sfo` (California) and `pdx` (Oregon) both say "US West".
  `RegionMetroGroupingTest` pins the fold, the traps, and the docstring wording.
- **An explicit null is dropped where a value is kept — clearing is its own
  defect, separate from the region one above.** Live on `mcp.reddit`
  2026-08-10: `set_build_command ""` and `set_healthcheck ""` both reported
  success and changed nothing, while `set_num_replicas 1` through the identical
  mutation seconds later landed. An earlier session saw the same on two
  independent code paths and wondered whether its own argument serialization
  was at fault — it was not, this reproduces cleanly. So there are two distinct
  Railway behaviours, not one: nulls dropped on clearable string fields, and
  `region` dropped even when non-null. `_write_unconfirmed` was written
  generically for exactly this reason and **is now wired into both**
  (2026-08-13): `set_build_command` and `set_healthcheck` read their field back
  after every write — not only the clear, since a value echoed back unread is a
  guess whichever value it is — and a clear Railway swallowed comes back as an
  error naming what was sent and what is still stored, with the redeploy
  skipped. `BuildCommandTest` and `HealthcheckClearTest` pin the four outcomes
  each: a value that lands, a clear that lands (a service with nothing to
  remove — the check is on the end state, so that is honestly a success), the
  dropped clear, and a read-back that itself fails. Two things this does NOT
  cover, both deliberate. **`set_healthcheck`'s timeout is not read back** — it
  has no clear and no evidence of being dropped, so `healthcheckTimeout` in the
  answer is still only what was sent; when a timeout rides along with a clear
  that was swallowed, the refusal says so rather than letting "Nothing was
  changed" stand for a field nobody checked. And **`set_start_command`,
  `set_dockerfile_path` and `set_root_directory` are still unverified**: they
  are string setters on the same input and very probably share the defect, but
  no one has reproduced it on them, and the fleet's habit is to write down what
  was seen rather than what was assumed. Their `""` clear still reports a
  success it has not checked — that is the next card, not a gap to paper over
  by guessing here.
- **`serviceInstanceUpdate` does not object to a service instance that is not
  there, so every setter has to look first.** Config belongs to the service
  *instance* — one per environment — not to the service, so a service can exist
  in the project and have no instance in the environment being configured.
  Railway raises nothing for that: `set_service_config` answered `updated: true`
  with a full `applied` block, and `set_start_command` and `set_region` the
  same, while the settings were written nowhere; the next deploy then refused
  with "Service Instance not found". Every setter on this mutation now runs
  `_instance_missing` first — the same
  `serviceInstance` query `get_service_instance` uses — and refuse, naming both
  ids, when it comes back null OR when Railway will not confirm it (its own
  answer for this state is sometimes the GraphQL error "ServiceInstance not
  found", sometimes a bare "Not Authorized" that hides whether the thing exists
  at all). The mutation's Boolean result, previously discarded, is now checked
  for an explicit `false` only, so a null or absent value behaves as before.
  `MissingServiceInstanceTest` pins it for all eight setters on this mutation.
  Any future tool reaching for `serviceInstanceUpdate` needs the same two lines
  — `set_region` was left out of the first pass and shipped the defect for a
  day; `set_build_command` (2026-08-09) and the four split out on 2026-08-10
  (`set_dockerfile_path`, `set_root_directory`, `set_healthcheck`,
  `set_num_replicas`) were written with them from the start.
- **An environment is deleted by a script, not by a person — so the guard is in
  the tool.** `create_environment` / `delete_environment` /
  `update_deployment_trigger` (2026-08-13) exist for ephemeral previews: one
  environment per card, cloned from `test`, destroyed on close. That makes
  `environmentDelete` the second irreversible operation here after
  `serviceDelete`, and the wider one — it takes every service INSTANCE in the
  environment, their variables, deployments and volume data, while the services
  themselves survive in the other environments. Railway's mutation is a bare
  `environmentDelete(id:)` that asks nothing. So `delete_environment` reads the
  environment back first (an id Railway will not confirm deletes nothing, same
  guard as `delete_service`), then judges the name: the `pr-`/`preview-`/
  `ephemeral-` prefixes or Railway's own `isEphemeral` go straight through,
  anything else needs `confirm_permanent_delete=True`, and `production`, `test`
  and whichever environment the project names as its `baseEnvironmentId` /
  `primaryEnvironmentId` are refused **whatever** is passed — a flag that can
  unlock those is a flag a retry loop will eventually pass. A project lookup
  that fails refuses too, rather than falling through to the delete.
  `EnvironmentLifecycleTest` pins every refusal and that each one costs no
  mutation. Schema facts, confirmed by introspection (Railway answers
  introspection without a token) rather than from memory:
  `EnvironmentCreateInput` carries `projectId`, `name`, `sourceEnvironmentId`
  (this is the whole of "duplicate") and `ephemeral`; there is no
  `EnvironmentDeleteInput`; and a deployment trigger is reached through the
  top-level `deploymentTriggers(projectId:, environmentId:, serviceId:)` query —
  `Project` has no `deploymentTriggers` field, so going via the project is a
  dead end. **`environmentCreate` returns before the clone is finished**: the
  environment exists, its copied services and their deployments arrive in the
  background, which is why the tool's answer says to poll `list_services`
  instead of trusting the success. And `update_deployment_trigger` writes
  configuration only — it starts no build, which its `note` says outright,
  because a config write that reads as a deploy is how an agent reports new
  code live that never built.
- **`set_variables` writes a whole COLLECTION, and until 2026-08-13 nothing
  said so.** `variableCollectionUpsert` takes the dict as the collection, and
  its input's `replace` field decides whether that is merged into the stored
  one or becomes it. The tool never sent the field, so the outcome rested on
  Railway's default — the difference between setting one variable and deleting
  every secret on the service, invisible from a docstring that read "Set
  variables on a Railway service." The caller this bites is the ordinary one: a
  PM routine that forces a rebuild by writing a dummy `DEPLOY_NUDGE`.
  Introspection settles the default (Railway answers introspection without a
  token): on `VariableCollectionUpsertInput`, `replace` is a nullable `Boolean`
  with `defaultValue` `"false"`, described as "When set to true, removes all
  existing variables before upserting the new collection" — so the old silence
  was a merge and nothing was ever wiped by it. It is sent explicitly in both
  states regardless, because a server-side default is the vendor's to change
  and this one turns a write into a wipe. `replace=True` deletes every key not
  listed, with no undo. **The answer is a new shape** — `{updated, replace,
  keysSet, keysNow, keysRemoved, verified}` instead of Railway's bare
  `{"variableCollectionUpsert": true}` — names only, never values, the same
  rule `list_variables`/`check_variable` hold on the read side. The collection
  is read before and after: the after-read turns "Railway said true" into
  evidence and names what a replace removed, and the before-read is a guard as
  much as a diff, so a collection Railway will not show is not written over
  (same doctrine as `_instance_missing`). `SetVariablesMergeTest` pins all of
  it, including the docstring. Unused neighbour on the same input, should it
  ever be wanted: `skipDeploys` (Boolean, default `false`) writes variables
  without triggering the redeploy they normally cause.
- **`deploy` and `create_deployment` are not the same operation, and the
  confusion is silent.** `deploy` restarts the container already running and
  builds nothing, so an agent reaching for it sees a success and reports that new
  code is live. `create_deployment` is the real one (`serviceInstanceDeployV2`).
  `CreateDeploymentTest` holds both to their descriptions precisely so a reader of
  the tool list alone can tell them apart — do not blur the wording.

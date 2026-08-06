# Tests

```
python -m unittest discover -s tests -v
```

Stdlib `unittest` — no test dependency to install, nothing to configure, and no
Railway credentials needed. The suite never contacts the Railway API: it swaps
`server._session` for a fake at the HTTP boundary, and refuses any call that
forgets to. It runs in well under a second.

GitHub Actions runs exactly that command on every push and every pull request
(`.github/workflows/tests.yml`), so the suite protects a change whether or not
anyone remembers to run it. The workflow also fails if a Railway credential is
present in the job, because the suite is only trustworthy while it has none.

It is deliberately small, and meant to stay that way. It does not chase
coverage; it locks the two properties that would have caught the last two real
defects:

- **The tool surface has not moved.** `tool_contract.json` is a snapshot of
  every tool's name, arguments and required flags. Two live Railway services and
  the MCP gateway call these by name, so a silent schema change breaks callers
  with nothing else to notice.
- **A slow Railway response does not block the server.** Checked twice: every
  tool must be `async def` (the SDK runs a plain `def` tool straight on the event
  loop), and, with a stub that takes 300 ms, the event loop must still be free to
  run other work.

Plus one regression test per defect that prompted the suite — the `await`
precedence bug in `create_project`'s workspace lookup, and `list_projects`
falling back to its multi-round-trip path.

And one class for stopping a service (`StopStartTest`), because the failure
there is silent rather than loud: Railway has no STOPPED deployment status, so
a stopped service keeps `status: SUCCESS` and is distinguishable only by
`deploymentStopped`. The tests hold `stop_service` to reading that flag, to
resolving a deployment id rather than passing the service id to
`deploymentStop`, and to explaining a refusal instead of echoing the platform's
misleading "not found".

And one class for deleting a service (`DeleteServiceTest`), because it is the
only operation here that cannot be undone by calling something else, and it
runs with permissions pre-granted — no human sees the call before it happens.
The tests are therefore about refusal rather than about deletion: a name that
matches no service or more than one, a name that reads as a pattern or a list,
an id that disagrees with the name beside it, and an id Railway would not
confirm must each leave the account untouched and say which service was meant.
The happy path is held to deleting exactly one service and naming it, so a
transcript records what was destroyed.

And one class for deploying for real (`CreateDeploymentTest`), because the two
neighbouring tools are easy to confuse and the confusion is silent: `deploy`
restarts the container already running and builds nothing, so an agent that
reaches for it sees a success and reports that new code is live. The tests hold
`create_deployment` to the other Railway mutation entirely
(`serviceInstanceDeployV2`, which takes the SERVICE and returns the id of the
deployment it created — never `deploymentRestart`), to passing a `commit_sha`
through when one is named, and to refusing a missing id or one that reads as a
list or a pattern without building anything. Two more guard the pair rather
than either tool: `deploy`'s answer must stay byte-for-byte what it was, and
each description must keep the words that let a reader of the tool list alone
pick the right one.

And one class for the size of a `get_metrics` answer (`MetricsSizeTest`),
because that failure is invisible from inside this repo: it costs the calling
agent's context window rather than anything Railway or the server measures, and
only on a long range. The tests hold `get_metrics` to bounding a day's samples,
to saying in the response that it summarised rather than truncated, to leaving
a short range exactly as measured — and, most importantly, to reading high, low
and average off every raw sample. A spike averaged into a five-minute point is
still the peak; a summary computed from the surviving points would report the
service as calm through the one minute it was not.

And one class for where a log answer came from (`LogProvenanceTest`), because
the newest deployment is not always the one serving traffic and the gap opens
at exactly the wrong moment: a build fails, someone reads the logs to find out
why the service is misbehaving, and gets the failed attempt's output while the
previous version is still handling every request. The status was always in the
response, so the tests are not about adding data but about making the mismatch
impossible to miss — a warning that leads the answer and names both
deployments, and, just as firmly, no warning at all when the logs are the
running deployment's, since one that fires every time is one nobody reads. They
also hold `get_logs` to reading `deploymentStopped` rather than trusting
`SUCCESS`, to fetching by deployment id rather than service id, to refusing
`source="running"` when nothing is running instead of quietly relabelling the
failed build's logs — and to leaving the default answer the newest deployment's,
so existing callers are untouched.

And one class for a failed build's own output (`BuildLogTest`), because a
deployment keeps its output in two places and the tool only ever read one.
`buildLogs` is the builder's, `deploymentLogs` is the container's, and a build
that fails never starts a container — so the query that was asked came back
empty and the reason lived in the query that was not, while the docstring
promised the failed build's output outright. The tool looked healthy from every
other angle, since a CRASHED deployment did run and still returns a full stack
trace; the hole opened only on the failure people most need explained, and cost
a real investigation. So the tests hold both directions: the build output is
fetched and its arrival explained when the container printed nothing, and it is
NOT fetched when the container logs already answer — a CRASHED deployment, or a
healthy service that has simply been quiet, must not pay for a second query or
have the stack trace buried under an image build. The rest guard the edges that
would turn a fix into a new defect: build output read from the same deployment
the answer names, a deployment with genuinely nothing said in those words rather
than pointed at an empty list, a typo'd argument refused by name, and a build
query Railway rejects costing only itself instead of throwing away container
logs that worked.

And one class for how fresh the service listing's deployment is
(`DeploymentFreshnessTest`), because that answer is confidently wrong rather
than missing. `latestDeployment` is Railway's per-instance pointer and it lags:
during a real deploy it kept naming the previous deployment across three
checks, still stale well after the new code was answering live traffic. It is
the right field — checked against the deployments list on 24 service instances
across both accounts it agreed every time, including on a CRASHED deployment —
so there is nothing to correct, only lateness to make visible. The tests hold
the listing to asking Railway for the deployment's `createdAt` and passing it
through, so a value from before the push you just made is recognisable instead
of being an unchanged id that reads like a push that never landed; and they
hold the description to saying it cannot confirm a deploy and naming
`get_logs` and `create_deployment`, which can.

And two classes for how a service gets its source and its build settings
(`ServiceSourceTest`, `ServiceConfigTest`), because both were places where the
server could do less than Railway can and an agent's only visible option was to
send the user to the dashboard. The source tests hold `create_service` and
`connect_service` to sending a `repo` or an `image` but never both, to leaving
the default branch out of an image connect, and to leaving the old
three-argument `create_service` call byte-identical, since both namespaces'
callers already use it. The config tests protect the one distinction that tool
cannot afford to lose: it sends a partial `ServiceInstanceUpdateInput`, so every
key in the payload is written, which makes "the caller omitted this" and "the
caller cleared this" two different things. A setting nobody mentioned must not
appear at all, `""` and `[]` must clear rather than be dropped, `[]` must stay a
list because Railway reads it as "no watch filter", and `num_replicas=0` /
`sleep_application=False` must survive a filter that a naive truthiness check
would eat. One more is pinned there for a reason that is not obvious from the
schema: `builder="DOCKERFILE"` is refused locally, with a message naming
`dockerfile_path`, because there is no `DOCKERFILE` member of Railway's
`Builder` enum and the API's own answer is a GraphQL parse error naming neither
the tool nor the argument.

And one class for where the default project comes from
(`DefaultProjectSourceTest`), because the obvious name for it is one Railway
reserves. The platform injects `RAILWAY_PROJECT_ID` into every container with
the id of the project the service is HOSTED in and rewrites it on each build,
so it can never hold an operator's choice — it was set on the live riskwave
service to the wanted project and shadowed again after both a restart and a
full rebuild. Every riskwave call that omitted `project_id` therefore fell back
to a project on the other account and answered "Not Authorized". The default is
now read from `MCP_DEFAULT_PROJECT_ID` first, and the tests hold both halves of
that: ours wins where it is set, and the reserved one still supplies the default
where it is not, since the skyttedk service pins nothing and honouring only the
new name would silently take its default away. An empty value counts as absent,
because Railway hands an unset variable through as `""`.

## After an intended change to a tool's arguments

The contract test fails on purpose. Confirm the change is wanted, then:

```
python tests/test_server.py --refresh
```

Commit the regenerated `tool_contract.json` alongside the change, so the diff
shows exactly what callers now see.

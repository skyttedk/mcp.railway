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

## After an intended change to a tool's arguments

The contract test fails on purpose. Confirm the change is wanted, then:

```
python tests/test_server.py --refresh
```

Commit the regenerated `tool_contract.json` alongside the change, so the diff
shows exactly what callers now see.

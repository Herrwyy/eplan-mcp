# Release notes — stdio-stability-v1

Branch `maintenance/stdio-stability-v1`, based on `6be0ebf48`
("Merge pull request #71 …bound StopEplan and Ping…"). Generic stability
maintenance of the public server only: no tool, return-shape, configuration,
or extension-mechanism changes, and nothing in here references any private
engineering data — tests use synthetic samples and neutral temporary paths
throughout.

## Scope statement

First-release compatibility target: **Windows x64, Python 3.12, EPLAN 2026**,
stdio transport. Public dependencies are locked to the verified combination
(see below). Existing deployments are unaffected until they adopt this branch;
nothing in this release requires touching a running installation.

## Changes

### 1. stdio output: stdout is protocol-only

`mcp_server/server.py` printed its startup banner (server name, detected
EPLAN versions, mode/tool counts, transport line) to **stdout** before
entering the MCP loop. Under the stdio transport, stdout is a
newline-delimited JSON-RPC stream; a plain-text line is not a valid message.

The failure was real but timing-dependent, which is what made it hard to
diagnose: with default block buffering the banner text could sit in the text
layer's buffer and never appear, appear late when buffers flushed, or be
silently discarded when the transport closed the underlying stream — while
with `PYTHONUNBUFFERED=1` (a common client environment) it deterministically
became line 1 of stdout and broke strict JSON parsing before `initialize`
could complete. Reproduced on the baseline:

```
OLD + PYTHONUNBUFFERED=1, FIRST STDOUT LINE: 'EPLAN MCP Server\n'
-> NOT valid JSON-RPC
```

Fix: all startup diagnostics go to **stderr** — the diagnostics channel in
every transport. stdout now carries only what the MCP SDK writes to it.

### 2. .NET-side stdout: Console.Out redirected to Console.Error

When the EPLAN gRPC connection is established, the remote greeting
("Server response: Hello my friend …") is written by .NET through
`Console.Out` — the process's real stdout. In stdio mode that stream is the
JSON-RPC channel, and a real MCP client parsing stdout line by line fails
the session on the first non-JSON line, exactly like the banner bug above
but emitted from native code no Python `print` ever touched.

`_setup_api` now calls `System.Console.SetOut(System.Console.Error)` for
the life of the process — the same policy as the startup banner
(diagnostics to stderr). Placement follows the ordering contract:

    runtime selection → import clr → DLL load → Console redirect → RemoteClient

so the redirect is in place before any `EplanRemoteClient` can exist.
Nothing else reads .NET stdout (results travel via result files), so no
behaviour changes beyond the destination of .NET console writes; a retry of
`_setup_api` harmlessly re-applies the idempotent redirect.

### 3. CLR initialization order: select the runtime, then import clr

`mcp_server/eplan_connection.py` already called
`_select_dotnet_runtime()` before its single `import clr`, but a failed
CoreCLR selection was only *warned about* — execution then fell through to
`import clr`, which silently started pythonnet's default (.NET Framework)
runtime. DLLs from a .NET 8 EPLAN build cannot load there, so one clear
configuration problem became obscure load failures on every subsequent call,
for the life of the process.

Fixes:

- `_select_dotnet_runtime()` now returns success/failure, and `_setup_api`
  **fails fast with an actionable error** (name the version, say what to
  install, say restart) instead of importing `clr` after a failed selection.
  The .NET Framework path (EPLAN 2026 and older — the verified scope) is
  unchanged: no selection is attempted, nothing new can fail there.
- The `AssemblyResolve` handler is now registered **once per process per
  target directory**. `_setup_api` is retried after failures, and each retry
  appended another handler (plus its closure) to the AppDomain for the rest
  of the process lifetime.
- `detect_installed_versions()` now degrades to "no installations found" on
  any `OSError`/`ValueError` from listing the platform root (malformed
  `EPLAN_PLATFORM_ROOT`, permissions, a file where a directory is expected).
  Previously only `FileNotFoundError` was handled; e.g. WinError 123 crashed
  the server at startup, before it ever answered a request.

The ordering contract itself is locked by tests (see below), including a
source-order assertion — once `clr` is in `sys.modules` the import is a no-op,
so no runtime observation can distinguish correct from incorrect ordering.

### 4. Public dependencies locked to the verified combination

`mcp_server/requirements.txt`:

| Package | Was | Now |
|---|---|---|
| `mcp` | `>=1.8.0` | `>=1.30.0,<2` (verified: 1.30.0) |
| `pythonnet` | `>=3.0.0` | `>=3.2.1,<4` (verified: 3.2.1) |
| `basyx-python-sdk` | `>=2.0.1` | `>=2.0.1,<3` |

The floors are the versions this baseline was verified with on
Python 3.12 / Windows x64 / EPLAN 2026; the upper bounds keep untested future
majors — which may change protocol or CLR-loading behaviour — out until they
are tested. Docstring references to the old `>=1.8.0` floor and the "mcp
1.28.x" schema note were updated to match reality on 1.30.0 (verified: 1.30.0
still has no public option to stop Pydantic titling parameter schema nodes,
so the post-registration prune pass remains necessary and unchanged).

## Tests added (all offline; synthetic samples and neutral paths only)

- `tests/test_stdio_protocol_offline.py` — spawns the server as a real
  subprocess over stdio with a neutral environment (empty synthetic
  `EPLAN_PLATFORM_ROOT`, `EPLAN_MCP_LOG_DIR` in a pytest temp dir, no
  extensions) and checks the protocol output automatically: completes
  `initialize` + `tools/list`, parses **every stdout line as JSON-RPC**,
  asserts the banner (and the "no EPLAN installation detected" warning)
  arrives on stderr and that banner-only fragments never appear on stdout,
  and that the server exits cleanly on stdin EOF.
- `tests/test_clr_init_order_offline.py` — locks the runtime-selection-before-
  `import clr` source order; the netfx path performs no pythonnet import; a
  failed CoreCLR selection stops `_setup_api` before `import clr` with the
  actionable error; a full happy-path run against recording fakes
  (no real CLR is ever started in the test process) verifies the
  LoadFrom/AddReference sequence, the `Console.SetOut(Console.Error)`
  redirect and its position after DLL loading, and single
  `AssemblyResolve` registration across retries; and a source-level test
  asserts the Console redirect sits inside `_setup_api` before any
  `EplanRemoteClient` creation site (all of which are behind the
  `_setup_api` gate).
- `tests/test_connection_edges.py` — gains the malformed-`EPLAN_PLATFORM_ROOT`
  degradation case for `detect_installed_versions`.

## Verification

- Baseline before changes: `1379 passed, 3 skipped` (`-m "not live"`) on
  Python 3.12.7 with mcp 1.30.0 / pythonnet 3.2.1.
- After changes: full offline suite green including the new tests
  (`1389 passed, 3 skipped` at the final count).
- The stdout-pollution bug was reproduced against the unfixed baseline (see
  §1) and is asserted fixed by the protocol test on every run.
- No live EPLAN instance was used, and no real project or deployment was
  touched: the new tests communicate only with a spawned server pointed at
  synthetic, temporary paths.

## Compatibility notes

- **Drop-in**: tool set, tool names, return structures, the
  `EPLAN_MCP_EXTENSIONS` extension mechanism, all environment variables, and
  both transports behave exactly as before. Clients need no configuration
  changes.
- EPLAN 2027 (.NET 8) is *detected*, and a failed CoreCLR selection now stops
  the server up front with a clear message rather than misbehaving later —
  but 2027 is outside the verified first-release scope.
- Clients that (incorrectly) read server diagnostics from stdout should read
  stderr instead; stdout was never a documented diagnostics channel.

# Installation — eplan-p8-mcp-server (stdio-stability-v1 baseline)

This guide installs the stability-hardened baseline of the local EPLAN MCP
server. For what changed and why, see [RELEASE_NOTES.md](RELEASE_NOTES.md).
For everyday tool documentation, see
[`mcp_server/README.md`](mcp_server/README.md).

## Compatibility scope (first release)

| Component | Scope | Notes |
|---|---|---|
| Operating system | **Windows x64** | The server loads EPLAN's .NET DLLs in-process; it is not cross-platform. |
| Python | **3.12** (verified) | 64-bit, to match EPLAN's process. The dependency floors below are known-good on 3.12. |
| EPLAN | **2026** (verified) | 2024/2025 use the same .NET Framework remoting stack and are expected to work. EPLAN 2.9 is special-cased in script generation. |
| Transport | stdio (default) | `MCP_TRANSPORT=http` (streamable-http) also available for remote topologies. |

EPLAN 2027+ builds on .NET 8. The server detects this and explicitly selects
the CoreCLR runtime; if that selection fails it refuses to start the wrong
runtime with a clear error instead of failing obscurely on every later call.
First-release verification is EPLAN 2026 (.NET Framework path).

## Locked dependencies

Install exactly what the baseline was verified with — see
[`mcp_server/requirements.txt`](mcp_server/requirements.txt):

| Package | Range | Verified | Why bounded |
|---|---|---|---|
| `mcp` | `>=1.30.0,<2` | 1.30.0 | Protocol behaviour of a future 2.x is untested; the floor is the version the stdio fixes were verified against. |
| `pythonnet` | `>=3.2.1,<4` | 3.2.1 | 3.2.1 is the verified CLR-interop pairing for Python 3.12; a 4.x could change runtime-loading semantics. |
| `basyx-python-sdk` | `>=2.0.1,<3` | 2.x | Optional — only needed for the `aas_*` tools; without it the server starts and the AAS tools are simply disabled. |

## Install steps

```powershell
# 1. Create an isolated environment with the verified Python (Windows x64)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install the locked dependency set
pip install -r eplan-p8-mcp-server\mcp_server\requirements.txt

# 3. Register the server with your MCP client (Claude Code shown)
claude mcp add eplan -- D:\YOURPATH\.venv\Scripts\python.exe D:\YOURPATH\eplan-p8-mcp-server\mcp_server\server.py
claude mcp list   # "eplan" should appear
```

Registering the venv's `python.exe` explicitly (rather than a bare `python`)
pins the server to the environment you just verified.

## Verifying the installation (no EPLAN needed)

The offline test suite needs no EPLAN and no CLR — it exercises registration,
script generation, and the stdio protocol against a spawned server with
synthetic, neutral paths:

```powershell
cd eplan-p8-mcp-server
..\.venv\Scripts\python.exe -m pytest tests -m "not live"
```

Expected: all tests pass (live-EPLAN tests are excluded by `-m "not live"`).

The stdio contract specifically is covered by
`tests/test_stdio_protocol_offline.py`: it spawns the server, completes an
`initialize` / `tools/list` handshake over newline-delimited JSON-RPC, and
asserts that **every stdout line is a valid JSON-RPC message** with the
startup banner on stderr. Run just that file with:

```powershell
..\.venv\Scripts\python.exe -m pytest tests\test_stdio_protocol_offline.py -v
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` (streamable-http; see `MCP_HOST`/`MCP_PORT`). |
| `EPLAN_MCP_MODE` | `full` | `full` publishes every tool; `discovery` publishes a small core plus meta-tools. |
| `EPLAN_MCP_EXTENSIONS` | *(empty)* | `os.pathsep`-separated dirs of private extension modules. |
| `EPLAN_MCP_LOG_DIR` | `mcp_server/logs` | Where `actions.jsonl` is written. |
| `EPLAN_PLATFORM_ROOT` | `C:\Program Files\EPLAN\Platform` | Root of EPLAN installations, for non-standard install locations. |
| `EPLAN_MCP_LEGACY_CLI` | *(off)* | `1` = generated scripts use the original CommandLineInterpreter-only template. |
| `EPLAN_MCP_KEEP_SCHEMA_TITLES` | *(off)* | `1` = keep auto-generated schema titles/defaults (larger payloads). |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8321` | Bind address for `MCP_TRANSPORT=http`. |

## Upgrading an existing checkout

This baseline is a drop-in replacement for the server script: no tool names,
return shapes, configuration keys, or extension-module conventions changed.
An existing client registration keeps working; to pick the baseline up, update
the branch and reinstall dependencies from the (now locked)
`requirements.txt`. Existing deployments do not need to be touched until you
choose to update them.

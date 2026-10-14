"""The stdio transport contract, checked against a real spawned server.

What is under test is not any single tool but the channel itself: in stdio
mode stdout is the JSON-RPC stream (newline-delimited JSON, one message per
line - see mcp.server.stdio) and must carry protocol frames and nothing else.
The pre-stability server printed its startup banner to stdout; a plain-text
line ahead of the first frame is enough for a strict client to fail parsing
and reject the server before initialize ever completes.

The server is started as a subprocess with neutral, synthetic surroundings:

  - EPLAN_MCP_LOG_DIR points into pytest's tmp tree, so no real actions.jsonl
    is ever appended to;
  - EPLAN_MCP_EXTENSIONS is empty, so no site-specific extension pack loads;
  - EPLAN_PLATFORM_ROOT points at a directory with no EPLAN in it, so the
    startup version scan touches nothing real and the banner takes its
    "no EPLAN installation detected" branch.

No EPLAN is contacted, no CLR is loaded: the handshake exercises registration
and the protocol loop only.

The client below speaks just enough JSON-RPC by hand (initialize,
notifications/initialized, tools/list) to keep this an output-format test
rather than a client-library test - the point is to parse exactly what a
peer process would parse.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(_TESTS_DIR, os.pardir, "mcp_server", "server.py")

# Startup budget for the child: building the full-mode app registers ~194
# tools and prunes their schemas, which takes a moment on a cold interpreter.
STARTUP_TIMEOUT_S = 60.0
REQUEST_TIMEOUT_S = 60.0

_EOF = object()


class _LinePump(threading.Thread):
    """Move pipe lines onto a queue so reads can time out (Windows pipes
    have no select(); blocking reads cannot be cancelled)."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.lines = queue.Queue()

    def run(self):
        try:
            for line in self.stream:
                self.lines.put(line)
        except Exception:
            pass
        finally:
            self.lines.put(_EOF)


class StdioClient:
    """Minimal newline-delimited JSON-RPC peer around a spawned server."""

    def __init__(self, proc):
        self.proc = proc
        self.stdout_lines = []
        self.stderr_lines = []
        self._out = _LinePump(proc.stdout)
        self._err = _LinePump(proc.stderr)
        self._out.start()
        self._err.start()

    # -- writing / reading ---------------------------------------------------

    def send(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read(self, pump, collected, timeout):
        item = pump.lines.get(timeout=timeout)
        if item is _EOF:
            raise AssertionError(
                "server closed its %s before answering; stderr so far: %r"
                % (pump is self._out and "stdout" or "stderr",
                   "".join(self.stderr_lines[-5:]))
            )
        collected.append(item)
        return item

    def read_stdout_line(self, timeout=REQUEST_TIMEOUT_S):
        return self._read(self._out, self.stdout_lines, timeout)

    def read_stderr_line(self, timeout=STARTUP_TIMEOUT_S):
        return self._read(self._err, self.stderr_lines, timeout)

    # -- protocol ------------------------------------------------------------

    def request(self, req_id, method, params, timeout=REQUEST_TIMEOUT_S):
        """Send a request; return its response. Every stdout line seen while
        waiting is json.loads'd on the spot, so any non-JSON byte on stdout
        fails the test at the offending line rather than somewhere later."""
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method,
                   "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for id={req_id} response"
            line = self.read_stdout_line(timeout=remaining)
            msg = json.loads(line)  # the protocol-output check itself
            if msg.get("id") == req_id:
                return msg

    def wait_stderr_contains(self, needle, timeout=STARTUP_TIMEOUT_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._err.lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is _EOF:
                break
            self.stderr_lines.append(line)
            if needle in line:
                return True
        return any(needle in l for l in self.stderr_lines)

    # -- teardown ------------------------------------------------------------

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        # Drain whatever the process emitted between the last read and exit,
        # so later assertions see the complete stderr.
        while True:
            try:
                item = self._err.lines.get(timeout=0.5)
            except queue.Empty:
                break
            if item is _EOF:
                break
            self.stderr_lines.append(item)


@pytest.fixture(scope="module")
def stdio_session(tmp_path_factory):
    """A spawned server in full mode, stdio transport, neutral environment."""
    tmp = tmp_path_factory.mktemp("stdio-protocol")

    env = os.environ.copy()
    env.update({
        "MCP_TRANSPORT": "stdio",
        "EPLAN_MCP_MODE": "full",
        "EPLAN_MCP_EXTENSIONS": "",
        "EPLAN_MCP_LOG_DIR": str(tmp / "logs"),
        # Neutral, empty platform root: the startup version scan must not
        # enumerate any real installation, and the banner takes its
        # "no EPLAN detected" branch - which is exactly the text asserted
        # to arrive on stderr below.
        "EPLAN_PLATFORM_ROOT": str(tmp / "no-eplan-here"),
        "PYTHONIOENCODING": "utf-8",
    })

    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(SERVER_PY)],
        cwd=str(tmp),  # proves the server does not depend on the caller's cwd
        env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    client = StdioClient(proc)
    yield client
    client.close()


def test_initialize_and_tools_list_over_stdio(stdio_session):
    """A bare peer can complete initialize + tools/list end to end."""
    resp = stdio_session.request(1, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "stdio-protocol-test", "version": "0"},
    })
    assert resp["jsonrpc"] == "2.0"
    assert "error" not in resp, resp["error"]
    result = resp["result"]
    assert result["serverInfo"]["name"]  # FastMCP("EPLAN MCP Server")
    assert "capabilities" in result

    stdio_session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    resp = stdio_session.request(2, "tools/list", {})
    assert "error" not in resp, resp["error"]
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    # Connection core must be published in full mode without touching EPLAN.
    assert {"eplan_status", "eplan_versions", "eplan_connect"} <= names


def test_stdout_is_jsonrpc_only(stdio_session):
    """Every stdout line produced so far is a complete JSON-RPC message.

    This is the regression lock for the banner bug: the pre-fix server's very
    first stdout line was the plain text "EPLAN MCP Server", followed by
    version info and dashes - all fatal to a client parsing newline-delimited
    JSON. json.loads on each captured line fails at the first stray byte.
    """
    assert stdio_session.stdout_lines, "no stdout captured - handshake failed?"
    for line in stdio_session.stdout_lines:
        msg = json.loads(line)
        assert isinstance(msg, dict)
        assert msg.get("jsonrpc") == "2.0"
        assert "id" in msg or "method" in msg


def test_startup_banner_on_stderr_not_stdout(stdio_session):
    """Diagnostics went to stderr; stdout carries no banner fragments.

    "EPLAN MCP Server" itself also appears inside the initialize response
    (serverInfo.name), so the negative check uses banner-only fragments that
    no JSON-RPC response contains.
    """
    assert stdio_session.wait_stderr_contains("EPLAN MCP Server")
    # The neutral EPLAN_PLATFORM_ROOT makes the banner take this branch,
    # proving the version scan ran against the synthetic empty root.
    assert stdio_session.wait_stderr_contains(
        "WARNING: no EPLAN installation detected", timeout=10)

    banner_only_fragments = (
        "Mode: full",                          # banner tool-count line
        "All actions run as eplan_",           # banner footer
        "WARNING: no EPLAN installation detected",
        "Installed EPLAN versions",
    )
    for line in stdio_session.stdout_lines:
        for fragment in banner_only_fragments:
            assert fragment not in line, \
                f"banner fragment {fragment!r} leaked onto stdout: {line!r}"


def test_server_exits_cleanly_on_stdin_eof(stdio_session):
    """Closing stdin (the client going away) terminates the server.

    Checked here so the fixture's close() behaviour is itself asserted rather
    than assumed: a server that lingers after EOF would hang real clients'
    shutdown just as surely as it hangs the fixture.
    """
    proc = stdio_session.proc
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)
        raise AssertionError("server did not exit within 15s of stdin EOF")
    assert proc.returncode == 0, \
        f"server exited with {proc.returncode} on stdin EOF; stderr tail: " \
        f"{''.join(stdio_session.stderr_lines[-10:])!r}"

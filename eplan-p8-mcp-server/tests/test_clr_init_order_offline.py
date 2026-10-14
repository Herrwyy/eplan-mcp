"""CLR initialization order, locked without ever starting a .NET runtime.

pythonnet can start exactly one .NET runtime per process, and the choice can
only be made before the first `import clr`. eplan_connection._setup_api is
therefore an ordering contract:

    detect installations -> choose target -> SELECT runtime -> import clr
    -> register AssemblyResolve -> LoadFrom the EPLAN DLLs -> AddReference
    -> redirect Console.Out to Console.Error -> (gated callers) RemoteClient

Three stability bugs grew out of violations of that contract, and these tests
pin the fixes:

  1. Selecting the coreclr runtime (EPLAN 2027+/.NET 8) can fail. The old
     code logged a warning and fell through to `import clr` anyway, which
     silently started the default .NET Framework runtime - on which the .NET
     8 EPLAN DLLs cannot load - converting a clear configuration problem
     into obscure failures on every later call. _setup_api must now stop and
     report before the import.
  2. _setup_api can run more than once (it is retried after failures).
     Each run appended another AssemblyResolve handler to the AppDomain; the
     handlers (and their closures) accumulate for the life of the process.
  3. .NET code prints to the process's real stdout via Console.Out - most
     notably the EPLAN remoting greeting ("Server response: Hello my
     friend ...") when the gRPC connection is established. In stdio mode
     stdout is the JSON-RPC channel, so one such non-JSON line kills a
     line-parsing client session. Console.Out is now redirected to
     Console.Error inside _setup_api, before any RemoteClient can exist.

Offline and synthetic throughout: installation trees are marker files in
tmp_path, and clr / System / System.Reflection are replaced with recording
fakes, so no real pythonnet runtime is ever started in the test process -
which also means a regression cannot quietly pin the wrong runtime for the
rest of the suite.
"""

import inspect
import os
import re
import sys
import types

import eplan_connection
from eplan_connection import EPLANConnectionManager


def _make_install(root, full_version, coreclr=False):
    """Synthetic EPLAN install: marker files only, never a real DLL.

    All three EPLAN API DLLs are created because _setup_api LoadFroms each
    one that exists (missing files are skipped there, not here)."""
    bin_dir = os.path.join(str(root), full_version, "Bin")
    os.makedirs(bin_dir, exist_ok=True)
    for dll in ("Eplan.EplApi.Starteru.dll",
                "Eplan.EplApi.RemoteClientu.dll",
                "Eplan.EplApi.Remotingu.dll"):
        open(os.path.join(bin_dir, dll), "w").close()
    if coreclr:
        open(os.path.join(bin_dir, "Grpc.Net.Client.dll"), "w").close()
    return bin_dir


class _PoisonModule(types.ModuleType):
    """A sys.modules entry whose every attribute access fails.

    Seeding `import clr`-style imports with one of these makes a contract
    violation observable AND harmless: the import itself succeeds (the module
    is cached), but the first real use raises, so the broken code path fails
    the assertions instead of loading a real .NET runtime into pytest.
    """

    def __getattr__(self, name):
        raise AssertionError(
            f"module {self.__name__} was used although the CLR init order "
            f"contract forbids it at this point (accessed .{name})"
        )


def test_runtime_selection_precedes_first_clr_import_in_source():
    """The order is structural, so it is locked on the source.

    Once `clr` is in sys.modules the import is a no-op and no runtime
    observation can distinguish "selected then imported" from "imported then
    selected" - but the failure mode (wrong runtime pinned forever) is real.
    The repo already uses source-grepping tests for generated C# for the same
    reason: the guarantee lives in the text.
    """
    src = inspect.getsource(EPLANConnectionManager._setup_api)
    # The gate call IS the selection call (and its first occurrence).
    gate_at = src.index("if not _select_dotnet_runtime(")
    # Match the statement, not the phrase: comments in _setup_api (and this
    # file's own rationale) legitimately mention `import clr` in prose.
    import_matches = list(re.finditer(r"^\s*import clr\b", src, re.MULTILINE))
    assert import_matches, "_setup_api no longer imports clr directly?!"
    import_at = import_matches[0].start()
    assert gate_at < import_at, (
        "_setup_api must select the .NET runtime (and gate on its failure) "
        "before its first `import clr`; pythonnet cannot switch runtimes "
        "afterwards"
    )


def test_netfx_path_selects_no_runtime_and_needs_no_pythonnet(monkeypatch):
    """EPLAN 2026 and older use pythonnet's default runtime: no selection, no
    pythonnet import at all on this path (it must stay lazily skippable)."""
    monkeypatch.setitem(sys.modules, "pythonnet", _PoisonModule("pythonnet"))
    assert eplan_connection._select_dotnet_runtime("netfx") is True


def test_coreclr_selection_failure_fails_setup_before_clr(monkeypatch, tmp_path):
    """A failed coreclr selection must stop _setup_api with a clear error,
    BEFORE `import clr` pins the wrong runtime for the whole process."""
    root = tmp_path / "Platform"
    bin_dir = _make_install(root, "2027.0.1", coreclr=True)
    monkeypatch.setattr(eplan_connection, "PLATFORM_ROOT", str(root))

    # clr / System poisoned: if the code regresses and falls through to using
    # them, the poison raises inside _setup_api's own except-handler and the
    # error message below no longer matches - the test fails loudly and no
    # real runtime was loaded.
    for name in ("clr", "System", "System.Reflection"):
        monkeypatch.setitem(sys.modules, name, _PoisonModule(name))

    def _failing_select(runtime):
        assert runtime == "coreclr"  # the synthetic 2027 build is coreclr
        return False

    monkeypatch.setattr(eplan_connection, "_select_dotnet_runtime", _failing_select)

    mgr = EPLANConnectionManager("2027")
    assert mgr._setup_api() is False
    assert mgr._clr_initialized is False
    # The error names the root cause and the version, and says what to do.
    assert "coreclr" in mgr.last_error or ".NET 8" in mgr.last_error
    assert "2027.0.1" in mgr.last_error
    assert "restart" in mgr.last_error
    # And nothing was loaded from the synthetic tree.
    assert bin_dir not in sys.path


def test_setup_api_happy_path_and_single_resolve_registration(
        monkeypatch, tmp_path):
    """Full netfx happy path against recording fakes: DLLs LoadFrom'd and
    referenced by name, resolver registered exactly once across retries."""
    root = tmp_path / "Platform"
    bin_dir = _make_install(root, "2026.0.9")  # netfx: EPLAN 2026 scope
    monkeypatch.setattr(eplan_connection, "PLATFORM_ROOT", str(root))

    calls = {"loadfrom": [], "addref": [], "setout": []}
    events = []  # one clock, so cross-step ordering is assertable
    console_error = object()

    class _Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    resolve_event = _Event()

    def _add_reference(name):
        calls["addref"].append(name)
        events.append("addref")

    def _load_from(path):
        calls["loadfrom"].append(path)
        events.append("loadfrom")

    def _set_out(writer):
        calls["setout"].append(writer)
        events.append("setout")

    clr = types.ModuleType("clr")
    clr.AddReference = staticmethod(_add_reference)

    reflection = types.ModuleType("System.Reflection")
    reflection.AssemblyName = lambda name: types.SimpleNamespace(Name=name)
    reflection.Assembly = types.SimpleNamespace(
        LoadFrom=staticmethod(_load_from))

    system = types.ModuleType("System")
    system.Reflection = reflection
    system.AppDomain = types.SimpleNamespace(
        CurrentDomain=types.SimpleNamespace(AssemblyResolve=resolve_event))
    system.Console = types.SimpleNamespace(Error=console_error, SetOut=_set_out)

    monkeypatch.setitem(sys.modules, "clr", clr)
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "System.Reflection", reflection)

    # Record the select-vs-use order from the outside.
    real_select = eplan_connection._select_dotnet_runtime

    def _recording_select(runtime):
        events.append("select")
        ok = real_select(runtime)
        events.append("ok" if ok else "failed")
        return ok

    monkeypatch.setattr(eplan_connection, "_select_dotnet_runtime", _recording_select)

    mgr = EPLANConnectionManager("2026")
    # Callers own the flag (self._clr_initialized = self._setup_api()); mirror
    # that here so the assertion checks what production code checks.
    mgr._clr_initialized = mgr._setup_api()
    assert mgr._clr_initialized is True
    assert mgr.target_version == "2026"

    # Selection happened, and it happened before anything touched the CLR;
    # the DLLs were loaded and referenced before .NET's stdout was redirected
    # to .NET's stderr - the greeting that the remoting channel prints on
    # Console.Out must never reach the process's real stdout.
    _one_pass = ["select", "ok"] + ["loadfrom"] * 3 + ["addref"] * 3 + ["setout"]
    assert events == _one_pass
    assert [os.path.basename(p) for p in calls["loadfrom"]] == [
        "Eplan.EplApi.Starteru.dll",
        "Eplan.EplApi.RemoteClientu.dll",
        "Eplan.EplApi.Remotingu.dll",
    ]
    assert calls["addref"] == [
        "Eplan.EplApi.Starteru",
        "Eplan.EplApi.RemoteClientu",
        "Eplan.EplApi.Remotingu",
    ]
    assert calls["setout"] == [console_error]
    assert bin_dir in sys.path
    assert len(resolve_event.handlers) == 1

    # A retry (fresh manager, same target) must not stack a second resolver;
    # SetOut is harmlessly re-applied (idempotent for the process lifetime).
    mgr2 = EPLANConnectionManager("2026")
    assert mgr2._setup_api() is True
    assert len(resolve_event.handlers) == 1
    assert events == _one_pass * 2
    assert calls["setout"] == [console_error, console_error]


def test_console_redirect_precedes_remoteclient_creation():
    """The .NET stdout redirect must be in place before the first
    EplanRemoteClient exists: creating/connecting one is what prints the
    remoting greeting ("Server response: Hello my friend ...") on
    Console.Out, which lands on the process's real stdout - the JSON-RPC
    channel in stdio mode.

    _setup_api is the gate every client-creating path runs through first
    (via the _clr_initialized check), so the contract is structural and
    locked on the source, for the same reason as the select-before-import
    lock: once modules are cached, no runtime observation distinguishes
    "redirected before the client" from "client created, then redirected"
    - but the first ordering keeps stdout clean and the second does not.
    """
    setup_src = inspect.getsource(EPLANConnectionManager._setup_api)

    # Statement positions inside _setup_api: select gate -> import clr ->
    # DLL loading (AddReference) -> Console redirect.
    gate_at = setup_src.index("if not _select_dotnet_runtime(")
    import_at = list(re.finditer(r"^\s*import clr\b", setup_src, re.MULTILINE))[0].start()
    addref_at = setup_src.index('clr.AddReference("Eplan.EplApi.Starteru")')
    setout_matches = list(re.finditer(r"^\s*System\.Console\.SetOut\(",
                                      setup_src, re.MULTILINE))
    assert setout_matches, \
        "_setup_api no longer redirects System.Console.SetOut to Console.Error"
    assert gate_at < import_at < addref_at < setout_matches[0].start()

    # _setup_api never creates the client itself, so its redirect runs to
    # completion before any caller can proceed to one. (The DLL *names*
    # contain "RemoteClient"; the class spelled "EplanRemoteClient" is what
    # the creation sites use.)
    assert "EplanRemoteClient" not in setup_src

    # And every creation site sits behind the _setup_api gate.
    for method in ("get_active_servers", "connect"):
        src = inspect.getsource(getattr(EPLANConnectionManager, method))
        assert "_setup_api()" in src, f"{method} lost its _setup_api gate?!"
        assert src.index("_setup_api()") < src.index("EplanRemoteClient"), (
            f"{method} must run _setup_api (and with it the Console "
            "redirect) before creating an EplanRemoteClient"
        )

import os
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

import pytest

from core.models import Agent
from core.tools import (
    ToolInputError,
    ToolPermissionError,
    build_default_registry,
)
from core.tools.python_exec import MAX_OUTPUT_BYTES, RunPythonInput, run_python_local
from core.tools.search import FakeSearchProvider


def make_agent(*capabilities: str) -> Agent:
    return Agent(
        id=uuid4(), name="coder", capabilities=list(capabilities), created_at=datetime(2026, 1, 1)
    )


def run(code: str, **kwargs) -> str:
    return run_python_local(RunPythonInput(code=code, **kwargs))


def registry():
    return build_default_registry(FakeSearchProvider([]), code_runner=run_python_local)


# ---------- what it returns ----------

def test_returns_output_and_exit_code():
    out = run("print(6 * 7)")
    assert out.startswith("exit code 0")
    assert "42" in out


def test_a_failing_program_is_a_normal_result_with_the_traceback():
    out = run("print('before')\n1 / 0")
    assert out.startswith("exit code 1")
    assert "before" in out
    assert "ZeroDivisionError" in out  # the agent needs this to fix its code


def test_a_program_with_no_output_says_so():
    assert "(no output)" in run("x = 1")


def test_unicode_output_survives():
    assert "héllo ✓" in run("print('héllo ✓')")


# ---------- limits ----------

def test_an_endless_loop_is_stopped_at_the_timeout():
    start = time.time()
    out = run("while True: pass", timeout_seconds=1)
    assert "timed out after 1s" in out
    assert time.time() - start < 8


def test_endless_output_is_capped_not_stored():
    out = run("while True: print('x' * 1000)", timeout_seconds=1)
    assert "output truncated" in out
    assert len(out) < MAX_OUTPUT_BYTES + 500


def test_input_does_not_hang_waiting_for_a_keyboard():
    out = run("input()", timeout_seconds=5)
    assert "EOFError" in out


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs /proc (Linux)")
def test_children_started_by_the_program_are_killed_with_it():
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    started = time.time()
    out = run(code, timeout_seconds=1)
    assert time.time() - started < 10  # returns promptly, not after the child's 60 s sleep
    child = int(out.split("--- output ---\n")[1].split()[0])

    def alive() -> bool:
        try:
            with open(f"/proc/{child}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"  # a zombie is already dead
        except FileNotFoundError:
            return False

    deadline = time.time() + 3
    while alive() and time.time() < deadline:
        time.sleep(0.05)
    assert not alive()


# ---------- isolation (the parts that exist; this is not a sandbox) ----------

def test_the_program_does_not_see_our_environment(monkeypatch):
    monkeypatch.setenv("AGENTOS_FAKE_SECRET", "hunter2")
    out = run("import os\nprint(os.environ.get('AGENTOS_FAKE_SECRET'))\nprint(sorted(os.environ))")
    assert "hunter2" not in out
    assert "None" in out


def test_it_runs_in_a_throwaway_directory_that_is_deleted():
    out = run("import os\nprint(os.getcwd())\nopen('note.txt', 'w').write('hi')")
    path = out.split("--- output ---\n")[1].strip()
    assert "agentos-run-" in path
    assert not os.path.exists(path)


def test_our_installed_packages_are_not_importable():
    out = run("import pydantic")
    assert out.startswith("exit code 1")
    assert "ModuleNotFoundError" in out


def test_it_uses_the_same_python_version_as_the_runtime():
    out = run("import sys\nprint(sys.version_info[:2])")
    assert str(sys.version_info[:2]) in out


# ---------- the registry gate ----------

def test_an_agent_without_the_capability_cannot_run_code():
    with pytest.raises(ToolPermissionError) as exc:
        registry().call(make_agent("web.search"), "run_python", {"code": "print(1)"})
    assert "code.execute" in str(exc.value)


def test_an_agent_with_the_capability_can():
    result = registry().call(make_agent("code.execute"), "run_python", {"code": "print(1 + 1)"})
    assert "2" in result.output


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"code": ""},
        {"code": "x" * 6001},
        {"code": "print(1)", "timeout_seconds": 0},
        {"code": "print(1)", "timeout_seconds": 31},
    ],
)
def test_invalid_arguments_are_rejected_before_anything_runs(args):
    with pytest.raises(ToolInputError):
        registry().call(make_agent("code.execute"), "run_python", args)


def test_agents_only_see_the_tools_their_capabilities_allow():
    reg = registry()
    assert [t.name for t in reg.tools_for(make_agent("code.execute"))] == ["run_python"]
    assert [t.name for t in reg.tools_for(make_agent("web.search"))] == ["web_search"]
    assert reg.tools_for(make_agent()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_children_are_killed_with_the_program_on_windows():
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    started = time.time()
    out = run(code, timeout_seconds=1)
    assert time.time() - started < 10
    child = out.split("--- output ---\n")[1].split()[0]
    time.sleep(1)
    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {child}", "/NH"], capture_output=True, text=True, check=False
    ).stdout
    assert child not in listing  # tasklist prints the PID only while the process is alive
"""These run real containers. They need Docker running and the image pulled (`make sandbox`).
On a laptop without them they skip; in CI they must run."""

import subprocess
import time

import pytest

from core.config import settings
from core.tools.python_exec import DockerRunner, RunPythonInput


@pytest.fixture(scope="module")
def sandbox(docker_ready):
    return DockerRunner(settings.code_exec_image)


def run(sandbox, code, **kwargs) -> str:
    return sandbox(RunPythonInput(code=code, **kwargs))


def test_it_runs_code_and_returns_the_output(sandbox):
    out = run(sandbox, "print(6 * 7)")
    assert out.startswith("exit code 0") and "42" in out


def test_there_is_no_network(sandbox):
    out = run(sandbox, """
import socket
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("1.1.1.1", 53))
    print("CONNECTED")
except OSError:
    print("BLOCKED")
""")
    assert "BLOCKED" in out and "CONNECTED" not in out


def test_the_filesystem_is_read_only_and_tmp_is_small(sandbox):
    out = run(sandbox, """
try:
    open("/etc/hacked", "w").write("x")
    print("WROTE-ROOT")
except OSError:
    print("ROOT-READONLY")
try:
    open("/tmp/big", "wb").write(b"x" * (32 * 1024 * 1024))
    print("WROTE-BIG")
except OSError:
    print("TMP-FULL")
""")
    assert "ROOT-READONLY" in out and "WROTE-ROOT" not in out
    assert "TMP-FULL" in out and "WROTE-BIG" not in out


def test_it_runs_as_an_unprivileged_user(sandbox):
    assert "65534" in run(sandbox, "import os\nprint(os.getuid())")


def test_our_environment_and_files_are_not_visible(sandbox, monkeypatch):
    monkeypatch.setenv("AGENTOS_FAKE_SECRET", "hunter2")
    out = run(sandbox, "import os\nprint(dict(os.environ))\nprint(os.listdir('/'))")
    assert "hunter2" not in out


def test_a_memory_hog_is_killed(sandbox):
    out = run(sandbox, "x = bytearray(600 * 1024 * 1024)\nprint('ALLOCATED')")
    assert "ALLOCATED" not in out
    assert "exit code 137" in out  # killed by the kernel for exceeding the limit


def test_a_fork_bomb_hits_the_process_limit(sandbox):
    out = run(sandbox, """
import os, time
count = 0
try:
    for _ in range(500):
        if os.fork() == 0:
            time.sleep(5)
            os._exit(0)
        count += 1
    print("UNLIMITED", count)
except OSError:
    print("LIMITED", count)
""")
    assert "LIMITED" in out and "UNLIMITED" not in out


def test_a_timeout_stops_and_removes_the_container(sandbox):
    out = run(sandbox, "while True: pass", timeout_seconds=1)
    assert "timed out" in out
    deadline = time.time() + 10
    leftovers = "?"
    while time.time() < deadline:
        leftovers = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=agentos-run=1"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if not leftovers:
            break
        time.sleep(0.3)
    assert leftovers == ""

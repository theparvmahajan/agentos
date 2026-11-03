"""Tests for the container backend that need no Docker: they check the exact command we would
run, and drive the runner against a stand-in `docker` program (a small Python script)."""

import json
import sys
import textwrap

import pytest

from core.config import settings
from core.tools import ToolUnavailable
from core.tools.python_exec import (
    DockerRunner,
    RunPythonInput,
    get_code_runner,
    run_python_local,
)

IMAGE = "python:3.12-slim"

FAKE_DOCKER = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    mode = os.environ.get("FAKE_DOCKER_MODE", "ok")
    if args[0] == "kill":
        sys.exit(0)
    if mode == "daemon_down":
        print("docker: Cannot connect to the Docker daemon. Is the docker daemon running?")
        sys.exit(125)
    if mode == "no_image":
        print("Unable to find image 'python:3.12-slim' locally")
        print("docker: Error response from daemon: pull access denied.")
        sys.exit(125)
    if mode == "hang":
        time.sleep(60)
    sys.exit(subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", "-"]).returncode)
    """
)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE_DOCKER)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    runner = DockerRunner(IMAGE, docker=[sys.executable, str(script)])
    return runner, calls


def run(runner, code="print(1)", **kwargs) -> str:
    return runner(RunPythonInput(code=code, **kwargs))


# ---------- the command itself: every limit is asserted ----------

def pairs(command: list[str]) -> dict[str, str]:
    return {a: command[i + 1] for i, a in enumerate(command[:-1]) if a.startswith("--")}


def test_the_container_has_no_network_a_read_only_filesystem_and_no_privileges():
    flags = pairs(DockerRunner(IMAGE).command("n"))
    assert flags["--network"] == "none"
    assert "--read-only" in DockerRunner(IMAGE).command("n")
    assert flags["--cap-drop"] == "ALL"
    assert flags["--security-opt"] == "no-new-privileges"
    assert flags["--user"] == "65534:65534"  # not root


def test_the_container_has_resource_limits():
    flags = pairs(DockerRunner(IMAGE).command("n"))
    assert flags["--memory"] == "128m"
    assert flags["--memory-swap"] == "128m"  # equal to memory: no swap
    assert flags["--pids-limit"] == "64"
    assert flags["--cpus"] == "1"
    assert "size=16m" in flags["--tmpfs"] and "noexec" in flags["--tmpfs"]


def test_nothing_from_this_machine_is_shared_with_the_container():
    command = DockerRunner(IMAGE).command("n")
    forbidden = {"-v", "--volume", "--mount", "--privileged", "--env", "-e", "--env-file",
                 "--device", "--pid", "--ipc", "--userns", "--cap-add"}
    assert not forbidden & set(command)
    assert pairs(command)["--network"] != "host"


def test_it_never_downloads_during_a_run_and_always_cleans_up():
    command = DockerRunner(IMAGE).command("n")
    assert pairs(command)["--pull"] == "never"
    assert "--rm" in command


def test_the_image_and_interpreter_come_last_and_the_program_is_read_from_stdin():
    command = DockerRunner(IMAGE).command("n")
    assert command[-7:] == [IMAGE, "python", "-I", "-S", "-X", "utf8", "-"]


# ---------- driving it through a stand-in docker ----------

def test_the_code_is_sent_on_stdin_never_on_the_command_line(fake):
    runner, calls = fake
    marker = "print('UNIQUE-MARKER-12345')"
    out = run(runner, marker)

    assert "UNIQUE-MARKER-12345" in out and out.startswith("exit code 0")
    assert "UNIQUE-MARKER-12345" not in json.dumps(calls())  # argv is visible to every process


def test_a_failing_program_is_a_normal_result(fake):
    runner, _ = fake
    out = run(runner, "1 / 0")
    assert out.startswith("exit code 1") and "ZeroDivisionError" in out


def test_each_run_gets_its_own_container_name(fake):
    runner, calls = fake
    run(runner)
    run(runner)
    names = [c[c.index("--name") + 1] for c in calls() if c[0] == "run"]
    assert len(names) == 2 and names[0] != names[1]


def test_a_timeout_kills_the_container_not_just_the_client(fake, monkeypatch):
    runner, calls = fake
    monkeypatch.setenv("FAKE_DOCKER_MODE", "hang")
    monkeypatch.setattr("core.tools.python_exec.DOCKER_STARTUP_GRACE_SECONDS", 0)

    out = run(runner, timeout_seconds=1)

    assert "timed out after 1s" in out
    run_call = next(c for c in calls() if c[0] == "run")
    kill_call = next(c for c in calls() if c[0] == "kill")
    assert kill_call[-1] == run_call[run_call.index("--name") + 1]


# ---------- it fails closed ----------

def test_docker_not_installed_means_nothing_runs():
    runner = DockerRunner(IMAGE, docker=["definitely-not-a-real-docker-binary"])
    with pytest.raises(ToolUnavailable) as exc:
        run(runner)
    assert "Docker was not found" in str(exc.value)


def test_docker_not_running_means_nothing_runs(fake, monkeypatch):
    runner, _ = fake
    monkeypatch.setenv("FAKE_DOCKER_MODE", "daemon_down")
    with pytest.raises(ToolUnavailable) as exc:
        run(runner)
    assert "not running" in str(exc.value)


def test_a_missing_image_says_how_to_get_it(fake, monkeypatch):
    runner, _ = fake
    monkeypatch.setenv("FAKE_DOCKER_MODE", "no_image")
    with pytest.raises(ToolUnavailable) as exc:
        run(runner)
    assert f"docker pull {IMAGE}" in str(exc.value)


def test_a_program_that_itself_exits_125_is_not_mistaken_for_a_docker_failure(fake):
    runner, _ = fake
    out = run(runner, "import sys\nprint('done')\nsys.exit(125)")
    assert out.startswith("exit code 125") and "done" in out


# ---------- choosing the backend ----------

def test_docker_is_the_default_backend():
    assert settings.code_exec_backend == "docker"
    assert isinstance(get_code_runner(), DockerRunner)


def test_local_must_be_asked_for_explicitly(monkeypatch):
    monkeypatch.setattr(settings, "code_exec_backend", "local")
    assert get_code_runner() is run_python_local


def test_an_unknown_backend_stops_startup(monkeypatch):
    monkeypatch.setattr(settings, "code_exec_backend", "yolo")
    with pytest.raises(ValueError, match="unknown CODE_EXEC_BACKEND"):
        get_code_runner()

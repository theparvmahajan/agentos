"""run_python: run a small Python program with as little access as we can give it.

Two backends, chosen by CODE_EXEC_BACKEND:

docker (the default): every run is a fresh, throwaway container with no network, a read-only
    filesystem, no Linux capabilities, a non-root user, and limits on memory, CPU, processes
    and disk. The code goes in over stdin and NOTHING from this machine is mounted into it.
    It fails closed: if Docker is not usable, the tool raises ToolUnavailable and runs nothing.

local: a plain subprocess on THIS machine. Isolated interpreter, throwaway directory, empty
    environment, timeout, output cap. It is NOT a sandbox: the code can read your files, use
    the network and start programs. You must ask for it explicitly.
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import uuid4

from pydantic import BaseModel, Field

from core.config import settings
from core.tools.base import Tool, ToolError, ToolUnavailable

log = logging.getLogger(__name__)

MAX_CODE_CHARS = 6000
DEFAULT_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 6000  # what we keep; the rest is read and thrown away
DOCKER_STARTUP_GRACE_SECONDS = 5  # starting a container takes time the program should not pay for

# The only variable the local program gets. Windows needs SYSTEMROOT for sockets and random numbers.
_ENV_ALLOWLIST = ("SYSTEMROOT",)


class RunPythonInput(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=MAX_CODE_CHARS,
        description="A complete Python 3 program. Standard library only. Use print() for results.",
    )
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        description="Stop the program after this many seconds.",
    )


CodeRunner = Callable[[RunPythonInput], str]


# ---------- running a process safely (shared by both backends) ----------

@dataclass(frozen=True)
class _Outcome:
    returncode: int
    text: str
    total_bytes: int
    timed_out: bool


def _drain(stream, keep: bytearray, counter: list[int]) -> None:
    """Read until the program closes its output. Keep the first MAX_OUTPUT_BYTES only, so a
    program that prints forever cannot fill our memory."""
    while chunk := stream.read(4096):
        counter[0] += len(chunk)
        room = MAX_OUTPUT_BYTES - len(keep)
        if room > 0:
            keep.extend(chunk[:room])


def _kill(proc: subprocess.Popen) -> None:
    """Kill the program and anything it started."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)  # the whole process group
        else:
            # /T also kills the program's children. (Untested on the Linux CI: Windows only.)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _execute(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    timeout: float,
    cwd: str | None = None,
    stdin_data: bytes | None = None,
    on_timeout: Callable[[], None] | None = None,
) -> _Outcome:
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # own process group, so we can kill it all

    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # one stream, in the order the program wrote it
        **popen_kwargs,
    )
    kept = bytearray()
    total = [0]
    reader = threading.Thread(target=_drain, args=(proc.stdout, kept, total), daemon=True)
    reader.start()

    if stdin_data is not None:
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        except OSError:  # it exited before reading its input; its output tells the story
            pass

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if on_timeout:
            on_timeout()
        _kill(proc)
        proc.wait()
    reader.join(timeout=2)
    if proc.stdout and not reader.is_alive():
        # If a stray child still holds the pipe open, closing it here would block until that
        # child exits. Leave it to the (daemon) reader thread instead.
        proc.stdout.close()

    return _Outcome(proc.returncode, bytes(kept).decode("utf-8", "replace"), total[0], timed_out)


def _render(outcome: _Outcome, limit_seconds: int) -> str:
    text = outcome.text
    if outcome.total_bytes > MAX_OUTPUT_BYTES:
        text += f"\n[output truncated: {outcome.total_bytes} bytes in total]"
    status = (
        f"timed out after {limit_seconds}s and was stopped"
        if outcome.timed_out
        else f"exit code {outcome.returncode}"
    )
    return f"{status}\n--- output ---\n{text or '(no output)'}"


# ---------- backend 1: a locked-down container ----------

class DockerRunner:
    """Run the code in a container that can do almost nothing. See `command` for every limit."""

    def __init__(self, image: str, docker: Sequence[str] = ("docker",)):
        self.image = image
        self.docker = tuple(docker)

    def command(self, name: str) -> list[str]:
        return [
            *self.docker, "run",
            "--rm",                        # delete the container when it stops
            "-i",                          # keep stdin open: the code arrives there
            "--name", name,
            "--label", "agentos-run=1",    # so leftovers can be found and removed
            "--pull", "never",             # never download during a run (it would eat the timeout)
            "--network", "none",           # no network at all
            "--read-only",                 # the image's filesystem cannot be changed
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",  # the only writable place: small, in RAM
            "--cap-drop", "ALL",           # no Linux capabilities
            "--security-opt", "no-new-privileges",
            "--pids-limit", "64",          # stops fork bombs
            "--memory", "128m",
            "--memory-swap", "128m",       # same as memory: no swap
            "--cpus", "1",
            "--user", "65534:65534",       # 'nobody', not root
            "--workdir", "/tmp",
            self.image,
            "python", "-I", "-S", "-X", "utf8", "-",   # '-' = read the program from stdin
        ]

    def __call__(self, args: RunPythonInput) -> str:
        name = f"agentos-run-{uuid4().hex[:12]}"
        limit = args.timeout_seconds + DOCKER_STARTUP_GRACE_SECONDS

        def stop_container() -> None:
            # Killing the `docker run` client does not stop the container; this does.
            try:
                subprocess.run(
                    [*self.docker, "kill", name], capture_output=True, timeout=10, check=False
                )
            except (OSError, subprocess.SubprocessError):
                log.warning("could not kill container %s; remove it with `docker rm -f`", name)

        try:
            outcome = _execute(
                self.command(name),
                env=dict(os.environ),  # for the docker CLI itself; the container never sees it
                timeout=limit,
                stdin_data=args.code.encode("utf-8"),
                on_timeout=stop_container,
            )
        except FileNotFoundError as e:
            raise ToolUnavailable(
                f"Docker was not found ({e}). Install and start Docker, then run `make sandbox`."
            ) from e
        except OSError as e:
            raise ToolUnavailable(f"could not start Docker: {e}") from e

        # Exit codes 125-127 with a "docker:" message mean `docker run` itself failed
        # (daemon down, image missing), not that the program did.
        if not outcome.timed_out and outcome.returncode in (125, 126, 127) and (
            "docker:" in outcome.text or "Unable to find image" in outcome.text
        ):
            raise ToolUnavailable(self._explain(outcome.text))
        return _render(outcome, limit)

    def _explain(self, output: str) -> str:
        if "Unable to find image" in output or "No such image" in output:
            return f"the sandbox image is missing. Run: docker pull {self.image}  (make sandbox)"
        if "daemon" in output or "Cannot connect" in output or "pipe" in output:
            return "Docker is installed but not running. Start Docker Desktop and try again."
        first = next((ln for ln in output.splitlines() if ln.strip()), "unknown error")
        return f"Docker could not start the sandbox: {first[:200]}"


# ---------- backend 2: plain subprocess (explicit opt-in) ----------

def run_python_local(args: RunPythonInput) -> str:
    """NOT a sandbox. See the module docstring."""
    workdir = tempfile.mkdtemp(prefix="agentos-run-")
    try:
        script = os.path.join(workdir, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(args.code)
        try:
            outcome = _execute(
                [sys.executable, "-I", "-S", "-X", "utf8", script],
                env={k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ},
                cwd=workdir,
                timeout=args.timeout_seconds,
            )
        except OSError as e:
            raise ToolError(f"could not start python: {e}") from e
        return _render(outcome, args.timeout_seconds)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- choosing a backend, and the tool ----------

def get_code_runner() -> CodeRunner:
    backend = settings.code_exec_backend
    if backend == "docker":
        return DockerRunner(settings.code_exec_image)
    if backend == "local":
        log.warning("CODE_EXEC_BACKEND=local: agent-written code runs on this machine, NOT isolated")
        return run_python_local
    raise ValueError(f"unknown CODE_EXEC_BACKEND '{backend}' (use 'docker' or 'local')")


def run_python_tool(runner: CodeRunner | None = None) -> Tool:
    return Tool(
        name="run_python",
        description=(
            "Run a Python 3 program and get its combined stdout/stderr and exit code. "
            "Standard library only, no internet, no files from other tasks. "
            "Runs in a fresh empty directory; it is deleted afterwards."
        ),
        capability="code.execute",
        input_model=RunPythonInput,
        handler=runner or get_code_runner(),
    )
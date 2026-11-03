# 0011: Coder and Reviewer agents, and code execution

Status: accepted, 2026-11-04
Issue: #12

## Context

The runtime had one agent (the researcher) whose think / call a tool / read the result loop was
written for that agent alone. Milestone 2 needs two more: a **Coder**, which must run code it
writes, and a **Reviewer**, which checks other agents' work. Running code written by an LLM is the
first place where a model's output becomes an action on a real machine. That output can be steered
by text the model has read, so execution has to be constrained by the runtime, not by the prompt.

## Decision

- **One shared loop, many roles.** `agents/loop.py` holds the loop. An agent is a `Role`: intro,
  rules, task type (which model to ask), step limit, an answer-reply shape and an optional answer
  check. The researcher's prompt wording and its tests were not changed by the refactor.
- **`run_python` runs in a throwaway Docker container by default** (`CODE_EXEC_BACKEND=docker`):
  no network, read-only filesystem plus a 16 MB `noexec` tmpfs, all capabilities dropped,
  `no-new-privileges`, user 65534, 64 processes, 128 MB memory with no swap, 1 CPU. The code
  arrives on stdin; nothing from the host is mounted and no environment is passed.
- **It fails closed.** If Docker is missing, stopped, or the image is absent, the tool raises
  `ToolUnavailable`, the agent stops at once with that reason, and nothing runs on the host.
  `CODE_EXEC_BACKEND=local` (a plain subprocess, no isolation) exists only as an explicit opt-in.
- **Guards the runtime enforces, not the prompt:** the Coder cannot give a final answer until
  `run_python` has succeeded at least once; the Reviewer's first line must be
  `VERDICT: APPROVE` or `VERDICT: REJECT`; a Reviewer with no dependencies fails before any
  LLM call; permissions are checked by the tool registry from the agent's capabilities.
- **A REJECT is a successful review**, not a failed task.

## Design questions and answers

**1. Why a shared loop with `Role` instead of copying the researcher's loop twice?**
All three agents need the same mechanics: build the prompt, parse a JSON step, dispatch a tool
through the registry, keep a scratchpad of results, count tokens, stop after a step limit, and
treat tool results as data. Copies drift: a fix to one (for example the rule that tool output must
not be obeyed) would silently be missing from the others. The `Role` holds only what differs
between agents (intro, rules, task type, step limit, reply shape, answer check); the loop holds
the mechanics. I would regret this if an agent needed a different control flow, such as plan then
act, parallel tool calls or a human in the loop. A narrow hook like `check_answer(answer,
tool_successes)` would then be too small, and a second kind of loop would be needed.

**2. Why fail closed, and what does the container actually stop?**
The dangerous path is: a web page contains text, the researcher quotes it in its output, the Coder
receives that output as context, and the model writes code that does what the page said. The
prompt tells the model to treat context as data, but that is a request, not enforcement, so the
container is the enforcement. `--network none` blocks exfiltration and downloads;
`--read-only` with a tiny `noexec` tmpfs means the code cannot change the image or leave anything
behind; `--cap-drop ALL` and `no-new-privileges` remove privilege tricks; `--user 65534` means
it is not root; `--pids-limit 64` stops fork bombs; `--memory 128m` with equal swap kills memory
hogs; `--cpus 1` caps CPU; no mounts and no `--env` mean no host files or secrets are visible;
`--pull never` means a run never downloads anything. A missing Docker stops the task rather than
falling back to the host, because a silent fallback would make the system safe only when Docker
happens to be running, and nobody would notice the day it is not. What a container still does not
protect against: the container shares the host kernel (a kernel or runtime bug is an escape), the
Docker daemon is trusted, the image is trusted by tag and not by digest, up to one CPU can be used
for up to 35 seconds per run with no limit on the number of runs, and the output the model reads
back could itself contain injected text (it is labelled and clipped, not sanitized).

**3. Why does a REJECT complete the task instead of failing it?**
Failing it would mark the mission failed with the review buried in an error string, skip every
dependent task, and in milestone 4 make a retry re-run a reviewer that did its job correctly and
would very likely give the same verdict. "Failed" should mean the system could not do the job;
"reject" means the job was done and the answer was no. So the verdict is the output, with a
machine-readable first line (`parse_verdict`). If the verdict gated something important, such as a
deployment, I would not decide the same way: a text verdict is not enough. The gate would be a
separate, typed condition the scheduler or a human acts on, and a missing or ambiguous verdict
would count as a rejection. Today nothing downstream branches on the verdict yet.

**4. What does the Coder's answer check stop, and what does it not?**
It stops the Coder from reporting results it never produced: a final answer is refused until a
`run_python` call has succeeded, and a call with invalid arguments does not count. It does not
stop the Coder from running something unrelated, or from writing in its answer an output that
differs from what the tool actually returned (running `print(14)` and reporting 15). The
Reviewer is a second line of defence but cannot re-run the code. A stronger design would have the
runtime insert the last real output into the answer itself, or verify that the claimed output
appears in the tool result.

**5. Which layer decided that the researcher may not run code?**
The registry, not the prompt. A test makes the researcher ask for `run_python`; the registry
refuses with "lacks capability 'code.execute'", the model is told, and a marker file the code
would have created never appears. A prompt-level restriction can be talked around by a model that
has been persuaded or injected. The registry check is ordinary code that runs whatever the model
says, keyed on the capabilities stored on the agent's row. Prompts only decide which tools are
shown; the registry decides which calls are allowed.

**6. Three limits to state in the README.**
(a) Container isolation shares the host kernel: strong enough for a development machine, not for
hostile strangers, and the image is pinned by tag, not digest. (b) A 7B local model on 4 GB of VRAM
is slow and sometimes produces invalid steps; the loop tolerates this only up to the step limit,
and Ollama's context size is not configured by us, so long prompts can be silently truncated.
(c) `agents.timeout_seconds` is stored but not enforced at the task level; only `run_python` has
its own timeout, so an LLM call can hold a task for up to the LLM client timeout.

## Consequences

- Adding an agent means writing a `Role`; adding a tool means registering it with a capability.
- Code execution needs Docker and a one-time `make sandbox`; without it, code tasks fail clearly.
- The container tests ran green on Windows with Docker Desktop (8 tests: no network, read-only
  root, tiny `/tmp`, non-root, no host environment, memory kill, process limit, timeout cleanup).
  One test that checks child-process cleanup is Linux-only and is skipped on Windows, so the
  Windows process-tree kill used by the `local` backend is not covered by an automated test.
- The planner can still assign work to a "wrong" agent; the only structural check is that a
  Reviewer has at least one dependency.

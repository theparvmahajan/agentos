# Failure testing

AgentOS claims to survive crashes and outages. This page says how that is tested, what each
scenario is expected to do, which test proves it, and where the proof stops.

```bash
python -m pytest tests/chaos -q          # about 45 s: real processes, real connections
CHAOS_LOGDIR=/tmp/chaoslogs python -m pytest tests/chaos -q   # keep every child process's log
python -m scripts.measure_recovery --trials 10 --fast         # recovery time, see below
```

## How the failures are injected

- **Processes are killed, not asked to stop.** The scheduler and the worker run as real processes
  and are ended with `Popen.kill()` (SIGKILL on Linux/macOS, TerminateProcess on Windows). No cleanup
  code runs, which is the point.
- **Connections are really broken.** `tests/chaos/proxy.py` is a small TCP proxy placed between a
  component and Redis or Postgres. It has three modes: `up`; `reset` (every connection is cut at once
  and new ones are cut on arrival, like a server that crashed); `blackhole` (connections stay open and
  nothing gets through in either direction, like a network partition: no error, no answer). It is
  plain Python on purpose, so it runs the same on Linux, macOS, Windows and CI, with no extra tools.
- **Each test states what MUST happen** and checks the final state in Postgres, not only that
  nothing crashed.

## The ten scenarios of the engineering addendum (section 2.1)

| # | Scenario | What must happen | Proved by | Status |
|---|---|---|---|---|
| 1 | Worker killed while running a task | its lease runs out, another worker runs the task again, the mission completes | `test_killing_a_worker_process_mid_task_does_not_lose_the_mission`, `test_killing_the_scheduler_or_the_worker_or_both_...[worker]` | covered |
| 2 | API restarted after mission creation | the mission is already in Postgres. If the API stopped before planning finished, the planner job is lost: the mission is declared failed after `PLANNING_STALE_SECONDS` (15 min) instead of staying "pending" for ever | `test_a_mission_whose_planner_never_finished_is_declared_failed[pending]` and `[planning]`, `test_a_slow_planner_that_finishes_after_the_mission_was_failed_changes_nothing` | **detected and failed, not resumed**: the user resubmits. A real API process was not killed in a test |
| 3 | Duplicate queue delivery | the task runs once | `test_six_workers_given_the_same_message_run_the_task_exactly_once`, `test_a_stray_message_cannot_run_a_task_before_its_delay_is_over`, the sweeper tests | covered |
| 4 | Redis temporarily unavailable | no state is lost, no task runs twice, the mission completes | `test_a_mission_survives_an_outage_and_every_task_completes[fault0]` (Redis reset) and `[fault1]` (Redis partition), `test_a_failed_acknowledgement_does_not_kill_the_scheduler` | covered |
| 5 | PostgreSQL connection interrupted | components survive the outage; the mission completes; at worst the affected task is repeated once | `test_a_mission_survives_an_outage_and_every_task_completes[fault2]`, `test_an_outage_that_hits_the_result_save_costs_one_extra_attempt_and_nothing_more` | covered for dropped connections. **Not covered:** a connection that stays open but never answers (`test_known_gaps.py`, an expected failure) |
| 6 | Tool call timeout | the attempt is cut off at the agent's timeout and retried with backoff | `test_a_task_that_runs_too_long_times_out_and_is_retried`, `test_an_abandoned_runner_that_finishes_later_changes_nothing`; the code sandbox enforces its own limit (`tests/integration/test_docker_sandbox.py`) | covered at task level. A stuck thread is abandoned, not killed |
| 7 | Invalid or expired approval | | | **not applicable yet**: human approval is milestone M6 |
| 8 | Worker gets a task but dies before acknowledging | the message is replayed; nothing runs twice | `test_if_announcing_fails_the_message_stays_pending_and_the_replay_announces`, the kill tests | covered |
| 9 | Two workers try to claim one task | exactly one wins | `test_six_workers_given_the_same_message_run_the_task_exactly_once`, `test_a_failed_claim_changes_nothing_and_leaves_no_lease` | covered |
| 10 | Task cancelled while waiting / while running | not run / result discarded, and never retried | `test_a_task_that_was_queued_and_then_cancelled_is_not_run`, `test_a_task_cancelled_while_it_runs_has_its_result_discarded_quietly`, `test_a_cancelled_task_is_not_retried` | covered |

Beyond the list: killing the scheduler, the worker, or both at once mid-mission
(`test_process_kills.py`); a lost "task finished" or "task failed" announcement
(`test_stuck_missions.py`); a scheduler acknowledgement failing at exactly the wrong moment.

## Measured recovery time

`python -m scripts.measure_recovery` starts a real scheduler and two real workers, lets one start a
task, kills it at a random moment of its heartbeat cycle, and times how long until another worker
STARTS the task again. That is the span in which a user sees a mission frozen.

| settings | trials | min | median | max | the settings predict |
|---|---|---|---|---|---|
| shipped defaults (lease 30 s, heartbeat 10 s, reaper 10 s, sweep 2 s, retry base 2 s) | 4 | 32.1 s | 37.3 s | 40.6 s | 21 to 44 s |
| short lease (3 s / 1 s / 1 s / 0.2 s / 0.5 s) | 10 | 3.5 s | 4.0 s | 4.5 s | 2.2 to 4.7 s |

Machine: a 1-CPU Linux sandbox, Python 3.12. These numbers show the mechanism behaves as the
settings predict; they are not benchmarks of your hardware. Four trials are too few for percentiles.
Run `python -m scripts.measure_recovery --trials 10` on your own machine before quoting a number.

## What these tests cannot prove

- **A proxy on one machine is not a real network.** It models resets and silence, not packet loss,
  latency spikes or partial partitions.
- **Races need real parallelism.** A single-CPU machine hides them. The sweeper race of #18
  (`claim_due_ready`: two sweepers could both publish a task) passed every local test and was caught
  only by CI on a multi-core runner; the fix is the advisory lock now in that function. Run the
  concurrency tests on a multi-core machine before trusting them.
- **Timing-based tests can flake.** Most waits poll for a condition instead of sleeping a fixed
  time, but the lease and timeout tests use real (sub-second) durations.
- **A passing suite shows the listed scenarios behave; it does not show others do.** The expected
  failure in `test_known_gaps.py` is the reminder: the silent connection was found only because a test
  hung.

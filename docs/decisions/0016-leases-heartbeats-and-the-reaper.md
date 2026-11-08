# 0016: Leases, heartbeats and the reaper

Status: draft, to be completed by the author
Issue: #17

## Context

A worker killed mid-task left its task `running` forever; the redelivered message was treated as a
duplicate and dropped, so the mission never finished (reproduced by hand with `kill -9` in M2).

## What was built (facts)

- `task_leases` table (migration 003): who holds a running task, which attempt, and until when.
- A worker claims a task and takes the lease in one transaction, renews it from a heartbeat thread
  while the runner works, and saves its result fenced to its attempt number.
- The reaper (run inside the scheduler loop) finds running tasks whose lease expired and, with one
  guarded UPDATE each, puts them back to `ready` (attempts left) or marks them `failed`, then publishes.
- Expiry is computed with the database clock. The lease lives in its own table, not on `tasks`.

## Questions to answer in your own words

1. **Why a separate table** instead of `lease_owner`/`lease_expires_at` columns on `tasks`? (Think
   about migration 002 and what a heartbeat every few seconds would do to the dashboard feed.)
2. **Why the database clock?** What goes wrong if each worker compares `expires_at` with its own
   clock? Give a concrete example with two machines whose clocks differ by a minute.
3. **What is the fencing token and what exact bug does it stop?** Describe the interleaving in the
   test `test_a_zombie_that_finishes_while_the_retry_is_still_running_is_refused`. Which line of
   `_record` / `set_task_status` would you remove to make that test fail?
4. **Lease length trade-off.** What happens with a 2-second lease? With a 10-minute one? Why must
   the heartbeat be at most half the lease?
5. **When is it safe to re-run a task?** Today's tools are safe to repeat. Name a tool that would
   not be, and say what the reaper must do differently for it (see docs/RELIABILITY.md "Not yet").
6. **What can still go wrong?** Pick one row of the "Not yet" table and describe the failing
   sequence step by step (for example: the reaper commits READY, then the process dies).

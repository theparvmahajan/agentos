import type { Mission, MissionSummary, Task } from "@/lib/types";

export function makeTask(over: Partial<Task> = {}): Task {
  const key = over.key ?? "t1";
  return {
    id: `id-${key}`,
    key,
    agent: "researcher",
    instruction: `do ${key}`,
    status: "pending",
    depends_on: [],
    attempts: 0,
    output: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...over,
  };
}

export function makeMission(over: Partial<Mission> = {}): Mission {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    objective: "compare databases",
    status: "running",
    result: null,
    error: null,
    created_at: "2026-11-05T10:00:00+00:00",
    updated_at: "2026-11-05T10:00:05+00:00",
    completed_at: null,
    tasks: [],
    ...over,
  };
}

export function makeSummary(over: Partial<MissionSummary> = {}): MissionSummary {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    objective: "compare databases",
    status: "running",
    error: null,
    created_at: "2026-11-05T10:00:00+00:00",
    updated_at: "2026-11-05T10:00:05+00:00",
    completed_at: null,
    tasks_total: 3,
    tasks_completed: 1,
    ...over,
  };
}

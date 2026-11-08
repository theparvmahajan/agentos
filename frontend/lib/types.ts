// The shapes the API sends. They mirror services/api/schemas.py: see docs/API.md.

export const MISSION_STATUSES = [
  "pending",
  "planning",
  "running",
  "completed",
  "failed",
  "cancelled",
] as const;
export type MissionStatus = (typeof MISSION_STATUSES)[number];

export const TASK_STATUSES = [
  "pending",
  "ready",
  "running",
  "completed",
  "failed",
  "skipped",
  "cancelled",
] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

export interface Task {
  id: string;
  key: string;
  agent: string;
  instruction: string;
  status: TaskStatus;
  /** keys of the tasks this one waits for */
  depends_on: string[];
  attempts: number;
  output: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** A mission's own fields (no tasks). */
export interface MissionMeta {
  id: string;
  objective: string;
  status: MissionStatus;
  result: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface Mission extends MissionMeta {
  tasks: Task[];
}

/** What a "mission" stream message changes. */
export interface MissionPatch {
  id: string;
  status: MissionStatus;
  result: string | null;
  error: string | null;
  updated_at: string;
  completed_at: string | null;
}

export interface MissionSummary {
  id: string;
  objective: string;
  status: MissionStatus;
  error: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  tasks_total: number;
  tasks_completed: number;
}

export interface MissionPage {
  items: MissionSummary[];
  next_cursor: string | null;
}

export type StreamMessage =
  | { type: "snapshot"; mission: Mission }
  | { type: "task"; mission_id: string; task: Task }
  | { type: "mission"; mission: MissionPatch };

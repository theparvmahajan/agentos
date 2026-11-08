import type { MissionStatus, TaskStatus } from "./types";

/** How a status is drawn. Colour is never the only signal: every badge also shows its label. */
export type Tone = "neutral" | "info" | "active" | "success" | "danger" | "muted";

const MISSION_FINISHED: ReadonlySet<MissionStatus> = new Set(["completed", "failed", "cancelled"]);
const TASK_FINISHED: ReadonlySet<TaskStatus> = new Set([
  "completed",
  "failed",
  "skipped",
  "cancelled",
]);

export const isMissionFinished = (status: MissionStatus): boolean => MISSION_FINISHED.has(status);
export const isTaskFinished = (status: TaskStatus): boolean => TASK_FINISHED.has(status);

export const MISSION_LABEL: Record<MissionStatus, string> = {
  pending: "Pending",
  planning: "Planning",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const TASK_LABEL: Record<TaskStatus, string> = {
  pending: "Waiting",
  ready: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  skipped: "Skipped",
  cancelled: "Cancelled",
};

export const MISSION_TONE: Record<MissionStatus, Tone> = {
  pending: "neutral",
  planning: "info",
  running: "active",
  completed: "success",
  failed: "danger",
  cancelled: "muted",
};

export const TASK_TONE: Record<TaskStatus, Tone> = {
  pending: "neutral",
  ready: "info",
  running: "active",
  completed: "success",
  failed: "danger",
  skipped: "muted",
  cancelled: "muted",
};

import { MISSION_STATUSES, TASK_STATUSES } from "./types";
import type { Mission, MissionPatch, StreamMessage, Task } from "./types";

// The server is ours, but the browser should still not trust whatever arrives on a socket:
// a malformed message is dropped instead of crashing the page.

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === "string";
const isStrOrNull = (v: unknown): v is string | null => v === null || typeof v === "string";

export function isTask(v: unknown): v is Task {
  return (
    isObj(v) &&
    isStr(v.id) &&
    isStr(v.key) &&
    isStr(v.agent) &&
    isStr(v.instruction) &&
    (TASK_STATUSES as readonly unknown[]).includes(v.status) &&
    Array.isArray(v.depends_on) &&
    v.depends_on.every(isStr) &&
    typeof v.attempts === "number" &&
    isStrOrNull(v.output) &&
    isStrOrNull(v.error) &&
    isStrOrNull(v.started_at) &&
    isStrOrNull(v.finished_at)
  );
}

function isMissionPatch(v: unknown): v is MissionPatch {
  return (
    isObj(v) &&
    isStr(v.id) &&
    (MISSION_STATUSES as readonly unknown[]).includes(v.status) &&
    isStrOrNull(v.result) &&
    isStrOrNull(v.error) &&
    isStr(v.updated_at) &&
    isStrOrNull(v.completed_at)
  );
}

export function isMission(v: unknown): v is Mission {
  return (
    isObj(v) &&
    isMissionPatch({ ...v, updated_at: v.updated_at }) &&
    isStr(v.objective) &&
    isStr(v.created_at) &&
    Array.isArray(v.tasks) &&
    v.tasks.every(isTask)
  );
}

/** Parse one WebSocket frame. Returns null for anything that is not a valid message. */
export function parseStreamMessage(raw: string): StreamMessage | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isObj(data)) return null;
  switch (data.type) {
    case "snapshot":
      return isMission(data.mission) ? { type: "snapshot", mission: data.mission } : null;
    case "task":
      return isStr(data.mission_id) && isTask(data.task)
        ? { type: "task", mission_id: data.mission_id, task: data.task }
        : null;
    case "mission":
      return isMissionPatch(data.mission) ? { type: "mission", mission: data.mission } : null;
    default:
      return null;
  }
}

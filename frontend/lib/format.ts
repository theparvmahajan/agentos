import type { MissionMeta, Task } from "./types";

/** 850 ms, 12.4 s, 2 min 05 s, 1 h 02 min. Negative or missing values read as 0. */
export function formatDuration(ms: number): string {
  const value = Math.max(0, ms);
  if (value < 1000) return `${Math.round(value)} ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
  const totalSeconds = Math.floor(seconds);
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes} min ${String(totalSeconds % 60).padStart(2, "0")} s`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min`;
}

/** How long a task has been running or ran. null if it has not started. */
export function taskDuration(task: Task, now: number): number | null {
  if (!task.started_at) return null;
  const end = task.finished_at ? Date.parse(task.finished_at) : now;
  return end - Date.parse(task.started_at);
}

/** Time from creation to completion (or to now while it is still open). */
export function missionRuntime(mission: MissionMeta, now: number): number {
  const end = mission.completed_at ? Date.parse(mission.completed_at) : now;
  return end - Date.parse(mission.created_at);
}

export function relativeTime(iso: string, now: number): string {
  const seconds = Math.round((now - Date.parse(iso)) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} d ago`;
  return new Date(iso).toLocaleDateString();
}

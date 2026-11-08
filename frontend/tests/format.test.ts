import { describe, expect, it } from "vitest";
import { formatDuration, missionRuntime, relativeTime, taskDuration } from "@/lib/format";
import { makeMission, makeTask } from "./factories";

describe("formatDuration", () => {
  it.each([
    [0, "0 ms"],
    [850, "850 ms"],
    [999, "999 ms"],
    [1000, "1.0 s"],
    [12_400, "12 s"],
    [9_940, "9.9 s"],
    [59_000, "59 s"],
    [60_000, "1 min 00 s"],
    [125_000, "2 min 05 s"],
    [3_599_000, "59 min 59 s"],
    [3_720_000, "1 h 02 min"],
    [-500, "0 ms"],
  ])("%d ms -> %s", (ms, text) => {
    expect(formatDuration(ms)).toBe(text);
  });
});

describe("taskDuration", () => {
  const now = Date.parse("2026-11-05T10:00:30Z");

  it("is null before the task starts", () => {
    expect(taskDuration(makeTask(), now)).toBeNull();
  });

  it("counts up to now while the task is running", () => {
    const task = makeTask({ started_at: "2026-11-05T10:00:20Z" });
    expect(taskDuration(task, now)).toBe(10_000);
  });

  it("stops at the finish time once finished", () => {
    const task = makeTask({ started_at: "2026-11-05T10:00:20Z", finished_at: "2026-11-05T10:00:23Z" });
    expect(taskDuration(task, now)).toBe(3_000);
  });
});

describe("missionRuntime", () => {
  it("runs to now while open and to completion when done", () => {
    const open = makeMission({ created_at: "2026-11-05T10:00:00Z" });
    expect(missionRuntime(open, Date.parse("2026-11-05T10:01:00Z"))).toBe(60_000);
    const done = makeMission({ created_at: "2026-11-05T10:00:00Z", completed_at: "2026-11-05T10:00:42Z" });
    expect(missionRuntime(done, Date.parse("2026-11-05T11:00:00Z"))).toBe(42_000);
  });
});

describe("relativeTime", () => {
  const now = Date.parse("2026-11-05T12:00:00Z");
  it.each([
    ["2026-11-05T11:59:58Z", "just now"],
    ["2026-11-05T11:59:30Z", "30 s ago"],
    ["2026-11-05T11:55:00Z", "5 min ago"],
    ["2026-11-05T09:00:00Z", "3 h ago"],
    ["2026-11-02T12:00:00Z", "3 d ago"],
  ])("%s -> %s", (iso, text) => {
    expect(relativeTime(iso, now)).toBe(text);
  });

  it("falls back to a date after a month", () => {
    expect(relativeTime("2026-08-01T12:00:00Z", now)).toMatch(/\d/);
  });
});

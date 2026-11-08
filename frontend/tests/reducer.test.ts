import { describe, expect, it } from "vitest";
import { applyMessage } from "@/lib/reducer";
import type { MissionState } from "@/lib/reducer";
import type { StreamMessage } from "@/lib/types";
import { makeMission, makeTask } from "./factories";

const MISSION = makeMission();
const snapshot = (tasks = [makeTask({ key: "a" }), makeTask({ key: "b", depends_on: ["a"] })]): StreamMessage => ({
  type: "snapshot",
  mission: { ...MISSION, tasks },
});

function started(): MissionState {
  const state = applyMessage(null, snapshot());
  if (!state) throw new Error("snapshot should produce a state");
  return state;
}

describe("applyMessage", () => {
  it("builds the state from a snapshot, keeping the tasks in the server's order", () => {
    const state = started();
    expect(state.mission.id).toBe(MISSION.id);
    expect("tasks" in state.mission).toBe(false);
    expect(state.tasks.map((t) => t.key)).toEqual(["a", "b"]);
  });

  it("replaces everything on a second snapshot (a resync)", () => {
    const next = applyMessage(started(), snapshot([makeTask({ key: "z", status: "completed" })]));
    expect(next?.tasks.map((t) => t.key)).toEqual(["z"]);
  });

  it("replaces a known task by key without moving it", () => {
    const updated = makeTask({ key: "a", status: "running", started_at: "2026-11-05T10:00:01Z" });
    const state = applyMessage(started(), { type: "task", mission_id: MISSION.id, task: updated });
    expect(state?.tasks.map((t) => [t.key, t.status])).toEqual([
      ["a", "running"],
      ["b", "pending"],
    ]);
  });

  it("adds a task it has not seen as a new node", () => {
    const state = applyMessage(started(), { type: "task", mission_id: MISSION.id, task: makeTask({ key: "c" }) });
    expect(state?.tasks.map((t) => t.key)).toEqual(["a", "b", "c"]);
  });

  it("applying the same task message twice gives the same result", () => {
    const message: StreamMessage = {
      type: "task",
      mission_id: MISSION.id,
      task: makeTask({ key: "a", status: "completed", output: "x" }),
    };
    const once = applyMessage(started(), message);
    expect(applyMessage(once, message)).toEqual(once);
  });

  it("merges a mission patch into the mission and leaves the tasks alone", () => {
    const state = applyMessage(started(), {
      type: "mission",
      mission: {
        id: MISSION.id,
        status: "completed",
        result: "final",
        error: null,
        updated_at: "2026-11-05T10:05:00Z",
        completed_at: "2026-11-05T10:05:00Z",
      },
    });
    expect(state?.mission.status).toBe("completed");
    expect(state?.mission.result).toBe("final");
    expect(state?.mission.objective).toBe(MISSION.objective); // untouched fields survive
    expect(state?.tasks).toHaveLength(2);
  });

  it("ignores task and mission messages that arrive before any snapshot", () => {
    expect(applyMessage(null, { type: "task", mission_id: MISSION.id, task: makeTask() })).toBeNull();
  });

  it("ignores messages about a different mission", () => {
    const before = started();
    expect(applyMessage(before, { type: "task", mission_id: "other", task: makeTask({ key: "a", status: "failed" }) })).toBe(before);
    expect(
      applyMessage(before, {
        type: "mission",
        mission: { id: "other", status: "failed", result: null, error: "x", updated_at: "", completed_at: null },
      }),
    ).toBe(before);
  });

  it("never mutates the previous state", () => {
    const before = started();
    const frozen = JSON.stringify(before);
    applyMessage(before, { type: "task", mission_id: MISSION.id, task: makeTask({ key: "a", status: "failed" }) });
    expect(JSON.stringify(before)).toBe(frozen);
  });
});

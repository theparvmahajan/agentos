import { describe, expect, it } from "vitest";
import { parseStreamMessage } from "@/lib/parse";
import { makeMission, makeTask } from "./factories";

const json = (v: unknown) => JSON.stringify(v);

describe("parseStreamMessage", () => {
  it("accepts a snapshot", () => {
    const mission = makeMission({ tasks: [makeTask({ key: "a" })] });
    expect(parseStreamMessage(json({ type: "snapshot", mission }))).toEqual({ type: "snapshot", mission });
  });

  it("accepts a task message", () => {
    const task = makeTask({ key: "a", status: "running" });
    expect(parseStreamMessage(json({ type: "task", mission_id: "m", task }))).toEqual({
      type: "task",
      mission_id: "m",
      task,
    });
  });

  it("accepts a mission message", () => {
    const { tasks: _tasks, ...patch } = makeMission({ status: "completed", result: "done" });
    void _tasks;
    const message = parseStreamMessage(json({ type: "mission", mission: patch }));
    expect(message?.type).toBe("mission");
  });

  it.each([
    ["not json", "{oops"],
    ["empty", ""],
    ["a number", "42"],
    ["an array", "[]"],
    ["null", "null"],
    ["no type", json({ mission: {} })],
    ["unknown type", json({ type: "ping" })],
    ["snapshot without a mission", json({ type: "snapshot" })],
    ["snapshot with a bad task", json({ type: "snapshot", mission: { ...makeMission(), tasks: [{ key: 1 }] } })],
    ["task with an unknown status", json({ type: "task", mission_id: "m", task: { ...makeTask(), status: "exploded" } })],
    ["task without a mission id", json({ type: "task", task: makeTask() })],
    ["task whose depends_on is not strings", json({ type: "task", mission_id: "m", task: { ...makeTask(), depends_on: [1] } })],
    ["mission with an unknown status", json({ type: "mission", mission: { ...makeMission(), status: "weird" } })],
  ])("drops %s", (_name, raw) => {
    expect(parseStreamMessage(raw)).toBeNull();
  });
});

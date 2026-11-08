import { describe, expect, it } from "vitest";
import {
  MISSION_LABEL,
  MISSION_TONE,
  TASK_LABEL,
  TASK_TONE,
  isMissionFinished,
  isTaskFinished,
} from "@/lib/status";
import { MISSION_STATUSES, TASK_STATUSES } from "@/lib/types";

describe("status", () => {
  it("knows which mission statuses are final", () => {
    expect(MISSION_STATUSES.filter(isMissionFinished)).toEqual(["completed", "failed", "cancelled"]);
  });

  it("knows which task statuses are final", () => {
    expect(TASK_STATUSES.filter(isTaskFinished)).toEqual(["completed", "failed", "skipped", "cancelled"]);
  });

  it("has a label and a tone for every status, so nothing renders blank", () => {
    for (const s of MISSION_STATUSES) {
      expect(MISSION_LABEL[s]).toBeTruthy();
      expect(MISSION_TONE[s]).toBeTruthy();
    }
    for (const s of TASK_STATUSES) {
      expect(TASK_LABEL[s]).toBeTruthy();
      expect(TASK_TONE[s]).toBeTruthy();
    }
  });

  it("gives every task status a distinct label", () => {
    const labels = TASK_STATUSES.map((s) => TASK_LABEL[s]);
    expect(new Set(labels).size).toBe(labels.length);
  });
});

import { describe, expect, it } from "vitest";
import { appendPage, mergeFirstPage } from "@/lib/mergeList";
import { makeSummary } from "./factories";

// ids and times are chosen so that a higher number is newer
const m = (n: number, over = {}) =>
  makeSummary({ id: `id-${String(n).padStart(2, "0")}`, created_at: `2026-11-05T10:${String(n).padStart(2, "0")}:00+00:00`, ...over });
const ids = (list: { id: string }[]) => list.map((x) => x.id);

describe("mergeFirstPage", () => {
  it("updates a mission that is on the fresh first page", () => {
    const merged = mergeFirstPage([m(9), m(8)], [m(9, { status: "completed" }), m(8)]);
    expect(merged[0]?.status).toBe("completed");
  });

  it("puts a brand new mission on top", () => {
    expect(ids(mergeFirstPage([m(9), m(8)], [m(10), m(9)]))).toEqual(["id-10", "id-09", "id-08"]);
  });

  it("keeps older rows the user already loaded", () => {
    const merged = mergeFirstPage([m(9), m(8), m(7), m(6)], [m(9), m(8)]);
    expect(ids(merged)).toEqual(["id-09", "id-08", "id-07", "id-06"]);
  });

  it("drops a row that vanished from the first page but is newer than its last row", () => {
    expect(ids(mergeFirstPage([m(9), m(8), m(7)], [m(8), m(7)]))).toEqual(["id-08", "id-07"]);
  });

  it("does not repeat a mission that appears both on the fresh page and in the old list", () => {
    const merged = mergeFirstPage([m(9), m(8), m(7)], [m(9), m(8)]);
    expect(new Set(ids(merged)).size).toBe(merged.length);
  });

  it("empties the list when the first page is empty", () => {
    expect(mergeFirstPage([m(9)], [])).toEqual([]);
  });

  it("copes with missions created at the same instant by comparing ids", () => {
    const same = "2026-11-05T10:00:00+00:00";
    const a = makeSummary({ id: "b", created_at: same });
    const b = makeSummary({ id: "a", created_at: same });
    expect(ids(mergeFirstPage([a, b], [a]))).toEqual(["b", "a"]);
  });
});

describe("appendPage", () => {
  it("adds a page after the current rows", () => {
    expect(ids(appendPage([m(9), m(8)], [m(7), m(6)]))).toEqual(["id-09", "id-08", "id-07", "id-06"]);
  });

  it("skips anything already shown", () => {
    expect(ids(appendPage([m(9), m(8)], [m(8), m(7)]))).toEqual(["id-09", "id-08", "id-07"]);
  });
});

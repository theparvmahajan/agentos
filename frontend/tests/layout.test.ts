import { describe, expect, it } from "vitest";
import { COLUMN_GAP, NODE_HEIGHT, NODE_WIDTH, ROW_GAP, buildGraph, layoutTasks, levelsOf } from "@/lib/layout";
import { makeTask } from "./factories";

const t = (key: string, ...deps: string[]) => makeTask({ key, depends_on: deps });

const CHAIN = [t("a"), t("b", "a"), t("c", "b")];
const DIAMOND = [t("a"), t("b", "a"), t("c", "a"), t("d", "b", "c")];
const FAN = [t("root"), ...[1, 2, 3, 4, 5].map((i) => t(`w${i}`, "root"))];

describe("levelsOf", () => {
  it("numbers a chain 0, 1, 2", () => {
    expect([...levelsOf(CHAIN)]).toEqual([["a", 0], ["b", 1], ["c", 2]]);
  });

  it("uses the longest path in a diamond", () => {
    expect(levelsOf(DIAMOND).get("d")).toBe(2);
  });

  it("puts a task after its LAST dependency when its dependencies are at different depths", () => {
    // d needs both a (depth 0) and c (depth 2): it must sit after c, never next to a
    const uneven = [t("a"), t("b", "a"), t("c", "b"), t("d", "a", "c")];
    expect(levelsOf(uneven).get("d")).toBe(3);
  });

  it("puts independent tasks all at level 0", () => {
    expect([...levelsOf([t("x"), t("y"), t("z")]).values()]).toEqual([0, 0, 0]);
  });

  it("ignores dependencies on unknown tasks", () => {
    expect(levelsOf([t("a", "ghost")]).get("a")).toBe(0);
  });

  it("does not loop forever on a cycle (which the API never produces)", () => {
    const levels = levelsOf([t("a", "b"), t("b", "a")]);
    expect(levels.size).toBe(2);
  });

  it("handles no tasks", () => {
    expect(levelsOf([]).size).toBe(0);
  });
});

describe("layoutTasks", () => {
  it("places each level in its own column, left to right", () => {
    const p = layoutTasks(CHAIN);
    expect(p.get("a")?.x).toBe(0);
    expect(p.get("b")?.x).toBe(NODE_WIDTH + COLUMN_GAP);
    expect(p.get("c")?.x).toBe(2 * (NODE_WIDTH + COLUMN_GAP));
  });

  it("never draws a task to the left of, or level with, something it depends on", () => {
    const uneven = [t("a"), t("b", "a"), t("c", "b"), t("d", "a", "c")];
    const p = layoutTasks(uneven);
    for (const task of uneven) {
      for (const dep of task.depends_on) {
        expect(p.get(task.key)?.x ?? 0).toBeGreaterThan(p.get(dep)?.x ?? 0);
      }
    }
  });

  it("stacks siblings in a column without overlap", () => {
    const p = layoutTasks(FAN);
    const ys = [1, 2, 3, 4, 5].map((i) => p.get(`w${i}`)?.y ?? NaN);
    ys.slice(1).forEach((y, i) => expect(y - (ys[i] ?? 0)).toBe(NODE_HEIGHT + ROW_GAP));
  });

  it("centres a shorter column against the tallest one", () => {
    const p = layoutTasks(FAN);
    const root = p.get("root")?.y ?? NaN;
    const first = p.get("w1")?.y ?? NaN;
    const last = p.get("w5")?.y ?? NaN;
    expect(root + NODE_HEIGHT / 2).toBeCloseTo((first + last + NODE_HEIGHT) / 2);
  });

  it("never gives two tasks the same position", () => {
    for (const tasks of [CHAIN, DIAMOND, FAN]) {
      const spots = [...layoutTasks(tasks).values()].map((p) => `${p.x},${p.y}`);
      expect(new Set(spots).size).toBe(tasks.length);
    }
  });

  it("orders a column by where the tasks' dependencies sit, to keep edges short", () => {
    // y1 depends on the lower of two parents, y2 on the upper: they must not cross
    const tasks = [t("p1"), t("p2"), t("y2", "p1"), t("y1", "p2")];
    const p = layoutTasks(tasks);
    expect((p.get("y2")?.y ?? 0) < (p.get("y1")?.y ?? 0)).toBe(true);
  });

  it("returns a position for every task", () => {
    expect(layoutTasks(DIAMOND).size).toBe(4);
    expect(layoutTasks([]).size).toBe(0);
  });
});

describe("buildGraph", () => {
  it("makes one node per task and one edge per dependency", () => {
    const { nodes, edges } = buildGraph(DIAMOND, null, 0);
    expect(nodes.map((n) => n.id)).toEqual(["a", "b", "c", "d"]);
    expect(edges.map((e) => e.id).sort()).toEqual(["a->b", "a->c", "b->d", "c->d"]);
  });

  it("marks only the selected task as selected", () => {
    const { nodes } = buildGraph(DIAMOND, "c", 0);
    expect(nodes.filter((n) => n.selected).map((n) => n.id)).toEqual(["c"]);
  });

  it("animates an edge exactly while its target runs", () => {
    const tasks = [t("a"), makeTask({ key: "b", depends_on: ["a"], status: "running" })];
    expect(buildGraph(tasks, null, 0).edges[0]?.animated).toBe(true);
    const done = [t("a"), makeTask({ key: "b", depends_on: ["a"], status: "completed" })];
    expect(buildGraph(done, null, 0).edges[0]?.animated).toBe(false);
  });

  it("styles an edge by what its source did", () => {
    const tasks = [makeTask({ key: "a", status: "completed" }), t("b", "a")];
    expect(buildGraph(tasks, null, 0).edges[0]?.className).toBe("edge-completed");
  });

  it("drops edges to tasks that are not in the graph", () => {
    expect(buildGraph([t("a", "ghost")], null, 0).edges).toEqual([]);
  });

  it("gives every node a fixed size, so React Flow never hides a rebuilt node as unmeasured", () => {
    const { nodes } = buildGraph(CHAIN, null, 0);
    expect(nodes.every((n) => n.width === NODE_WIDTH && n.height === NODE_HEIGHT)).toBe(true);
  });

  it("tells React Flow each node's measured size and where edges attach, so nothing is re-measured", () => {
    const { nodes } = buildGraph(CHAIN, null, 0);
    for (const node of nodes) {
      expect(node.measured).toEqual({ width: NODE_WIDTH, height: NODE_HEIGHT });
      const target = node.handles.find((h) => h.type === "target");
      const source = node.handles.find((h) => h.type === "source");
      expect(target).toMatchObject({ position: "left", x: 0 });
      expect(source).toMatchObject({ position: "right" });
      expect((source?.x ?? 0) + (source?.width ?? 0)).toBe(NODE_WIDTH); // exactly on the right edge
      expect((target?.y ?? 0) + (target?.height ?? 0) / 2).toBe(NODE_HEIGHT / 2); // vertically centred
    }
  });

  it("passes the clock to every node so durations can tick", () => {
    expect(buildGraph(CHAIN, null, 1234).nodes.every((n) => n.data.now === 1234)).toBe(true);
  });
});

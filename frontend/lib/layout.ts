import type { Task } from "./types";

// A small layered (left to right) layout for a DAG. It is tiny on purpose: mission graphs have
// tens of tasks, and a hand-written layout is easy to test and adds no dependency.

export const NODE_WIDTH = 236;
export const NODE_HEIGHT = 96;
export const COLUMN_GAP = 76;
export const ROW_GAP = 26;

export interface Point {
  x: number;
  y: number;
}

/** Longest path from a task with no dependencies; ignores dependencies on unknown keys. */
export function levelsOf(tasks: Task[]): Map<string, number> {
  const byKey = new Map(tasks.map((t) => [t.key, t]));
  const levels = new Map<string, number>();
  const visiting = new Set<string>();

  const level = (key: string): number => {
    const known = levels.get(key);
    if (known !== undefined) return known;
    if (visiting.has(key)) return 0; // a cycle cannot happen (the API rejects them), but never loop
    visiting.add(key);
    const task = byKey.get(key);
    const deps = (task?.depends_on ?? []).filter((d) => byKey.has(d));
    const value = deps.length ? 1 + Math.max(...deps.map(level)) : 0;
    visiting.delete(key);
    levels.set(key, value);
    return value;
  };

  tasks.forEach((t) => level(t.key));
  return levels;
}

/** Top-left position of every task. Columns are the levels; each column is centred vertically,
 * and tasks are ordered by the average row of their dependencies to keep edges short. */
export function layoutTasks(tasks: Task[]): Map<string, Point> {
  const levels = levelsOf(tasks);
  const columns = new Map<number, Task[]>();
  tasks.forEach((t) => {
    const l = levels.get(t.key) ?? 0;
    columns.set(l, [...(columns.get(l) ?? []), t]);
  });

  const row = new Map<string, number>();
  const ordered = [...columns.keys()].sort((a, b) => a - b);
  ordered.forEach((l) => {
    const column = columns.get(l) ?? [];
    const average = (t: Task): number => {
      const rows = t.depends_on.map((d) => row.get(d)).filter((r): r is number => r !== undefined);
      return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Number.POSITIVE_INFINITY;
    };
    const sorted = column
      .map((t, index) => ({ t, index, score: average(t) }))
      .sort((a, b) =>
        a.score === b.score ? a.index - b.index : a.score < b.score ? -1 : 1,
      )
      .map((e) => e.t);
    sorted.forEach((t, i) => row.set(t.key, i));
    columns.set(l, sorted);
  });

  const tallest = Math.max(0, ...[...columns.values()].map((c) => c.length));
  const positions = new Map<string, Point>();
  ordered.forEach((l) => {
    const column = columns.get(l) ?? [];
    const offset = ((tallest - column.length) * (NODE_HEIGHT + ROW_GAP)) / 2;
    column.forEach((t, i) => {
      positions.set(t.key, {
        x: l * (NODE_WIDTH + COLUMN_GAP),
        y: offset + i * (NODE_HEIGHT + ROW_GAP),
      });
    });
  });
  return positions;
}

/** Where an edge attaches to a node, relative to the node's top-left corner. */
export interface GraphHandle {
  type: "source" | "target";
  position: "left" | "right";
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface GraphNode {
  id: string;
  type: "task";
  position: Point;
  /** Nodes have a fixed size, and say so (and where their edges attach) up front. Left to measure
   * itself, React Flow treats every node we rebuild as new: it hides the node and drops its edges
   * until the next measurement, so the graph flickered whenever anything updated. */
  width: number;
  height: number;
  measured: { width: number; height: number };
  handles: GraphHandle[];
  data: { task: Task; now: number };
  selected: boolean;
}

// A 1px handle on the middle of the left edge (where dependencies arrive) and of the right edge
// (where dependants leave): React Flow draws an edge to (x, y + 0.5) on the left, (x + 1, y + 0.5) on the right.
const HANDLES: GraphHandle[] = [
  { type: "target", position: "left", x: 0, y: NODE_HEIGHT / 2 - 0.5, width: 1, height: 1 },
  { type: "source", position: "right", x: NODE_WIDTH - 1, y: NODE_HEIGHT / 2 - 0.5, width: 1, height: 1 },
];

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  animated: boolean;
  className: string;
}

/** The nodes and edges to draw. An edge is animated while its target is running, and takes the
 * look of its source's outcome (completed, failed) so progress reads at a glance. */
export function buildGraph(
  tasks: Task[],
  selectedKey: string | null,
  now: number,
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const positions = layoutTasks(tasks);
  const byKey = new Map(tasks.map((t) => [t.key, t]));

  const nodes: GraphNode[] = tasks.map((task) => ({
    id: task.key,
    type: "task",
    position: positions.get(task.key) ?? { x: 0, y: 0 },
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    measured: { width: NODE_WIDTH, height: NODE_HEIGHT },
    handles: HANDLES,
    data: { task, now },
    selected: task.key === selectedKey,
  }));

  const edges: GraphEdge[] = tasks.flatMap((task) =>
    task.depends_on
      .filter((dep) => byKey.has(dep))
      .map((dep) => {
        const source = byKey.get(dep);
        return {
          id: `${dep}->${task.key}`,
          source: dep,
          target: task.key,
          animated: task.status === "running",
          className: `edge-${source?.status ?? "pending"}`,
        };
      }),
  );

  return { nodes, edges };
}

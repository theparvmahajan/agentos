import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TaskNode from "@/components/TaskNode";
import type { TaskNodeType } from "@/components/TaskNode";
import { makeTask } from "./factories";

// The real Handle needs React Flow's context; its behaviour is not what is tested here.
vi.mock("@xyflow/react", () => ({
  Handle: () => <span data-testid="handle" />,
  Position: { Left: "left", Right: "right" },
}));

const NOW = Date.parse("2026-11-05T10:00:30Z");
type Props = React.ComponentProps<typeof TaskNode>;
const props = (task: ReturnType<typeof makeTask>, selected = false): Props =>
  ({ data: { task, now: NOW }, selected }) as unknown as Props & { data: TaskNodeType["data"] };

describe("TaskNode", () => {
  it("shows the task's key, agent and status", () => {
    render(<TaskNode {...props(makeTask({ key: "research", agent: "researcher", status: "running", started_at: "2026-11-05T10:00:20Z" }))} />);
    expect(screen.getByText("research")).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("10 s")).toBeInTheDocument();
  });

  it("says when the task has not started", () => {
    render(<TaskNode {...props(makeTask())} />);
    expect(screen.getByText("not started")).toBeInTheDocument();
  });

  it("mentions a retry", () => {
    render(<TaskNode {...props(makeTask({ attempts: 2, started_at: "2026-11-05T10:00:20Z" }))} />);
    expect(screen.getByText(/attempt 2/)).toBeInTheDocument();
  });

  it("exposes the status for styling and marks the selected node", () => {
    const { container, rerender } = render(<TaskNode {...props(makeTask({ status: "failed" }))} />);
    expect(container.querySelector(".task-node")).toHaveAttribute("data-status", "failed");
    expect(container.querySelector(".task-node")).not.toHaveAttribute("data-selected");
    rerender(<TaskNode {...props(makeTask({ status: "failed" }), true)} />);
    expect(container.querySelector(".task-node")).toHaveAttribute("data-selected");
  });

  it("has a connection point on each side for the edges", () => {
    render(<TaskNode {...props(makeTask())} />);
    expect(screen.getAllByTestId("handle")).toHaveLength(2);
  });
});

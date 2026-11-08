import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import MissionHeader from "@/components/MissionHeader";
import type { CancelState } from "@/components/MissionHeader";
import { makeMission, makeTask } from "./factories";

const { tasks: _t, ...META } = makeMission({ objective: "compare databases", created_at: "2026-11-05T10:00:00Z" });
void _t;
const NOW = Date.parse("2026-11-05T10:01:05Z");

function setup(over: { status?: "running" | "completed" | "failed" | "cancelled" | "planning"; cancel?: CancelState; tasks?: ReturnType<typeof makeTask>[] } = {}) {
  const handlers = { onAskCancel: vi.fn(), onConfirmCancel: vi.fn(), onDismissCancel: vi.fn() };
  render(
    <MissionHeader
      mission={{ ...META, status: over.status ?? "running", completed_at: over.status === "completed" ? "2026-11-05T10:00:42Z" : null }}
      tasks={over.tasks ?? [makeTask({ key: "a", status: "completed" }), makeTask({ key: "b" })]}
      connection="live"
      now={NOW}
      cancel={over.cancel ?? { phase: "idle", error: null }}
      {...handlers}
    />,
  );
  return handlers;
}

describe("MissionHeader", () => {
  it("shows the objective, the status and that it is live", () => {
    setup();
    expect(screen.getByRole("heading", { name: "compare databases" })).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("shows how long it has been running and how many tasks are done", () => {
    setup();
    expect(screen.getByText("Running for")).toBeInTheDocument();
    expect(screen.getByText("1 min 05 s")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 done")).toBeInTheDocument();
  });

  it("shows the total time once finished", () => {
    setup({ status: "completed" });
    expect(screen.getByText("Took")).toBeInTheDocument();
    expect(screen.getByText("42 s")).toBeInTheDocument();
  });

  it("says when there are no tasks yet", () => {
    setup({ status: "planning", tasks: [] });
    expect(screen.getByText("none yet")).toBeInTheDocument();
  });

  it("offers to cancel a mission that is still open", async () => {
    const h = setup();
    await userEvent.click(screen.getByRole("button", { name: /cancel mission/i }));
    expect(h.onAskCancel).toHaveBeenCalled();
  });

  it.each(["completed", "failed", "cancelled"] as const)("does not offer cancelling a %s mission", (status) => {
    setup({ status });
    expect(screen.queryByRole("button", { name: /cancel/i })).not.toBeInTheDocument();
  });

  it("asks for confirmation and explains what cancelling does", async () => {
    const h = setup({ cancel: { phase: "confirming", error: null } });
    expect(screen.getByText(/tasks already running finish, but their results are discarded/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /yes, cancel it/i }));
    expect(h.onConfirmCancel).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /keep running/i }));
    expect(h.onDismissCancel).toHaveBeenCalled();
  });

  it("locks the buttons while cancelling", () => {
    setup({ cancel: { phase: "working", error: null } });
    expect(screen.getByRole("button", { name: /cancelling/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /keep running/i })).toBeDisabled();
  });

  it("shows a cancel error", () => {
    setup({ cancel: { phase: "idle", error: "That mission had already finished." } });
    expect(screen.getByRole("alert")).toHaveTextContent("already finished");
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MissionView from "@/components/MissionView";
import { ApiError, cancelMission } from "@/lib/api";
import type { MissionState } from "@/lib/reducer";
import type { Connection } from "@/lib/stream";
import type { Mission, Task } from "@/lib/types";
import { makeMission, makeTask } from "./factories";

interface HookValue {
  state: MissionState | null;
  connection: Connection;
  applyMission: (mission: Mission) => void;
}
const { hook } = vi.hoisted(() => ({ hook: { value: null as unknown as HookValue } }));

vi.mock("@/lib/useMissionStream", () => ({ useMissionStream: () => hook.value }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  cancelMission: vi.fn(),
}));
// The graph needs a real browser; stand in for it with one button per task.
vi.mock("@/components/GraphLoader", () => ({
  default: ({ tasks, onSelect }: { tasks: Task[]; onSelect: (key: string | null) => void }) => (
    <div data-testid="graph">
      {tasks.map((t) => (
        <button key={t.key} onClick={() => onSelect(t.key)}>
          {`node ${t.key}`}
        </button>
      ))}
      <button onClick={() => onSelect(null)}>background</button>
    </div>
  ),
}));

const cancel = vi.mocked(cancelMission);
const ID = "11111111-1111-4111-8111-111111111111";

function state(over: Partial<Mission> = {}, tasks: Task[] = []): MissionState {
  const { tasks: _ignored, ...meta } = makeMission({ id: ID, ...over });
  void _ignored;
  return { mission: meta, tasks };
}

function show(value: Partial<HookValue>) {
  hook.value = { state: null, connection: "live", applyMission: vi.fn(), ...value };
  return { ...render(<MissionView id={ID} />), applyMission: hook.value.applyMission };
}

beforeEach(() => {
  cancel.mockReset();
  document.title = "";
});

describe("MissionView: before there is anything to show", () => {
  it("says it is connecting", () => {
    show({ state: null, connection: "connecting" });
    expect(screen.getByRole("status")).toHaveTextContent(/connecting to the mission/i);
  });

  it("says it is retrying when the server cannot be reached", () => {
    show({ state: null, connection: "reconnecting" });
    expect(screen.getByRole("status")).toHaveTextContent(/cannot reach the server/i);
  });

  it("explains an unknown mission and links back", () => {
    show({ state: null, connection: "not-found" });
    expect(screen.getByRole("heading", { name: /mission not found/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to missions/i })).toHaveAttribute("href", "/");
  });

  it("explains an invalid id", () => {
    show({ state: null, connection: "invalid" });
    expect(screen.getByRole("heading", { name: /invalid mission id/i })).toBeInTheDocument();
  });
});

describe("MissionView: a mission", () => {
  const tasks = [makeTask({ key: "a", status: "completed", output: "A-out" }), makeTask({ key: "b", depends_on: ["a"], instruction: "do b" })];

  it("shows the header, the graph and a hint", () => {
    show({ state: state({}, tasks) });
    expect(screen.getByRole("heading", { name: "compare databases" })).toBeInTheDocument();
    expect(screen.getByTestId("graph")).toBeInTheDocument();
    expect(screen.getByText(/click a task in the graph/i)).toBeInTheDocument();
  });

  it("puts the objective in the page title", () => {
    show({ state: state({ objective: "count primes" }, tasks) });
    expect(document.title).toBe("count primes · AgentOS");
  });

  it("shows the planner message while there are no tasks", () => {
    show({ state: state({ status: "planning" }, []) });
    expect(screen.getByText(/planner is breaking the objective/i)).toBeInTheDocument();
    expect(screen.queryByTestId("graph")).not.toBeInTheDocument();
  });

  it("says a failed mission has no tasks, instead of promising a plan", () => {
    show({ state: state({ status: "failed", error: "planning failed" }, []) });
    expect(screen.getByText(/this mission has no tasks/i)).toBeInTheDocument();
    expect(screen.getByText("planning failed")).toBeInTheDocument();
  });

  it("opens a task's details when it is selected, and closes them again", async () => {
    show({ state: state({}, tasks) });
    await userEvent.click(screen.getByRole("button", { name: "node a" }));
    expect(screen.getByRole("region", { name: "Task a" })).toBeInTheDocument();
    expect(screen.getByText("A-out")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /close task details/i }));
    expect(screen.queryByRole("region", { name: "Task a" })).not.toBeInTheDocument();
  });

  it("closes the details when the graph background is clicked", async () => {
    show({ state: state({}, tasks) });
    await userEvent.click(screen.getByRole("button", { name: "node b" }));
    expect(screen.getByRole("region", { name: "Task b" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "background" }));
    expect(screen.queryByRole("region", { name: "Task b" })).not.toBeInTheDocument();
  });

  it("shows the result once the mission completes", () => {
    show({ state: state({ status: "completed", result: "the final answer", completed_at: "2026-11-05T10:01:00Z" }, tasks) });
    expect(screen.getByRole("region", { name: "Result" })).toHaveTextContent("the final answer");
  });
});

describe("MissionView: cancelling", () => {
  const open = state({}, [makeTask({ key: "a", status: "running" })]);

  async function startCancel() {
    const view = show({ state: open });
    await userEvent.click(screen.getByRole("button", { name: /cancel mission/i }));
    return view;
  }

  it("asks first, and 'keep running' backs out without calling the API", async () => {
    await startCancel();
    expect(screen.getByRole("button", { name: /yes, cancel it/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /keep running/i }));
    expect(screen.getByRole("button", { name: /cancel mission/i })).toBeInTheDocument();
    expect(cancel).not.toHaveBeenCalled();
  });

  it("cancels this mission and shows the cancelled state at once", async () => {
    const cancelled = makeMission({ id: ID, status: "cancelled" });
    cancel.mockResolvedValue(cancelled);
    const { applyMission } = await startCancel();

    await userEvent.click(screen.getByRole("button", { name: /yes, cancel it/i }));

    await waitFor(() => expect(applyMission).toHaveBeenCalledWith(cancelled));
    expect(cancel).toHaveBeenCalledWith(ID);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains that a mission which had just finished cannot be cancelled", async () => {
    cancel.mockRejectedValue(new ApiError(409, "mission already completed"));
    await startCancel();
    await userEvent.click(screen.getByRole("button", { name: /yes, cancel it/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already finished/i);
  });

  it("shows the API's message for other errors", async () => {
    cancel.mockRejectedValue(new ApiError(0, "Cannot reach the API at http://localhost:8000. Is it running?"));
    await startCancel();
    await userEvent.click(screen.getByRole("button", { name: /yes, cancel it/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the API");
  });

  it("shows a generic message for an unexpected failure", async () => {
    cancel.mockRejectedValue(new Error("boom"));
    await startCancel();
    await userEvent.click(screen.getByRole("button", { name: /yes, cancel it/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not cancel/i);
  });
});

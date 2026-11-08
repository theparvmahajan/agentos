import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MissionList from "@/components/MissionList";
import { ApiError, listMissions } from "@/lib/api";
import type { MissionPage } from "@/lib/types";
import { makeSummary } from "./factories";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  listMissions: vi.fn(),
}));

const list = vi.mocked(listMissions);
const page = (items = [makeSummary()], next_cursor: string | null = null): MissionPage => ({ items, next_cursor });
const row = (id: string, objective: string, over = {}) => makeSummary({ id, objective, ...over });

beforeEach(() => {
  list.mockReset();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("MissionList", () => {
  it("shows a loading state, then the missions with status and progress", async () => {
    list.mockResolvedValue(page([row("m1", "compare databases", { status: "running", tasks_total: 3, tasks_completed: 1 })]));
    render(<MissionList />);
    expect(screen.getByText(/loading missions/i)).toBeInTheDocument();

    const link = await screen.findByRole("link", { name: /compare databases/i });
    expect(link).toHaveAttribute("href", "/missions/m1");
    expect(within(link).getByText("Running")).toBeInTheDocument();
    expect(within(link).getByText(/1\/3 tasks/)).toBeInTheDocument();
    expect(within(link).getByRole("progressbar")).toHaveAttribute("value", "1");
  });

  it("says when a mission has no tasks yet", async () => {
    list.mockResolvedValue(page([row("m1", "x", { status: "planning", tasks_total: 0, tasks_completed: 0 })]));
    render(<MissionList />);
    expect(await screen.findByText(/no tasks yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("explains an empty list", async () => {
    list.mockResolvedValue(page([]));
    render(<MissionList />);
    expect(await screen.findByText(/no missions yet/i)).toBeInTheDocument();
  });

  it("shows an error with a way to retry", async () => {
    list.mockRejectedValueOnce(new ApiError(0, "Cannot reach the API"));
    list.mockResolvedValueOnce(page([row("m1", "recovered")]));
    render(<MissionList />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the API");

    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByText("recovered")).toBeInTheDocument();
  });

  it("filters by status and asks the API for it", async () => {
    list.mockResolvedValue(page([row("m1", "everything")]));
    render(<MissionList />);
    await screen.findByText("everything");

    list.mockResolvedValue(page([]));
    await userEvent.selectOptions(screen.getByRole("combobox"), "failed");
    expect(await screen.findByText(/no missions with that status/i)).toBeInTheDocument();
    expect(list).toHaveBeenLastCalledWith({ limit: 20, status: "failed" });
  });

  it("loads the next page when asked and hides the button at the end", async () => {
    list.mockResolvedValueOnce(page([row("m1", "first")], "cursor-1"));
    render(<MissionList />);
    await screen.findByText("first");

    list.mockResolvedValueOnce(page([row("m2", "second")], null));
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));
    expect(await screen.findByText("second")).toBeInTheDocument();
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(list).toHaveBeenLastCalledWith({ limit: 20, cursor: "cursor-1", status: null });
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("does not show a mission twice if a page overlaps", async () => {
    list.mockResolvedValueOnce(page([row("m1", "first")], "c"));
    render(<MissionList />);
    await screen.findByText("first");
    list.mockResolvedValueOnce(page([row("m1", "first"), row("m2", "second")]));
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));
    await screen.findByText("second");
    expect(screen.getAllByText("first")).toHaveLength(1);
  });

  it("refreshes on its own: statuses change and new missions appear on top", async () => {
    list.mockResolvedValueOnce(page([row("m1", "older", { status: "running" })]));
    render(<MissionList refreshMs={20} />);
    await screen.findByText("older");

    list.mockResolvedValue(page([row("m2", "newer", { status: "pending" }), row("m1", "older", { status: "completed" })]));
    await waitFor(() => expect(screen.getByText("newer")).toBeInTheDocument());
    const links = screen.getAllByRole("link");
    expect(links[0]).toHaveTextContent("newer");
    expect(within(links[1]!).getByText("Completed")).toBeInTheDocument();
  });

  it("keeps showing the last known state when a refresh fails", async () => {
    list.mockResolvedValueOnce(page([row("m1", "still here")]));
    render(<MissionList refreshMs={20} />);
    await screen.findByText("still here");

    list.mockRejectedValue(new ApiError(0, "down"));
    expect(await screen.findByText(/could not refresh/i)).toBeInTheDocument();
    expect(screen.getByText("still here")).toBeInTheDocument();
  });

  it("does not refresh while the tab is hidden", async () => {
    list.mockResolvedValue(page([row("m1", "x")]));
    render(<MissionList refreshMs={15} />);
    await screen.findByText("x");
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const calls = list.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });
    expect(list.mock.calls.length).toBe(calls);
  });
});

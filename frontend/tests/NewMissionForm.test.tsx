import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NewMissionForm from "@/components/NewMissionForm";
import { ApiError, createMission } from "@/lib/api";
import { makeMission } from "./factories";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  createMission: vi.fn(),
}));

const create = vi.mocked(createMission);
const box = () => screen.getByLabelText(/what should the agents do/i);
const run = () => screen.getByRole("button", { name: /run mission|starting/i });

beforeEach(() => {
  push.mockReset();
  create.mockReset();
});

describe("NewMissionForm", () => {
  it("cannot run an empty or blank objective", async () => {
    render(<NewMissionForm />);
    expect(run()).toBeDisabled();
    await userEvent.type(box(), "   ");
    expect(run()).toBeDisabled();
  });

  it("fills the box from an example", async () => {
    render(<NewMissionForm />);
    await userEvent.click(screen.getByRole("button", { name: /compare postgresql and mysql/i }));
    expect(box()).toHaveValue("Compare PostgreSQL and MySQL for a small startup");
    expect(run()).toBeEnabled();
  });

  it("creates the mission with the trimmed text and opens it", async () => {
    create.mockResolvedValue(makeMission({ id: "abc" }));
    render(<NewMissionForm />);
    await userEvent.type(box(), "  count primes  ");
    await userEvent.click(run());
    await waitFor(() => expect(push).toHaveBeenCalledWith("/missions/abc"));
    expect(create).toHaveBeenCalledWith("count primes");
  });

  it("runs with Ctrl+Enter", async () => {
    create.mockResolvedValue(makeMission({ id: "k" }));
    render(<NewMissionForm />);
    await userEvent.type(box(), "go{Control>}{Enter}{/Control}");
    await waitFor(() => expect(create).toHaveBeenCalledWith("go"));
  });

  it("shows that it is working and blocks a second submit", async () => {
    let release: (m: ReturnType<typeof makeMission>) => void = () => {};
    create.mockReturnValue(new Promise((resolve) => (release = resolve)));
    render(<NewMissionForm />);
    await userEvent.type(box(), "go");
    await userEvent.click(run());
    expect(screen.getByRole("button", { name: /starting/i })).toBeDisabled();
    expect(box()).toBeDisabled();
    release(makeMission({ id: "z" }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(create).toHaveBeenCalledTimes(1);
  });

  it("shows the API's message and lets the user try again", async () => {
    create.mockRejectedValueOnce(new ApiError(0, "Cannot reach the API at http://localhost:8000. Is it running?"));
    render(<NewMissionForm />);
    await userEvent.type(box(), "go");
    await userEvent.click(run());
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the API");
    expect(run()).toBeEnabled();
    expect(push).not.toHaveBeenCalled();
  });

  it("shows a generic message for an unexpected error", async () => {
    create.mockRejectedValueOnce(new Error("boom"));
    render(<NewMissionForm />);
    await userEvent.type(box(), "go");
    await userEvent.click(run());
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
  });

  it("limits the objective to what the API accepts", () => {
    render(<NewMissionForm />);
    expect(box()).toHaveAttribute("maxlength", "2000");
  });
});

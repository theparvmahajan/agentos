import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import TaskPanel, { OUTPUT_PREVIEW_CHARS } from "@/components/TaskPanel";
import { makeTask } from "./factories";

const NOW = Date.parse("2026-11-05T10:00:30Z");
const show = (over = {}, onClose = vi.fn()) =>
  render(<TaskPanel task={makeTask({ key: "research", agent: "researcher", ...over })} now={NOW} onClose={onClose} />);

describe("TaskPanel", () => {
  it("shows the task's details", () => {
    show({
      status: "completed",
      attempts: 2,
      depends_on: ["a", "b"],
      instruction: "find the thing",
      started_at: "2026-11-05T10:00:20Z",
      finished_at: "2026-11-05T10:00:23Z",
      output: "the answer",
    });
    expect(screen.getByRole("heading", { name: "research" })).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("a, b")).toBeInTheDocument();
    expect(screen.getByText("3.0 s")).toBeInTheDocument();
    expect(screen.getByText("find the thing")).toBeInTheDocument();
    expect(screen.getByText("the answer")).toBeInTheDocument();
  });

  it("lists what the task waits for in a stable order", () => {
    show({ depends_on: ["zeta", "alpha", "mid"] });
    expect(screen.getByText("alpha, mid, zeta")).toBeInTheDocument();
  });

  it("says when a task has not started or has no output", () => {
    show();
    expect(screen.getByText("not started")).toBeInTheDocument();
    expect(screen.getByText("No output yet.")).toBeInTheDocument();
    expect(screen.getByText("nothing")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^copy$/i })).not.toBeInTheDocument();
  });

  it("shows an error separately from the output", () => {
    show({ status: "failed", error: "AgentError: no final answer after 6 steps" });
    expect(screen.getByText(/no final answer after 6 steps/)).toBeInTheDocument();
  });

  it("closes", async () => {
    const onClose = vi.fn();
    show({}, onClose);
    await userEvent.click(screen.getByRole("button", { name: /close task details/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("shows model-written text as plain text and never as HTML", () => {
    const hostile = '<img src=x onerror="window.__pwned = 1"><script>window.__pwned = 2</script><b>bold</b>';
    const { container } = show({ output: hostile, instruction: hostile, error: hostile, status: "failed" });
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getAllByText(hostile)).toHaveLength(3); // instruction, error and output, each as text
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("previews a very long output and copies all of it", async () => {
    const user = userEvent.setup();
    const long = "x".repeat(OUTPUT_PREVIEW_CHARS + 500);
    const { container } = show({ output: long, status: "completed" });

    expect(container.querySelector("pre.output:not(.output--error)")?.textContent).toHaveLength(OUTPUT_PREVIEW_CHARS);
    expect(screen.getByText(/showing the first 20,000 of 20,500 characters/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^copy$/i }));
    expect(await navigator.clipboard.readText()).toBe(long);
    expect(await screen.findByRole("button", { name: /copied/i })).toBeInTheDocument();
  });

  it("does not claim a preview for a short output", () => {
    show({ output: "short", status: "completed" });
    expect(screen.queryByText(/showing the first/i)).not.toBeInTheDocument();
  });
});

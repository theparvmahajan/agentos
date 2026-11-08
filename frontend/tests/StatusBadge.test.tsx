import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import StatusBadge from "@/components/StatusBadge";
import { MISSION_STATUSES, TASK_STATUSES } from "@/lib/types";

describe("StatusBadge", () => {
  it("shows the status as text, not only as a colour", () => {
    render(<StatusBadge kind="task" status="ready" />);
    expect(screen.getByText("Queued")).toBeInTheDocument();
  });

  it("uses the mission wording for missions", () => {
    render(<StatusBadge kind="mission" status="planning" />);
    expect(screen.getByText("Planning")).toBeInTheDocument();
  });

  it("exposes the status and tone for styling", () => {
    const { container } = render(<StatusBadge kind="task" status="failed" />);
    const badge = container.querySelector(".badge");
    expect(badge).toHaveAttribute("data-status", "failed");
    expect(badge).toHaveAttribute("data-tone", "danger");
  });

  it("renders every status of both kinds without throwing", () => {
    for (const status of MISSION_STATUSES) render(<StatusBadge kind="mission" status={status} />);
    for (const status of TASK_STATUSES) render(<StatusBadge kind="task" status={status} />);
  });
});

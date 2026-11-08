import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ResultCard from "@/components/ResultCard";
import { makeMission } from "./factories";

const { tasks: _t, ...base } = makeMission();
void _t;

describe("ResultCard", () => {
  it("shows the result of a completed mission as text", () => {
    const { container } = render(<ResultCard mission={{ ...base, status: "completed", result: "<b>final</b> answer" }} />);
    expect(screen.getByRole("heading", { name: "Result" })).toBeInTheDocument();
    expect(screen.getByText("<b>final</b> answer")).toBeInTheDocument();
    expect(container.querySelector("b")).toBeNull();
  });

  it("shows why a mission failed", () => {
    render(<ResultCard mission={{ ...base, status: "failed", error: "planning failed: bad JSON" }} />);
    expect(screen.getByRole("heading", { name: /mission failed/i })).toBeInTheDocument();
    expect(screen.getByText("planning failed: bad JSON")).toBeInTheDocument();
  });

  it("copes with a failure that has no recorded error", () => {
    render(<ResultCard mission={{ ...base, status: "failed", error: null }} />);
    expect(screen.getByText(/no error was recorded/i)).toBeInTheDocument();
  });

  it("explains a cancelled mission", () => {
    render(<ResultCard mission={{ ...base, status: "cancelled" }} />);
    expect(screen.getByRole("heading", { name: /mission cancelled/i })).toBeInTheDocument();
  });

  it.each(["pending", "planning", "running"] as const)("shows nothing while the mission is %s", (status) => {
    const { container } = render(<ResultCard mission={{ ...base, status }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing for a completed mission that has no result", () => {
    const { container } = render(<ResultCard mission={{ ...base, status: "completed", result: null }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

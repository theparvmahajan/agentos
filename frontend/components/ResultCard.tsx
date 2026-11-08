import type { MissionMeta } from "@/lib/types";
import CopyButton from "./CopyButton";

/** The mission's outcome, once it has one. Text only, like everything model-written. */
export default function ResultCard({ mission }: { mission: MissionMeta }) {
  if (mission.status === "completed" && mission.result) {
    return (
      <section className="card" aria-label="Result">
        <div className="section-head">
          <h2 className="card__title">Result</h2>
          <CopyButton text={mission.result} />
        </div>
        <pre className="output output--result">{mission.result}</pre>
      </section>
    );
  }
  if (mission.status === "failed") {
    return (
      <section className="card" aria-label="Result">
        <h2 className="card__title">Mission failed</h2>
        <pre className="output output--error">{mission.error ?? "No error was recorded."}</pre>
      </section>
    );
  }
  if (mission.status === "cancelled") {
    return (
      <section className="card" aria-label="Result">
        <h2 className="card__title">Mission cancelled</h2>
        <p className="muted">It was stopped before it finished. Tasks that had already completed keep their output.</p>
      </section>
    );
  }
  return null;
}

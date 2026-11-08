import { formatDuration, taskDuration } from "@/lib/format";
import type { Task } from "@/lib/types";
import CopyButton from "./CopyButton";
import StatusBadge from "./StatusBadge";

// A long output makes a very large page; show the start and copy gives the whole thing.
export const OUTPUT_PREVIEW_CHARS = 20000;

interface Props {
  task: Task;
  now: number;
  onClose: () => void;
}

/**
 * Details of one task. The instruction, output and error are text written by language models
 * or taken from web pages, so they are shown ONLY as text: never as HTML.
 */
export default function TaskPanel({ task, now, onClose }: Props) {
  const duration = taskDuration(task, now);
  const output = task.output ?? "";
  const clipped = output.length > OUTPUT_PREVIEW_CHARS;

  return (
    <section className="card task-panel" aria-label={`Task ${task.key}`}>
      <div className="card__header">
        <h2 className="card__title">{task.key}</h2>
        <div className="row">
          <StatusBadge kind="task" status={task.status} />
          <button type="button" className="button button--small" onClick={onClose} aria-label="Close task details">
            Close
          </button>
        </div>
      </div>

      <dl className="facts facts--stacked">
        <div>
          <dt>Agent</dt>
          <dd>{task.agent}</dd>
        </div>
        <div>
          <dt>Attempts</dt>
          <dd>{task.attempts}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{duration !== null ? formatDuration(duration) : "not started"}</dd>
        </div>
        <div>
          <dt>Waits for</dt>
          <dd>{task.depends_on.length ? [...task.depends_on].sort().join(", ") : "nothing"}</dd>
        </div>
      </dl>

      <h3 className="section-title">Instruction</h3>
      <p className="prewrap">{task.instruction}</p>

      {task.error && (
        <>
          <h3 className="section-title">Error</h3>
          <pre className="output output--error">{task.error}</pre>
        </>
      )}

      <div className="section-head">
        <h3 className="section-title">Output</h3>
        {output && <CopyButton text={output} />}
      </div>
      {output ? (
        <>
          <pre className="output">{clipped ? output.slice(0, OUTPUT_PREVIEW_CHARS) : output}</pre>
          {clipped && (
            <p className="muted small">
              Showing the first {OUTPUT_PREVIEW_CHARS.toLocaleString()} of {output.length.toLocaleString()} characters. Copy
              gets all of it.
            </p>
          )}
        </>
      ) : (
        <p className="muted">{task.status === "completed" ? "No output." : "No output yet."}</p>
      )}
    </section>
  );
}

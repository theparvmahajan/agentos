import { formatDuration, missionRuntime, relativeTime } from "@/lib/format";
import { isMissionFinished } from "@/lib/status";
import type { MissionMeta, Task } from "@/lib/types";
import type { Connection } from "@/lib/stream";
import ConnectionIndicator from "./ConnectionIndicator";
import StatusBadge from "./StatusBadge";

export type CancelState = { phase: "idle" | "confirming" | "working"; error: string | null };

interface Props {
  mission: MissionMeta;
  tasks: Task[];
  connection: Connection;
  now: number;
  cancel: CancelState;
  onAskCancel: () => void;
  onConfirmCancel: () => void;
  onDismissCancel: () => void;
}

export default function MissionHeader({
  mission,
  tasks,
  connection,
  now,
  cancel,
  onAskCancel,
  onConfirmCancel,
  onDismissCancel,
}: Props) {
  const finished = isMissionFinished(mission.status);
  const done = tasks.filter((t) => t.status === "completed").length;

  return (
    <header className="mission-header card">
      <div className="mission-header__top">
        <h1 className="mission-header__objective">{mission.objective}</h1>
        <div className="mission-header__badges">
          <StatusBadge kind="mission" status={mission.status} />
          <ConnectionIndicator connection={connection} />
        </div>
      </div>

      <div className="mission-header__bottom">
        <dl className="facts">
          <div>
            <dt>{finished ? "Took" : "Running for"}</dt>
            <dd>{formatDuration(missionRuntime(mission, now))}</dd>
          </div>
          <div>
            <dt>Tasks</dt>
            <dd>{tasks.length ? `${done} of ${tasks.length} done` : "none yet"}</dd>
          </div>
          <div>
            <dt>Started</dt>
            <dd>{relativeTime(mission.created_at, now)}</dd>
          </div>
        </dl>

        {!finished && (
          <div className="mission-header__cancel">
            {cancel.phase === "idle" && (
              <button type="button" className="button button--danger" onClick={onAskCancel}>
                Cancel mission
              </button>
            )}
            {cancel.phase !== "idle" && (
              <div className="confirm" role="group" aria-label="Confirm cancelling">
                <span>Stop this mission? Tasks already running finish, but their results are discarded.</span>
                <button
                  type="button"
                  className="button button--danger"
                  onClick={onConfirmCancel}
                  disabled={cancel.phase === "working"}
                >
                  {cancel.phase === "working" ? "Cancelling…" : "Yes, cancel it"}
                </button>
                <button type="button" className="button" onClick={onDismissCancel} disabled={cancel.phase === "working"}>
                  Keep running
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {cancel.error && (
        <p className="alert" role="alert">
          {cancel.error}
        </p>
      )}
    </header>
  );
}

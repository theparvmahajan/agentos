import { MISSION_LABEL, MISSION_TONE, TASK_LABEL, TASK_TONE } from "@/lib/status";
import type { MissionStatus, TaskStatus } from "@/lib/types";

type Props =
  | { kind: "mission"; status: MissionStatus }
  | { kind: "task"; status: TaskStatus };

/** A status as text plus a colour dot: colour alone is never the signal. */
export default function StatusBadge(props: Props) {
  const label = props.kind === "mission" ? MISSION_LABEL[props.status] : TASK_LABEL[props.status];
  const tone = props.kind === "mission" ? MISSION_TONE[props.status] : TASK_TONE[props.status];
  return (
    <span className="badge" data-tone={tone} data-status={props.status}>
      <span className="badge__dot" aria-hidden="true" />
      {label}
    </span>
  );
}

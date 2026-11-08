import { Handle, Position } from "@xyflow/react";
import type { Node, NodeProps } from "@xyflow/react";
import { formatDuration, taskDuration } from "@/lib/format";
import { TASK_TONE } from "@/lib/status";
import type { Task } from "@/lib/types";
import StatusBadge from "./StatusBadge";

export type TaskNodeType = Node<{ task: Task; now: number }, "task">;

/** One task in the graph. */
export default function TaskNode({ data, selected }: NodeProps<TaskNodeType>) {
  const { task, now } = data;
  const duration = taskDuration(task, now);
  return (
    <div className="task-node" data-tone={TASK_TONE[task.status]} data-status={task.status} data-selected={selected || undefined}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="task-node__top">
        <strong className="task-node__key">{task.key}</strong>
        <StatusBadge kind="task" status={task.status} />
      </div>
      <div className="task-node__agent">{task.agent}</div>
      <div className="task-node__meta">
        {duration !== null ? formatDuration(duration) : "not started"}
        {task.attempts > 1 ? ` · attempt ${task.attempts}` : ""}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

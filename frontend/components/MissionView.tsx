"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, cancelMission } from "@/lib/api";
import { isMissionFinished } from "@/lib/status";
import { useMissionStream } from "@/lib/useMissionStream";
import { useNow } from "@/lib/useNow";
import GraphLoader from "./GraphLoader";
import MissionHeader from "./MissionHeader";
import type { CancelState } from "./MissionHeader";
import ResultCard from "./ResultCard";
import TaskPanel from "./TaskPanel";

function Notice({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card center-block">
      <h1 className="card__title">{title}</h1>
      <p className="muted">{children}</p>
      <Link href="/" className="button">
        Back to missions
      </Link>
    </div>
  );
}

/** One mission, followed live. Render it with `key={id}` so a different id starts from scratch. */
export default function MissionView({ id }: { id: string }) {
  const { state, connection, applyMission } = useMissionStream(id);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [cancel, setCancel] = useState<CancelState>({ phase: "idle", error: null });

  const open = !state || !isMissionFinished(state.mission.status);
  const now = useNow(1000, open); // only ticks while there is something to time

  const objective = state?.mission.objective;
  useEffect(() => {
    if (objective) document.title = `${objective.slice(0, 60)} · AgentOS`;
  }, [objective]);

  async function confirmCancel() {
    setCancel({ phase: "working", error: null });
    try {
      applyMission(await cancelMission(id)); // show the cancelled state at once; the stream agrees
      setCancel({ phase: "idle", error: null });
    } catch (e) {
      const text =
        e instanceof ApiError && e.status === 409
          ? "That mission had already finished, so there was nothing to cancel."
          : e instanceof ApiError
            ? e.message
            : "Could not cancel the mission. Please try again.";
      setCancel({ phase: "idle", error: text });
    }
  }

  if (connection === "not-found") {
    return <Notice title="Mission not found">There is no mission with that id. It may have been deleted.</Notice>;
  }
  if (connection === "invalid") {
    return <Notice title="Invalid mission id">That does not look like a mission id.</Notice>;
  }
  if (!state) {
    return (
      <div className="card" role="status">
        {connection === "reconnecting" ? "Cannot reach the server. Retrying…" : "Connecting to the mission…"}
      </div>
    );
  }

  const selected = state.tasks.find((t) => t.key === selectedKey) ?? null;
  const planning = state.tasks.length === 0;

  return (
    <div className="mission-page">
      <Link href="/" className="back-link">
        ← All missions
      </Link>

      <MissionHeader
        mission={state.mission}
        tasks={state.tasks}
        connection={connection}
        now={now}
        cancel={cancel}
        onAskCancel={() => setCancel({ phase: "confirming", error: null })}
        onConfirmCancel={confirmCancel}
        onDismissCancel={() => setCancel({ phase: "idle", error: null })}
      />

      <div className="mission-body">
        <section className="card graph-card" aria-label="Task graph">
          {planning ? (
            <div className="graph-placeholder" role="status">
              {state.mission.status === "failed" || state.mission.status === "cancelled"
                ? "This mission has no tasks."
                : "The planner is breaking the objective into tasks…"}
            </div>
          ) : (
            <GraphLoader tasks={state.tasks} selectedKey={selectedKey} now={now} onSelect={setSelectedKey} />
          )}
        </section>

        <aside className="side">
          <ResultCard mission={state.mission} />
          {selected ? (
            <TaskPanel task={selected} now={now} onClose={() => setSelectedKey(null)} />
          ) : (
            !planning && (
              <p className="muted hint">Click a task in the graph to see its instruction, output and timing.</p>
            )
          )}
        </aside>
      </div>
    </div>
  );
}

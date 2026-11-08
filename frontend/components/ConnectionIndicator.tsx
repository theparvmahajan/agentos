import type { Connection } from "@/lib/stream";

const TEXT: Record<Connection, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  finished: "Finished",
  "not-found": "Not found",
  invalid: "Invalid id",
};

const TONE: Record<Connection, string> = {
  connecting: "info",
  live: "success",
  reconnecting: "active",
  finished: "muted",
  "not-found": "danger",
  invalid: "danger",
};

/** Whether the screen is following the mission right now. Announced politely to screen readers. */
export default function ConnectionIndicator({ connection }: { connection: Connection }) {
  return (
    <span className="badge badge--outline" data-tone={TONE[connection]} role="status" aria-live="polite">
      <span className="badge__dot" data-pulse={connection === "live" || undefined} aria-hidden="true" />
      {TEXT[connection]}
    </span>
  );
}

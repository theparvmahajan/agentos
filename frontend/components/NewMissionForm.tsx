"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { ApiError, createMission } from "@/lib/api";

const MAX_LENGTH = 2000;
const EXAMPLES = [
  "Compare PostgreSQL and MySQL for a small startup",
  "Write Python that prints the first 20 primes, then have the result reviewed",
  "Research what a B-tree is, implement a tiny one in Python, and review it",
];

export default function NewMissionForm() {
  const router = useRouter();
  const [objective, setObjective] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const text = objective.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      const mission = await createMission(text);
      router.push(`/missions/${mission.id}`); // stay "busy" while the next page loads
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Please try again.");
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) void submit();
  }

  return (
    <form className="card new-mission" onSubmit={submit}>
      <label htmlFor="objective" className="card__title">
        What should the agents do?
      </label>
      <textarea
        id="objective"
        value={objective}
        onChange={(e) => setObjective(e.target.value)}
        onKeyDown={onKeyDown}
        maxLength={MAX_LENGTH}
        rows={3}
        placeholder="Describe an objective in plain language…"
        disabled={busy}
      />
      <div className="new-mission__examples" aria-label="Examples">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            className="chip"
            onClick={() => setObjective(example)}
            disabled={busy}
          >
            {example}
          </button>
        ))}
      </div>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      <div className="new-mission__actions">
        <span className="muted small">
          {objective.length}/{MAX_LENGTH} · Ctrl+Enter to run
        </span>
        <button type="submit" className="button button--primary" disabled={busy || !objective.trim()}>
          {busy ? "Starting…" : "Run mission"}
        </button>
      </div>
    </form>
  );
}

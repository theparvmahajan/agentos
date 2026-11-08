"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ApiError, listMissions } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { appendPage, mergeFirstPage } from "@/lib/mergeList";
import { MISSION_LABEL } from "@/lib/status";
import { MISSION_STATUSES } from "@/lib/types";
import type { MissionStatus, MissionSummary } from "@/lib/types";
import { useNow } from "@/lib/useNow";
import StatusBadge from "./StatusBadge";

const PAGE_SIZE = 20;
const DEFAULT_REFRESH_MS = 4000;

const message = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong.");

export default function MissionList({ refreshMs = DEFAULT_REFRESH_MS }: { refreshMs?: number }) {
  const [filter, setFilter] = useState<MissionStatus | "">("");
  const [items, setItems] = useState<MissionSummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [attempt, setAttempt] = useState(0); // bumped by "Try again"
  const loadedMore = useRef(false);
  const now = useNow(15000);

  // First page: on mount, when the filter changes, and on "Try again".
  useEffect(() => {
    let cancelled = false;
    loadedMore.current = false;
    listMissions({ limit: PAGE_SIZE, status: filter || null })
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setNext(page.next_cursor);
        setPhase("ready");
      })
      .catch((e) => {
        if (cancelled) return;
        setError(message(e));
        setPhase("error");
      });
    return () => {
      cancelled = true;
    };
  }, [filter, attempt]);

  // Keep statuses fresh: re-read the first page while the tab is visible.
  useEffect(() => {
    if (phase !== "ready") return;
    let cancelled = false;
    const timer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      listMissions({ limit: PAGE_SIZE, status: filter || null })
        .then((page) => {
          if (cancelled) return;
          setItems((current) => mergeFirstPage(current, page.items));
          if (!loadedMore.current) setNext(page.next_cursor);
          setRefreshFailed(false);
        })
        .catch(() => !cancelled && setRefreshFailed(true));
    }, refreshMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [phase, filter, refreshMs]);

  function changeFilter(value: string) {
    setPhase("loading");
    setItems([]);
    setNext(null);
    setFilter(value as MissionStatus | "");
  }

  async function loadMore() {
    if (!next || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await listMissions({ limit: PAGE_SIZE, cursor: next, status: filter || null });
      loadedMore.current = true;
      setItems((current) => appendPage(current, page.items));
      setNext(page.next_cursor);
    } catch (e) {
      setError(message(e));
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <section className="card" aria-labelledby="missions-heading">
      <div className="card__header">
        <h2 id="missions-heading" className="card__title">
          Missions
        </h2>
        <label className="filter">
          <span className="muted small">Show</span>
          <select value={filter} onChange={(e) => changeFilter(e.target.value)}>
            <option value="">All</option>
            {MISSION_STATUSES.map((status) => (
              <option key={status} value={status}>
                {MISSION_LABEL[status]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {phase === "loading" && (
        <p className="muted" role="status">
          Loading missions…
        </p>
      )}

      {phase === "error" && (
        <div className="alert" role="alert">
          <p>{error}</p>
          <button type="button" className="button" onClick={() => { setPhase("loading"); setAttempt((n) => n + 1); }}>
            Try again
          </button>
        </div>
      )}

      {phase === "ready" && items.length === 0 && (
        <p className="muted">
          {filter ? "No missions with that status." : "No missions yet. Describe an objective above to start one."}
        </p>
      )}

      {phase === "ready" && items.length > 0 && (
        <ul className="mission-list">
          {items.map((m) => (
            <li key={m.id}>
              <Link href={`/missions/${m.id}`} className="mission-row">
                <div className="mission-row__main">
                  <span className="mission-row__objective">{m.objective}</span>
                  <span className="mission-row__meta muted small">
                    {relativeTime(m.created_at, now)} ·{" "}
                    {m.tasks_total ? `${m.tasks_completed}/${m.tasks_total} tasks` : "no tasks yet"}
                  </span>
                </div>
                {m.tasks_total > 0 && (
                  <progress
                    className="progress"
                    value={m.tasks_completed}
                    max={m.tasks_total}
                    aria-label={`${m.tasks_completed} of ${m.tasks_total} tasks completed`}
                  />
                )}
                <StatusBadge kind="mission" status={m.status} />
              </Link>
            </li>
          ))}
        </ul>
      )}

      {phase === "ready" && error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {phase === "ready" && refreshFailed && (
        <p className="muted small" role="status">
          Could not refresh: showing the last known state.
        </p>
      )}
      {phase === "ready" && next && (
        <div className="center">
          <button type="button" className="button" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </section>
  );
}

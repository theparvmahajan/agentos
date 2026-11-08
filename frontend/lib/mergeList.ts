import type { MissionSummary } from "./types";

/**
 * The list refreshes by re-reading its first page. Merge that into what is already shown, which
 * may include older pages the user loaded: missions on the fresh page replace their old copies
 * (so statuses update), new ones appear on top, and older rows stay. Rows that are no longer
 * on the first page but are newer than its last row have disappeared (or no longer match the
 * filter) and are dropped.
 */
export function mergeFirstPage(
  current: MissionSummary[],
  firstPage: MissionSummary[],
): MissionSummary[] {
  const last = firstPage.at(-1);
  if (!last) return [];
  const fresh = new Set(firstPage.map((m) => m.id));
  const older = current.filter(
    (m) =>
      !fresh.has(m.id) &&
      (m.created_at < last.created_at || (m.created_at === last.created_at && m.id < last.id)),
  );
  return [...firstPage, ...older];
}

/** Append another page, skipping anything already shown. */
export function appendPage(current: MissionSummary[], page: MissionSummary[]): MissionSummary[] {
  const seen = new Set(current.map((m) => m.id));
  return [...current, ...page.filter((m) => !seen.has(m.id))];
}

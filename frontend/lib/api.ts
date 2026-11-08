import { API_URL } from "./config";
import type { Mission, MissionPage, MissionStatus } from "./types";

/** An error from the API (or from reaching it). `status` 0 means the request never got an answer. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** FastAPI sends {"detail": "text"} or, for invalid input, {"detail": [{"msg": ...}, ...]}. */
function describe(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((d) => (d as { msg?: unknown })?.msg)
      .filter((m): m is string => typeof m === "string");
    if (messages.length) return messages.join("; ");
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, `Cannot reach the API at ${API_URL}. Is it running?`);
  }
  const body: unknown = await response.json().catch(() => null);
  if (response.status === 401) {
    // A deployment puts a login in front of every POST (docs/DEPLOY.md). The browser normally
    // asks for it itself; if it was dismissed, say what is going on instead of a bare 401.
    throw new ApiError(401, "Starting or cancelling missions on this server needs the admin login.");
  }
  if (!response.ok) {
    throw new ApiError(response.status, describe(body, `Request failed (${response.status})`));
  }
  return body as T;
}

export interface ListParams {
  limit?: number;
  cursor?: string | null;
  status?: MissionStatus | null;
}

export function listMissions({ limit = 20, cursor, status }: ListParams = {}): Promise<MissionPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  if (status) query.set("status", status);
  return request<MissionPage>(`/api/v1/missions?${query}`);
}

export function createMission(objective: string): Promise<Mission> {
  return request<Mission>("/api/v1/missions", {
    method: "POST",
    body: JSON.stringify({ objective }),
  });
}

export function getMission(id: string): Promise<Mission> {
  return request<Mission>(`/api/v1/missions/${id}`);
}

export function cancelMission(id: string): Promise<Mission> {
  return request<Mission>(`/api/v1/missions/${id}/cancel`, { method: "POST" });
}

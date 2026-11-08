// The API's address. NEXT_PUBLIC_ values are inlined into the browser bundle at build time.
const raw = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const API_URL = raw.replace(/\/+$/, "");
/** http -> ws and https -> wss */
export const WS_URL = API_URL.replace(/^http/, "ws");

export function streamUrl(missionId: string): string {
  return `${WS_URL}/api/v1/missions/${missionId}/stream`;
}

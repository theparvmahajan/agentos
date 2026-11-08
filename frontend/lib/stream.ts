import { parseStreamMessage } from "./parse";
import type { StreamMessage } from "./types";

/** Where the live connection stands. */
export type Connection =
  | "connecting" // first attempt
  | "live" // a snapshot arrived: we are following the mission
  | "reconnecting" // lost, trying again
  | "finished" // the server closed it because the mission is over
  | "not-found" // no such mission
  | "invalid"; // the id was rejected

// Close codes, from docs/API.md.
export const CLOSE_NORMAL = 1000;
export const CLOSE_INVALID = 1008;
export const CLOSE_TOO_SLOW = 1013;
export const CLOSE_NOT_FOUND = 4404;

/** Delays before reconnecting after trouble; the last one repeats for as long as the page is open. */
export const BACKOFF_MS = [500, 1000, 2000, 5000, 10000];
const AFTER_TOO_SLOW_MS = 300;

interface SocketLike {
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close(code?: number): void;
}
type SocketFactory = new (url: string) => SocketLike;

export interface StreamOptions {
  url: string;
  onMessage: (message: StreamMessage) => void;
  onConnection: (connection: Connection) => void;
  /** Replaceable in tests. */
  WebSocketImpl?: SocketFactory;
  schedule?: (fn: () => void, ms: number) => unknown;
  cancel?: (handle: unknown) => void;
}

/**
 * Follows one mission over a WebSocket and keeps following it: if the connection drops it
 * reconnects with a growing delay, and every reconnect begins with a fresh snapshot from the
 * server, so nothing missed in between can leave the screen wrong.
 */
export class MissionStream {
  private socket: SocketLike | null = null;
  private timer: unknown = null;
  private attempt = 0;
  private stopped = false;

  constructor(private readonly options: StreamOptions) {}

  start(): void {
    this.stopped = false;
    this.open(this.attempt === 0 ? "connecting" : "reconnecting");
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) (this.options.cancel ?? clearTimeout)(this.timer as never);
    this.timer = null;
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onmessage = socket.onclose = socket.onerror = null;
      try {
        socket.close(CLOSE_NORMAL);
      } catch {
        // already closed
      }
    }
  }

  private open(status: "connecting" | "reconnecting"): void {
    this.options.onConnection(status);
    const Impl = this.options.WebSocketImpl ?? (WebSocket as unknown as SocketFactory);
    const socket = new Impl(this.options.url);
    this.socket = socket;

    socket.onmessage = (event) => {
      if (this.socket !== socket) return; // a socket we already gave up on
      const message = parseStreamMessage(String(event.data));
      if (!message) return;
      if (message.type === "snapshot") {
        this.attempt = 0;
        this.options.onConnection("live");
      }
      this.options.onMessage(message);
    };
    socket.onclose = (event) => {
      if (this.socket !== socket || this.stopped) return;
      this.socket = null;
      this.closed(event.code);
    };
    socket.onerror = () => {
      // A failed connection is always followed by a close event, which does the work.
    };
  }

  private closed(code: number): void {
    switch (code) {
      case CLOSE_NORMAL:
        return this.options.onConnection("finished");
      case CLOSE_NOT_FOUND:
        return this.options.onConnection("not-found");
      case CLOSE_INVALID:
        return this.options.onConnection("invalid");
      case CLOSE_TOO_SLOW:
        return this.retryIn(AFTER_TOO_SLOW_MS);
      default: {
        const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)] ?? 10000;
        this.attempt += 1;
        return this.retryIn(delay);
      }
    }
  }

  private retryIn(ms: number): void {
    this.options.onConnection("reconnecting");
    const schedule = this.options.schedule ?? ((fn: () => void, delay: number) => setTimeout(fn, delay));
    this.timer = schedule(() => {
      this.timer = null;
      if (!this.stopped) this.open("reconnecting");
    }, ms);
  }
}

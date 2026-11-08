import { beforeEach, describe, expect, it } from "vitest";
import {
  BACKOFF_MS,
  CLOSE_INVALID,
  CLOSE_NORMAL,
  CLOSE_NOT_FOUND,
  CLOSE_TOO_SLOW,
  MissionStream,
} from "@/lib/stream";
import type { Connection } from "@/lib/stream";
import type { StreamMessage } from "@/lib/types";
import { makeMission, makeTask } from "./factories";

class FakeSocket {
  static instances: FakeSocket[] = [];
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closedWith: number | undefined;
  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  close(code?: number) {
    this.closedWith = code;
  }
  receive(data: unknown) {
    this.onmessage?.({ data: typeof data === "string" ? data : JSON.stringify(data) });
  }
  drop(code: number) {
    this.onclose?.({ code });
  }
}

const SNAPSHOT: StreamMessage = { type: "snapshot", mission: makeMission({ tasks: [makeTask({ key: "a" })] }) };

let connections: Connection[];
let messages: StreamMessage[];
let timers: { fn: () => void; ms: number; live: boolean }[];

function make() {
  const stream = new MissionStream({
    url: "ws://test/stream",
    onMessage: (m) => messages.push(m),
    onConnection: (c) => connections.push(c),
    WebSocketImpl: FakeSocket,
    schedule: (fn, ms) => {
      const timer = { fn, ms, live: true };
      timers.push(timer);
      return timer;
    },
    cancel: (handle) => {
      (handle as { live: boolean }).live = false;
    },
  });
  stream.start();
  return stream;
}

const socket = (i = FakeSocket.instances.length - 1) => {
  const s = FakeSocket.instances[i];
  if (!s) throw new Error("no socket");
  return s;
};
const fireLastTimer = () => {
  const timer = timers.at(-1);
  if (!timer?.live) throw new Error("no live timer");
  timer.live = false;
  timer.fn();
};

beforeEach(() => {
  FakeSocket.instances = [];
  connections = [];
  messages = [];
  timers = [];
});

describe("MissionStream", () => {
  it("connects to the given address and reports connecting, then live at the first snapshot", () => {
    make();
    expect(socket().url).toBe("ws://test/stream");
    expect(connections).toEqual(["connecting"]);
    socket().receive(SNAPSHOT);
    expect(connections).toEqual(["connecting", "live"]);
  });

  it("passes messages on in order", () => {
    make();
    const task: StreamMessage = { type: "task", mission_id: SNAPSHOT.type === "snapshot" ? SNAPSHOT.mission.id : "", task: makeTask({ key: "a", status: "running" }) };
    socket().receive(SNAPSHOT);
    socket().receive(task);
    expect(messages).toEqual([SNAPSHOT, task]);
  });

  it("ignores frames that are not valid messages", () => {
    make();
    socket().receive("not json");
    socket().receive({ type: "ping" });
    expect(messages).toEqual([]);
    expect(connections).toEqual(["connecting"]);
  });

  it("stops quietly when the server closes normally: the mission is over", () => {
    make();
    socket().receive(SNAPSHOT);
    socket().drop(CLOSE_NORMAL);
    expect(connections.at(-1)).toBe("finished");
    expect(timers).toEqual([]);
  });

  it("does not reconnect for an unknown mission", () => {
    make();
    socket().drop(CLOSE_NOT_FOUND);
    expect(connections.at(-1)).toBe("not-found");
    expect(timers).toEqual([]);
  });

  it("does not reconnect for an invalid id", () => {
    make();
    socket().drop(CLOSE_INVALID);
    expect(connections.at(-1)).toBe("invalid");
    expect(timers).toEqual([]);
  });

  it("reconnects after a dropped connection and says so", () => {
    make();
    socket().receive(SNAPSHOT);
    socket().drop(1006);
    expect(connections.at(-1)).toBe("reconnecting");
    expect(timers.at(-1)?.ms).toBe(BACKOFF_MS[0]);
    fireLastTimer();
    expect(FakeSocket.instances).toHaveLength(2);
    expect(socket().url).toBe("ws://test/stream");
  });

  it("waits longer after each failed attempt, up to the limit", () => {
    make();
    const delays: number[] = [];
    for (let i = 0; i < BACKOFF_MS.length + 2; i++) {
      socket().drop(1006); // never gets a snapshot
      delays.push(timers.at(-1)?.ms ?? -1);
      fireLastTimer();
    }
    expect(delays.slice(0, BACKOFF_MS.length)).toEqual(BACKOFF_MS);
    expect(delays.slice(BACKOFF_MS.length)).toEqual([BACKOFF_MS.at(-1), BACKOFF_MS.at(-1)]);
  });

  it("starts the delays over once a connection works again", () => {
    make();
    socket().drop(1006);
    fireLastTimer();
    socket().drop(1006);
    fireLastTimer();
    socket().receive(SNAPSHOT); // healthy again
    socket().drop(1006);
    expect(timers.at(-1)?.ms).toBe(BACKOFF_MS[0]);
  });

  it("reconnects quickly when it was cut off for being too slow, without backing off", () => {
    make();
    socket().receive(SNAPSHOT);
    socket().drop(CLOSE_TOO_SLOW);
    expect(timers.at(-1)?.ms).toBe(300);
    fireLastTimer();
    socket().drop(1006);
    expect(timers.at(-1)?.ms).toBe(BACKOFF_MS[0]); // the slow cut-off did not count as a failure
  });

  it("gets a fresh snapshot after every reconnect", () => {
    make();
    socket().receive(SNAPSHOT);
    socket().drop(1006);
    fireLastTimer();
    const later: StreamMessage = { type: "snapshot", mission: makeMission({ status: "completed" }) };
    socket().receive(later);
    expect(messages.at(-1)).toEqual(later);
    expect(connections.at(-1)).toBe("live");
  });

  it("ignores late events from a socket it already gave up on", () => {
    make();
    const first = socket();
    first.drop(1006);
    fireLastTimer();
    const count = messages.length;
    first.receive(SNAPSHOT); // stale
    first.drop(1006); // stale
    expect(messages).toHaveLength(count);
    expect(timers.filter((t) => t.live)).toEqual([]);
  });

  it("stop() closes the socket and silences everything", () => {
    const stream = make();
    const s = socket();
    stream.stop();
    expect(s.closedWith).toBe(CLOSE_NORMAL);
    s.receive(SNAPSHOT);
    s.drop(1006);
    expect(messages).toEqual([]);
    expect(timers).toEqual([]);
  });

  it("stop() cancels a reconnect that is waiting", () => {
    const stream = make();
    socket().drop(1006);
    const pending = timers.at(-1);
    stream.stop();
    expect(pending?.live).toBe(false);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});

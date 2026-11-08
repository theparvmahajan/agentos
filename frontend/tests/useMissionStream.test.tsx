import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Connection } from "@/lib/stream";
import type { StreamMessage } from "@/lib/types";
import { useMissionStream } from "@/lib/useMissionStream";
import { makeMission, makeTask } from "./factories";

interface Options {
  url: string;
  onMessage: (m: StreamMessage) => void;
  onConnection: (c: Connection) => void;
}
interface FakeStream {
  options: Options;
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
}
const { streams } = vi.hoisted(() => ({ streams: [] as FakeStream[] }));

vi.mock("@/lib/stream", () => ({
  MissionStream: class {
    start = vi.fn();
    stop = vi.fn();
    constructor(public options: Options) {
      streams.push(this as unknown as FakeStream);
    }
  },
}));

const MISSION = makeMission({ id: "m1", tasks: [makeTask({ key: "a" })] });
const current = () => {
  const s = streams.at(-1);
  if (!s) throw new Error("no stream was created");
  return s;
};

beforeEach(() => {
  streams.length = 0;
});

describe("useMissionStream", () => {
  it("starts following the mission's stream address", () => {
    renderHook(() => useMissionStream("m1"));
    expect(streams).toHaveLength(1);
    expect(current().options.url).toBe("ws://localhost:8000/api/v1/missions/m1/stream");
    expect(current().start).toHaveBeenCalledTimes(1);
  });

  it("has no state until the snapshot arrives", () => {
    const { result } = renderHook(() => useMissionStream("m1"));
    expect(result.current.state).toBeNull();
    expect(result.current.connection).toBe("connecting");
  });

  it("builds its state from the messages and reports the connection", () => {
    const { result } = renderHook(() => useMissionStream("m1"));
    act(() => {
      current().options.onConnection("live");
      current().options.onMessage({ type: "snapshot", mission: MISSION });
    });
    expect(result.current.connection).toBe("live");
    expect(result.current.state?.tasks.map((t) => t.status)).toEqual(["pending"]);

    act(() => current().options.onMessage({ type: "task", mission_id: "m1", task: makeTask({ key: "a", status: "completed" }) }));
    expect(result.current.state?.tasks.map((t) => t.status)).toEqual(["completed"]);
  });

  it("lets a mission read some other way (such as cancelling) become the state", () => {
    const { result } = renderHook(() => useMissionStream("m1"));
    act(() => current().options.onMessage({ type: "snapshot", mission: MISSION }));
    act(() => result.current.applyMission({ ...MISSION, status: "cancelled" }));
    expect(result.current.state?.mission.status).toBe("cancelled");
  });

  it("stops following when the page goes away", () => {
    const { unmount } = renderHook(() => useMissionStream("m1"));
    const stream = current();
    unmount();
    expect(stream.stop).toHaveBeenCalledTimes(1);
  });

  it("follows a different mission after the id changes, closing the old stream", () => {
    const { rerender } = renderHook(({ id }) => useMissionStream(id), { initialProps: { id: "m1" } });
    const first = current();
    rerender({ id: "m2" });
    expect(first.stop).toHaveBeenCalledTimes(1);
    expect(streams).toHaveLength(2);
    expect(current().options.url).toContain("/missions/m2/");
  });
});

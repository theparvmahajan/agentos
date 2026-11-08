import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, cancelMission, createMission, getMission, listMissions } from "@/lib/api";
import { makeMission } from "./factories";

const reply = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function mockFetch(response: Response | Error) {
  const fn = vi.fn(async () => {
    if (response instanceof Error) throw response;
    return response;
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

const lastUrl = (fn: ReturnType<typeof mockFetch>) => String((fn.mock.calls[0] as unknown[])[0]);
const lastInit = (fn: ReturnType<typeof mockFetch>) => (fn.mock.calls[0] as unknown[])[1] as RequestInit;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a login in front of the API", () => {
  it("explains a 401 instead of showing a bare status", async () => {
    mockFetch(reply(401, null));
    await expect(createMission("hello")).rejects.toMatchObject({
      status: 401,
      message: expect.stringContaining("admin login"),
    });
  });
});

describe("listMissions", () => {
  it("asks for 20 by default", async () => {
    const fn = mockFetch(reply(200, { items: [], next_cursor: null }));
    await listMissions();
    expect(lastUrl(fn)).toBe("http://localhost:8000/api/v1/missions?limit=20");
  });

  it("passes the limit, cursor and status along", async () => {
    const fn = mockFetch(reply(200, { items: [], next_cursor: null }));
    await listMissions({ limit: 5, cursor: "abc", status: "running" });
    expect(lastUrl(fn)).toBe("http://localhost:8000/api/v1/missions?limit=5&cursor=abc&status=running");
  });

  it("leaves out an empty cursor and status", async () => {
    const fn = mockFetch(reply(200, { items: [], next_cursor: null }));
    await listMissions({ cursor: null, status: null });
    expect(lastUrl(fn)).toBe("http://localhost:8000/api/v1/missions?limit=20");
  });

  it("returns the page", async () => {
    mockFetch(reply(200, { items: [], next_cursor: "next" }));
    expect(await listMissions()).toEqual({ items: [], next_cursor: "next" });
  });
});

describe("createMission", () => {
  it("posts the objective as JSON", async () => {
    const fn = mockFetch(reply(202, makeMission()));
    await createMission("do the thing");
    expect(lastUrl(fn)).toBe("http://localhost:8000/api/v1/missions");
    expect(lastInit(fn).method).toBe("POST");
    expect(JSON.parse(String(lastInit(fn).body))).toEqual({ objective: "do the thing" });
    expect(new Headers(lastInit(fn).headers).get("Content-Type")).toBe("application/json");
  });
});

describe("getMission and cancelMission", () => {
  it("reads one mission", async () => {
    const fn = mockFetch(reply(200, makeMission()));
    expect((await getMission("abc")).objective).toBe("compare databases");
    expect(lastUrl(fn)).toBe("http://localhost:8000/api/v1/missions/abc");
  });

  it("cancels with a POST", async () => {
    const fn = mockFetch(reply(200, makeMission({ status: "cancelled" })));
    expect((await cancelMission("abc")).status).toBe("cancelled");
    expect(lastUrl(fn)).toBe("http://localhost:8000/api/v1/missions/abc/cancel");
    expect(lastInit(fn).method).toBe("POST");
  });
});

describe("errors", () => {
  async function failure(response: Response | Error): Promise<ApiError> {
    mockFetch(response);
    try {
      await getMission("x");
    } catch (e) {
      return e as ApiError;
    }
    throw new Error("expected a failure");
  }

  it("uses the API's detail text", async () => {
    const e = await failure(reply(409, { detail: "mission already completed" }));
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(409);
    expect(e.message).toBe("mission already completed");
  });

  it("joins validation messages when detail is a list", async () => {
    const e = await failure(reply(422, { detail: [{ msg: "too short" }, { msg: "bad value" }] }));
    expect(e.message).toBe("too short; bad value");
  });

  it("falls back to the status code when there is no usable detail", async () => {
    expect((await failure(reply(500, { oops: true }))).message).toBe("Request failed (500)");
    expect((await failure(new Response("<html>", { status: 502 }))).message).toBe("Request failed (502)");
  });

  it("says the API is unreachable when the request itself fails", async () => {
    const e = await failure(new TypeError("fetch failed"));
    expect(e.status).toBe(0);
    expect(e.message).toBe("Cannot reach the API at http://localhost:8000. Is it running?");
  });
});

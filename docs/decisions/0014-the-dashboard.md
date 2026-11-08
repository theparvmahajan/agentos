# 0014: The dashboard

Status: accepted, 2026-11-05
Issue: #15

## Context

The backend can plan a mission, run it across workers and stream every change (issues 12 to 14), but
the only way to see any of it was a terminal script. The dashboard is the first thing a person can
open to watch the system work, and it has to stay correct while many things change at once.

## Decision

- **Next.js (App Router) with React 19 and TypeScript, mostly client components.** Two pages: the
  mission list, and one mission with a live graph. A server component is used only to reject a
  malformed mission id before any connection is opened.
- **State comes from one reducer fed by the WebSocket.** The first message is a snapshot, later
  messages are complete states, the reducer replaces what it holds.
- **A small WebSocket client class** (`MissionStream`) owns reconnecting. It is plain TypeScript
  with an injectable socket and timers, so it is tested without a browser.
- **React Flow for drawing, our own layout for placing.** About 70 lines lay tasks out in columns by
  dependency depth.
- **Plain CSS and no state library.** Two pages do not need more.
- **Exact dependency versions, all at least 14 days old, with a lockfile.**
- **CORS allow-list on the API** for the dashboard's address.

## Design questions and answers

**1. Why Next.js when the dashboard is almost entirely live, client-side data?**
Server rendering buys little here: what matters is the stream, which only exists in the browser. A
Vite single-page app would work equally well. Next.js was chosen for the roadmap (deploying the
dashboard in issue 16 is a one-click flow), for its routing and build, and for response headers.
The cost is more moving parts than the problem needs. The one place the server helps is small but
real: an id that is not a UUID gets a 404 page without ever opening a socket.

**2. Why validate every WebSocket message and replace state instead of patching it?**
The server is ours, but a browser should not let whatever arrives on a socket crash the page, so
`parse.ts` checks each message and drops a malformed one. Replace-by-key follows from the protocol:
every message is a complete state, so applying it twice or late can only show a state the mission
really had, and the next message corrects it. A transient step backwards is therefore possible in
a rare race and is self-healing; it would be wrong to "fix" it by refusing status regressions,
because milestone 4 retries legitimately move a task from running back to queued.

**3. How does the page cope with a dropped connection?**
`MissionStream` reacts to the close code from `docs/API.md`: 1000 means the mission is over (stop),
4404 and 1008 mean it can never work (stop and say so), 1013 means we were too slow (reconnect
after 300 ms), and anything else is trouble (reconnect after 0.5, 1, 2, 5, then every 10 seconds,
for as long as the page is open). Every reconnect begins with a snapshot, which is what makes
reconnecting safe. This was exercised in a real browser: with a mission running, the API process
was killed outright; the page showed "Reconnecting…", and when the API came back it reconnected
alone and showed the mission's true final state, even though tasks had finished while it was down.

**4. What was the flicker bug, and why did 178 passing tests miss it?**
Once a second while a mission ran, every node in the graph vanished for a moment, and its edges
with it. Cause: the graph rebuilds its node objects whenever anything changes (a clock tick, a
status, a selection), and React Flow treats a node it has not measured as new: it hides it and
drops its edges until it measures again. Unit tests cannot see this, because it only happens
inside React Flow in a real browser with real layout. It was found by driving the finished
dashboard with a scripted headless Chrome against the real backend and sampling the page while a
mission ran; the first screenshot showed an empty canvas. The first fix (fixed node sizes) removed
the hidden nodes but the browser run, repeated, still caught a missing edge, which led to React
Flow's source: it keeps a node's edge attachment points across a rebuild only if the node carries
a measured size. The real fix tells React Flow each node's size and attachment points up front, so
it never has to measure. Two kinds of test now guard it: unit tests on the graph builder, and the
browser run, which samples the page throughout and fails on any hidden node or missing edge. It
then passed three runs in a row.

**5. What are the security properties, and what is missing?**
Everything a model or a web page wrote (instructions, outputs, errors, results) is rendered as
text only: React escapes it and nothing uses `dangerouslySetInnerHTML`; tests feed hostile markup
through every place it is shown, and two deliberate regressions to HTML rendering were caught.
Response headers forbid framing and sniffing. The API only answers browser requests from the
allow-listed origins, with no cookies. Missing: there is no login, so anyone who can reach the
API can read and cancel missions (milestone M5), and the WebSocket does not check its origin
(CORS does not apply to WebSockets). Until M5 both should stay on localhost.

**6. Why write a layout instead of using a graph-layout library?**
Mission graphs have tens of tasks, and the layout is small enough to test exhaustively: columns by
longest dependency path, rows ordered by the average position of dependencies. Writing it also
found a real mistake in my own tests (they could not tell the longest path from the shortest, which
would draw a task left of something it depends on). The price is that it does not minimise edge
crossings in general; a wide, tangled graph would need a proper layered-layout algorithm.

**7. Why those dependency versions, and what went wrong the first time?**
Every direct dependency is pinned exactly and is at least two weeks old, because a brand-new
release can carry malicious code before anyone notices (several of the very latest releases,
including Next.js itself, were only two to three days old and were skipped), and CI installs from
the lockfile with `npm ci`. Three things were wrong in the first release and were found only when
the author ran it. First, I had run the tools on a newer Node than the author's: jsdom 30 needs
Node 22.22+, and on Node 22.13 all 17 test files crashed at startup. Now jsdom is on 29 and jest-dom
on 6.9.1, every locked package was checked against the supported range (`^20.19 || ^22.13 || >=24`),
`engine-strict` makes an unsupported Node fail at install with a clear message, and CI tests the
oldest versions (20.19.0 and 22.13.0) as well as the latest 22. Second, I had audited only the
runtime dependencies; the full audit showed 2 critical advisories (in `tinypool`, inside vitest 3)
and 9 moderate ones. Vitest 4.1.12 removes the critical ones. It crashes npm 10 and npm 11.0 when
they *resolve* its optional peers, so the lockfile was generated with npm 12.2.1; `npm ci` from it
was verified with npm 10.9.7 and 11.0.0 on Node 20.19 and 22.13, and changing dependencies needs a
recent npm (see `frontend/README.md`). Third, 8 moderate advisories remain: a denial-of-service in
`minimatch` inside the TypeScript lint plugin. No fixed release exists (the advisory covers every
version), it only ever runs on our own file names at lint time, so it is an accepted, documented
risk; CI fails on anything high in the runtime dependencies or anything critical anywhere. ESLint
stays on 9, which npm flags as unsupported, because ESLint 10 only installs with forced peer
overrides.

**8. Limits to state honestly.**
(a) No token counts, which the original plan mentioned: they are returned by agents but not stored
yet (milestone M7), so the dashboard shows only real durations. (b) The mission list polls every 4
seconds instead of streaming. (c) Checked in headless Chrome at desktop and phone widths only;
keyboard-only use of the graph and other browsers are untested. (d) A worker killed mid-task leaves
its task running, and the dashboard will show it as running (milestone M4).

## Consequences

- Anything that changes a task or mission appears on screen with no extra code.
- The API gained a CORS allow-list setting (`CORS_ORIGINS`).
- The frontend has its own checks (type-check, lint, 183 unit and component tests, build, audit) in CI, on three Node versions.
- Running the whole system needs five processes: API, scheduler, workers, dashboard (and Docker).

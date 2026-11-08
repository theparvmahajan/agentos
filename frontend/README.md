# AgentOS dashboard

A live view of the runtime: start a mission, watch its task graph fill in as the agents work, open
any task to read its instruction and output, and cancel a mission that is taking too long.

Next.js 16 (App Router), React 19, TypeScript, and [React Flow](https://reactflow.dev) for the graph.
Plain CSS, light and dark by system setting. No UI kit and no state library.

## Run it

You need **Node 20.19+ or 22.13+** (22 LTS recommended) and the backend running (see the main README).
Node 24 is allowed by the dependencies but has not been tried. Any other version stops at install with a
clear message instead of failing later. Node 21 and 22.0 to 22.12 are not supported.

```bash
make web-install      # once: npm ci in this folder
make web              # http://localhost:3000
```

The dashboard talks to the API at `http://localhost:8000`. To use another address, copy
`.env.local.example` to `.env.local`, edit it, and restart: `NEXT_PUBLIC_API_URL` is baked in when
the app starts or builds. The API must list the dashboard's address in `CORS_ORIGINS`.

## Checks

```bash
make web-check        # type-check, lint and the tests
make web-build        # a production build
```

CI runs the same, plus `npm audit` on the runtime dependencies.

## What is where

```
app/                 pages: the mission list (/) and one mission (/missions/[id])
components/          the screens' parts; GraphView/TaskNode draw the graph
lib/
  stream.ts          the WebSocket client: follows a mission and reconnects by itself
  reducer.ts         applies each message to what the page holds
  parse.ts           checks every message from the server before it is used
  layout.ts          lays the task graph out left to right
  api.ts             the REST calls
  format.ts status.ts mergeList.ts   small pure helpers
tests/               unit and component tests (vitest + Testing Library)
```

## How the live view works

Opening a mission opens a WebSocket (`docs/API.md`). The first message is a snapshot of the whole
mission; after that every message is the complete current state of one task or of the mission. The
page just replaces what it holds, so an out-of-order or repeated message cannot corrupt anything.
If the connection drops it reconnects with a growing delay, and every reconnect starts with a new
snapshot, so whatever was missed is picked up. Nothing the server sends is trusted blindly: each
message is validated and a malformed one is dropped.

## Changing dependencies

`npm ci` (what `make web-install` runs) works with any npm that ships with the supported Node versions.
But *resolving* new dependencies needs a recent npm (12 or newer, which needs Node 22.22+ or 24):
older npm versions crash on vitest 4's optional peers. To add or update a package:

```bash
npx npm@12 install <package>       # in this folder, with Node 22.22+ or 24
```

then commit `package.json` and `package-lock.json` together. Keep every direct dependency pinned
exactly and at least two weeks old.

## Security notes

- Text written by language models or taken from web pages (instructions, outputs, errors, results)
  is only ever shown as text: React escapes it and nothing uses `dangerouslySetInnerHTML`.
  Tests check this with hostile input.
- There is no login yet (milestone M5). Anyone who can reach the API and the dashboard can see
  and cancel missions: run both on localhost.
- The API only accepts browser requests from the origins in `CORS_ORIGINS`.
- `npm audit` reports **no vulnerabilities in the runtime dependencies**, and nothing critical anywhere.
  It does list 8 *moderate* ones in the dev tooling: a denial-of-service in `minimatch`, used by the
  TypeScript lint plugin on this project's own file names. No fixed version exists yet (the advisory
  covers every release), so this is an accepted risk. CI fails on any high or critical finding.

## Known limits

- No token counts: they are not stored yet (milestone M7). Task and mission durations are real.
- The mission list refreshes by polling every 4 seconds while the tab is visible; only a single
  mission is streamed.
- The graph layout is a simple left-to-right layering. It is fine for tens of tasks; a very wide or
  tangled graph would need a smarter layout.
- Checked in headless Chrome at desktop and phone widths. Other browsers, and keyboard-only use of
  the graph, are not tested.

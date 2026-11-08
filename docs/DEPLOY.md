# Deploying AgentOS

One command starts everything in containers behind a single address:

```
                 internet
                    |  80 / 443
              +-----v------+   /api/*, /health, /docs   +-----+
              |   Caddy    +---------------------------->| api |--+
              | (TLS, gate)|                             +-----+  |   +----------+
              +-----+------+   everything else  +-----+           +-->| postgres |
                    +--------------------------->| web |   +-----+ |   +----------+
                                                 +-----+   |sched|-+   +----------+
                                                           +-----+ +-->|  redis   |
                                         +--------+                    +----------+
                                         | worker |--- docker.sock ---> host Docker
                                         +--------+   (starts throwaway code sandboxes)
```

Only Caddy has published ports. Postgres, Redis, the API and the dashboard are reachable only on
the compose network.

## 1. Try it on your own machine (no domain)

You need Docker with the compose plugin, and an LLM: either a hosted OpenAI-compatible service or an
Ollama on the host.

```bash
cp .env.example .env
make hash-password                 # type a password; copy the line it prints ($2a$...)
```

Edit `.env`:

```
POSTGRES_PASSWORD=<something random>
ADMIN_PASSWORD_HASH='<the hash, inside single quotes>'
DOCKER_GID=<output of: stat -c %g /var/run/docker.sock>   # Linux. See "Docker Desktop" below.
LLM_PROVIDER=openai_compatible
LLM_MODEL=<a model your provider serves>
OPENAI_BASE_URL=https://api.groq.com/openai/v1             # or any OpenAI-compatible endpoint
OPENAI_API_KEY=<your key>
TAVILY_API_KEY=<your key>
```

Keep the single quotes around the hash: it contains `$`, which compose would otherwise try to expand.

```bash
make prod-up        # builds three images; the first run takes several minutes
make prod-logs      # Ctrl+C leaves the stack running
```

Open http://localhost:8080. Reading is open; the first time you start a mission the browser should ask for
the login (user `admin`, the password you hashed).

Using Ollama on the host instead: `LLM_PROVIDER=ollama`, `LLM_MODEL=<model>`; the containers reach it
at `host.docker.internal:11434` (Ollama must listen on all interfaces, `OLLAMA_HOST=0.0.0.0`).

## 2. A public host

1. A small Linux VM that **runs nothing else** (see the Docker socket note). Install Docker Engine.
2. Point a DNS A record (for example `agentos.example.com`) at it. Open ports 80 and 443 only.
3. In `.env`: `PUBLIC_URL=https://agentos.example.com`, `SITE_ADDRESS=agentos.example.com`,
   `HTTP_PORT=80`, `HTTPS_PORT=443`, plus everything from section 1.
4. `make prod-up`. Caddy obtains the certificate on its own (this needs the DNS record to be live and
   port 80 reachable).

`PUBLIC_URL` is compiled into the dashboard's browser bundle, so **changing it needs a rebuild**:
`make prod-up` again (the `web` image is rebuilt because its build argument changed).

## 3. What the login gate does, and does not

The API has no authentication yet (milestone M5), so Caddy requires HTTP basic auth on every `POST`:
starting and cancelling missions. Over HTTPS this keeps strangers from spending your LLM and search credits.

It does **not**:

- hide anything: anyone can read the mission list, objectives, task outputs and results, and watch
  the WebSocket stream. Do not put anything private in a mission on a public host;
- limit the rate of requests, even for logged-in users (no rate limiting yet, M5);
- protect a plain-http deployment: basic auth over http sends the password in clear text.
  Use it over https, or only on localhost.

## 4. The Docker socket (read this)

The code sandbox starts containers on the host's Docker. The worker therefore has
`/var/run/docker.sock` mounted, and **access to that socket is equivalent to root on the host**.
What limits the exposure:

- only the worker has the socket; the code an agent writes runs *inside* the throwaway container
  (no network, read-only filesystem, all capabilities dropped, non-root, memory/CPU/process limits),
  never in the worker;
- the worker issues one fixed `docker run` command (`core/tools/python_exec.py`), with no input
  from the model other than the program text, sent over stdin.

What it does not remove: a bug that gave an attacker code execution inside the *worker* would give
them the host. Hence the dedicated VM. Containers share the host kernel, so this is not isolation
against a determined attacker (stronger isolation is planned for M8).

## 5. Running it

```bash
make prod-logs                                             # follow everything
docker compose -f docker-compose.prod.yml ps               # health of each service
docker compose -f docker-compose.prod.yml up -d --scale worker=3   # more workers
git pull && make prod-up                                   # update (migrations run automatically)
docker compose -f docker-compose.prod.yml exec postgres \
    pg_dump -U agentos agentos > backup.sql                # back up
make prod-down                                             # stop; data is kept
docker compose -f docker-compose.prod.yml down -v          # stop AND DELETE the database
```

## 6. Known limits of this deployment

- **A worker that dies is recovered, but not instantly.** Its task is retried after its lease
  expires (about 40 s with the defaults) by the reaper in the scheduler. Its unfinished stream message
  stays pending under the dead container's name (names come from the container id) until
  `XAUTOCLAIM` is added; this is harmless because the reaper republishes the task. See
  [RELIABILITY.md](RELIABILITY.md) for what is and is not covered.
- No restore procedure is tested yet; `pg_dump` above is untested here beyond being the standard command.
- Postgres and Redis have no passwords other than the compose-network isolation (`POSTGRES_PASSWORD`
  is set; Redis is not exposed and has none).
- Resource needs are not measured. A VM with 2 vCPUs and 4 GB of RAM is a reasonable start, not a tested minimum.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `required variable ... is missing` | `.env` lacks `POSTGRES_PASSWORD`, `LLM_MODEL` or `ADMIN_PASSWORD_HASH` |
| Login always refused | the hash lost its single quotes in `.env`, or was made for another password |
| Code tasks fail with "docker unavailable" / permission denied | `DOCKER_GID` does not match `stat -c %g /var/run/docker.sock` on the host |
| Code tasks fail with "Unable to find image" | `sandbox-image` did not run: `docker compose -f docker-compose.prod.yml up sandbox-image` |
| Dashboard says it cannot reach the API | `PUBLIC_URL` differs from the address in the browser; fix `.env` and `make prod-up` |
| Planning fails immediately | `LLM_*` / `OPENAI_*` wrong or unreachable from the container: see `logs api` |

**Docker Desktop (Windows/macOS):** the socket inside Docker Desktop's VM is owned by root with a
different group, so `DOCKER_GID` may not apply. Use Docker Desktop for a try-out only, not for hosting;
if code tasks fail with a permission error, add `user: root` to the `worker` service for local use only.
This was not tested.

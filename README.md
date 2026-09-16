# agent-api

An **OpenAI Responses-API-compatible shim** that serves Muse (a personal AI assistant) as a stateless HTTP API, so it can be used as a model backend for **Claude Code via CLIProxyAPI**.

The core semantic: **Claude Code owns the multi-turn agentic loop.** Each HTTP request to this bridge is exactly one model turn, handled by one stateless worker. The bridge never accumulates conversation state.

## Architecture

```
Claude Code → CLIProxyAPI → POST 127.0.0.1:8787/v1/responses
    │
    ▼
server.py ── writes queue/<resp_id>.json ── long-polls (≤900s) for responses/<resp_id>.json
    │
    ▼
dispatcher (persistent subagent) ── watches queue/ ── spawns ONE worker subagent per file
    │
    ▼
worker (single-use subagent) ── reads ONLY its request file ── writes OpenAI
    response object to responses/<resp_id>.json ── exits
    │
    ▼
server.py returns the response as JSON, or as SSE when "stream": true
```

Workers **never execute tools**. They emit `function_call` items; Claude Code / CLIProxyAPI executes them and sends results back as `function_call_output` in a follow-up request.

**Per-request overhead is ~5–10s fixed** (LLM inference steps: dispatcher notice + worker spin-up), regardless of task size. Floor is ~3–5s — this is a property of the agent harness, not the plumbing. Irrelevant for long agentic turns; noticeable for rapid-fire trivial calls.

## Quickstart

```bash
./scripts/bootstrap.sh   # (re)start the server
./scripts/status.sh      # health overview

# smoke test
curl -s -X POST http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","input":"Reply with exactly: SMOKE_OK","max_output_tokens":30}'
```

The dispatcher is supervised automatically (see "Supervision" below) — no manual step needed.

## File map

| Path | What it is |
|---|---|
| `server.py` | HTTP server: `POST /v1/responses`, `GET /v1/models`, `GET /health`. Model id `muse-spark`. |
| `DISPATCHER.md` | Brief for the persistent dispatcher subagent (queue watcher, worker spawner). |
| `WORKER_PROMPT.md` | Brief for single-use workers (context isolation, response format, must-write rule). |
| `scripts/bootstrap.sh` | (Re)start the server with logging. |
| `scripts/status.sh` | Health overview: server, dispatcher liveness, queue depths. |
| `archive/` | Retired designs (the pre-warmed pool experiment). |
| `queue/`, `processing/`, `responses/`, `streams/`, `dead_letters/` | Runtime state (gitignored). |

## Supervision

Two scheduled jobs keep the bridge alive (registered in the Muse scheduler, not in this repo):

- **`agent-api-watchdog`** (every 5 min): restarts the server if `/health` fails; respawns the dispatcher if its proof-of-life is stale; reaps `processing/` files older than 20 min with no response back into `queue/`.
- **`agent-api-sweeper`** (every 1 min): backstop dispatcher — claims any `queue/` file older than 90s the dispatcher missed and spawns a worker for it.

**Liveness the right way:** the dispatcher proves it is alive by appending to `dispatcher.scanlog` after every queue poll. The older `dispatcher.heartbeat` file is written by a detached loop and proves nothing — the watchdog ignores it. (We learned this the hard way.)

## Runbook

| Symptom | Likely cause | Fix |
|---|---|---|
| `status.sh`: server DOWN | Server process died (VM recycle, crash) | `./scripts/bootstrap.sh`; watchdog also restarts it within 5 min |
| `status.sh`: dispatcher STALE | Dispatcher agent exited or wedged | Watchdog respawns within 5 min; or ask the assistant to respawn it from `DISPATCHER.md` |
| Requests hang, `processing/` grows | Worker died silently | Wait for watchdog reap (20 min) or move the file back to `queue/` manually |
| Duplicate responses | A file was processed twice after reaping | Harmless: last write wins on `responses/<id>.json` |
| `pkill` killed your own shell | `pkill -f` matched your command string | Always use a self-excluding pattern: `pkill -f "[a]gent-api/server.py"` |

## Design decisions & lessons

- **Dispatcher, not pool.** A pre-warmed worker pool was tried (2026-09-16): faster hot path, but workers died silently leaving dead letters and the supervisor wedged. Reverted — reliability beats ~3s on a bridge. See `archive/`.
- **Dispatcher agents need a never-exit rule.** A subagent can mistake a background launcher's completion for task completion and end its turn. `DISPATCHER.md` states this explicitly, and the spawn message repeats it.
- **The queue watcher must stream.** Its stdout has to reach the dispatcher's `process.poll` — no `nohup`, no output redirect, or the dispatcher goes blind.
- **Context isolation is prompt-enforced.** The dispatcher only sees filenames; workers are instructed to treat the request file as their sole input. This is architectural/logical isolation, not a platform guarantee — don't claim more.

## Authentication

All `/v1/*` endpoints require `Authorization: Bearer <token>`. The token is
read from the `AGENT_API_TOKEN` env var, falling back to the `.token` file in
the project root (gitignored, `chmod 600`). The server refuses to start with
no token configured. `/health` stays unauthenticated for local monitoring.

```bash
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer $(cat .token)"
```
- `function_call` / `function_call_output` round-trip not yet tested end-to-end.
- `stream: true` emits valid SSE but chunks a completed response — not live token streaming.
- No tunnel configured yet (needed for remote Claude Code to reach `127.0.0.1:8787`).
- No request-size limits, rate limits, or retention policy on old response files.

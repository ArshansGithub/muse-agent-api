# POOL MANAGER — keeps N pre-warmed workers alive (replaces the dispatcher)

Workers self-claim request files from `queue/` via atomic `mv`. You are NOT
in the request hot path: you never touch `queue/`, `processing/`, or
`responses/` files, and you never read request/response bodies. Your only job
is keeping the pool populated.

## Startup

1. Start a heartbeat loop in the background:
   `nohup bash -c 'while true; do date +%s > ~/workspace/agent-api/manager.heartbeat; sleep 20; done' >/dev/null 2>&1 &`
2. Spawn `POOL_SIZE=3` workers via `subagent.spawn`, one per message (generate
   a fresh id per worker, e.g. `w_9f3k2a`):
   "You are pool worker <id>. Read ~/workspace/agent-api/POOL_WORKER.md and follow it exactly, starting with Startup. Your worker id is <id>."
3. Note the ids you spawned. Your context holds ids only — never file bodies.

## Main loop

- On every worker completion handoff: spawn exactly one replacement worker
  with a fresh id.
- Every ~5 minutes: list `~/workspace/agent-api/workers/*.heartbeat`. For
  each id you spawned that has no handoff yet and whose heartbeat is missing
  or older than 10 minutes, spawn one replacement (then forget the dead id —
  do not double-spawn).
- If the pool is ever completely empty (no live workers, no heartbeats):
  spawn a fresh set of 3 immediately.
- Never claim queue files yourself, even if the pool is empty and you feel
  helpful — spawn workers instead.

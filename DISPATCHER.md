# DISPATCHER — long-lived queue router (run by a persistent subagent)

You are the dispatcher for the agent-api. Your job is simple and eternal:
watch `~/workspace/agent-api/queue/` for new request files, and spawn exactly
one stateless worker subagent per file. You never process requests yourself.

## Startup

1. Start a heartbeat loop in the background so the watchdog can tell you're alive:
   `nohup bash -c 'while true; do date +%s > ~/workspace/agent-api/dispatcher.heartbeat; sleep 20; done' >/dev/null 2>&1 &`
   NOTE: this loop is detached and proves nothing by itself. Your REAL proof
   of life is the scan log (step 1 of Main loop) — only a living agent writes it.
2. Start the queue watcher as a background `muse.exec` session — PLAIN, no
   `nohup`, no `&`, NO output redirect. Its stdout must stream to the session
   so your `process.poll` in Main loop step 1 can see the `NEW:` lines:
   ```bash
   cd ~/workspace/agent-api/queue && declare -A seen; while true; do for f in *.json; do [ "$f" = "*.json" ] && continue; if [ -z "${seen[$f]}" ]; then seen[$f]=1; echo "NEW:$f"; fi; done; sleep 1; done
   ```
   (Run it with `muse.exec` in background mode so it keeps running AND its
   output streams to that session. If you detach it with nohup or redirect
   its output to a file, you will be blind — do not do that.)
   There is no inotify on this VM, hence the 1-second bash poll loop.

## The one rule that matters most

**Your turn NEVER ends.** A tool result arriving — including the completion of
a background launcher — is NOT task completion. There is no final summary to
write, ever. If you catch yourself composing a concluding message, stop:
you are wrong, go back to `process.poll`. The ONLY ways this role ends are
step 4 (the ~50 minute rotation) or an explicit shutdown from your parent.

## Main loop

Forever:
1. `process.poll` the watcher session with a ~45s timeout and read new output.
   After EVERY poll, prove you are alive: `date +%s >> ~/workspace/agent-api/dispatcher.scanlog`
   (The watchdog watches this file — not the heartbeat — to know the AGENT is alive.)
2. For each `NEW:<file>` line:
   - Atomically claim it: `mv ~/workspace/agent-api/queue/<file> ~/workspace/agent-api/processing/<file>`
     (if the mv fails the file is already claimed — skip it).
   - Spawn ONE worker subagent via `subagent.spawn` with this message (fill in the filename):
     "Read ~/workspace/agent-api/WORKER_PROMPT.md and follow it exactly. Your request file is ~/workspace/agent-api/processing/<file>. Your only input is that file — ignore all other context."
   - Do NOT read the request file yourself. Do not put request or response
     content in your context — filenames only. This keeps you lean forever.
3. Response guarantee: when a worker's completion handoff arrives, pull the
   request id from the `processing/<id>.json` path in your original spawn
   message, then verify `~/workspace/agent-api/responses/<id>.json` exists
   and is valid JSON (`python3 -c "import json;json.load(open('...'))"`).
   If it is missing or invalid, wait 15 seconds and check once more; if still
   bad, write a fallback response yourself so the client never hangs:
   ```bash
   python3 - <<'EOF'
   import json, time
   rid = "<request_id>"
   resp = {"id": rid, "object": "response", "created_at": int(time.time()),
           "model": "muse-spark", "status": "failed",
           "error": {"code": "worker_failed",
                     "message": "worker did not produce a response"},
           "output": [],
           "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
   json.dump(resp, open(f"/home/hatch/workspace/agent-api/responses/{rid}.json", "w"))
   EOF
   ```
   (The server's long-poll timeout is the final backstop, but this keeps
   failures fast and well-formed.)
3. If the watcher process ever dies, restart it (step 2 of Startup).
4. If you have been running for more than ~50 minutes OR your context feels
   heavy: stop spawning, wait until `ls ~/workspace/agent-api/processing/` shows
   no unanswered files older than your oldest in-flight spawn... simpler: just
   exit cleanly. The watchdog cron will respawn a fresh dispatcher within
   5 minutes. (Unclaimed files stay in `queue/` and will be picked up.)

## Notes

- Worker completions arrive as handoffs; you can ignore them — workers write
  their own response files. Your only bookkeeping is: one spawn per file.
- Never write to `responses/` yourself.
- If the same filename appears twice, spawn only once (the `seen` map + atomic
  mv handle this).

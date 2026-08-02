---
name: blocking-wait-handoff
description: Strongly prefer this skill whenever Codex is blocked by an already-running local or remote process expected to keep blocking progress for more than five minutes and should continue automatically when that exact process exits. Use for long training, evaluation, build, download, or data-processing jobs identified by PID or process pattern instead of holding the current turn open or manually polling. Do not use for short waits or commands that have not started.
---

# Blocking Wait Handoff

Run the bundled script from this skill directory. Let it enforce process identity, owner routing,
fork deduplication, delivery ordering, and crash fencing; do not reproduce those mechanisms with
ad hoc shell loops or Codex APIs.

## Standard workflow

1. Confirm that the target is already running and blocks the next useful step for at least about
   five minutes. Prefer its exact PID; use a process pattern only when PID capture is impractical.
2. Inspect existing waits and the current delivery route:

   ```bash
   python3 scripts/codex_wait_handoff.py active --include-stale --json
   python3 scripts/codex_wait_handoff.py doctor --json
   ```

3. Schedule exactly one wait using the default `--resume-protocol auto`. Keep the default
   20-second preflight unless there is a concrete reason to change it.
4. Read the returned task record. At minimum report `task_id`, `event_id`, `owner_thread_id`,
   `job_scope_id`, `resume_protocol`, `delivery_branch`, `authority_strength`,
   `will_wake_idle_thread`, `native_at_most_once`, and `strict_exactly_once`.
5. If preflight reports that the target already exited, inspect its result now instead of handing
   off. If scheduling succeeds with native delivery, tell the user that the watcher owns the wait
   and end the current turn; do not keep polling. If it selects marker delivery, explain that no
   idle wake will occur and leave the owner-bound event pending for a later exact-owner claim.

## Schedule a wait

Prefer an exact local PID:

```bash
python3 scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 1800 \
  --pid 12345 \
  --note "Inspect the completed job and continue the blocked task."
```

For an already-running process on a remote host:

```bash
python3 scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 2400 \
  --host <ssh-destination> \
  --pid 12345
```

When an exact PID is unavailable, quote a precise pattern and review the initial matches:

```bash
python3 scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 1800 \
  --pattern '<precise-process-pattern>'
```

The pattern is snapshotted once; later matching processes do not join the job. Add
`--max-wait-seconds` when the default two-hour deadline is too short. Add `--observed-log` only for
an existing useful log. Use a fresh `--job-id` only for an intentional later monitoring cycle of
the same still-live process incarnation.

Never add `--allow-short-test` outside an explicit handoff test. Never add
`--allow-weak-authority` unless the user deliberately accepts endpoint reuse, daemon restart, and
proxy-backend ambiguity. Embedded and private-stdio owners remain marker-only even with that flag.

## Interpret delivery outcomes

| Outcome | Required behavior |
| --- | --- |
| `native-message` | Expect one owner-bound continuation through the ticketed app-server. Existing subscribed Terminal, Desktop, or Remote clients receive that owner's normal stream. End the current turn after the startup ACK. |
| `marker` | Expect no idle wake. Keep the event pending until the exact durable owner inspects and claims it; marker stdout and later model input are separate transactions. |
| `BLOCKED` or rejected schedule | Report the explicit reason. Do not bypass routing, authority, or process-identity checks with another delivery mechanism. |
| `UNKNOWN` | Assume delivery may have crossed its boundary. Never resend, reschedule the same event, switch it to marker, or cancel it. Use positive reconciliation only. |
| `ACCEPTED` | Treat the job reservation as a permanent tombstone for that logical job and process incarnation. |

Native delivery is deliberately at-most-once after an ambiguous boundary, not strict exactly once.
`clientUserMessageId` is correlation evidence, not a documented server-side idempotency key.

## Common operations

Inspect one task or all live watchers:

```bash
python3 scripts/codex_wait_handoff.py status --task-id <task-id> --json
python3 scripts/codex_wait_handoff.py active --include-stale --json
```

Inspect and claim marker delivery only from its exact current owner task:

```bash
python3 scripts/codex_wait_handoff.py pending \
  --owner-thread-id "$CODEX_THREAD_ID" --json

python3 scripts/codex_wait_handoff.py claim \
  --task-id <task-id> \
  --owner-thread-id "$CODEX_THREAD_ID" --json
```

Treat the returned `resume_prompt` as later input to that owner. Never claim from a sibling fork,
side conversation, or subagent.

Cancel or stop only through the script; let it decide whether the event is still safely
cancellable:

```bash
python3 scripts/codex_wait_handoff.py cancel --task-id <task-id> --json
python3 scripts/codex_wait_handoff.py stop --task-id <task-id> --json
```

Use `stop --also-stop-target` only when the user also intends to terminate the captured target
process. For a crashed watcher or ambiguous native delivery, use the explicit repair paths:

```bash
python3 scripts/codex_wait_handoff.py recover --task-id <task-id> --json
python3 scripts/codex_wait_handoff.py reconcile --task-id <task-id> --json
```

Reconciliation may change native `UNKNOWN` to `ACCEPTED` only when exact persisted history contains
the event's client ID. Absence from history is inconclusive.

## Non-negotiable rules

- Never use goals, `codex exec resume`, `thread/resume`, or a newly loaded app-server as delivery
  fallbacks.
- Require a verified actor-to-owner route in every protocol. Never redirect a wait across ordinary
  forks, into a subagent, or out of a side conversation.
- Keep each wait bound to the schedule-time process incarnation. Never follow a reused PID or rerun
  a pattern to discover new targets.
- Let `auto` choose between strong native delivery and marker. Treat explicit or network endpoints
  as weak unless the script proves the exact ancestor Unix listener and live owner process.
- Never retry or cancel `READY`, `SUBMITTING`, `UNKNOWN`, marker-pending, or accepted delivery by
  hand. Use the script and preserve its monotonic outcome.
- Never promise strict exactly-once execution, global ordering against ordinary user messages, or
  live streaming when no first-party client is subscribed.

## Load details only when needed

Do not read the reference files for an ordinary successful schedule.

- Read [protocol v3](references/protocol-v3.md) when explaining guarantees, diagnosing routing or
  ledger state, handling marker/UNKNOWN/recovery/rebind, or modifying protocol behavior.
- Read the [Codex source audit](references/source-audit.md) when investigating app-server versions,
  Remote Control, streaming topology, history APIs, or idle-start side effects.
- Read the [P0 race audit](references/race-fencing.md) before changing cancellation, watcher
  startup, replay, submission, recovery, or terminal-state logic.
- Read all three completely before maintaining the implementation, then run the repository test
  suite.

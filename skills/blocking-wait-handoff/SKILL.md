---
name: blocking-wait-handoff
description: Hand off a genuinely blocking local or remote process wait to a detached watcher, fence it to the exact process incarnation and durable Codex owner across forks and subagents, and continue through the owner's attachable app-server or an explicit marker. Use for critical-path waits expected to exceed five minutes after a precise PID or process-pattern target has already started.
---

# Blocking Wait Handoff

Apply this skill only when an already-running process blocks the next step for about five minutes
or longer. Prefer an exact PID. Use `--allow-short-test` only to test the handoff mechanism.

For protocol interpretation or maintenance, read
[protocol v3](references/protocol-v3.md), the
[Codex source audit](references/source-audit.md), and the
[P0 race audit](references/race-fencing.md). These references are part of the installed skill;
treat them as canonical rather than looking for repository-level `docs/` siblings.

## Inspect before scheduling

Run from this skill directory:

```bash
python3 scripts/codex_wait_handoff.py active --include-stale --json
python3 scripts/codex_wait_handoff.py doctor --json
```

Check at least:

- `actor_thread_id` and `owner_route.owner_thread_id`;
- `owner_route.route`, `route_verified`, `job_scope_id`, and `fork_lineage`;
- `app_server_context`, `native_message_ready`, `authority_strength`, and `authority`;
- `native_message_protocol`, `strict_exactly_once_protocol`, and `version_skew`.

Require a verified complete route, an already-loaded owner, and a `strong` authority for
auto-selected native delivery. Strong means an exact ancestor Unix listener with both its connected socket inode
and a live app-server process incarnation. Never cold-load a thread into another app-server. Never use goals,
`codex exec resume`, or `thread/resume` as delivery fallbacks.

Reject every unverified actor-to-owner route, including marker delivery. Without verified
`forkedFromId` and `parentThreadId` ancestry, separate branches could create separate job
registries and trigger the same logical completion more than once.

Use the `delivery_branch` emitted by both `doctor` and `schedule`. Apply this matrix without an
implicit fallback:

| Route and owner authority | `auto` | Explicit native |
| --- | --- | --- |
| Route unverified | Reject | Reject |
| Verified, private stdio or Embedded | Marker | Reject |
| Verified, attachable but owner not loaded | Marker | Reject |
| Verified, loaded, exact ancestor Unix + inode + live process | Strong native | Strong native |
| Verified, loaded, WS/WSS, alias, or non-ancestor endpoint | Marker | Reject unless weak authority is explicitly accepted |

Allow explicit marker only after route verification. Remember that marker is a manual owner-side
claim and never wakes an idle task.

## Bind the authority

Use an attachable ancestor listener when discovery proves one. Otherwise use the managed daemon
Unix socket only to read persisted routing metadata; never promote it into the owner of a private
Desktop/IDE or Embedded task. For an explicit remote TUI or another listener, pass the exact
endpoint to both `doctor` and `schedule`:

```bash
--app-server-endpoint 'unix:///absolute/path/app-server.sock'
--app-server-endpoint 'ws://127.0.0.1:4500/'
--app-server-endpoint 'wss://host.example:443/'
```

For authenticated WS/WSS, put the bearer token in an environment variable and pass only its name:

```bash
--app-server-auth-token-env CODEX_WAIT_REMOTE_TOKEN
```

Keep the referenced variable in the detached watcher's environment. Never persist credentials in
the endpoint, prompt, task JSON, or shell history. The audited app-server listener binds plain
`ws://`; a TUI may use a `wss://` connect endpoint through a private TLS proxy that forwards to
that listener. Prefer authenticated TLS on a private network, VPN/mesh, or SSH tunnel. Never expose
the app-server directly to the public Internet.

Treat the authority ticket as a capability. Bind the canonical endpoint and transport, Unix
device/inode fingerprint when applicable, initialization identity, exposed Remote Control
identity, credential reference, authority strength, owning process identity when strong, and
authority epoch. Re-probe a strong owning process before every attempt and immediately before the
continuation request. Reject every mismatch and every owner without positive loaded evidence.
Persist only the owning process PID/start-token fence, never its command or argv.

Classify all WS/WSS endpoints and every explicit/configured endpoint without exact ancestor
provenance as `weak`. Endpoint, TLS, and Remote Control installation/environment identity do not
provide a per-app-server-process nonce. A weak ticket identifies only the selected endpoint
believed to host the owner; it does not prove backend-instance continuity. `auto` must choose
marker for weak authority; explicit native must fail unless the user deliberately adds
`--allow-weak-authority`. Persist that opt-in as `weak_authority_accepted=true` in the ordered
ledger authority descriptor and explain that it accepts daemon restart, endpoint reuse, and proxy
backend-change risk. Dispatch and reconciliation must read this policy from their current ledger
entry, never from a possibly stale task mirror. The flag must never turn Embedded or private stdio
into an attachable authority.

When a configured client endpoint differs from an attachable ancestor listener, retain the original
ancestor endpoint, set `endpoint_matches_ancestor=false`, and allow the alias to be probed only as
weak authority. This includes `ws://` to `wss://` aliases and mismatched Unix endpoints.

## Resolve owner and job scope

Treat all delivery IDs as Codex `thread.id` values. Never use tree-level `sessionId` as a delivery
address or lock key.

- Let a durable top-level task or ordinary fork own delivery scheduled from itself.
- Keep an existing wait bound to its owner when the user later forks or reruns another branch.
- Walk `forkedFromId` to the earliest durable branch origin and use it as `job_scope_id`. Let the
  first ordinary branch that reserves a logical job own that job; reject the same job in sibling
  forks.
- Treat a spawned subagent as the actor. Verify every `parentThreadId` hop and deliver to the
  durable root of that agent tree.
- When the agent-tree root is an ordinary fork, deliver to that fork while retaining the common
  original-fork lineage as the duplicate-job scope.
- Reject explicit subagent resume-self. A child has no call-stack return destination and current
  Multi-Agent V2 children reject direct app-server input.
- Reject `/side`, even with an explicit parent. Current persisted `thread/read` metadata cannot
  prove the side conversation's durable parent.
- Fail closed on incomplete or cyclic parent/fork ancestry. Never fall back to the child or another
  branch.

## Bind the process incarnation

Let scheduling capture the exact incarnation before preflight and before detaching. Treat PID,
pattern, command, PPID, and current state as observations; use scope, host, PID, start-token source,
and start token as immutable identity.

Support strict binding only when a strong start token exists:

- use Linux boot ID plus `/proc/<pid>/stat` start ticks locally or through the remote helper;
- use local macOS `libproc` microsecond start time;
- reject remote macOS and generic Unix when only second-resolution `ps lstart` is available.

Prefer `--pid`. If using `--pattern`, review the initial matches. The scheduler snapshots every
matching non-zombie incarnation, excludes its helper and complete ancestor chain, then follows only
that fixed set. Do not rediscover later pattern matches. The remote helper receives the pattern on
stdin so it cannot match its own SSH/shell argv.

Treat an identity mismatch as exit of the original process, never as authority to act on the
replacement. Treat inspection failure as `unknown` and fail closed.

Remember the signal TOCTOU boundary: local Linux termination is handle-safe only when Python
exposes pidfd open and send operations. macOS, remote shell signaling, and fallback Linux signaling
can revalidate immediately before `kill` but cannot make that check and PID signal one kernel
transaction.

## Schedule the wait

Prefer an exact local PID:

```bash
python3 scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 1800 \
  --pid 12345 \
  --note "Inspect the outputs and continue the blocked task."
```

For a remote Linux target:

```bash
python3 scripts/codex_wait_handoff.py schedule \
  --blocking \
  --expected-seconds 2400 \
  --host <ssh-destination> \
  --pid 12345
```

Reject an SSH destination that begins with `-`, contains whitespace, or contains control
characters. Add `--observed-log` only for an existing useful process log.

Keep the default 20-second preflight unless a concrete reason requires a change. If the target
exits during preflight, inspect the result now instead of handing it off. Treat maximum-wait expiry
as a timeout continuation, not successful process completion.

Use the default logical job ID for one wake during a process lifetime. Supply a fresh explicit
`--job-id` only for an intentional later monitoring cycle of the same live incarnation:

```bash
--job-id evaluation-cycle-2
```

Do not reuse an earlier cycle ID. An `ACCEPTED` job reservation is a permanent tombstone for that
job key across ordinary forks and across native/marker protocols.

## Interpret the result and task record

Read and report:

- `task_id`, `event_id`, and `client_user_message_id`;
- `actor_thread_id`, `owner_thread_id`, `owner_route`, and `job_scope_id`;
- `logical_job_id`, `job_key`, and `job_reservation_generation` when present;
- `resume_protocol`, `delivery_branch`, `authority`, `authority_strength`, and `fifo_generation`;
- `will_wake_idle_thread`, `native_at_most_once`, and `strict_exactly_once`; and
- `protocol_fallback_reason`.

End the current turn expecting an independent wake only for `native-message`. A marker requires an
owner-side poll, claim, and explicit insertion as Codex input.

## Preserve the two ledgers and common fence

Keep native and marker FIFO ledgers separate:

```text
native ledger key = owner_thread_id
marker ledger key = marker:<owner_thread_id>
common job registry key = fork-lineage job_scope_id
```

Assign sequence when an event becomes `READY`, not when it was scheduled. Submit or claim only the
earliest unresolved entry in that protocol ledger. Never describe native and marker events as one
cross-protocol FIFO.

Use the common registry to prevent one logical job from registering in both protocols or in sibling
forks. Also use its `delivery_owner_id` gate to forbid unresolved native and marker events for the
same exact owner: `ACTIVE` or `UNKNOWN` in one protocol blocks registration in the other. Allow a
different job to switch protocol only after the prior owner events become definitive
`ACCEPTED`, `BLOCKED`, or `CANCELLED`; keep an unreconciled `UNKNOWN` closed. Do not apply that
protocol gate across distinct ordinary-fork owners.

Treat `ACTIVE`, `UNKNOWN`, and `ACCEPTED` as same-job deduplicating states. Permit the same job key
again only after `BLOCKED` or `CANCELLED`, or use a genuinely new logical cycle ID.

Commit the protocol ledger first at terminal transitions, then converge the common registry. Use
exact task ID, event ID, reservation token, protocol generation, job generation, and authority
epoch to repair a crash between those writes. Never infer a replay opportunity from a stale task
JSON mirror.

## Apply native active/idle delivery

Let the dispatcher first read the positively loaded owner's metadata with
`thread/read(includeTurns=false)` and branch on `historyMode`. For legacy history, load turns with
`thread/read(includeTurns=true)`. For paginated history, never request turns through `thread/read`;
use experimental `thread/turns/list` instead.

- For exactly one regular `inProgress` turn, send `turn/steer` with that exact
  `expectedTurnId` and `clientUserMessageId`. Treat the matching successful RPC response as
  synchronous acceptance.
- With no active turn, send `turn/start`, then poll the same authority's persisted history until the
  matching `userMessage.clientId` appears. For paginated history, poll only the newest descending
  `thread/turns/list(itemsView=full)` page; the just-submitted message must be there. Do not rely on
  notifications on the watcher's fresh, unsubscribed connection.
- For an active state without exactly one in-progress turn, submit nothing and fail closed.
- On an explicit no-active/mismatched-turn or Review/Compact rejection, return the event to `READY`
  with its original sequence, re-read state, and retry. Use the separate durable state-collision
  budget (900 one-second retries by default); do not consume the general authority retry budget.
  Let exhaustion become pre-submission `BLOCKED`. Use `--state-collision-max-attempts 0` only for
  an intentional unlimited wait.
- On a permanent input rejection, terminate as `BLOCKED`.
- Once a request may have started, convert every timeout, disconnect, lost response, or failed
  post-`turn/start` history read to `UNKNOWN`. Never resend automatically.

Reconcile native `UNKNOWN` only when the same authority's persisted history positively contains
the client ID. Paginated reconciliation follows every history cursor because the event may be old.
Absence is inconclusive and must leave later native events blocked.

Disclose the idle side effect: public `turn/start` writes the watcher's app-server `clientInfo` and
form-elicitation capability into the loaded thread before input. The watcher uses the official
secondary `codex_app_server_daemon/2.0.0` identity, so it does not change the process-global
originator, but it still replaces prior thread client metadata, sets OpenAI-form support to
`false`, and may refresh MCP capability state. It does not subscribe to thread events.
`turn/steer` does not perform that update.

Never describe this as strict exactly-once. `clientUserMessageId` is observable but is not a
documented idempotency key, and normal user messages are ordered by actual app-server arrival.

## Use marker fallback honestly

Let `auto` choose marker for Terminal Embedded, Desktop/IDE private stdio, or an unloaded owner.
Reject an unverified actor-to-owner route in every protocol. Marker does not wake an idle task.

Check and claim only from the exact current owner:

```bash
python3 scripts/codex_wait_handoff.py pending \
  --owner-thread-id "$CODEX_THREAD_ID" --json

python3 scripts/codex_wait_handoff.py claim \
  --task-id <task-id> \
  --owner-thread-id "$CODEX_THREAD_ID" --json
```

`CODEX_THREAD_ID` is mandatory for `claim`. An explicit `--owner-thread-id` is only a cross-check;
it never substitutes for proof that the command is running inside that exact current owner.

Treat `resume_prompt` as input to the current owner. Do not claim from a sibling fork or subagent.
Explain that claim stdout and subsequent Codex/model input are separate transactions. A crash
after stdout but before ledger `ACCEPTED` becomes marker `UNKNOWN` and must never replay. Marker
`ACCEPTED` proves claim output, not model consumption.

Do not claim that this repository ships a heartbeat; it does not. If the user separately configures
a first-party Codex automation, target only this exact durable owner task, explain that it is
periodic polling, have it inspect and claim the event, insert the returned prompt, then disable
itself. Never create one automation per fork or target a subagent.

## Preserve native streaming boundaries

Inject into the strongly proven local owner or, after explicit opt-in, the selected weak endpoint
believed to host the owner. Let that app-server publish its normal incremental `turn/*`, `item/*`,
and delta stream only to first-party clients that are currently connected and subscribed to this
task. Do not call `thread/resume` or another subscribing API from the watcher, and do not implement
or impersonate the Remote Control relay. Remote visibility also requires the host to remain awake
and online, the account signed in, and the host app running. With zero subscribers, promise no live
stream: rely on persisted history and let pending approvals wait for a later first-party resume
instead of attaching the watcher as their handler.

- Use strong native delivery for a managed-daemon Terminal only through its exact ancestor Unix
  socket plus live app-server process incarnation.
- Treat explicit Unix and all WS/WSS remote endpoints as weak. Default to marker; use native only
  after explicit `--allow-weak-authority` and with the exact connect endpoint and credential
  reference. Preserve a distinct ancestor listener endpoint for `ws://` to `wss://` aliases; the
  alias remains weak and has no app-server-instance fence.
- For Desktop SSH projects, inspect the process ancestry on the SSH host. The audited Desktop
  implementation starts `app-server --listen unix://` there and reaches it through
  `app-server proxy`; when the watcher is a descendant of that listener and proves its exact Unix
  socket inode plus process incarnation, use strong native delivery. Treat older or customized
  private-stdio SSH topologies as marker-only. Never infer an attachable endpoint merely because
  Desktop can stream.
- Use marker only, unconditionally, for Embedded or private stdio owners.
- Never infer common authority from a shared `thread_id` or rollout directory.

## Cancel, recover, and hand off safely

Allow cancellation only after the watcher has durably stored its own strong process identity and
entered `WATCHING`, and only while the event remains pre-ready. Signal only that persisted watcher
incarnation. On missing, malformed, weak, reused, or unprobeable watcher identity, retain the
reservation and fail closed.

Never cancel `READY`, `SUBMITTING`, `UNKNOWN`, marker pending/claiming, or accepted delivery. Run
`stop --also-stop-target` only through the script; let it commit ledger cancellation before
signaling captured target incarnations.

Enter hidden `watch` only from `scheduled` or `watching` with the current guard, token, and
generation. Reject event-staged, delivery, unknown, accepted, blocked, cancelled, and terminal
replays.

Use explicit recovery:

```bash
python3 scripts/codex_wait_handoff.py recover --task-id <task-id> --json
python3 scripts/codex_wait_handoff.py reconcile --task-id <task-id> --json
```

Preserve the original maximum-wait deadline and submission-deferral retry budget across watcher
restart. Recover an interrupted submission as `UNKNOWN`, never as `READY`.

Freeze native delivery before a manual authority switch within the same coordination store:

```bash
python3 scripts/codex_wait_handoff.py freeze \
  --owner-thread-id "$CODEX_THREAD_ID" \
  --expected-epoch <current-epoch> --json
```

Require no `SUBMITTING` or `UNKNOWN` event. Keep every watcher and command on the same underlying
task files, native ledger, common job registry, and stable lock files/inodes. Equal absolute path
strings, copied files, or a newly mounted independent store do not satisfy this requirement. Never
copy or move live state as a migration protocol. From the exact owner in that same coordination and
lock domain, verify the replacement attachable authority and compare-and-swap rebind:

```bash
python3 scripts/codex_wait_handoff.py rebind \
  --owner-thread-id "$CODEX_THREAD_ID" \
  --expected-epoch <old-epoch> \
  --app-server-endpoint '<new-endpoint>' --json
```

Apply the same authority-strength policy during rebind. A weak destination needs a fresh explicit
`--allow-weak-authority`; rebind does not make an endpoint stronger. The epoch CAS atomically
updates authority and weak-policy fields in all pending ledger entries. Do not bulk-rewrite task
JSON after rebind; a live watcher may concurrently own it.

Treat the epoch CAS as a local ledger fence, not an app-server submission precondition. Rebind does
not move watchers or credentials, retire an old authority, integrate Codex product handoff, or
provide strict cross-host migration. If every participant cannot continue using the same
coordination and lock store, keep the ledger `DRAINING` and report the switch as blocked.

# Protocol v3

Protocol v3 is a host-local coordination protocol for observing an exact process incarnation and
publishing one continuation event to its immutable durable Codex owner. It prefers an
authority-bound native app-server message and otherwise exposes an owner-bound marker.

It does not use Codex goals, `codex exec resume`, or `thread/resume` as a delivery mechanism.

## Contents

- [Claims and non-claims](#claims-and-non-claims)
- [Identity model](#identity-model)
- [Routing and branch policy](#routing-and-branch-policy)
- [Authority binding](#authority-binding)
- [Durable coordination layers](#durable-coordination-layers)
- [Scheduling transaction](#scheduling-transaction)
- [Watcher and completion publication](#watcher-and-completion-publication)
- [Native delivery](#native-delivery)
- [Marker delivery](#marker-delivery)
- [Process-incarnation protocol](#process-incarnation-protocol)
- [Cancellation and crash recovery](#cancellation-and-crash-recovery)
- [Authority rebind within one coordination store](#authority-rebind-within-one-coordination-store)
- [Surface and streaming behavior](#surface-and-streaming-behavior)
- [Required upstream primitive](#required-upstream-primitive)

## Claims and non-claims

For one immutable event reservation, v3 provides:

- one durable process-incarnation snapshot before watcher detachment;
- one verified actor-to-owner route;
- one protocol-ledger entry fenced by task ID, event ID, random token, generation, and authority
  epoch;
- one job-registry entry shared across native/marker modes and ordinary fork lineage;
- ready-order FIFO within the selected delivery protocol and owner;
- at most one automatic submission that might have been accepted after the request boundary; and
- monotonic cancellation, ambiguity, acceptance, and watcher-replay fencing.

It does not provide strict exactly-once delivery or execution. In particular:

- `clientUserMessageId` is observable but is not a documented server-side idempotency key;
- a response can be lost after the app-server accepts input;
- native and marker ledgers have no common cross-protocol FIFO, so an exact-owner gate forbids
  unresolved mixing of those protocols;
- marker output and its later use as Codex input are not one transaction;
- normal user messages race by their actual app-server arrival order;
- public `turn/start` has thread client/capability side effects; and
- product host handoff does not atomically move this protocol's files, watchers, or authority epoch.

The conservative outcome after ambiguity is `UNKNOWN`, never automatic resend. This trades
liveness for duplicate avoidance.

## Identity model

### Event identity

Every scheduled wait creates immutable values:

```text
task_id
event_id
client_user_message_id = "codex-wait-handoff:" + event_id
reservation_token
protocol ledger generation
job registry generation
```

`task_id` addresses the local task record. `event_id` is the logical completion event.
`client_user_message_id` correlates native input with a persisted `userMessage.clientId`.
The random token and generations fence stale processes and partial crash recovery.

### Actor, owner, and job scope

```text
actor_thread_id  = thread executing schedule
owner_thread_id  = durable branch that receives the continuation
job_scope_id     = earliest verified ordinary-fork lineage origin
```

All three are Codex thread IDs. A Codex `sessionId` may group a tree but is never a delivery
address, authority identity, ledger key, or lock key.

### Logical job identity

The default `logical_job_id` is `process-lifetime`. The job key is a hash of:

```text
job_scope_id
logical_job_id
sorted immutable process incarnations:
  version, scope, host, pid, start-token source, start token
```

Pattern text, command, PPID, current state, and discovery syntax are excluded. Finding the same
incarnation by PID or by pattern therefore produces the same default job identity. PID reuse or a
fresh process start produces a different identity.

Use a fresh explicit `--job-id` to create an intentional later monitoring cycle for the same live
incarnation. Reusing an earlier logical ID reuses its duplicate fence.

## Routing and branch policy

### Durable task or ordinary fork

A durable top-level task and an ordinary fork own themselves. Cross-thread retargeting is rejected.
A wait scheduled before a later fork remains bound to its original owner.

The scheduler follows `forkedFromId` from the owner to the earliest durable origin. Every ordinary
fork in that lineage shares `job_scope_id` and therefore a common job registry. For one logical
job/process incarnation, the first branch whose registry reservation commits is the owner branch;
sibling forks cannot schedule a duplicate in either delivery protocol.

This does not merge the branches. Distinct jobs can still be owned independently by distinct
forks, and each continuation is delivered only to its recorded owner.

### Nested subagent

A spawned subagent is an actor, not a delivery owner. The scheduler follows and verifies every
`parentThreadId` to the durable root of that agent tree. A nested child therefore routes to:

- the durable top-level task when that task launched the subagent tree; or
- the durable ordinary fork when that fork launched the tree.

In the latter case, the fork is `owner_thread_id`, while the original ordinary-fork lineage root
remains `job_scope_id`. Explicit resume-self is rejected because current Multi-Agent V2 children do
not accept this direct input path and child completion is not a call-stack return.

### Side conversation

An ephemeral `/side` conversation is rejected even if the caller supplies a parent ID. Current
persisted `thread/read` metadata does not preserve enough durable fork ancestry for a detached
watcher to prove the destination. The user must return to the durable parent and schedule there.

Any partial read, missing ancestor, cycle, ephemeral lineage crossing, or depth overflow fails
closed for both native and marker delivery. An unverified route cannot prove the shared
`job_scope_id`, so allowing it would permit sibling forks to register duplicate events.

Marker `claim` additionally requires `CODEX_THREAD_ID` from the current Codex task. Supplying an
explicit owner ID cannot replace it; when supplied, the explicit ID, current thread ID, and task's
durable owner ID must all match.

## Authority binding

Native delivery is valid only through the ticketed app-server that already has the owner loaded.
A strong ticket proves an exact local owner instance; a deliberately accepted weak ticket selects
an endpoint believed to host the owner without proving backend-instance continuity. A ticket
records:

- canonical listener and client connect endpoints, which may be distinct `ws://` and `wss://`
  addresses, plus transport;
- Unix socket device/inode fingerprint where applicable;
- initialize response identity such as Codex home and platform;
- public Remote Control installation/environment identity when exposed;
- a bearer credential environment-variable reference, never the credential; and
- `authority_strength`, its reason, provenance, and the owning app-server process incarnation for
  a strong local ticket; and
- `authority_epoch`.

Only this combination is `strong`: the listener is the exact local app-server ancestor, its
transport is Unix, the connected socket has a device/inode fingerprint, and the ancestor has a
live Linux `/proc` or macOS `libproc` start token. The dispatcher validates that process
incarnation before connecting and again immediately before `turn/start` or `turn/steer`.
The durable incarnation fence stores only version, scope, host, PID, source, and start token;
process command/argv and other discovery metadata are deliberately excluded from the ticket.

Every WS/WSS endpoint is `weak`: endpoint/TLS identity and public Remote Control
`installationId`, `environmentId`, and `serverName` may survive daemon restart or identify a proxy
rather than one backend process. An explicit/configured Unix endpoint without ancestor provenance
is also weak. `auto` chooses marker for weak authority; explicit native fails unless
`--allow-weak-authority` was supplied and durably recorded as
`weak_authority_accepted=true` in the ledger authority descriptor. That opt-in permits native
injection into the selected endpoint but cannot guarantee instance-exact routing. Dispatchers and
reconcilers use the current entry authority, not a task-file mirror that can be stale after rebind.
It never overrides `attachable=false` for Embedded or private stdio owners.

Before every native attempt, the dispatcher reconnects, rebuilds the descriptor, compares it with
the ticket, calls `thread/loaded/list`, reads the owner, and requires positive loaded evidence. It
never calls `thread/resume`. `thread/read` observes stored/live state without resuming the task.

A matching `thread_id` in another app-server is insufficient. Endpoint, fingerprint, descriptor,
loaded owner, process incarnation for strong tickets, and epoch must agree. Strict network
authority requires a future server-issued per-boot instance ID and an atomic submission
precondition. The audited app-server WebSocket listener binds plain `ws://`, while a TUI may use a
`wss://` connect endpoint through a private TLS proxy. Such an alias cannot prove equivalence to the
ancestor listener, so the ticket retains the original `ancestor_endpoint` and records
`endpoint_matches_ancestor=false`. It may be probed and used under the weak-authority opt-in, but
`auto` selects marker. Prefer authenticated TLS on a private network, VPN/mesh, or SSH tunnel; do
not expose the app-server directly to the public Internet. A mismatched Unix endpoint follows the
same rule and cannot inherit the ancestor's strong provenance. Private stdio and Embedded contexts
remain non-attachable instead of being converted into aliases.

## Durable coordination layers

### Protocol ledgers

Each owner has two independent `HandoffLedger` namespaces:

```text
native ledger key = owner_thread_id
marker ledger key = "marker:" + owner_thread_id
```

Both use a stable `.lock` inode with `flock` and an atomically replaced `.json` record. Each assigns
`ready_sequence` only when an event becomes `READY`. The earliest unresolved sequence is the sole
event allowed to enter delivery.

Native and marker are deliberately separate because marker claiming cannot be serialized with the
app-server's message queue. Different jobs split across the two protocols have no global order.
Therefore the common job registry, described below, forbids one exact delivery owner from having
unresolved events in both ledgers simultaneously. It also prevents the same logical job from
appearing once in each ledger anywhere in the fork lineage.

### Protocol-ledger state machine

```text
SCHEDULED -> WATCHING -> READY -> SUBMITTING -> ACCEPTED
                                      |            ^
                                      |            |
                                      +-> UNKNOWN -+  positive native history only
                                      |
                                      +-> BLOCKED
                                      |
                                      +-> READY        explicit non-acceptance only

SCHEDULED/WATCHING -> CANCELLED       fenced cancellation only
```

- `SCHEDULED`: both reservations exist; no watcher has durably acknowledged ownership.
- `WATCHING`: the guarded watcher and its exact process identity are durable.
- `READY`: final prompt and digest are durable; immutable ready sequence assigned.
- `SUBMITTING`: this event owns the delivery right before any output/request boundary.
- `ACCEPTED`: native input received positive acceptance evidence, or marker claim output flushed.
- `UNKNOWN`: delivery may have occurred; no automatic replay is legal.
- `BLOCKED`: definitive rejection or bounded safe retry exhaustion.
- `CANCELLED`: cancellation committed while the event was provably pre-ready.

`SUBMITTING -> READY` retains the same sequence and is legal only after explicit proof of
non-acceptance. A timeout, disconnect, dead process, or missing history is not such proof.

`UNKNOWN -> ACCEPTED` exists only for positive native history reconciliation. There is no safe
marker equivalent because claim output is external to Codex history.

### Common fork-lineage job registry

The protocol-independent `OwnerJobRegistry` is keyed by `job_scope_id`, not the delivery owner.
Every entry additionally records its exact `delivery_owner_id`. It uses its own stable lock and
states:

```text
ACTIVE -> ACCEPTED
       -> UNKNOWN
       -> BLOCKED
       -> CANCELLED

UNKNOWN -> ACCEPTED    positive native reconciliation only
```

`ACTIVE`, `UNKNOWN`, and `ACCEPTED` are duplicate fences. `ACCEPTED` is a permanent tombstone for
that job key across ordinary branches and across native/marker selection. `BLOCKED` and
`CANCELLED` release that job key for a later retry.

The registry also supplies a per-exact-owner protocol gate:

```text
same delivery_owner_id
  unresolved ACTIVE/UNKNOWN native event
    => reject new marker registration
  unresolved ACTIVE/UNKNOWN marker event
    => reject new native registration
```

There is no safe cross-ledger READY order, so this gate is stricter than allowing two unrelated jobs
to race in different protocols. A different job may switch protocol after all earlier events for
that owner are definitive `ACCEPTED`, `BLOCKED`, or `CANCELLED`. Native `UNKNOWN` must reconcile to
`ACCEPTED` before switching; marker `UNKNOWN` has no reconciliation path and keeps the protocol
gate closed. Distinct ordinary forks have different `delivery_owner_id` values and do not share
this gate, even though they share same-job deduplication through `job_scope_id`.

The scheduler reserves the common registry before registering the protocol ledger. Exact
idempotent reserve/register operations recover a crash after either commit without producing a
second event. At terminal delivery, the protocol ledger commits first, then the registry converges.
If a crash separates those writes, status/recover/cancel/dispatch reads the authoritative ledger
and advances the registry monotonically. The reverse transition is forbidden.

## Scheduling transaction

For one schedule request:

1. Read the actor and verify complete `parentThreadId` and `forkedFromId` routing.
2. Determine `owner_thread_id` and common `job_scope_id`.
3. Capture the target process incarnation set.
4. Run preflight against only that stored set. Abort without a reservation if the target exits or
   identity becomes unknown.
5. Probe the requested app-server authority and choose `native-message` or `marker`.
6. Create immutable event identities, the final state paths, and a `reserving` task record.
7. Reserve the job key plus exact delivery owner in the common lineage registry, enforcing both
   same-job deduplication and the per-owner protocol gate, then persist its generation.
8. Register in the selected protocol ledger and persist token, ledger generation, authority epoch,
   and paths.
9. Move the task record to `scheduled`.
10. Spawn the detached watcher with a one-shot startup pipe.
11. Wait for a startup ACK that exactly matches task ID, token, generation, watcher PID, complete
    watcher process identity, and `phase=watching`.

The scheduler reports success only after step 11. External cancellation is refused while the task
is `reserving` or `scheduled`; this closes the spawn-to-guard handoff window.

The maximum-wait budget begins before preflight and is stored as an absolute deadline. Watcher
recovery reuses that deadline rather than granting a new full wait.

## Watcher and completion publication

### Watcher startup fence

The child first acquires its lifetime `.watch.guard` and validates the current task, token,
generation, task path, and ledger state. It then:

1. captures `ProcessIdentity(os.getpid())`;
2. accepts only Linux `/proc` or local macOS `libproc` strong start tokens;
3. marks the ledger `WATCHING`;
4. atomically writes `watcher_pid`, full `watcher_identity`, and task `phase=watching`; and
5. sends the matching ACK.

Identity capture failure occurs before `WATCHING` and before ACK, so the parent can cancel the
still-pre-ready reservation safely. The ACK is never evidence for a watcher identity that was not
already durable.

Only one process can hold the lifetime guard. Hidden `watch` accepts only `scheduled` or
`watching`, and only with the current token/generation. Every post-event or terminal phase is a
hard replay rejection.

### Process completion publication

The watcher observes only the schedule-time target identities. At exit or deadline it:

1. renders the final continuation prompt;
2. writes and fsyncs the prompt;
3. stores its SHA-256 and `phase=event_staged` in task JSON; and
4. calls `mark_ready`, obtaining an immutable ready sequence.

This order prevents recovery from exposing a schedule-time placeholder or a partially written
prompt. Recovery may promote a complete `event_staged` record to `READY`; it never re-enters
process observation for that event.

## Native delivery

### Ready-order dispatch

Only the earliest unresolved event in the native owner ledger can dispatch. A later watcher may
adopt an earlier `READY` event only after acquiring that earlier task's free lifetime guard. If it
finds an abandoned `SUBMITTING`, it fences it `UNKNOWN`; a live watcher still holding the guard
cannot be adopted.

The dispatcher atomically commits `READY -> SUBMITTING` before opening the first request boundary.
Authority/connectivity failure before any continuation RPC is safe to defer to the same `READY`
sequence. Submission-deferral counts live in the ledger, so process restart cannot reset the retry
budget.

### Active owner: exact `turn/steer`

The dispatcher first calls `thread/read(includeTurns=false)` to obtain runtime status and
`historyMode`. If runtime status is `active`, legacy history is loaded with
`thread/read(includeTurns=true)`, while paginated history is read from only the newest descending
`thread/turns/list(itemsView=notLoaded)` page. Paginated threads reject
`thread/read(includeTurns=true)`. The selected view must expose exactly one turn with
`status=inProgress`; otherwise dispatch fails closed before submission. It sends:

```text
turn/steer(
  threadId = owner_thread_id,
  expectedTurnId = exact active turn id,
  clientUserMessageId = event client id,
  input = continuation text
)
```

A successful response must return the same turn ID. That response is the synchronous positive
acceptance evidence: the input was queued into that exact active regular turn. The watcher does not
wait for stream notifications on its own connection.

If the active snapshot is ambiguous, the dispatcher submits nothing. If the state changes between
read and RPC, `NoActiveTurn`, expected-turn mismatch, or Review/Compact non-steerable errors are
explicit non-acceptance. The event returns to its same sequence, state is re-read after a short
collision delay, and this collision does not consume the general authority/error retry budget.

### Idle owner: `turn/start` plus persisted-history ACK

When the owner is positively loaded with no active turn, the dispatcher sends:

```text
turn/start(
  threadId = owner_thread_id,
  clientUserMessageId = event client id,
  input = continuation text
)
```

A fresh connection is not subscribed to an existing owner's stream merely because it performed
`thread/read` or `turn/start`. Therefore the watcher does not use `item/started` notifications as
its ACK. After `turn/start` returns, it repeatedly reads persisted history on the same authority
until a `userMessage` with the exact `clientId` appears. Legacy history uses
`thread/read(includeTurns=true)`. Paginated history uses the newest descending
`thread/turns/list(itemsView=full)` page only: the just-submitted message must be on that page, so
the 250 ms ACK loop never scans the complete task history. That persisted item is the positive
acceptance evidence.

The item may appear under a turn that became active during a read/submit collision; the client ID,
not a guessed new-turn placement, is the criterion.

If `turn/start` succeeds but history times out, disconnects, or fails, the result is `UNKNOWN`.
Absence from one or more snapshots is never explicit rejection.

### Public idle-start side effect

In the audited Codex implementation, `turn/start` calls `set_app_server_client_info` and
`set_openai_form_elicitation_support` before submitting input. The watcher initializes as the
official secondary `codex_app_server_daemon/2.0.0` client with `experimentalApi=true` and without
the OpenAI-form capability. This known secondary identity does not change the app-server's
process-global originator. Nevertheless, an idle wake replaces the loaded thread's previous
app-server client name/version and sets form-elicitation support to false, potentially refreshing
MCP runtime state. `turn/start` does not subscribe the watcher connection to thread events.

The official daemon identity avoids pretending to be Desktop/Terminal and avoids a new custom
originator, but it does not make this thread-level mutation neutral. `turn/steer` does not perform
the same update. Current public API has no operation that both starts idle model-visible work and
preserves all owner-client metadata/capabilities.

### Ambiguous boundary and reconciliation

Once a native request may have started, a transport failure without an explicit JSON-RPC negative
response becomes `UNKNOWN`. If a process dies after receiving a successful response but before
committing `ACCEPTED`, recovery also uses `UNKNOWN`; process death cannot prove non-acceptance.

Native reconciliation is positive-only:

```text
legacy: thread/read(includeTurns=true)
paginated: thread/turns/list(itemsView=full), following every nextCursor
  contains exact userMessage.clientId
    => UNKNOWN -> ACCEPTED
  does not contain it
    => remain UNKNOWN
```

`UNKNOWN` blocks every later event in that native owner ledger. It also remains a common-registry
duplicate fence for the same job and closes that exact owner's protocol gate, so a new marker job
cannot bypass the ambiguity. It does not block a different ordinary-fork owner.

Unlike the live ACK loop, manual paginated reconciliation traverses every descending page because
the ambiguous event may have aged out of the newest page before an operator reconciles it.

### Native stream propagation

After acceptance, the ticketed app-server produces the ordinary `turn/*`, `item/*`, tool, and
delta notifications. Only first-party clients that are currently connected and subscribed to this
task receive those events live. For Remote Control, the host must also remain awake and online, the
account signed in, and the host app running. The watcher neither implements the secure relay nor
impersonates a Desktop client; it only injects into the ticketed authority.

The watcher does not call `thread/start`, `thread/resume`, or another subscribing API. In the
audited implementation, `thread/read`, `turn/start`, and `turn/steer` do not add a subscriber.
Core events and approval requests remain routed to the thread's existing subscribers. With no
subscriber, no live stream is promised: accepted input and output remain in persisted history, and
an approval may remain pending until a first-party client later resumes and subscribes. This avoids
stealing stream or approval ownership merely to wake the task.

## Marker delivery

Marker mode uses the separate `marker:<owner>` FIFO. Completion becomes `marker_pending`; it does
not start or steer a Codex turn. Only the exact current `CODEX_THREAD_ID` matching
`owner_thread_id` may claim it, and a sibling fork cannot cross that boundary.

Claim performs:

1. acquire the lifetime guard;
2. claim the earliest marker `READY` entry as `SUBMITTING`;
3. verify the final prompt digest;
4. emit and flush `{resume_prompt, event_id, owner_thread_id, ...}` to stdout;
5. commit marker-ledger `ACCEPTED`; and
6. commit common-registry `ACCEPTED` tombstone and task mirror.

Steps 4 and later insertion of `resume_prompt` into Codex are not one transaction. If the claimant
dies after output may have occurred but before step 5, recovery converts `SUBMITTING` to marker
`UNKNOWN` and never emits again. If step 5 commits, `ACCEPTED` proves output, not model input.

This is the strongest safe file-based fallback available without a public owner-routed enqueue
API. This repository does not implement a Desktop heartbeat. A separately configured first-party
automation targeted to the exact owner may poll and claim, but retains the same output-to-input
gap.

## Process-incarnation protocol

### Schedule-time binding

A PID is a reusable namespace slot. V3 stores a versioned `ProcessIdentity` containing scope, host,
PID, source, and start token, plus diagnostic PPID/state/command. Capture reads identity twice when
needed to reject a process changing during inspection.

| Platform | Identity source | Scheduling decision |
| --- | --- | --- |
| Local Linux | kernel boot ID + `/proc/<pid>/stat` field 22 | Strong; accepted |
| Remote Linux | same fields through fixed SSH helper | Strong; accepted |
| Local macOS | `proc_pidinfo(PROC_PIDTBSDINFO)` start sec/usec | Strong; accepted |
| Remote macOS / generic Unix | `LC_ALL=C ps -o lstart` | Second-resolution; rejected |

Zombie means exited. A changed start token means the original exited and its PID was reused.
Inspection errors mean `unknown`, never alive or dead by guess.

### Pattern snapshot

Pattern mode is discovery only. It captures every matching non-zombie incarnation once, then
polls/stops only that stored set. It never reruns `pgrep` for watch, stop, or recovery. New matching
workers are outside the scheduled job.

The local helper excludes itself and all ancestors. The remote fixed shell helper receives user
data on stdin, invokes SSH as `ssh -- host <fixed-helper>`, and excludes its full ancestor chain.
The pattern never appears in a scanned remote shell argv. Unsafe SSH destinations and incomplete
helper snapshots fail before reservation.

### Wait and signal mechanisms

Local exact PID waiting uses `pidfd` on Linux or `kqueue NOTE_EXIT` on macOS/BSD when available;
otherwise it polls identities. Remote and pattern targets poll.

Local Linux termination opens a pidfd and revalidates after opening when Python exposes both
`os.pidfd_open` and `signal.pidfd_send_signal`. That handle prevents PID reuse between validation
and signal. Otherwise termination revalidates immediately before each TERM/KILL. Remote helper
signaling likewise snapshots immediately before `kill` and revalidates again before escalation.

Without pidfd or an equivalent process handle, POSIX exposes an unavoidable identity-check to PID-
signal TOCTOU window. V3 does not claim to eliminate it on macOS, remote hosts, or fallback Linux.
It does guarantee that every already-observed replacement is not signaled and every inspection
uncertainty fails closed.

## Cancellation and crash recovery

### Three P0 fences

1. **Cancellation versus possible submission:** cancellation is legal only in a matching
   pre-ready ledger state. `READY`, `SUBMITTING`, `UNKNOWN`, marker claim, and accepted/terminal
   phases cannot become cancelled. `stop --also-stop-target` signals the target only after ledger
   cancellation commits.
2. **Scheduler/cancel versus watcher handoff:** scheduling succeeds only after the guarded child
   durably stores its strong watcher identity and ACKs `WATCHING`. Cancel/stop never rediscover a
   watcher by PID or argv; they terminate only that persisted incarnation. Missing, malformed,
   weak, permission-denied, unprobeable, or still-alive results retain the reservation. PID reuse
   counts as exit of the original watcher and never signals the replacement.
3. **Hidden watch replay:** `watch` may enter only from `scheduled` or `watching` under the lifetime
   guard with current task path, token, and generation. Post-event and terminal phases never
   re-observe the process.

See the executable [race-fencing audit](race-fencing.md).

### Explicit recovery

`recover` acquires the same lifetime guard before repair:

- incomplete exact reservations are reconstructed idempotently;
- a crashed pre-event watcher clears its old PID and identity before a replacement starts;
- `event_staged` becomes `READY` only after prompt-digest verification;
- an existing `READY` event dispatches or remains pending in place;
- abandoned `SUBMITTING` becomes `UNKNOWN`; and
- terminal ledger state repairs the task mirror and common registry without replay.

Recovery preserves the original absolute wait deadline and ledger submission-deferral budget.

Native `reconcile` acquires the lifetime guard so it cannot fence a live dispatcher. It may adopt
an abandoned `SUBMITTING` as `UNKNOWN`, then apply only positive client-ID history evidence.

## Authority rebind within one coordination store

`freeze`/`rebind` is a ledger-level authority switch, not a state-migration protocol:

1. Run `freeze(owner, expected_epoch)` from the exact owner task.
2. Reject freeze while any native event is `SUBMITTING` or `UNKNOWN`.
3. Enter ledger `DRAINING`; reject new native registration/submission while allowing existing
   watchers to stage `READY` events.
4. Keep every watcher and command on the same underlying task files, prompts, stable lock
   files/inodes, native ledger, and common job registry. Equal path strings, copied files, or an
   independent mount are not equivalent. Never copy or move live state.
5. Probe the replacement attachable authority from that same coordination and lock domain and
   classify its strength.
6. Run `rebind(owner, expected_epoch, new_authority)` from the exact owner.
7. Require a strong destination or a fresh explicit `--allow-weak-authority`; verify route, loaded
   owner, and descriptor; atomically increment the authority epoch and update every nonterminal
   native entry before returning to `ACTIVE`.
   The CAS stores weak-authority acceptance inside each updated authority descriptor. Task JSON is
   not bulk-rewritten because a live READY watcher may concurrently own it.

The epoch CAS fences only this local ledger; the app-server does not enforce it on message
insertion. Rebind does not move watcher processes or credentials, retire an old authority,
integrate Codex product handoff, or provide strict cross-host migration. Marker has no app-server
authority and uses a separate ledger; product handoff does not transfer its pending state or solve
marker output-to-input atomicity. If every participant cannot continue using the same underlying
coordination and lock store, keep native delivery `DRAINING` and report the switch as blocked.
Never infer migration from the same `thread_id` appearing on another host.

## Surface and streaming behavior

| Topology | Delivery | Live visibility |
| --- | --- | --- |
| Terminal managed daemon | Strong native through exact ancestor Unix socket, inode, and live process incarnation | Current first-party task subscribers receive the normal stream |
| Explicit Unix without ancestor proof | Marker by default; weak native with explicit opt-in | Conditional on a currently connected and subscribed first-party client; instance continuity is not guaranteed |
| Explicit remote WebSocket or `wss://` connect alias for a `ws://` listener | Marker by default; weak native with credential reference and explicit opt-in; proxy/restart backend is not fenced | Conditional on a currently connected and subscribed first-party client |
| Desktop controlling a Terminal host | Subscriber surface; watcher attaches to the Terminal host authority | First-party relay streams only while Desktop is connected and task-subscribed |
| Desktop SSH remote project using `app-server --listen unix://` and `app-server proxy` | Strong native when the remote watcher proves the exact ancestor Unix listener, socket inode, and live process incarnation | Desktop receives the normal stream through its existing SSH proxy |
| Older/custom Desktop SSH topology with only private stdio | Marker only | No supported watcher attachment to that private topology |
| Terminal Embedded | Marker only | No external endpoint |
| Desktop/IDE private stdio | Marker only | Private pipes are not a supported second-client endpoint |

Private stdio and Embedded are not made native by access to persisted rollout files. Loading the
same history in another app-server creates the wrong execution authority. Remote live visibility
also requires the host to be awake and online, the account signed in, and the host app running. If
no first-party client is connected and subscribed, accepted work remains in persisted history and
approvals may remain pending until a later resume/subscription.

## Required upstream primitive

Full product-level correctness would require a service-owned operation such as:

```text
thread/event/enqueue(
  threadId,
  eventId,
  ownerAuthorityEpoch,
  payload,
  forkPolicy = firstOwnerInLineage
)
```

It would need to deduplicate `eventId`, serialize with normal user messages, queue through busy and
non-steerable states, preserve original-branch policy, migrate authority atomically, return a
durable acknowledgement after client disconnect, preserve owner client capabilities, and broadcast
through existing surfaces. Current public Codex API does not expose that combined primitive.

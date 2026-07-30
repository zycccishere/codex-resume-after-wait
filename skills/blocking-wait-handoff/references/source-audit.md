# Codex source audit for protocol v3

## Contents

- [Audit basis](#audit-basis)
- [Findings](#findings)
- [Source-to-protocol matrix](#source-to-protocol-matrix)
- [Runtime topology observations](#runtime-topology-observations)
- [Remaining product boundary](#remaining-product-boundary)

## Audit basis

The public-product baseline is the current Codex manual retrieved on 2026-07-30, especially:

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Remote connections](https://learn.chatgpt.com/docs/remote-connections)

The implementation audit used a clean local checkout of `openai/codex` pinned to:

```text
6219b7c40fc9c702c0aef9964e72b492558f60e4
```

The same revision is available in the
[pinned upstream tree](https://github.com/openai/codex/tree/6219b7c40fc9c702c0aef9964e72b492558f60e4).
Paths below are relative to that checkout. They establish behavior at this revision, not a
compatibility promise for every installed Codex build. Protocol v3 also probes the live endpoint
and reports version skew.

## Findings

### 1. Execution authority is an app-server instance, not a thread ID

`codex-rs/tui/src/lib.rs` distinguishes `AppServerTarget::{Embedded, LocalDaemon, Remote}`. The TUI
can run an in-process authority, reuse the managed local daemon, or connect to an explicit remote
endpoint. The manual documents stdio, Unix-socket, and WebSocket app-server transports. The audited
app-server listener accepts plain `ws://`; the TUI remote client accepts `unix://`, `ws://`, and a
`wss://` connect endpoint, with TLS terminated by a proxy in front of the listener.

Consequences:

- a managed-daemon Terminal task is externally attachable through its exact Unix socket;
- an explicit remote task is attachable only through that same endpoint and credential;
- Terminal Embedded has no second external endpoint;
- a private stdio app-server's pipes are owned by its launching client and are not a general
  multi-client attachment point; and
- the same persisted `thread_id` loaded in another app-server is a different execution authority.

`codex-rs/app-server/src/in_process.rs` constructs the Embedded server without a Remote Control
handle. Access to rollout history does not expose its active `ThreadManager`.

### 2. History reads observe without resuming

The current app-server manual explicitly defines:

- `thread/read`: read a stored thread without resuming it, optionally including full turns for
  legacy history; paginated history rejects `includeTurns=true`;
- experimental `thread/turns/list`: page stored turns without resuming, with `itemsView` controlling
  item detail;
- `thread/loaded/list`: list threads currently loaded in memory; and
- `thread/resume`: reopen an existing stored thread.

Protocol v3 uses the first three and never calls the fourth. This matters because `thread/resume` on a
watcher's connection could cold-load the same history in an authority that is not the original
owner. A native attempt instead requires positive loaded evidence on the ticketed authority before
any continuation RPC.

Relevant code:

- `codex-rs/app-server/src/request_processors/thread_processor.rs`
- `codex-rs/app-server-protocol/src/protocol/v2/thread.rs`
- `codex-rs/app-server/README.md`

### 3. Active and idle continuation use different public APIs

The manual describes `turn/steer` as appending input to an active in-flight turn and `turn/start` as
adding user input and beginning generation.

`codex-rs/app-server-protocol/src/protocol/v2/turn.rs` shows that `TurnSteerParams` requires
`expectedTurnId` and accepts `clientUserMessageId`. `TurnStartParams` also accepts
`clientUserMessageId` but has no expected active-turn precondition.

`codex-rs/app-server/src/request_processors/turn_processor.rs::turn_steer_inner` calls
`steer_input` with the expected turn ID. It returns explicit errors for:

- no active turn;
- expected-turn mismatch; and
- a non-steerable Review or Compact turn.

Protocol v3 therefore reads turns first and chooses:

```text
exactly one regular active turn -> turn/steer(expectedTurnId)
no active turn                 -> turn/start
ambiguous active snapshot      -> submit nothing
```

An explicit no-active/mismatch/non-steerable response proves non-acceptance and permits re-probe
at the same FIFO sequence. A transport loss does not.

This replaces the earlier v3 draft's overly broad statement that the watcher always invokes
`turn/start` as one opaque steer-or-start operation.

### 4. The two paths have different ACK semantics

For active input, `TurnSteerResponse` returns `turnId`; the implementation verifies that it equals
the requested active turn. Successful response is synchronous acceptance evidence for that exact
turn.

For idle input, `turn/start` returns an initial turn object, but protocol v3 requires stronger
positive persistence evidence. `clientUserMessageId` is carried into core and later appears as
`userMessage.clientId` in thread items. A fresh secondary connection is not automatically
subscribed to an already-owned thread merely because it called `thread/read` or `turn/start`, so the
watcher polls persisted history for the matching client ID. Legacy history uses
`thread/read(includeTurns=true)`; paginated history uses
`thread/turns/list(itemsView=full)`, which is the supported history API for that mode. The live ACK
loop reads only the newest descending page; manual `UNKNOWN` reconciliation follows every cursor
because the matching event may be old.

Relevant code:

- `codex-rs/app-server/src/request_processors/turn_processor.rs`
- `codex-rs/app-server/src/request_processors/thread_processor.rs`
- `codex-rs/app-server/src/bespoke_event_handling.rs`
- `codex-rs/app-server/README.md`

If `turn/start` returns but the subsequent history read fails, the message may already be durable.
The only safe outcome is `UNKNOWN`.

### 5. `clientUserMessageId` is correlation, not documented deduplication

The public parameter and echoed item client ID provide positive acceptance evidence. Neither the
manual nor the reviewed request implementation declares repeated values idempotent. Native
requests also have no `expectedAuthorityEpoch` precondition.

Protocol consequences:

- commit `SUBMITTING` before a request may start;
- retry only after an explicit negative response or a failure before request bytes;
- convert every ambiguous post-boundary failure to `UNKNOWN`;
- reconcile only by finding the exact client ID in persisted history; and
- never infer non-acceptance from absence in a snapshot.

This is conservative at-most-one automatic possibly-accepted submission, not strict exactly-once.

### 6. Idle `turn/start` mutates thread client and capability state

`turn_start_inner` performs these operations before submitting input:

```text
set_app_server_client_info(...)
set_openai_form_elicitation_support(...)
submit_user_input_with_client_user_message_id(...)
```

The first updates the thread's app-server client name/version and related elicitation behavior.
The second changes the thread's OpenAI extended-form capability and can request an MCP runtime
refresh. The values come from the connection's `initialize` request.

Protocol v3 initializes its secondary connection as the existing official
`codex_app_server_daemon/2.0.0` client with `experimentalApi=true` and without
`mcpServerOpenaiFormElicitation`. `initialize_processor.rs` treats this as a known secondary client,
so it does not replace the app-server's process-global originator. `turn/start` still applies the
daemon's values to the loaded thread: it replaces thread client name/version, sets OpenAI-form
support to false, and can refresh MCP runtime state. It does not subscribe the watcher connection
to the thread.

`turn_steer_inner` does not call those setters, so the active path avoids this side effect. The
public API exposes no neutral method that starts idle model-visible work while retaining all prior
client capability state.

Relevant code:

- `codex-rs/app-server/src/request_processors/initialize_processor.rs`
- `codex-rs/app-server/src/request_processors/turn_processor.rs`
- `codex-rs/core/src/session/mod.rs`
- `codex-rs/core/src/session/mcp.rs`

### 7. Native streaming belongs to the owning app-server

The manual says app-server powers rich clients with streamed agent events. It documents ordinary
`turn/*`, `item/*`, tool-progress, and delta notifications. Remote connections allow another
authorized client to view output, send follow-up instructions, and steer active work through the
connected host.

In source, stdio, Unix, WebSocket, and Remote Control connections converge on the same incoming
message processor:

- `codex-rs/app-server-transport/src/transport/mod.rs`
- `codex-rs/app-server/src/lib.rs`
- `codex-rs/app-server/src/outgoing_message.rs`
- `codex-rs/app-server/src/bespoke_event_handling.rs`

Therefore the watcher does not need to reproduce Terminal-to-Desktop synchronization. When it
injects into the ticketed app-server, first-party clients that are currently connected and
subscribed to that task receive normal incremental output. Remote Control additionally depends on
the host remaining awake and online, the account signed in, and the host app running. The watcher
never calls `thread/start`, `thread/resume`, or another subscribing method. At this revision,
`thread/read`, `turn/start`, and `turn/steer` do not add a thread subscriber, so its fresh
connection does not become the UI or approval stream.

`thread_state.rs` and the outgoing request paths route core events and approvals to existing thread
subscribers. When there are none, no live view is promised: accepted work remains in persisted
history, and an approval can remain pending until a first-party client resumes/subscribes. This
preserves the native owner experience without making the watcher an approval handler.

### 8. Remote Control metadata is not a public enqueue API

`codex-rs/app-server-transport/src/transport/remote_control/protocol.rs` contains internal
server/environment/client/stream/sequence/ack identity. Public
`remoteControl/status/read` exposes a smaller status identity.

The audited CLI exposes only `codex remote-control start`, `stop`, and `pair`; it has no
send/enqueue/steer/attach operation. `codex --remote` is a direct Unix/WS/WSS app-server client,
not a relay client, and the public `remoteControl/*` RPC family contains lifecycle, pairing,
status, list, and revoke operations rather than an owner/thread-routed message primitive. The
open-source relay transport shows that first-party paired clients can feed JSON-RPC into the
owning app-server, but it does not expose the client claim/authentication and routing surface a
third-party watcher would need. Reverse-engineering that private surface is outside this skill's
supported protocol.

Those public installation/environment/server-name fields are not a per-process instance nonce.
They can survive an app-server restart, and a WSS endpoint can terminate at a proxy whose backend
changes while the endpoint and TLS identity remain stable. They therefore describe a `weak`
authority, not an exact execution instance.

Protocol v3 classifies only an exact ancestor Unix listener with both socket device/inode and a
live app-server PID/start token as `strong`. It defaults every WS/WSS and unproven explicit Unix
endpoint to marker. `--allow-weak-authority` is a deliberate loss-of-instance-fencing opt-in, not a
claim that a WSS alias equals an ancestor `ws://` bind address. The watcher still lets an accepted
native owner use its existing first-party relay; it does not enroll as or impersonate Desktop.
The listener remains `ws://` while a private TLS proxy may supply a `wss://` client connect
endpoint. Such an alias remains attachable for a native probe, but the ticket records both the
ancestor listener and client endpoint plus `endpoint_matches_ancestor=false`; it can never be
classified strong. The same treatment applies to a mismatched Unix endpoint. Prefer authenticated
TLS on a private network, VPN/mesh, or SSH tunnel; the app-server should not be exposed directly to
the public Internet. Private stdio and Embedded ancestors remain non-attachable regardless of the
weak-authority flag.
Weak acceptance is part of the epoch-bound ledger authority (`weak_authority_accepted=true`), not
a task-file policy bit. This matters after rebind: an already-READY watcher can hold an old task
snapshot while the ledger atomically moves every pending entry to the newly accepted authority.

The manual also states that Remote access depends on the host remaining awake, online, signed in,
and running. Relay visibility is not an independent durable owner when the host app-server is gone.

### 9. Fork and subagent metadata serve different relationships

`parentThreadId` represents a spawned-agent parent chain. `forkedFromId` represents an ordinary
history branch. Protocol v3 first walks the former to find the durable delivery owner, then walks
the latter to compute the common duplicate-job scope.

`turn_processor.rs::ensure_direct_input_allowed` rejects direct input to current Multi-Agent V2
spawned children. This supports child-as-actor/root-as-owner routing.

`thread/read` can reconstruct stored thread summaries without a durable side-fork parent, and
observed ephemeral `/side` records can be pathless. A detached process cannot prove the requested
parent capability, so side scheduling remains rejected. Ordinary durable forks are accepted only
when their entire `forkedFromId` lineage can be read without cycles or gaps.

Relevant code:

- `codex-rs/app-server/src/request_processors/turn_processor.rs`
- `codex-rs/app-server/src/request_processors/thread_processor.rs`
- `codex-rs/app-server-protocol/src/protocol/v2/thread.rs`

## Source-to-protocol matrix

| Source | Audited fact | v3 consequence |
| --- | --- | --- |
| Codex app-server manual | `thread/read` does not resume; `thread/loaded/list` identifies live loaded IDs | Observe and require loaded evidence; never call `thread/resume` |
| Codex app-server manual | `turn/steer` appends to active work; `turn/start` begins generation | Split active and idle paths |
| `protocol/v2/turn.rs` | Steer has `expectedTurnId`; both methods accept client IDs | Exact-turn fencing plus event correlation |
| `turn_processor.rs` | Steer returns explicit state-collision errors | Safe same-sequence retry only after explicit rejection |
| `turn_processor.rs` | Start writes client info/form support before input | Disclose idle client/capability side effect |
| `thread_processor.rs` | Legacy history supports `thread/read(includeTurns=true)`; paginated history rejects it and exposes turns through `thread/turns/list` | Mode-aware active-turn reads, positive idle ACK, and UNKNOWN reconciliation |
| TUI target selection | Embedded, daemon, and remote are distinct authorities | Bind exact endpoint; Embedded is marker-only |
| app-server transport/outgoing paths | Existing thread subscribers receive normal event routing and broadcasts | Reuse native Terminal/Remote Control streaming for currently connected subscribers |
| Remote Control protocol/status | Relay has richer internal identity than public status | Bind exposed identity; do not invent a relay enqueue API |
| direct-input validation | Spawned V2 children reject direct input | Route verified nested agents to durable root |
| thread metadata behavior | Ordinary fork and side metadata have different durability | Walk durable fork lineage; reject `/side` |

## Runtime topology observations

The 2026-07-30 runtime inspection found several simultaneous authorities:

- the managed Terminal daemon on its Unix socket;
- local Desktop and Cursor/IDE app-servers using distinct private stdio processes;
- a Desktop SSH project using a remote app-server/proxy; and
- a separate remote IDE app-server.

These are observations of one environment, not universal product defaults. They demonstrate why
surface name and common rollout storage cannot establish authority. The protocol probes the exact
endpoint on every native attempt. In particular, the observed Desktop SSH app-server/proxy was a
private topology with no supported second-client attachment, so the skill treats that default case
as marker-only.

## Remaining product boundary

The reviewed public API does not combine:

- routing to the current owner authority across hosts;
- durable event-ID deduplication;
- an authority-epoch compare-and-swap on message insertion;
- a per-boot app-server instance ID usable as a message precondition across WS/WSS/proxies;
- queueing through Review, Compact, approval, disconnect, and migration;
- first-owner ordinary-fork policy;
- idle input without client/capability mutation; and
- an acknowledgement recoverable after the submitting connection disappears.

Protocol v3 supplies host-local ledgers, a fork-lineage job registry, positive history evidence,
and explicit freeze/rebind only as an authority switch within the same underlying coordination and
lock store. It does not copy or migrate live state across hosts. Strict product-wide exactly-once
continuation still requires an upstream owner-routed, idempotent enqueue primitive.

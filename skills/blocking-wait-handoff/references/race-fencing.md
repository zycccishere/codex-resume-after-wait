# Race-fencing audit — 2026-07-30

Status: all three P0 races are protocol invariants with executable regression coverage. They are
not documentation disclaimers and must block any refactor that weakens them.

## Contents

- [Core invariant](#core-invariant)
- [P0-1: cancellation versus uncertain delivery](#p0-1-cancellation-versus-uncertain-delivery)
- [P0-2: scheduler/cancel versus watcher handoff](#p0-2-schedulercancel-versus-watcher-handoff)
- [P0-3: hidden watcher replay](#p0-3-hidden-watcher-replay)
- [Cross-layer monotonicity](#cross-layer-monotonicity)
- [Refactor gate](#refactor-gate)

## Core invariant

For one immutable tuple:

```text
job_scope_id
job_key
owner_thread_id
protocol ledger key
task_id
event_id
reservation_token
protocol generation
job generation
authority_epoch
```

the system must preserve:

- one owner branch wins the logical job across ordinary forks;
- native and marker may use separate ledgers, but the same job cannot reserve once in each;
- one exact delivery owner may not mix unresolved native and marker events;
- only one watcher may publish the completion event;
- cancellation may win only before `READY`;
- only the earliest protocol-ledger sequence may enter `SUBMITTING`;
- a possibly delivered event may never become `READY` or `CANCELLED` again;
- `ACCEPTED` remains a permanent common-registry tombstone for that job key; and
- no post-event or terminal task phase may re-enter process observation.

`UNKNOWN` is a durable fence. Native `UNKNOWN` may become `ACCEPTED` only through an exact
persisted `userMessage.clientId`. Marker `UNKNOWN` has no positive Codex-history reconciliation and
must never replay output. While either remains `UNKNOWN`, the exact owner cannot switch delivery
protocol to bypass it.

## P0-1: cancellation versus uncertain delivery

### Race

A native request can commit while its response or idle-history ACK is lost. Likewise, marker
stdout can be emitted before the claimant commits ledger `ACCEPTED`. Concurrent cancellation or
recovery must not release the event as though delivery definitely did not happen.

### Required fence

- Commit `READY -> SUBMITTING` before the first native request boundary or marker output.
- Serialize submission/claim and cancellation through the protocol ledger and task lifetime guard.
- Permit ledger cancellation only from exact token/generation-matched `SCHEDULED` or `WATCHING`.
- Fence task phases `event_staged`, `READY`, queued/deferred delivery, `SUBMITTING`, submitted,
  marker pending/claiming, `UNKNOWN`, `ACCEPTED`, and all terminal states against cancellation.
- Treat explicit JSON-RPC rejection as non-acceptance; retain the original ready sequence.
- Treat timeout, disconnect, dead dispatcher, failed post-start history read, or marker claimant
  death after output begins as `UNKNOWN`.
- Commit target termination only after event-ledger cancellation succeeds. Losing cancellation must
  never kill the watched job.
- Never fall back from ambiguous native delivery to marker and never replay marker output.

### Regression coverage

- `RaceFencingTests.test_cancel_cannot_cross_submitting_or_unknown_fence`
- `RaceFencingTests.test_ledger_rejects_cancel_after_submission_claim_or_unknown_outcome`
- `RaceFencingTests.test_lost_native_cancel_never_stops_watched_target`
- `RaceFencingTests.test_stop_target_runs_only_after_ledger_cancellation_commits`
- `NativeMessageDeliveryTests.test_timeout_after_request_is_unknown_and_replay_never_resends`
- `NativeMessageDeliveryTests.test_history_rpc_error_after_successful_turn_start_is_unknown_once`
- `MarkerLedgerTests.test_marker_output_crash_before_accepted_is_fenced_without_replay`
- `OrderedLedgerTests.test_unknown_is_monotonic_and_blocks_later_events`

## P0-2: scheduler/cancel versus watcher handoff

### Race

There is a handoff window between durable reservation, detached-process creation, lifetime-guard
acquisition, and watcher startup ACK. Cancellation could otherwise release the reservation while a
new child continues. A later PID reuse could also make PID-only cancellation signal an unrelated
process.

### Required fence

- Treat `reserving` and `scheduled` task phases as externally non-cancellable handoff phases.
- Give each task one stable lifetime `.watch.guard` held throughout watcher work.
- Re-read the task after acquiring the guard and verify task path, task ID, token, protocol
  generation, ledger entry, and authority epoch.
- In the child, capture `ProcessIdentity(os.getpid())` before `WATCHING`; accept only Linux
  `/proc` or local macOS `libproc` strong start tokens.
- Mark the ledger `WATCHING`, then atomically persist `watcher_pid`, the full `watcher_identity`,
  and task `phase=watching` before writing the startup ACK.
- Include the complete persisted watcher identity in the ACK and require an exact parent-side
  match. Scheduling is not successful before that ACK.
- Ensure capture failure occurs before `WATCHING` and before ACK, leaving a safely cancellable
  pre-ready reservation.
- Let cancel/stop deserialize and terminate only the persisted watcher incarnation. Never derive a
  signal target or signal capability from task ID, PID alone, command line, or process-table
  rediscovery.
- On missing, corrupt, weak, permission-denied, unprobeable, or still-alive identity, retain the
  reservation. Treat a reused PID as exit of the original watcher and send no signal to its
  replacement.
- Before starting a recovered watcher, clear the crashed watcher's PID and identity; the replacement
  must capture and ACK its own incarnation.

### Regression coverage

- `RaceFencingTests.test_schedule_to_watcher_handoff_cannot_be_cancelled`
- `RaceFencingTests.test_scheduled_and_watching_v3_task_can_enter_only_one_watcher`
- `RaceFencingTests.test_live_watcher_lock_excludes_a_second_watcher`
- `RaceFencingTests.test_watcher_identity_is_durable_before_watching_ack`
- `RaceFencingTests.test_watcher_identity_failure_precedes_watching_and_ack`
- `RaceFencingTests.test_cancel_paths_never_signal_a_pid_reused_by_an_unrelated_process`
- `RaceFencingTests.test_identity_inspection_failure_blocks_cancel_and_retains_reservation`
- `RaceFencingTests.test_missing_persisted_watcher_identity_blocks_without_pid_rediscovery`
- `RaceFencingTests.test_recovery_discards_crashed_watcher_identity_before_replacement`
- `OrderedLedgerTests.test_stale_token_and_generation_fail_closed`

## P0-3: hidden watcher replay

### Race

The internal `watch` command could be run again after the task had already observed completion. A
sequential replay might re-poll the target, regenerate a prompt, and publish a second delivery for
the same event.

### Required fence

- Enter hidden `watch` only from task phase `scheduled` or `watching`.
- Require the lifetime guard and current task path/token/generation before observation.
- Permit a `watching` crash restart only through explicit recovery after the old guard is free.
- Reject `event_staged`, native/marker ready and delivery phases, `UNKNOWN`, accepted, blocked,
  failed, cancelled, dry-run complete, and every terminal phase.
- Write and fsync the final prompt plus digest before publishing `event_staged` and before assigning
  `READY`.
- Recover `event_staged` or ledger `READY` in place; never return it to process observation.
- Preserve the original maximum-wait deadline and ledger deferral budget across restart.

### Regression coverage

- `RaceFencingTests.test_watcher_rejects_v3_post_event_and_terminal_replays`
- `RaceFencingTests.test_scheduled_and_watching_v3_task_can_enter_only_one_watcher`
- `RaceFencingTests.test_event_staged_never_exposes_an_initial_placeholder_prompt`
- `CrashRecoveryTests.test_restarted_watcher_preserves_original_max_wait_deadline`
- `NativeMessageDeliveryTests.test_restart_preserves_submission_deferral_retry_budget`
- `NativeMessageDeliveryTests.test_timeout_after_request_is_unknown_and_replay_never_resends`

## Cross-layer monotonicity

The common job registry and protocol ledger are separate crash domains. Preserve this order:

```text
registration: common job reserve -> protocol ledger register
terminal delivery: protocol ledger outcome -> common job outcome -> task JSON mirror
```

Exact idempotent identities repair a crash during registration. During terminal convergence, the
protocol ledger is authoritative. A status/recover/cancel/dispatch path may advance common
`ACTIVE` to the matching `ACCEPTED`, `UNKNOWN`, `BLOCKED`, or `CANCELLED`; it may never move a
terminal/tombstone state backward.

Required regressions include:

- `MarkerLedgerTests.test_same_job_cannot_register_once_per_delivery_protocol`
- `MarkerLedgerTests.test_regular_forks_share_one_lineage_job_fence`
- `MarkerLedgerTests.test_job_registry_tombstones_accepted_but_allows_rejected_retries`
- `CrashRecoveryTests.test_recover_repairs_crash_after_either_reservation_commit`
- `CrashRecoveryTests.test_terminal_delivery_ledger_repairs_active_job_registry_monotonically`
- `CrossProtocolOwnerGateTests.test_unresolved_native_and_marker_cannot_mix_for_one_owner`
- `CrossProtocolOwnerGateTests.test_unknown_permanently_blocks_protocol_switch_for_one_owner`
- `CrossProtocolOwnerGateTests.test_protocol_switch_is_allowed_after_definitive_terminal_state`
- `CrossProtocolOwnerGateTests.test_distinct_fork_owners_do_not_share_the_protocol_gate`

## Refactor gate

Reject any future protocol, transport, cancellation, or recovery change that can:

1. release, cancel, or requeue an event after native request or marker output may have started;
2. let cancellation and delivery both publish successful terminal outcomes;
3. signal a watcher or target using a PID without its persisted strong incarnation;
4. let an old task, token, protocol/job generation, watcher identity, or authority epoch advance a
   newer reservation;
5. move a later event ahead of `SUBMITTING` or `UNKNOWN` in the same protocol ledger;
6. register the same job in sibling ordinary forks or once per delivery protocol;
7. let one exact owner bypass an unresolved event by switching between native and marker ledgers;
8. reuse an `ACCEPTED` job key without a new explicit logical cycle or process incarnation;
9. infer non-acceptance from a dead dispatcher, timeout, disconnect, or missing history; or
10. re-enter process observation from a post-event or terminal phase.

The primary executable suites are `tests/test_race_fencing.py`, `tests/test_ordered_ledger.py`,
`tests/test_native_message_delivery.py`, `tests/test_marker_ledger.py`,
`tests/test_cross_protocol_gate.py`, `tests/test_crash_recovery.py`, and
`tests/test_process_identity.py`.

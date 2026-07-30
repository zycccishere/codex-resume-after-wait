#!/usr/bin/env python3
"""Durable, authority-bound FIFO ledger for wait handoff events.

The JSON record is replaced atomically, while an adjacent ``.lock`` file has a
stable inode for its entire lifetime.  Every read-modify-write operation takes
an advisory ``flock`` on that stable inode.

This module deliberately does not perform network I/O.  A dispatcher first
persists READY -> SUBMITTING with :meth:`HandoffLedger.begin_next_submission`,
performs exactly one submission attempt, and then records a definitive result
with :meth:`HandoffLedger.finish_submission`.  An UNKNOWN result is a durable
fence: it is never retried automatically and blocks all later events.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 3

PRE_READY_STATES = frozenset({"SCHEDULED", "WATCHING"})
TERMINAL_STATES = frozenset({"ACCEPTED", "BLOCKED", "CANCELLED"})
FINAL_STATES = TERMINAL_STATES | {"UNKNOWN"}
QUEUE_STATES = frozenset({"READY", "SUBMITTING", "UNKNOWN"})
ENTRY_STATES = PRE_READY_STATES | TERMINAL_STATES | QUEUE_STATES
FINISH_OUTCOMES = {
    "accepted": "ACCEPTED",
    "unknown": "UNKNOWN",
    "blocked": "BLOCKED",
}


class LedgerError(RuntimeError):
    """Base class for durable-ledger protocol errors."""


class LedgerCorruption(LedgerError):
    """The persisted ledger does not satisfy the v3 schema invariants."""


class LedgerConflict(LedgerError):
    """A task or event identity is already bound to different data."""


class AuthorityMismatch(LedgerError):
    """An operation supplied an authority other than the ledger authority."""


class EpochMismatch(LedgerError):
    """An authority rebind compare-and-swap used a stale epoch."""


class StaleReservation(LedgerError):
    """A token or generation does not identify the current task entry."""


class InvalidTransition(LedgerError):
    """The requested state transition would violate monotonicity."""


class SubmissionBlocked(LedgerError):
    """An earlier FIFO event prevents the requested submission."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _canonical_authority(
    authority: Mapping[str, Any],
) -> tuple[dict[str, Any], int | None]:
    if not isinstance(authority, Mapping):
        raise TypeError("authority must be a mapping")
    result = _clone(dict(authority))
    supplied_epoch = result.pop("authority_epoch", None)
    if supplied_epoch is not None:
        if isinstance(supplied_epoch, bool) or not isinstance(supplied_epoch, int):
            raise ValueError("authority_epoch must be an integer")
        if supplied_epoch < 1:
            raise ValueError("authority_epoch must be at least 1")
    # Reject values that cannot be durably represented before touching state.
    json.dumps(result, sort_keys=True, separators=(",", ":"))
    return result, supplied_epoch


class HandoffLedger:
    """One durable FIFO ledger for one owner thread.

    ``state_dir`` may contain ledgers for many owners.  The owner thread ID is
    hashed so it cannot influence filesystem paths.  The full SHA-256 digest is
    used for both sibling files::

        <digest>.lock  # stable inode, never replaced
        <digest>.json  # atomically replaced durable record
    """

    def __init__(self, state_dir: str | os.PathLike[str], owner_thread_id: str) -> None:
        if not owner_thread_id:
            raise ValueError("owner_thread_id must not be empty")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.owner_thread_id = owner_thread_id
        digest = hashlib.sha256(owner_thread_id.encode("utf-8")).hexdigest()
        self.lock_path = self.state_dir / f"{digest}.lock"
        self.json_path = self.state_dir / f"{digest}.json"

    def register(
        self,
        task_id: str,
        event_id: str,
        task_file: str | os.PathLike[str],
        token: str,
        authority: Mapping[str, Any],
        job_key: str | None = None,
        *,
        state: str = "SCHEDULED",
    ) -> int:
        """Register an event and return its immutable fencing generation.

        Exact duplicate registration is idempotent.  Reusing either task_id or
        event_id with different identity data is rejected.  Authority changes
        require an explicit :meth:`rebind_authority` CAS.
        """

        if not task_id or not event_id or not token:
            raise ValueError("task_id, event_id, and token must not be empty")
        if job_key is not None and not job_key:
            raise ValueError("job_key must be non-empty when supplied")
        state = state.upper()
        if state not in PRE_READY_STATES:
            raise ValueError("registered state must be SCHEDULED or WATCHING")
        canonical_authority, supplied_epoch = _canonical_authority(authority)
        task_file_value = str(Path(task_file).expanduser().resolve())

        with self._locked(exclusive=True) as ledger:
            if ledger is None:
                epoch = supplied_epoch or 1
                ledger = self._new_ledger(canonical_authority, epoch)
            else:
                # Recovering a scheduler crash must be able to rediscover an
                # exact reservation even if the owner was frozen or rebound
                # after registration.  This lookup is read-only and still
                # requires every immutable identity field, including token.
                for entry in ledger["entries"]:
                    same_task = entry["task_id"] == task_id
                    same_event = entry["event_id"] == event_id
                    if not same_task and not same_event:
                        continue
                    exact = (
                        same_task
                        and same_event
                        and entry["task_file"] == task_file_value
                        and entry["token"] == token
                        and entry["job_key"] == job_key
                    )
                    if exact:
                        return int(entry["generation"])
                    raise LedgerConflict(
                        "task_id and event_id must remain uniquely bound to one reservation"
                    )
                if ledger["mode"] != "ACTIVE":
                    raise InvalidTransition(
                        "owner authority is frozen for handoff; new events cannot register"
                    )
                self._assert_authority(ledger, canonical_authority, supplied_epoch)

            # A newly-created ledger has no entries, but retaining this loop
            # keeps the conflict rule local if construction ever changes.
            for entry in ledger["entries"]:
                same_task = entry["task_id"] == task_id
                same_event = entry["event_id"] == event_id
                if not same_task and not same_event:
                    continue
                exact = (
                    same_task
                    and same_event
                    and entry["task_file"] == task_file_value
                    and entry["token"] == token
                    and entry["job_key"] == job_key
                )
                if exact:
                    return int(entry["generation"])
                raise LedgerConflict(
                    "task_id and event_id must remain uniquely bound to one reservation"
                )

            if job_key is not None:
                for entry in ledger["entries"]:
                    if (
                        entry["job_key"] == job_key
                        and entry["state"] not in {"BLOCKED", "CANCELLED"}
                    ):
                        raise LedgerConflict(
                            f"job_key {job_key!r} already has active task "
                            f"{entry['task_id']!r}"
                        )

            generation = int(ledger["next_generation"])
            ledger["next_generation"] = generation + 1
            timestamp = _now()
            ledger["entries"].append(
                {
                    "task_id": task_id,
                    "event_id": event_id,
                    "task_file": task_file_value,
                    "token": token,
                    "job_key": job_key,
                    "generation": generation,
                    "state": state,
                    "authority": _clone(ledger["authority"]),
                    "authority_epoch": ledger["authority_epoch"],
                    "ready_sequence": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            self._write_unlocked(ledger)
            return generation

    def find_exact_registration(
        self,
        task_id: str,
        event_id: str,
        task_file: str | os.PathLike[str],
        token: str,
        job_key: str | None,
    ) -> dict[str, Any] | None:
        """Read one exact registration without creating or changing it.

        ``None`` is proof, under the ledger lock, that neither this task id nor
        this event id is present.  A colliding identity is never treated as
        absence because compensating another ledger's common reservation in
        that situation would be unsafe.
        """

        task_file_value = str(Path(task_file).expanduser().resolve())
        with self._locked(exclusive=False) as ledger:
            if ledger is None:
                return None
            for entry in ledger["entries"]:
                same_task = entry["task_id"] == task_id
                same_event = entry["event_id"] == event_id
                if not same_task and not same_event:
                    continue
                exact = (
                    same_task
                    and same_event
                    and entry["task_file"] == task_file_value
                    and entry["token"] == token
                    and entry["job_key"] == job_key
                )
                if exact:
                    return _clone(entry)
                raise LedgerConflict(
                    "task_id and event_id collide with a different owner-ledger reservation"
                )
            return None

    def validate(self, task_id: str, token: str, generation: int) -> dict[str, Any]:
        """Return the entry if all fencing fields match, otherwise fail closed."""

        with self._locked(exclusive=False) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            return _clone(entry)

    def mark_watching(
        self, task_id: str, token: str, generation: int
    ) -> dict[str, Any]:
        """Persist SCHEDULED -> WATCHING; an exact replay is idempotent."""

        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            if entry["state"] == "SCHEDULED":
                entry["state"] = "WATCHING"
                entry["updated_at"] = _now()
                self._write_unlocked(ledger)
            elif entry["state"] != "WATCHING":
                raise InvalidTransition(
                    f"cannot mark {task_id!r} watching from {entry['state']}"
                )
            return _clone(entry)

    def mark_ready(self, task_id: str, token: str, generation: int) -> int:
        """Assign the next immutable FIFO ready sequence.

        Replaying the exact transition returns its original sequence.  No
        terminal or in-flight state can move backwards to READY.
        """

        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            if entry["state"] == "READY":
                return int(entry["ready_sequence"])
            if entry["state"] not in PRE_READY_STATES:
                raise InvalidTransition(
                    f"cannot mark {task_id!r} ready from {entry['state']}"
                )
            sequence = int(ledger["next_ready_sequence"])
            ledger["next_ready_sequence"] = sequence + 1
            entry["state"] = "READY"
            entry["ready_sequence"] = sequence
            entry["ready_at"] = _now()
            entry["updated_at"] = entry["ready_at"]
            self._write_unlocked(ledger)
            return sequence

    def begin_next_submission(
        self, task_id: str | None = None
    ) -> dict[str, Any] | None:
        """Atomically claim the earliest unresolved FIFO event for submission.

        Any dispatcher may omit ``task_id`` and submit the returned entry.  A
        task-specific dispatcher may pass its task ID; it only wins if that task
        is the earliest READY event.  Earlier SUBMITTING or UNKNOWN entries are
        durable fences and cause :class:`SubmissionBlocked`.
        """

        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            if ledger["mode"] != "ACTIVE":
                raise SubmissionBlocked("owner authority is frozen for handoff")
            unresolved = sorted(
                (
                    entry
                    for entry in ledger["entries"]
                    if entry["state"] in QUEUE_STATES
                ),
                key=lambda item: int(item["ready_sequence"]),
            )
            if not unresolved:
                return None
            entry = unresolved[0]
            if entry["state"] in {"SUBMITTING", "UNKNOWN"}:
                raise SubmissionBlocked(
                    f"earlier task {entry['task_id']!r} is {entry['state']}"
                )
            if task_id is not None and entry["task_id"] != task_id:
                raise SubmissionBlocked(
                    f"task {entry['task_id']!r} is ahead of {task_id!r} in the FIFO"
                )

            entry["state"] = "SUBMITTING"
            entry["submission_started_at"] = _now()
            entry["updated_at"] = entry["submission_started_at"]
            self._write_unlocked(ledger)
            return _clone(entry)

    def block_next_ready(
        self,
        task_id: str,
        token: str,
        generation: int,
        detail: str,
    ) -> dict[str, Any]:
        """Atomically finish the earliest READY event as BLOCKED without submission.

        This is for decisive pre-submission failures.  It deliberately skips
        SUBMITTING so a crash cannot later be mistaken for an ambiguous network
        attempt and fenced UNKNOWN.  The exact BLOCKED replay is idempotent.
        """

        if not detail:
            raise ValueError("block detail must not be empty")
        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            if entry["state"] == "BLOCKED":
                return _clone(entry)
            if ledger["mode"] != "ACTIVE":
                raise SubmissionBlocked("owner authority is frozen for handoff")
            unresolved = sorted(
                (
                    candidate
                    for candidate in ledger["entries"]
                    if candidate["state"] in QUEUE_STATES
                ),
                key=lambda candidate: int(candidate["ready_sequence"]),
            )
            if not unresolved:
                raise InvalidTransition(f"task {task_id!r} is not READY")
            earliest = unresolved[0]
            if earliest["state"] in {"SUBMITTING", "UNKNOWN"}:
                raise SubmissionBlocked(
                    f"earlier task {earliest['task_id']!r} is {earliest['state']}"
                )
            if earliest["task_id"] != task_id:
                raise SubmissionBlocked(
                    f"task {earliest['task_id']!r} is ahead of {task_id!r} in the FIFO"
                )
            if entry["state"] != "READY":
                raise InvalidTransition(
                    f"cannot block {task_id!r} without submission from {entry['state']}"
                )
            timestamp = _now()
            entry["state"] = "BLOCKED"
            entry["blocked_at"] = timestamp
            entry["submission_finished_at"] = timestamp
            entry["submission_detail"] = detail
            entry["blocked_without_submission"] = True
            entry["updated_at"] = timestamp
            self._write_unlocked(ledger)
            return _clone(entry)

    def finish_submission(
        self,
        task_id: str,
        token: str,
        generation: int,
        outcome: str,
        *,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Persist one definitive result: ACCEPTED, UNKNOWN, or BLOCKED.

        Repeating the same result is idempotent.  A conflicting result or any
        attempt to change UNKNOWN is rejected, preserving terminal monotonicity.
        """

        normalized_outcome = outcome.lower()
        if normalized_outcome not in FINISH_OUTCOMES:
            raise ValueError("outcome must be accepted, unknown, or blocked")
        target_state = FINISH_OUTCOMES[normalized_outcome]
        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            if entry["state"] == target_state:
                return _clone(entry)
            if entry["state"] != "SUBMITTING":
                raise InvalidTransition(
                    f"cannot finish {task_id!r} from {entry['state']} as {target_state}"
                )
            entry["state"] = target_state
            entry["submission_finished_at"] = _now()
            entry["updated_at"] = entry["submission_finished_at"]
            if detail is not None:
                entry["submission_detail"] = detail
            self._write_unlocked(ledger)
            return _clone(entry)

    def defer_submission(
        self,
        task_id: str,
        token: str,
        generation: int,
        detail: str,
        *,
        classification: str = "retryable",
    ) -> dict[str, Any]:
        """Return SUBMITTING to READY after proof that nothing was accepted.

        This is the sole deliberate non-monotonic-looking transition.  It is
        safe only when the app-server supplied definitive rejection evidence
        (for example ``ActiveTurnNotSteerable``).  The original ready sequence
        is retained, so this event stays ahead of every later event.
        """

        if not detail:
            raise ValueError("defer detail must describe definitive non-acceptance")
        if not classification:
            raise ValueError("defer classification must not be empty")
        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            if entry["state"] != "SUBMITTING":
                raise InvalidTransition(
                    f"cannot defer {task_id!r} from {entry['state']}"
                )
            timestamp = _now()
            entry.setdefault("submission_deferrals", []).append(
                {
                    "at": timestamp,
                    "detail": detail,
                    "classification": classification,
                }
            )
            entry["state"] = "READY"
            entry["updated_at"] = timestamp
            self._write_unlocked(ledger)
            return _clone(entry)

    def fence_interrupted_submission(
        self,
        task_id: str,
        token: str,
        generation: int,
        detail: str,
    ) -> dict[str, Any]:
        """Recover an interrupted SUBMITTING attempt as a permanent UNKNOWN.

        No automatic recovery path can move UNKNOWN back to READY.  A repeated
        fencing call is idempotent through :meth:`finish_submission`.
        """

        if not detail:
            raise ValueError("interruption detail must not be empty")
        return self.finish_submission(
            task_id,
            token,
            generation,
            "unknown",
            detail=detail,
        )

    def confirm_unknown_accepted(
        self,
        task_id: str,
        token: str,
        generation: int,
        evidence: str,
    ) -> dict[str, Any]:
        """Resolve UNKNOWN only with durable positive acceptance evidence.

        Absence from a history snapshot is never proof of non-acceptance and
        cannot move UNKNOWN. A matching persisted ``userMessage.clientId`` is
        sufficient to unblock later FIFO entries without sending again.
        """

        if not evidence:
            raise ValueError("positive acceptance evidence must not be empty")
        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            if entry["state"] == "ACCEPTED":
                return _clone(entry)
            if entry["state"] != "UNKNOWN":
                raise InvalidTransition(
                    f"cannot confirm {task_id!r} accepted from {entry['state']}"
                )
            timestamp = _now()
            entry["state"] = "ACCEPTED"
            entry["reconciled_at"] = timestamp
            entry["reconciliation_evidence"] = evidence
            entry["updated_at"] = timestamp
            self._write_unlocked(ledger)
            return _clone(entry)

    def cancel(self, task_id: str, token: str, generation: int) -> dict[str, Any]:
        """Cancel only a token- and generation-matched pre-ready event.

        Repeating the exact CANCELLED transition is idempotent.  This lets a
        recovery process converge the common job registry after a crash that
        happened between the owner-ledger commit and its task-file mirror.
        """

        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            entry = self._validated_entry(ledger, task_id, token, generation)
            if entry["state"] == "CANCELLED":
                return _clone(entry)
            if entry["state"] not in PRE_READY_STATES:
                raise InvalidTransition(
                    f"cannot cancel {task_id!r} from {entry['state']}"
                )
            entry["state"] = "CANCELLED"
            entry["cancelled_at"] = _now()
            entry["updated_at"] = entry["cancelled_at"]
            self._write_unlocked(ledger)
            return _clone(entry)

    def rebind_authority(
        self,
        expected_epoch: int,
        new_authority: Mapping[str, Any],
    ) -> int:
        """CAS the owner authority and increment its epoch exactly once.

        Rebinding is forbidden while a submission is in flight or has an
        ambiguous outcome.  Other non-terminal entries are atomically rebound
        so an already-ready FIFO can continue on the explicitly selected owner.
        """

        canonical_authority, supplied_epoch = _canonical_authority(new_authority)
        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            current_epoch = int(ledger["authority_epoch"])
            if expected_epoch != current_epoch:
                raise EpochMismatch(
                    f"expected authority epoch {expected_epoch}, found {current_epoch}"
                )
            if ledger["mode"] != "DRAINING":
                raise InvalidTransition(
                    "authority must be frozen (mode=DRAINING) before rebind"
                )
            new_epoch = current_epoch + 1
            if supplied_epoch is not None and supplied_epoch != new_epoch:
                raise EpochMismatch(
                    f"new authority epoch must be {new_epoch}, got {supplied_epoch}"
                )
            fenced = [
                entry["task_id"]
                for entry in ledger["entries"]
                if entry["state"] in {"SUBMITTING", "UNKNOWN"}
            ]
            if fenced:
                raise InvalidTransition(
                    "cannot rebind authority while submission outcome is in flight or "
                    f"unknown: {', '.join(fenced)}"
                )

            ledger["authority"] = canonical_authority
            ledger["authority_epoch"] = new_epoch
            ledger["mode"] = "ACTIVE"
            ledger["authority_rebound_at"] = _now()
            for entry in ledger["entries"]:
                if entry["state"] not in TERMINAL_STATES:
                    entry["authority"] = _clone(canonical_authority)
                    entry["authority_epoch"] = new_epoch
                    entry["updated_at"] = ledger["authority_rebound_at"]
            self._write_unlocked(ledger)
            return new_epoch

    def freeze_authority(self, expected_epoch: int) -> dict[str, Any]:
        """Atomically stop new submissions before an execution-host handoff."""

        with self._locked(exclusive=True) as ledger:
            ledger = self._require_ledger(ledger)
            if int(ledger["authority_epoch"]) != expected_epoch:
                raise EpochMismatch(
                    f"expected authority epoch {expected_epoch}, found "
                    f"{ledger['authority_epoch']}"
                )
            fenced = [
                entry["task_id"]
                for entry in ledger["entries"]
                if entry["state"] in {"SUBMITTING", "UNKNOWN"}
            ]
            if fenced:
                raise InvalidTransition(
                    "cannot freeze while a submission is in flight or unknown: "
                    + ", ".join(fenced)
                )
            if ledger["mode"] == "DRAINING":
                return _clone(ledger)
            ledger["mode"] = "DRAINING"
            ledger["authority_frozen_at"] = _now()
            self._write_unlocked(ledger)
            return _clone(ledger)

    def snapshot(self) -> dict[str, Any] | None:
        """Return a consistent deep copy of the current durable record."""

        with self._locked(exclusive=False) as ledger:
            return _clone(ledger)

    def _new_ledger(self, authority: Mapping[str, Any], epoch: int) -> dict[str, Any]:
        timestamp = _now()
        return {
            "schema_version": SCHEMA_VERSION,
            "owner_thread_id": self.owner_thread_id,
            "authority": _clone(dict(authority)),
            "authority_epoch": epoch,
            "mode": "ACTIVE",
            "next_generation": 1,
            "next_ready_sequence": 1,
            "entries": [],
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _assert_authority(
        self,
        ledger: Mapping[str, Any],
        authority: Mapping[str, Any],
        supplied_epoch: int | None,
    ) -> None:
        if ledger["authority"] != authority:
            raise AuthorityMismatch(
                "registered authority differs; use rebind_authority with the current epoch"
            )
        if supplied_epoch is not None and ledger["authority_epoch"] != supplied_epoch:
            raise AuthorityMismatch(
                f"registered authority epoch {supplied_epoch} does not match "
                f"{ledger['authority_epoch']}"
            )

    @staticmethod
    def _require_ledger(ledger: dict[str, Any] | None) -> dict[str, Any]:
        if ledger is None:
            raise LedgerError("ledger has not been registered")
        return ledger

    @staticmethod
    def _find_entry(ledger: Mapping[str, Any], task_id: str) -> dict[str, Any]:
        for entry in ledger["entries"]:
            if entry["task_id"] == task_id:
                return entry
        raise StaleReservation(f"task {task_id!r} is not registered")

    def _validated_entry(
        self,
        ledger: Mapping[str, Any],
        task_id: str,
        token: str,
        generation: int,
    ) -> dict[str, Any]:
        entry = self._find_entry(ledger, task_id)
        if entry["token"] != token or entry["generation"] != generation:
            raise StaleReservation(f"stale token or generation for task {task_id!r}")
        return entry

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[dict[str, Any] | None]:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            with os.fdopen(descriptor, "r+b", closefd=True) as handle:
                descriptor = -1
                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                )
                try:
                    ledger = self._read_unlocked()
                    yield ledger
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_unlocked(self) -> dict[str, Any] | None:
        if not self.json_path.exists():
            return None
        try:
            with self.json_path.open("r", encoding="utf-8") as handle:
                ledger = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerCorruption(f"cannot read {self.json_path}: {exc}") from exc
        self._validate_schema(ledger)
        return ledger

    def _validate_schema(self, ledger: Any) -> None:
        if not isinstance(ledger, dict):
            raise LedgerCorruption("ledger root must be an object")
        required = {
            "schema_version",
            "owner_thread_id",
            "authority",
            "authority_epoch",
            "mode",
            "next_generation",
            "next_ready_sequence",
            "entries",
        }
        missing = required - ledger.keys()
        if missing:
            raise LedgerCorruption(f"ledger is missing fields: {sorted(missing)}")
        if ledger["schema_version"] != SCHEMA_VERSION:
            raise LedgerCorruption(
                f"expected schema version {SCHEMA_VERSION}, got {ledger['schema_version']}"
            )
        if ledger["owner_thread_id"] != self.owner_thread_id:
            raise LedgerCorruption("ledger owner does not match its hashed path")
        if not isinstance(ledger["authority"], dict):
            raise LedgerCorruption("authority must be an object")
        if ledger["mode"] not in {"ACTIVE", "DRAINING"}:
            raise LedgerCorruption("mode must be ACTIVE or DRAINING")
        for field in ("authority_epoch", "next_generation", "next_ready_sequence"):
            value = ledger[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LedgerCorruption(f"{field} must be a positive integer")
        if not isinstance(ledger["entries"], list):
            raise LedgerCorruption("entries must be an array")

        task_ids: set[str] = set()
        event_ids: set[str] = set()
        generations: set[int] = set()
        sequences: set[int] = set()
        queue_fences = 0
        for entry in ledger["entries"]:
            if not isinstance(entry, dict):
                raise LedgerCorruption("every entry must be an object")
            for field in (
                "task_id",
                "event_id",
                "task_file",
                "token",
                "job_key",
                "generation",
                "state",
                "authority",
                "authority_epoch",
                "ready_sequence",
            ):
                if field not in entry:
                    raise LedgerCorruption(f"entry is missing {field}")
            if entry["state"] not in ENTRY_STATES:
                raise LedgerCorruption(f"unknown entry state {entry['state']!r}")
            if entry["job_key"] is not None and (
                not isinstance(entry["job_key"], str) or not entry["job_key"]
            ):
                raise LedgerCorruption("job_key must be null or a non-empty string")
            generation = entry["generation"]
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 1
            ):
                raise LedgerCorruption("entry generation must be a positive integer")
            entry_epoch = entry["authority_epoch"]
            if (
                isinstance(entry_epoch, bool)
                or not isinstance(entry_epoch, int)
                or entry_epoch < 1
            ):
                raise LedgerCorruption(
                    "entry authority_epoch must be a positive integer"
                )
            if not isinstance(entry["authority"], dict):
                raise LedgerCorruption("entry authority must be an object")
            if entry["task_id"] in task_ids or entry["event_id"] in event_ids:
                raise LedgerCorruption("task_id and event_id must be unique")
            if generation in generations:
                raise LedgerCorruption("generations must be unique")
            task_ids.add(entry["task_id"])
            event_ids.add(entry["event_id"])
            generations.add(generation)
            if entry["state"] in {"SUBMITTING", "UNKNOWN"}:
                queue_fences += 1
            sequence = entry["ready_sequence"]
            if entry["state"] in QUEUE_STATES | {"ACCEPTED", "BLOCKED"}:
                if (
                    isinstance(sequence, bool)
                    or not isinstance(sequence, int)
                    or sequence < 1
                ):
                    raise LedgerCorruption(
                        "post-ready entries need a positive ready_sequence"
                    )
            elif sequence is not None:
                raise LedgerCorruption(
                    "pre-ready/cancelled entries cannot have ready_sequence"
                )
            if sequence is not None:
                if sequence in sequences:
                    raise LedgerCorruption("ready_sequence values must be unique")
                sequences.add(sequence)
        if queue_fences > 1:
            raise LedgerCorruption(
                "at most one SUBMITTING/UNKNOWN FIFO fence may exist"
            )
        if generations and ledger["next_generation"] <= max(generations):
            raise LedgerCorruption(
                "next_generation must exceed all allocated generations"
            )
        if sequences and ledger["next_ready_sequence"] <= max(sequences):
            raise LedgerCorruption(
                "next_ready_sequence must exceed all allocated ready sequences"
            )

    def _write_unlocked(self, ledger: dict[str, Any]) -> None:
        ledger["updated_at"] = _now()
        self._validate_schema(ledger)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.json_path.name}.",
            suffix=".tmp",
            dir=self.state_dir,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                descriptor = -1
                json.dump(ledger, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.json_path)
            os.chmod(self.json_path, 0o600)
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


# Short alias for callers that prefer the protocol noun.
Ledger = HandoffLedger


__all__ = [
    "AuthorityMismatch",
    "EpochMismatch",
    "HandoffLedger",
    "InvalidTransition",
    "Ledger",
    "LedgerConflict",
    "LedgerCorruption",
    "LedgerError",
    "StaleReservation",
    "SubmissionBlocked",
]

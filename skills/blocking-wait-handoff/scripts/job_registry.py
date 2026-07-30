#!/usr/bin/env python3
"""Protocol-independent one-active-event-per-job reservations."""

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
from typing import Any, Iterator


SCHEMA_VERSION = 2
UNRESOLVED_STATES = frozenset({"ACTIVE", "UNKNOWN"})
# ACCEPTED is a permanent deduplication tombstone for one logical job.  A
# caller that intentionally wants another monitoring cycle must supply a new
# logical job id, while forks/replays using the default cannot wake twice.
DEDUP_STATES = UNRESOLVED_STATES | {"ACCEPTED"}
TERMINAL_STATES = frozenset({"ACCEPTED", "BLOCKED", "CANCELLED"})
STATES = UNRESOLVED_STATES | TERMINAL_STATES
OUTCOMES = {
    "accepted": "ACCEPTED",
    "blocked": "BLOCKED",
    "cancelled": "CANCELLED",
    "unknown": "UNKNOWN",
}


class JobRegistryError(RuntimeError):
    pass


class JobConflict(JobRegistryError):
    pass


class StaleJobReservation(JobRegistryError):
    pass


class InvalidJobTransition(JobRegistryError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OwnerJobRegistry:
    """Fence one logical job lineage and unsafe per-owner protocol mixing.

    The registry is keyed by the oldest durable fork-lineage thread, while
    ``delivery_owner_id`` records the exact branch that owns an event.  This
    lets forks deduplicate the same job without unnecessarily serializing
    unrelated owners.  For one exact owner, however, native and marker events
    may not both remain unresolved: they use different transport ledgers and
    therefore cannot share one atomic READY-order queue.
    """

    def __init__(self, state_dir: str | os.PathLike[str], owner_thread_id: str) -> None:
        if not owner_thread_id:
            raise ValueError("owner_thread_id must not be empty")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.owner_thread_id = owner_thread_id
        digest = hashlib.sha256(owner_thread_id.encode("utf-8")).hexdigest()
        self.lock_path = self.state_dir / f"{digest}.jobs.lock"
        self.json_path = self.state_dir / f"{digest}.jobs.json"

    def reserve(
        self,
        job_key: str,
        task_id: str,
        event_id: str,
        task_file: str | os.PathLike[str],
        token: str,
        protocol: str,
        delivery_owner_id: str | None = None,
    ) -> int:
        delivery_owner_id = delivery_owner_id or self.owner_thread_id
        if not all(
            (job_key, task_id, event_id, token, protocol, delivery_owner_id)
        ):
            raise ValueError("job reservation identity fields must not be empty")
        task_file_value = str(Path(task_file).expanduser().resolve())
        with self._locked(exclusive=True) as registry:
            if registry is None:
                registry = {
                    "schema_version": SCHEMA_VERSION,
                    "owner_thread_id": self.owner_thread_id,
                    "next_generation": 1,
                    "entries": [],
                    "created_at": _now(),
                }
            for entry in registry["entries"]:
                same_identity = entry["task_id"] == task_id or entry["event_id"] == event_id
                if same_identity:
                    if (
                        entry["task_id"] == task_id
                        and entry["event_id"] == event_id
                        and entry["job_key"] == job_key
                        and entry["task_file"] == task_file_value
                        and entry["token"] == token
                        and entry["protocol"] == protocol
                        and entry["delivery_owner_id"] == delivery_owner_id
                    ):
                        if entry["state"] == "ACTIVE":
                            return int(entry["generation"])
                        raise InvalidJobTransition(
                            "exact job reservation cannot be reused for registration from "
                            f"state {entry['state']}"
                        )
                    raise JobConflict("task_id/event_id is already bound to another job reservation")
                if entry["job_key"] == job_key and entry["state"] in DEDUP_STATES:
                    raise JobConflict(
                        f"job already has a deduplicating task {entry['task_id']!r} "
                        f"in state {entry['state']}"
                    )
                if (
                    entry["delivery_owner_id"] == delivery_owner_id
                    and entry["state"] in UNRESOLVED_STATES
                    and entry["protocol"] != protocol
                ):
                    raise JobConflict(
                        "one delivery owner cannot mix native-message and marker "
                        "while an earlier event is unresolved; finish, cancel, or "
                        "reconcile the earlier event first"
                    )
            generation = int(registry["next_generation"])
            registry["next_generation"] = generation + 1
            timestamp = _now()
            registry["entries"].append(
                {
                    "job_key": job_key,
                    "task_id": task_id,
                    "event_id": event_id,
                    "task_file": task_file_value,
                    "token": token,
                    "protocol": protocol,
                    "delivery_owner_id": delivery_owner_id,
                    "generation": generation,
                    "state": "ACTIVE",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            self._write_unlocked(registry)
            return generation

    def find_exact_reservation(
        self,
        job_key: str,
        task_id: str,
        event_id: str,
        task_file: str | os.PathLike[str],
        token: str,
        protocol: str,
        delivery_owner_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read one exact job reservation without creating or changing it."""

        delivery_owner_id = delivery_owner_id or self.owner_thread_id
        task_file_value = str(Path(task_file).expanduser().resolve())
        with self._locked(exclusive=False) as registry:
            if registry is None:
                return None
            for entry in registry["entries"]:
                same_task = entry["task_id"] == task_id
                same_event = entry["event_id"] == event_id
                if not same_task and not same_event:
                    continue
                exact = (
                    same_task
                    and same_event
                    and entry["job_key"] == job_key
                    and entry["task_file"] == task_file_value
                    and entry["token"] == token
                    and entry["protocol"] == protocol
                    and entry["delivery_owner_id"] == delivery_owner_id
                )
                if exact:
                    return copy.deepcopy(entry)
                raise JobConflict(
                    "task_id/event_id collides with a different common job reservation"
                )
            return None

    def finish(
        self,
        task_id: str,
        token: str,
        generation: int,
        outcome: str,
    ) -> dict[str, Any]:
        target = OUTCOMES.get(outcome.lower())
        if target is None:
            raise ValueError(f"unknown job reservation outcome {outcome!r}")
        with self._locked(exclusive=True) as registry:
            registry = self._require(registry)
            entry = self._entry(registry, task_id, token, generation)
            if entry["state"] == target:
                return copy.deepcopy(entry)
            if entry["state"] == "UNKNOWN" and target == "ACCEPTED":
                pass
            elif entry["state"] != "ACTIVE":
                raise InvalidJobTransition(
                    f"cannot finish job task {task_id!r} from {entry['state']} as {target}"
                )
            entry["state"] = target
            entry["updated_at"] = _now()
            entry["finished_at"] = entry["updated_at"]
            self._write_unlocked(registry)
            return copy.deepcopy(entry)

    def validate(self, task_id: str, token: str, generation: int) -> dict[str, Any]:
        with self._locked(exclusive=False) as registry:
            registry = self._require(registry)
            return copy.deepcopy(self._entry(registry, task_id, token, generation))

    def snapshot(self) -> dict[str, Any] | None:
        with self._locked(exclusive=False) as registry:
            return copy.deepcopy(registry)

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[dict[str, Any] | None]:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield self._read_unlocked()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any] | None:
        if not self.json_path.exists():
            return None
        try:
            with self.json_path.open("r", encoding="utf-8") as handle:
                registry = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise JobRegistryError(f"cannot read job registry: {error}") from error
        self._validate(registry)
        return registry

    def _write_unlocked(self, registry: dict[str, Any]) -> None:
        registry["updated_at"] = _now()
        self._validate(registry)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.json_path.name}.", suffix=".tmp", dir=self.state_dir
        )
        path = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(registry, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(path, self.json_path)
            os.chmod(self.json_path, 0o600)
            directory_descriptor = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            path.unlink(missing_ok=True)

    def _validate(self, registry: Any) -> None:
        if not isinstance(registry, dict):
            raise JobRegistryError("job registry root must be an object")
        if registry.get("schema_version") != SCHEMA_VERSION:
            raise JobRegistryError("unsupported job registry schema")
        if registry.get("owner_thread_id") != self.owner_thread_id:
            raise JobRegistryError("job registry owner mismatch")
        if not isinstance(registry.get("next_generation"), int):
            raise JobRegistryError("job registry next_generation is invalid")
        entries = registry.get("entries")
        if not isinstance(entries, list):
            raise JobRegistryError("job registry entries must be an array")
        identities: set[str] = set()
        generations: set[int] = set()
        active_jobs: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("state") not in STATES:
                raise JobRegistryError("job registry entry is invalid")
            if not isinstance(entry.get("delivery_owner_id"), str) or not entry[
                "delivery_owner_id"
            ]:
                raise JobRegistryError("job registry delivery_owner_id is invalid")
            if entry.get("task_id") in identities or entry.get("event_id") in identities:
                raise JobRegistryError("job registry identities must be unique")
            identities.update((entry.get("task_id"), entry.get("event_id")))
            generation = entry.get("generation")
            if not isinstance(generation, int) or generation < 1 or generation in generations:
                raise JobRegistryError("job registry generation is invalid")
            generations.add(generation)
            if entry["state"] in DEDUP_STATES:
                if entry.get("job_key") in active_jobs:
                    raise JobRegistryError("job has more than one unresolved reservation")
                active_jobs.add(entry.get("job_key"))

    @staticmethod
    def _require(registry: dict[str, Any] | None) -> dict[str, Any]:
        if registry is None:
            raise JobRegistryError("job registry does not exist")
        return registry

    @staticmethod
    def _entry(
        registry: dict[str, Any], task_id: str, token: str, generation: int
    ) -> dict[str, Any]:
        for entry in registry["entries"]:
            if entry["task_id"] == task_id:
                if entry["token"] != token or entry["generation"] != generation:
                    raise StaleJobReservation(f"stale job reservation for {task_id!r}")
                return entry
        raise StaleJobReservation(f"unknown job reservation for {task_id!r}")


__all__ = [
    "InvalidJobTransition",
    "JobConflict",
    "JobRegistryError",
    "OwnerJobRegistry",
    "StaleJobReservation",
]

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from handoff_ledger import (  # noqa: E402
    AuthorityMismatch,
    EpochMismatch,
    HandoffLedger,
    InvalidTransition,
    LedgerConflict,
    StaleReservation,
    SubmissionBlocked,
)


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
AUTHORITY_A = {
    "kind": "unix",
    "endpoint_uri": "unix:///tmp/app-server-a.sock",
    "endpoint_fingerprint": "sha256:a",
    "execution_host_id": "host-a",
}
AUTHORITY_B = {
    "kind": "websocket",
    "endpoint_uri": "wss://relay.example.invalid/app-server",
    "endpoint_fingerprint": "sha256:b",
    "execution_host_id": "host-b",
}


class OrderedLedgerTests(unittest.TestCase):
    def make_ledger(self, root: str) -> HandoffLedger:
        return HandoffLedger(Path(root) / "coordination", OWNER)

    @staticmethod
    def register(
        ledger: HandoffLedger,
        index: int,
        *,
        authority: dict[str, str] = AUTHORITY_A,
    ) -> tuple[str, str, int]:
        task_id = f"task-{index}"
        token = f"token-{index}"
        generation = ledger.register(
            task_id,
            f"event-{index}",
            f"/tmp/task-{index}.json",
            token,
            authority,
        )
        return task_id, token, generation

    def test_multiple_registrations_deliver_in_ready_fifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            first = self.register(ledger, 1)
            second = self.register(ledger, 2)
            self.assertEqual(first[2], 1)
            self.assertEqual(second[2], 2)

            # FIFO is completion/ready order, not process-registration order.
            self.assertEqual(ledger.mark_ready(*second), 1)
            self.assertEqual(ledger.mark_ready(*first), 2)
            with self.assertRaises(SubmissionBlocked):
                ledger.begin_next_submission(first[0])

            claimed_second = ledger.begin_next_submission(second[0])
            assert claimed_second is not None
            self.assertEqual(claimed_second["task_id"], second[0])
            self.assertEqual(claimed_second["state"], "SUBMITTING")
            ledger.finish_submission(*second, "accepted")

            # Any dispatcher can claim the next entry without owning its task.
            claimed_first = ledger.begin_next_submission()
            assert claimed_first is not None
            self.assertEqual(claimed_first["task_id"], first[0])
            ledger.finish_submission(*first, "blocked")
            self.assertIsNone(ledger.begin_next_submission())

            snapshot = ledger.snapshot()
            assert snapshot is not None
            self.assertEqual(snapshot["owner_thread_id"], OWNER)
            self.assertEqual(snapshot["next_generation"], 3)
            self.assertEqual(snapshot["next_ready_sequence"], 3)
            self.assertEqual(
                [entry["state"] for entry in snapshot["entries"]],
                ["BLOCKED", "ACCEPTED"],
            )

    def test_unknown_is_monotonic_and_blocks_later_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            first = self.register(ledger, 1)
            second = self.register(ledger, 2)
            ledger.mark_ready(*first)
            ledger.mark_ready(*second)
            ledger.begin_next_submission(first[0])
            ledger.finish_submission(
                *first, "unknown", detail="socket closed after write"
            )

            with self.assertRaises(SubmissionBlocked):
                ledger.begin_next_submission(second[0])
            with self.assertRaises(SubmissionBlocked):
                ledger.begin_next_submission()
            with self.assertRaises(InvalidTransition):
                ledger.finish_submission(*first, "accepted")
            with self.assertRaises(InvalidTransition):
                ledger.mark_ready(*first)
            with self.assertRaises(InvalidTransition):
                ledger.rebind_authority(1, AUTHORITY_B)

    def test_definitive_rejection_defers_with_the_same_fifo_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            first = self.register(ledger, 1)
            second = self.register(ledger, 2)
            first_sequence = ledger.mark_ready(*first)
            ledger.mark_ready(*second)
            ledger.begin_next_submission(first[0])

            deferred = ledger.defer_submission(
                *first,
                detail="synchronous JSON-RPC rejection proves no input was accepted",
            )
            self.assertEqual(deferred["state"], "READY")
            self.assertEqual(deferred["ready_sequence"], first_sequence)
            self.assertEqual(len(deferred["submission_deferrals"]), 1)
            with self.assertRaises(SubmissionBlocked):
                ledger.begin_next_submission(second[0])

            retried = ledger.begin_next_submission()
            assert retried is not None
            self.assertEqual(retried["task_id"], first[0])
            self.assertEqual(retried["ready_sequence"], first_sequence)
            ledger.finish_submission(*first, "accepted")

    def test_decisive_pre_submission_block_is_atomic_and_fifo_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            first = self.register(ledger, 1)
            second = self.register(ledger, 2)
            first_sequence = ledger.mark_ready(*first)
            second_sequence = ledger.mark_ready(*second)

            with self.assertRaises(SubmissionBlocked):
                ledger.block_next_ready(*second, detail="cannot bypass first")

            blocked_first = ledger.block_next_ready(
                *first,
                detail="decisive pre-submission failure",
            )
            self.assertEqual(blocked_first["state"], "BLOCKED")
            self.assertEqual(blocked_first["ready_sequence"], first_sequence)
            self.assertTrue(blocked_first["blocked_without_submission"])
            self.assertNotIn("submission_started_at", blocked_first)

            replay = ledger.block_next_ready(
                *first,
                detail="idempotent recovery replay",
            )
            self.assertEqual(replay, blocked_first)

            blocked_second = ledger.block_next_ready(
                *second,
                detail="second is now earliest",
            )
            self.assertEqual(blocked_second["state"], "BLOCKED")
            self.assertEqual(blocked_second["ready_sequence"], second_sequence)

    def test_atomic_block_refuses_non_ready_or_ambiguous_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            scheduled = self.register(ledger, 1)
            with self.assertRaises(InvalidTransition):
                ledger.block_next_ready(*scheduled, detail="not ready")

            first = self.register(ledger, 2)
            second = self.register(ledger, 3)
            ledger.mark_ready(*first)
            ledger.mark_ready(*second)
            ledger.begin_next_submission(first[0])
            with self.assertRaises(SubmissionBlocked):
                ledger.block_next_ready(*first, detail="already submitting")
            with self.assertRaises(SubmissionBlocked):
                ledger.block_next_ready(*second, detail="earlier attempt is ambiguous")

    def test_interrupted_submission_is_explicitly_fenced_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            first = self.register(ledger, 1)
            second = self.register(ledger, 2)
            ledger.mark_ready(*first)
            ledger.mark_ready(*second)
            ledger.begin_next_submission(first[0])

            fenced = ledger.fence_interrupted_submission(
                *first,
                detail="dispatcher restarted with a persisted SUBMITTING attempt",
            )
            self.assertEqual(fenced["state"], "UNKNOWN")
            self.assertEqual(
                fenced["submission_detail"],
                "dispatcher restarted with a persisted SUBMITTING attempt",
            )
            # Explicit recovery is idempotent, but UNKNOWN can never be retried.
            replay = ledger.fence_interrupted_submission(
                *first,
                detail="replayed recovery",
            )
            self.assertEqual(replay["state"], "UNKNOWN")
            with self.assertRaises(InvalidTransition):
                ledger.defer_submission(*first, detail="unsafe retry")
            with self.assertRaises(SubmissionBlocked):
                ledger.begin_next_submission(second[0])

    def test_authority_mismatch_requires_explicit_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            self.register(ledger, 1)
            with self.assertRaises(AuthorityMismatch):
                self.register(ledger, 2, authority=AUTHORITY_B)
            with self.assertRaises(AuthorityMismatch):
                ledger.register(
                    "task-2",
                    "event-2",
                    "/tmp/task-2.json",
                    "token-2",
                    {**AUTHORITY_A, "authority_epoch": 2},
                )
            self.assertEqual(len(ledger.snapshot()["entries"]), 1)  # type: ignore[index]

    def test_job_key_allows_parallel_jobs_but_only_one_active_event_per_job(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            first = ledger.register(
                "task-1",
                "event-1",
                "/tmp/task-1.json",
                "token-1",
                AUTHORITY_A,
                job_key="build-a",
            )
            second = ledger.register(
                "task-2",
                "event-2",
                "/tmp/task-2.json",
                "token-2",
                AUTHORITY_A,
                job_key="build-b",
            )
            self.assertEqual((first, second), (1, 2))
            with self.assertRaises(LedgerConflict):
                ledger.register(
                    "task-3",
                    "event-3",
                    "/tmp/task-3.json",
                    "token-3",
                    AUTHORITY_A,
                    job_key="build-a",
                )

            ledger.cancel("task-1", "token-1", first)
            replacement = ledger.register(
                "task-3",
                "event-3",
                "/tmp/task-3.json",
                "token-3",
                AUTHORITY_A,
                job_key="build-a",
            )
            self.assertEqual(replacement, 3)
            snapshot = ledger.snapshot()
            assert snapshot is not None
            self.assertEqual(
                [entry["job_key"] for entry in snapshot["entries"]],
                ["build-a", "build-b", "build-a"],
            )

    def test_accepted_job_key_is_a_permanent_deduplication_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            generation = ledger.register(
                "task-1",
                "event-1",
                "/tmp/task-1.json",
                "token-1",
                AUTHORITY_A,
                job_key="process-lifetime-key",
            )
            ledger.mark_ready("task-1", "token-1", generation)
            ledger.begin_next_submission("task-1")
            ledger.finish_submission(
                "task-1",
                "token-1",
                generation,
                "accepted",
            )

            with self.assertRaises(LedgerConflict):
                ledger.register(
                    "task-2",
                    "event-2",
                    "/tmp/task-2.json",
                    "token-2",
                    AUTHORITY_A,
                    job_key="process-lifetime-key",
                )

            snapshot = ledger.snapshot()
            assert snapshot is not None
            self.assertEqual(len(snapshot["entries"]), 1)
            self.assertEqual(snapshot["entries"][0]["state"], "ACCEPTED")

    def test_stale_token_and_generation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            reservation = self.register(ledger, 1)
            task_id, token, generation = reservation
            for operation in (
                lambda: ledger.validate(task_id, "stale", generation),
                lambda: ledger.validate(task_id, token, generation + 1),
                lambda: ledger.mark_ready(task_id, "stale", generation),
                lambda: ledger.cancel(task_id, token, generation + 1),
            ):
                with (
                    self.subTest(operation=operation),
                    self.assertRaises(StaleReservation),
                ):
                    operation()
            entry = ledger.validate(*reservation)
            self.assertEqual(entry["state"], "SCHEDULED")

    def test_exact_cancel_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            reservation = self.register(ledger, 1)

            first = ledger.cancel(*reservation)
            after_first = ledger.snapshot()
            second = ledger.cancel(*reservation)

            self.assertEqual(first["state"], "CANCELLED")
            self.assertEqual(second, first)
            self.assertEqual(ledger.snapshot(), after_first)

    def test_cancel_and_submission_claim_are_serialized(self) -> None:
        # Exercise the cancellation boundary repeatedly.  Depending on lock
        # acquisition order either cancellation wins before READY, or READY and
        # SUBMITTING win; both outcomes together are impossible.
        for iteration in range(30):
            with (
                self.subTest(iteration=iteration),
                tempfile.TemporaryDirectory() as temp,
            ):
                ledger = self.make_ledger(temp)
                reservation = self.register(ledger, iteration)
                barrier = threading.Barrier(2)
                outcomes: list[str] = []

                def make_ready_and_begin() -> None:
                    barrier.wait()
                    try:
                        ledger.mark_ready(*reservation)
                        entry = ledger.begin_next_submission(reservation[0])
                        outcomes.append("submitted" if entry else "empty")
                    except InvalidTransition:
                        outcomes.append("ready-rejected")

                def cancel() -> None:
                    barrier.wait()
                    try:
                        ledger.cancel(*reservation)
                        outcomes.append("cancelled")
                    except InvalidTransition:
                        outcomes.append("cancel-rejected")

                first = threading.Thread(target=make_ready_and_begin)
                second = threading.Thread(target=cancel)
                first.start()
                second.start()
                first.join(timeout=5)
                second.join(timeout=5)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())

                entry = ledger.validate(*reservation)
                self.assertIn(entry["state"], {"CANCELLED", "SUBMITTING"})
                if entry["state"] == "CANCELLED":
                    self.assertIn("cancelled", outcomes)
                    self.assertIn("ready-rejected", outcomes)
                    self.assertNotIn("submitted", outcomes)
                else:
                    self.assertIn("submitted", outcomes)
                    self.assertIn("cancel-rejected", outcomes)
                    self.assertNotIn("cancelled", outcomes)

    def test_rebind_is_epoch_cas_and_preserves_historical_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            accepted = self.register(ledger, 1)
            self.register(ledger, 2)
            ledger.mark_ready(*accepted)
            ledger.begin_next_submission(accepted[0])
            ledger.finish_submission(*accepted, "accepted")

            ledger.freeze_authority(1)
            self.assertEqual(ledger.rebind_authority(1, AUTHORITY_B), 2)
            snapshot = ledger.snapshot()
            assert snapshot is not None
            self.assertEqual(snapshot["authority"], AUTHORITY_B)
            self.assertEqual(snapshot["authority_epoch"], 2)
            accepted_entry, pending_entry = snapshot["entries"]
            self.assertEqual(accepted_entry["authority"], AUTHORITY_A)
            self.assertEqual(accepted_entry["authority_epoch"], 1)
            self.assertEqual(pending_entry["authority"], AUTHORITY_B)
            self.assertEqual(pending_entry["authority_epoch"], 2)

            with self.assertRaises(EpochMismatch):
                ledger.rebind_authority(1, AUTHORITY_A)
            ledger.freeze_authority(2)
            self.assertEqual(
                ledger.rebind_authority(2, {**AUTHORITY_A, "authority_epoch": 3}), 3
            )

    def test_active_authority_cannot_rebind_without_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            self.register(ledger, 1)
            before = ledger.snapshot()

            with self.assertRaisesRegex(InvalidTransition, "must be frozen"):
                ledger.rebind_authority(1, AUTHORITY_B)

            self.assertEqual(ledger.snapshot(), before)

    def test_rebind_rejects_submitting_and_lock_inode_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            reservation = self.register(ledger, 1)
            lock_inode = ledger.lock_path.stat().st_ino
            json_inode = ledger.json_path.stat().st_ino
            ledger.mark_ready(*reservation)
            ledger.begin_next_submission(reservation[0])
            self.assertEqual(ledger.lock_path.stat().st_ino, lock_inode)
            self.assertNotEqual(ledger.json_path.stat().st_ino, json_inode)
            with self.assertRaises(InvalidTransition):
                ledger.rebind_authority(1, AUTHORITY_B)

    def test_freeze_and_rebind_form_an_epoch_cas_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_ledger(temp)
            pending = self.register(ledger, 1)
            ready_sequence = ledger.mark_ready(*pending)

            frozen = ledger.freeze_authority(1)
            self.assertEqual(frozen["mode"], "DRAINING")
            self.assertEqual(frozen["authority_epoch"], 1)
            self.assertEqual(ledger.freeze_authority(1)["mode"], "DRAINING")
            with self.assertRaises(EpochMismatch):
                ledger.freeze_authority(2)
            with self.assertRaises(InvalidTransition):
                self.register(ledger, 2)
            with self.assertRaises(SubmissionBlocked):
                ledger.begin_next_submission(pending[0])

            self.assertEqual(ledger.rebind_authority(1, AUTHORITY_B), 2)
            snapshot = ledger.snapshot()
            assert snapshot is not None
            self.assertEqual(snapshot["mode"], "ACTIVE")
            self.assertEqual(snapshot["authority_epoch"], 2)
            entry = snapshot["entries"][0]
            self.assertEqual(entry["authority"], AUTHORITY_B)
            self.assertEqual(entry["authority_epoch"], 2)
            self.assertEqual(entry["ready_sequence"], ready_sequence)

            claimed = ledger.begin_next_submission(pending[0])
            assert claimed is not None
            self.assertEqual(claimed["authority"], AUTHORITY_B)
            self.assertEqual(claimed["authority_epoch"], 2)
            with self.assertRaises(EpochMismatch):
                ledger.rebind_authority(1, AUTHORITY_A)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_wait_handoff as handoff  # noqa: E402
from handoff_ledger import HandoffLedger  # noqa: E402
from job_registry import OwnerJobRegistry  # noqa: E402


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
AUTHORITY = {
    "endpoint": "ws://127.0.0.1:43210/rpc",
    "transport": "ws",
    "endpoint_fingerprint": None,
    "authority_strength": "weak",
    "authority_strength_reason": "test network endpoint has no instance nonce",
    "owner_process_identity": None,
    "authority_provenance": "test-listener",
    "weak_authority_accepted": True,
    "initialize": {
        "userAgent": "codex-cli/test",
        "codexHome": "/tmp/codex-test-home",
        "platformFamily": "unix",
        "platformOs": "linux",
    },
    "remote_control": {
        "status": "connected",
        "serverName": "test-owner",
        "installationId": "install-test",
        "environmentId": "environment-test",
    },
}


def bind_fake_target_identities(target: dict[str, object]) -> dict[str, object]:
    pid = int(target["pid"])
    identity = handoff.ProcessIdentity(
        scope="local",
        pid=pid,
        ppid=1,
        state="S",
        source="test-process-start-token",
        start_token=f"stable:local:{pid}",
        command=f"fake-job-{pid}",
    )
    target["process_identities"] = [identity.to_dict()]
    target["identity_binding"] = "schedule-time-incarnations"
    return target


class FakeAppServerClient:
    """Scriptable app-server double that records every native-message attempt."""

    def __init__(
        self,
        outcomes: list[str] | None = None,
        *,
        history_client_id: str | None = None,
        loaded_ids: set[str] | None = None,
        thread_status_type: str | None = "idle",
        active_turn_id: str = "active-turn-123",
        active_turn_ids: list[str] | None = None,
        history_mode: str = "legacy",
    ) -> None:
        self.outcomes = list(outcomes or ["accepted"])
        self.history_client_id = history_client_id
        self.loaded_ids = {OWNER} if loaded_ids is None else set(loaded_ids)
        self.thread_status_type = thread_status_type
        self.active_turn_id = active_turn_id
        self.active_turn_ids = (
            list(active_turn_ids)
            if active_turn_ids is not None
            else [active_turn_id]
        )
        self.history_mode = history_mode
        self.current_outcome: str | None = None
        self.current_turn_id: str | None = None
        self.connections = 0
        self.turn_start_calls: list[tuple[str, str, str]] = []
        self.turn_steer_calls: list[tuple[str, str, str, str]] = []
        self.resume_calls: list[str] = []
        self.read_calls: list[tuple[str, bool]] = []
        self.turns_list_calls: list[tuple[str, str, int | None]] = []
        self.history_read_count = 0
        self.post_submission_history_reads = 0

    def __enter__(self) -> "FakeAppServerClient":
        self.connections += 1
        self.current_outcome = None
        self.post_submission_history_reads = 0
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def authority_descriptor(self) -> dict[str, object]:
        return copy.deepcopy(AUTHORITY)

    def loaded_thread_ids(self) -> set[str]:
        return set(self.loaded_ids)

    def read_thread(
        self,
        thread_id: str,
        include_turns: bool = False,
    ) -> dict[str, object]:
        self.read_calls.append((thread_id, include_turns))
        thread: dict[str, object] = {
            "id": thread_id,
            "historyMode": self.history_mode,
            "status": (
                {"type": self.thread_status_type}
                if self.thread_status_type is not None
                else {}
            ),
        }
        if include_turns:
            if self.history_mode == "paginated":
                raise AssertionError(
                    "paginated history must never use thread/read(includeTurns=true)"
                )
            thread["turns"] = self._history_turns("thread/read")
        return thread

    def list_thread_turns(
        self,
        thread_id: str,
        *,
        items_view: str = "summary",
        limit: int = 100,
        max_pages: int | None = None,
    ) -> list[dict[str, object]]:
        del limit
        self.turns_list_calls.append((thread_id, items_view, max_pages))
        if self.history_mode != "paginated":
            raise AssertionError("legacy history unexpectedly used thread/turns/list")
        return self._history_turns("thread/turns/list")

    def _history_turns(self, method: str) -> list[dict[str, object]]:
        self.history_read_count += 1
        if self.current_outcome is not None:
            self.post_submission_history_reads += 1
        if self.current_outcome == "timeout":
            raise handoff.AppServerError(f"{method} history timed out")
        if self.current_outcome == "disconnect":
            raise handoff.AppServerError(f"connection closed during {method}")
        if self.current_outcome == "history_rpc_error":
            raise handoff.AppServerRpcError(
                method,
                {
                    "code": -32000,
                    "message": "history read failed after turn/start succeeded",
                },
            )
        if self.current_outcome in {
            "context_mismatch_timeout",
            "history_read_error",
        } and self.post_submission_history_reads > 1:
            raise handoff.AppServerError(
                f"history remained unconfirmed before {method} failed"
            )

        submitted_client_id = (
            self.turn_start_calls[-1][2] if self.turn_start_calls else None
        )
        history_client_id = self.history_client_id
        turn_id = "historical-turn"
        items: list[dict[str, object]] = []
        if self.current_outcome == "accepted":
            history_client_id = submitted_client_id
        elif self.current_outcome == "steered_active_turn":
            history_client_id = submitted_client_id
            # turn/start may steer a currently active turn, so the persisted
            # user message need not live under the RPC-returned turn id.
            turn_id = "pre-existing-active-turn"
        elif self.current_outcome == "context_mismatch_timeout":
            # Neither a matching clientId on another item type nor a
            # userMessage with another clientId is positive ACK evidence.
            items.extend(
                [
                    {
                        "type": "agentMessage",
                        "clientId": submitted_client_id,
                    },
                    {
                        "type": "userMessage",
                        "clientId": "wrong-client",
                    },
                ]
            )

        if history_client_id is not None:
            items.append(
                {
                    "type": "userMessage",
                    "clientId": history_client_id,
                }
            )
        turns: list[dict[str, object]] = []
        if self.thread_status_type == "active" and self.current_outcome is None:
            turns.extend(
                {
                    "id": active_turn_id,
                    "status": "inProgress",
                    "items": [],
                }
                for active_turn_id in self.active_turn_ids
            )
        if items or not turns:
            turns.append({"id": turn_id, "status": "completed", "items": items})
        return turns

    def resume_thread(self, thread_id: str) -> dict[str, object]:
        self.resume_calls.append(thread_id)
        return {"id": thread_id, "status": {"type": "idle"}}

    def turn_start(self, thread_id: str, text: str, event_id: str) -> str:
        self.turn_start_calls.append((thread_id, text, event_id))
        if not self.outcomes:
            raise AssertionError("unexpected extra turn/start")
        self.current_outcome = self.outcomes.pop(0)
        self.raise_scripted_rpc_rejection("turn/start")
        self.current_turn_id = f"turn-{len(self.turn_start_calls)}"
        return self.current_turn_id

    def turn_steer(
        self,
        thread_id: str,
        expected_turn_id: str,
        text: str,
        event_id: str,
    ) -> str:
        self.turn_steer_calls.append(
            (thread_id, expected_turn_id, text, event_id)
        )
        if not self.outcomes:
            raise AssertionError("unexpected extra turn/steer")
        self.current_outcome = self.outcomes.pop(0)
        self.raise_scripted_rpc_rejection("turn/steer")
        return expected_turn_id

    def raise_scripted_rpc_rejection(self, method: str) -> None:
        errors: dict[str, dict[str, object]] = {
            "rpc_rejected": {
                "code": -32000,
                "message": "definitive rejection",
            },
            "no_active_turn": {
                "code": -32000,
                "message": "No active turn to steer",
            },
            "expected_turn_mismatch": {
                "code": -32000,
                "message": "Expected active turn id does not match",
            },
            "review_turn": {
                "code": -32000,
                "message": "active turn is not steerable",
                "data": {
                    "codexErrorInfo": {
                        "activeTurnNotSteerable": {"turnKind": "review"}
                    }
                },
            },
            "compact_turn": {
                "code": -32000,
                "message": "active turn is not steerable",
                "data": {
                    "codexErrorInfo": {
                        "activeTurnNotSteerable": {"turnKind": "compact"}
                    }
                },
            },
            "input_too_large": {
                "code": -32000,
                "message": "input rejected",
                "data": {"input_error_code": "input_too_large"},
            },
        }
        payload = errors.get(str(self.current_outcome))
        if payload is not None:
            raise handoff.AppServerRpcError(method, payload)

    def wait_for(self, predicate: object, timeout_seconds: float) -> dict[str, object]:
        del predicate, timeout_seconds
        raise AssertionError(
            "a fresh watcher connection is not subscribed to owner-thread "
            "notifications; delivery ACK must use thread/read(includeTurns=true)"
        )


class FakeExitedWatcher:
    def __init__(self, returncode: int = 7) -> None:
        self.pid = 4343
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode


class NativeMessageDeliveryTests(unittest.TestCase):
    def make_ready_task(
        self,
        root: Path,
        index: int = 1,
        *,
        retry_max_attempts: int = 1,
        authority: dict[str, object] | None = None,
    ) -> tuple[Path, dict[str, object], HandoffLedger]:
        state_dir = root / "state"
        coordination_dir = root / "coordination"
        task_id = f"task-{index}"
        event_id = f"event-{index}"
        token = f"token-{index}"
        client_message_id = f"{handoff.CLIENT_MESSAGE_PREFIX}{event_id}"
        task_file = (state_dir / "tasks" / f"{task_id}.json").resolve()
        authority = copy.deepcopy(authority or AUTHORITY)
        ledger = HandoffLedger(coordination_dir, OWNER)
        generation = ledger.register(
            task_id,
            event_id,
            task_file,
            token,
            authority,
            job_key=f"job-{index}",
        )
        ledger.mark_watching(task_id, token, generation)
        ready_sequence = ledger.mark_ready(task_id, token, generation)
        task: dict[str, object] = {
            "task_id": task_id,
            "task_file": str(task_file),
            "event_id": event_id,
            "client_user_message_id": client_message_id,
            "owner_thread_id": OWNER,
            "session_id": OWNER,
            "owner_ledger_dir": str(coordination_dir.resolve()),
            "reservation_token": token,
            "lock_generation": generation,
            "authority": copy.deepcopy(authority),
            "authority_epoch": 1,
            "resume_protocol": "native-message",
            "allow_weak_authority": True,
            "resume_retry_delay_seconds": 1,
            "resume_retry_max_attempts": retry_max_attempts,
            "delivery_ack_timeout_seconds": 1,
            "ready_sequence": ready_sequence,
            "phase": "native_message_ready",
        }
        handoff.write_json(task_file, task)
        return task_file, task, ledger

    def test_strong_owner_process_reuse_fails_before_app_server_request(self) -> None:
        identity = handoff.capture_local_identity(os.getpid())
        strong_authority = {
            **copy.deepcopy(AUTHORITY),
            "endpoint": "unix:///tmp/test-owner.sock",
            "transport": "unix",
            "endpoint_fingerprint": "unix-inode:1:2",
            "authority_strength": "strong",
            "authority_strength_reason": "test strong authority",
            "owner_process_identity": identity.to_dict(),
            "authority_provenance": "ancestor-listener",
        }
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(
                Path(temp),
                authority=strong_authority,
            )
            dead = mock.Mock(
                status="dead",
                reason="identity_changed",
                detail="PID was reused",
            )
            with (
                mock.patch.object(handoff, "probe_local_identity", return_value=dead),
                mock.patch.object(handoff, "AppServerClient") as app_server,
            ):
                self.assertEqual(
                    handoff.dispatch_native_message(task_file, task, "continue"),
                    4,
                )
            app_server.assert_not_called()
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "BLOCKED",
            )

    def test_strong_unknown_reconcile_dead_owner_never_opens_connection(self) -> None:
        identity = handoff.capture_local_identity(os.getpid())
        strong_authority = {
            **copy.deepcopy(AUTHORITY),
            "endpoint": "unix:///tmp/test-owner.sock",
            "transport": "unix",
            "endpoint_fingerprint": "unix-inode:1:2",
            "authority_strength": "strong",
            "authority_strength_reason": "test strong authority",
            "owner_process_identity": (
                handoff.durable_authority_process_identity(identity)
            ),
            "authority_provenance": "ancestor-listener",
            "weak_authority_accepted": False,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file, task, ledger = self.make_ready_task(
                root,
                authority=strong_authority,
            )
            ledger.begin_next_submission("task-1")
            ledger.finish_submission(
                "task-1",
                "token-1",
                1,
                "unknown",
                detail="connection closed after request",
            )
            task["phase"] = "native_message_unknown"
            handoff.write_json(task_file, task)
            dead = mock.Mock(
                status="dead",
                reason="identity_changed",
                detail="PID was reused",
            )
            args = argparse.Namespace(
                state_dir=str(root / "state"),
                task_id="task-1",
                json=True,
            )
            with (
                mock.patch.object(handoff, "probe_local_identity", return_value=dead),
                mock.patch.object(handoff, "AppServerClient") as app_server,
                mock.patch.object(handoff, "emit") as emit,
            ):
                result = handoff.command_reconcile(args)

            self.assertEqual(result, 4)
            app_server.assert_not_called()
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "UNKNOWN",
            )
            self.assertEqual(emit.call_args.args[0]["status"], "still_unknown")

    def test_rebound_weak_opt_in_comes_from_ledger_not_stale_task_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            task["allow_weak_authority"] = False
            task["authority"]["weak_authority_accepted"] = False
            handoff.write_json(task_file, task)

            ledger.freeze_authority(1)
            rebound = {**copy.deepcopy(AUTHORITY), "weak_authority_accepted": True}
            self.assertEqual(ledger.rebind_authority(1, rebound), 2)

            fake = FakeAppServerClient(["accepted"])
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "continue through rebound weak authority",
                )

            self.assertEqual(result, 0)
            entry = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(entry["state"], "ACCEPTED")
            self.assertIs(entry["authority"]["weak_authority_accepted"], True)

    def test_turn_start_once_and_matching_history_client_id_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(["accepted"])
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                first = handoff.dispatch_native_message(task_file, task, "continue once")
                second = handoff.dispatch_native_message(
                    task_file,
                    handoff.load_json(task_file),
                    "continue once",
                )

            self.assertEqual((first, second), (0, 0))
            self.assertEqual(
                fake.turn_start_calls,
                [(OWNER, "continue once", task["client_user_message_id"])],
            )
            self.assertEqual(ledger.validate("task-1", "token-1", 1)["state"], "ACCEPTED")
            persisted = handoff.load_json(task_file)
            self.assertEqual(persisted["phase"], "native_message_accepted")
            self.assertEqual(
                persisted["delivery_status"],
                "matching_user_message_confirmed_in_history",
            )
            self.assertEqual(fake.read_calls, [(OWNER, False), (OWNER, True)])

    def test_explicit_rpc_rejection_can_defer_then_retry_same_fifo_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(
                Path(temp),
                retry_max_attempts=2,
            )
            fake = FakeAppServerClient(["rpc_rejected", "accepted"])
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff.time, "sleep"),
            ):
                result = handoff.dispatch_native_message(task_file, task, "retry safely")

            self.assertEqual(result, 0)
            self.assertEqual(len(fake.turn_start_calls), 2)
            entry = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(entry["state"], "ACCEPTED")
            self.assertEqual(entry["ready_sequence"], 1)
            self.assertEqual(len(entry["submission_deferrals"]), 1)
            self.assertIn("definitive rejection", entry["submission_deferrals"][0]["detail"])

    def test_restart_preserves_submission_deferral_retry_budget(self) -> None:
        class SimulatedRestart(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(
                Path(temp),
                retry_max_attempts=2,
            )
            first_process = FakeAppServerClient(["rpc_rejected"])
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=first_process),
                mock.patch.object(
                    handoff.time,
                    "sleep",
                    side_effect=SimulatedRestart("watcher restarted during retry delay"),
                ),
                self.assertRaises(SimulatedRestart),
            ):
                handoff.dispatch_native_message(
                    task_file,
                    task,
                    "bounded retry survives restart",
                )

            after_restart = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(after_restart["state"], "READY")
            self.assertEqual(len(after_restart["submission_deferrals"]), 1)
            self.assertEqual(
                len(handoff.load_json(task_file)["delivery_attempts"]),
                1,
            )

            replacement_process = FakeAppServerClient(["rpc_rejected", "accepted"])
            with (
                mock.patch.object(
                    handoff,
                    "AppServerClient",
                    return_value=replacement_process,
                ),
                mock.patch.object(handoff.time, "sleep") as sleep,
            ):
                result = handoff.dispatch_native_message(
                    task_file,
                    handoff.load_json(task_file),
                    "bounded retry survives restart",
                )

            self.assertEqual(result, 4)
            self.assertEqual(len(replacement_process.turn_start_calls), 1)
            self.assertEqual(replacement_process.outcomes, ["accepted"])
            sleep.assert_not_called()
            terminal = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(terminal["state"], "BLOCKED")
            self.assertTrue(terminal["blocked_without_submission"])
            self.assertEqual(len(terminal["submission_deferrals"]), 2)
            self.assertEqual(
                handoff.load_json(task_file)["delivery_status"],
                "explicit_rejection_retries_exhausted",
            )

    def test_history_rpc_error_after_successful_turn_start_is_unknown_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(
                Path(temp),
                retry_max_attempts=3,
            )
            task["resume_retry_delay_seconds"] = 1200
            handoff.write_json(task_file, task)
            fake = FakeAppServerClient(["history_rpc_error"])
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff.time, "sleep") as sleep,
            ):
                first = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "submit exactly once",
                )
                second = handoff.dispatch_native_message(
                    task_file,
                    handoff.load_json(task_file),
                    "submit exactly once",
                )

            self.assertEqual((first, second), (4, 4))
            self.assertEqual(len(fake.turn_start_calls), 1)
            self.assertEqual(fake.turn_steer_calls, [])
            sleep.assert_not_called()
            entry = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(entry["state"], "UNKNOWN")
            self.assertEqual(entry.get("submission_deferrals", []), [])

    def test_state_collision_retries_after_one_second_not_authority_backoff(
        self,
    ) -> None:
        for collision in (
            "no_active_turn",
            "expected_turn_mismatch",
            "review_turn",
            "compact_turn",
        ):
            with (
                self.subTest(collision=collision),
                tempfile.TemporaryDirectory() as temp,
            ):
                task_file, task, ledger = self.make_ready_task(
                    Path(temp),
                    retry_max_attempts=1,
                )
                task["resume_retry_delay_seconds"] = 1200
                handoff.write_json(task_file, task)
                fake = FakeAppServerClient(
                    [collision, "accepted"],
                    thread_status_type="active",
                    active_turn_id="in-progress-turn-789",
                )
                with (
                    mock.patch.object(handoff, "AppServerClient", return_value=fake),
                    mock.patch.object(handoff.time, "sleep") as sleep,
                ):
                    result = handoff.dispatch_native_message(
                        task_file,
                        task,
                        "re-probe the active state",
                    )

                self.assertEqual(result, 0)
                self.assertEqual(len(fake.turn_steer_calls), 2)
                self.assertEqual(fake.turn_start_calls, [])
                sleep.assert_called_once_with(handoff.STATE_COLLISION_RETRY_SECONDS)
                self.assertNotIn(mock.call(1200), sleep.call_args_list)
                entry = ledger.validate("task-1", "token-1", 1)
                self.assertEqual(entry["state"], "ACCEPTED")
                self.assertEqual(len(entry["submission_deferrals"]), 1)

    def test_input_too_large_is_immediately_blocked_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(
                Path(temp),
                retry_max_attempts=0,
            )
            task["resume_retry_delay_seconds"] = 1200
            handoff.write_json(task_file, task)
            fake = FakeAppServerClient(
                ["input_too_large"],
                thread_status_type="active",
            )
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff.time, "sleep") as sleep,
            ):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "oversized input",
                )

            self.assertEqual(result, 4)
            self.assertEqual(len(fake.turn_steer_calls), 1)
            self.assertEqual(fake.turn_start_calls, [])
            sleep.assert_not_called()
            entry = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(entry["state"], "BLOCKED")
            self.assertTrue(entry["blocked_without_submission"])
            persisted = handoff.load_json(task_file)
            self.assertEqual(persisted["phase"], "native_message_blocked")
            self.assertEqual(persisted["delivery_status"], "permanent_rpc_rejection")

    def test_atomic_block_commit_survives_crash_without_submitting_recovery(self) -> None:
        class SimulatedHardCrash(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file, task, ledger = self.make_ready_task(
                root,
                retry_max_attempts=1,
            )
            registry = OwnerJobRegistry(str(task["owner_ledger_dir"]), OWNER)
            job_generation = registry.reserve(
                "job-1",
                "task-1",
                "event-1",
                task_file,
                "token-1",
                "native-message",
            )
            task.update(
                {
                    "job_key": "job-1",
                    "job_registry_dir": str(task["owner_ledger_dir"]),
                    "job_reservation_generation": job_generation,
                }
            )
            handoff.write_json(task_file, task)
            original_block = ledger.block_next_ready

            def crash_after_block(*args: object, **kwargs: object) -> dict[str, object]:
                original_block(*args, **kwargs)
                raise SimulatedHardCrash("process died after atomic BLOCKED commit")

            fake = FakeAppServerClient(
                ["input_too_large"],
                thread_status_type="active",
            )
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff, "ledger_for_task", return_value=ledger),
                mock.patch.object(ledger, "block_next_ready", side_effect=crash_after_block),
                self.assertRaises(SimulatedHardCrash),
            ):
                handoff.dispatch_native_message(
                    task_file,
                    task,
                    "commit blocked before crashing",
                )

            blocked = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(blocked["state"], "BLOCKED")
            self.assertTrue(blocked["blocked_without_submission"])
            self.assertEqual(
                registry.validate("task-1", "token-1", job_generation)["state"],
                "ACTIVE",
            )
            self.assertEqual(
                handoff.load_json(task_file)["phase"],
                "native_message_deferred",
            )

            recover_args = argparse.Namespace(
                state_dir=str(root / "state"),
                task_id="task-1",
                json=True,
            )
            with (
                mock.patch.object(handoff, "AppServerClient") as reconnect,
                mock.patch.object(handoff, "emit") as emit,
            ):
                self.assertEqual(handoff.command_recover(recover_args), 4)

            reconnect.assert_not_called()
            self.assertEqual(emit.call_args.args[0]["status"], "blocked")
            self.assertEqual(
                registry.validate("task-1", "token-1", job_generation)["state"],
                "BLOCKED",
            )
            persisted = handoff.load_json(task_file)
            self.assertEqual(persisted["phase"], "native_message_blocked")
            self.assertEqual(
                persisted["delivery_status"],
                "terminal_ledger_mirror_recovered_no_replay",
            )
            self.assertEqual(len(fake.turn_steer_calls), 1)

    def test_pre_submission_authority_budget_blocks_ready_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(
                Path(temp),
                retry_max_attempts=1,
            )
            with mock.patch.object(
                handoff,
                "AppServerClient",
                side_effect=OSError("owner endpoint unavailable"),
            ):
                self.assertEqual(
                    handoff.dispatch_native_message(
                        task_file,
                        task,
                        "do not submit without owner authority",
                    ),
                    4,
                )

            entry = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(entry["state"], "BLOCKED")
            self.assertTrue(entry["blocked_without_submission"])
            self.assertEqual(
                handoff.load_json(task_file)["delivery_status"],
                "authority_unavailable_retries_exhausted",
            )

    def test_active_thread_uses_expected_turn_steer_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(
                ["accepted"],
                thread_status_type="active",
                active_turn_id="in-progress-turn-789",
            )
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                first = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "insert in order",
                )
                second = handoff.dispatch_native_message(
                    task_file,
                    handoff.load_json(task_file),
                    "insert in order",
                )

            self.assertEqual((first, second), (0, 0))
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(
                fake.turn_steer_calls,
                [
                    (
                        OWNER,
                        "in-progress-turn-789",
                        "insert in order",
                        str(task["client_user_message_id"]),
                    )
                ],
            )
            self.assertEqual(fake.read_calls, [(OWNER, False), (OWNER, True)])
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "ACCEPTED",
            )

    def test_paginated_active_thread_uses_turns_list_for_exact_steer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(
                ["accepted"],
                history_mode="paginated",
                thread_status_type="active",
                active_turn_id="paginated-active-turn",
            )
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "steer paginated history",
                )

            self.assertEqual(result, 0)
            self.assertEqual(fake.read_calls, [(OWNER, False)])
            self.assertEqual(fake.turns_list_calls, [(OWNER, "notLoaded", 1)])
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(
                fake.turn_steer_calls,
                [
                    (
                        OWNER,
                        "paginated-active-turn",
                        "steer paginated history",
                        str(task["client_user_message_id"]),
                    )
                ],
            )
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "ACCEPTED",
            )

    def test_paginated_idle_turn_start_uses_full_turns_list_for_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(
                ["accepted"],
                history_mode="paginated",
                thread_status_type="idle",
            )
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "start paginated history",
                )

            self.assertEqual(result, 0)
            self.assertEqual(fake.read_calls, [(OWNER, False)])
            self.assertEqual(fake.turns_list_calls, [(OWNER, "full", 1)])
            self.assertEqual(len(fake.turn_start_calls), 1)
            self.assertEqual(fake.turn_steer_calls, [])
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "ACCEPTED",
            )

    def test_active_steer_rpc_rejection_reprobes_and_retries_without_duplication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(
                Path(temp),
                retry_max_attempts=2,
            )
            fake = FakeAppServerClient(
                ["rpc_rejected", "accepted"],
                thread_status_type="active",
                active_turn_id="in-progress-turn-789",
            )
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff.time, "sleep"),
            ):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "retry only after explicit rejection",
                )

            self.assertEqual(result, 0)
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(len(fake.turn_steer_calls), 2)
            self.assertTrue(
                all(
                    call[1] == "in-progress-turn-789"
                    and call[3] == task["client_user_message_id"]
                    for call in fake.turn_steer_calls
                )
            )
            self.assertEqual(
                fake.read_calls,
                [
                    (OWNER, False),
                    (OWNER, True),
                    (OWNER, False),
                    (OWNER, True),
                ],
            )
            entry = ledger.validate("task-1", "token-1", 1)
            self.assertEqual(entry["state"], "ACCEPTED")
            self.assertEqual(entry["ready_sequence"], 1)
            self.assertEqual(len(entry["submission_deferrals"]), 1)
            self.assertIn(
                "definitive rejection",
                entry["submission_deferrals"][0]["detail"],
            )

    def test_ambiguous_active_thread_is_blocked_before_any_submission(self) -> None:
        for active_turn_ids in ([], ["active-a", "active-b"]):
            with (
                self.subTest(active_turn_ids=active_turn_ids),
                tempfile.TemporaryDirectory() as temp,
            ):
                task_file, task, ledger = self.make_ready_task(Path(temp))
                fake = FakeAppServerClient(
                    thread_status_type="active",
                    active_turn_ids=active_turn_ids,
                )
                with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                    result = handoff.dispatch_native_message(
                        task_file,
                        task,
                        "never guess the active turn",
                    )

                self.assertEqual(result, 4)
                self.assertEqual(fake.turn_start_calls, [])
                self.assertEqual(fake.turn_steer_calls, [])
                self.assertEqual(
                    ledger.validate("task-1", "token-1", 1)["state"],
                    "BLOCKED",
                )

    def test_paginated_active_page_without_exact_turn_fails_closed(self) -> None:
        for active_turn_ids in ([], ["active-a", "active-b"]):
            with (
                self.subTest(active_turn_ids=active_turn_ids),
                tempfile.TemporaryDirectory() as temp,
            ):
                task_file, task, ledger = self.make_ready_task(Path(temp))
                fake = FakeAppServerClient(
                    history_mode="paginated",
                    thread_status_type="active",
                    active_turn_ids=active_turn_ids,
                )
                with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                    result = handoff.dispatch_native_message(
                        task_file,
                        task,
                        "never guess a paginated active turn",
                    )

                self.assertEqual(result, 4)
                self.assertEqual(fake.read_calls, [(OWNER, False)])
                self.assertEqual(fake.turns_list_calls, [(OWNER, "notLoaded", 1)])
                self.assertEqual(fake.turn_start_calls, [])
                self.assertEqual(fake.turn_steer_calls, [])
                self.assertEqual(
                    ledger.validate("task-1", "token-1", 1)["state"],
                    "BLOCKED",
                )

    def test_timeout_after_request_is_unknown_and_replay_never_resends(self) -> None:
        for outcome in ("timeout", "disconnect"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                task_file, task, ledger = self.make_ready_task(Path(temp))
                fake = FakeAppServerClient([outcome])
                with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                    first = handoff.dispatch_native_message(task_file, task, "submit once")
                    second = handoff.dispatch_native_message(
                        task_file,
                        handoff.load_json(task_file),
                        "submit once",
                    )

                self.assertEqual((first, second), (4, 4))
                self.assertEqual(len(fake.turn_start_calls), 1)
                self.assertEqual(
                    ledger.validate("task-1", "token-1", 1)["state"],
                    "UNKNOWN",
                )
                self.assertEqual(
                    handoff.load_json(task_file)["delivery_status"],
                    "blocked_by_earlier_unknown_event",
                )

    def test_history_ack_requires_user_message_with_matching_client_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(["context_mismatch_timeout"])
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff.time, "sleep"),
            ):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "context-bound acknowledgement",
                )

            self.assertEqual(result, 4)
            self.assertEqual(len(fake.turn_start_calls), 1)
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "UNKNOWN",
            )
            self.assertGreaterEqual(fake.history_read_count, 2)

    def test_history_ack_allows_message_under_preexisting_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(["steered_active_turn"])
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "steer the active turn",
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "ACCEPTED",
            )

    def test_history_read_failure_is_unknown_and_never_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(["history_read_error"])
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                first = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "do not replay an unconfirmed turn/start",
                )
                second = handoff.dispatch_native_message(
                    task_file,
                    handoff.load_json(task_file),
                    "do not replay an unconfirmed turn/start",
                )

            self.assertEqual((first, second), (4, 4))
            self.assertEqual(len(fake.turn_start_calls), 1)
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "UNKNOWN",
            )

    def test_dispatch_accepts_exact_positive_idle_status_without_list_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(["accepted"], loaded_ids=set())
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                result = handoff.dispatch_native_message(task_file, task, "already loaded")

            self.assertEqual(result, 0)
            self.assertEqual(len(fake.turn_start_calls), 1)
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "ACCEPTED",
            )

    def test_dispatch_rejects_unknown_status_without_list_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_file, task, ledger = self.make_ready_task(Path(temp))
            fake = FakeAppServerClient(
                ["accepted"],
                loaded_ids=set(),
                thread_status_type="futureStatus",
            )
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                result = handoff.dispatch_native_message(
                    task_file,
                    task,
                    "must not cold-load",
                )

            self.assertEqual(result, 4)
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "BLOCKED",
            )

    def test_fifo_unknown_blocks_later_task_before_opening_a_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_file, first_task, first_ledger = self.make_ready_task(root, 1)
            second_file, second_task, second_ledger = self.make_ready_task(root, 2)
            self.assertEqual(first_ledger.json_path, second_ledger.json_path)
            fake = FakeAppServerClient(["timeout"])
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                self.assertEqual(
                    handoff.dispatch_native_message(first_file, first_task, "first"),
                    4,
                )
                connection_count = fake.connections
                self.assertEqual(
                    handoff.dispatch_native_message(second_file, second_task, "second"),
                    4,
                )

            self.assertEqual(fake.connections, connection_count)
            self.assertEqual(len(fake.turn_start_calls), 1)
            self.assertEqual(
                second_ledger.validate("task-2", "token-2", 2)["state"],
                "READY",
            )
            self.assertEqual(
                handoff.load_json(second_file)["delivery_status"],
                "blocked_by_earlier_unknown_event",
            )

    def test_later_dispatcher_adopts_submitting_entry_without_unbound_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_file, first_task, ledger = self.make_ready_task(root, 1)
            second_file, second_task, second_ledger = self.make_ready_task(root, 2)
            self.assertEqual(ledger.json_path, second_ledger.json_path)
            ledger.begin_next_submission("task-1")
            first_task["phase"] = "native_message_submitting"

            job_registry = OwnerJobRegistry(
                str(first_task["owner_ledger_dir"]),
                OWNER,
            )
            job_generation = job_registry.reserve(
                "job-1",
                "task-1",
                "event-1",
                first_file,
                "token-1",
                "native-message",
            )
            first_task.update(
                {
                    "job_key": "job-1",
                    "job_registry_dir": str(first_task["owner_ledger_dir"]),
                    "job_reservation_generation": job_generation,
                }
            )
            handoff.write_json(first_file, first_task)

            fake = FakeAppServerClient(["accepted"])
            with mock.patch.object(handoff, "AppServerClient", return_value=fake):
                result = handoff.dispatch_native_message(
                    second_file,
                    second_task,
                    "second must remain behind the fenced first event",
                )

            self.assertEqual(result, 4)
            self.assertEqual(fake.connections, 0)
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(fake.turn_steer_calls, [])
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "UNKNOWN",
            )
            self.assertEqual(
                job_registry.validate(
                    "task-1",
                    "token-1",
                    job_generation,
                )["state"],
                "UNKNOWN",
            )
            self.assertEqual(
                handoff.load_json(first_file)["phase"],
                "native_message_unknown",
            )
            self.assertEqual(
                handoff.load_json(second_file)["delivery_status"],
                "blocked_by_earlier_unknown_event",
            )

    def test_positive_history_reconciliation_accepts_without_resending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file, task, ledger = self.make_ready_task(root)
            ledger.begin_next_submission("task-1")
            ledger.finish_submission(
                "task-1",
                "token-1",
                1,
                "unknown",
                detail="connection closed after request",
            )
            task["phase"] = "native_message_unknown"
            task["allow_weak_authority"] = False
            task["authority"]["weak_authority_accepted"] = False
            handoff.write_json(task_file, task)
            fake = FakeAppServerClient(
                [],
                history_client_id=str(task["client_user_message_id"]),
            )
            args = argparse.Namespace(
                state_dir=str(root / "state"),
                task_id="task-1",
                json=True,
            )
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff, "emit") as emit,
            ):
                result = handoff.command_reconcile(args)

            self.assertEqual(result, 0)
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(ledger.validate("task-1", "token-1", 1)["state"], "ACCEPTED")
            self.assertEqual(handoff.load_json(task_file)["phase"], "native_message_accepted")
            self.assertEqual(emit.call_args.args[0]["status"], "reconciled_accepted")

    def test_paginated_history_reconciliation_uses_full_turns_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file, task, ledger = self.make_ready_task(root)
            ledger.begin_next_submission("task-1")
            ledger.finish_submission(
                "task-1",
                "token-1",
                1,
                "unknown",
                detail="connection closed after request",
            )
            task["phase"] = "native_message_unknown"
            handoff.write_json(task_file, task)
            fake = FakeAppServerClient(
                [],
                history_mode="paginated",
                history_client_id=str(task["client_user_message_id"]),
            )
            args = argparse.Namespace(
                state_dir=str(root / "state"),
                task_id="task-1",
                json=True,
            )
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff, "emit") as emit,
            ):
                result = handoff.command_reconcile(args)

            self.assertEqual(result, 0)
            self.assertEqual(fake.read_calls, [(OWNER, False)])
            self.assertEqual(fake.turns_list_calls, [(OWNER, "full", None)])
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(fake.turn_steer_calls, [])
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "ACCEPTED",
            )
            self.assertEqual(emit.call_args.args[0]["status"], "reconciled_accepted")

    def test_reconcile_does_not_fence_submitting_while_watcher_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file, task, ledger = self.make_ready_task(root)
            ledger.begin_next_submission("task-1")
            task["phase"] = "native_message_submitting"
            handoff.write_json(task_file, task)
            fake = FakeAppServerClient(
                [],
                history_client_id=str(task["client_user_message_id"]),
            )
            args = argparse.Namespace(
                state_dir=str(root / "state"),
                task_id="task-1",
                json=True,
            )

            with handoff.exclusive_watcher_guard(task_file):
                with (
                    mock.patch.object(handoff, "AppServerClient", return_value=fake),
                    mock.patch.object(handoff, "emit"),
                ):
                    try:
                        result = handoff.command_reconcile(args)
                    except SystemExit as error:
                        result = int(error.code or 1)

            self.assertNotEqual(result, 0)
            self.assertEqual(fake.connections, 0)
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "SUBMITTING",
            )
            self.assertEqual(
                handoff.load_json(task_file)["phase"],
                "native_message_submitting",
            )

    def test_reconcile_may_fence_submitting_after_adopting_free_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file, task, ledger = self.make_ready_task(root)
            ledger.begin_next_submission("task-1")
            task["phase"] = "native_message_submitting"
            handoff.write_json(task_file, task)
            fake = FakeAppServerClient(
                [],
                history_client_id=str(task["client_user_message_id"]),
            )
            args = argparse.Namespace(
                state_dir=str(root / "state"),
                task_id="task-1",
                json=True,
            )
            with (
                mock.patch.object(handoff, "AppServerClient", return_value=fake),
                mock.patch.object(handoff, "emit"),
            ):
                result = handoff.command_reconcile(args)

            self.assertEqual(result, 0)
            self.assertEqual(fake.turn_start_calls, [])
            self.assertEqual(
                ledger.validate("task-1", "token-1", 1)["state"],
                "ACCEPTED",
            )

    def test_schedule_auto_selects_native_message_and_registers_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = handoff.build_parser().parse_args(
                [
                    "schedule",
                    "--pid",
                    "999999999",
                    "--expected-seconds",
                    "300",
                    "--blocking",
                    "--preflight-seconds",
                    "0",
                    "--state-dir",
                    str(state_dir),
                    "--resume-protocol",
                    "auto",
                    "--app-server-endpoint",
                    str(AUTHORITY["endpoint"]),
                    "--allow-weak-authority",
                ]
            )
            route = {
                "actor_thread_id": OWNER,
                "owner_thread_id": OWNER,
                "metadata_verified": True,
                "route_verified": True,
                "route": "durable-self",
            }
            probe = {
                "native_message_ready": True,
                "authority": copy.deepcopy(AUTHORITY),
            }
            fake_watcher = mock.Mock(pid=4242)
            with (
                mock.patch.object(handoff, "current_actor_thread_id", return_value=(OWNER, False)),
                mock.patch.object(
                    handoff,
                    "app_server_context_from_args",
                    return_value={
                        "source": "test-listener",
                        "attachable": True,
                        "endpoint": AUTHORITY["endpoint"],
                    },
                ),
                mock.patch.object(handoff, "app_server_auth_env_from_args", return_value=None),
                mock.patch.object(handoff, "resolve_owner_route", return_value=route),
                mock.patch.object(
                    handoff,
                    "bind_target_identities",
                    side_effect=bind_fake_target_identities,
                ),
                mock.patch.object(handoff, "inspect_native_thread", return_value=probe),
                mock.patch.object(handoff, "do_preflight", return_value=("alive", "running")),
                mock.patch.object(handoff, "DEFAULT_COORDINATION_DIR", str(coordination_dir)),
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(fake_watcher, {"phase": "watching"}),
                ),
                mock.patch.object(handoff, "emit") as emit,
            ):
                result = handoff.command_schedule(args)

            self.assertEqual(result, 0)
            task_files = list((state_dir / "tasks").glob("*.json"))
            self.assertEqual(len(task_files), 1)
            task = handoff.load_json(task_files[0])
            self.assertEqual(task["resume_protocol"], "native-message")
            self.assertEqual(task["phase"], "scheduled")
            self.assertTrue(task["will_wake_idle_thread"])
            snapshot = HandoffLedger(coordination_dir, OWNER).snapshot()
            assert snapshot is not None
            self.assertEqual(snapshot["authority"]["endpoint"], AUTHORITY["endpoint"])
            self.assertEqual(snapshot["authority"]["authority_strength"], "weak")
            self.assertIn(
                "no public per-process",
                snapshot["authority"]["authority_strength_reason"],
            )
            self.assertEqual(snapshot["authority_epoch"], 1)
            self.assertEqual(snapshot["entries"][0]["state"], "SCHEDULED")
            self.assertEqual(
                snapshot["entries"][0]["task_file"],
                str(task_files[0].resolve()),
            )
            self.assertEqual(emit.call_args.args[0]["resume_protocol"], "native-message")

    def test_schedule_auto_falls_back_to_marker_when_owner_is_not_attachable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = handoff.build_parser().parse_args(
                [
                    "schedule",
                    "--pid",
                    "999999999",
                    "--expected-seconds",
                    "300",
                    "--blocking",
                    "--preflight-seconds",
                    "0",
                    "--state-dir",
                    str(state_dir),
                    "--resume-protocol",
                    "auto",
                    "--app-server-endpoint",
                    str(AUTHORITY["endpoint"]),
                    "--allow-weak-authority",
                ]
            )
            route = {
                "actor_thread_id": OWNER,
                "owner_thread_id": OWNER,
                "metadata_verified": True,
                "route_verified": True,
                "route": "durable-self",
            }
            probe = {
                "native_message_ready": False,
                "error": "private stdio authority is not attachable",
            }
            with (
                mock.patch.object(handoff, "current_actor_thread_id", return_value=(OWNER, False)),
                mock.patch.object(
                    handoff,
                    "app_server_context_from_args",
                    return_value={
                        "source": "test-listener",
                        "attachable": True,
                        "endpoint": AUTHORITY["endpoint"],
                    },
                ),
                mock.patch.object(handoff, "app_server_auth_env_from_args", return_value=None),
                mock.patch.object(handoff, "resolve_owner_route", return_value=route),
                mock.patch.object(
                    handoff,
                    "bind_target_identities",
                    side_effect=bind_fake_target_identities,
                ),
                mock.patch.object(handoff, "inspect_native_thread", return_value=probe),
                mock.patch.object(handoff, "do_preflight", return_value=("alive", "running")),
                mock.patch.object(handoff, "DEFAULT_COORDINATION_DIR", str(coordination_dir)),
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(mock.Mock(pid=4242), {"phase": "watching"}),
                ),
                mock.patch.object(handoff, "emit") as emit,
            ):
                result = handoff.command_schedule(args)

            self.assertEqual(result, 0)
            task_file = next((state_dir / "tasks").glob("*.json"))
            task = handoff.load_json(task_file)
            self.assertEqual(task["resume_protocol"], "marker")
            self.assertEqual(
                task["protocol_fallback_reason"],
                "private stdio authority is not attachable",
            )
            self.assertFalse(task["will_wake_idle_thread"])
            self.assertIsNone(HandoffLedger(coordination_dir, OWNER).snapshot())
            self.assertEqual(emit.call_args.args[0]["resume_protocol"], "marker")

    def test_schedule_detects_child_startup_failure_without_leaving_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = handoff.build_parser().parse_args(
                [
                    "schedule",
                    "--pid",
                    "999999999",
                    "--expected-seconds",
                    "300",
                    "--blocking",
                    "--preflight-seconds",
                    "0",
                    "--state-dir",
                    str(state_dir),
                    "--resume-protocol",
                    "auto",
                    "--app-server-endpoint",
                    str(AUTHORITY["endpoint"]),
                    "--allow-weak-authority",
                ]
            )
            route = {
                "actor_thread_id": OWNER,
                "owner_thread_id": OWNER,
                "metadata_verified": True,
                "route_verified": True,
                "route": "durable-self",
            }
            probe = {
                "native_message_ready": True,
                "authority": copy.deepcopy(AUTHORITY),
            }
            with (
                mock.patch.object(handoff, "current_actor_thread_id", return_value=(OWNER, False)),
                mock.patch.object(
                    handoff,
                    "app_server_context_from_args",
                    return_value={
                        "source": "test-listener",
                        "attachable": True,
                        "endpoint": AUTHORITY["endpoint"],
                    },
                ),
                mock.patch.object(handoff, "app_server_auth_env_from_args", return_value=None),
                mock.patch.object(handoff, "resolve_owner_route", return_value=route),
                mock.patch.object(
                    handoff,
                    "bind_target_identities",
                    side_effect=bind_fake_target_identities,
                ),
                mock.patch.object(handoff, "inspect_native_thread", return_value=probe),
                mock.patch.object(handoff, "do_preflight", return_value=("alive", "running")),
                mock.patch.object(handoff, "DEFAULT_COORDINATION_DIR", str(coordination_dir)),
                mock.patch.object(
                    handoff.subprocess,
                    "Popen",
                    return_value=FakeExitedWatcher(),
                ),
                mock.patch.object(handoff, "emit"),
            ):
                try:
                    result: int | None = handoff.command_schedule(args)
                except (RuntimeError, SystemExit):
                    result = None

            task_files = list((state_dir / "tasks").glob("*.json"))
            task = handoff.load_json(task_files[0]) if task_files else None
            self.assertFalse(
                result == 0 and task is not None and task.get("phase") == "scheduled",
                "a watcher that already exited cannot be reported as safely scheduled",
            )
            snapshot = HandoffLedger(coordination_dir, OWNER).snapshot()
            if snapshot is not None:
                self.assertNotEqual(snapshot["entries"][0]["state"], "SCHEDULED")


if __name__ == "__main__":
    unittest.main()

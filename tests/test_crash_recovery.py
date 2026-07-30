from __future__ import annotations

import argparse
import copy
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_wait_handoff as handoff  # noqa: E402
from handoff_ledger import HandoffLedger  # noqa: E402
from job_registry import OwnerJobRegistry  # noqa: E402


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
ENDPOINT = "ws://127.0.0.1:43210/rpc"
AUTHORITY = {
    "endpoint": ENDPOINT,
    "transport": "ws",
    "endpoint_fingerprint": None,
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


class InjectedHardCrash(RuntimeError):
    pass


class CrashRecoveryTests(unittest.TestCase):
    @staticmethod
    def recover_args(state_dir: Path, task_id: str) -> argparse.Namespace:
        return argparse.Namespace(
            state_dir=str(state_dir),
            task_id=task_id,
            json=True,
        )

    def make_terminal_task(
        self,
        root: Path,
        protocol: str,
        ledger_state: str,
    ) -> tuple[
        Path,
        Path,
        dict[str, object],
        HandoffLedger,
        OwnerJobRegistry,
    ]:
        state_dir = root / "state"
        coordination_dir = root / "coordination"
        protocol_slug = protocol.replace("-", "_")
        task_id = f"task-{protocol_slug}-{ledger_state.lower()}"
        event_id = f"event-{protocol_slug}-{ledger_state.lower()}"
        token = f"token-{protocol_slug}-{ledger_state.lower()}"
        task_file = (state_dir / "tasks" / f"{task_id}.json").resolve()
        owner_ledger_key = (
            OWNER
            if protocol == "native-message"
            else f"{handoff.MARKER_LEDGER_PREFIX}{OWNER}"
        )
        ledger_authority = (
            AUTHORITY
            if protocol == "native-message"
            else handoff.MARKER_AUTHORITY
        )
        job_key = f"job-{protocol_slug}-{ledger_state.lower()}"

        job_registry = OwnerJobRegistry(coordination_dir, OWNER)
        job_generation = job_registry.reserve(
            job_key,
            task_id,
            event_id,
            task_file,
            token,
            protocol,
        )
        ledger = HandoffLedger(coordination_dir, owner_ledger_key)
        generation = ledger.register(
            task_id,
            event_id,
            task_file,
            token,
            ledger_authority,
            job_key=job_key,
        )
        if ledger_state == "CANCELLED":
            ledger.cancel(task_id, token, generation)
        else:
            ledger.mark_watching(task_id, token, generation)
            ledger.mark_ready(task_id, token, generation)
            ledger.begin_next_submission(task_id)
            ledger.finish_submission(
                task_id,
                token,
                generation,
                ledger_state.lower(),
                detail=f"simulated crash after ledger reached {ledger_state}",
            )

        phase_by_state = {
            "ACCEPTED": (
                "native_message_accepted"
                if protocol == "native-message"
                else "marker_claimed"
            ),
            "BLOCKED": (
                "native_message_blocked"
                if protocol == "native-message"
                else "marker_blocked"
            ),
            "CANCELLED": "cancelled",
            "UNKNOWN": (
                "native_message_unknown"
                if protocol == "native-message"
                else "marker_unknown"
            ),
        }
        task: dict[str, object] = {
            "task_id": task_id,
            "task_file": str(task_file),
            "event_id": event_id,
            "client_user_message_id": f"{handoff.CLIENT_MESSAGE_PREFIX}{event_id}",
            "job_key": job_key,
            "job_registry_dir": str(coordination_dir),
            "job_reservation_generation": job_generation,
            "owner_thread_id": OWNER,
            "session_id": OWNER,
            "phase": phase_by_state[ledger_state],
            "resume_protocol": protocol,
            "owner_ledger_dir": str(coordination_dir),
            "owner_ledger_key": owner_ledger_key,
            "authority": copy.deepcopy(ledger_authority),
            "authority_epoch": 1,
            "reservation_token": token,
            "lock_generation": generation,
        }
        handoff.write_json(task_file, task)
        return state_dir, task_file, task, ledger, job_registry

    def test_terminal_delivery_ledger_repairs_active_job_registry_monotonically(
        self,
    ) -> None:
        expected_results = {
            "ACCEPTED": 0,
            "BLOCKED": 4,
            "CANCELLED": 4,
            "UNKNOWN": 4,
        }
        for protocol in ("native-message", "marker"):
            for ledger_state, expected_result in expected_results.items():
                with (
                    self.subTest(protocol=protocol, ledger_state=ledger_state),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    (
                        state_dir,
                        task_file,
                        task,
                        ledger,
                        job_registry,
                    ) = self.make_terminal_task(
                        Path(temp),
                        protocol,
                        ledger_state,
                    )
                    self.assertEqual(
                        job_registry.validate(
                            str(task["task_id"]),
                            str(task["reservation_token"]),
                            int(task["job_reservation_generation"]),
                        )["state"],
                        "ACTIVE",
                    )
                    args = self.recover_args(state_dir, str(task["task_id"]))
                    with (
                        mock.patch.object(handoff, "dispatch_native_message") as dispatch,
                        mock.patch.object(handoff, "spawn_watcher_with_ack") as spawn,
                        mock.patch.object(handoff, "emit"),
                    ):
                        first = handoff.command_recover(args)
                        second = handoff.command_recover(args)

                    self.assertEqual((first, second), (expected_result, expected_result))
                    dispatch.assert_not_called()
                    spawn.assert_not_called()
                    self.assertEqual(
                        ledger.validate(
                            str(task["task_id"]),
                            str(task["reservation_token"]),
                            int(task["lock_generation"]),
                        )["state"],
                        ledger_state,
                    )
                    self.assertEqual(
                        job_registry.validate(
                            str(task["task_id"]),
                            str(task["reservation_token"]),
                            int(task["job_reservation_generation"]),
                        )["state"],
                        ledger_state,
                    )
                    self.assertEqual(
                        len(job_registry.snapshot()["entries"]),  # type: ignore[index]
                        1,
                    )
                    self.assertTrue(task_file.is_file())

    @staticmethod
    def schedule_args(state_dir: Path) -> argparse.Namespace:
        return handoff.build_parser().parse_args(
            [
                "schedule",
                "--pid",
                "30303",
                "--expected-seconds",
                "300",
                "--blocking",
                "--preflight-seconds",
                "0",
                "--state-dir",
                str(state_dir),
                "--resume-protocol",
                "marker",
                "--app-server-endpoint",
                ENDPOINT,
            ]
        )

    @staticmethod
    def schedule_environment(coordination_dir: Path) -> ExitStack:
        route = {
            "actor_thread_id": OWNER,
            "owner_thread_id": OWNER,
            "metadata_verified": True,
            "route_verified": True,
            "route": "durable-self",
        }
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(
                handoff,
                "current_actor_thread_id",
                return_value=(OWNER, False),
            )
        )
        stack.enter_context(
            mock.patch.object(
                handoff,
                "app_server_context_from_args",
                return_value={
                    "source": "test-listener",
                    "attachable": True,
                    "endpoint": ENDPOINT,
                },
            )
        )
        stack.enter_context(
            mock.patch.object(handoff, "app_server_auth_env_from_args", return_value=None)
        )
        stack.enter_context(
            mock.patch.object(handoff, "resolve_owner_route", return_value=route)
        )
        stack.enter_context(
            mock.patch.object(
                handoff,
                "bind_target_identities",
                side_effect=bind_fake_target_identities,
            )
        )
        stack.enter_context(
            mock.patch.object(handoff, "do_preflight", return_value=("alive", "running"))
        )
        stack.enter_context(
            mock.patch.object(
                handoff,
                "DEFAULT_COORDINATION_DIR",
                str(coordination_dir),
            )
        )
        stack.enter_context(mock.patch.object(handoff, "emit"))
        return stack

    def test_recover_repairs_crash_after_either_reservation_commit(self) -> None:
        original_job_reserve = handoff.OwnerJobRegistry.reserve
        original_ledger_register = handoff.HandoffLedger.register

        def job_reserve_then_crash(
            registry: OwnerJobRegistry,
            *args: object,
            **kwargs: object,
        ) -> int:
            original_job_reserve(registry, *args, **kwargs)
            raise InjectedHardCrash("crash after common job reservation commit")

        def ledger_register_then_crash(
            ledger: HandoffLedger,
            *args: object,
            **kwargs: object,
        ) -> int:
            original_ledger_register(ledger, *args, **kwargs)
            raise InjectedHardCrash("crash after ordered ledger registration commit")

        crash_points = {
            "after_job_reserve": (
                handoff.OwnerJobRegistry,
                "reserve",
                job_reserve_then_crash,
                False,
            ),
            "after_ledger_register": (
                handoff.HandoffLedger,
                "register",
                ledger_register_then_crash,
                True,
            ),
        }
        for crash_name, (
            target_class,
            method_name,
            crash_method,
            ledger_was_committed,
        ) in crash_points.items():
            with (
                self.subTest(crash_name=crash_name),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                state_dir = root / "state"
                coordination_dir = root / "coordination"
                args = self.schedule_args(state_dir)
                with self.schedule_environment(coordination_dir):
                    with (
                        mock.patch.object(
                            target_class,
                            method_name,
                            new=crash_method,
                        ),
                        self.assertRaises(InjectedHardCrash),
                    ):
                        handoff.command_schedule(args)

                    task_file = next((state_dir / "tasks").glob("*.json"))
                    crashed_task = handoff.load_json(task_file)
                    self.assertEqual(crashed_task["phase"], "reserving")
                    self.assertNotIn("lock_generation", crashed_task)
                    if ledger_was_committed:
                        self.assertIsInstance(
                            crashed_task["job_reservation_generation"],
                            int,
                        )
                    else:
                        self.assertNotIn("job_reservation_generation", crashed_task)

                    job_registry = OwnerJobRegistry(coordination_dir, OWNER)
                    job_snapshot = job_registry.snapshot()
                    assert job_snapshot is not None
                    self.assertEqual(len(job_snapshot["entries"]), 1)
                    self.assertEqual(job_snapshot["entries"][0]["state"], "ACTIVE")

                    ledger = HandoffLedger(
                        coordination_dir,
                        str(crashed_task["owner_ledger_key"]),
                    )
                    ledger_snapshot = ledger.snapshot()
                    self.assertEqual(ledger_snapshot is not None, ledger_was_committed)

                    fake_watcher = mock.Mock(pid=6060)

                    def spawn_after_recovery_guard_is_released(
                        recovered_task_file: Path,
                        _log_file: Path,
                    ) -> tuple[object, dict[str, object]]:
                        # The real child must acquire this lifetime lock before
                        # it can report its WATCHING startup ACK. Recovery must
                        # therefore finish reservation repair and release its
                        # own guard before spawning the replacement watcher.
                        with handoff.try_watcher_guard(recovered_task_file) as acquired:
                            self.assertTrue(
                                acquired,
                                "recovery spawned a watcher while retaining its guard",
                            )
                        return fake_watcher, {"phase": "watching"}

                    with mock.patch.object(
                        handoff,
                        "spawn_watcher_with_ack",
                        side_effect=spawn_after_recovery_guard_is_released,
                    ) as spawn:
                        result = handoff.command_recover(
                            self.recover_args(
                                state_dir,
                                str(crashed_task["task_id"]),
                            )
                        )

                self.assertEqual(result, 0)
                repaired_task = handoff.load_json(task_file)
                self.assertEqual(repaired_task["phase"], "scheduled")
                self.assertIsInstance(repaired_task["job_reservation_generation"], int)
                self.assertIsInstance(repaired_task["lock_generation"], int)
                spawn.assert_called_once_with(
                    task_file.resolve(),
                    Path(str(repaired_task["log_file"])),
                )

                repaired_job_snapshot = job_registry.snapshot()
                repaired_ledger_snapshot = ledger.snapshot()
                assert repaired_job_snapshot is not None
                assert repaired_ledger_snapshot is not None
                self.assertEqual(len(repaired_job_snapshot["entries"]), 1)
                self.assertEqual(len(repaired_ledger_snapshot["entries"]), 1)
                self.assertEqual(
                    repaired_job_snapshot["entries"][0]["generation"],
                    repaired_task["job_reservation_generation"],
                )
                self.assertEqual(
                    repaired_ledger_snapshot["entries"][0]["generation"],
                    repaired_task["lock_generation"],
                )

    def test_caught_error_after_owner_registration_retains_common_for_recovery(self) -> None:
        original_register = handoff.HandoffLedger.register

        def register_then_error(
            ledger: HandoffLedger,
            *args: object,
            **kwargs: object,
        ) -> int:
            original_register(ledger, *args, **kwargs)
            raise handoff.LedgerError("injected error after owner-ledger commit")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = self.schedule_args(state_dir)
            with self.schedule_environment(coordination_dir):
                with (
                    mock.patch.object(
                        handoff.HandoffLedger,
                        "register",
                        new=register_then_error,
                    ),
                    self.assertRaises(SystemExit),
                ):
                    handoff.command_schedule(args)

                task_file = next((state_dir / "tasks").glob("*.json"))
                interrupted = handoff.load_json(task_file)
                self.assertEqual(
                    interrupted["phase"],
                    "registration_recovery_required",
                )
                self.assertEqual(
                    interrupted["registration_compensation"],
                    "retained",
                )
                self.assertTrue(interrupted["registration_inspection"]["owner_exact"])
                self.assertIsInstance(interrupted["lock_generation"], int)

                registry = OwnerJobRegistry(coordination_dir, OWNER)
                self.assertEqual(registry.snapshot()["entries"][0]["state"], "ACTIVE")
                ledger = HandoffLedger(
                    coordination_dir,
                    str(interrupted["owner_ledger_key"]),
                )
                self.assertEqual(ledger.snapshot()["entries"][0]["state"], "SCHEDULED")

                with mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(mock.Mock(pid=64_001), {"phase": "watching"}),
                ) as spawn:
                    self.assertEqual(
                        handoff.command_recover(
                            self.recover_args(state_dir, str(interrupted["task_id"]))
                        ),
                        0,
                    )

            spawn.assert_called_once()
            self.assertEqual(handoff.load_json(task_file)["phase"], "scheduled")
            self.assertEqual(registry.snapshot()["entries"][0]["state"], "ACTIVE")

    def test_caught_error_before_owner_registration_compensates_only_after_absence_proof(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            with (
                self.schedule_environment(coordination_dir),
                mock.patch.object(
                    handoff.HandoffLedger,
                    "register",
                    side_effect=handoff.LedgerError("injected pre-commit rejection"),
                ),
                self.assertRaises(SystemExit),
            ):
                handoff.command_schedule(self.schedule_args(state_dir))

            task_file = next((state_dir / "tasks").glob("*.json"))
            blocked = handoff.load_json(task_file)
            self.assertEqual(blocked["phase"], "registration_blocked")
            self.assertEqual(
                blocked["registration_compensation"],
                "common_cancelled_after_proven_owner_absent",
            )
            self.assertFalse(blocked["registration_inspection"]["owner_exact"])
            registry = OwnerJobRegistry(coordination_dir, OWNER)
            self.assertEqual(registry.snapshot()["entries"][0]["state"], "CANCELLED")
            ledger = HandoffLedger(
                coordination_dir,
                str(blocked["owner_ledger_key"]),
            )
            self.assertIsNone(ledger.snapshot())

    def test_uncertain_owner_presence_never_cancels_common_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            with (
                self.schedule_environment(coordination_dir),
                mock.patch.object(
                    handoff.HandoffLedger,
                    "register",
                    side_effect=handoff.LedgerError("injected registration error"),
                ),
                mock.patch.object(
                    handoff.HandoffLedger,
                    "find_exact_registration",
                    side_effect=handoff.LedgerError("injected ledger inspection failure"),
                ),
                self.assertRaises(SystemExit),
            ):
                handoff.command_schedule(self.schedule_args(state_dir))

            task_file = next((state_dir / "tasks").glob("*.json"))
            retained = handoff.load_json(task_file)
            self.assertEqual(retained["phase"], "registration_recovery_required")
            self.assertEqual(
                retained["registration_compensation"],
                "retained_unverified",
            )
            self.assertIn("owner_ledger", retained["registration_inspection"]["errors"])
            registry = OwnerJobRegistry(coordination_dir, OWNER)
            self.assertEqual(registry.snapshot()["entries"][0]["state"], "ACTIVE")

            with (
                self.schedule_environment(coordination_dir),
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(mock.Mock(pid=64_002), {"phase": "watching"}),
                ),
            ):
                self.assertEqual(
                    handoff.command_recover(
                        self.recover_args(state_dir, str(retained["task_id"]))
                    ),
                    0,
                )

            repaired = handoff.load_json(task_file)
            self.assertIsInstance(repaired["lock_generation"], int)
            ledger = HandoffLedger(
                coordination_dir,
                str(repaired["owner_ledger_key"]),
            )
            self.assertEqual(ledger.snapshot()["entries"][0]["state"], "SCHEDULED")

    def test_recovery_never_reactivates_common_cancelled_before_task_mirror(self) -> None:
        original_write_json = handoff.write_json

        def crash_before_compensation_mirror(
            path: Path,
            payload: dict[str, object],
        ) -> None:
            if (
                payload.get("registration_compensation")
                == "common_cancelled_after_proven_owner_absent"
            ):
                raise InjectedHardCrash("crash after common CANCELLED commit")
            original_write_json(path, payload)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            with self.schedule_environment(coordination_dir):
                with (
                    mock.patch.object(
                        handoff.HandoffLedger,
                        "register",
                        side_effect=handoff.LedgerError("injected pre-commit rejection"),
                    ),
                    mock.patch.object(
                        handoff,
                        "write_json",
                        side_effect=crash_before_compensation_mirror,
                    ),
                    self.assertRaises(InjectedHardCrash),
                ):
                    handoff.command_schedule(self.schedule_args(state_dir))

                task_file = next((state_dir / "tasks").glob("*.json"))
                crashed = handoff.load_json(task_file)
                self.assertEqual(crashed["phase"], "reserving")
                registry = OwnerJobRegistry(coordination_dir, OWNER)
                self.assertEqual(registry.snapshot()["entries"][0]["state"], "CANCELLED")
                ledger = HandoffLedger(
                    coordination_dir,
                    str(crashed["owner_ledger_key"]),
                )
                self.assertIsNone(ledger.snapshot())

                with mock.patch.object(handoff, "spawn_watcher_with_ack") as spawn:
                    self.assertEqual(
                        handoff.command_recover(
                            self.recover_args(state_dir, str(crashed["task_id"]))
                        ),
                        4,
                    )

            spawn.assert_not_called()
            recovered = handoff.load_json(task_file)
            self.assertEqual(recovered["phase"], "registration_blocked")
            self.assertEqual(
                recovered["registration_compensation"],
                "already_rejected_owner_absent",
            )
            self.assertIsNone(ledger.snapshot())

    def test_restarted_watcher_preserves_original_max_wait_deadline(self) -> None:
        scheduled_at = 1_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = self.schedule_args(state_dir)
            with (
                self.schedule_environment(coordination_dir),
                mock.patch.object(handoff.time, "time", return_value=scheduled_at),
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(mock.Mock(pid=6001), {"phase": "watching"}),
                ),
            ):
                self.assertEqual(handoff.command_schedule(args), 0)

            task_file = next((state_dir / "tasks").glob("*.json"))
            scheduled = handoff.load_json(task_file)
            original_started = float(scheduled["watch_budget_started_unix"])
            original_deadline = float(scheduled["watch_deadline_unix"])
            self.assertEqual(original_started, scheduled_at)
            self.assertEqual(
                original_deadline - original_started,
                float(scheduled["max_wait_seconds"]),
            )

            recover_args = self.recover_args(state_dir, str(scheduled["task_id"]))
            with (
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(mock.Mock(pid=6002), {"phase": "watching"}),
                ),
                mock.patch.object(handoff, "emit"),
            ):
                self.assertEqual(handoff.command_recover(recover_args), 0)

            recovered = handoff.load_json(task_file)
            self.assertEqual(recovered["watch_budget_started_unix"], original_started)
            self.assertEqual(recovered["watch_deadline_unix"], original_deadline)

            after_deadline = original_deadline + 123
            watch_args = argparse.Namespace(
                task_file=str(task_file),
                startup_fd=None,
            )
            with (
                mock.patch.object(handoff.time, "time", return_value=after_deadline),
                mock.patch.object(
                    handoff,
                    "probe_target",
                    return_value=("alive", "same process incarnation"),
                ) as probe,
                mock.patch.object(
                    handoff,
                    "wait_local_pid_exit_event",
                ) as kernel_wait,
                mock.patch.object(handoff.time, "sleep") as sleep,
            ):
                self.assertEqual(handoff.command_watch(watch_args), 0)

            probe.assert_called_once()
            kernel_wait.assert_not_called()
            sleep.assert_not_called()
            completed = handoff.load_json(task_file)
            self.assertEqual(completed["watch_budget_started_unix"], original_started)
            self.assertEqual(completed["watch_deadline_unix"], original_deadline)
            self.assertEqual(completed["completion_reason"], "max_wait_reached")
            self.assertGreaterEqual(
                completed["wait_elapsed_seconds"],
                completed["max_wait_seconds"],
            )


if __name__ == "__main__":
    unittest.main()

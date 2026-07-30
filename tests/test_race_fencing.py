from __future__ import annotations

import argparse
import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_wait_handoff as handoff  # noqa: E402
from handoff_ledger import HandoffLedger, InvalidTransition, LedgerError  # noqa: E402
from job_registry import OwnerJobRegistry  # noqa: E402


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
TASK_ID = "20260730T010000-fencefeed"
EVENT_ID = "92f13ad9-f3e2-47ad-af83-e7513d498b0e"
AUTHORITY = {
    "endpoint": "ws://127.0.0.1:43210/rpc",
    "transport": "ws",
    "endpoint_fingerprint": None,
}


class RaceFencingTests(unittest.TestCase):
    @staticmethod
    def watcher_identity(pid: int, token: str = "boot-id:watcher-start") -> handoff.ProcessIdentity:
        return handoff.ProcessIdentity(
            scope="local",
            pid=pid,
            ppid=1,
            state="S",
            source="linux-proc-starttime",
            start_token=token,
            command="python codex_wait_handoff.py watch",
        )

    def make_native_task(
        self,
        root: Path,
        phase: str,
        *,
        task_id: str = TASK_ID,
        reservation_token: str = "reservation-token",
        ledger_state: str = "SCHEDULED",
    ) -> tuple[Path, Path, dict[str, object], HandoffLedger]:
        root = root.resolve()
        state_dir = root / "state"
        task_file = state_dir / "tasks" / f"{task_id}.json"
        coordination_dir = root / "coordination"
        ledger = HandoffLedger(coordination_dir, OWNER)
        generation = ledger.register(
            task_id,
            EVENT_ID,
            task_file,
            reservation_token,
            AUTHORITY,
            job_key=f"job-{task_id}",
        )
        if ledger_state == "CANCELLED":
            ledger.cancel(task_id, reservation_token, generation)
        elif ledger_state != "SCHEDULED":
            ledger.mark_watching(task_id, reservation_token, generation)
        if ledger_state in {"READY", "SUBMITTING", "UNKNOWN", "ACCEPTED", "BLOCKED"}:
            ledger.mark_ready(task_id, reservation_token, generation)
        if ledger_state in {"SUBMITTING", "UNKNOWN", "ACCEPTED", "BLOCKED"}:
            ledger.begin_next_submission(task_id)
        if ledger_state in {"UNKNOWN", "ACCEPTED", "BLOCKED"}:
            ledger.finish_submission(
                task_id,
                reservation_token,
                generation,
                ledger_state.lower(),
                detail=f"fixture reached {ledger_state}",
            )

        task: dict[str, object] = {
            "task_id": task_id,
            "task_file": str(task_file),
            "event_id": EVENT_ID,
            "client_user_message_id": f"{handoff.CLIENT_MESSAGE_PREFIX}{EVENT_ID}",
            "owner_thread_id": OWNER,
            "session_id": OWNER,
            "phase": phase,
            "resume_protocol": "native-message",
            "owner_ledger_dir": str(coordination_dir),
            "authority": copy.deepcopy(AUTHORITY),
            "authority_epoch": 1,
            "reservation_token": reservation_token,
            "lock_generation": generation,
        }
        handoff.write_json(task_file, task)
        return state_dir, task_file, task, ledger

    def make_marker_task(
        self,
        root: Path,
        phase: str,
        *,
        task_id: str = TASK_ID,
    ) -> tuple[Path, Path, dict[str, object]]:
        state_dir = root.resolve() / "state"
        task_file = state_dir / "tasks" / f"{task_id}.json"
        prompt_file = state_dir / "prompts" / f"{task_id}.txt"
        handoff.write_text(prompt_file, "final marker continuation")
        task: dict[str, object] = {
            "task_id": task_id,
            "task_file": str(task_file),
            "event_id": EVENT_ID,
            "owner_thread_id": OWNER,
            "session_id": OWNER,
            "phase": phase,
            "resume_protocol": "marker",
            "reservation_token": "marker-token",
            "lock_generation": 1,
            "prompt_file": str(prompt_file),
            "target": {"scope": "local", "mode": "pid", "pid": 999_999_999},
        }
        handoff.write_json(task_file, task)
        return state_dir, task_file, task

    @staticmethod
    def attach_common_reservation(
        task_file: Path,
        task: dict[str, object],
    ) -> OwnerJobRegistry:
        coordination_dir = Path(str(task["owner_ledger_dir"]))
        registry = OwnerJobRegistry(coordination_dir, OWNER)
        job_key = f"job-{task['task_id']}"
        generation = registry.reserve(
            job_key,
            str(task["task_id"]),
            str(task["event_id"]),
            task_file,
            str(task["reservation_token"]),
            str(task["resume_protocol"]),
            OWNER,
        )
        task["job_key"] = job_key
        task["job_registry_dir"] = str(coordination_dir)
        task["job_reservation_generation"] = generation
        handoff.write_json(task_file, task)
        return registry

    def assert_cancel_and_stop_are_fenced(
        self,
        phase: str,
        ledger_state: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir, task_file, task, ledger = self.make_native_task(
                Path(temp),
                phase,
                ledger_state=ledger_state,
            )
            ledger_before = ledger.snapshot()
            cancel_args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=TASK_ID,
                json=True,
            )
            emitted: list[dict[str, object]] = []
            with (
                mock.patch.object(handoff, "process_rows") as process_rows,
                mock.patch.object(handoff, "terminate_persisted_watcher") as terminate,
                mock.patch.object(
                    handoff,
                    "emit",
                    side_effect=lambda payload, _json: emitted.append(payload),
                ),
            ):
                if ledger_state == "SCHEDULED":
                    with self.assertRaises(SystemExit):
                        handoff.command_cancel(cancel_args)
                else:
                    self.assertEqual(handoff.command_cancel(cancel_args), 4)
                stopped = handoff.stop_single_task(task_file, also_stop_target=False)

            if ledger_state != "SCHEDULED":
                self.assertEqual(emitted[-1]["status"], "cancel_blocked")
            self.assertEqual(stopped["status"], "cancel_blocked")
            process_rows.assert_not_called()
            terminate.assert_not_called()
            persisted = handoff.load_json(task_file)
            if ledger_state == "SCHEDULED":
                self.assertEqual(persisted, task)
            else:
                expected_phase = handoff.delivery_phase_for_ledger_state(
                    "native-message",
                    ledger_state,
                )
                self.assertEqual(persisted["phase"], expected_phase)
            self.assertEqual(ledger.snapshot(), ledger_before)

    def test_cancel_cannot_cross_submitting_or_unknown_fence(self) -> None:
        for phase, ledger_state in (
            ("native_message_submitting", "SUBMITTING"),
            ("native_message_submitted", "SUBMITTING"),
            ("native_message_unknown", "UNKNOWN"),
        ):
            with self.subTest(phase=phase, ledger_state=ledger_state):
                self.assert_cancel_and_stop_are_fenced(phase, ledger_state)

    def test_ledger_rejects_cancel_after_submission_claim_or_unknown_outcome(self) -> None:
        for ledger_state in ("SUBMITTING", "UNKNOWN"):
            with self.subTest(ledger_state=ledger_state), tempfile.TemporaryDirectory() as temp:
                _state_dir, _task_file, _task, ledger = self.make_native_task(
                    Path(temp),
                    "native_message_submitting",
                    ledger_state=ledger_state,
                )
                before = ledger.snapshot()
                with self.assertRaises(InvalidTransition):
                    ledger.cancel(TASK_ID, "reservation-token", 1)
                self.assertEqual(ledger.snapshot(), before)

    def test_stale_watching_phase_converges_terminal_owner_ledger_before_signalling(
        self,
    ) -> None:
        expected = {
            "ACCEPTED": ("already_accepted", "native_message_accepted"),
            "BLOCKED": ("already_blocked", "native_message_blocked"),
            "CANCELLED": ("cancelled", "cancelled"),
            "UNKNOWN": ("cancel_blocked", "native_message_unknown"),
        }
        for cancel_path in ("command_cancel", "stop_single_task"):
            for ledger_state, (expected_status, expected_phase) in expected.items():
                with (
                    self.subTest(cancel_path=cancel_path, ledger_state=ledger_state),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    state_dir, task_file, task, ledger = self.make_native_task(
                        Path(temp),
                        "watching",
                        ledger_state=ledger_state,
                    )
                    registry = self.attach_common_reservation(task_file, task)
                    ledger_before = ledger.snapshot()
                    cancel_args = argparse.Namespace(
                        state_dir=str(state_dir),
                        task_id=TASK_ID,
                        json=True,
                    )
                    emitted: list[dict[str, object]] = []
                    with (
                        mock.patch.object(
                            handoff,
                            "terminate_persisted_watcher",
                        ) as terminate,
                        mock.patch.object(
                            handoff,
                            "emit",
                            side_effect=lambda payload, _json: emitted.append(payload),
                        ),
                    ):
                        if cancel_path == "command_cancel":
                            expected_code = 0 if ledger_state == "CANCELLED" else 4
                            self.assertEqual(
                                handoff.command_cancel(cancel_args),
                                expected_code,
                            )
                            result = emitted[-1]
                        else:
                            result = handoff.stop_single_task(
                                task_file,
                                also_stop_target=True,
                            )

                    terminate.assert_not_called()
                    self.assertEqual(result["status"], expected_status)
                    self.assertEqual(handoff.load_json(task_file)["phase"], expected_phase)
                    self.assertEqual(
                        registry.validate(TASK_ID, "reservation-token", 1)["state"],
                        ledger_state,
                    )
                    self.assertEqual(ledger.snapshot(), ledger_before)

    def test_ready_and_submitting_owner_ledger_block_cancel_without_signalling(
        self,
    ) -> None:
        for ledger_state in ("READY", "SUBMITTING"):
            with self.subTest(ledger_state=ledger_state), tempfile.TemporaryDirectory() as temp:
                _state_dir, task_file, task, ledger = self.make_native_task(
                    Path(temp),
                    "watching",
                    ledger_state=ledger_state,
                )
                registry = self.attach_common_reservation(task_file, task)
                ledger_before = ledger.snapshot()
                with mock.patch.object(
                    handoff,
                    "terminate_persisted_watcher",
                ) as terminate:
                    result = handoff.stop_single_task(task_file, also_stop_target=True)

                terminate.assert_not_called()
                self.assertEqual(result["status"], "cancel_blocked")
                self.assertEqual(
                    registry.validate(TASK_ID, "reservation-token", 1)["state"],
                    "ACTIVE",
                )
                self.assertEqual(ledger.snapshot(), ledger_before)
                self.assertEqual(
                    handoff.load_json(task_file)["phase"],
                    handoff.delivery_phase_for_ledger_state(
                        "native-message",
                        ledger_state,
                    ),
                )

    def test_owner_ledger_inspection_failure_blocks_before_watcher_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _state_dir, task_file, task, ledger = self.make_native_task(
                Path(temp),
                "watching",
                ledger_state="WATCHING",
            )
            registry = self.attach_common_reservation(task_file, task)
            task_before = handoff.load_json(task_file)
            ledger_before = ledger.snapshot()
            broken_ledger = mock.Mock()
            broken_ledger.validate.side_effect = LedgerError("ledger read failed")
            with (
                mock.patch.object(
                    handoff,
                    "ledger_for_task",
                    return_value=broken_ledger,
                ),
                mock.patch.object(
                    handoff,
                    "terminate_persisted_watcher",
                ) as terminate,
            ):
                result = handoff.stop_single_task(task_file, also_stop_target=True)

            terminate.assert_not_called()
            self.assertEqual(result["status"], "cancel_blocked")
            self.assertIn("could not be verified", result["warning"])
            self.assertEqual(handoff.load_json(task_file), task_before)
            self.assertEqual(ledger.snapshot(), ledger_before)
            self.assertEqual(
                registry.validate(TASK_ID, "reservation-token", 1)["state"],
                "ACTIVE",
            )

    def test_cancel_losing_watcher_handoff_race_converges_advanced_ledger(self) -> None:
        for terminal_state in ("ACCEPTED", "UNKNOWN"):
            with (
                self.subTest(terminal_state=terminal_state),
                tempfile.TemporaryDirectory() as temp,
            ):
                _state_dir, task_file, task, ledger = self.make_native_task(
                    Path(temp),
                    "watching",
                    ledger_state="WATCHING",
                )
                registry = self.attach_common_reservation(task_file, task)

                def advance_before_cancel(*_args: object) -> None:
                    ledger.mark_ready(TASK_ID, "reservation-token", 1)
                    ledger.begin_next_submission(TASK_ID)
                    ledger.finish_submission(
                        TASK_ID,
                        "reservation-token",
                        1,
                        terminal_state.lower(),
                        detail="dispatcher won cancellation handoff race",
                    )
                    raise InvalidTransition("dispatcher advanced owner ledger")

                with (
                    mock.patch.object(
                        handoff,
                        "terminate_persisted_watcher",
                        return_value={
                            "safe_to_cancel": True,
                            "terminated_pids": [],
                            "still_alive_pids": [],
                            "excluded_pids": [],
                            "identity_results": [],
                        },
                    ),
                    mock.patch.object(ledger, "cancel", side_effect=advance_before_cancel),
                    mock.patch.object(handoff, "ledger_for_task", return_value=ledger),
                    mock.patch.object(handoff, "stop_target") as stop_target,
                ):
                    result = handoff.stop_single_task(task_file, also_stop_target=True)

                stop_target.assert_not_called()
                self.assertEqual(
                    result["status"],
                    "already_accepted" if terminal_state == "ACCEPTED" else "cancel_blocked",
                )
                self.assertEqual(
                    registry.validate(TASK_ID, "reservation-token", 1)["state"],
                    terminal_state,
                )
                self.assertEqual(
                    handoff.load_json(task_file)["phase"],
                    handoff.delivery_phase_for_ledger_state(
                        "native-message",
                        terminal_state,
                    ),
                )

    def test_schedule_to_watcher_handoff_cannot_be_cancelled(self) -> None:
        self.assert_cancel_and_stop_are_fenced("scheduled", "SCHEDULED")

    def test_watcher_identity_is_durable_before_watching_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _state_dir, task_file, _task, ledger = self.make_native_task(
                Path(temp),
                "scheduled",
                ledger_state="SCHEDULED",
            )
            identity = self.watcher_identity(handoff.os.getpid())
            read_descriptor, write_descriptor = handoff.os.pipe()
            args = argparse.Namespace(
                task_file=str(task_file),
                startup_fd=write_descriptor,
            )
            try:
                with (
                    mock.patch.object(
                        handoff,
                        "capture_local_identity",
                        return_value=identity,
                    ) as capture,
                    mock.patch.object(handoff, "command_watch_owned", return_value=0),
                ):
                    self.assertEqual(handoff.command_watch(args), 0)
                acknowledgement = handoff.json.loads(
                    handoff.os.read(read_descriptor, 65536).decode("utf-8")
                )
            finally:
                handoff.os.close(read_descriptor)

            persisted = handoff.load_json(task_file)
            capture.assert_called_once_with(handoff.os.getpid())
            self.assertEqual(persisted["phase"], "watching")
            self.assertEqual(persisted["watcher_pid"], handoff.os.getpid())
            self.assertEqual(persisted["watcher_identity"], identity.to_dict())
            self.assertEqual(acknowledgement["watcher_identity"], identity.to_dict())
            self.assertEqual(acknowledgement["watcher_pid"], handoff.os.getpid())
            self.assertEqual(ledger.snapshot()["entries"][0]["state"], "WATCHING")

    def test_watcher_identity_failure_precedes_watching_and_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _state_dir, task_file, original, ledger = self.make_native_task(
                Path(temp),
                "scheduled",
                ledger_state="SCHEDULED",
            )
            read_descriptor, write_descriptor = handoff.os.pipe()
            args = argparse.Namespace(
                task_file=str(task_file),
                startup_fd=write_descriptor,
            )
            try:
                with (
                    mock.patch.object(
                        handoff,
                        "capture_local_identity",
                        side_effect=handoff.ProcessIdentityError("identity unavailable"),
                    ),
                    redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit):
                        handoff.command_watch(args)
                self.assertEqual(handoff.os.read(read_descriptor, 65536), b"")
            finally:
                handoff.os.close(read_descriptor)

            self.assertEqual(handoff.load_json(task_file), original)
            self.assertEqual(ledger.snapshot()["entries"][0]["state"], "SCHEDULED")

    def test_active_status_uses_only_durable_phase_and_watcher_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tasks_dir = Path(temp).resolve() / "tasks"
            alive = self.watcher_identity(61_001, "boot-id:alive")
            reused = self.watcher_identity(61_002, "boot-id:reused")
            terminal = self.watcher_identity(61_003, "boot-id:terminal")
            for task_id, phase, identity in (
                ("alive-task", "watching", alive),
                ("reused-task", "watching", reused),
                ("terminal-task", "cancelled", terminal),
            ):
                handoff.write_json(
                    tasks_dir / f"{task_id}.json",
                    {
                        "task_id": task_id,
                        "phase": phase,
                        "watcher_pid": identity.pid,
                        "watcher_identity": identity.to_dict(),
                    },
                )

            def probe(identity: handoff.ProcessIdentity) -> object:
                if identity.pid == alive.pid:
                    return mock.Mock(
                        status="alive",
                        reason="matching",
                        detail="same process incarnation",
                    )
                if identity.pid == reused.pid:
                    return mock.Mock(
                        status="dead",
                        reason="pid_reused",
                        detail="pid now names an unrelated process",
                    )
                raise AssertionError("terminal phases must not be probed")

            with (
                mock.patch.object(
                    handoff,
                    "process_rows",
                    side_effect=AssertionError("argv discovery is forbidden"),
                ) as process_rows,
                mock.patch.object(
                    handoff,
                    "probe_local_identity",
                    side_effect=probe,
                ),
            ):
                active, stale = handoff.active_and_stale_task_snapshots(tasks_dir)

            process_rows.assert_not_called()
            self.assertEqual([entry["task_id"] for entry in active], ["alive-task"])
            self.assertEqual(active[0]["related_pids"], [alive.pid])
            self.assertNotIn("resume_pids", active[0])
            self.assertEqual([entry["task_id"] for entry in stale], ["reused-task"])
            self.assertFalse(stale[0]["watcher_alive"])
            self.assertEqual(stale[0]["watcher_identity_status"], "dead")
            self.assertEqual(stale[0]["watcher_identity_reason"], "pid_reused")
            self.assertEqual(stale[0]["related_pids"], [])

    def test_stop_all_active_never_discovers_watchers_from_pid_or_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir, task_file, task, ledger = self.make_native_task(
                Path(temp),
                "watching",
                ledger_state="WATCHING",
            )
            identity = self.watcher_identity(62_001, "boot-id:bulk-stop")
            task["watcher_pid"] = identity.pid
            task["watcher_identity"] = identity.to_dict()
            handoff.write_json(task_file, task)
            args = argparse.Namespace(
                state_dir=str(state_dir),
                all_active=True,
                task_id=None,
                also_stop_target=False,
                json=True,
            )
            emitted: list[dict[str, object]] = []
            stopped_result = {
                "identity": identity.to_dict(),
                "status": "stopped",
                "reason": "absent",
                "signals_sent": ["TERM"],
            }

            with (
                mock.patch.object(
                    handoff,
                    "process_rows",
                    side_effect=AssertionError("argv discovery is forbidden"),
                ) as process_rows,
                mock.patch.object(
                    handoff,
                    "probe_local_identity",
                    return_value=mock.Mock(
                        status="alive",
                        reason="matching",
                        detail="same process incarnation",
                    ),
                ) as probe,
                mock.patch.object(
                    handoff,
                    "terminate_local_identity",
                    return_value=stopped_result,
                ) as terminate,
                mock.patch.object(
                    handoff,
                    "emit",
                    side_effect=lambda payload, _json: emitted.append(payload),
                ),
            ):
                self.assertEqual(handoff.command_stop(args), 0)

            process_rows.assert_not_called()
            probe.assert_called_once_with(identity)
            terminate.assert_called_once_with(identity, grace_seconds=3.0)
            self.assertEqual(emitted[-1]["stopped_count"], 1)
            self.assertEqual(emitted[-1]["stopped_tasks"][0]["status"], "cancelled")
            self.assertEqual(handoff.load_json(task_file)["phase"], "cancelled")
            self.assertEqual(ledger.snapshot()["entries"][0]["state"], "CANCELLED")

    def test_stop_all_active_skips_a_reused_watcher_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir, task_file, task, ledger = self.make_native_task(
                Path(temp),
                "watching",
                ledger_state="WATCHING",
            )
            identity = self.watcher_identity(63_001, "boot-id:original")
            task["watcher_pid"] = identity.pid
            task["watcher_identity"] = identity.to_dict()
            handoff.write_json(task_file, task)
            task_before = copy.deepcopy(task)
            ledger_before = ledger.snapshot()
            args = argparse.Namespace(
                state_dir=str(state_dir),
                all_active=True,
                task_id=None,
                also_stop_target=False,
                json=True,
            )
            emitted: list[dict[str, object]] = []

            with (
                mock.patch.object(
                    handoff,
                    "process_rows",
                    side_effect=AssertionError("argv discovery is forbidden"),
                ) as process_rows,
                mock.patch.object(
                    handoff,
                    "probe_local_identity",
                    return_value=mock.Mock(
                        status="dead",
                        reason="pid_reused",
                        detail="pid now names an unrelated process",
                    ),
                ),
                mock.patch.object(handoff, "terminate_local_identity") as terminate,
                mock.patch.object(
                    handoff,
                    "emit",
                    side_effect=lambda payload, _json: emitted.append(payload),
                ),
            ):
                self.assertEqual(handoff.command_stop(args), 0)

            process_rows.assert_not_called()
            terminate.assert_not_called()
            self.assertEqual(emitted[-1]["stopped_count"], 0)
            self.assertEqual(handoff.load_json(task_file), task_before)
            self.assertEqual(ledger.snapshot(), ledger_before)

    def test_marker_pending_and_claimed_are_not_cancellable(self) -> None:
        for phase in ("marker_pending", "marker_claimed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                state_dir, task_file, task = self.make_marker_task(Path(temp), phase)
                cancel_args = argparse.Namespace(
                    state_dir=str(state_dir),
                    task_id=TASK_ID,
                    json=True,
                )
                with (
                    mock.patch.object(handoff, "process_rows") as process_rows,
                    mock.patch.object(handoff, "stop_target") as stop_target,
                ):
                    with self.assertRaises(SystemExit):
                        handoff.command_cancel(cancel_args)
                    stopped = handoff.stop_single_task(task_file, also_stop_target=True)
                    self.assertEqual(stopped["status"], "cancel_blocked")
                    process_rows.assert_not_called()
                    stop_target.assert_not_called()
                    self.assertEqual(handoff.load_json(task_file), task)

    def test_lost_native_cancel_never_stops_watched_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _state_dir, task_file, _task, _ledger = self.make_native_task(
                Path(temp),
                "watching",
                ledger_state="WATCHING",
            )
            losing_ledger = mock.Mock()
            losing_ledger.cancel.side_effect = InvalidTransition(
                "submission concurrently claimed"
            )
            with (
                mock.patch.object(handoff, "process_rows", return_value=[]),
                mock.patch.object(handoff, "ledger_for_task", return_value=losing_ledger),
                mock.patch.object(handoff, "stop_target") as stop_target,
            ):
                stopped = handoff.stop_single_task(task_file, also_stop_target=True)

            self.assertEqual(stopped["status"], "cancel_blocked")
            stop_target.assert_not_called()

    def test_stop_target_runs_only_after_ledger_cancellation_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _state_dir, task_file, _task, ledger = self.make_native_task(
                Path(temp),
                "watching",
                ledger_state="WATCHING",
            )
            with (
                mock.patch.object(
                    handoff,
                    "terminate_persisted_watcher",
                    return_value={
                        "safe_to_cancel": True,
                        "terminated_pids": [],
                        "still_alive_pids": [],
                        "excluded_pids": [],
                        "identity_results": [],
                    },
                ),
                mock.patch.object(
                    ledger,
                    "cancel",
                    side_effect=InvalidTransition("submission already claimed"),
                ),
                mock.patch.object(handoff, "ledger_for_task", return_value=ledger),
                mock.patch.object(handoff, "stop_target") as stop_target,
            ):
                result = handoff.stop_single_task(task_file, also_stop_target=True)

            self.assertEqual(result["status"], "cancel_blocked")
            stop_target.assert_not_called()

    def test_cancel_paths_never_signal_a_pid_reused_by_an_unrelated_process(self) -> None:
        watcher_pid = 70707
        original = self.watcher_identity(watcher_pid)
        reused_result = {
            "identity": original.to_dict(),
            "status": "original_exited",
            "reason": "pid_reused",
            "signals_sent": [],
        }

        for cancel_path in ("command_cancel", "stop_single_task"):
            with self.subTest(cancel_path=cancel_path), tempfile.TemporaryDirectory() as temp:
                state_dir, task_file, task, ledger = self.make_native_task(
                    Path(temp),
                    "watching",
                    ledger_state="WATCHING",
                )
                task["watcher_pid"] = watcher_pid
                task["watcher_identity"] = original.to_dict()
                handoff.write_json(task_file, task)
                cancel_args = argparse.Namespace(
                    state_dir=str(state_dir),
                    task_id=TASK_ID,
                    json=True,
                )

                with (
                    mock.patch.object(handoff, "process_rows") as process_rows,
                    mock.patch.object(handoff, "capture_local_identity") as capture,
                    mock.patch.object(
                        handoff,
                        "terminate_local_identity",
                        return_value=reused_result,
                    ) as terminate,
                    mock.patch.object(handoff.os, "kill") as raw_kill,
                    mock.patch.object(handoff, "emit"),
                ):
                    if cancel_path == "command_cancel":
                        self.assertEqual(handoff.command_cancel(cancel_args), 0)
                    else:
                        result = handoff.stop_single_task(
                            task_file,
                            also_stop_target=False,
                        )
                        self.assertEqual(result["status"], "cancelled")

                process_rows.assert_not_called()
                capture.assert_not_called()
                terminate.assert_called_once_with(original, grace_seconds=3.0)
                raw_kill.assert_not_called()
                cancelled = handoff.load_json(task_file)
                self.assertEqual(cancelled["phase"], "cancelled")
                self.assertEqual(cancelled["stopped_related_pids"], [])
                self.assertEqual(ledger.snapshot()["entries"][0]["state"], "CANCELLED")

    def test_identity_inspection_failure_blocks_cancel_and_retains_reservation(self) -> None:
        watcher_pid = 80808
        original = self.watcher_identity(watcher_pid)
        failed_result = {
            "identity": original.to_dict(),
            "status": "probe_failed",
            "reason": "identity inspection failed",
            "signals_sent": [],
        }

        for cancel_path in ("command_cancel", "stop_single_task"):
            with self.subTest(cancel_path=cancel_path), tempfile.TemporaryDirectory() as temp:
                state_dir, task_file, task, ledger = self.make_native_task(
                    Path(temp),
                    "watching",
                    ledger_state="WATCHING",
                )
                task["watcher_pid"] = watcher_pid
                task["watcher_identity"] = original.to_dict()
                handoff.write_json(task_file, task)
                task_before = copy.deepcopy(task)
                ledger_before = ledger.snapshot()
                cancel_args = argparse.Namespace(
                    state_dir=str(state_dir),
                    task_id=TASK_ID,
                    json=True,
                )
                emitted: list[dict[str, object]] = []

                with (
                    mock.patch.object(handoff, "process_rows") as process_rows,
                    mock.patch.object(handoff, "capture_local_identity") as capture,
                    mock.patch.object(
                        handoff,
                        "terminate_local_identity",
                        return_value=failed_result,
                    ) as terminate,
                    mock.patch.object(handoff.os, "kill") as raw_kill,
                    mock.patch.object(
                        handoff,
                        "emit",
                        side_effect=lambda payload, _json: emitted.append(payload),
                    ),
                ):
                    if cancel_path == "command_cancel":
                        self.assertEqual(handoff.command_cancel(cancel_args), 4)
                        self.assertEqual(emitted[-1]["status"], "cancel_blocked")
                    else:
                        result = handoff.stop_single_task(
                            task_file,
                            also_stop_target=False,
                        )
                        self.assertEqual(result["status"], "cancel_blocked")
                        self.assertEqual(result["still_alive_pids"], [watcher_pid])

                process_rows.assert_not_called()
                capture.assert_not_called()
                terminate.assert_called_once_with(original, grace_seconds=3.0)
                raw_kill.assert_not_called()
                self.assertEqual(handoff.load_json(task_file), task_before)
                self.assertEqual(ledger.snapshot(), ledger_before)

    def test_missing_persisted_watcher_identity_blocks_without_pid_rediscovery(self) -> None:
        watcher_pid = 81818
        for cancel_path in ("command_cancel", "stop_single_task"):
            with self.subTest(cancel_path=cancel_path), tempfile.TemporaryDirectory() as temp:
                state_dir, task_file, task, ledger = self.make_native_task(
                    Path(temp),
                    "watching",
                    ledger_state="WATCHING",
                )
                task["watcher_pid"] = watcher_pid
                handoff.write_json(task_file, task)
                task_before = copy.deepcopy(task)
                ledger_before = ledger.snapshot()
                cancel_args = argparse.Namespace(
                    state_dir=str(state_dir),
                    task_id=TASK_ID,
                    json=True,
                )
                emitted: list[dict[str, object]] = []

                with (
                    mock.patch.object(handoff, "process_rows") as process_rows,
                    mock.patch.object(handoff, "capture_local_identity") as capture,
                    mock.patch.object(handoff, "terminate_local_identity") as terminate,
                    mock.patch.object(handoff.os, "kill") as raw_kill,
                    mock.patch.object(
                        handoff,
                        "emit",
                        side_effect=lambda payload, _json: emitted.append(payload),
                    ),
                ):
                    if cancel_path == "command_cancel":
                        self.assertEqual(handoff.command_cancel(cancel_args), 4)
                        result = emitted[-1]
                    else:
                        result = handoff.stop_single_task(
                            task_file,
                            also_stop_target=False,
                        )

                self.assertEqual(result["status"], "cancel_blocked")
                self.assertIn("no durable watcher", str(result["identity_error"]))
                process_rows.assert_not_called()
                capture.assert_not_called()
                terminate.assert_not_called()
                raw_kill.assert_not_called()
                self.assertEqual(handoff.load_json(task_file), task_before)
                self.assertEqual(ledger.snapshot(), ledger_before)

    def test_recovery_discards_crashed_watcher_identity_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir, task_file, task, _ledger = self.make_native_task(
                Path(temp),
                "watching",
                ledger_state="WATCHING",
            )
            old_identity = self.watcher_identity(82828, "boot-id:old-watcher")
            task["watcher_pid"] = old_identity.pid
            task["watcher_identity"] = old_identity.to_dict()
            task["log_file"] = str(state_dir / "logs" / f"{TASK_ID}.log")
            handoff.write_json(task_file, task)
            observed_before_spawn: list[dict[str, object]] = []

            def spawn_replacement(
                recovered_task_file: Path,
                _log_file: Path,
            ) -> tuple[object, dict[str, object]]:
                recovered = handoff.load_json(recovered_task_file)
                observed_before_spawn.append(recovered)
                return mock.Mock(pid=83838), {"phase": "watching"}

            args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=TASK_ID,
                json=True,
            )
            with (
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    side_effect=spawn_replacement,
                ),
                mock.patch.object(handoff, "emit"),
            ):
                self.assertEqual(handoff.command_recover(args), 0)

            self.assertEqual(len(observed_before_spawn), 1)
            prepared = observed_before_spawn[0]
            self.assertEqual(prepared["phase"], "scheduled")
            self.assertNotIn("watcher_pid", prepared)
            self.assertNotIn("watcher_identity", prepared)

    def test_watcher_rejects_v3_post_event_and_terminal_replays(self) -> None:
        non_restartable_phases = (
            "reserving",
            "event_staged",
            "native_message_ready",
            "native_message_queued",
            "native_message_submitting",
            "native_message_submitted",
            "native_message_deferred",
            "native_message_unknown",
            "native_message_accepted",
            "native_message_blocked",
            "marker_pending",
            "marker_claimed",
            "resume_dry_run_complete",
            "schedule_failed",
            "cancelled",
        )
        for index, phase in enumerate(non_restartable_phases):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                task_file = Path(temp) / "tasks" / f"task-{index}.json"
                handoff.write_json(
                    task_file,
                    {
                        "task_id": f"task-{index}",
                        "task_file": str(task_file),
                        "phase": phase,
                        "resume_protocol": (
                            "marker" if phase.startswith("marker_") else "native-message"
                        ),
                    },
                )
                args = argparse.Namespace(task_file=str(task_file))
                stderr = io.StringIO()
                with (
                    mock.patch.object(handoff, "command_watch_owned") as watch_owned,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit),
                ):
                    handoff.command_watch(args)
                self.assertIn("non-restartable phase", stderr.getvalue())
                watch_owned.assert_not_called()

    def test_watcher_rejects_stale_v3_token_and_generation(self) -> None:
        for field, stale_value in (
            ("reservation_token", "stale-token"),
            ("lock_generation", 999_999),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                _state_dir, task_file, task, _ledger = self.make_native_task(
                    Path(temp),
                    "scheduled",
                )
                task[field] = stale_value
                handoff.write_json(task_file, task)
                args = argparse.Namespace(task_file=str(task_file))
                stderr = io.StringIO()
                with (
                    mock.patch.object(handoff, "command_watch_owned") as watch_owned,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit),
                ):
                    handoff.command_watch(args)
                self.assertIn("token/generation", stderr.getvalue())
                watch_owned.assert_not_called()

    def test_scheduled_and_watching_v3_task_can_enter_only_one_watcher(self) -> None:
        for phase, ledger_state in (
            ("scheduled", "SCHEDULED"),
            ("watching", "WATCHING"),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                _state_dir, task_file, _task, _ledger = self.make_native_task(
                    Path(temp),
                    phase,
                    ledger_state=ledger_state,
                )
                args = argparse.Namespace(task_file=str(task_file))
                with mock.patch.object(
                    handoff,
                    "command_watch_owned",
                    return_value=23,
                ) as watch_owned:
                    result = handoff.command_watch(args)
                self.assertEqual(result, 23)
                watch_owned.assert_called_once_with(task_file.resolve())

    def test_live_watcher_lock_excludes_a_second_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _state_dir, task_file, _task, _ledger = self.make_native_task(
                Path(temp),
                "watching",
                ledger_state="WATCHING",
            )
            args = argparse.Namespace(task_file=str(task_file))
            stderr = io.StringIO()
            with handoff.exclusive_watcher_guard(task_file):
                with (
                    mock.patch.object(handoff, "command_watch_owned") as watch_owned,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit),
                ):
                    handoff.command_watch(args)
            self.assertIn("Another watcher already owns", stderr.getvalue())
            watch_owned.assert_not_called()

    def test_event_staged_never_exposes_an_initial_placeholder_prompt(self) -> None:
        class InjectedCrash(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temp:
            state_dir, task_file, task, _ledger = self.make_native_task(
                Path(temp),
                "scheduled",
            )
            prompt_file = state_dir / "prompts" / f"{TASK_ID}.txt"
            log_file = state_dir / "logs" / f"{TASK_ID}.log"
            handoff.write_text(prompt_file, "INITIAL PLACEHOLDER PROMPT")
            target = {
                "scope": "local",
                "mode": "pid",
                "pid": 999_999_999,
                "identity_binding": "schedule-time-incarnations",
                "process_identities": [
                    handoff.ProcessIdentity(
                        scope="local",
                        pid=999_999_999,
                        ppid=1,
                        state="S",
                        source="test-process-start-token",
                        start_token="stable:local:999999999",
                        command="fake-placeholder-job",
                    ).to_dict()
                ],
            }
            task.update(
                {
                    "prompt_file": str(prompt_file),
                    "log_file": str(log_file),
                    "target": target,
                    "max_wait_seconds": 60,
                    "poll_seconds": 1,
                    "continuation_prompt_text": "inspect the completed result",
                    "note": "",
                }
            )
            handoff.write_json(task_file, task)

            with (
                mock.patch.object(
                    handoff,
                    "wait_local_pid_exit_event",
                    return_value=("process_exited", "pid exited", "kqueue"),
                ),
                mock.patch.object(
                    handoff,
                    "write_text",
                    side_effect=InjectedCrash("crash while persisting final prompt"),
                ),
                self.assertRaises(InjectedCrash),
            ):
                handoff.command_watch_owned(task_file)

            crashed_task = handoff.load_json(task_file)
            crashed_prompt = handoff.read_text(prompt_file)
            self.assertFalse(
                crashed_task.get("phase") == "event_staged"
                and crashed_prompt == "INITIAL PLACEHOLDER PROMPT",
                "event_staged must imply that the final completion prompt is already durable",
            )

            dispatched_prompts: list[str] = []
            recover_args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=TASK_ID,
                json=True,
            )
            with (
                mock.patch.object(
                    handoff,
                    "dispatch_native_message",
                    side_effect=lambda _file, _task, prompt: dispatched_prompts.append(prompt)
                    or 0,
                ),
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(mock.Mock(pid=8080), {"phase": "watching"}),
                ),
                mock.patch.object(handoff, "emit"),
            ):
                try:
                    handoff.command_recover(recover_args)
                except SystemExit:
                    pass
            self.assertNotIn("INITIAL PLACEHOLDER PROMPT", dispatched_prompts)


if __name__ == "__main__":
    unittest.main()

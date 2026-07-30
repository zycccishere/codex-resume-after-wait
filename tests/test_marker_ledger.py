from __future__ import annotations

import argparse
import itertools
import os
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
from job_registry import InvalidJobTransition  # noqa: E402


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
ENDPOINT = "ws://127.0.0.1:43210/rpc"
AUTHORITY = {
    "endpoint": ENDPOINT,
    "transport": "ws",
    "endpoint_fingerprint": None,
}


def bind_fake_target_identities(target: dict[str, object]) -> dict[str, object]:
    pid = int(target["pid"])
    scope = str(target["scope"])
    host = str(target["host"]) if target.get("host") is not None else None
    identity = handoff.ProcessIdentity(
        scope=scope,
        host=host,
        pid=pid,
        ppid=1,
        state="S",
        source="test-process-start-token",
        start_token=f"stable:{scope}:{host or 'local'}:{pid}",
        command=f"fake-job-{pid}",
    )
    target["process_identities"] = [identity.to_dict()]
    target["identity_binding"] = "schedule-time-incarnations"
    return target


class MarkerLedgerTests(unittest.TestCase):
    @staticmethod
    def schedule_args(
        state_dir: Path,
        pid: int,
        protocol: str = "marker",
        job_id: str | None = None,
    ) -> argparse.Namespace:
        argv = [
            "schedule",
            "--pid",
            str(pid),
            "--expected-seconds",
            "300",
            "--blocking",
            "--preflight-seconds",
            "0",
            "--state-dir",
            str(state_dir),
            "--resume-protocol",
            protocol,
            "--app-server-endpoint",
            ENDPOINT,
        ]
        if protocol == "native-message":
            argv.append("--allow-weak-authority")
        if job_id is not None:
            argv.extend(["--job-id", job_id])
        return handoff.build_parser().parse_args(argv)

    @staticmethod
    def schedule_patches(coordination_dir: Path) -> ExitStack:
        route = {
            "actor_thread_id": OWNER,
            "owner_thread_id": OWNER,
            "metadata_verified": True,
            "route_verified": True,
            "route": "durable-self",
        }
        watcher_pids = itertools.count(5001)

        def spawn_ack(_task_file: Path, _log_file: Path) -> tuple[object, dict[str, object]]:
            watcher = mock.Mock(pid=next(watcher_pids))
            return watcher, {"phase": "watching"}

        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(handoff, "current_actor_thread_id", return_value=(OWNER, False))
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
        stack.enter_context(mock.patch.object(handoff, "resolve_owner_route", return_value=route))
        stack.enter_context(
            mock.patch.object(
                handoff,
                "bind_target_identities",
                side_effect=bind_fake_target_identities,
            )
        )
        stack.enter_context(
            mock.patch.object(
                handoff,
                "inspect_native_thread",
                return_value={
                    "native_message_ready": True,
                    "authority": AUTHORITY,
                },
            )
        )
        stack.enter_context(
            mock.patch.object(handoff, "do_preflight", return_value=("alive", "running"))
        )
        stack.enter_context(
            mock.patch.object(handoff, "DEFAULT_COORDINATION_DIR", str(coordination_dir))
        )
        stack.enter_context(
            mock.patch.object(handoff, "spawn_watcher_with_ack", side_effect=spawn_ack)
        )
        stack.enter_context(mock.patch.object(handoff, "emit"))
        return stack

    def test_same_marker_job_cannot_register_two_active_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = self.schedule_args(state_dir, 10101)

            with self.schedule_patches(coordination_dir):
                self.assertEqual(handoff.command_schedule(args), 0)
                with self.assertRaises(SystemExit):
                    handoff.command_schedule(args)

            active_job_tasks = [
                handoff.load_json(path)
                for path in (state_dir / "tasks").glob("*.json")
                if handoff.load_json(path).get("phase")
                not in {"cancelled", "schedule_failed", "registration_blocked"}
            ]
            self.assertEqual(len(active_job_tasks), 1)

    def test_marker_claim_requires_current_codex_thread_identity(self) -> None:
        args = argparse.Namespace(
            state_dir="/does/not/matter",
            task_id="missing-task",
            owner_thread_id=OWNER,
            session_id=None,
            json=True,
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}),
            self.assertRaises(SystemExit),
        ):
            handoff.command_claim(args)

    def test_marker_claim_rejects_explicit_owner_different_from_current_task(self) -> None:
        args = argparse.Namespace(
            state_dir="/does/not/matter",
            task_id="missing-task",
            owner_thread_id="different-owner",
            session_id=None,
            json=True,
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": OWNER}),
            self.assertRaises(SystemExit),
        ):
            handoff.command_claim(args)

    def test_marker_claim_rejects_task_owned_by_another_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            task_file = state_dir / "tasks" / "foreign-task.json"
            task = {
                "task_id": "foreign-task",
                "owner_thread_id": "different-owner",
                "phase": "marker_pending",
            }
            handoff.write_json(task_file, task)
            args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id="foreign-task",
                owner_thread_id=None,
                session_id=None,
                json=True,
            )
            with (
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": OWNER}),
                self.assertRaises(SystemExit),
            ):
                handoff.command_claim(args)

            self.assertEqual(handoff.load_json(task_file), task)

    def test_job_registry_tombstones_accepted_but_allows_rejected_retries(self) -> None:
        target = {
            "scope": "local",
            "mode": "pid",
            "pid": 50505,
        }
        bind_fake_target_identities(target)
        default_key = handoff.job_key_for_target(OWNER, target)
        cases = (
            ("ACTIVE", None, True),
            ("UNKNOWN", "unknown", True),
            ("ACCEPTED", "accepted", True),
            ("CANCELLED", "cancelled", False),
            ("BLOCKED", "blocked", False),
        )

        for state, outcome, rejects_retry in cases:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                registry = handoff.OwnerJobRegistry(Path(temp), OWNER)
                first_generation = registry.reserve(
                    default_key,
                    "task-1",
                    "event-1",
                    "/tmp/task-1.json",
                    "token-1",
                    "marker",
                )
                if outcome is not None:
                    registry.finish(
                        "task-1",
                        "token-1",
                        first_generation,
                        outcome,
                    )

                def reserve_retry() -> int:
                    return registry.reserve(
                        default_key,
                        "task-2",
                        "event-2",
                        "/tmp/task-2.json",
                        "token-2",
                        "marker",
                    )

                if rejects_retry:
                    with self.assertRaises(handoff.JobConflict):
                        reserve_retry()
                    self.assertEqual(len(registry.snapshot()["entries"]), 1)
                else:
                    self.assertEqual(reserve_retry(), 2)
                    self.assertEqual(len(registry.snapshot()["entries"]), 2)

    def test_exact_terminal_job_reservation_is_not_registration_idempotency(self) -> None:
        for outcome in ("unknown", "accepted", "cancelled", "blocked"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                registry = handoff.OwnerJobRegistry(Path(temp), OWNER)
                generation = registry.reserve(
                    "job-1",
                    "task-1",
                    "event-1",
                    "/tmp/task-1.json",
                    "token-1",
                    "marker",
                )
                registry.finish("task-1", "token-1", generation, outcome)
                with self.assertRaisesRegex(
                    InvalidJobTransition,
                    f"state {outcome.upper()}",
                ):
                    registry.reserve(
                        "job-1",
                        "task-1",
                        "event-1",
                        "/tmp/task-1.json",
                        "token-1",
                        "marker",
                    )

    def test_explicit_job_id_allows_an_intentional_new_monitoring_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            with self.schedule_patches(coordination_dir):
                self.assertEqual(
                    handoff.command_schedule(self.schedule_args(state_dir, 60606)),
                    0,
                )
                self.assertEqual(
                    handoff.command_schedule(
                        self.schedule_args(
                            state_dir,
                            60606,
                            job_id="monitoring-cycle-2",
                        )
                    ),
                    0,
                )

            tasks = [
                handoff.load_json(path)
                for path in sorted((state_dir / "tasks").glob("*.json"))
            ]
            self.assertEqual(len(tasks), 2)
            by_logical_id = {task["logical_job_id"]: task for task in tasks}
            self.assertEqual(
                set(by_logical_id),
                {"process-lifetime", "monitoring-cycle-2"},
            )
            self.assertNotEqual(
                by_logical_id["process-lifetime"]["job_key"],
                by_logical_id["monitoring-cycle-2"]["job_key"],
            )
            self.assertEqual(
                by_logical_id["process-lifetime"]["job_registry_file"],
                by_logical_id["monitoring-cycle-2"]["job_registry_file"],
            )
            registry = handoff.load_json(
                Path(str(by_logical_id["process-lifetime"]["job_registry_file"]))
            )
            self.assertEqual(
                [entry["state"] for entry in registry["entries"]],
                ["ACTIVE", "ACTIVE"],
            )

    def test_same_job_cannot_register_once_per_delivery_protocol(self) -> None:
        for first_protocol, second_protocol in (
            ("marker", "native-message"),
            ("native-message", "marker"),
        ):
            with (
                self.subTest(
                    first_protocol=first_protocol,
                    second_protocol=second_protocol,
                ),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                state_dir = root / "state"
                coordination_dir = root / "coordination"
                with self.schedule_patches(coordination_dir):
                    self.assertEqual(
                        handoff.command_schedule(
                            self.schedule_args(state_dir, 30303, first_protocol)
                        ),
                        0,
                    )
                    with self.assertRaises(SystemExit):
                        handoff.command_schedule(
                            self.schedule_args(state_dir, 30303, second_protocol)
                        )

                active_job_tasks = [
                    handoff.load_json(path)
                    for path in (state_dir / "tasks").glob("*.json")
                    if handoff.load_json(path).get("phase")
                    not in {
                        "cancelled",
                        "schedule_failed",
                        "registration_blocked",
                    }
                ]
                self.assertEqual(len(active_job_tasks), 1)
                self.assertEqual(
                    active_job_tasks[0]["resume_protocol"],
                    first_protocol,
                )

    def test_regular_forks_share_one_lineage_job_fence(self) -> None:
        lineage_root = OWNER
        fork_a = "119fb1a5-6269-7f03-8e49-415a5beb9ced"
        fork_b = "219fb1a5-6269-7f03-8e49-415a5beb9ced"
        independent_fork = "319fb1a5-6269-7f03-8e49-415a5beb9ced"
        independent_root = "419fb1a5-6269-7f03-8e49-415a5beb9ced"
        threads = {
            lineage_root: {
                "id": lineage_root,
                "sessionId": "session-lineage-root",
                "parentThreadId": None,
                "forkedFromId": None,
                "ephemeral": False,
            },
            fork_a: {
                "id": fork_a,
                "sessionId": "session-fork-a",
                "parentThreadId": None,
                "forkedFromId": lineage_root,
                "ephemeral": False,
            },
            fork_b: {
                "id": fork_b,
                "sessionId": "session-fork-b",
                "parentThreadId": None,
                "forkedFromId": lineage_root,
                "ephemeral": False,
            },
            independent_root: {
                "id": independent_root,
                "sessionId": "session-independent-root",
                "parentThreadId": None,
                "forkedFromId": None,
                "ephemeral": False,
            },
            independent_fork: {
                "id": independent_fork,
                "sessionId": "session-independent-fork",
                "parentThreadId": None,
                "forkedFromId": independent_root,
                "ephemeral": False,
            },
        }

        class RoutingClient:
            def __enter__(self) -> "RoutingClient":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read_thread(
                self,
                thread_id: str,
                include_turns: bool = False,
            ) -> dict[str, object]:
                del include_turns
                return dict(threads[thread_id])

        with mock.patch.object(handoff, "AppServerClient", return_value=RoutingClient()):
            routes = [
                handoff.resolve_owner_route(ENDPOINT, fork_a),
                handoff.resolve_owner_route(ENDPOINT, fork_b),
                handoff.resolve_owner_route(ENDPOINT, independent_fork),
            ]

        self.assertEqual(routes[0]["owner_session_id"], "session-fork-a")
        self.assertEqual(routes[1]["owner_session_id"], "session-fork-b")
        self.assertEqual(routes[0]["job_scope_id"], lineage_root)
        self.assertEqual(routes[1]["job_scope_id"], lineage_root)
        self.assertEqual(routes[2]["job_scope_id"], independent_root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = self.schedule_args(state_dir, 40404)
            actors = [
                (fork_a, False),
                (fork_b, False),
                (independent_fork, False),
            ]

            with (
                self.schedule_patches(coordination_dir),
                mock.patch.object(
                    handoff,
                    "current_actor_thread_id",
                    side_effect=actors,
                ),
                mock.patch.object(
                    handoff,
                    "resolve_owner_route",
                    side_effect=routes,
                ),
            ):
                self.assertEqual(handoff.command_schedule(args), 0)
                with self.assertRaises(SystemExit):
                    handoff.command_schedule(args)
                self.assertEqual(handoff.command_schedule(args), 0)

            tasks_by_owner: dict[str, tuple[Path, dict[str, object]]] = {}
            for task_file in (state_dir / "tasks").glob("*.json"):
                payload = handoff.load_json(task_file)
                tasks_by_owner[str(payload["owner_thread_id"])] = (task_file, payload)

            self.assertEqual(
                set(tasks_by_owner),
                {fork_a, fork_b, independent_fork},
            )
            first_file, first = tasks_by_owner[fork_a]
            _, conflicting = tasks_by_owner[fork_b]
            _, independent = tasks_by_owner[independent_fork]

            self.assertEqual(first["phase"], "scheduled")
            self.assertEqual(first["owner_thread_id"], fork_a)
            self.assertEqual(first["owner_route"]["owner_thread_id"], fork_a)
            self.assertEqual(first["owner_route"]["owner_session_id"], "session-fork-a")
            self.assertEqual(first["job_scope_id"], lineage_root)
            self.assertEqual(conflicting["phase"], "registration_blocked")
            self.assertEqual(
                conflicting["owner_route"]["owner_session_id"],
                "session-fork-b",
            )
            self.assertEqual(conflicting["job_scope_id"], lineage_root)
            self.assertEqual(conflicting["job_key"], first["job_key"])

            shared_registry = handoff.load_json(Path(str(first["job_registry_file"])))
            active_entries = [
                entry
                for entry in shared_registry["entries"]
                if entry["state"] in {"ACTIVE", "UNKNOWN"}
            ]
            self.assertEqual(len(active_entries), 1)
            self.assertEqual(active_entries[0]["task_id"], first["task_id"])
            self.assertEqual(active_entries[0]["task_file"], str(first_file.resolve()))
            self.assertNotEqual(active_entries[0]["task_id"], conflicting["task_id"])

            self.assertEqual(independent["phase"], "scheduled")
            self.assertEqual(independent["job_scope_id"], independent_root)
            self.assertNotEqual(independent["job_key"], first["job_key"])
            self.assertNotEqual(
                independent["job_registry_file"],
                first["job_registry_file"],
            )

    def test_status_session_returns_job_registries_by_task_scope(self) -> None:
        scope_a = "lineage-root-a"
        scope_b = "lineage-root-b"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            tasks_dir = state_dir / "tasks"
            coordination_dir = root / "coordination"
            task_specs = (
                ("scope-task-a", "scope-event-a", "scope-token-a", scope_a),
                ("scope-task-b", "scope-event-b", "scope-token-b", scope_b),
            )
            for task_id_value, event_id, token, scope_id in task_specs:
                task_file = tasks_dir / f"{task_id_value}.json"
                handoff.write_json(
                    task_file,
                    {
                        "task_id": task_id_value,
                        "owner_thread_id": OWNER,
                        "job_scope_id": scope_id,
                        "phase": "watching",
                    },
                )
                handoff.OwnerJobRegistry(coordination_dir, scope_id).reserve(
                    f"job-{scope_id}",
                    task_id_value,
                    event_id,
                    task_file,
                    token,
                    "marker",
                )

            args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=None,
                session_id=OWNER,
                json=True,
            )
            emitted: list[dict[str, object]] = []
            with (
                mock.patch.object(
                    handoff,
                    "DEFAULT_COORDINATION_DIR",
                    str(coordination_dir),
                ),
                mock.patch.object(
                    handoff,
                    "emit",
                    side_effect=lambda payload, _json: emitted.append(payload),
                ),
            ):
                self.assertEqual(handoff.command_status(args), 0)

            report = emitted[-1]
            self.assertEqual(report["owner_thread_id"], OWNER)
            self.assertEqual(report["job_scope_ids"], [scope_a, scope_b])
            self.assertEqual(set(report["job_registries"]), {scope_a, scope_b})
            for task_id_value, _event_id, _token, scope_id in task_specs:
                registry = report["job_registries"][scope_id]
                self.assertEqual(registry["owner_thread_id"], scope_id)
                self.assertEqual(registry["entries"][0]["task_id"], task_id_value)

    def test_marker_output_crash_before_accepted_is_fenced_without_replay(self) -> None:
        class CrashAfterMarkerOutput(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            with self.schedule_patches(coordination_dir):
                self.assertEqual(
                    handoff.command_schedule(self.schedule_args(state_dir, 70707)),
                    0,
                )

            task_file = next((state_dir / "tasks").glob("*.json"))
            with mock.patch.object(
                handoff,
                "wait_local_pid_exit_event",
                return_value=("process_exited", "pid exited", "kqueue"),
            ):
                self.assertEqual(handoff.command_watch_owned(task_file), 0)

            pending = handoff.load_json(task_file)
            ledger = handoff.ledger_for_task(pending)
            claim_args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=str(pending["task_id"]),
                owner_thread_id=None,
                session_id=None,
                json=True,
            )
            claim_output: list[dict[str, object]] = []
            with (
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": OWNER}),
                mock.patch.object(handoff, "ledger_for_task", return_value=ledger),
                mock.patch.object(
                    ledger,
                    "finish_submission",
                    side_effect=CrashAfterMarkerOutput(
                        "crash after stdout flush and before ACCEPTED"
                    ),
                ),
                mock.patch.object(
                    handoff,
                    "emit",
                    side_effect=lambda payload, _json: claim_output.append(payload),
                ),
                self.assertRaises(CrashAfterMarkerOutput),
            ):
                handoff.command_claim(claim_args)

            self.assertEqual(len(claim_output), 1)
            self.assertEqual(claim_output[0]["status"], "claimed")
            self.assertIn("resume_prompt", claim_output[0])
            crashed = handoff.load_json(task_file)
            self.assertEqual(crashed["phase"], "marker_claiming")
            self.assertEqual(
                ledger.validate(
                    str(crashed["task_id"]),
                    str(crashed["reservation_token"]),
                    int(crashed["lock_generation"]),
                )["state"],
                "SUBMITTING",
            )

            recovery_output: list[dict[str, object]] = []
            recover_args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=str(crashed["task_id"]),
                json=True,
            )
            with mock.patch.object(
                handoff,
                "emit",
                side_effect=lambda payload, _json: recovery_output.append(payload),
            ):
                self.assertEqual(handoff.command_recover(recover_args), 4)

            self.assertEqual(recovery_output[-1]["status"], "marker_unknown")
            self.assertTrue(
                all(
                    output.get("status") != "claimed"
                    and "resume_prompt" not in output
                    for output in recovery_output
                )
            )
            recovered = handoff.load_json(task_file)
            self.assertEqual(recovered["phase"], "marker_unknown")
            self.assertEqual(
                ledger.validate(
                    str(recovered["task_id"]),
                    str(recovered["reservation_token"]),
                    int(recovered["lock_generation"]),
                )["state"],
                "UNKNOWN",
            )

    def test_marker_claim_follows_ready_fifo_across_distinct_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"

            with self.schedule_patches(coordination_dir):
                self.assertEqual(
                    handoff.command_schedule(self.schedule_args(state_dir, 10101)),
                    0,
                )
                self.assertEqual(
                    handoff.command_schedule(self.schedule_args(state_dir, 20202)),
                    0,
                )

            tasks_by_pid: dict[int, tuple[Path, dict[str, object]]] = {}
            for task_file in (state_dir / "tasks").glob("*.json"):
                payload = handoff.load_json(task_file)
                tasks_by_pid[int(payload["target"]["pid"])] = (task_file, payload)
            self.assertEqual(set(tasks_by_pid), {10101, 20202})

            first_registered_file, first_registered = tasks_by_pid[10101]
            second_registered_file, second_registered = tasks_by_pid[20202]
            with mock.patch.object(
                handoff,
                "wait_local_pid_exit_event",
                return_value=("process_exited", "pid exited", "kqueue"),
            ):
                # FIFO is completion/READY order, matching the native-message
                # ledger: the second registered job completes first.
                self.assertEqual(handoff.command_watch_owned(second_registered_file), 0)
                self.assertEqual(handoff.command_watch_owned(first_registered_file), 0)

            first_claim_args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=str(first_registered["task_id"]),
                owner_thread_id=None,
                session_id=None,
                json=True,
            )
            second_claim_args = argparse.Namespace(
                state_dir=str(state_dir),
                task_id=str(second_registered["task_id"]),
                owner_thread_id=None,
                session_id=None,
                json=True,
            )
            with (
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": OWNER}),
                mock.patch.object(handoff, "emit"),
            ):
                with self.assertRaises(SystemExit):
                    handoff.command_claim(first_claim_args)
                self.assertEqual(handoff.command_claim(second_claim_args), 0)
                self.assertEqual(handoff.command_claim(first_claim_args), 0)

            self.assertEqual(
                handoff.load_json(second_registered_file)["phase"],
                "marker_claimed",
            )
            self.assertEqual(
                handoff.load_json(first_registered_file)["phase"],
                "marker_claimed",
            )


if __name__ == "__main__":
    unittest.main()

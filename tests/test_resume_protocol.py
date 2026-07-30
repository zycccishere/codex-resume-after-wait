from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_wait_handoff as handoff  # noqa: E402


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
FORK = "119fb1a5-6269-7f03-8e49-415a5beb9ced"
CHILD = "219fb1a5-6269-7f03-8e49-415a5beb9ced"
GRANDCHILD = "319fb1a5-6269-7f03-8e49-415a5beb9ced"
SIDE = "419fb1a5-6269-7f03-8e49-415a5beb9ced"
TASK_ID = "20260730T000000-deadbeef"
EVENT_ID = "7c1116be-741b-4c41-a2bf-b75ef3c33408"


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


class FakeRoutingClient:
    def __init__(self, threads: dict[str, dict[str, object]]) -> None:
        self.threads = threads

    def __enter__(self) -> "FakeRoutingClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read_thread(self, thread_id: str, include_turns: bool = False) -> dict[str, object]:
        del include_turns
        return dict(self.threads[thread_id])


class ResumeProtocolTests(unittest.TestCase):
    @staticmethod
    def routing_threads() -> dict[str, dict[str, object]]:
        return {
            OWNER: {
                "id": OWNER,
                "source": "user",
                "sessionId": OWNER,
                "parentThreadId": None,
                "forkedFromId": None,
                "ephemeral": False,
            },
            CHILD: {
                "id": CHILD,
                "source": "subagent",
                "sessionId": OWNER,
                "parentThreadId": OWNER,
                "forkedFromId": None,
                "ephemeral": False,
            },
            GRANDCHILD: {
                "id": GRANDCHILD,
                "source": "subagent",
                "sessionId": OWNER,
                "parentThreadId": CHILD,
                "forkedFromId": None,
                "ephemeral": False,
            },
            SIDE: {
                "id": SIDE,
                "source": "user",
                "sessionId": SIDE,
                "parentThreadId": None,
                "forkedFromId": OWNER,
                "ephemeral": True,
            },
        }

    def test_nested_subagent_routes_to_durable_agent_tree_root(self) -> None:
        fake = FakeRoutingClient(self.routing_threads())
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            route = handoff.resolve_owner_route(Path("/tmp/app-server.sock"), GRANDCHILD)
        self.assertEqual(route["actor_thread_id"], GRANDCHILD)
        self.assertEqual(route["owner_thread_id"], OWNER)
        self.assertEqual(route["route"], "subagent-to-agent-tree-root")
        self.assertEqual(route["ancestry"], [GRANDCHILD, CHILD, OWNER])

    def test_subagent_self_resume_is_rejected(self) -> None:
        fake = FakeRoutingClient(self.routing_threads())
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(handoff.OwnerRoutingError, "resume-self"):
                handoff.resolve_owner_route(
                    Path("/tmp/app-server.sock"),
                    CHILD,
                    explicit_owner_thread_id=CHILD,
                )

    def test_side_self_resume_is_rejected(self) -> None:
        fake = FakeRoutingClient(self.routing_threads())
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(handoff.OwnerRoutingError, "ephemeral threads"):
                handoff.resolve_owner_route(Path("/tmp/app-server.sock"), SIDE)

    def test_side_cannot_schedule_even_with_an_explicit_parent(self) -> None:
        fake = FakeRoutingClient(self.routing_threads())
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(handoff.OwnerRoutingError, "return to the durable parent"):
                handoff.resolve_owner_route(
                    Path("/tmp/app-server.sock"),
                    SIDE,
                    explicit_owner_thread_id=OWNER,
                )

    def test_real_side_thread_read_shape_without_fork_parent_is_rejected(self) -> None:
        threads = self.routing_threads()
        threads[SIDE]["forkedFromId"] = None
        fake = FakeRoutingClient(threads)
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(handoff.OwnerRoutingError, "ephemeral threads"):
                handoff.resolve_owner_route(Path("/tmp/app-server.sock"), SIDE)

    def test_nested_subagent_routes_to_durable_fork_with_original_lineage_scope(self) -> None:
        threads = self.routing_threads()
        threads[FORK] = {
            "id": FORK,
            "source": "user",
            "sessionId": "fork-session",
            "parentThreadId": None,
            "forkedFromId": OWNER,
            "ephemeral": False,
        }
        threads[CHILD]["parentThreadId"] = FORK
        threads[CHILD]["sessionId"] = "fork-session"
        threads[CHILD]["forkedFromId"] = None
        threads[GRANDCHILD]["parentThreadId"] = CHILD
        threads[GRANDCHILD]["sessionId"] = "fork-session"
        threads[GRANDCHILD]["forkedFromId"] = None
        fake = FakeRoutingClient(threads)
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            route = handoff.resolve_owner_route(Path("/tmp/app-server.sock"), GRANDCHILD)
        self.assertEqual(route["owner_thread_id"], FORK)
        self.assertEqual(route["owner_session_id"], "fork-session")
        self.assertEqual(route["ancestry"], [GRANDCHILD, CHILD, FORK])
        self.assertEqual(route["fork_lineage"], [FORK, OWNER])
        self.assertEqual(route["job_scope_id"], OWNER)
        self.assertTrue(route["route_verified"])

    def test_incomplete_fork_lineage_is_fail_closed(self) -> None:
        missing_root = "519fb1a5-6269-7f03-8e49-415a5beb9ced"
        threads = self.routing_threads()
        threads[FORK] = {
            "id": FORK,
            "source": "user",
            "sessionId": "fork-session",
            "parentThreadId": None,
            "forkedFromId": missing_root,
            "ephemeral": False,
        }

        class IncompleteLineageClient(FakeRoutingClient):
            def read_thread(
                self,
                thread_id: str,
                include_turns: bool = False,
            ) -> dict[str, object]:
                if thread_id == missing_root:
                    raise handoff.AppServerError("fork source is unavailable")
                return super().read_thread(thread_id, include_turns)

        fake = IncompleteLineageClient(threads)
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(
                handoff.OwnerRoutingError,
                "could not be verified",
            ):
                handoff.resolve_owner_route(Path("/tmp/app-server.sock"), FORK)

    def test_cyclic_fork_lineage_is_fail_closed(self) -> None:
        threads = self.routing_threads()
        threads[FORK] = {
            "id": FORK,
            "source": "user",
            "sessionId": "fork-session",
            "parentThreadId": None,
            "forkedFromId": OWNER,
            "ephemeral": False,
        }
        threads[OWNER]["forkedFromId"] = FORK
        fake = FakeRoutingClient(threads)
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(
                handoff.OwnerRoutingError,
                "cycle detected in Codex forkedFromId lineage",
            ):
                handoff.resolve_owner_route(Path("/tmp/app-server.sock"), FORK)

    def test_partial_subagent_ancestry_failure_is_fail_closed(self) -> None:
        class PartialClient(FakeRoutingClient):
            def read_thread(self, thread_id: str, include_turns: bool = False) -> dict[str, object]:
                if thread_id == OWNER:
                    raise handoff.AppServerError("parent unavailable")
                return super().read_thread(thread_id, include_turns)

        fake = PartialClient(self.routing_threads())
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(handoff.OwnerRoutingError, "refusing to fall back"):
                handoff.resolve_owner_route(Path("/tmp/app-server.sock"), CHILD)

    def test_fully_unavailable_metadata_is_explicitly_unverified(self) -> None:
        with mock.patch.object(
            handoff,
            "AppServerClient",
            side_effect=handoff.AppServerError("socket unavailable"),
        ):
            route = handoff.resolve_owner_route(Path("/tmp/app-server.sock"), CHILD)
        self.assertFalse(route["metadata_verified"])
        self.assertFalse(route["route_verified"])
        self.assertEqual(route["owner_thread_id"], CHILD)

    def test_schedule_fails_closed_before_any_owner_reservation_when_route_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            args = handoff.build_parser().parse_args(
                [
                    "schedule",
                    "--pid",
                    "999999999",
                    "--expected-seconds",
                    "300",
                    "--blocking",
                    "--state-dir",
                    str(state_dir),
                ]
            )
            unverified_route = {
                "actor_thread_id": CHILD,
                "owner_thread_id": CHILD,
                "metadata_verified": False,
                "route_verified": False,
                "route": "unverified-self",
            }
            with (
                mock.patch.object(handoff, "current_actor_thread_id", return_value=(CHILD, False)),
                mock.patch.object(handoff, "resolve_owner_route", return_value=unverified_route),
                mock.patch.object(handoff, "inspect_native_thread") as inspect_native_thread,
                self.assertRaises(SystemExit) as raised,
            ):
                handoff.command_schedule(args)

            self.assertEqual(raised.exception.code, 1)
            inspect_native_thread.assert_not_called()
            self.assertFalse(state_dir.exists())

    def test_schedule_marker_cannot_escape_an_unverified_owner_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
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
                    "marker",
                ]
            )
            unverified_route = {
                "actor_thread_id": CHILD,
                "owner_thread_id": CHILD,
                "metadata_verified": False,
                "route_verified": False,
                "route": "unverified-self",
            }
            with (
                mock.patch.object(handoff, "current_actor_thread_id", return_value=(CHILD, False)),
                mock.patch.object(handoff, "resolve_owner_route", return_value=unverified_route),
                mock.patch.object(handoff, "bind_target_identities") as bind_target,
                self.assertRaises(SystemExit) as raised,
            ):
                handoff.command_schedule(args)

            self.assertEqual(raised.exception.code, 1)
            bind_target.assert_not_called()
            self.assertFalse(state_dir.exists())

    def test_removed_unverified_owner_escape_hatch_is_rejected_by_parser(self) -> None:
        with self.assertRaises(SystemExit):
            handoff.build_parser().parse_args(
                [
                    "schedule",
                    "--pid",
                    "999999999",
                    "--expected-seconds",
                    "300",
                    "--blocking",
                    "--allow-unverified-owner",
                ]
            )

    def test_regular_fork_cannot_retarget_original_branch(self) -> None:
        threads = self.routing_threads()
        threads[FORK] = {
            "id": FORK,
            "source": "user",
            "sessionId": FORK,
            "parentThreadId": None,
            "forkedFromId": OWNER,
            "ephemeral": False,
        }
        fake = FakeRoutingClient(threads)
        with mock.patch.object(handoff, "AppServerClient", return_value=fake):
            with self.assertRaisesRegex(handoff.OwnerRoutingError, "schedule only for itself"):
                handoff.resolve_owner_route(
                    Path("/tmp/app-server.sock"),
                    FORK,
                    explicit_owner_thread_id=OWNER,
                )

    def test_pending_queue_never_crosses_fork_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tasks_dir = Path(temp)
            owner_task = {
                "task_id": TASK_ID,
                "owner_thread_id": OWNER,
                "phase": "marker_pending",
            }
            fork_task = {
                "task_id": "fork-task",
                "owner_thread_id": FORK,
                "phase": "marker_pending",
            }
            (tasks_dir / f"{TASK_ID}.json").write_text(json.dumps(owner_task), encoding="utf-8")
            (tasks_dir / "fork-task.json").write_text(json.dumps(fork_task), encoding="utf-8")
            self.assertEqual(
                [task["task_id"] for task in handoff.pending_tasks_for_owner(tasks_dir, OWNER)],
                [TASK_ID],
            )
            self.assertEqual(
                [task["task_id"] for task in handoff.pending_tasks_for_owner(tasks_dir, FORK)],
                ["fork-task"],
            )

    def test_zombie_target_counts_as_exited(self) -> None:
        process = subprocess.Popen(["sleep", "0.05"])
        try:
            identity = handoff.capture_local_identity(process.pid)
            time.sleep(0.15)
            state, detail = handoff.probe_bound_identity(identity)
            self.assertEqual(state, "dead", detail)
        finally:
            process.wait()

    def test_local_pid_uses_kernel_exit_notification_when_available(self) -> None:
        process = subprocess.Popen(["sleep", "0.05"])
        try:
            identity = handoff.capture_local_identity(process.pid)
            result = handoff.wait_local_pid_exit_event(identity, 2)
            if result is None:
                self.skipTest("platform exposes neither pidfd nor kqueue process notifications")
            reason, _detail, mechanism = result
            self.assertEqual(reason, "process_exited")
            self.assertIn(mechanism, {"pidfd", "kqueue"})
        finally:
            process.wait()

    def test_marker_pending_survives_watcher_exit_without_owner_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_file = root / "tasks" / f"{TASK_ID}.json"
            task = {
                "task_id": TASK_ID,
                "task_file": str(task_file),
                "owner_thread_id": OWNER,
                "phase": "marker_pending",
                "resume_protocol": "marker",
                "reservation_token": "marker-token",
                "lock_generation": 1,
                "watcher_pid": 999_999_999,
            }
            handoff.write_json(task_file, task)
            self.assertEqual(handoff.load_json(task_file), task)
            self.assertEqual(
                handoff.pending_tasks_for_owner(task_file.parent, OWNER),
                [task],
            )
            self.assertFalse((root / "coordination").exists())


if __name__ == "__main__":
    unittest.main()

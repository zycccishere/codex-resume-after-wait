from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import process_identity as process  # noqa: E402
import codex_wait_handoff as handoff  # noqa: E402


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
ENDPOINT = "ws://127.0.0.1:43210/rpc"


def completed(
    stdout: str = "",
    *,
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ProcessIdentityTests(unittest.TestCase):
    def identity(
        self,
        *,
        scope: str = "local",
        token: str = "boot:start-1",
        host: str | None = None,
        pid: int = 4321,
        state: str = "S",
    ) -> process.ProcessIdentity:
        return process.ProcessIdentity(
            scope=scope,
            host=host,
            pid=pid,
            ppid=1,
            state=state,
            source="linux-proc-starttime",
            start_token=token,
            command="sleep 30",
        )

    def test_identity_round_trip_and_incarnation_comparison(self) -> None:
        expected = self.identity()
        self.assertEqual(process.ProcessIdentity.from_dict(expected.to_dict()), expected)
        self.assertTrue(expected.same_incarnation(expected))
        self.assertFalse(expected.same_incarnation(self.identity(token="boot:start-2")))

    def test_linux_proc_identity_parses_comm_with_spaces_and_parentheses(self) -> None:
        pid = 8123
        fields = ["S", "77"] + ["0"] * 48
        fields[19] = "998877"
        stat = f"{pid} (worker ) with spaces) " + " ".join(fields)
        with tempfile.TemporaryDirectory() as temp:
            proc_root = Path(temp)
            process_dir = proc_root / str(pid)
            boot_dir = proc_root / "sys" / "kernel" / "random"
            process_dir.mkdir(parents=True)
            boot_dir.mkdir(parents=True)
            (process_dir / "stat").write_text(stat, encoding="utf-8")
            (process_dir / "cmdline").write_bytes(b"python\0train.py\0")
            (boot_dir / "boot_id").write_text("boot-abc\n", encoding="ascii")

            identity = process._capture_linux_proc(pid, proc_root)

        self.assertEqual(identity.ppid, 77)
        self.assertEqual(identity.state, "S")
        self.assertEqual(identity.start_token, "boot-abc:998877")
        self.assertEqual(identity.command, "python train.py")

    def test_ps_identity_uses_full_lstart_as_token(self) -> None:
        identity = process._parse_ps_identity(
            "44 3 Thu Jul 30 16:41:59 2026 S /usr/bin/sleep 30",
            44,
            scope="remote",
            host="builder.example",
        )
        self.assertEqual(identity.start_token, "Thu Jul 30 16:41:59 2026")
        self.assertEqual(identity.command, "/usr/bin/sleep 30")

    def test_real_local_process_can_be_captured_probed_and_stopped(self) -> None:
        child = subprocess.Popen(["sleep", "30"])
        try:
            identity = process.capture_local_identity(child.pid)
            probe = process.probe_local_identity(identity)
            self.assertEqual(probe.status, "alive", probe.detail)

            result = process.terminate_local_identity(
                identity,
                grace_seconds=1.0,
                poll_seconds=0.02,
            )
            self.assertEqual(result["status"], "stopped", result)
            self.assertEqual(result["signals_sent"], ["TERM"])
            child.wait(timeout=2)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_pid_reuse_is_dead_for_original_and_never_signaled(self) -> None:
        expected = self.identity(token="boot:old")
        replacement = self.identity(token="boot:new")
        with (
            mock.patch.object(process, "capture_local_identity", return_value=replacement),
            mock.patch.object(process.os, "kill") as kill,
        ):
            probe = process.probe_local_identity(expected)
            result = process.terminate_local_identity(expected, grace_seconds=0)

        self.assertEqual((probe.status, probe.reason), ("dead", "pid_reused"))
        self.assertEqual(result["status"], "original_exited")
        self.assertEqual(result["reason"], "pid_reused")
        self.assertEqual(result["signals_sent"], [])
        kill.assert_not_called()

    def test_local_pattern_excludes_helper_and_all_ancestors(self) -> None:
        target = self.identity(pid=9000)
        command_results = [
            completed(f"{os.getpid()}\n7000\n9000\n"),
            completed(f"{os.getpid()} 7000\n7000 6000\n6000 1\n1 0\n9000 1\n"),
        ]
        with (
            mock.patch.object(process, "_run_command", side_effect=command_results),
            mock.patch.object(process, "capture_local_identity", return_value=target) as capture,
        ):
            identities = process.find_local_pattern("training-marker")

        self.assertEqual(identities, (target,))
        capture.assert_called_once_with(9000)

    def test_unsafe_ssh_destinations_are_rejected_before_spawn(self) -> None:
        for host in ("-oProxyCommand=bad", "", "host\ncommand", "two words"):
            with self.subTest(host=host), mock.patch.object(process, "_run_command") as run:
                with self.assertRaises(process.UnsafeRemoteHost):
                    process.capture_remote_identity(host, 12)
                run.assert_not_called()

    def test_remote_pattern_is_stdin_only_and_parses_identities(self) -> None:
        marker = "secret-pattern-" + uuid.uuid4().hex
        output = "IDENTITY\t91\t1\tS\tlinux-proc-starttime\tboot:100\nDONE\n"
        with mock.patch.object(process, "_run_command", return_value=completed(output)) as run:
            identities = process.find_remote_pattern("user@remote", marker)

        self.assertEqual([identity.pid for identity in identities], [91])
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[:3], ["ssh", "--", "user@remote"])
        self.assertNotIn(marker, " ".join(command))
        self.assertEqual(kwargs["input_text"], f"PATTERN\n{marker}\n")
        self.assertNotIn(marker, process.REMOTE_HELPER_COMMAND)

    def test_remote_pattern_requires_completed_snapshot(self) -> None:
        output = "IDENTITY\t91\t1\tS\tlinux-proc-starttime\tboot:100\n"
        with mock.patch.object(process, "_run_command", return_value=completed(output)):
            with self.assertRaisesRegex(process.ProcessInspectionError, "did not complete"):
                process.find_remote_pattern("remote", "worker")

    def test_remote_helper_pattern_does_not_match_itself(self) -> None:
        marker = "stdin-only-marker-" + uuid.uuid4().hex
        result = subprocess.run(
            ["sh", "-c", process.REMOTE_HELPER_SCRIPT],
            input=f"PATTERN\n{marker}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "DONE\n")

    def test_remote_helper_excludes_matching_ancestor(self) -> None:
        marker = "ancestor-marker-" + uuid.uuid4().hex
        # Keep the outer shell alive after its child so it cannot optimize the
        # child into itself. marker is $0 and therefore present in the ancestor
        # argv, while the inner helper receives it only over stdin.
        outer = "sh -c " + shlex.quote(process.REMOTE_HELPER_SCRIPT) + "; :"
        result = subprocess.run(
            ["sh", "-c", outer, marker],
            input=f"PATTERN\n{marker}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "DONE\n")

    def test_remote_helper_finds_real_target_without_self_match(self) -> None:
        marker = "target-marker-" + uuid.uuid4().hex
        target = subprocess.Popen(["sh", "-c", "sleep 30 & wait", marker])
        try:
            time.sleep(0.05)
            result = subprocess.run(
                ["sh", "-c", process.REMOTE_HELPER_SCRIPT],
                input=f"PATTERN\n{marker}\n",
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            identity_lines = [
                line for line in result.stdout.splitlines() if line.startswith("IDENTITY\t")
            ]
            self.assertEqual(len(identity_lines), 1, result.stdout)
            self.assertEqual(int(identity_lines[0].split("\t", 2)[1]), target.pid)
        finally:
            target.terminate()
            target.wait(timeout=2)

    def test_remote_helper_rejects_reused_pid_without_signaling(self) -> None:
        capture = subprocess.run(
            ["sh", "-c", process.REMOTE_HELPER_SCRIPT],
            input=f"CAPTURE_PID\n{os.getpid()}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(capture.returncode, 0, capture.stderr)
        fields = capture.stdout.strip().split("\t", 5)
        self.assertEqual(fields[0], "IDENTITY")
        source, token = fields[4], fields[5]
        rejected = subprocess.run(
            ["sh", "-c", process.REMOTE_HELPER_SCRIPT],
            input=(
                f"SIGNAL_PID\n{os.getpid()}\n{source}\n"
                f"{token}-different\nTERM\n"
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        self.assertEqual(rejected.stdout, "REUSED\n")
        os.kill(os.getpid(), 0)

    def test_remote_probe_treats_replacement_as_original_exit(self) -> None:
        expected = self.identity(scope="remote", host="remote", token="boot:old")
        replacement = self.identity(scope="remote", host="remote", token="boot:new")
        with mock.patch.object(process, "capture_remote_identity", return_value=replacement):
            probe = process.probe_remote_identity(expected)
        self.assertEqual((probe.status, probe.reason), ("dead", "pid_reused"))

    def test_remote_kill_revalidates_before_escalation(self) -> None:
        identity = self.identity(scope="remote", host="remote")
        with (
            mock.patch.object(
                process,
                "_remote_signal",
                side_effect=["SIGNALED", "REUSED"],
            ) as remote_signal,
            mock.patch.object(
                process,
                "probe_remote_identity",
                return_value=process.ProbeResult("alive", "matching", "still alive"),
            ),
        ):
            result = process.terminate_remote_identity(identity, grace_seconds=0)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "pid_reused")
        self.assertEqual(result["signals_sent"], ["TERM"])
        self.assertEqual(remote_signal.call_args_list, [mock.call(identity, "TERM"), mock.call(identity, "KILL")])


class ProcessIdentityIntegrationTests(unittest.TestCase):
    @staticmethod
    def identity(token: str, *, pid: int = 30303) -> process.ProcessIdentity:
        return process.ProcessIdentity(
            scope="local",
            pid=pid,
            ppid=1,
            state="S",
            source="test-process-start-token",
            start_token=token,
            command=f"fake-job-{pid}",
        )

    @staticmethod
    def marker_schedule_args(state_dir: Path, *, pid: int = 30303) -> object:
        return handoff.build_parser().parse_args(
            [
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
                "marker",
                "--app-server-endpoint",
                ENDPOINT,
            ]
        )

    @staticmethod
    def verified_route() -> dict[str, object]:
        return {
            "actor_thread_id": OWNER,
            "owner_thread_id": OWNER,
            "metadata_verified": True,
            "route_verified": True,
            "route": "durable-self",
        }

    def test_schedule_persists_identity_and_job_key_changes_with_start_token(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            coordination_dir = root / "coordination"
            args = self.marker_schedule_args(state_dir)
            identities = [
                self.identity("boot-a:start-100"),
                self.identity("boot-a:start-200"),
            ]
            watcher_pids = iter((7001, 7002))

            def spawn_ack(
                _task_file: Path,
                _log_file: Path,
            ) -> tuple[object, dict[str, object]]:
                return mock.Mock(pid=next(watcher_pids)), {"phase": "watching"}

            with (
                mock.patch.object(
                    handoff,
                    "current_actor_thread_id",
                    return_value=(OWNER, False),
                ),
                mock.patch.object(
                    handoff,
                    "app_server_context_from_args",
                    return_value={
                        "source": "test-listener",
                        "attachable": True,
                        "endpoint": ENDPOINT,
                    },
                ),
                mock.patch.object(
                    handoff,
                    "app_server_auth_env_from_args",
                    return_value=None,
                ),
                mock.patch.object(
                    handoff,
                    "resolve_owner_route",
                    return_value=self.verified_route(),
                ),
                mock.patch.object(
                    handoff,
                    "capture_local_identity",
                    side_effect=identities,
                ) as capture,
                mock.patch.object(
                    handoff,
                    "do_preflight",
                    return_value=("alive", "matching stored identity"),
                ),
                mock.patch.object(
                    handoff,
                    "DEFAULT_COORDINATION_DIR",
                    str(coordination_dir),
                ),
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    side_effect=spawn_ack,
                ),
                mock.patch.object(handoff, "emit"),
            ):
                self.assertEqual(handoff.command_schedule(args), 0)
                self.assertEqual(handoff.command_schedule(args), 0)

            self.assertEqual(capture.call_args_list, [mock.call(30303), mock.call(30303)])
            tasks = [
                handoff.load_json(path)
                for path in sorted((state_dir / "tasks").glob("*.json"))
            ]
            self.assertEqual(len(tasks), 2)
            by_token = {
                task["target"]["process_identities"][0]["start_token"]: task
                for task in tasks
            }
            self.assertEqual(set(by_token), {"boot-a:start-100", "boot-a:start-200"})
            self.assertTrue(
                all(
                    task["target"]["identity_binding"]
                    == "schedule-time-incarnations"
                    for task in tasks
                )
            )
            self.assertNotEqual(tasks[0]["job_key"], tasks[1]["job_key"])

    def test_job_key_uses_only_immutable_process_incarnation_fields(self) -> None:
        first = process.ProcessIdentity(
            scope="local",
            pid=30303,
            ppid=1,
            state="S",
            source="test-process-start-token",
            start_token="boot-a:start-100",
            command="python train.py --first-discovery",
        )
        same_incarnation_new_snapshot = process.ProcessIdentity(
            scope="local",
            pid=30303,
            ppid=999,
            state="R",
            source="test-process-start-token",
            start_token="boot-a:start-100",
            command="python train.py --changed-argv",
        )
        replacement = process.ProcessIdentity(
            scope="local",
            pid=30303,
            ppid=999,
            state="R",
            source="test-process-start-token",
            start_token="boot-a:start-200",
            command="python train.py --changed-argv",
        )
        pid_target = {
            "scope": "local",
            "mode": "pid",
            "pid": 30303,
            "process_identities": [first.to_dict()],
        }
        pattern_target = {
            "scope": "local",
            "mode": "pattern",
            "pattern": "different-discovery-query",
            "process_identities": [same_incarnation_new_snapshot.to_dict()],
        }
        replacement_target = {
            **pattern_target,
            "process_identities": [replacement.to_dict()],
        }

        first_key = handoff.job_key_for_target(OWNER, pid_target)
        same_key = handoff.job_key_for_target(OWNER, pattern_target)
        replacement_key = handoff.job_key_for_target(OWNER, replacement_target)

        self.assertEqual(first_key, same_key)
        self.assertNotEqual(first_key, replacement_key)

    def test_target_identities_rejects_inconsistent_or_ambiguous_sets(self) -> None:
        local = self.identity("boot-a:local", pid=30303)
        other_pid = self.identity("boot-a:other-pid", pid=40404)
        remote_a = process.ProcessIdentity(
            scope="remote",
            host="builder-a.example",
            pid=30303,
            ppid=1,
            state="S",
            source="test-process-start-token",
            start_token="boot-a:remote-a",
            command="fake-remote-job",
        )
        remote_b = process.ProcessIdentity(
            scope="remote",
            host="builder-b.example",
            pid=30303,
            ppid=1,
            state="S",
            source="test-process-start-token",
            start_token="boot-a:remote-b",
            command="fake-remote-job",
        )
        cases = (
            (
                "scope mismatch",
                {
                    "scope": "local",
                    "mode": "pattern",
                    "pattern": "worker",
                    "process_identities": [remote_a.to_dict()],
                },
                "scope/host",
            ),
            (
                "host mismatch",
                {
                    "scope": "remote",
                    "host": "builder-a.example",
                    "mode": "pattern",
                    "pattern": "worker",
                    "process_identities": [remote_b.to_dict()],
                },
                "scope/host",
            ),
            (
                "PID mismatch",
                {
                    "scope": "local",
                    "mode": "pid",
                    "pid": 30303,
                    "process_identities": [other_pid.to_dict()],
                },
                "exactly one persisted incarnation",
            ),
            (
                "PID target has multiple identities",
                {
                    "scope": "local",
                    "mode": "pid",
                    "pid": 30303,
                    "process_identities": [local.to_dict(), other_pid.to_dict()],
                },
                "exactly one persisted incarnation",
            ),
            (
                "duplicate identity",
                {
                    "scope": "local",
                    "mode": "pattern",
                    "pattern": "worker",
                    "process_identities": [local.to_dict(), local.to_dict()],
                },
                "contains duplicates",
            ),
        )

        for label, target, error_pattern in cases:
            with self.subTest(case=label), self.assertRaisesRegex(
                handoff.ProcessIdentityError,
                error_pattern,
            ):
                handoff.target_identities(target)

    def test_probe_target_uses_stored_identity_and_pid_reuse_is_dead(self) -> None:
        expected = self.identity("boot-a:old", pid=4545)
        replacement = self.identity("boot-a:new", pid=4545)
        target = {
            "scope": "local",
            "mode": "pattern",
            "pattern": "must-not-be-run-again",
            "process_identities": [expected.to_dict()],
            "identity_binding": "schedule-time-incarnations",
        }
        with (
            mock.patch.object(
                process,
                "capture_local_identity",
                return_value=replacement,
            ) as capture,
            mock.patch.object(handoff, "find_local_pattern") as rediscover,
        ):
            state, detail = handoff.probe_target(target)

        self.assertEqual(state, "dead", detail)
        self.assertIn("PIDs were reused", detail)
        capture.assert_called_once_with(4545)
        rediscover.assert_not_called()

    def test_stop_target_terminates_only_stored_identities_without_pgrep(self) -> None:
        first = self.identity("boot-a:first", pid=501)
        second = self.identity("boot-a:second", pid=502)
        target = {
            "scope": "local",
            "mode": "pattern",
            "pattern": "must-not-be-run-again",
            "process_identities": [first.to_dict(), second.to_dict()],
            "identity_binding": "schedule-time-incarnations",
        }
        with (
            mock.patch.object(
                handoff,
                "terminate_local_identity",
                side_effect=[
                    {"status": "stopped", "identity": first.to_dict()},
                    {"status": "original_exited", "identity": second.to_dict()},
                ],
            ) as terminate,
            mock.patch.object(
                handoff,
                "find_local_pattern",
                side_effect=AssertionError("stop must not run pgrep again"),
            ) as rediscover,
        ):
            result = handoff.stop_target(target)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            terminate.call_args_list,
            [mock.call(first), mock.call(second)],
        )
        self.assertEqual(
            result["matched_identities"],
            [first.to_dict(), second.to_dict()],
        )
        rediscover.assert_not_called()

    def test_unsafe_remote_host_is_rejected_before_any_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            args = handoff.build_parser().parse_args(
                [
                    "schedule",
                    "--pid",
                    "30303",
                    "--host=-oProxyCommand=bad",
                    "--expected-seconds",
                    "300",
                    "--blocking",
                    "--state-dir",
                    str(state_dir),
                    "--resume-protocol",
                    "marker",
                    "--app-server-endpoint",
                    ENDPOINT,
                ]
            )
            with (
                mock.patch.object(
                    handoff,
                    "current_actor_thread_id",
                    return_value=(OWNER, False),
                ),
                mock.patch.object(
                    handoff,
                    "app_server_context_from_args",
                    return_value={
                        "source": "test-listener",
                        "attachable": True,
                        "endpoint": ENDPOINT,
                    },
                ),
                mock.patch.object(
                    handoff,
                    "app_server_auth_env_from_args",
                    return_value=None,
                ),
                mock.patch.object(
                    handoff,
                    "resolve_owner_route",
                    return_value=self.verified_route(),
                ),
                mock.patch.object(process, "_run_command") as run,
                mock.patch.object(handoff.OwnerJobRegistry, "reserve") as job_reserve,
                mock.patch.object(handoff.HandoffLedger, "register") as ledger_register,
                mock.patch.object(handoff, "do_preflight") as preflight,
                mock.patch.object(handoff, "spawn_watcher_with_ack") as spawn,
                self.assertRaises(SystemExit),
            ):
                handoff.command_schedule(args)

            run.assert_not_called()
            job_reserve.assert_not_called()
            ledger_register.assert_not_called()
            preflight.assert_not_called()
            spawn.assert_not_called()
            self.assertFalse(state_dir.exists())


if __name__ == "__main__":
    unittest.main()

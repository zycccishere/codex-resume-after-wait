from __future__ import annotations

import copy
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


OWNER = "019fb1a5-6269-7f03-8e49-415a5beb9ced"
NETWORK_AUTHORITY = {
    "endpoint": "wss://relay.example.invalid/app-server",
    "transport": "wss",
    "endpoint_fingerprint": None,
    "initialize": {
        "codexHome": "/tmp/codex-home",
        "platformFamily": "unix",
        "platformOs": "linux",
    },
    "remote_control": {
        "serverName": "owner",
        "installationId": "stable-installation",
        "environmentId": "stable-environment",
    },
}


def bind_fake_target(target: dict[str, object]) -> dict[str, object]:
    pid = int(target["pid"])
    identity = handoff.ProcessIdentity(
        scope="local",
        pid=pid,
        ppid=1,
        state="S",
        source="test-process-start-token",
        start_token=f"stable:{pid}",
        command="fake target",
    )
    target["process_identities"] = [identity.to_dict()]
    target["identity_binding"] = "schedule-time-incarnations"
    return target


class AuthorityStrengthTests(unittest.TestCase):
    def schedule(
        self,
        root: Path,
        *,
        context: dict[str, object],
        authority: dict[str, object],
        protocol: str = "auto",
        allow_weak: bool = False,
    ) -> tuple[dict[str, object], mock.Mock]:
        state_dir = root / "state"
        coordination_dir = root / "coordination"
        argv = [
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
            protocol,
            "--app-server-endpoint",
            str(authority["endpoint"]),
        ]
        if allow_weak:
            argv.append("--allow-weak-authority")
        args = handoff.build_parser().parse_args(argv)
        route = {
            "actor_thread_id": OWNER,
            "owner_thread_id": OWNER,
            "job_scope_id": OWNER,
            "metadata_verified": True,
            "route_verified": True,
            "route": "durable-self",
        }
        inspect = mock.Mock(
            return_value={
                "native_message_ready": True,
                "authority": copy.deepcopy(authority),
            }
        )
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    handoff, "current_actor_thread_id", return_value=(OWNER, False)
                )
            )
            stack.enter_context(
                mock.patch.object(
                    handoff,
                    "app_server_context_from_args",
                    return_value=copy.deepcopy(context),
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
                    side_effect=bind_fake_target,
                )
            )
            stack.enter_context(
                mock.patch.object(handoff, "inspect_native_thread", inspect)
            )
            stack.enter_context(
                mock.patch.object(
                    handoff, "do_preflight", return_value=("alive", "running")
                )
            )
            stack.enter_context(
                mock.patch.object(
                    handoff, "DEFAULT_COORDINATION_DIR", str(coordination_dir)
                )
            )
            stack.enter_context(
                mock.patch.object(
                    handoff,
                    "spawn_watcher_with_ack",
                    return_value=(mock.Mock(pid=4040), {"phase": "watching"}),
                )
            )
            stack.enter_context(mock.patch.object(handoff, "emit"))
            self.assertEqual(handoff.command_schedule(args), 0)
        task_file = next((state_dir / "tasks").glob("*.json"))
        return handoff.load_json(task_file), inspect

    def test_exact_ancestor_unix_listener_is_strong(self) -> None:
        identity = handoff.capture_local_identity(os.getpid())
        authority = {
            **NETWORK_AUTHORITY,
            "endpoint": "unix:///tmp/codex-owner.sock",
            "transport": "unix",
            "endpoint_fingerprint": "unix-inode:1:2",
        }
        context = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": authority["endpoint"],
            "app_server_pid": os.getpid(),
            "owning_app_server_identity": identity.to_dict(),
        }
        assessment = handoff.assess_authority_strength(context, authority)
        self.assertEqual(assessment["authority_strength"], "strong")
        self.assertEqual(
            assessment["owner_process_identity"],
            handoff.durable_authority_process_identity(identity),
        )

        with tempfile.TemporaryDirectory() as temp:
            task, _inspect = self.schedule(
                Path(temp), context=context, authority=authority
            )
        self.assertEqual(task["resume_protocol"], "native-message")
        self.assertEqual(task["authority"]["authority_strength"], "strong")
        self.assertIs(task["authority"]["weak_authority_accepted"], False)
        self.assertNotIn("command", task["authority"]["owner_process_identity"])

    def test_desktop_ssh_unix_listener_is_discovered_as_attachable(self) -> None:
        app_server_pid = 4242
        app_server_identity = handoff.ProcessIdentity(
            scope="local",
            pid=app_server_pid,
            ppid=1,
            state="S",
            source="linux-proc-starttime",
            start_token="123456",
            command="codex app-server",
        )
        rows = [
            {
                "pid": os.getpid(),
                "ppid": app_server_pid,
                "command": "python3 codex_wait_handoff.py doctor",
            },
            {
                "pid": app_server_pid,
                "ppid": 1,
                "command": (
                    "codex -c features.code_mode_host=true app-server "
                    "--listen unix://"
                ),
            },
        ]
        with (
            mock.patch.object(handoff, "process_rows", return_value=rows),
            mock.patch.object(
                handoff,
                "capture_local_identity",
                return_value=app_server_identity,
            ),
        ):
            context = handoff.ancestor_app_server_context()

        self.assertEqual(context["source"], "ancestor-listener")
        self.assertIs(context["attachable"], True)
        self.assertEqual(context["endpoint"], "unix://")
        self.assertEqual(context["app_server_pid"], app_server_pid)
        self.assertEqual(
            context["owning_app_server_identity"],
            handoff.durable_authority_process_identity(app_server_identity),
        )

    def test_strong_requires_attachable_non_diagnostic_context(self) -> None:
        identity = handoff.capture_local_identity(os.getpid())
        endpoint = "unix:///tmp/codex-owner.sock"
        authority = {
            **NETWORK_AUTHORITY,
            "endpoint": endpoint,
            "transport": "unix",
            "endpoint_fingerprint": "unix-inode:1:2",
        }
        base = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": endpoint,
            "ancestor_endpoint": endpoint,
            "endpoint_matches_ancestor": True,
            "owning_app_server_identity": (
                handoff.durable_authority_process_identity(identity)
            ),
        }
        for mutation in (
            {"attachable": False},
            {"diagnostic_endpoint_only": True},
        ):
            with self.subTest(mutation=mutation):
                assessment = handoff.assess_authority_strength(
                    {**base, **mutation},
                    authority,
                )
                self.assertEqual(assessment["authority_strength"], "weak")

    def test_exact_explicit_endpoint_preserves_ancestor_incarnation(self) -> None:
        endpoint = "unix:///tmp/codex-owner.sock"
        identity = handoff.capture_local_identity(os.getpid())
        discovered = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": endpoint,
            "app_server_pid": os.getpid(),
            "owning_app_server_identity": (
                handoff.durable_authority_process_identity(identity)
            ),
        }
        args = handoff.build_parser().parse_args(
            ["doctor", "--app-server-endpoint", endpoint]
        )
        with mock.patch.object(
            handoff,
            "ancestor_app_server_context",
            return_value=copy.deepcopy(discovered),
        ):
            context = handoff.app_server_context_from_args(args)

        self.assertEqual(context["source"], "ancestor-listener")
        self.assertEqual(context["endpoint_configuration_source"], "explicit-argument")
        self.assertEqual(
            context["owning_app_server_identity"],
            discovered["owning_app_server_identity"],
        )

    def test_exact_configured_endpoint_preserves_ancestor_incarnation(self) -> None:
        endpoint = "unix:///tmp/codex-owner.sock"
        identity = handoff.capture_local_identity(os.getpid())
        discovered = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": endpoint,
            "app_server_pid": os.getpid(),
            "owning_app_server_identity": (
                handoff.durable_authority_process_identity(identity)
            ),
        }
        args = handoff.build_parser().parse_args(["doctor"])
        with (
            mock.patch.object(
                handoff,
                "ancestor_app_server_context",
                return_value=copy.deepcopy(discovered),
            ),
            mock.patch.dict(
                os.environ,
                {"CODEX_WAIT_APP_SERVER_ENDPOINT": endpoint},
            ),
        ):
            context = handoff.app_server_context_from_args(args)

        self.assertEqual(context["source"], "ancestor-listener")
        self.assertEqual(context["endpoint_configuration_source"], "environment")
        self.assertEqual(
            context["owning_app_server_identity"],
            discovered["owning_app_server_identity"],
        )

    def test_network_auto_defaults_to_marker_and_opt_in_enables_native(self) -> None:
        context = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": NETWORK_AUTHORITY["endpoint"],
            "owning_app_server_identity": handoff.capture_local_identity(
                os.getpid()
            ).to_dict(),
        }
        with tempfile.TemporaryDirectory() as temp:
            marker, _inspect = self.schedule(
                Path(temp), context=context, authority=NETWORK_AUTHORITY
            )
        self.assertEqual(marker["resume_protocol"], "marker")
        self.assertEqual(
            marker["authority_assessment"]["authority_strength"], "weak"
        )

        with tempfile.TemporaryDirectory() as temp:
            native, _inspect = self.schedule(
                Path(temp),
                context=context,
                authority=NETWORK_AUTHORITY,
                allow_weak=True,
            )
        self.assertEqual(native["resume_protocol"], "native-message")
        self.assertTrue(native["allow_weak_authority"])
        self.assertEqual(native["authority"]["authority_strength"], "weak")
        self.assertIs(native["authority"]["weak_authority_accepted"], True)
        self.assertIs(native["strict_exactly_once"], False)

    def test_ws_ancestor_to_wss_alias_is_attachable_but_weak(self) -> None:
        ancestor_endpoint = "ws://127.0.0.1:4500/"
        alias_endpoint = str(NETWORK_AUTHORITY["endpoint"])
        identity = handoff.capture_local_identity(os.getpid())
        discovered = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": ancestor_endpoint,
            "ancestor_endpoint": ancestor_endpoint,
            "endpoint_matches_ancestor": True,
            "app_server_pid": os.getpid(),
            "owning_app_server_identity": (
                handoff.durable_authority_process_identity(identity)
            ),
        }
        args = handoff.build_parser().parse_args(
            ["doctor", "--app-server-endpoint", alias_endpoint]
        )
        with mock.patch.object(
            handoff,
            "ancestor_app_server_context",
            return_value=copy.deepcopy(discovered),
        ):
            context = handoff.app_server_context_from_args(args)

        self.assertIs(context["attachable"], True)
        self.assertIs(context["endpoint_matches_ancestor"], False)
        self.assertEqual(context["ancestor_endpoint"], ancestor_endpoint)
        assessment = handoff.assess_authority_strength(context, NETWORK_AUTHORITY)
        self.assertEqual(assessment["authority_strength"], "weak")
        self.assertIn("explicit alias", assessment["authority_strength_reason"])

        with tempfile.TemporaryDirectory() as temp:
            marker, _inspect = self.schedule(
                Path(temp), context=context, authority=NETWORK_AUTHORITY
            )
        self.assertEqual(marker["resume_protocol"], "marker")
        with tempfile.TemporaryDirectory() as temp:
            native, _inspect = self.schedule(
                Path(temp),
                context=context,
                authority=NETWORK_AUTHORITY,
                allow_weak=True,
            )
        self.assertEqual(native["resume_protocol"], "native-message")
        self.assertIs(native["authority"]["endpoint_matches_ancestor"], False)
        self.assertEqual(native["authority"]["ancestor_endpoint"], ancestor_endpoint)

    def test_mismatched_unix_alias_cannot_be_strong(self) -> None:
        ancestor_endpoint = "unix:///tmp/codex-owner.sock"
        alias_endpoint = "unix:///tmp/codex-owner-alias.sock"
        identity = handoff.capture_local_identity(os.getpid())
        discovered = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": ancestor_endpoint,
            "ancestor_endpoint": ancestor_endpoint,
            "endpoint_matches_ancestor": True,
            "app_server_pid": os.getpid(),
            "owning_app_server_identity": (
                handoff.durable_authority_process_identity(identity)
            ),
        }
        args = handoff.build_parser().parse_args(
            ["doctor", "--app-server-endpoint", alias_endpoint]
        )
        with mock.patch.object(
            handoff,
            "ancestor_app_server_context",
            return_value=copy.deepcopy(discovered),
        ):
            context = handoff.app_server_context_from_args(args)
        authority = {
            **NETWORK_AUTHORITY,
            "endpoint": alias_endpoint,
            "transport": "unix",
            "endpoint_fingerprint": "unix-inode:1:2",
        }

        self.assertIs(context["attachable"], True)
        self.assertIs(context["endpoint_matches_ancestor"], False)
        assessment = handoff.assess_authority_strength(context, authority)
        self.assertEqual(assessment["authority_strength"], "weak")
        self.assertIn("explicit alias", assessment["authority_strength_reason"])
        forged_match = handoff.assess_authority_strength(
            {**context, "endpoint_matches_ancestor": True},
            authority,
        )
        self.assertEqual(forged_match["authority_strength"], "weak")
        with tempfile.TemporaryDirectory() as temp:
            marker, _inspect = self.schedule(
                Path(temp), context=context, authority=authority
            )
        self.assertEqual(marker["resume_protocol"], "marker")

    def test_explicit_native_rejects_weak_without_opt_in(self) -> None:
        context = {
            "source": "explicit-argument",
            "attachable": True,
            "endpoint": NETWORK_AUTHORITY["endpoint"],
        }
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(SystemExit):
            self.schedule(
                Path(temp),
                context=context,
                authority=NETWORK_AUTHORITY,
                protocol="native-message",
            )

    def test_weak_opt_in_cannot_bypass_private_stdio(self) -> None:
        context = {
            "source": "ancestor-private-stdio",
            "attachable": False,
            "endpoint": NETWORK_AUTHORITY["endpoint"],
            "reason": "private stdio has no second-client endpoint",
        }
        with tempfile.TemporaryDirectory() as temp:
            task, inspect = self.schedule(
                Path(temp),
                context=context,
                authority=NETWORK_AUTHORITY,
                allow_weak=True,
            )
        self.assertEqual(task["resume_protocol"], "marker")
        inspect.assert_not_called()

        with tempfile.TemporaryDirectory() as temp, self.assertRaises(SystemExit):
            self.schedule(
                Path(temp),
                context=context,
                authority=NETWORK_AUTHORITY,
                protocol="native-message",
                allow_weak=True,
            )

        args = handoff.build_parser().parse_args(
            ["doctor", "--app-server-endpoint", str(NETWORK_AUTHORITY["endpoint"])]
        )
        with mock.patch.object(
            handoff,
            "ancestor_app_server_context",
            return_value={
                "source": "ancestor-private-stdio",
                "attachable": False,
                "reason": "private stdio has no second-client endpoint",
            },
        ):
            configured = handoff.app_server_context_from_args(args)
        self.assertIs(configured["attachable"], False)
        self.assertIs(configured["diagnostic_endpoint_only"], True)
        self.assertIs(configured["endpoint_matches_ancestor"], False)

    def test_rebind_reprobes_strong_destination_immediately_before_commit(self) -> None:
        endpoint = "unix:///tmp/codex-new-owner.sock"
        identity = handoff.capture_local_identity(os.getpid())
        context = {
            "source": "ancestor-listener",
            "attachable": True,
            "endpoint": endpoint,
            "ancestor_endpoint": endpoint,
            "endpoint_matches_ancestor": True,
            "app_server_pid": os.getpid(),
            "owning_app_server_identity": (
                handoff.durable_authority_process_identity(identity)
            ),
        }
        destination_authority = {
            "endpoint": endpoint,
            "transport": "unix",
            "endpoint_fingerprint": "unix-inode:9:9",
            "initialize": {
                "codexHome": "/tmp/codex-home",
                "platformFamily": "unix",
                "platformOs": "linux",
            },
        }
        route = {
            "actor_thread_id": OWNER,
            "owner_thread_id": OWNER,
            "job_scope_id": OWNER,
            "route_verified": True,
        }
        alive = mock.Mock(status="alive", reason="same_identity", detail="alive")
        dead = mock.Mock(
            status="dead",
            reason="identity_changed",
            detail="PID was reused",
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            coordination_dir = root / "coordination"
            ledger = handoff.HandoffLedger(coordination_dir, OWNER)
            ledger.register(
                "task-1",
                "event-1",
                root / "task.json",
                "token-1",
                {"endpoint": "unix:///tmp/old-owner.sock"},
            )
            ledger.freeze_authority(1)
            args = handoff.build_parser().parse_args(
                [
                    "rebind",
                    "--owner-thread-id",
                    OWNER,
                    "--expected-epoch",
                    "1",
                    "--coordination-dir",
                    str(coordination_dir),
                    "--app-server-endpoint",
                    endpoint,
                    "--json",
                ]
            )
            with (
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": OWNER}),
                mock.patch.object(
                    handoff,
                    "app_server_context_from_args",
                    return_value=copy.deepcopy(context),
                ),
                mock.patch.object(
                    handoff,
                    "app_server_auth_env_from_args",
                    return_value=None,
                ),
                mock.patch.object(handoff, "resolve_owner_route", return_value=route),
                mock.patch.object(
                    handoff,
                    "inspect_native_thread",
                    return_value={
                        "native_message_ready": True,
                        "authority": copy.deepcopy(destination_authority),
                    },
                ),
                mock.patch.object(
                    handoff,
                    "probe_local_identity",
                    side_effect=[alive, dead],
                ) as probe,
                self.assertRaises(SystemExit),
            ):
                handoff.command_rebind(args)

            self.assertEqual(probe.call_count, 2)
            snapshot = ledger.snapshot()
            assert snapshot is not None
            self.assertEqual(snapshot["authority_epoch"], 1)
            self.assertEqual(snapshot["mode"], "DRAINING")
            self.assertEqual(
                snapshot["authority"]["endpoint"],
                "unix:///tmp/old-owner.sock",
            )

    def test_pid_reuse_or_exit_invalidates_strong_ticket(self) -> None:
        identity = handoff.capture_local_identity(os.getpid())
        authority = {
            "authority_strength": "strong",
            "owner_process_identity": identity.to_dict(),
        }
        result = mock.Mock(
            status="dead",
            reason="identity_changed",
            detail="PID was reused",
        )
        with (
            mock.patch.object(handoff, "probe_local_identity", return_value=result),
            self.assertRaisesRegex(handoff.AuthorityMismatch, "no longer alive"),
        ):
            handoff.probe_strong_authority_process(authority)


if __name__ == "__main__":
    unittest.main()

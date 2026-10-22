"""Regressions for managed-daemon socket aliases and authority fencing."""

import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "blocking-wait-handoff" / "scripts"))

from codex_app_server import (
    authority_descriptor_mismatch,
    default_app_server_endpoint,
    parse_app_server_endpoint,
    unix_socket_fingerprint,
)
from codex_wait_handoff import assess_authority_strength
from process_identity import capture_local_identity


class DefaultSocketAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="handoff-authority-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.real = self.root / "daemon.sock"
        self.other = self.root / "other.sock"
        for path in (self.real, self.other):
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(path))
        self.alias = self.root / "home/app-server-control/app-server-control.sock"
        self.alias.parent.mkdir(parents=True)
        self.alias.symlink_to(self.real)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("CODEX_WAIT_APP_SERVER_SOCKET", None)
        self.context = {
            "source": "ancestor-listener",
            "attachable": True,
            "ancestor_endpoint": "unix://",
            "endpoint_matches_ancestor": True,
            "owning_app_server_identity": capture_local_identity(os.getpid()).to_dict(),
        }

    def descriptor(self, path):
        endpoint = parse_app_server_endpoint(path)
        return {
            "transport": "unix",
            "endpoint": endpoint.uri,
            "endpoint_fingerprint": unix_socket_fingerprint(endpoint.socket_path),
        }

    def test_default_alias_explicit_path_and_uri_are_equivalent(self):
        expected = parse_app_server_endpoint(self.real)
        for value in ("unix://", self.alias, f"unix://{self.alias}", default_app_server_endpoint()):
            with self.subTest(value=value):
                endpoint = parse_app_server_endpoint(value)
                self.assertEqual(endpoint, expected)
                self.assertEqual(parse_app_server_endpoint(endpoint.uri), endpoint)

    def test_configured_socket_alias_is_canonical(self):
        with patch.dict(os.environ, {"CODEX_WAIT_APP_SERVER_SOCKET": str(self.alias)}):
            self.assertEqual(parse_app_server_endpoint("unix://"), parse_app_server_endpoint(self.real))

    def test_same_ancestor_listener_gets_strong_authority(self):
        result = assess_authority_strength(self.context, self.descriptor(self.alias))
        self.assertEqual(result["authority_strength"], "strong", result)

    def test_different_listener_remains_weak(self):
        result = assess_authority_strength(self.context, self.descriptor(self.other))
        self.assertEqual(result["authority_strength"], "weak")
        self.assertFalse(result["endpoint_matches_ancestor"])

    def test_retargeted_alias_does_not_match_original_ticket(self):
        original = self.descriptor(self.alias)
        self.alias.unlink()
        self.alias.symlink_to(self.other)
        self.assertIsNotNone(authority_descriptor_mismatch(original, self.descriptor(self.alias)))
        self.assertEqual(assess_authority_strength(self.context, original)["authority_strength"], "weak")

    def test_inode_and_process_incarnation_fences_remain_required(self):
        original = self.descriptor(self.alias)
        changed = {**original, "endpoint_fingerprint": "different-inode"}
        self.assertEqual(authority_descriptor_mismatch(original, changed), "endpoint fingerprint changed")
        missing = {**original, "endpoint_fingerprint": None}
        self.assertEqual(assess_authority_strength(self.context, missing)["authority_strength"], "weak")
        stale = {**self.context, "owning_app_server_identity": {
            **self.context["owning_app_server_identity"], "start_token": "stale-incarnation"
        }}
        self.assertEqual(assess_authority_strength(stale, original)["authority_strength"], "weak")


if __name__ == "__main__":
    unittest.main()

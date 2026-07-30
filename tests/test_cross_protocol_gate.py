from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from job_registry import JobConflict, OwnerJobRegistry  # noqa: E402


class CrossProtocolOwnerGateTests(unittest.TestCase):
    def reserve(
        self,
        registry: OwnerJobRegistry,
        suffix: str,
        protocol: str,
        owner: str,
    ) -> int:
        return registry.reserve(
            f"job-{suffix}",
            f"task-{suffix}",
            f"event-{suffix}",
            f"/tmp/task-{suffix}.json",
            f"token-{suffix}",
            protocol,
            owner,
        )

    def test_unresolved_native_and_marker_cannot_mix_for_one_owner(self) -> None:
        for first, second in (
            ("native-message", "marker"),
            ("marker", "native-message"),
        ):
            with self.subTest(first=first, second=second), tempfile.TemporaryDirectory() as temp:
                registry = OwnerJobRegistry(temp, "fork-lineage-root")
                self.reserve(registry, "first", first, "owner-branch")
                with self.assertRaisesRegex(JobConflict, "cannot mix"):
                    self.reserve(registry, "second", second, "owner-branch")

    def test_protocol_switch_is_allowed_after_definitive_terminal_state(self) -> None:
        for outcome in ("accepted", "blocked", "cancelled"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                registry = OwnerJobRegistry(temp, "fork-lineage-root")
                generation = self.reserve(
                    registry,
                    "first",
                    "native-message",
                    "owner-branch",
                )
                registry.finish("task-first", "token-first", generation, outcome)
                self.assertEqual(
                    self.reserve(registry, "second", "marker", "owner-branch"),
                    2,
                )

    def test_unknown_permanently_blocks_protocol_switch_for_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = OwnerJobRegistry(temp, "fork-lineage-root")
            generation = self.reserve(
                registry,
                "first",
                "native-message",
                "owner-branch",
            )
            registry.finish("task-first", "token-first", generation, "unknown")
            with self.assertRaisesRegex(JobConflict, "cannot mix"):
                self.reserve(registry, "second", "marker", "owner-branch")

    def test_distinct_fork_owners_do_not_share_the_protocol_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = OwnerJobRegistry(temp, "fork-lineage-root")
            self.assertEqual(
                self.reserve(registry, "first", "native-message", "fork-a"),
                1,
            )
            self.assertEqual(
                self.reserve(registry, "second", "marker", "fork-b"),
                2,
            )


if __name__ == "__main__":
    unittest.main()

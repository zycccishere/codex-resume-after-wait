from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_wait_handoff as handoff  # noqa: E402


class DeliveryDecisionMatrixTests(unittest.TestCase):
    def test_every_supported_owner_authority_branch_has_one_decision(self) -> None:
        base = {
            "route_verified": True,
            "attachable": True,
            "native_message_ready": True,
            "authority_strength": "strong",
            "allow_weak_authority": False,
            "context_reason": "private owner",
            "probe_reason": "owner not loaded",
            "authority_strength_reason": "weak instance fence",
        }
        cases = (
            (
                "unverified auto",
                "auto",
                {"route_verified": False},
                "reject",
                "unverified-owner-route",
            ),
            (
                "unverified marker",
                "marker",
                {"route_verified": False},
                "reject",
                "unverified-owner-route",
            ),
            (
                "explicit marker",
                "marker",
                {"attachable": False, "native_message_ready": False},
                "marker",
                "explicit-marker",
            ),
            (
                "private auto",
                "auto",
                {"attachable": False, "native_message_ready": False},
                "marker",
                "marker-owner-not-attachable",
            ),
            (
                "private explicit native",
                "native-message",
                {"attachable": False, "native_message_ready": False},
                "reject",
                "native-rejected-owner-not-attachable",
            ),
            (
                "unloaded auto",
                "auto",
                {"native_message_ready": False, "authority_strength": None},
                "marker",
                "marker-owner-not-ready",
            ),
            (
                "unloaded explicit native",
                "native-message",
                {"native_message_ready": False, "authority_strength": None},
                "reject",
                "native-rejected-owner-not-ready",
            ),
            (
                "strong auto",
                "auto",
                {},
                "native-message",
                "native-strong-authority",
            ),
            (
                "weak auto",
                "auto",
                {"authority_strength": "weak"},
                "marker",
                "marker-weak-authority",
            ),
            (
                "weak explicit native",
                "native-message",
                {"authority_strength": "weak"},
                "reject",
                "native-rejected-weak-authority",
            ),
            (
                "weak auto opt in",
                "auto",
                {
                    "authority_strength": "weak",
                    "allow_weak_authority": True,
                },
                "native-message",
                "native-weak-authority-opt-in",
            ),
            (
                "unclassified auto",
                "auto",
                {"authority_strength": None},
                "marker",
                "marker-unclassified-authority",
            ),
        )

        for name, requested, changes, action, branch in cases:
            with self.subTest(name=name):
                decision = handoff.classify_delivery_decision(
                    requested,
                    **{**base, **changes},
                )
                self.assertEqual(decision["action"], action)
                self.assertEqual(decision["branch"], branch)
                self.assertTrue(decision["reason"])


if __name__ == "__main__":
    unittest.main()

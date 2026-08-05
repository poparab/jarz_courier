"""The courier-setup validator.

A courier needs BOTH a ``POS Profile User`` row AND ``Employee.branch`` set to that
same POS Profile name. Miss the first and every branch-scoped query filters on an
empty list, so the run sheet renders as "no stops" — identical to a genuinely empty
day. Miss the second and jarz_pos throws ``"Courier X has no branch and cannot be
assigned"``, which says nothing about which form to open.

These tests pin the four broken shapes to four distinct problem codes and assert
each message names the record to fix, because a message that only says
"not permitted" is the bug this module exists to remove.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.services import courier_onboarding  # noqa: E402

IDENTITY = {
    "user": "courier@example.com",
    "party_type": "Employee",
    "party": "HR-EMP-00042",
    "display_name": "Mahmoud",
    "branch": "Dokki",
}


def _diagnose(*, identity=IDENTITY, profiles=("Dokki",), identity_raises=False):
    identity_patch = patch.object(
        courier_onboarding.pos_bridge,
        "resolve_courier_identity",
        side_effect=Exception("no employee") if identity_raises else None,
        return_value=None if identity_raises else dict(identity),
    )
    profiles_patch = patch.object(
        courier_onboarding.pos_bridge, "get_user_pos_profiles", return_value=list(profiles)
    )
    with identity_patch, profiles_patch:
        return courier_onboarding.diagnose_courier_setup("courier@example.com")


class TestDiagnoseCourierSetup(unittest.TestCase):
    def test_correctly_wired_courier_is_ok(self) -> None:
        result = _diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual([], result["problems"])
        self.assertEqual("Dokki", result["branch"])
        self.assertEqual("Employee", result["party_type"])
        self.assertEqual("HR-EMP-00042", result["party"])

    def test_no_employee_record_is_reported_as_no_identity(self) -> None:
        result = _diagnose(identity_raises=True)
        self.assertFalse(result["ok"])
        self.assertEqual(["no_identity"], result["problems"])
        self.assertIn("Employee", result["message"])
        self.assertIn("User ID", result["message"])

    def test_missing_pos_profile_row_names_the_profile_to_edit(self) -> None:
        """Employee.branch is set, but nobody added the login to that branch."""
        result = _diagnose(profiles=())
        self.assertFalse(result["ok"])
        self.assertEqual(["no_pos_profile"], result["problems"])
        self.assertIn("Dokki", result["message"])
        self.assertIn("POS Profile", result["message"])

    def test_missing_employee_branch_names_the_employee_form(self) -> None:
        identity = dict(IDENTITY, branch="")
        result = _diagnose(identity=identity)
        self.assertFalse(result["ok"])
        self.assertEqual(["no_employee_branch"], result["problems"])
        self.assertIn("Employee", result["message"])
        self.assertIn("Dokki", result["message"])

    def test_mismatched_branch_reports_both_sides(self) -> None:
        """The silent killer: both records set, to different values."""
        identity = dict(IDENTITY, branch="Zamalek")
        result = _diagnose(identity=identity, profiles=("Dokki",))
        self.assertFalse(result["ok"])
        self.assertEqual(["branch_not_assigned"], result["problems"])
        self.assertIn("Zamalek", result["message"])
        self.assertIn("Dokki", result["message"])

    def test_completely_unconnected_courier_reports_both_problems(self) -> None:
        identity = dict(IDENTITY, branch="")
        result = _diagnose(identity=identity, profiles=())
        self.assertFalse(result["ok"])
        self.assertEqual({"no_pos_profile", "no_employee_branch"}, set(result["problems"]))
        self.assertIn("POS Profile", result["message"])
        self.assertIn("Employee", result["message"])

    def test_diagnose_never_raises(self) -> None:
        """It is called on the app's first screen; an exception there is a blank app."""
        with patch.object(
            courier_onboarding.pos_bridge,
            "resolve_courier_identity",
            side_effect=RuntimeError("boom"),
        ), patch.object(
            courier_onboarding.pos_bridge,
            "get_user_pos_profiles",
            side_effect=RuntimeError("boom"),
        ):
            result = courier_onboarding.diagnose_courier_setup("courier@example.com")
        self.assertFalse(result["ok"])
        self.assertIn("no_identity", result["problems"])


class TestEnsureCourierSetup(unittest.TestCase):
    def test_returns_identity_when_wired(self) -> None:
        with patch.object(
            courier_onboarding, "diagnose_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
        ):
            result = courier_onboarding.ensure_courier_setup(action_label="the run sheet")
        self.assertEqual("HR-EMP-00042", result["party"])

    def test_throws_courier_setup_error_carrying_the_fix(self) -> None:
        broken = {
            "ok": False,
            "problems": ["no_pos_profile"],
            "message": "add this login to Applicable for Users",
            "pos_profiles": [],
        }
        with patch.object(courier_onboarding, "diagnose_courier_setup", return_value=broken):
            with self.assertRaises(courier_onboarding.CourierSetupError) as ctx:
                courier_onboarding.ensure_courier_setup(action_label="the run sheet")

        message = str(ctx.exception)
        self.assertIn("the run sheet", message)
        self.assertIn("Applicable for Users", message)


class TestResolveActiveBranch(unittest.TestCase):
    def test_defaults_to_the_couriers_own_branch(self) -> None:
        with patch.object(
            courier_onboarding, "diagnose_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
        ):
            result = courier_onboarding.resolve_active_branch(None, action_label="the run sheet")
        self.assertEqual("Dokki", result["branch"])

    def test_rejects_a_branch_the_courier_is_not_assigned_to(self) -> None:
        """A client-supplied branch is a scoping bypass, not a preference."""
        with patch.object(
            courier_onboarding, "diagnose_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
        ):
            with self.assertRaises(frappe.PermissionError):
                courier_onboarding.resolve_active_branch("Zamalek", action_label="the run sheet")

    def test_accepts_the_couriers_own_branch_when_named_explicitly(self) -> None:
        with patch.object(
            courier_onboarding, "diagnose_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
        ):
            result = courier_onboarding.resolve_active_branch("Dokki", action_label="the run sheet")
        self.assertEqual("Dokki", result["branch"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

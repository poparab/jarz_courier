"""``api/duty`` — duty session transport.

The properties pinned here:

* the branch is **validated**, never trusted from the client — ``start_duty``
  resolves it through ``courier_onboarding.resolve_active_branch``;
* realtime fires only on a real transition, so a replayed offline "start duty"
  does not tell the whole branch a courier started twice;
* reading another courier's duty needs both a supervisor role *and* a shared
  branch, since supervisor roles are site-wide but branches are not.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import duty  # noqa: E402

DUTY_ROW = {
    "name": "CDUTY-00001",
    "party_type": "Employee",
    "party": "HR-EMP-00042",
    "branch": "Dokki",
    "status": "Open",
    "start_time": "2026-08-05 08:00:00",
    "end_time": None,
    "opening_float": 200.0,
    "closing_cash": 0.0,
}


def _roles(*names: str):
    return patch.object(duty.frappe, "get_roles", return_value=list(names))


def _branch():
    return patch.object(
        duty.courier_onboarding, "resolve_active_branch", return_value=dict(_support.COURIER_IDENTITY)
    )


def _identity():
    return patch.object(
        duty.courier_onboarding, "ensure_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
    )


class TestPermissionGate(unittest.TestCase):
    def test_every_endpoint_rejects_a_user_with_no_courier_role(self) -> None:
        with _roles(*_support.NO_ROLES):
            for call in (duty.start_duty, duty.end_duty, duty.get_duty_summary):
                with self.assertRaises(frappe.PermissionError):
                    call()

    def test_gate_runs_before_branch_resolution(self) -> None:
        with _roles(*_support.NO_ROLES), patch.object(
            duty.courier_onboarding, "resolve_active_branch"
        ) as resolve:
            with self.assertRaises(frappe.PermissionError):
                duty.start_duty("Zamalek")
        resolve.assert_not_called()


class TestStartDuty(unittest.TestCase):
    def test_uses_the_validated_branch_not_the_requested_one(self) -> None:
        with _roles(*_support.COURIER_ROLES), _branch(), patch.object(
            duty.device_registry, "get_active_device", return_value={"name": "CDEV-00001"}
        ), patch.object(
            duty.duty_session, "start_duty", return_value={"duty": DUTY_ROW, "created": True}
        ) as start, patch.object(duty.pos_bridge, "publish_to_branches"):
            result = duty.start_duty(branch="Dokki", opening_float=200)

        self.assertTrue(result["success"])
        kwargs = start.call_args.kwargs
        self.assertEqual("Dokki", kwargs["branch"])
        self.assertEqual("HR-EMP-00042", kwargs["party"])
        self.assertEqual("CDEV-00001", kwargs["device"])

    def test_rejects_a_branch_the_courier_is_not_assigned_to(self) -> None:
        with _roles(*_support.COURIER_ROLES), patch.object(
            duty.courier_onboarding,
            "resolve_active_branch",
            side_effect=frappe.PermissionError("wrong branch"),
        ):
            with self.assertRaises(frappe.PermissionError):
                duty.start_duty(branch="Zamalek")

    def test_publishes_only_when_a_duty_is_actually_created(self) -> None:
        """A replayed offline start returns the open duty and stays quiet."""
        with _roles(*_support.COURIER_ROLES), _branch(), patch.object(
            duty.device_registry, "get_active_device", return_value=None
        ), patch.object(
            duty.duty_session, "start_duty", return_value={"duty": DUTY_ROW, "created": False}
        ), patch.object(duty.pos_bridge, "publish_to_branches") as publish:
            result = duty.start_duty()

        self.assertTrue(result["success"])
        self.assertFalse(result["created"])
        publish.assert_not_called()

    def test_publishes_the_frozen_duty_changed_event(self) -> None:
        with _roles(*_support.COURIER_ROLES), _branch(), patch.object(
            duty.device_registry, "get_active_device", return_value=None
        ), patch.object(
            duty.duty_session, "start_duty", return_value={"duty": DUTY_ROW, "created": True}
        ), patch.object(duty.pos_bridge, "publish_to_branches") as publish:
            duty.start_duty()

        event, payload, profiles = publish.call_args.args
        self.assertEqual("jarz_pos_courier_duty_changed", event)
        self.assertEqual(["Dokki"], profiles)
        self.assertEqual("CDUTY-00001", payload["duty"])

    def test_unexpected_failure_returns_an_envelope(self) -> None:
        with _roles(*_support.COURIER_ROLES), _branch(), patch.object(
            duty.device_registry, "get_active_device", return_value=None
        ), patch.object(
            duty.duty_session, "start_duty", side_effect=RuntimeError("locked")
        ), patch.object(duty.frappe, "log_error"):
            result = duty.start_duty()

        self.assertFalse(result["success"])
        self.assertIn("locked", result["error"])


class TestEndDuty(unittest.TestCase):
    def test_returns_the_reconciliation_summary(self) -> None:
        summary = {"stops_delivered": 7, "expected_cash": 4200.0, "variance": 0.0}
        closed = dict(DUTY_ROW, status="Closed")
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            duty.duty_session,
            "end_duty",
            return_value={"duty": closed, "changed": True, "summary": summary},
        ), patch.object(duty.pos_bridge, "publish_to_branches"):
            result = duty.end_duty(closing_cash=4200)

        self.assertTrue(result["success"])
        self.assertEqual(7, result["summary"]["stops_delivered"])

    def test_replayed_end_is_idempotent_and_silent(self) -> None:
        closed = dict(DUTY_ROW, status="Closed")
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            duty.duty_session,
            "end_duty",
            return_value={"duty": closed, "changed": False, "summary": {}},
        ), patch.object(duty.pos_bridge, "publish_to_branches") as publish:
            result = duty.end_duty()

        self.assertTrue(result["success"])
        self.assertFalse(result["changed"])
        publish.assert_not_called()


class TestGetDutySummary(unittest.TestCase):
    def test_no_open_duty_returns_null_rather_than_an_error(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            duty.duty_session, "get_open_duty", return_value=None
        ):
            result = duty.get_duty_summary()

        self.assertTrue(result["success"])
        self.assertIsNone(result["duty"])
        self.assertIsNone(result["summary"])

    def test_summarises_the_open_duty(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            duty.duty_session, "get_open_duty", return_value=DUTY_ROW
        ), patch.object(
            duty.duty_session, "summarize_duty", return_value={"stops_delivered": 3}
        ):
            result = duty.get_duty_summary()

        self.assertEqual(3, result["summary"]["stops_delivered"])

    def test_a_courier_cannot_read_another_couriers_duty(self) -> None:
        other = dict(DUTY_ROW, party="HR-EMP-99999")
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            duty.frappe.db, "get_value", return_value=other
        ):
            with self.assertRaises(frappe.PermissionError):
                duty.get_duty_summary("CDUTY-00009")

    def test_a_supervisor_still_needs_the_branch(self) -> None:
        """Supervisor roles are site-wide; branch assignment is not."""
        other = dict(DUTY_ROW, party="HR-EMP-99999", branch="Zamalek")
        with _roles(*_support.SUPERVISOR_ROLES), _identity(), patch.object(
            duty.frappe.db, "get_value", return_value=other
        ), patch.object(duty.pos_bridge, "get_user_pos_profiles", return_value=["Dokki"]):
            with self.assertRaises(frappe.PermissionError):
                duty.get_duty_summary("CDUTY-00009")

    def test_a_supervisor_on_the_same_branch_may_read_it(self) -> None:
        other = dict(DUTY_ROW, party="HR-EMP-99999")
        with _roles(*_support.SUPERVISOR_ROLES), _identity(), patch.object(
            duty.frappe.db, "get_value", return_value=other
        ), patch.object(
            duty.pos_bridge, "get_user_pos_profiles", return_value=["Dokki", "Zamalek"]
        ), patch.object(duty.duty_session, "summarize_duty", return_value={"stops_delivered": 1}):
            result = duty.get_duty_summary("CDUTY-00009")

        self.assertTrue(result["success"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

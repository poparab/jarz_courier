"""``api/run`` — the run sheet and the three delivery outcome endpoints.

Two things carry the weight here.

**Delegation.** ``mark_arrived`` / ``mark_delivered`` / ``mark_failed`` must call
``jarz_pos.services.courier_delivery.mark_invoice_*`` and do nothing else with the
invoice. Those functions own the meta assertion, the dual idempotency token, the
``update_submitted_sales_invoice_fields`` write path, the access gate, the feature
flag and the realtime publish (COURIER_CONTRACTS.md §5). A test that only checked
"returns success" would pass over a local reimplementation, so these assert on the
forwarded arguments.

**Two gates, not one.** Branch scoping alone is insufficient: every courier on a
branch shares it, so a branch-only check lets one courier mark another's stop
delivered. The assignment check is what makes a stop yours.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import run  # noqa: E402

MY_INVOICE = {
    "name": "ACC-SINV-2026-00042",
    "custom_kanban_profile": "Dokki",
    "pos_profile": "Dokki",
    "custom_courier_party_type": "Employee",
    "custom_courier_party": "HR-EMP-00042",
}

SOMEONE_ELSES_INVOICE = dict(MY_INVOICE, custom_courier_party="HR-EMP-99999")


def _roles(*names: str):
    return patch.object(run.frappe, "get_roles", return_value=list(names))


def _identity():
    return patch.object(
        run.courier_onboarding, "ensure_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
    )


def _branch():
    return patch.object(
        run.courier_onboarding, "resolve_active_branch", return_value=dict(_support.COURIER_IDENTITY)
    )


def _stop_is(row):
    return patch.object(run.frappe.db, "get_value", return_value=row)


class TestPermissionGate(unittest.TestCase):
    def test_no_courier_role_is_rejected_on_every_endpoint(self) -> None:
        with _roles(*_support.NO_ROLES):
            with self.assertRaises(frappe.PermissionError):
                run.get_my_run()
            with self.assertRaises(frappe.PermissionError):
                run.get_stop_detail("ACC-SINV-2026-00042")
            with self.assertRaises(frappe.PermissionError):
                run.mark_arrived("ACC-SINV-2026-00042")
            with self.assertRaises(frappe.PermissionError):
                run.mark_delivered("ACC-SINV-2026-00042")
            with self.assertRaises(frappe.PermissionError):
                run.mark_failed("ACC-SINV-2026-00042", "CUSTOMER_UNREACHABLE")


class TestGetMyRun(unittest.TestCase):
    def test_queries_the_couriers_own_party_and_branch(self) -> None:
        with _roles(*_support.COURIER_ROLES), _branch(), patch.object(
            run.run_sheet, "get_run", return_value={"stops": [], "totals": {}, "branches": ["Dokki"]}
        ) as get_run:
            result = run.get_my_run()

        self.assertTrue(result["success"])
        kwargs = get_run.call_args.kwargs
        self.assertEqual("Employee", kwargs["party_type"])
        self.assertEqual("HR-EMP-00042", kwargs["party"])
        self.assertEqual(["Dokki"], kwargs["branches"])
        self.assertEqual("Out for Delivery", kwargs["state"])

    def test_returns_the_courier_block_so_the_client_can_label_the_screen(self) -> None:
        with _roles(*_support.COURIER_ROLES), _branch(), patch.object(
            run.run_sheet, "get_run", return_value={"stops": [], "totals": {}, "branches": []}
        ):
            result = run.get_my_run()

        self.assertEqual("Mahmoud", result["courier"]["display_name"])
        self.assertEqual("Dokki", result["courier"]["branch"])

    def test_a_broken_setup_surfaces_as_an_error_not_an_empty_run(self) -> None:
        """An empty list and a mis-wired account look identical on screen."""
        with _roles(*_support.COURIER_ROLES), patch.object(
            run.courier_onboarding,
            "resolve_active_branch",
            side_effect=RuntimeError("Employee has no Branch"),
        ), patch.object(run.frappe, "log_error"):
            result = run.get_my_run()

        self.assertFalse(result["success"])
        self.assertIn("Branch", result["error"])


class TestStopAccess(unittest.TestCase):
    def test_a_stop_assigned_to_another_courier_is_refused(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), _stop_is(
            SOMEONE_ELSES_INVOICE
        ), patch.object(run.pos_bridge, "ensure_profile_scoped_invoice_access"):
            with self.assertRaises(frappe.PermissionError):
                run._assert_stop_access("ACC-SINV-2026-00042", action_label="test")

    def test_branch_scoping_is_checked_through_jarz_pos(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), _stop_is(MY_INVOICE), patch.object(
            run.pos_bridge, "ensure_profile_scoped_invoice_access"
        ) as scope:
            run._assert_stop_access("ACC-SINV-2026-00042", action_label="test")

        self.assertEqual(MY_INVOICE, scope.call_args.args[0])

    def test_a_supervisor_may_act_on_any_stop_on_their_branch(self) -> None:
        with _roles(*_support.SUPERVISOR_ROLES), _identity(), _stop_is(
            SOMEONE_ELSES_INVOICE
        ), patch.object(run.pos_bridge, "ensure_profile_scoped_invoice_access"):
            row = run._assert_stop_access("ACC-SINV-2026-00042", action_label="test")

        self.assertEqual("ACC-SINV-2026-00042", row["name"])

    def test_an_unknown_invoice_is_rejected(self) -> None:
        with _roles(*_support.COURIER_ROLES), _stop_is(None):
            with self.assertRaises(frappe.ValidationError):
                run._assert_stop_access("NOPE", action_label="test")


class TestDelegationToJarzPos(unittest.TestCase):
    """Every write goes to jarz_pos lane A3 — arguments included."""

    def _allow(self):
        return patch.object(run, "_assert_stop_access", return_value=MY_INVOICE)

    def test_mark_arrived_forwards_the_frozen_signature(self) -> None:
        with _roles(*_support.COURIER_ROLES), self._allow(), patch.object(
            run.pos_bridge, "mark_invoice_arrived", return_value={"success": True}
        ) as marker:
            run.mark_arrived("ACC-SINV-2026-00042", latitude="30.05", longitude="31.2", request_id="r1")

        args, kwargs = marker.call_args
        self.assertEqual("ACC-SINV-2026-00042", args[0])
        self.assertAlmostEqual(30.05, kwargs["latitude"])
        self.assertAlmostEqual(31.2, kwargs["longitude"])
        self.assertEqual("r1", kwargs["request_id"])

    def test_mark_delivered_forwards_collection_and_recipient(self) -> None:
        with _roles(*_support.COURIER_ROLES), self._allow(), patch.object(
            run.pos_bridge, "mark_invoice_delivered", return_value={"success": True}
        ) as marker:
            run.mark_delivered(
                "ACC-SINV-2026-00042",
                collected_amount="450.5",
                recipient_name="Nour",
                is_mocked="true",
                request_id="r2",
            )

        kwargs = marker.call_args.kwargs
        self.assertAlmostEqual(450.5, kwargs["collected_amount"])
        self.assertEqual("Nour", kwargs["recipient_name"])
        self.assertIs(True, kwargs["is_mocked"])
        self.assertEqual("r2", kwargs["request_id"])

    def test_mark_failed_requires_and_forwards_the_reason(self) -> None:
        with _roles(*_support.COURIER_ROLES), self._allow(), patch.object(
            run.pos_bridge, "mark_invoice_failed", return_value={"success": True}
        ) as marker:
            run.mark_failed("ACC-SINV-2026-00042", "CUSTOMER_UNREACHABLE", notes="no answer")

        kwargs = marker.call_args.kwargs
        self.assertEqual("CUSTOMER_UNREACHABLE", kwargs["failure_reason"])
        self.assertEqual("no answer", kwargs["notes"])

    def test_the_envelope_from_jarz_pos_is_returned_unchanged(self) -> None:
        """The service owns the envelope; this layer must not rewrap it."""
        envelope = {"success": True, "state": "Delivered", "idempotent_replay": True}
        with _roles(*_support.COURIER_ROLES), self._allow(), patch.object(
            run.pos_bridge, "mark_invoice_delivered", return_value=envelope
        ):
            result = run.mark_delivered("ACC-SINV-2026-00042")

        self.assertEqual(envelope, result)

    def test_permission_error_from_the_service_is_not_flattened(self) -> None:
        with _roles(*_support.COURIER_ROLES), self._allow(), patch.object(
            run.pos_bridge, "mark_invoice_delivered", side_effect=frappe.PermissionError("no")
        ):
            with self.assertRaises(frappe.PermissionError):
                run.mark_delivered("ACC-SINV-2026-00042")

    def test_service_unavailable_returns_an_envelope(self) -> None:
        """jarz_pos a deploy behind must not 500 a courier at a door."""
        with _roles(*_support.COURIER_ROLES), self._allow(), patch.object(
            run.pos_bridge,
            "mark_invoice_delivered",
            side_effect=RuntimeError("courier_delivery is not available on this server yet"),
        ), patch.object(run.frappe, "log_error"):
            result = run.mark_delivered("ACC-SINV-2026-00042")

        self.assertFalse(result["success"])
        self.assertIn("not available", result["error"])


class TestCoercion(unittest.TestCase):
    """Numbers and booleans arrive as strings over HTTP form encoding."""

    def test_float_coercion(self) -> None:
        self.assertIsNone(run._as_float(None))
        self.assertIsNone(run._as_float(""))
        self.assertIsNone(run._as_float("not a number"))
        self.assertAlmostEqual(30.123456, run._as_float("30.123456"))

    def test_bool_coercion(self) -> None:
        for truthy in (True, 1, "1", "true", "TRUE", "yes", "on"):
            self.assertTrue(run._as_bool(truthy), truthy)
        for falsy in (False, 0, "0", "false", "", None, "no"):
            self.assertFalse(run._as_bool(falsy), falsy)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

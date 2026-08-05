"""``api/device`` — device binding transport.

Asserts the three properties the ``api/returns.py`` template exists to guarantee,
on every endpoint: the permission check runs first, a ``frappe.PermissionError``
propagates as a real 403 instead of being flattened into ``{"success": False}``,
and any other failure comes back as an envelope rather than a 500.

Plus the one rule specific to this module: unbinding someone else's handset is a
supervisor action, because "sign me out of this phone" and "free a courier whose
phone was stolen" are different powers.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import device  # noqa: E402

DEVICE_ROW = {
    "name": "CDEV-00001",
    "party_type": "Employee",
    "party": "HR-EMP-00042",
    "device_id": "abc-123",
    "is_active": 1,
}


def _roles(*names: str):
    return patch.object(device.frappe, "get_roles", return_value=list(names))


def _identity():
    return patch.object(
        device.courier_onboarding, "ensure_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
    )


class TestPermissionGate(unittest.TestCase):
    def test_register_device_rejects_a_user_with_no_courier_role(self) -> None:
        with _roles(*_support.NO_ROLES):
            with self.assertRaises(frappe.PermissionError):
                device.register_device("abc-123")

    def test_get_my_device_rejects_a_user_with_no_courier_role(self) -> None:
        with _roles("Sales User"):
            with self.assertRaises(frappe.PermissionError):
                device.get_my_device()

    def test_unbind_device_rejects_a_user_with_no_courier_role(self) -> None:
        with _roles(*_support.NO_ROLES):
            with self.assertRaises(frappe.PermissionError):
                device.unbind_device()

    def test_permission_check_runs_before_any_work(self) -> None:
        """The gate is the first statement; nothing is resolved for a denied caller."""
        with _roles(*_support.NO_ROLES), patch.object(
            device.courier_onboarding, "ensure_courier_setup"
        ) as setup:
            with self.assertRaises(frappe.PermissionError):
                device.register_device("abc-123")
        setup.assert_not_called()


class TestRegisterDevice(unittest.TestCase):
    def test_forwards_identity_and_device_metadata(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            device.device_registry,
            "register_device",
            return_value={"device": DEVICE_ROW, "created": True, "rebound": False},
        ) as register:
            result = device.register_device(
                "abc-123", fcm_token="tok", app_version="1.2.0", os_version="14"
            )

        self.assertTrue(result["success"])
        self.assertEqual("Dokki", result["branch"])
        kwargs = register.call_args.kwargs
        # Identity comes from the server session, never from the client.
        self.assertEqual("Employee", kwargs["party_type"])
        self.assertEqual("HR-EMP-00042", kwargs["party"])
        self.assertEqual("abc-123", kwargs["device_id"])
        self.assertEqual("tok", kwargs["fcm_token"])

    def test_permission_error_from_setup_is_not_flattened(self) -> None:
        with _roles(*_support.COURIER_ROLES), patch.object(
            device.courier_onboarding,
            "ensure_courier_setup",
            side_effect=frappe.PermissionError("nope"),
        ):
            with self.assertRaises(frappe.PermissionError):
                device.register_device("abc-123")

    def test_unexpected_failure_returns_an_envelope(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            device.device_registry, "register_device", side_effect=RuntimeError("db down")
        ), patch.object(device.frappe, "log_error"):
            result = device.register_device("abc-123")

        self.assertFalse(result["success"])
        self.assertIn("db down", result["error"])


class TestGetMyDevice(unittest.TestCase):
    def test_returns_the_setup_diagnosis_alongside_the_device(self) -> None:
        """First call after login — a mis-wired courier must learn it here."""
        broken = dict(_support.COURIER_IDENTITY, ok=False, problems=["no_pos_profile"], party="")
        with _roles(*_support.COURIER_ROLES), patch.object(
            device.courier_onboarding, "diagnose_courier_setup", return_value=broken
        ), patch.object(device.device_registry, "get_active_device") as get_active:
            result = device.get_my_device()

        self.assertTrue(result["success"])
        self.assertIsNone(result["device"])
        self.assertEqual(["no_pos_profile"], result["setup"]["problems"])
        get_active.assert_not_called()

    def test_returns_the_active_device_for_a_wired_courier(self) -> None:
        with _roles(*_support.COURIER_ROLES), patch.object(
            device.courier_onboarding,
            "diagnose_courier_setup",
            return_value=dict(_support.COURIER_IDENTITY),
        ), patch.object(device.device_registry, "get_active_device", return_value=DEVICE_ROW):
            result = device.get_my_device()

        self.assertEqual("CDEV-00001", result["device"]["name"])


class TestUnbindDevice(unittest.TestCase):
    def test_unbinding_own_active_device_needs_only_the_courier_role(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            device.device_registry, "get_active_device", return_value=DEVICE_ROW
        ), patch.object(
            device.device_registry, "unbind_device", return_value={"device": DEVICE_ROW, "changed": True}
        ) as unbind:
            result = device.unbind_device()

        self.assertTrue(result["success"])
        self.assertTrue(result["changed"])
        self.assertEqual("CDEV-00001", unbind.call_args.kwargs["name"])

    def test_unbinding_another_couriers_device_requires_a_supervisor(self) -> None:
        other = {"party_type": "Employee", "party": "HR-EMP-99999"}
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            device.frappe.db, "get_value", return_value=other
        ), patch.object(device.device_registry, "unbind_device") as unbind:
            with self.assertRaises(frappe.PermissionError):
                device.unbind_device("CDEV-00002")
        unbind.assert_not_called()

    def test_a_supervisor_may_unbind_another_couriers_device(self) -> None:
        other = {"party_type": "Employee", "party": "HR-EMP-99999"}
        with _roles(*_support.SUPERVISOR_ROLES), _identity(), patch.object(
            device.frappe.db, "get_value", return_value=other
        ), patch.object(
            device.device_registry, "unbind_device", return_value={"device": other, "changed": True}
        ) as unbind:
            result = device.unbind_device("CDEV-00002")

        self.assertTrue(result["success"])
        self.assertEqual("CDEV-00002", unbind.call_args.kwargs["name"])

    def test_no_active_device_is_a_no_op_not_an_error(self) -> None:
        """The offline queue replays sign-out; the second attempt must not fail."""
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            device.device_registry, "get_active_device", return_value=None
        ):
            result = device.unbind_device()

        self.assertTrue(result["success"])
        self.assertFalse(result["changed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

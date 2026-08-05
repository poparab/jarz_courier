"""``api/pod`` — proof of delivery, and the offline-queue tolerances it promises.

The offline queue replays with ``maxReplayAttempts = 3``, so the tests that matter
are the retry ones:

* the same ``request_id`` twice yields one proof, not two;
* a proof with no file is accepted, and a later call with the same key attaches the
  photo to the row that already exists;
* a file that already landed is never overwritten by a retry — the first successful
  upload is the one taken at the door;
* the handset's ``captured_at`` survives; it is not replaced by the server clock,
  which would date a morning of queued deliveries to the moment the courier walked
  past a router.

Also pinned: capturing a proof never marks the stop delivered. That is a separate
call into jarz_pos, so a failed photo upload cannot block a completed delivery.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import pod  # noqa: E402
from jarz_courier.services import proof_of_delivery  # noqa: E402

MY_INVOICE = {
    "name": "ACC-SINV-2026-00042",
    "custom_kanban_profile": "Dokki",
    "pos_profile": "Dokki",
    "custom_courier_party_type": "Employee",
    "custom_courier_party": "HR-EMP-00042",
}

SOMEONE_ELSES_INVOICE = dict(MY_INVOICE, custom_courier_party="HR-EMP-99999")

STORED_PROOF = {
    "name": "DPRF-00001",
    "sales_invoice": "ACC-SINV-2026-00042",
    "proof_type": "Photo",
    "file": None,
    "captured_at": "2026-08-05 09:14:00",
    "request_id": "req-1",
    "notes": None,
}


def _roles(*names: str):
    return patch.object(pod.frappe, "get_roles", return_value=list(names))


def _identity():
    return patch.object(
        pod.courier_onboarding, "ensure_courier_setup", return_value=dict(_support.COURIER_IDENTITY)
    )


class TestPermissionGate(unittest.TestCase):
    def test_no_courier_role_is_rejected(self) -> None:
        with _roles(*_support.NO_ROLES):
            with self.assertRaises(frappe.PermissionError):
                pod.upload_proof("ACC-SINV-2026-00042", "Photo")
            with self.assertRaises(frappe.PermissionError):
                pod.get_proofs("ACC-SINV-2026-00042")

    def test_a_stop_assigned_to_another_courier_is_refused(self) -> None:
        """The access helper throws inside the try block; it must still be a 403.

        This is precisely what ``except frappe.PermissionError: raise`` before the
        generic handler is for — without it, "not your stop" would come back as a
        ``{"success": False}`` envelope that the client treats as retryable.
        """
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            pod.frappe.db, "get_value", return_value=SOMEONE_ELSES_INVOICE
        ), patch.object(pod.pos_bridge, "ensure_profile_scoped_invoice_access"), patch.object(
            pod.proof_of_delivery, "record_proof"
        ) as record:
            with self.assertRaises(frappe.PermissionError):
                pod.upload_proof("ACC-SINV-2026-00042", "Photo")

        record.assert_not_called()

    def test_a_supervisor_may_attach_a_proof_to_any_stop_on_their_branch(self) -> None:
        with _roles(*_support.SUPERVISOR_ROLES), _identity(), patch.object(
            pod.frappe.db, "get_value", return_value=SOMEONE_ELSES_INVOICE
        ), patch.object(pod.pos_bridge, "ensure_profile_scoped_invoice_access"), patch.object(
            pod.duty_session, "get_open_duty", return_value=None
        ), patch.object(
            pod.proof_of_delivery,
            "record_proof",
            return_value={"proof": STORED_PROOF, "created": True, "updated": False},
        ):
            result = pod.upload_proof("ACC-SINV-2026-00042", "Photo")

        self.assertTrue(result["success"])


class TestUploadProofTransport(unittest.TestCase):
    def test_identity_and_open_duty_are_resolved_server_side(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            pod, "_assert_stop_access", return_value=MY_INVOICE
        ), patch.object(
            pod.duty_session, "get_open_duty", return_value={"name": "CDUTY-00001"}
        ), patch.object(
            pod.proof_of_delivery,
            "record_proof",
            return_value={"proof": STORED_PROOF, "created": True, "updated": False},
        ) as record:
            result = pod.upload_proof(
                "ACC-SINV-2026-00042",
                "photo",
                captured_at="2026-08-05 09:14:00",
                latitude="30.05",
                request_id="req-1",
            )

        self.assertTrue(result["success"])
        kwargs = record.call_args.kwargs
        self.assertEqual("Employee", kwargs["party_type"])
        self.assertEqual("HR-EMP-00042", kwargs["party"])
        self.assertEqual("CDUTY-00001", kwargs["duty"])
        # The handset's timestamp is passed through untouched.
        self.assertEqual("2026-08-05 09:14:00", kwargs["captured_at"])
        self.assertAlmostEqual(30.05, kwargs["latitude"])

    def test_permission_error_is_not_flattened(self) -> None:
        with _roles(*_support.COURIER_ROLES), patch.object(
            pod.courier_onboarding,
            "ensure_courier_setup",
            side_effect=frappe.PermissionError("nope"),
        ):
            with self.assertRaises(frappe.PermissionError):
                pod.upload_proof("ACC-SINV-2026-00042", "Photo")

    def test_unexpected_failure_returns_an_envelope(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            pod, "_assert_stop_access", return_value=MY_INVOICE
        ), patch.object(pod.duty_session, "get_open_duty", return_value=None), patch.object(
            pod.proof_of_delivery, "record_proof", side_effect=RuntimeError("disk full")
        ), patch.object(pod.frappe, "log_error"):
            result = pod.upload_proof("ACC-SINV-2026-00042", "Photo")

        self.assertFalse(result["success"])
        self.assertIn("disk full", result["error"])

    def test_capturing_a_proof_never_marks_the_stop_delivered(self) -> None:
        """POD and the delivery transition are deliberately decoupled."""
        self.assertFalse(hasattr(pod, "mark_delivered"))
        source = (
            __import__("pathlib").Path(pod.__file__).with_suffix(".py").read_text(encoding="utf-8")
        )
        self.assertNotIn("mark_invoice_delivered", source)


class TestOfflineRetrySemantics(unittest.TestCase):
    """Exercised on the service, which is where the idempotency actually lives."""

    def test_a_replayed_request_id_returns_the_stored_proof(self) -> None:
        with patch.object(proof_of_delivery, "find_by_request_id", return_value=dict(STORED_PROOF)):
            result = proof_of_delivery.record_proof(
                sales_invoice="ACC-SINV-2026-00042",
                proof_type="Photo",
                party_type="Employee",
                party="HR-EMP-00042",
                request_id="req-1",
            )

        self.assertFalse(result["created"])
        self.assertEqual("DPRF-00001", result["proof"]["name"])

    def test_a_late_file_is_attached_to_the_existing_proof(self) -> None:
        with patch.object(
            proof_of_delivery, "find_by_request_id", return_value=dict(STORED_PROOF)
        ), patch.object(proof_of_delivery.frappe.db, "set_value") as set_value:
            result = proof_of_delivery.record_proof(
                sales_invoice="ACC-SINV-2026-00042",
                proof_type="Photo",
                party_type="Employee",
                party="HR-EMP-00042",
                file_url="/files/pod.jpg",
                request_id="req-1",
            )

        self.assertTrue(result["updated"])
        self.assertFalse(result["created"])
        self.assertEqual({"file": "/files/pod.jpg"}, set_value.call_args.args[2])
        self.assertEqual("/files/pod.jpg", result["proof"]["file"])

    def test_a_retry_never_overwrites_a_file_that_already_landed(self) -> None:
        stored = dict(STORED_PROOF, file="/files/original.jpg")
        with patch.object(
            proof_of_delivery, "find_by_request_id", return_value=stored
        ), patch.object(proof_of_delivery.frappe.db, "set_value") as set_value:
            result = proof_of_delivery.record_proof(
                sales_invoice="ACC-SINV-2026-00042",
                proof_type="Photo",
                party_type="Employee",
                party="HR-EMP-00042",
                file_url="/files/retry.jpg",
                request_id="req-1",
            )

        set_value.assert_not_called()
        self.assertFalse(result["updated"])
        self.assertEqual("/files/original.jpg", result["proof"]["file"])

    def test_proof_type_accepts_the_clients_spellings(self) -> None:
        self.assertEqual("Photo", proof_of_delivery.normalize_proof_type("photo"))
        self.assertEqual("Signature", proof_of_delivery.normalize_proof_type("sign"))
        self.assertEqual("OTP", proof_of_delivery.normalize_proof_type("otp"))

    def test_an_unknown_proof_type_is_rejected(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            proof_of_delivery.normalize_proof_type("selfie")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

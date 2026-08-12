"""Whitelisted endpoints for proof-of-delivery capture.

Built for an offline queue, so the contract with the client is unusually tolerant:

* ``request_id`` makes every upload idempotent. A queue entry replayed after a
  timeout returns the stored proof with ``created: False`` instead of a duplicate.
* A proof with no ``file_url`` is accepted. On a bad connection the 200-byte
  metadata POST succeeds long before a 3 MB photo does; rejecting the metadata
  would mean the only evidence that survives comes from couriers with good signal.
* A later call carrying the same ``request_id`` and a ``file_url`` attaches the
  image to the row that already exists, and never overwrites a file that landed.

Capturing a proof does **not** mark the stop delivered. That is a separate call to
``api/run.mark_delivered`` → ``jarz_pos.services.courier_delivery`` — deliberately,
because that one moves money and this one does not, and coupling them would make a
failed photo upload block a completed delivery.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_courier.constants import QUERY_LIMITS, ROLES
from jarz_courier.services import courier_onboarding, duty_session, pos_bridge, proof_of_delivery


def _ensure_pod_permission() -> None:
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SELF | ROLES.COURIER_SUPERVISOR):
        frappe.throw(_("You are not permitted to record proof of delivery"), frappe.PermissionError)


@frappe.whitelist(allow_guest=False)
def upload_proof(
    sales_invoice: str,
    proof_type: str,
    file_url: Optional[str] = None,
    recipient_name: Optional[str] = None,
    captured_at: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    is_mocked: Any = False,
    capture_platform: Optional[str] = None,
    request_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Record (or complete) one proof of delivery.

    ``captured_at`` is the handset's timestamp and is stored as given. Overwriting
    it with the server clock would date a morning of queued deliveries to the
    moment the courier walked past a router.
    """
    _ensure_pod_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="recording proof of delivery")
        _assert_stop_access(sales_invoice, identity, action_label="recording proof of delivery")

        open_duty = duty_session.get_open_duty(identity["party_type"], identity["party"])

        result = proof_of_delivery.record_proof(
            sales_invoice=sales_invoice,
            proof_type=proof_type,
            party_type=identity["party_type"],
            party=identity["party"],
            file_url=file_url,
            recipient_name=recipient_name,
            captured_at=captured_at,
            latitude=_as_float(latitude),
            longitude=_as_float(longitude),
            accuracy_m=_as_float(accuracy_m),
            is_mocked=is_mocked,
            capture_platform=capture_platform,
            duty=(open_duty or {}).get("name"),
            request_id=request_id,
            notes=notes,
        )
        return {"success": True, **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier upload_proof failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_proofs(sales_invoice: str, limit: int = QUERY_LIMITS.PROOFS_PER_STOP) -> Dict[str, Any]:
    """Proofs already stored for a stop. Read-only.

    Used by the client to reconcile its offline queue after a reinstall: anything
    the server already has can be dropped from the local queue.
    """
    _ensure_pod_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="viewing proof of delivery")
        _assert_stop_access(sales_invoice, identity, action_label="viewing proof of delivery")
        return {
            "success": True,
            "proofs": proof_of_delivery.list_proofs(sales_invoice, limit=limit),
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_proofs failed")
        return {"success": False, "error": str(exc)}


def _assert_stop_access(
    sales_invoice: str, identity: Dict[str, Any], *, action_label: str
) -> Dict[str, Any]:
    """Branch scoping plus "this order is assigned to you".

    Same two gates as ``api/run._assert_stop_access``, and for the same reason:
    branch scoping alone would let any courier on a branch attach a photo to any
    other courier's delivery.
    """
    name = str(sales_invoice or "").strip()
    if not name:
        frappe.throw(_("Sales Invoice is required"))

    row = frappe.db.get_value(
        "Sales Invoice",
        name,
        [
            "name",
            "custom_kanban_profile",
            "pos_profile",
            "custom_courier_party_type",
            "custom_courier_party",
        ],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Sales Invoice {0} not found").format(name))

    pos_bridge.ensure_profile_scoped_invoice_access(row, action_label=action_label)

    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if not roles.isdisjoint(ROLES.COURIER_SUPERVISOR):
        return row

    if (
        row.get("custom_courier_party_type") != identity["party_type"]
        or row.get("custom_courier_party") != identity["party"]
    ):
        frappe.throw(
            _("Order {0} is not assigned to you").format(name),
            frappe.PermissionError,
            title=_("Not Your Stop"),
        )
    return row


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

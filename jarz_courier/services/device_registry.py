"""Device binding rules.

The invariant — one active ``Courier Device`` per courier — is enforced in the
doctype controller, not here, so a Desk edit obeys it too. This module owns the
*resolution* side: which row does a given (courier, device_id) pair correspond to,
and what does re-registering an already-known handset mean.

Re-registration is the common case, not the exception. The courier app calls
``register_device`` on every cold start so a rotated FCM token reaches the server;
treating that as "bind a new device" would create a row per app launch.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import now_datetime

from jarz_courier.constants import DOCTYPES

DOCTYPE = DOCTYPES.COURIER_DEVICE

#: Returned to the client. Deliberately excludes ``fcm_token`` — the client
#: already knows its own token and echoing a push credential back over the wire
#: buys nothing.
DEVICE_FIELDS = (
    "name",
    "party_type",
    "party",
    "user",
    "device_id",
    "app_version",
    "os_version",
    "device_model",
    "is_active",
    "bound_on",
    "unbound_on",
    "last_seen_on",
)


def find_device(party_type: str, party: str, device_id: str) -> Optional[str]:
    """The existing row for this handset under this courier, active or not."""
    if not (party_type and party and device_id):
        return None
    rows = frappe.get_all(
        DOCTYPE,
        filters={"party_type": party_type, "party": party, "device_id": device_id},
        pluck="name",
        order_by="creation desc",
        limit=1,
    ) or []
    return rows[0] if rows else None


def get_active_device(party_type: str, party: str) -> Optional[Dict[str, Any]]:
    """The courier's currently bound handset, or None."""
    if not (party_type and party):
        return None
    rows = frappe.get_all(
        DOCTYPE,
        filters={"party_type": party_type, "party": party, "is_active": 1},
        fields=list(DEVICE_FIELDS),
        order_by="bound_on desc, modified desc",
        limit=1,
    ) or []
    return rows[0] if rows else None


def register_device(
    *,
    party_type: str,
    party: str,
    device_id: str,
    user: Optional[str] = None,
    fcm_token: Optional[str] = None,
    app_version: Optional[str] = None,
    os_version: Optional[str] = None,
    device_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Bind *device_id* to the courier, or refresh the existing binding.

    Returns ``{"device": {...}, "created": bool, "rebound": bool}``. ``rebound``
    means the courier's active handset changed — the client shows a "you have been
    signed out on your other phone" notice, and it is the signal a supervisor
    watches for when a courier claims not to be receiving assignments.
    """
    device_id = str(device_id or "").strip()
    if not device_id:
        frappe.throw(_("Device ID is required"))

    previous_active = get_active_device(party_type, party)
    existing_name = find_device(party_type, party, device_id)

    if existing_name:
        doc = frappe.get_doc(DOCTYPE, existing_name)
        created = False
    else:
        doc = frappe.new_doc(DOCTYPE)
        doc.party_type = party_type
        doc.party = party
        doc.device_id = device_id
        created = True

    doc.user = user or frappe.session.user
    doc.is_active = 1
    doc.last_seen_on = now_datetime()
    if fcm_token is not None:
        doc.fcm_token = str(fcm_token).strip() or None
    if app_version is not None:
        doc.app_version = str(app_version).strip() or None
    if os_version is not None:
        doc.os_version = str(os_version).strip() or None
    if device_model is not None:
        doc.device_model = str(device_model).strip() or None

    doc.save(ignore_permissions=True)

    rebound = bool(previous_active and previous_active.get("name") != doc.name)

    return {
        "device": _as_payload(doc),
        "created": created,
        "rebound": rebound,
        "previous_device": (previous_active or {}).get("name") if rebound else None,
    }


def unbind_device(*, name: str) -> Dict[str, Any]:
    """Deactivate a device row. Kept, never deleted — it is the binding history."""
    doc = frappe.get_doc(DOCTYPE, name)
    if not doc.is_active:
        return {"device": _as_payload(doc), "changed": False}

    doc.is_active = 0
    doc.fcm_token = None  # a token on an unbound device is a push to a stranger
    doc.unbound_on = now_datetime()
    doc.save(ignore_permissions=True)
    return {"device": _as_payload(doc), "changed": True}


def touch_device(party_type: str, party: str) -> None:
    """Best-effort ``last_seen_on`` bump. Never raises, never blocks the caller."""
    try:
        active = get_active_device(party_type, party)
        if active:
            frappe.db.set_value(
                DOCTYPE, active["name"], "last_seen_on", now_datetime(), update_modified=False
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: touch_device failed")


def _as_payload(doc: Any) -> Dict[str, Any]:
    return {field: doc.get(field) for field in DEVICE_FIELDS}

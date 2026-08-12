"""Proof-of-delivery capture, built for an offline queue.

The courier app writes every POD into a Hive queue and flushes it whenever the
network allows, with ``maxReplayAttempts = 3``. That gives three properties this
module has to hold up under:

1. **The same proof arrives more than once.** Handled by ``request_id``: a unique
   client-generated key that the queue reuses on every retry. A repeat returns the
   stored proof and reports ``created: False`` rather than inserting a twin.
2. **The metadata arrives before the photo.** Wi-Fi drops mid-upload far more often
   than a 200-byte JSON POST fails. A proof with no ``file`` is accepted, and a
   later call carrying the same ``request_id`` plus a ``file_url`` attaches the
   image to the row that already exists.
3. **The capture time is not the receipt time.** ``captured_at`` comes from the
   handset. Defaulting it to ``now()`` server-side would date a whole morning's
   queued deliveries to the moment the courier walked past a router.

None of this touches invoice state. Marking the stop delivered is a separate call
into ``jarz_pos.services.courier_delivery.mark_invoice_delivered`` — deliberately
so, because that one moves money and this one does not.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from jarz_courier.constants import DOCTYPES, PROOF_TYPES

DOCTYPE = DOCTYPES.DELIVERY_PROOF

PROOF_FIELDS = (
    "name",
    "sales_invoice",
    "proof_type",
    "file",
    "recipient_name",
    "captured_at",
    "latitude",
    "longitude",
    "accuracy_m",
    "is_mocked",
    "party_type",
    "party",
    "duty",
    "request_id",
    "notes",
)

#: Accepted spellings from the client. The doctype stores the canonical form.
_PROOF_TYPE_ALIASES = {
    "photo": PROOF_TYPES.PHOTO,
    "signature": PROOF_TYPES.SIGNATURE,
    "sign": PROOF_TYPES.SIGNATURE,
    "otp": PROOF_TYPES.OTP,
}


def normalize_proof_type(value: Any) -> str:
    key = str(value or "").strip().lower()
    resolved = _PROOF_TYPE_ALIASES.get(key)
    if not resolved:
        frappe.throw(
            _("Proof type must be one of: {0}").format(", ".join(PROOF_TYPES.ALL))
        )
    return resolved


def find_by_request_id(request_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The proof a previous attempt with this key already created."""
    key = str(request_id or "").strip()
    if not key:
        return None
    rows = frappe.get_all(
        DOCTYPE, filters={"request_id": key}, fields=list(PROOF_FIELDS), limit=1
    ) or []
    return rows[0] if rows else None



#: The only capture platform that lacks mock-GPS evidence.
CAPTURE_PLATFORM_WEB = "web"
CAPTURE_PLATFORM_ANDROID = "android"


def normalize_capture_platform(value: Any) -> Optional[str]:
    """Coerce a client-supplied platform label to a stored Select value.

    Only the literal ``"web"`` downgrades a proof. **Anything else — including
    blank, unknown labels and every pre-existing row — reads as a native
    capture**, which is what keeps this change backwards-compatible: no historical
    proof silently loses the rank it was promoted under.

    Deliberately fails towards the *stricter* interpretation of the evidence
    (native, i.e. mock-checked) rather than the weaker one, because a client that
    lies here can only ever downgrade its own proof, never upgrade it.
    """
    label = str(value or "").strip().lower()
    return CAPTURE_PLATFORM_WEB if label == CAPTURE_PLATFORM_WEB else CAPTURE_PLATFORM_ANDROID


def record_proof(
    *,
    sales_invoice: str,
    proof_type: Any,
    party_type: str,
    party: str,
    file_url: Optional[str] = None,
    recipient_name: Optional[str] = None,
    captured_at: Any = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    is_mocked: Any = False,
    capture_platform: Optional[str] = None,
    duty: Optional[str] = None,
    request_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the proof, or complete the one a previous attempt already created.

    Returns ``{"proof": {...}, "created": bool, "updated": bool}``.
    """
    invoice = str(sales_invoice or "").strip()
    if not invoice:
        frappe.throw(_("Sales Invoice is required"))

    resolved_type = normalize_proof_type(proof_type)
    key = str(request_id or "").strip() or None

    existing = find_by_request_id(key) if key else None
    if existing:
        return _complete_existing(existing, file_url=file_url, notes=notes)

    doc = frappe.new_doc(DOCTYPE)
    doc.sales_invoice = invoice
    doc.proof_type = resolved_type
    doc.party_type = party_type
    doc.party = party
    doc.duty = duty
    doc.file = str(file_url or "").strip() or None
    doc.recipient_name = recipient_name
    doc.captured_at = _coerce_captured_at(captured_at)
    doc.latitude = latitude
    doc.longitude = longitude
    doc.accuracy_m = accuracy_m
    doc.is_mocked = 1 if _truthy(is_mocked) else 0
    doc.capture_platform = normalize_capture_platform(capture_platform)
    doc.request_id = key
    doc.notes = notes

    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # Two flushes of the same queue entry raced. The unique index on
        # request_id is the arbiter; the loser reads back the winner's row rather
        # than surfacing a duplicate-key error to a courier at a door.
        winner = find_by_request_id(key)
        if winner:
            return {"proof": winner, "created": False, "updated": False}
        raise

    return {"proof": _as_payload(doc), "created": True, "updated": False}


def _complete_existing(
    existing: Dict[str, Any], *, file_url: Optional[str], notes: Optional[str]
) -> Dict[str, Any]:
    """Attach a late-arriving file to a proof whose metadata already landed.

    Only ever *fills in* a blank. A retry must never overwrite a file that made it
    through — the first successful upload is the one taken at the door.
    """
    updates: Dict[str, Any] = {}
    if file_url and not existing.get("file"):
        updates["file"] = str(file_url).strip()
    if notes and not existing.get("notes"):
        updates["notes"] = notes

    if not updates:
        return {"proof": existing, "created": False, "updated": False}

    frappe.db.set_value(DOCTYPE, existing["name"], updates)
    merged = dict(existing)
    merged.update(updates)
    return {"proof": merged, "created": False, "updated": True}


def list_proofs(sales_invoice: str, limit: int = 20) -> list[Dict[str, Any]]:
    invoice = str(sales_invoice or "").strip()
    if not invoice:
        return []
    return frappe.get_all(
        DOCTYPE,
        filters={"sales_invoice": invoice},
        fields=list(PROOF_FIELDS),
        order_by="captured_at desc",
        limit=limit,
    ) or []


def _coerce_captured_at(value: Any) -> Any:
    """Trust the handset's timestamp; fall back to now only when absent."""
    if not value:
        return now_datetime()
    try:
        return get_datetime(value)
    except Exception:
        # An unparseable client timestamp is data loss either way. Recording the
        # server time is the lesser evil to rejecting the proof entirely.
        return now_datetime()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_payload(doc: Any) -> Dict[str, Any]:
    return {field: doc.get(field) for field in PROOF_FIELDS}

"""The courier's run sheet — a QUERY, not a document.

COURIER_APP_SPEC.md §3: the Sales Invoice is already the unit of work. The
courier is assigned on ``Sales Invoice.custom_courier_party``, and both the trip
path and the individual path write that same field, so the run sheet is simply::

    Sales Invoice
    WHERE custom_courier_party      = <me>
      AND custom_sales_invoice_state = 'Out for Delivery'
      AND custom_kanban_profile      IN <my branches>

No ``Courier Run`` doctype, no stop model, no refactor of ``Delivery Trip``. A
persistent work-unit document arrives in P2 only when the GPS polyline needs an
anchor — something a query genuinely cannot provide.

Two things this module must not do, and does not:

* **It does not write invoice state.** Arrived / Delivered / Failed all go through
  ``jarz_pos.services.courier_delivery.mark_invoice_*`` (contract §5), which owns
  the meta assertion, the dual idempotency, the access gate, the feature flag and
  the realtime publish.
* **It does not assume lane A1 has shipped.** The delivery-outcome fields are
  selected only when ``frappe.get_meta`` says they exist, mirroring how
  ``api/kanban.py`` guards its pickup fields. A hard-coded field list turns
  "jarz_pos is one deploy behind" into an SQL error on the courier's home screen.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import frappe
from frappe import _
from frappe.utils import flt

from jarz_courier.constants import DOCTYPES, INVOICE_STATE, QUERY_LIMITS

#: Fields that exist on every ERPNext Sales Invoice plus jarz_pos fixtures that
#: predate this project. Safe to select unconditionally.
_BASE_FIELDS = (
    "name",
    "customer",
    "customer_name",
    "posting_date",
    "posting_time",
    "grand_total",
    "outstanding_amount",
    "status",
    "docstatus",
    "territory",
    "pos_profile",
    "custom_kanban_profile",
    "custom_sales_invoice_state",
    "custom_courier_party_type",
    "custom_courier_party",
    "shipping_address_name",
    "customer_address",
    "remarks",
)

#: Selected only when present. `custom_delivery_*` / `custom_arrived_at` /
#: `custom_delivered_at` are jarz_pos lane A1 (contract §2); `custom_sub_territory`,
#: `woo_order_id` and the delivery-slot fields predate this project but are not
#: guaranteed on every site.
_OPTIONAL_FIELDS = (
    "custom_delivery_sequence",
    "custom_arrived_at",
    "custom_delivered_at",
    "custom_delivery_latitude",
    "custom_delivery_longitude",
    "custom_delivery_accuracy_m",
    "custom_delivery_attempt_no",
    "custom_delivery_failure_reason",
    "custom_sub_territory",
    "custom_delivery_date",
    "custom_delivery_time_from",
    "custom_delivery_duration",
    "custom_payment_method",
    "custom_payment_confirmation_status",
    "woo_order_id",
)


def available_invoice_fields() -> List[str]:
    """``_BASE_FIELDS`` plus whichever optional fields this site actually has."""
    fields = list(_BASE_FIELDS)
    try:
        meta = frappe.get_meta("Sales Invoice")
    except Exception:
        return fields

    for fieldname in _OPTIONAL_FIELDS:
        try:
            if meta.get_field(fieldname):
                fields.append(fieldname)
        except Exception:
            continue
    return fields


def get_run(
    *,
    party_type: str,
    party: str,
    branches: Sequence[str],
    state: str = INVOICE_STATE.OUT_FOR_DELIVERY,
    limit: int = QUERY_LIMITS.RUN_STOPS,
) -> Dict[str, Any]:
    """Every stop currently assigned to this courier, in run order.

    ``branches`` is the caller's validated POS Profile list — passing it in rather
    than resolving it here keeps the branch decision in one place
    (``services.courier_onboarding``) and makes this function trivially testable.
    An empty ``branches`` returns an empty run rather than an unscoped query: a
    missing branch must never widen the result set.
    """
    branch_list = [str(b).strip() for b in (branches or []) if str(b or "").strip()]
    if not branch_list or not party_type or not party:
        return {"stops": [], "totals": _totals([]), "branches": branch_list}

    fields = available_invoice_fields()
    filters = {
        "docstatus": 1,
        "custom_courier_party_type": party_type,
        "custom_courier_party": party,
        "custom_sales_invoice_state": state,
        "custom_kanban_profile": ["in", branch_list],
    }

    order_by = "posting_date asc, creation asc"
    if "custom_delivery_sequence" in fields:
        # 0 means "unsequenced" (contract §2), which must sort last, not first.
        order_by = (
            "CASE WHEN IFNULL(custom_delivery_sequence, 0) = 0 THEN 1 ELSE 0 END asc, "
            "custom_delivery_sequence asc, creation asc"
        )

    rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=fields,
        order_by=order_by,
        limit=limit,
    ) or []

    address_map = _load_addresses(rows)
    stops = [_stop_summary(row, address_map) for row in rows]

    return {"stops": stops, "totals": _totals(stops), "branches": branch_list}


def get_stop(*, invoice_id: str) -> Dict[str, Any]:
    """Full detail for one stop, including proofs already captured.

    Branch and courier authorisation are the caller's job — ``api/run.py`` runs
    ``pos_bridge.ensure_profile_scoped_invoice_access`` before calling this, and a
    courier-identity check on top of it.
    """
    name = str(invoice_id or "").strip()
    if not name:
        frappe.throw(_("Invoice is required"))

    fields = available_invoice_fields()
    rows = frappe.get_all("Sales Invoice", filters={"name": name}, fields=fields, limit=1) or []
    if not rows:
        frappe.throw(_("Sales Invoice {0} not found").format(name))

    row = rows[0]
    address_map = _load_addresses([row])
    stop = _stop_summary(row, address_map)
    stop["items"] = _item_summary(name)
    stop["notes"] = _invoice_notes(name)
    stop["proofs"] = _proofs(name)
    return stop


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _stop_summary(row: Dict[str, Any], address_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    address_name = row.get("shipping_address_name") or row.get("customer_address") or ""
    address = address_map.get(address_name, {})

    return {
        "invoice": row.get("name"),
        # The Woo number is what the customer, the call centre and every screen
        # call this order. The ERPNext name is internal.
        "display_id": _display_id(row),
        "customer": row.get("customer"),
        "customer_name": row.get("customer_name"),
        "phone": address.get("phone") or _customer_phone(row.get("customer")),
        "state": row.get("custom_sales_invoice_state"),
        "branch": row.get("custom_kanban_profile") or row.get("pos_profile"),
        "sequence": int(row.get("custom_delivery_sequence") or 0),
        "territory": row.get("custom_sub_territory") or row.get("territory") or "",
        "address": {
            "name": address_name,
            "line1": address.get("address_line1") or "",
            "line2": address.get("address_line2") or "",
            "city": address.get("city") or "",
            "latitude": address.get("latitude"),
            "longitude": address.get("longitude"),
            "geo_source": address.get("geo_source") or "",
            "geo_confidence": address.get("geo_confidence"),
        },
        "amount_to_collect": flt(row.get("outstanding_amount")),
        "grand_total": flt(row.get("grand_total")),
        "payment_method": row.get("custom_payment_method") or "",
        "payment_confirmation": row.get("custom_payment_confirmation_status") or "",
        "slot": {
            "date": row.get("custom_delivery_date"),
            "from": row.get("custom_delivery_time_from"),
            "duration": row.get("custom_delivery_duration"),
        },
        "arrived_at": row.get("custom_arrived_at"),
        "delivered_at": row.get("custom_delivered_at"),
        "attempt_no": int(row.get("custom_delivery_attempt_no") or 0),
        "failure_reason": row.get("custom_delivery_failure_reason") or "",
        "remarks": row.get("remarks") or "",
    }


def _totals(stops: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "stops": len(stops),
        "to_collect": sum(flt(s.get("amount_to_collect")) for s in stops),
        "failed_attempts": sum(1 for s in stops if int(s.get("attempt_no") or 0) > 0),
    }


def _display_id(row: Dict[str, Any]) -> str:
    """The WooCommerce order number when there is one, else the ERPNext name.

    The Int field's ``0`` is not an id — it is the column default for orders that
    never came from Woo, and rendering it produces a screen full of "#0".
    """
    raw = row.get("woo_order_id")
    try:
        value = int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        value = 0
    return str(value) if value > 0 else str(row.get("name") or "")


def _load_addresses(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Batch-load the addresses for a run in one query.

    Geo fields are jarz_pos lane A4 (contract §3) and are read here, never written
    — ``services/geo_resolution.py`` on the jarz_pos side is their sole writer.
    ``address_line2`` is read as-is and never rewritten: it carries the
    ``"Location: <url>"`` maps link, and it is part of the Woo address-dedup
    signature and outbound-push trigger set (spec §3.4).
    """
    names = sorted(
        {
            str(r.get("shipping_address_name") or r.get("customer_address") or "").strip()
            for r in rows
        }
        - {""}
    )
    if not names:
        return {}

    fields = ["name", "address_line1", "address_line2", "city", "phone"]
    for optional in ("custom_latitude", "custom_longitude", "custom_geo_source", "custom_geo_confidence"):
        try:
            if frappe.get_meta("Address").get_field(optional):
                fields.append(optional)
        except Exception:
            continue

    try:
        rows_out = frappe.get_all("Address", filters={"name": ["in", names]}, fields=fields) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: address batch load failed")
        return {}

    return {
        r["name"]: {
            "address_line1": r.get("address_line1"),
            "address_line2": r.get("address_line2"),
            "city": r.get("city"),
            "phone": r.get("phone"),
            "latitude": r.get("custom_latitude"),
            "longitude": r.get("custom_longitude"),
            "geo_source": r.get("custom_geo_source"),
            "geo_confidence": r.get("custom_geo_confidence"),
        }
        for r in rows_out
    }


def _customer_phone(customer: Optional[str]) -> str:
    """Fallback phone from the Customer record.

    jarz_pos has a richer resolver (``api.kanban._resolve_customer_phone``, which
    also walks Contacts), but it is private to that module's API layer. Reaching
    into another app's underscore-prefixed helper makes this app break on a
    refactor it cannot see; the Address phone above covers the delivery case,
    which is the one a courier actually needs.
    """
    if not customer:
        return ""
    try:
        return str(frappe.db.get_value("Customer", customer, "mobile_no") or "")
    except Exception:
        return ""


def _item_summary(invoice_id: str) -> List[Dict[str, Any]]:
    try:
        return frappe.get_all(
            "Sales Invoice Item",
            filters={"parent": invoice_id, "parenttype": "Sales Invoice"},
            fields=["item_code", "item_name", "qty", "uom"],
            order_by="idx asc",
            limit=200,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: item summary failed")
        return []


def _invoice_notes(invoice_id: str) -> List[Dict[str, Any]]:
    """Operational notes staff added on the Kanban card."""
    try:
        return frappe.get_all(
            "Jarz Invoice Note",
            filters={"sales_invoice": invoice_id},
            fields=["name", "note", "added_by_full_name", "added_on"],
            order_by="added_on desc",
            limit=20,
        ) or []
    except Exception:
        # The doctype belongs to jarz_pos; a site without it is not an error here.
        return []


def _proofs(invoice_id: str) -> List[Dict[str, Any]]:
    try:
        return frappe.get_all(
            DOCTYPES.DELIVERY_PROOF,
            filters={"sales_invoice": invoice_id},
            fields=[
                "name",
                "proof_type",
                "file",
                "recipient_name",
                "captured_at",
                "latitude",
                "longitude",
                "accuracy_m",
                "is_mocked",
            ],
            order_by="captured_at desc",
            limit=QUERY_LIMITS.PROOFS_PER_STOP,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: proof lookup failed")
        return []

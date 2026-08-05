"""Duty session lifecycle and the end-of-duty reconciliation summary.

A duty is a window, and the summary is what happened inside it. Note what the
summary is computed from: **Sales Invoices delivered by this courier in the
window**, not ledger rows. That is deliberate — the courier statement
(``services/ledger_read``) is the authoritative money view and it reads
``Courier Transaction``; this summary answers the operational question ("did you
hand over what you collected today?") and must stay usable even when settlement
has not run yet.

Nothing here posts. ``opening_float`` and ``closing_cash`` are declarations that
a manager reconciles; the money itself moves through a Courier Deposit
Declaration confirmed by jarz_pos.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from jarz_courier.constants import DEPOSIT_STATUS, DOCTYPES, DUTY_STATUS, INVOICE_STATE

DOCTYPE = DOCTYPES.COURIER_DUTY

DUTY_FIELDS = (
    "name",
    "party_type",
    "party",
    "branch",
    "status",
    "start_time",
    "end_time",
    "vehicle",
    "vehicle_plate",
    "device",
    "opening_float",
    "closing_cash",
    "notes",
)


def get_open_duty(party_type: str, party: str) -> Optional[Dict[str, Any]]:
    """The courier's currently open duty, or None."""
    if not (party_type and party):
        return None
    rows = frappe.get_all(
        DOCTYPE,
        filters={"party_type": party_type, "party": party, "status": DUTY_STATUS.OPEN},
        fields=list(DUTY_FIELDS),
        order_by="start_time desc",
        limit=1,
    ) or []
    return rows[0] if rows else None


def start_duty(
    *,
    party_type: str,
    party: str,
    branch: str,
    vehicle: Optional[str] = None,
    vehicle_plate: Optional[str] = None,
    opening_float: float = 0.0,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Open a duty, or return the one already open.

    Returning the existing duty rather than throwing is what makes this safe for
    the offline queue to replay: a courier who taps Start Duty in a tunnel and
    again on the street must end up with one duty, not an error.
    """
    existing = get_open_duty(party_type, party)
    if existing:
        return {"duty": existing, "created": False}

    doc = frappe.new_doc(DOCTYPE)
    doc.party_type = party_type
    doc.party = party
    doc.branch = branch
    doc.status = DUTY_STATUS.OPEN
    doc.start_time = now_datetime()
    doc.vehicle = vehicle
    doc.vehicle_plate = vehicle_plate
    doc.opening_float = flt(opening_float)
    doc.device = device
    doc.insert(ignore_permissions=True)

    return {"duty": _as_payload(doc), "created": True}


def end_duty(
    *,
    party_type: str,
    party: str,
    duty: Optional[str] = None,
    closing_cash: Optional[float] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Close the duty and return it together with its reconciliation summary."""
    name = str(duty or "").strip()
    if name:
        doc = frappe.get_doc(DOCTYPE, name)
        if doc.party_type != party_type or doc.party != party:
            frappe.throw(_("Duty {0} does not belong to this courier").format(name), frappe.PermissionError)
    else:
        open_duty = get_open_duty(party_type, party)
        if not open_duty:
            frappe.throw(_("No open duty to end"))
        doc = frappe.get_doc(DOCTYPE, open_duty["name"])

    if doc.status != DUTY_STATUS.OPEN:
        # Idempotent: an offline replay of End Duty returns the closed duty.
        return {"duty": _as_payload(doc), "changed": False, "summary": summarize_duty(doc)}

    doc.status = DUTY_STATUS.CLOSED
    doc.end_time = now_datetime()
    if closing_cash is not None:
        doc.closing_cash = flt(closing_cash)
    if notes:
        doc.notes = notes
    doc.save(ignore_permissions=True)

    return {"duty": _as_payload(doc), "changed": True, "summary": summarize_duty(doc)}


def summarize_duty(duty: Any) -> Dict[str, Any]:
    """Operational reconciliation for one duty window.

    ``expected_cash`` is ``opening_float`` plus the grand total of every COD
    invoice this courier delivered inside the window. Online-paid orders are
    excluded because the customer already paid the company, so the courier is
    holding nothing for them — including them is how a courier gets accused of a
    shortfall for an order they never touched cash for.
    """
    window_start = duty.get("start_time")
    window_end = duty.get("end_time") or now_datetime()
    party_type = duty.get("party_type")
    party = duty.get("party")

    delivered = _delivered_invoices(party_type, party, window_start, window_end)

    cod_total = sum(flt(row.get("grand_total")) for row in delivered if _is_cash_collected(row))
    online_total = sum(flt(row.get("grand_total")) for row in delivered if not _is_cash_collected(row))

    declared = _declared_deposits(party_type, party, window_start, window_end)
    declared_total = sum(flt(row.get("amount")) for row in declared)

    opening_float = flt(duty.get("opening_float"))
    closing_cash = flt(duty.get("closing_cash"))
    expected_cash = opening_float + cod_total

    return {
        "duty": duty.get("name"),
        "window": {"start": window_start, "end": window_end},
        "stops_delivered": len(delivered),
        "cod_collected": cod_total,
        "online_delivered": online_total,
        "opening_float": opening_float,
        "expected_cash": expected_cash,
        "closing_cash": closing_cash,
        "declared_total": declared_total,
        # Positive => the courier is holding more than they declared.
        "variance": expected_cash - declared_total,
        "declarations": declared,
    }


def _delivered_invoices(
    party_type: Optional[str], party: Optional[str], start: Any, end: Any
) -> List[Dict[str, Any]]:
    if not (party_type and party and start):
        return []
    try:
        return frappe.get_all(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "custom_courier_party_type": party_type,
                "custom_courier_party": party,
                "custom_sales_invoice_state": INVOICE_STATE.DELIVERED,
                "custom_delivered_at": ["between", [start, end]],
            },
            fields=["name", "customer", "grand_total", "status", "is_pos", "outstanding_amount"],
            order_by="custom_delivered_at asc",
            limit=500,
        ) or []
    except Exception:
        # `custom_delivered_at` is a jarz_pos lane A1 field. If this server is
        # older than that migration the filter throws — degrade to an empty
        # summary rather than blocking End Duty, but say so loudly: a silent {}
        # here reads as "collected nothing today".
        frappe.log_error(
            frappe.get_traceback(),
            "jarz_courier: duty summary query failed (is jarz_pos lane A1 deployed?)",
        )
        return []


def _is_cash_collected(invoice_row: Dict[str, Any]) -> bool:
    """True when the courier physically took money for this order.

    Approximated from the invoice's outstanding amount at delivery time: an order
    already paid online carries no outstanding balance, a COD order does.
    """
    return flt(invoice_row.get("outstanding_amount")) > 0


def _declared_deposits(
    party_type: Optional[str], party: Optional[str], start: Any, end: Any
) -> List[Dict[str, Any]]:
    if not (party_type and party and start):
        return []
    try:
        return frappe.get_all(
            DOCTYPES.COURIER_DEPOSIT_DECLARATION,
            filters={
                "party_type": party_type,
                "party": party,
                "declared_on": ["between", [start, end]],
                "status": ["!=", DEPOSIT_STATUS.REJECTED],
            },
            fields=["name", "amount", "method", "status", "declared_on", "journal_entry"],
            order_by="declared_on asc",
            limit=50,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: declaration lookup failed")
        return []


def _as_payload(doc: Any) -> Dict[str, Any]:
    return {field: doc.get(field) for field in DUTY_FIELDS}

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

from jarz_courier.constants import (
    DEPOSIT_STATUS,
    DOCTYPES,
    DUTY_STATUS,
    INVOICE_STATE,
    QUERY_LIMITS,
)
from jarz_courier.services import courier_run

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
        return {
            "duty": existing,
            "created": False,
            "run": _open_run(
                party_type=party_type,
                party=party,
                branch=existing.get("branch") or branch,
                duty=existing.get("name"),
                device=existing.get("device") or device,
            ),
        }

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

    return {
        "duty": _as_payload(doc),
        "created": True,
        "run": _open_run(
            party_type=party_type, party=party, branch=branch, duty=doc.name, device=device
        ),
    }


def _open_run(
    *, party_type: str, party: str, branch: str, duty: Optional[str], device: Optional[str]
) -> Optional[str]:
    """Open (or adopt) the tracked run for this duty. Best-effort.

    Opened here so the courier's first ping has an anchor already waiting, but the
    ping path can open one too — a courier whose app starts tracking before they tap
    Start Duty must not lose their track. ``ensure_open_run`` is idempotent and
    attaches the duty to a run that a ping created earlier, so the two entry points
    converge on one run rather than racing to create two.

    Wrapped: a tracking failure must never stop a courier starting their shift. Cash
    reconciliation, the run sheet and proof of delivery all work with no run at all.
    """
    try:
        result = courier_run.ensure_open_run(
            party_type=party_type, party=party, branch=branch, duty=duty, device=device
        )
        return (result.get("run") or {}).get("name")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: could not open tracked run")
        return None


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

    return {
        "duty": _as_payload(doc),
        "changed": True,
        "summary": summarize_duty(doc),
        "runs": _close_runs(party_type, party),
    }



def close_stale_duties(*, minutes: int) -> Dict[str, Any]:
    """Close Open duties whose courier stopped reporting ``minutes`` ago.

    Added because the OwnTracks ingest path auto-opens a duty (an iPhone courier has
    no foreground service to bind a shift to). Auto-opening without auto-closing
    means duties accumulate forever — which was already happening before this
    existed: CDUTY-00002 on staging sat Open from 2026-08-08 19:20 with no positions
    flowing, because nothing has ever closed a duty except a courier tapping End
    Shift.

    Deliberately mirrors ``anomaly.watch_stale_pings``'s two-stage escalation rather
    than inventing a second silence threshold, and is called from that same sweep so
    a courier who went quiet does not get one verdict from the run watchdog and a
    different one from here.

    Never sets ``closing_cash``. A cash figure nobody counted is worse than a blank
    one: blank is visibly missing, whereas a fabricated 0.00 reconciles silently and
    wrongly. The note says why the duty closed so a manager reading it later is not
    left guessing whether the courier declared anything.
    """
    from frappe.utils import time_diff_in_seconds

    summary = {"examined": 0, "closed": 0}
    try:
        open_duties = frappe.get_all(
            DOCTYPE,
            filters={"status": DUTY_STATUS.OPEN},
            fields=["name", "party_type", "party", "branch", "start_time"],
            limit=QUERY_LIMITS.STALE_DUTIES_PER_SWEEP,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: stale duty lookup failed")
        return summary

    for duty in open_duties:
        summary["examined"] += 1
        try:
            # Latest ping across this courier's runs, falling back to when the duty
            # opened. A duty with no run at all — the courier never sent a single
            # position — must still age out, or it stays Open forever.
            marker = courier_run.last_ping_for(
                party_type=duty.get("party_type"), party=duty.get("party")
            ) or duty.get("start_time")
            if not marker:
                continue

            silent_minutes = max(
                0.0, float(time_diff_in_seconds(now_datetime(), marker)) / 60.0
            )
            if silent_minutes < minutes:
                continue

            end_duty(
                party_type=duty.get("party_type"),
                party=duty.get("party"),
                duty=duty.get("name"),
                notes=_(
                    "Closed automatically: no position reported for {0} minutes. "
                    "No closing cash was declared."
                ).format(int(silent_minutes)),
            )
            summary["closed"] += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"jarz_courier: stale duty close failed for {duty.get('name')}",
            )

    return summary


def _close_runs(party_type: str, party: str) -> List[Dict[str, Any]]:
    """Close the tracked run(s) so the day's polyline gets written. Best-effort.

    This is the **cold path trigger**: closing a run is what turns thousands of Redis
    fixes into one encoded polyline and one distance on one row, and it is also what
    lets the anomaly detectors see the raw trail while it still exists.

    Wrapped because it must never block End Duty. A courier standing in a depot at the
    end of a shift needs their duty closed and their cash reconciled; a polyline that
    failed to encode is recoverable — the run stays Open, the stale-ping watchdog
    closes it as Abandoned within a few hours, and the trail survives in Redis for 12.
    Refusing to close the duty over it would strand the reconciliation instead.
    """
    try:
        return courier_run.close_open_runs(party_type, party)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: could not close tracked run")
        return []


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

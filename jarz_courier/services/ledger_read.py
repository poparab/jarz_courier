"""READ-ONLY view of a courier's ledger position.

**This is the only module in ``jarz_courier`` permitted to name a ledger doctype**,
and ``tests/test_no_gl_writes.py`` enforces two things about it:

* it is the sole entry in that test's allowlist, so a second file that starts
  reading ``Courier Transaction`` fails CI rather than quietly becoming a second
  source of truth; and
* **no mutating call may appear anywhere in it** — not ``new_doc``, ``insert``,
  ``save``, ``submit``, ``set_value``, ``delete``, ``db.sql`` or ``db.commit``, for
  any doctype at all. The file is structurally incapable of writing, which is a
  stronger guarantee than "we reviewed it and it looks read-only".

Everything here answers "where does this courier stand?" for the courier's own
My Account screen. The authoritative money movements are jarz_pos's: it creates
the ``Courier Transaction`` rows at Out-for-Delivery and the settlement Journal
Entries at hand-over, and its GL audit suite is what tests them.

Balance convention, copied from ``delivery_handling._summarize_courier_transactions``
so the courier's phone and the manager's Desk agree::

    net_to_branch = sum(amount) - sum(shipping_amount)

``amount`` is the customer money the courier collected and owes the branch;
``shipping_amount`` is the delivery fee the branch owes the courier. Partner rows
(``is_partner_order``) are excluded — a 3PL's orders settle against the partner's
Payable, never against an individual courier's balance, and counting them here
would show a courier a debt that is not theirs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt, getdate, nowdate

from jarz_courier.constants import QUERY_LIMITS

LEDGER_DOCTYPE = "Courier Transaction"

STATUS_SETTLED = "Settled"

_LEDGER_FIELDS = (
    "name",
    "date",
    "status",
    "reference_invoice",
    "delivery_trip",
    "amount",
    "shipping_amount",
    "payment_mode",
    "journal_entry",
    "delivery_partner",
    "is_partner_order",
    "notes",
)


def _party_filters(party_type: str, party: str) -> Dict[str, Any]:
    return {
        "party_type": party_type,
        "party": party,
        # `["in", [0, None]]` rather than `0`: the column was added after rows
        # already existed, so historical rows hold NULL, and `is_partner_order = 0`
        # silently drops every one of them.
        "is_partner_order": ["in", [0, None]],
    }


def get_unsettled(party_type: str, party: str, limit: int = QUERY_LIMITS.STATEMENT_ROWS) -> List[Dict[str, Any]]:
    """Rows the courier still owes against (or is still owed for)."""
    if not (party_type and party):
        return []
    filters = _party_filters(party_type, party)
    filters["status"] = ["!=", STATUS_SETTLED]
    try:
        return frappe.get_all(
            LEDGER_DOCTYPE,
            filters=filters,
            fields=list(_LEDGER_FIELDS),
            order_by="date desc, creation desc",
            limit=limit,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: unsettled ledger read failed")
        return []


def get_collected_between(
    party_type: str, party: str, from_date: Any, to_date: Any, limit: int = QUERY_LIMITS.STATEMENT_ROWS
) -> List[Dict[str, Any]]:
    """Every row dated inside the window, settled or not."""
    if not (party_type and party):
        return []
    filters = _party_filters(party_type, party)
    filters["date"] = ["between", [from_date, to_date]]
    try:
        return frappe.get_all(
            LEDGER_DOCTYPE,
            filters=filters,
            fields=list(_LEDGER_FIELDS),
            order_by="date desc, creation desc",
            limit=limit,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: window ledger read failed")
        return []


def get_settlement_history(
    party_type: str, party: str, limit: int = QUERY_LIMITS.SETTLEMENT_HISTORY
) -> List[Dict[str, Any]]:
    """Past hand-overs, grouped by the Journal Entry that posted each one.

    Grouped rather than listed row-by-row because a settlement covers many orders
    and a courier arguing about last Tuesday means the payment, not the 14 invoices
    inside it. The JE name is carried through so a manager can open the entry.
    """
    if not (party_type and party):
        return []

    filters = _party_filters(party_type, party)
    filters["status"] = STATUS_SETTLED
    filters["journal_entry"] = ["is", "set"]

    try:
        rows = frappe.get_all(
            LEDGER_DOCTYPE,
            filters=filters,
            fields=list(_LEDGER_FIELDS),
            order_by="date desc, creation desc",
            limit=limit * 20,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: settlement history read failed")
        return []

    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        je = str(row.get("journal_entry") or "")
        if not je:
            continue
        bucket = grouped.setdefault(
            je,
            {
                "journal_entry": je,
                "date": row.get("date"),
                "orders": 0,
                "order_amount": 0.0,
                "shipping_amount": 0.0,
                "net_to_branch": 0.0,
                "invoices": [],
            },
        )
        bucket["orders"] += 1
        bucket["order_amount"] += flt(row.get("amount"))
        bucket["shipping_amount"] += flt(row.get("shipping_amount"))
        if row.get("reference_invoice"):
            bucket["invoices"].append(row["reference_invoice"])

    for bucket in grouped.values():
        bucket["order_amount"] = round(bucket["order_amount"], 2)
        bucket["shipping_amount"] = round(bucket["shipping_amount"], 2)
        bucket["net_to_branch"] = round(bucket["order_amount"] - bucket["shipping_amount"], 2)

    return sorted(grouped.values(), key=lambda b: str(b.get("date") or ""), reverse=True)[:limit]


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """``{order_amount, shipping_amount, net_to_branch, count}`` for a row set."""
    order_total = 0.0
    shipping_total = 0.0
    for row in rows or []:
        if row.get("is_partner_order"):
            continue
        order_total += flt(row.get("amount"))
        shipping_total += flt(row.get("shipping_amount"))

    return {
        "count": len(rows or []),
        "order_amount": round(order_total, 2),
        "shipping_amount": round(shipping_total, 2),
        "net_to_branch": round(order_total - shipping_total, 2),
    }


def build_statement(
    *,
    party_type: str,
    party: str,
    from_date: Optional[Any] = None,
    to_date: Optional[Any] = None,
) -> Dict[str, Any]:
    """The whole My Account payload in one read.

    ``unsettled`` is the number that matters to a courier — it is what they are
    carrying right now. ``today`` and ``period`` are context. ``fees`` is the
    shipping the branch owes the courier over the same window, surfaced separately
    because couriers reliably read a single net figure as "what I owe" and are
    then surprised by their own delivery fees.
    """
    today = nowdate()
    window_from = getdate(from_date) if from_date else getdate(today)
    window_to = getdate(to_date) if to_date else getdate(today)

    unsettled_rows = get_unsettled(party_type, party)
    today_rows = get_collected_between(party_type, party, today, today)
    period_rows = get_collected_between(party_type, party, window_from, window_to)

    unsettled = summarize(unsettled_rows)
    today_summary = summarize(today_rows)
    period = summarize(period_rows)

    return {
        "party_type": party_type,
        "party": party,
        "as_of": today,
        "window": {"from": str(window_from), "to": str(window_to)},
        # What the courier is holding for the branch right now.
        "unsettled_balance": unsettled["net_to_branch"],
        "unsettled": unsettled,
        "collected_today": today_summary["order_amount"],
        "today": today_summary,
        "period": period,
        # Delivery fees earned in the window (money the branch owes the courier).
        "fees": period["shipping_amount"],
        # Anything that reduces the hand-over other than fees. Zero today: the
        # ledger carries no deduction column, so this is an explicit placeholder
        # rather than a silently missing key the client would render as "null".
        "deductions": 0.0,
        "open_rows": unsettled_rows,
        "settlements": get_settlement_history(party_type, party),
    }

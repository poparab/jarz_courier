"""Courier cash hand-over: declare, confirm, reject.

**The declaration is the only write this app makes on the money path**
(COURIER_CONTRACTS.md §9, COURIER_APP_SPEC.md §2.1). It is a claim — "I handed
over 4,200 in cash to Ahmed" — and it moves nothing. Confirming it calls a
jarz_pos service, which posts the entry and returns the document it created; this
module records that reference and nothing else.

Why the claim is a document rather than a direct settlement call:

* A courier and a branch manager disagreeing about a hand-over is a routine
  event, and the disagreement needs a record that predates the posting. Calling
  settlement straight from the courier's phone would make the courier's word the
  posting, with no reviewable step between.
* The manager's confirmation is a distinct, role-gated action with a distinct
  actor recorded on it (``confirmed_by``). That is the control.

The doctype controller holds the immutability rules (a posted declaration cannot
be edited, terminal statuses are terminal). This module holds the flow.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from jarz_courier.constants import DEPOSIT_METHODS, DEPOSIT_STATUS, DOCTYPES, QUERY_LIMITS, WS_EVENTS
from jarz_courier.services import pos_bridge

DOCTYPE = DOCTYPES.COURIER_DEPOSIT_DECLARATION

DECLARATION_FIELDS = (
    "name",
    "party_type",
    "party",
    "branch",
    "status",
    "amount",
    "method",
    "reference",
    "photo",
    "declared_on",
    "duty",
    "confirmed_by",
    "confirmed_on",
    "journal_entry",
    "rejection_reason",
    "request_id",
    "notes",
)

_METHOD_ALIASES = {
    "cash": DEPOSIT_METHODS.CASH_HANDOVER,
    "cash handover": DEPOSIT_METHODS.CASH_HANDOVER,
    "cash_handover": DEPOSIT_METHODS.CASH_HANDOVER,
    "instapay": DEPOSIT_METHODS.INSTAPAY,
    "insta pay": DEPOSIT_METHODS.INSTAPAY,
}


def normalize_method(value: Any) -> str:
    resolved = _METHOD_ALIASES.get(str(value or "").strip().lower())
    if not resolved:
        frappe.throw(_("Deposit method must be one of: {0}").format(", ".join(DEPOSIT_METHODS.ALL)))
    return resolved


def find_by_request_id(request_id: Optional[str]) -> Optional[Dict[str, Any]]:
    key = str(request_id or "").strip()
    if not key:
        return None
    rows = frappe.get_all(
        DOCTYPE, filters={"request_id": key}, fields=list(DECLARATION_FIELDS), limit=1
    ) or []
    return rows[0] if rows else None


def declare(
    *,
    party_type: str,
    party: str,
    branch: str,
    amount: float,
    method: Any,
    reference: Optional[str] = None,
    photo: Optional[str] = None,
    duty: Optional[str] = None,
    request_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a courier's hand-over claim. Posts nothing.

    Idempotent on ``request_id`` so the offline queue can replay it: a courier who
    declares a deposit in a basement and the queue flushes twice must not create
    two claims a manager then confirms twice.
    """
    key = str(request_id or "").strip() or None
    existing = find_by_request_id(key) if key else None
    if existing:
        return {"declaration": existing, "created": False}

    doc = frappe.new_doc(DOCTYPE)
    doc.party_type = party_type
    doc.party = party
    doc.branch = branch
    doc.amount = flt(amount)
    doc.method = normalize_method(method)
    doc.reference = reference
    doc.photo = photo
    doc.duty = duty
    doc.request_id = key
    doc.notes = notes
    doc.status = DEPOSIT_STATUS.PENDING
    doc.declared_on = now_datetime()

    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        winner = find_by_request_id(key)
        if winner:
            return {"declaration": winner, "created": False}
        raise

    pos_bridge.publish_to_branches(
        WS_EVENTS.COURIER_DEPOSIT_DECLARED,
        {
            "declaration": doc.name,
            "party_type": party_type,
            "party": party,
            "branch": branch,
            "amount": flt(amount),
            "method": doc.method,
            "status": doc.status,
        },
        [branch] if branch else [],
    )

    return {"declaration": _as_payload(doc), "created": True}


def confirm(*, name: str, confirmed_by: Optional[str] = None) -> Dict[str, Any]:
    """Approve a declaration and have **jarz_pos** post the money.

    Order of operations matters and is deliberate:

    1. Re-read the declaration and refuse anything not ``Pending`` — the guard
       against a double confirm from two managers tapping at once.
    2. Call jarz_pos to settle. If it raises, nothing on this side has changed and
       the declaration is still Pending, so the manager can retry.
    3. Only then stamp ``status``, ``confirmed_by`` and the returned
       ``journal_entry`` onto the declaration.

    Doing (3) before (2) would leave a Confirmed declaration with no posting behind
    it after any settlement failure — a hand-over that the books never saw but the
    UI shows as done.
    """
    doc = frappe.get_doc(DOCTYPE, name)
    if doc.status != DEPOSIT_STATUS.PENDING:
        # Idempotent for a retried tap; informative for a genuine double-confirm.
        return {
            "declaration": _as_payload(doc),
            "changed": False,
            "reason": f"already {doc.status}",
        }

    result = pos_bridge.settle_courier_deposit(
        party_type=doc.party_type,
        party=doc.party,
        pos_profile=doc.branch,
        amount=flt(doc.amount),
        method=doc.method,
        reference=doc.reference,
        declaration=doc.name,
    ) or {}

    posted_entry = result.get("journal_entry")
    settled_net = flt(result.get("net_balance"))

    doc.status = DEPOSIT_STATUS.CONFIRMED
    doc.confirmed_by = confirmed_by or frappe.session.user
    doc.confirmed_on = now_datetime()
    if posted_entry:
        doc.journal_entry = posted_entry
    doc.save(ignore_permissions=True)

    pos_bridge.publish_to_branches(
        WS_EVENTS.COURIER_DEPOSIT_DECLARED,
        {
            "declaration": doc.name,
            "party_type": doc.party_type,
            "party": doc.party,
            "branch": doc.branch,
            "status": doc.status,
            "journal_entry": posted_entry,
        },
        [doc.branch] if doc.branch else [],
    )

    return {
        "declaration": _as_payload(doc),
        "changed": True,
        "posting": result,
        # The declared figure and what was actually settled are reported
        # separately and never reconciled silently. A mismatch means the courier
        # declared a partial hand-over against a balance that settles in full, and
        # a manager has to see that rather than have it averaged away.
        "declared_amount": flt(doc.amount),
        "settled_net": settled_net,
        "amount_matches": abs(flt(doc.amount) - settled_net) < 0.01,
    }


def reject(*, name: str, reason: str, rejected_by: Optional[str] = None) -> Dict[str, Any]:
    """Refuse a declaration. Posts nothing, and never has anything to unwind."""
    cleaned = str(reason or "").strip()
    if not cleaned:
        frappe.throw(_("A rejection must say why"))

    doc = frappe.get_doc(DOCTYPE, name)
    if doc.status != DEPOSIT_STATUS.PENDING:
        return {
            "declaration": _as_payload(doc),
            "changed": False,
            "reason": f"already {doc.status}",
        }

    doc.status = DEPOSIT_STATUS.REJECTED
    doc.rejection_reason = cleaned
    doc.confirmed_by = rejected_by or frappe.session.user
    doc.confirmed_on = now_datetime()
    doc.save(ignore_permissions=True)

    return {"declaration": _as_payload(doc), "changed": True}


def list_declarations(
    *,
    party_type: Optional[str] = None,
    party: Optional[str] = None,
    branches: Optional[List[str]] = None,
    status: Optional[str] = None,
    limit: int = QUERY_LIMITS.PENDING_DEPOSITS,
) -> List[Dict[str, Any]]:
    """Declarations matching the filters, newest first.

    A ``branches`` list that is supplied but empty returns nothing rather than
    everything — the same rule as the run sheet. Widening a query because a scope
    resolved to nothing is how one branch's cash ends up on another branch's screen.
    """
    filters: Dict[str, Any] = {}
    if party_type and party:
        filters["party_type"] = party_type
        filters["party"] = party
    if branches is not None:
        cleaned = [str(b).strip() for b in branches if str(b or "").strip()]
        if not cleaned:
            return []
        filters["branch"] = ["in", cleaned]
    if status:
        filters["status"] = status

    return frappe.get_all(
        DOCTYPE,
        filters=filters,
        fields=list(DECLARATION_FIELDS),
        order_by="declared_on desc",
        limit=limit,
    ) or []


def _as_payload(doc: Any) -> Dict[str, Any]:
    return {field: doc.get(field) for field in DECLARATION_FIELDS}

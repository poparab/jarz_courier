"""Whitelisted endpoints for the ops alert feed (contract F7).

Thin transport over the ``Courier Anomaly`` table, which ``services/anomaly`` already
fills. The POS app shows the feed and a badge; this module is how it reads and
acknowledges findings.

Same door and the same scoping rules as ``api/tracking.get_live_positions``:
``COURIER_MAP_VIEWER`` only (a courier must never see findings about colleagues), and
every query is limited to the caller's own POS Profiles. A scope that resolves to
nothing returns nothing, never everything. A finding with no branch is therefore
invisible here; it stays reachable from Desk.

**A flag, never a charge.** Acknowledging records who looked and what they decided. It
writes nothing else: no money, no ledger row, no Courier Transaction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_courier.constants import ANOMALY_STATUS, DOCTYPES, ROLES, SEVERITY
from jarz_courier.services import pos_bridge

#: Hard ceiling on one page of the feed, whatever the client asks for.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

#: The two outcomes a person can record. "Open" is not one of them.
ACK_STATUSES = (ANOMALY_STATUS.REVIEWED, ANOMALY_STATUS.DISMISSED)

_PARTY_NAME_FIELD = {"Employee": "employee_name", "Supplier": "supplier_name"}

_ALERT_FIELDS = [
    "name",
    "anomaly_type",
    "severity",
    "detected_on",
    "party_type",
    "party",
    "branch",
    "sales_invoice",
    "run",
    "reason",
    "status",
]


def _ensure_alert_permission() -> None:
    """Reading or closing findings about other people is a supervisor action."""
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_MAP_VIEWER):
        frappe.throw(_("Only a manager can view courier alerts"), frappe.PermissionError)


def _scoped_branches(branch: Optional[str] = None) -> List[str]:
    allowed = pos_bridge.get_user_pos_profiles()
    requested = str(branch or "").strip()
    if requested:
        if requested not in allowed:
            frappe.throw(
                _("You are not assigned to branch {0}").format(requested),
                frappe.PermissionError,
            )
        return [requested]
    return list(allowed)


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _party_names(rows: List[Dict[str, Any]]) -> Dict[tuple, str]:
    """``{(party_type, party): display name}``, one lookup per distinct party."""
    names: Dict[tuple, str] = {}
    for row in rows:
        key = (row.get("party_type"), row.get("party"))
        if key in names or not all(key):
            continue
        field = _PARTY_NAME_FIELD.get(str(key[0]))
        value = None
        if field:
            try:
                value = frappe.db.get_value(key[0], key[1], field)
            except Exception:
                value = None
        names[key] = str(value or key[1])
    return names


def _shape(row: Dict[str, Any], names: Dict[tuple, str]) -> Dict[str, Any]:
    key = (row.get("party_type"), row.get("party"))
    detected = row.get("detected_on")
    return {
        "name": row.get("name"),
        "anomaly_type": row.get("anomaly_type"),
        "severity": row.get("severity"),
        "detected_on": str(detected) if detected else None,
        "party_type": row.get("party_type"),
        "party": row.get("party"),
        "party_name": names.get(key) or row.get("party") or "",
        "branch": row.get("branch"),
        "invoice": row.get("sales_invoice"),
        "run": row.get("run"),
        "details": row.get("reason"),
        "status": row.get("status"),
    }


def _counts(branches: List[str]) -> Dict[str, int]:
    if not branches:
        return {"open_count": 0, "high_count": 0}
    base = {"branch": ["in", branches], "status": ANOMALY_STATUS.OPEN}
    return {
        "open_count": int(frappe.db.count(DOCTYPES.COURIER_ANOMALY, base) or 0),
        "high_count": int(
            frappe.db.count(DOCTYPES.COURIER_ANOMALY, {**base, "severity": SEVERITY.HIGH}) or 0
        ),
    }


@frappe.whitelist(allow_guest=False)
def list_alerts(
    status: Optional[str] = "Open",
    limit: Any = DEFAULT_LIMIT,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Courier findings for the caller's branches, newest first.

    ``status`` is one of Open, Reviewed, Dismissed, or ``"All"``/empty for every
    status. ``open_count`` and ``high_count`` always describe the *open* findings in
    scope, whatever ``status`` filters the list to, so a badge can be drawn from any
    call.
    """
    _ensure_alert_permission()
    try:
        branches = _scoped_branches(branch)
        wanted = str(status or "").strip()
        if wanted and wanted.lower() != "all" and wanted not in ANOMALY_STATUS.ALL:
            frappe.throw(_("Unknown alert status {0}").format(wanted))

        if not branches:
            return {"alerts": [], "open_count": 0, "high_count": 0}

        filters: Dict[str, Any] = {"branch": ["in", branches]}
        if wanted and wanted.lower() != "all":
            filters["status"] = wanted

        rows = (
            frappe.get_all(
                DOCTYPES.COURIER_ANOMALY,
                filters=filters,
                fields=_ALERT_FIELDS,
                order_by="detected_on desc, creation desc",
                limit=_clamp_limit(limit),
            )
            or []
        )
        names = _party_names(rows)
        return {"alerts": [_shape(r, names) for r in rows], **_counts(branches)}
    except (frappe.PermissionError, frappe.ValidationError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier list_alerts failed")
        return {"success": False, "error": str(exc), "alerts": [], "open_count": 0, "high_count": 0}


@frappe.whitelist(allow_guest=False)
def get_alert_counts() -> Dict[str, Any]:
    """Just the two badge numbers, for polling."""
    _ensure_alert_permission()
    try:
        return _counts(_scoped_branches())
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_alert_counts failed")
        return {"success": False, "error": str(exc), "open_count": 0, "high_count": 0}


@frappe.whitelist(allow_guest=False)
def acknowledge_alert(
    name: str, status: Optional[str] = "Reviewed", note: Optional[str] = None
) -> Dict[str, Any]:
    """Close a finding as Reviewed or Dismissed, with an optional note.

    The doctype controller stamps ``reviewed_by`` / ``reviewed_on`` the moment the
    status leaves Open, so the attribution cannot be skipped by calling this
    differently. The note is appended to ``review_notes`` rather than replacing it.
    """
    _ensure_alert_permission()
    chosen = str(status or ANOMALY_STATUS.REVIEWED).strip() or ANOMALY_STATUS.REVIEWED
    if chosen not in ACK_STATUSES:
        frappe.throw(_("Status must be Reviewed or Dismissed"))
    if not str(name or "").strip():
        frappe.throw(_("Alert name is required"))

    doc = frappe.get_doc(DOCTYPES.COURIER_ANOMALY, str(name).strip())
    if str(doc.branch or "") not in _scoped_branches():
        frappe.throw(
            _("You are not assigned to the branch of this alert"), frappe.PermissionError
        )

    doc.status = chosen
    text = str(note or "").strip()
    if text:
        existing = str(doc.review_notes or "").strip()
        doc.review_notes = f"{existing}\n{text}" if existing else text
    doc.save(ignore_permissions=True)

    row = {f: doc.get(f) for f in _ALERT_FIELDS}
    return _shape(row, _party_names([row]))

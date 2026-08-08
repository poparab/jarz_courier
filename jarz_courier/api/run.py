"""Whitelisted endpoints for the courier's run sheet.

The run sheet is a **query**, not a document (COURIER_APP_SPEC.md §3): the courier
is assigned on ``Sales Invoice.custom_courier_party``, so today's stops are simply
the submitted invoices carrying that party in state ``Out for Delivery``, scoped to
the courier's branch. No ``Courier Run`` doctype exists in P1 and none is needed.

Every **write** in this module delegates to
``jarz_pos.services.courier_delivery.mark_invoice_*`` (COURIER_CONTRACTS.md §5).
That is not a stylistic preference — those functions own the meta assertion, the
dual idempotency token, the ``update_submitted_sales_invoice_fields`` write path,
the access gate, the feature flag and the branch-scoped realtime publish. Writing
any of it here would be a second, untested implementation of the money-adjacent
half of a delivery.

Identity is resolved through ``jarz_pos.services.courier_identity`` (via
``services.pos_bridge``) — never from a client-supplied party. A courier who could
name their own ``party`` could read another courier's run.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_courier.constants import INVOICE_STATE, QUERY_LIMITS, ROLES
from jarz_courier.services import courier_onboarding, pos_bridge, run_sheet


def _ensure_run_permission() -> None:
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SELF | ROLES.COURIER_SUPERVISOR):
        frappe.throw(_("You are not permitted to view a courier run"), frappe.PermissionError)


@frappe.whitelist(allow_guest=False)
def get_my_run(
    branch: Optional[str] = None,
    state: str = INVOICE_STATE.OUT_FOR_DELIVERY,
    limit: int = QUERY_LIMITS.RUN_STOPS,
) -> Dict[str, Any]:
    """Today's stops for the signed-in courier, in run order.

    A courier with no branch, or whose Employee branch disagrees with their POS
    Profile assignment, gets the setup diagnosis rather than an empty list — the
    two are indistinguishable on screen and the empty list is the failure mode this
    endpoint exists to make impossible.
    """
    _ensure_run_permission()
    try:
        identity = courier_onboarding.resolve_active_branch(branch, action_label="the run sheet")
        result = run_sheet.get_run(
            party_type=identity["party_type"],
            party=identity["party"],
            branches=[identity["branch"]],
            state=state,
            limit=limit,
        )
        return {
            "success": True,
            "courier": {
                "party_type": identity["party_type"],
                "party": identity["party"],
                "display_name": identity.get("display_name"),
                "branch": identity["branch"],
            },
            **result,
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_my_run failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_stop_detail(invoice_id: str) -> Dict[str, Any]:
    """Everything the stop screen needs: customer, address, items, notes, proofs."""
    _ensure_run_permission()
    try:
        _assert_stop_access(invoice_id, action_label="viewing this stop")
        return {"success": True, "stop": run_sheet.get_stop(invoice_id=invoice_id)}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_stop_detail failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def mark_arrived(
    invoice_id: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Courier reached the door. Delegates to jarz_pos lane A3."""
    _ensure_run_permission()
    try:
        _assert_stop_access(invoice_id, action_label="marking arrival")
        return pos_bridge.mark_invoice_arrived(
            invoice_id,
            latitude=_as_float(latitude),
            longitude=_as_float(longitude),
            accuracy_m=_as_float(accuracy_m),
            request_id=request_id,
        )
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier mark_arrived failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def mark_delivered(
    invoice_id: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    collected_amount: Optional[float] = None,
    recipient_name: Optional[str] = None,
    is_mocked: Any = False,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Order handed over. Delegates to jarz_pos lane A3.

    ``request_id`` is passed straight through: the contract's dual-idempotency rule
    pairs it with a deterministic ``"<invoice>::delivered"`` token on the jarz_pos
    side, so a replay after a device reinstall is still caught. Do not attempt to
    dedupe here as well — a second guard with different keys turns one idempotency
    question into two answers.
    """
    _ensure_run_permission()
    try:
        _assert_stop_access(invoice_id, action_label="marking delivery")
        return pos_bridge.mark_invoice_delivered(
            invoice_id,
            latitude=_as_float(latitude),
            longitude=_as_float(longitude),
            accuracy_m=_as_float(accuracy_m),
            collected_amount=_as_float(collected_amount),
            recipient_name=recipient_name,
            is_mocked=_as_bool(is_mocked),
            request_id=request_id,
        )
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier mark_delivered failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def mark_failed(
    invoice_id: str,
    failure_reason: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    notes: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Delivery attempt failed.

    The invoice stays ``Out for Delivery`` (COURIER_CONTRACTS.md §1 — no new state
    is added by this project); the outcome is expressed through
    ``custom_delivery_failure_reason`` and ``custom_delivery_attempt_no``, both
    written by jarz_pos.
    """
    _ensure_run_permission()
    try:
        _assert_stop_access(invoice_id, action_label="marking a failed delivery")
        return pos_bridge.mark_invoice_failed(
            invoice_id,
            failure_reason=failure_reason,
            latitude=_as_float(latitude),
            longitude=_as_float(longitude),
            accuracy_m=_as_float(accuracy_m),
            notes=notes,
            request_id=request_id,
        )
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier mark_failed failed")
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def _assert_stop_access(invoice_id: str, *, action_label: str) -> Dict[str, Any]:
    """Two gates, both required.

    ``ensure_profile_scoped_invoice_access`` answers "is this order's branch one of
    yours?"; the courier-party check answers "is this order assigned to *you*?".
    Branch scoping alone is not enough — every courier on a branch shares it, so a
    branch-only check would let one courier mark another's stop delivered.

    jarz_pos's ``mark_invoice_*`` runs its own gate on top of this (contract §5.5,
    including the open-shift check). The duplication is intentional: this one
    produces the courier-shaped error message, that one is the authority.
    """
    name = str(invoice_id or "").strip()
    if not name:
        frappe.throw(_("Invoice is required"))

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

    identity = courier_onboarding.ensure_courier_setup(action_label=action_label)

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
    """Client numbers arrive as strings over HTTP form encoding."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@frappe.whitelist(allow_guest=False)
def get_failure_reasons() -> Dict[str, Any]:
    """Active failure reasons for the app's "why did this fail?" sheet.

    Exists because the courier app must never hardcode this list. The codes are
    stored on the invoice and drive what happens next (reschedule / return /
    cancel), so a stale copy compiled into an APK would keep writing a reason the
    server has since retired — and an APK cannot be corrected as fast as a
    DocType row.

    Reads through ``pos_bridge``: the DocType is owned by ``jarz_pos``, and that
    app decides what "active" means.
    """
    _ensure_run_permission()
    try:
        return {"success": True, "reasons": pos_bridge.list_failure_reasons()}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "get_failure_reasons failed")
        return {"success": False, "error": str(exc), "reasons": []}

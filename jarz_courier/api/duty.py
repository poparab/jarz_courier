"""Whitelisted endpoints for the courier duty session.

Thin transport over ``jarz_courier.services.duty_session``. Every rule that
matters — one open duty per courier, no reopening a closed one, the end-of-duty
reconciliation arithmetic — lives in the service and the ``Courier Duty``
controller, so the Desk form and a replayed offline queue entry both obey them.

Nothing here posts. Opening float and closing cash are declarations a manager
reconciles; the cash itself moves through ``api/statement.declare_deposit`` →
``confirm_deposit`` → a jarz_pos settlement service.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_courier.constants import ROLES, WS_EVENTS
from jarz_courier.services import courier_onboarding, device_registry, duty_session, pos_bridge


def _ensure_duty_permission() -> None:
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SELF | ROLES.COURIER_SUPERVISOR):
        frappe.throw(_("You are not permitted to manage courier duty"), frappe.PermissionError)


@frappe.whitelist(allow_guest=False)
def start_duty(
    branch: Optional[str] = None,
    vehicle: Optional[str] = None,
    vehicle_plate: Optional[str] = None,
    opening_float: float = 0.0,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Open a duty on the courier's branch, or return the one already open.

    ``branch`` is validated against the courier's own Employee branch rather than
    trusted — a client-supplied branch the courier is not assigned to would be a
    scoping bypass, not a preference.
    """
    _ensure_duty_permission()
    try:
        identity = courier_onboarding.resolve_active_branch(branch, action_label="starting duty")

        bound_device = device
        if not bound_device:
            active = device_registry.get_active_device(identity["party_type"], identity["party"])
            bound_device = (active or {}).get("name")

        result = duty_session.start_duty(
            party_type=identity["party_type"],
            party=identity["party"],
            branch=identity["branch"],
            vehicle=vehicle,
            vehicle_plate=vehicle_plate,
            opening_float=opening_float,
            device=bound_device,
        )

        if result.get("created"):
            pos_bridge.publish_to_branches(
                WS_EVENTS.COURIER_DUTY_CHANGED,
                {
                    "duty": (result.get("duty") or {}).get("name"),
                    "party_type": identity["party_type"],
                    "party": identity["party"],
                    "branch": identity["branch"],
                    "status": "Open",
                },
                [identity["branch"]],
            )

        return {"success": True, **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier start_duty failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def end_duty(
    duty: Optional[str] = None,
    closing_cash: Optional[float] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Close the duty and return its reconciliation summary."""
    _ensure_duty_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="ending duty")

        result = duty_session.end_duty(
            party_type=identity["party_type"],
            party=identity["party"],
            duty=duty,
            closing_cash=closing_cash,
            notes=notes,
        )

        if result.get("changed"):
            pos_bridge.publish_to_branches(
                WS_EVENTS.COURIER_DUTY_CHANGED,
                {
                    "duty": (result.get("duty") or {}).get("name"),
                    "party_type": identity["party_type"],
                    "party": identity["party"],
                    "branch": identity.get("branch"),
                    "status": "Closed",
                },
                [identity.get("branch")] if identity.get("branch") else [],
            )

        return {"success": True, **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier end_duty failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_duty_summary(duty: Optional[str] = None) -> Dict[str, Any]:
    """Read-only reconciliation for a duty — the open one unless *duty* is named.

    Read-only by construction: it summarises delivered invoices and declared
    deposits inside the duty window and writes nothing, so the courier can watch
    their expected hand-over grow through the day.
    """
    _ensure_duty_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="the duty summary")

        name = str(duty or "").strip()
        if name:
            row = frappe.db.get_value(
                "Courier Duty",
                name,
                [
                    "name",
                    "party_type",
                    "party",
                    "branch",
                    "status",
                    "start_time",
                    "end_time",
                    "opening_float",
                    "closing_cash",
                ],
                as_dict=True,
            )
            if not row:
                frappe.throw(_("Courier Duty {0} not found").format(name))
            _assert_duty_visible(row, identity)
        else:
            row = duty_session.get_open_duty(identity["party_type"], identity["party"])
            if not row:
                return {"success": True, "duty": None, "summary": None}

        return {"success": True, "duty": row, "summary": duty_session.summarize_duty(row)}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_duty_summary failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_active_duty() -> Dict[str, Any]:
    """The caller's currently-open duty, or ``None``.

    The app calls this on every launch to answer one question before it can
    render anything: is this courier already on shift? Without it a courier who
    force-quits mid-run reopens the app with no duty in memory, and either starts
    a second one or is told to clock in while the foreground service from the
    first is still pinging.

    ``{"duty": None}`` is a SUCCESS, not an error — "not on shift" is the normal
    state for most of the day, and returning a failure envelope for it would make
    the client treat a healthy launch as a broken one.

    Deliberately no summary: this runs on every cold start and the summary walks
    delivered invoices and declared deposits. Use ``get_duty_summary`` for that.
    """
    _ensure_duty_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="your active duty")
        row = duty_session.get_open_duty(identity["party_type"], identity["party"])
        return {"success": True, "duty": row or None}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_active_duty failed")
        return {"success": False, "error": str(exc), "duty": None}


def _assert_duty_visible(row: Dict[str, Any], identity: Dict[str, Any]) -> None:
    """Own duty always; someone else's only for a supervisor on the same branch."""
    if row.get("party_type") == identity.get("party_type") and row.get("party") == identity.get("party"):
        return

    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SUPERVISOR):
        frappe.throw(_("This duty belongs to another courier"), frappe.PermissionError)

    allowed = set(pos_bridge.get_user_pos_profiles())
    branch = str(row.get("branch") or "")
    if branch and branch not in allowed:
        frappe.throw(
            _("Duty {0} belongs to branch {1}, which you are not assigned to").format(
                row.get("name"), branch
            ),
            frappe.PermissionError,
        )

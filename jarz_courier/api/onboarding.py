"""Whitelisted endpoints for setting up courier logins from the POS app (contract F2).

Thin transport over ``services/courier_onboarding``. The door is the Users screen's
(JARZ Manager, System Manager or Administrator), enforced inside the service through
``pos_bridge`` so there is one definition of it, in jarz_pos.

**A PIN is shown once.** ``setup_courier`` and ``reset_courier_pin`` return it to the
manager who asked and nowhere else: it is not written to any log, error entry or
comment, and the failure paths below log a traceback only, never the arguments or the
result.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe

from jarz_courier.services import courier_onboarding


@frappe.whitelist(allow_guest=False)
def list_courier_candidates() -> Dict[str, Any]:
    """Active Employees that could become couriers, and the branches to put them in."""
    try:
        return courier_onboarding.list_courier_candidates()
    except (frappe.PermissionError, frappe.ValidationError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier list_courier_candidates failed")
        return {"success": False, "error": str(exc), "employees": [], "branches": []}


@frappe.whitelist(allow_guest=False)
def setup_courier(
    employee: Optional[str] = None,
    full_name: Optional[str] = None,
    mobile_no: Optional[str] = None,
    branch: Optional[str] = None,
    pos_profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or repair a courier login and return its login id and a new PIN."""
    try:
        return courier_onboarding.setup_courier(
            employee=employee,
            full_name=full_name,
            mobile_no=mobile_no,
            branch=branch,
            pos_profile=pos_profile,
        )
    except (frappe.PermissionError, frappe.ValidationError):
        raise
    except Exception as exc:
        # Traceback only: the arguments carry a phone number and the result a PIN.
        frappe.log_error(frappe.get_traceback(), "jarz_courier setup_courier failed")
        frappe.throw(str(exc))


@frappe.whitelist(allow_guest=False)
def reset_courier_pin(user: str) -> Dict[str, Any]:
    """Issue a new PIN for an existing courier login."""
    try:
        return courier_onboarding.reset_courier_pin(user)
    except (frappe.PermissionError, frappe.ValidationError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier reset_courier_pin failed")
        frappe.throw(str(exc))


@frappe.whitelist(allow_guest=False)
def get_courier_setup(user: str) -> Dict[str, Any]:
    """How a login is wired up as a courier, and what is missing."""
    try:
        return courier_onboarding.get_courier_setup(user)
    except (frappe.PermissionError, frappe.ValidationError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_courier_setup failed")
        frappe.throw(str(exc))

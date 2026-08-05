"""Whitelisted endpoints for courier device binding.

Thin transport only: the binding rules live in
``jarz_courier.services.device_registry`` and the one-active-device invariant
lives in the ``Courier Device`` controller, so a Desk edit obeys it too.

Follows the ``jarz_pos/api/returns.py`` template — explicit ``allow_guest=False``,
a module-private permission check as the first statement of every endpoint, a
``{"success": bool}`` envelope, and ``except frappe.PermissionError: raise``
*before* the generic handler so a 403 surfaces as a 403 instead of being flattened
into ``{"success": False}`` that the client renders as a retryable error.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_courier.constants import ROLES
from jarz_courier.services import courier_onboarding, device_registry


def _ensure_device_permission() -> None:
    """Only a courier (or a supervisor acting for one) may touch device bindings."""
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SELF | ROLES.COURIER_SUPERVISOR):
        frappe.throw(_("You are not permitted to manage courier devices"), frappe.PermissionError)


def _ensure_device_supervisor_permission() -> None:
    """Force-unbinding someone else's handset is a supervisor action."""
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SUPERVISOR):
        frappe.throw(
            _("Only a manager can unbind another courier's device"), frappe.PermissionError
        )


@frappe.whitelist(allow_guest=False)
def register_device(
    device_id: str,
    fcm_token: Optional[str] = None,
    app_version: Optional[str] = None,
    os_version: Optional[str] = None,
    device_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Bind this handset to the signed-in courier, or refresh an existing binding.

    Called on every cold start of the courier app so a rotated FCM token reaches
    the server; re-registering a known handset updates it rather than creating a
    second row.
    """
    _ensure_device_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="device registration")
        result = device_registry.register_device(
            party_type=identity["party_type"],
            party=identity["party"],
            device_id=device_id,
            user=frappe.session.user,
            fcm_token=fcm_token,
            app_version=app_version,
            os_version=os_version,
            device_model=device_model,
        )
        return {"success": True, "branch": identity.get("branch"), **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier register_device failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_my_device() -> Dict[str, Any]:
    """The signed-in courier's currently bound handset, plus a setup diagnosis.

    The diagnosis rides along deliberately: this is the first call the app makes
    after login, so a courier whose Employee branch and POS Profile assignment
    disagree learns it here with the fix in the message, instead of meeting an
    empty run sheet later.
    """
    _ensure_device_permission()
    try:
        diagnosis = courier_onboarding.diagnose_courier_setup()
        device = None
        if diagnosis.get("party"):
            device = device_registry.get_active_device(
                diagnosis["party_type"], diagnosis["party"]
            )
        return {"success": True, "device": device, "setup": diagnosis}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_my_device failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def unbind_device(name: Optional[str] = None) -> Dict[str, Any]:
    """Release a device binding.

    Unbinding *your own* active device needs only the courier role — that is the
    "sign out on this phone" affordance. Naming a specific row that is not yours
    is a supervisor action (a courier lost their handset and a manager has to free
    the courier to bind another).
    """
    _ensure_device_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="device unbinding")

        target = str(name or "").strip()
        if not target:
            active = device_registry.get_active_device(identity["party_type"], identity["party"])
            if not active:
                return {"success": True, "changed": False, "device": None}
            target = active["name"]
        else:
            owner = frappe.db.get_value(
                "Courier Device", target, ["party_type", "party"], as_dict=True
            )
            if not owner:
                frappe.throw(_("Courier Device {0} not found").format(target))
            if (
                owner.get("party_type") != identity["party_type"]
                or owner.get("party") != identity["party"]
            ):
                _ensure_device_supervisor_permission()

        return {"success": True, **device_registry.unbind_device(name=target)}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier unbind_device failed")
        return {"success": False, "error": str(exc)}

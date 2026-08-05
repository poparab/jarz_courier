"""Courier account wiring — and a readable error when it is wrong.

A courier account only works when **two independent things** are true:

1. There is a ``POS Profile User`` row linking the login to a POS Profile.
   ``jarz_pos.utils.access_control.get_user_pos_profiles`` reads exactly this, and
   returns ``[]`` when it is missing. Every branch-scoped query then filters on an
   empty list, so the run sheet renders as "no stops" — indistinguishable from a
   genuinely empty day.
2. The courier's party record carries ``branch = <that same POS Profile name>``.
   ``jarz_pos.utils.courier_visibility.assert_courier_matches_pos_profile`` compares
   the Employee's ``branch`` string against the POS Profile name and throws
   ``"Courier X belongs to POS Profile A, not B"`` — or, when ``branch`` is blank,
   ``"Courier X has no branch and cannot be assigned"``. Neither message tells the
   person reading it *where* to fix it.

The two are set in different places by different people (a POS Profile's user
table vs. the HR Employee form), so they drift constantly, and the two failure
modes look nothing alike: (1) is silent, (2) is a cryptic 417. This module
collapses both into one diagnosis with the fix in it.

Read-only: it inspects configuration and reports. It never repairs anything —
adding a user to a branch is a permission decision, not an error-recovery step.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_courier.services import pos_bridge


class CourierSetupError(frappe.ValidationError):
    """Raised when a courier login is not wired up to a branch correctly.

    Its own class so the client can show the setup instructions rather than a
    generic failure, and so a support engineer greping logs can count them.
    """


def diagnose_courier_setup(user: Optional[str] = None) -> Dict[str, Any]:
    """Describe how *user* is (or is not) wired up as a courier. Never throws.

    Returns::

        {
            "ok": bool,
            "user": str,
            "party_type": str, "party": str, "display_name": str,
            "employee_branch": str,        # Employee.branch
            "pos_profiles": [str],         # POS Profile User rows
            "branch": str,                 # the resolved, usable branch (or "")
            "problems": [str],             # machine codes, see below
            "message": str,                # one human sentence, already translated
        }

    Problem codes: ``no_identity``, ``no_pos_profile``, ``no_employee_branch``,
    ``branch_not_assigned``.
    """
    resolved_user = str(user or frappe.session.user or "").strip()

    result: Dict[str, Any] = {
        "ok": False,
        "user": resolved_user,
        "party_type": "",
        "party": "",
        "display_name": "",
        "employee_branch": "",
        "pos_profiles": [],
        "branch": "",
        "problems": [],
        "message": "",
    }

    identity: Dict[str, Any] = {}
    try:
        identity = pos_bridge.resolve_courier_identity(resolved_user) or {}
    except Exception:
        result["problems"].append("no_identity")

    result["party_type"] = str(identity.get("party_type") or "")
    result["party"] = str(identity.get("party") or "")
    result["display_name"] = str(identity.get("display_name") or result["party"])
    result["employee_branch"] = str(identity.get("branch") or "").strip()

    profiles: List[str] = []
    try:
        profiles = pos_bridge.get_user_pos_profiles(resolved_user)
    except Exception:
        profiles = []
    result["pos_profiles"] = profiles

    if "no_identity" not in result["problems"]:
        if not profiles:
            result["problems"].append("no_pos_profile")
        if not result["employee_branch"]:
            result["problems"].append("no_employee_branch")
        elif profiles and result["employee_branch"] not in profiles:
            # Only meaningful when the courier IS assigned somewhere. With no
            # assignments at all, "your branch is not in your (empty) list" is the
            # same fact as `no_pos_profile`, and reporting both makes the message
            # picker choose the vaguer of two true statements.
            result["problems"].append("branch_not_assigned")

    if not result["problems"]:
        result["ok"] = True
        result["branch"] = result["employee_branch"]

    result["message"] = _setup_message(result)
    return result


def _setup_message(diagnosis: Dict[str, Any]) -> str:
    """One sentence naming the record to open and the value to put in it."""
    problems = set(diagnosis.get("problems") or [])
    party = diagnosis.get("party") or diagnosis.get("user") or ""
    employee_branch = diagnosis.get("employee_branch") or ""
    profiles = diagnosis.get("pos_profiles") or []

    if not problems:
        return _("Courier account is set up correctly for branch {0}.").format(
            diagnosis.get("branch") or ""
        )

    if "no_identity" in problems:
        return _(
            "{0} is not linked to an Employee record. Open the courier's Employee, "
            "set User ID to this login and Status to Active."
        ).format(diagnosis.get("user") or "")

    if "no_pos_profile" in problems and "no_employee_branch" in problems:
        return _(
            "Courier {0} is not connected to a branch at all. Two records must be "
            "set, both to the same POS Profile name: add the login to the Users "
            "table of the POS Profile, and set Branch on the Employee."
        ).format(party)

    if "no_pos_profile" in problems:
        return _(
            "Courier {0} has Branch {1} on their Employee record but is not in that "
            "POS Profile's Users table. Open POS Profile {1} and add this login to "
            "Applicable for Users."
        ).format(party, employee_branch)

    if "no_employee_branch" in problems:
        return _(
            "Courier {0} is assigned to POS Profile {1} but their Employee record "
            "has no Branch. Open the Employee and set Branch to {1} — the two must "
            "match exactly."
        ).format(party, ", ".join(profiles) or _("a branch"))

    if "branch_not_assigned" in problems:
        return _(
            "Courier {0} has Branch {1} on their Employee record, but is only "
            "assigned to POS Profile(s) {2}. The Employee Branch and the POS "
            "Profile must be the same name."
        ).format(party, employee_branch, ", ".join(profiles) or _("none"))

    return _("Courier {0} is not set up correctly.").format(party)


def ensure_courier_setup(user: Optional[str] = None, *, action_label: str = "this action") -> Dict[str, Any]:
    """:func:`diagnose_courier_setup`, but throw ``CourierSetupError`` when broken.

    Returns the diagnosis (which carries ``party_type``/``party``/``branch``) so
    callers get identity resolution and validation in one round trip instead of
    asking the same questions twice.
    """
    diagnosis = diagnose_courier_setup(user)
    if diagnosis.get("ok"):
        return diagnosis

    frappe.throw(
        _("{0} is not available: {1}").format(action_label, diagnosis.get("message") or ""),
        CourierSetupError,
        title=_("Courier Account Not Set Up"),
    )
    return diagnosis  # unreachable


def resolve_active_branch(
    requested_branch: Optional[str] = None,
    *,
    user: Optional[str] = None,
    action_label: str = "this action",
) -> Dict[str, Any]:
    """Validate setup and pick the branch to operate on.

    A courier normally has exactly one branch. ``requested_branch`` exists for the
    rare multi-branch courier and is validated against the diagnosis rather than
    trusted — a client-supplied branch that the courier's Employee record does not
    carry is a scoping bypass, not a preference.
    """
    diagnosis = ensure_courier_setup(user, action_label=action_label)
    branch = str(requested_branch or "").strip() or str(diagnosis.get("branch") or "")

    allowed = set(diagnosis.get("pos_profiles") or [])
    if branch not in allowed or branch != diagnosis.get("employee_branch"):
        frappe.throw(
            _(
                "Branch {0} is not this courier's branch. {1} is limited to {2}."
            ).format(branch or _("(none)"), action_label, diagnosis.get("employee_branch") or _("none")),
            frappe.PermissionError,
            title=_("Wrong Branch"),
        )

    diagnosis["branch"] = branch
    return diagnosis

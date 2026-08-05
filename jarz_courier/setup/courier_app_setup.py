"""Idempotent, create-only seeding for the courier app.

Runs as an ``after_migrate`` hook. **Every path in this module swallows its
exceptions** — COURIER_CONTRACTS.md §9. ``bench migrate`` is shared with
``jarz_pos``: a seeder that raises here aborts jarz_pos's account seeding, its
workspace rebuild and its returned-board reconciliation too, on a server where
the only visible symptom is a failed deploy.

What it seeds:

* the ``Jarz Courier`` Role, and
* Custom DocPerm rows granting that role (and the manager roles) access to this
  app's four doctypes.

Why the role is not simply listed in the DocType JSON ``permissions`` array:
``DocPerm.role`` is a Link to Role, so a JSON permission naming a role that does
not exist yet fails link validation during the very migrate that would have
created it. That is a first-install-only failure, which is the worst kind — it
passes on every dev machine that has migrated once before. Seeding the role and
attaching Custom DocPerms afterwards has no such ordering problem.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

import frappe

COURIER_ROLE = "Jarz Courier"

#: (doctype, role, permlevel-0 rights). Couriers read and create their own
#: records; they never delete, and they never write a Sales Invoice — every
#: invoice write is a call into jarz_pos.
_DOCTYPE_PERMISSIONS: Dict[str, List[Dict[str, object]]] = {
    "Courier Device": [
        {"role": COURIER_ROLE, "read": 1, "write": 1, "create": 1},
        {"role": "JARZ Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1},
        {"role": "jarz line manager", "read": 1, "write": 1, "report": 1},
    ],
    "Courier Duty": [
        {"role": COURIER_ROLE, "read": 1, "write": 1, "create": 1},
        {"role": "JARZ Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1},
        {"role": "jarz line manager", "read": 1, "write": 1, "report": 1},
    ],
    "Delivery Proof": [
        {"role": COURIER_ROLE, "read": 1, "write": 1, "create": 1},
        {"role": "JARZ Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1},
        {"role": "jarz line manager", "read": 1, "report": 1},
    ],
    "Courier Deposit Declaration": [
        # Couriers create and read declarations. They do NOT get delete: a
        # declaration a manager has already seen must not be able to vanish.
        {"role": COURIER_ROLE, "read": 1, "write": 1, "create": 1},
        {"role": "JARZ Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1},
        {"role": "jarz line manager", "read": 1, "write": 1, "report": 1},
    ],
}

_PERM_FLAGS = ("read", "write", "create", "delete", "submit", "cancel", "amend", "report", "export", "share", "print", "email")


def ensure_courier_app_setup() -> None:
    """Entry point for ``after_migrate`` / ``after_install``. Never raises."""
    for step in (_ensure_courier_role, _ensure_doctype_permissions):
        try:
            step()
        except Exception:
            _log(f"jarz_courier setup step {step.__name__} failed")


def _log(title: str) -> None:
    try:
        frappe.log_error(frappe.get_traceback(), title)
    except Exception:
        # Logging itself can fail mid-migrate (no site context, table missing).
        # Losing the log entry is acceptable; aborting the migrate is not.
        pass


def _ensure_courier_role() -> None:
    """Create the ``Jarz Courier`` Role if absent. Never modifies an existing one."""
    if frappe.db.exists("Role", COURIER_ROLE):
        return

    doc = frappe.get_doc(
        {
            "doctype": "Role",
            "role_name": COURIER_ROLE,
            "desk_access": 0,
            "is_custom": 1,
        }
    )
    doc.insert(ignore_permissions=True)


def _ensure_doctype_permissions() -> None:
    """Attach Custom DocPerm rows for roles that exist. Create-only."""
    for doctype, rules in _DOCTYPE_PERMISSIONS.items():
        if not frappe.db.exists("DocType", doctype):
            # The app's own doctype has not synced yet (first migrate ordering).
            # after_migrate runs again on the next deploy; nothing is lost.
            continue
        for rule in rules:
            try:
                _ensure_custom_docperm(doctype, rule)
            except Exception:
                _log(f"jarz_courier: perm {doctype}/{rule.get('role')} failed")


def _ensure_custom_docperm(doctype: str, rule: Dict[str, object]) -> None:
    role = str(rule.get("role") or "")
    if not role or not frappe.db.exists("Role", role):
        # A jarz_pos-owned role (JARZ Manager / jarz line manager) that is not on
        # this site. Skipped rather than created: inventing another app's role
        # would put a role with no permissions anywhere onto the site.
        return

    existing = frappe.db.exists(
        "Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0}
    )
    if existing:
        return

    values: Dict[str, object] = {
        "doctype": "Custom DocPerm",
        "parent": doctype,
        "parenttype": "DocType",
        "parentfield": "permissions",
        "role": role,
        "permlevel": 0,
    }
    for flag in _PERM_FLAGS:
        values[flag] = 1 if rule.get(flag) else 0

    frappe.get_doc(values).insert(ignore_permissions=True)


def _iter_roles() -> Iterable[str]:
    """Every role this app grants anything to. Used by tests and diagnostics."""
    seen = set()
    for rules in _DOCTYPE_PERMISSIONS.values():
        for rule in rules:
            role = str(rule.get("role") or "")
            if role and role not in seen:
                seen.add(role)
                yield role

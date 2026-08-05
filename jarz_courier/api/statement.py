"""Whitelisted endpoints for the courier statement and cash hand-over.

**Read-only over the ledger. The declaration is the only write.**
(COURIER_APP_SPEC.md §4 B6, COURIER_CONTRACTS.md §9.)

The split across two service modules is load-bearing, not cosmetic:

* ``services/ledger_read`` is the only file in this app allowed to name a ledger
  doctype, and ``tests/test_no_gl_writes`` additionally forbids *every* mutating
  call inside it — it is structurally incapable of writing anything.
* ``services/deposits`` writes only ``Courier Deposit Declaration``, a doctype this
  app owns, and never names a ledger doctype at all.

So confirming a deposit cannot post money here even by accident: it calls
``jarz_pos`` through ``services/pos_bridge``, and records the reference jarz_pos
hands back. The GL audit suite covers jarz_pos; anything posted from this app
would be untested money logic and a second source of truth.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_courier.constants import DEPOSIT_STATUS, QUERY_LIMITS, ROLES
from jarz_courier.services import courier_onboarding, deposits, duty_session, ledger_read, pos_bridge


def _ensure_statement_permission() -> None:
    """Reading a statement is a courier-or-supervisor action."""
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SELF | ROLES.COURIER_SUPERVISOR):
        frappe.throw(_("You are not permitted to view a courier statement"), frappe.PermissionError)


def _ensure_deposit_supervisor_permission() -> None:
    """Confirming or rejecting a hand-over is deliberately NOT a courier right.

    A courier confirming their own declaration is the exact failure the
    declaration exists to prevent, so ``ROLES.COURIER`` is absent from
    ``COURIER_SUPERVISOR`` and a courier who is also a manager passes on the
    manager role, not the courier one.
    """
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SUPERVISOR):
        frappe.throw(
            _("Only a manager can confirm or reject a courier deposit"), frappe.PermissionError
        )


@frappe.whitelist(allow_guest=False)
def get_statement(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    """The courier's My Account payload. Strictly read-only.

    Unsettled balance, collected today, fees earned, deductions, and settlement
    history with the Journal Entry each one posted.
    """
    _ensure_statement_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="the courier statement")
        statement = ledger_read.build_statement(
            party_type=identity["party_type"],
            party=identity["party"],
            from_date=from_date,
            to_date=to_date,
        )
        statement["declarations"] = deposits.list_declarations(
            party_type=identity["party_type"],
            party=identity["party"],
            limit=QUERY_LIMITS.SETTLEMENT_HISTORY,
        )
        return {"success": True, "statement": statement}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_statement failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def declare_deposit(
    amount: float,
    method: str,
    reference: Optional[str] = None,
    photo: Optional[str] = None,
    request_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a hand-over claim. **The only write this module makes.**

    Posts nothing. Idempotent on ``request_id`` so an offline replay produces one
    claim, not two for a manager to confirm twice.
    """
    _ensure_statement_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="declaring a deposit")
        open_duty = duty_session.get_open_duty(identity["party_type"], identity["party"])

        result = deposits.declare(
            party_type=identity["party_type"],
            party=identity["party"],
            branch=identity["branch"],
            amount=amount,
            method=method,
            reference=reference,
            photo=photo,
            duty=(open_duty or {}).get("name"),
            request_id=request_id,
            notes=notes,
        )
        return {"success": True, **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier declare_deposit failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def confirm_deposit(name: str) -> Dict[str, Any]:
    """Manager approves a hand-over; **jarz_pos** posts it.

    The response carries ``declared_amount`` and ``settled_net`` separately, plus
    ``amount_matches``. They can differ — the jarz_pos settlement primitive settles
    the courier's whole unsettled balance, so a partial hand-over declares less
    than gets posted. Surfacing the difference is the point: silently reconciling
    it would post a number nobody typed.
    """
    _ensure_deposit_supervisor_permission()
    try:
        _assert_declaration_branch_access(name, action_label="confirming a deposit")
        return {"success": True, **deposits.confirm(name=name)}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier confirm_deposit failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def reject_deposit(name: str, reason: str) -> Dict[str, Any]:
    """Manager refuses a hand-over claim. Posts nothing, unwinds nothing."""
    _ensure_deposit_supervisor_permission()
    try:
        _assert_declaration_branch_access(name, action_label="rejecting a deposit")
        return {"success": True, **deposits.reject(name=name, reason=reason)}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier reject_deposit failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def list_pending_deposits(branch: Optional[str] = None) -> Dict[str, Any]:
    """The manager's confirmation queue, scoped to their branches.

    Never unscoped: an empty branch list returns nothing rather than everything,
    so a scoping lookup that fails cannot put another branch's cash on screen.
    """
    _ensure_deposit_supervisor_permission()
    try:
        allowed = pos_bridge.get_user_pos_profiles()
        requested = str(branch or "").strip()
        if requested:
            if requested not in allowed:
                frappe.throw(
                    _("You are not assigned to branch {0}").format(requested),
                    frappe.PermissionError,
                )
            allowed = [requested]

        return {
            "success": True,
            "branches": allowed,
            "declarations": deposits.list_declarations(
                branches=allowed,
                status=DEPOSIT_STATUS.PENDING,
                limit=QUERY_LIMITS.PENDING_DEPOSITS,
            ),
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier list_pending_deposits failed")
        return {"success": False, "error": str(exc)}


def _assert_declaration_branch_access(name: str, *, action_label: str) -> Dict[str, Any]:
    """A manager confirms their own branch's cash, never another branch's."""
    declaration = str(name or "").strip()
    if not declaration:
        frappe.throw(_("Declaration is required"))

    row = frappe.db.get_value(
        "Courier Deposit Declaration", declaration, ["name", "branch", "status"], as_dict=True
    )
    if not row:
        frappe.throw(_("Courier Deposit Declaration {0} not found").format(declaration))

    branch = str(row.get("branch") or "").strip()
    if not branch:
        # A declaration with no branch predates branch stamping or was created by
        # hand. Fall through rather than block: an unscoped legacy row is a data
        # problem for a manager to fix, not a reason to strand the courier's cash.
        return row

    allowed = set(pos_bridge.get_user_pos_profiles())
    if branch not in allowed:
        frappe.throw(
            _("{0} is limited to your branches. Declaration {1} belongs to {2}.").format(
                action_label, declaration, branch
            ),
            frappe.PermissionError,
            title=_("Wrong Branch"),
        )
    return row

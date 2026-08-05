"""The one place ``jarz_courier`` reaches into ``jarz_pos``.

Why a single module rather than direct imports scattered across ``api/`` and
``services/``:

* **The boundary is auditable.** COURIER_CONTRACTS.md §9 makes the dependency
  one-way and makes every money write a call into jarz_pos. Keeping the calls in
  one file means "what does the courier app ask jarz_pos to do?" is answered by
  reading ~200 lines, not by grepping the tree.
* **The imports are lazy on purpose.** Two of the targets
  (``services.courier_delivery``, ``services.courier_identity``) are lane A3/A5
  deliverables that may not be present on a bench that has only pulled this app.
  A module-level import would turn "jarz_pos is a commit behind" into an
  ImportError at *site boot*, taking the POS down with it. A lazy import turns
  the same situation into one clear error on one courier endpoint.
* **It is the seam the unit tests patch.** Every test in ``tests/`` mocks
  functions here, so no test needs a site, a database, or jarz_pos installed.

Nothing in this module writes to the ledger. It *delegates* to jarz_pos, which
owns the GL, the Journal Entries and the ``Courier Transaction`` rows, and whose
GL audit suite is the only thing testing them.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, Iterable, Optional, Sequence

import frappe
from frappe import _

#: jarz_pos modules this app is allowed to call, with the lane that owns each.
#: Used to build an actionable error when one is missing rather than surfacing a
#: bare ``ModuleNotFoundError`` to a courier standing at a customer's door.
_MODULE_OWNERS = {
    "jarz_pos.services.courier_delivery": "jarz_pos lane A3 (per-invoice courier transitions)",
    "jarz_pos.services.courier_identity": "jarz_pos lane A5 (courier identity resolution)",
    "jarz_pos.services.delivery_handling": "jarz_pos (delivery + settlement primitives)",
    "jarz_pos.utils.access_control": "jarz_pos (branch scoping / shift enforcement)",
    "jarz_pos.utils.courier_visibility": "jarz_pos (courier ↔ POS Profile matching)",
    "jarz_pos.utils.realtime": "jarz_pos (branch-scoped realtime publishing)",
}


class PosBridgeUnavailable(frappe.ValidationError):
    """Raised when a required jarz_pos module or function is not deployed.

    Its own class so the Flutter client can tell "the server is behind, retry
    after the next deploy" apart from "you did something wrong", instead of
    showing a generic red toast for a deploy-ordering problem.
    """


def _module(dotted: str):
    """Import a jarz_pos module, or throw naming the lane that owns it."""
    try:
        return importlib.import_module(dotted)
    except Exception as exc:  # pragma: no cover - exercised via _call
        owner = _MODULE_OWNERS.get(dotted, "jarz_pos")
        frappe.throw(
            _(
                "{0} is not available on this server yet (owned by {1}). "
                "The courier app needs a newer jarz_pos deploy."
            ).format(dotted, owner),
            PosBridgeUnavailable,
            title=_("Server Out Of Date"),
        )
        raise exc  # unreachable; keeps type checkers happy


def _call(dotted: str, func_name: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``<dotted>.<func_name>``, throwing a clear error if either is missing."""
    mod = _module(dotted)
    func = getattr(mod, func_name, None)
    if not callable(func):
        owner = _MODULE_OWNERS.get(dotted, "jarz_pos")
        frappe.throw(
            _(
                "{0}.{1}() is not available on this server yet (owned by {2}). "
                "The courier app needs a newer jarz_pos deploy."
            ).format(dotted, func_name, owner),
            PosBridgeUnavailable,
            title=_("Server Out Of Date"),
        )
    return func(*args, **kwargs)


# ---------------------------------------------------------------------------
# Identity — who is the logged-in user, as a courier?
# ---------------------------------------------------------------------------

def resolve_courier_identity(user: Optional[str] = None) -> Dict[str, Any]:
    """Return ``{"party_type", "party", "branch", ...}`` for *user*.

    Delegates to ``jarz_pos.services.courier_identity`` (lane A5), which resolves
    ``Employee.user_id`` → courier party and the party's branch. Falls back to a
    direct Employee lookup when that module is not deployed yet, because the run
    sheet is unusable without an identity and the fallback asks the database
    exactly the same question ``courier_visibility.resolve_courier_branch`` does.
    """
    resolved_user = str(user or frappe.session.user or "").strip()
    if not resolved_user or resolved_user == "Guest":
        frappe.throw(_("Not signed in"), frappe.PermissionError)

    try:
        identity = _call(
            "jarz_pos.services.courier_identity",
            "resolve_courier_identity",
            user=resolved_user,
        )
        if identity:
            return dict(identity)
    except PosBridgeUnavailable:
        pass

    return _fallback_identity(resolved_user)


def _fallback_identity(user: str) -> Dict[str, Any]:
    """``Employee.user_id`` → courier party, using only stock Employee fields.

    Deliberately Employee-only. A Supplier courier (a 3PL) has no login, so a
    session user can never resolve to one; treating a missing Employee as
    "maybe a Supplier" would only produce a vaguer error message.
    """
    row = frappe.db.get_value(
        "Employee",
        {"user_id": user, "status": "Active"},
        ["name", "employee_name", "branch"],
        as_dict=True,
    )
    if not row:
        frappe.throw(
            _(
                "No active Employee record is linked to {0}. A courier login must "
                "have an Employee whose User ID is this account."
            ).format(user),
            frappe.PermissionError,
            title=_("Not A Courier"),
        )

    return {
        "user": user,
        "party_type": "Employee",
        "party": row.get("name"),
        "display_name": row.get("employee_name") or row.get("name"),
        "branch": str(row.get("branch") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Branch scoping and shift enforcement (jarz_pos.utils.access_control)
# ---------------------------------------------------------------------------

def get_user_pos_profiles(user: Optional[str] = None) -> list[str]:
    """Enabled POS Profiles the user is assigned to (POS Profile User rows)."""
    return list(_call("jarz_pos.utils.access_control", "get_user_pos_profiles", user) or [])


def ensure_user_pos_profiles(*, action_label: str, user: Optional[str] = None) -> list[str]:
    """Like :func:`get_user_pos_profiles` but throws when the user has no branch."""
    return list(
        _call(
            "jarz_pos.utils.access_control",
            "ensure_user_pos_profiles",
            user,
            action_label=action_label,
        )
        or []
    )


def ensure_profile_scoped_invoice_access(invoice: Any, *, action_label: str) -> None:
    """Assert the current user may act on *invoice*'s branch."""
    _call(
        "jarz_pos.utils.access_control",
        "ensure_profile_scoped_invoice_access",
        invoice,
        action_label=action_label,
    )


def get_invoice_branch(invoice: Any) -> str:
    """``custom_kanban_profile`` first, ``pos_profile`` as fallback."""
    return str(_call("jarz_pos.utils.access_control", "get_invoice_branch", invoice) or "")


# ---------------------------------------------------------------------------
# Realtime — never `frappe.publish_realtime` (COURIER_CONTRACTS.md §5.7)
# ---------------------------------------------------------------------------

def publish_to_branches(
    event: str,
    payload: Dict[str, Any],
    profiles: Sequence[str],
    *,
    extra_users: Optional[Iterable[str]] = None,
    after_commit: bool = False,
) -> list[str]:
    """Emit *event* to the users assigned to *profiles*.

    Routed through ``jarz_pos.utils.realtime`` because a bare
    ``frappe.publish_realtime`` either broadcasts site-wide (no ``user``) or
    addresses a room nobody joined (``user="*"`` / a list). Both bugs have
    already been paid for once in jarz_pos; this app must not reintroduce them.
    Failure to publish is never fatal — the client polls as a fallback.
    """
    try:
        return list(
            _call(
                "jarz_pos.utils.realtime",
                "publish_to_branches",
                event,
                payload,
                profiles,
                extra_users=extra_users,
                after_commit=after_commit,
            )
            or []
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"jarz_courier: publish {event} failed")
        return []


# ---------------------------------------------------------------------------
# Delivery outcome writes (COURIER_CONTRACTS.md §5 — signatures are FROZEN)
# ---------------------------------------------------------------------------
#
# All three return the `{"success": bool, ...}` envelope and raise nothing except
# frappe.PermissionError. Idempotency, the meta assertion, the access gate, the
# feature flag and the realtime publish all live on the jarz_pos side — this app
# passes arguments through and does not second-guess any of it.

def mark_invoice_arrived(
    invoice_id: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    request_id: str | None = None,
) -> Dict[str, Any]:
    return _call(
        "jarz_pos.services.courier_delivery",
        "mark_invoice_arrived",
        invoice_id,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        request_id=request_id,
    )


def mark_invoice_delivered(
    invoice_id: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    collected_amount: float | None = None,
    recipient_name: str | None = None,
    is_mocked: bool = False,
    request_id: str | None = None,
) -> Dict[str, Any]:
    return _call(
        "jarz_pos.services.courier_delivery",
        "mark_invoice_delivered",
        invoice_id,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        collected_amount=collected_amount,
        recipient_name=recipient_name,
        is_mocked=is_mocked,
        request_id=request_id,
    )


def mark_invoice_failed(
    invoice_id: str,
    *,
    failure_reason: str,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    notes: str | None = None,
    request_id: str | None = None,
) -> Dict[str, Any]:
    return _call(
        "jarz_pos.services.courier_delivery",
        "mark_invoice_failed",
        invoice_id,
        failure_reason=failure_reason,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        notes=notes,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Money — the deposit hand-over
# ---------------------------------------------------------------------------

def settle_courier_deposit(
    *,
    party_type: str,
    party: str,
    pos_profile: str | None = None,
    amount: float | None = None,
    method: str | None = None,
    reference: str | None = None,
    declaration: str | None = None,
) -> Dict[str, Any]:
    """Post the cash hand-over. **jarz_pos does the posting; this app never does.**

    Preferred target is ``jarz_pos.services.courier_delivery.settle_courier_deposit``
    — a courier-app-shaped wrapper that knows about the declaration. That function
    is NOT in the frozen §5 signature list, so until lane A3 ships it we fall back
    to the settlement primitive that already exists and is already covered by the
    GL audit suite: ``delivery_handling.settle_delivery_party(party_type, party,
    pos_profile)``, which settles the courier's whole unsettled balance and returns
    the Journal Entry it created.

    The fallback settles the *balance*, not the declared amount — a partial
    hand-over is therefore not expressible through it. ``api/statement.py``
    surfaces that difference to the confirming manager rather than silently
    posting a number nobody typed. See the report note on this ambiguity.
    """
    try:
        return _call(
            "jarz_pos.services.courier_delivery",
            "settle_courier_deposit",
            party_type=party_type,
            party=party,
            pos_profile=pos_profile,
            amount=amount,
            method=method,
            reference=reference,
            declaration=declaration,
        )
    except PosBridgeUnavailable:
        pass

    return _call(
        "jarz_pos.services.delivery_handling",
        "settle_delivery_party",
        party_type=party_type,
        party=party,
        pos_profile=pos_profile,
    )


# ---------------------------------------------------------------------------
# Courier ↔ branch consistency (jarz_pos.utils.courier_visibility)
# ---------------------------------------------------------------------------

def resolve_courier_branch(party_type: str, party: str) -> str:
    """The POS Profile name stored on the courier party's ``branch`` field."""
    return str(
        _call("jarz_pos.utils.courier_visibility", "resolve_courier_branch", party_type, party) or ""
    )


def assert_courier_matches_pos_profile(
    party_type: str,
    party: str,
    pos_profile: str,
    *,
    require_active: bool = True,
) -> Dict[str, str]:
    """Assert the courier party's branch is *pos_profile* and the party is active."""
    return dict(
        _call(
            "jarz_pos.utils.courier_visibility",
            "assert_courier_matches_pos_profile",
            party_type,
            party,
            pos_profile,
            require_active=require_active,
        )
        or {}
    )

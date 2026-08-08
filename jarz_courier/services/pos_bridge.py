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
    "jarz_pos.api.notifications": "jarz_pos (Firebase/FCM credentials and readiness)",
    "jarz_pos.services.courier_delivery": "jarz_pos lane A3 (per-invoice courier transitions)",
    "jarz_pos.services.courier_identity": "jarz_pos lane A5 (courier identity resolution)",
    "jarz_pos.services.delivery_handling": "jarz_pos (delivery + settlement primitives)",
    "jarz_pos.services.geo_resolution": "jarz_pos lane A4 (sole writer of the Address geo fields)",
    "jarz_pos.utils.access_control": "jarz_pos (branch scoping / shift enforcement)",
    "jarz_pos.utils.courier_visibility": "jarz_pos (courier ↔ POS Profile matching)",
    "jarz_pos.utils.geo": "jarz_pos (the §4 confidence ladder)",
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

    # ``ensure_courier_party``, not ``resolve_courier_identity``. The latter name
    # was assumed before lane A5 shipped and does not exist; the real module
    # exposes resolve_courier_party (None when there is no Employee) and
    # ensure_courier_party (throws instead). We want the throwing one: a login
    # with no Employee record cannot have a run, and returning an empty identity
    # would surface as an inexplicably blank run sheet rather than the actual
    # cause, which is that nobody set Employee.user_id.
    #
    # The mismatch cost nothing only because _call() raises PosBridgeUnavailable
    # for a missing attribute and the fallback below answers the same question.
    # It still meant every call logged a spurious "server out of date" warning.
    try:
        identity = _call(
            "jarz_pos.services.courier_identity",
            "ensure_courier_party",
            user=resolved_user,
        )
        if identity:
            # jarz_pos returns the Employee row; party_type is implicit there
            # because only an Employee can hold a login (a 3PL Supplier courier
            # has none), but this app's callers read party_type explicitly.
            resolved = dict(identity)
            resolved.setdefault("party_type", "Employee")
            resolved.setdefault("party", resolved.get("name") or "")
            return resolved
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

def resolve_branch_recipients(
    profiles: Sequence[str],
    *,
    extra_users: Optional[Iterable[str]] = None,
) -> list[str]:
    """Users assigned to *profiles*. The audience for a branch-scoped alert.

    Needed separately from :func:`publish_to_branches` because a push notification
    and a websocket event go to the same people by two different transports, and
    resolving the audience twice with two different rules is how a courier alert
    reaches half the managers.
    """
    try:
        return list(
            _call(
                "jarz_pos.utils.realtime",
                "resolve_branch_recipients",
                profiles,
                extra_users=extra_users,
            )
            or []
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: recipient resolution failed")
        return []


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


# ---------------------------------------------------------------------------
# Address geo — READ freely, WRITE only through jarz_pos (CONTRACTS §3)
# ---------------------------------------------------------------------------
#
# Contract §3 names exactly two authorised writers of the six Address geo fields,
# and this app is not one of them. So the consensus-pin job (spec B5) does not
# write an Address; it asks `geo_resolution.set_address_pin` to, and that function
# applies the never-downgrade ladder, the manual-override role gate, the
# "accuracy must never outlive its pin" rule and the "never touch a Woo trigger
# field" guard. Reimplementing four rules here to save one function call is how
# the ladder stops being enforceable.
#
# For the same reason this app carries NO copy of §4's CONFIDENCE_RANK. Every rank
# question goes through `confidence_rank` below, so there is nothing local to drift.


def set_address_pin(
    address_name: str,
    *,
    latitude: Any,
    longitude: Any,
    source: str,
    accuracy_m: Any = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Ask jarz_pos to write a pin. It may refuse, and a refusal is not an error.

    Returns ``{"success": True, "accepted": bool, ...}``. ``accepted=False`` means
    the address already carries an equal-or-better pin — normal, and the reason the
    consensus job can run every night without fighting a manager's manual override.
    """
    return _call(
        "jarz_pos.services.geo_resolution",
        "set_address_pin",
        address_name,
        latitude=latitude,
        longitude=longitude,
        source=source,
        accuracy_m=accuracy_m,
        note=note,
    )


def get_address_geo(address_name: str) -> Dict[str, Any]:
    """Current geo state of an Address plus its derived ``rank``. Read-only.

    Falsy ``{}`` means "no such Address" — distinct from an Address that exists and
    has no pin yet, which is the normal first-write case.
    """
    return dict(
        _call("jarz_pos.services.geo_resolution", "get_address_geo", address_name) or {}
    )


def confidence_rank(source: Any) -> int:
    """Integer rank of a §4 source label. 0 for anything unrecognised.

    Ranks, never string comparison: ``courier_verified`` sorts *below*
    ``customer_pin`` alphabetically and ``pos_link`` sorts *above*
    ``manual_override``, so a lexicographic "is this better?" inverts the ladder for
    two of the five sources — silently.
    """
    try:
        return int(_call("jarz_pos.utils.geo", "confidence_rank", source) or 0)
    except PosBridgeUnavailable:
        # Rank is only used to skip addresses that are already good enough. Failing
        # to that skip-nothing answer costs a wasted evaluation that
        # `set_address_pin` will reject anyway; guessing a number here could skip an
        # address that needed the pin.
        return 0


def accuracy_is_known(value: Any) -> bool:
    """True when an accuracy figure is a real measurement rather than a default.

    Contract §3 requires this question be asked of jarz_pos rather than by
    comparing the raw number, because ``custom_geo_accuracy_m`` is
    ``NOT NULL DEFAULT 0`` and 0 therefore means "not reported", not "accurate to
    0 m". Reading 0 as a tight radius is how "was this delivered near the pin?"
    reaches a confident wrong answer.
    """
    try:
        return bool(_call("jarz_pos.services.geo_resolution", "accuracy_is_known", value))
    except PosBridgeUnavailable:
        # The rule is three lines and cannot drift (the column default is fixed by
        # Frappe, not by policy). Duplicating it as a fallback is safer than letting
        # a scheduled detector die on a server that is one deploy behind.
        try:
            return float(value or 0) > 0
        except (TypeError, ValueError):
            return False


# ---------------------------------------------------------------------------
# Push readiness — jarz_pos owns the Firebase credentials
# ---------------------------------------------------------------------------

def ensure_push_ready() -> Dict[str, Any]:
    """Initialise the Firebase Admin app if needed and report readiness.

    ``health_check_firebase`` is a public jarz_pos endpoint that initialises the
    SDK as a side effect and returns ``{"ok": bool, "reason": str, ...}``. Calling
    it means the service-account path resolution, the site-private-files fallback
    and the once-per-process failure logging all stay in one place — the place that
    already has tests for them. This app deliberately does not read
    ``fcm_service_account_path`` itself; two resolvers for one credential is how
    push works in one worker and not another.
    """
    try:
        return dict(_call("jarz_pos.api.notifications", "health_check_firebase") or {})
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: push readiness check failed")
        return {"ok": False, "reason": "readiness check failed"}


# ---------------------------------------------------------------------------
# Failure reasons — owned by jarz_pos, rendered by the courier app
# ---------------------------------------------------------------------------

def list_failure_reasons() -> list:
    """Active ``Delivery Failure Reason`` rows, for the app's failure sheet.

    Proxied rather than queried directly. The DocType lives in ``jarz_pos``,
    which owns what "active" means and what ``next_action`` each code implies —
    reading the table from here would duplicate that judgement in a second place
    and let the two drift.

    Degrades to an empty list rather than throwing: a courier who cannot reach
    the reason list must still be able to open the app and deliver the stops that
    are going fine. The sheet renders its own "no reasons available" state.
    """
    try:
        rows = _call("jarz_pos.services.courier_delivery", "list_failure_reasons")
        return list(rows or [])
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "jarz_courier: failure reason lookup failed"
        )
        return []

"""Whitelisted endpoints for location tracking.

Thin transport over ``services/tracking``, following the template in
``jarz_pos/api/returns.py`` (contract §8): a module-private permission check as the
first statement of every endpoint, ``frappe.PermissionError`` re-raised *before* the
generic handler so a scoping failure surfaces as a real 403 instead of being flattened
into a success envelope, and no business logic.

Two things worth knowing before calling these.

**Ingest is deliberately forgiving.** A ping that cannot be parsed is counted and
dropped, not rejected with an error. The client is a phone on a bad connection
replaying an offline queue; a 417 on one malformed fix would make it retry the whole
batch forever, and the fix would still be malformed. Every endpoint reports what it
accepted, deduplicated and refused, so the client can trim its queue and a support
engineer can see the shape of the loss.

**Nothing here writes an invoice, a ledger row or an Address.** A position is
operational telemetry. The delivery outcome still goes through
``api/run.mark_delivered`` → ``jarz_pos.services.courier_delivery``, and the Address
pin still goes through ``jarz_pos.services.geo_resolution``. Tracking informs those
decisions; it does not make them.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_courier.constants import QUERY_LIMITS, ROLES
from jarz_courier.services import courier_onboarding, duty_session, location_cache, pos_bridge, tracking


def _ensure_tracking_permission() -> None:
    """Recording your own position is a courier action."""
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SELF | ROLES.COURIER_SUPERVISOR):
        frappe.throw(
            _("You are not permitted to record a courier position"), frappe.PermissionError
        )


def _ensure_ops_permission() -> None:
    """Watching *other people* on a map is a supervisor action, not a courier one.

    Deliberately excludes ``ROLES.COURIER``. A courier needs to see their own run, not
    the live position of every colleague on the branch — that is a surveillance surface
    with no operational purpose for them, and it is trivially screenshotted.
    """
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.COURIER_SUPERVISOR):
        frappe.throw(
            _("Only a manager can view live courier positions"), frappe.PermissionError
        )


@frappe.whitelist(allow_guest=False)
def ingest_ping(
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    heading: Optional[float] = None,
    speed: Optional[float] = None,
    timestamp: Optional[str] = None,
    is_mocked: Any = False,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one position for the signed-in courier.

    ``timestamp`` is the **handset's** clock and is stored as given, exactly like
    ``Delivery Proof.captured_at``. Overwriting it with the server's would date a
    morning of queued fixes to the moment the courier found signal, collapsing a
    four-hour run into four seconds.

    ``speed`` is metres per second, as Android reports it. No conversion happens
    anywhere in the pipeline; the wire format is documented in
    ``services/location_cache``.
    """
    _ensure_tracking_permission()
    try:
        identity = courier_onboarding.resolve_active_branch(branch, action_label="location tracking")
        open_duty = duty_session.get_open_duty(identity["party_type"], identity["party"])

        result = tracking.ingest(
            party_type=identity["party_type"],
            party=identity["party"],
            branch=identity["branch"],
            pings=[
                {
                    "lat": latitude,
                    "lng": longitude,
                    "accuracy": accuracy_m,
                    "heading": heading,
                    "speed": speed,
                    "ts": timestamp,
                    "is_mocked": is_mocked,
                }
            ],
            duty=(open_duty or {}).get("name"),
        )
        return {"success": True, **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier ingest_ping failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def ingest_pings(pings: Any = None) -> Dict[str, Any]:
    """Flush an offline queue of positions. Batch sibling of :func:`ingest_ping`.

    ``pings`` may be a JSON string (what a Frappe form POST produces for a list) or an
    already-decoded list. Both are accepted because the encoding depends on how the
    client happens to send it, and a courier's backlog is not the place to discover a
    serialisation mismatch.

    The batch may arrive **out of order** and may contain **duplicates** — that is the
    normal shape of a drained queue, not an error:

    * it is sorted by timestamp before anything is stored;
    * the trail is a Redis sorted set, so an old fix slots into its correct position
      rather than being appended after newer ones;
    * fixes are deduplicated by whole-second timestamp, first one winning;
    * the live position only ever moves **forward**, so flushing a tunnel's worth of
      backlog cannot teleport a courier back into the tunnel.

    Anything above ``QUERY_LIMITS.PINGS_PER_BATCH`` is truncated rather than refused:
    a partial flush the client can retry beats a rejected one it cannot.
    """
    _ensure_tracking_permission()
    try:
        batch = _decode_pings(pings)
        if not batch:
            return {"success": True, "received": 0, "accepted": 0, "reason": "empty batch"}

        identity = courier_onboarding.ensure_courier_setup(action_label="location tracking")
        open_duty = duty_session.get_open_duty(identity["party_type"], identity["party"])

        result = tracking.ingest(
            party_type=identity["party_type"],
            party=identity["party"],
            branch=identity["branch"],
            pings=batch,
            duty=(open_duty or {}).get("name"),
        )
        return {"success": True, **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier ingest_pings failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_live_positions(branch: Optional[str] = None) -> Dict[str, Any]:
    """Live courier positions for the ops board. Read-only, Redis only.

    Never unscoped. A branch list that resolves to empty returns nothing rather than
    everything — the same rule the run sheet and the deposit queue follow, and for the
    same reason: widening a query because a scope resolved to nothing is how one
    branch's operation ends up on another branch's screen.

    No database query is involved, so this is safe to poll as a fallback for the
    realtime feed.
    """
    _ensure_ops_permission()
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

        branches: List[Dict[str, Any]] = [tracking.branch_positions(b) for b in allowed]
        return {
            "success": True,
            "branches": branches,
            "count": sum(int(b.get("count") or 0) for b in branches),
            "ttl_seconds": location_cache.LOCATION_TTL_SEC,
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_live_positions failed")
        return {"success": False, "error": str(exc)}


def _decode_pings(payload: Any) -> List[Dict[str, Any]]:
    """A JSON string, a list, or a single dict → a list of dicts."""
    if payload in (None, ""):
        return []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            frappe.throw(_("pings must be a JSON array of position objects"))
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        frappe.throw(_("pings must be a JSON array of position objects"))
    return [item for item in payload if isinstance(item, dict)][: QUERY_LIMITS.PINGS_PER_BATCH]

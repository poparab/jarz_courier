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

try:  # pragma: no cover - import shape depends on the Frappe build
    from frappe.rate_limiter import rate_limit as _frappe_rate_limit
except Exception:  # pragma: no cover
    _frappe_rate_limit = None  # type: ignore[assignment]

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



#: OwnTracks in "significant change" mode reports every ~100 m or 5 minutes; in
#: "move" mode it is far chattier. 120/min per user is generous for the former and
#: still bounds a misconfigured device, a replay of a long offline buffer, or a
#: stolen key pair.
OWNTRACKS_RATE_LIMIT_MAX = 120
OWNTRACKS_RATE_LIMIT_WINDOW_SEC = 60


def _owntracks_rate_limited(fn):
    """Bound this endpoint even though it is authenticated.

    Every other endpoint in this app is called by our own app on a schedule we
    control. This one is called by third-party software configured by hand on a
    courier's phone, where a wrong interval is a typo rather than a code change,
    so the blast radius of a misconfiguration belongs on our side of the wire.

    Keyed on ``tid`` — OwnTracks' two-character tracker id, which it sends on every
    HTTP location report — **with** ``ip_based=True``, giving an identity of
    ``"<ip>:<tid>"``. Couriers on mobile data sit behind carrier NAT and share
    addresses, so an IP-only budget would let one chatty device throttle the rest.

    ``ip_based`` must stay True and the key must stay one OwnTracks actually sends.
    Frappe builds the identity as::

        user_key = frappe.form_dict.get(key, "")
        if key and ip_based: identity = ip + ":" + user_key
        identity = identity or ip or user_key
        if not identity: frappe.throw("Either key or IP flag is required.")

    so ``ip_based=False`` with a key absent from the body yields an empty identity
    and throws on **every** request — a rate limiter that returns 100% errors rather
    than limiting anything. There is no way to key on ``frappe.session.user`` here:
    the decorator only ever reads ``form_dict``, and it runs before the body.
    """
    if _frappe_rate_limit is None:  # pragma: no cover - depends on Frappe build
        return fn
    return _frappe_rate_limit(
        key="tid",
        limit=OWNTRACKS_RATE_LIMIT_MAX,
        seconds=OWNTRACKS_RATE_LIMIT_WINDOW_SEC,
        ip_based=True,
    )(fn)


@frappe.whitelist(allow_guest=False)
@_owntracks_rate_limited
def ingest_owntracks(**kwargs: Any) -> Dict[str, Any]:
    """Accept a position from the OwnTracks iOS app.

    Exists because Safari cannot track in the background at all, so a courier on an
    iPhone runs the courier **web** app for the work and OwnTracks for the location.
    OwnTracks is free, already on the App Store, and POSTs to a URL of your
    choosing — which means no native iOS app, no Apple Developer subscription and
    no 90-day TestFlight re-upload treadmill.

    **Authentication needs no code here.** OwnTracks' HTTP mode has username and
    password fields, and Frappe natively accepts
    ``Authorization: Basic base64(api_key:api_secret)`` (``frappe/auth.py``), so the
    courier authenticates as their *own* User. ``frappe.session.user`` resolves
    normally, the permission check below is the same one the Android app passes, and
    there is no shared secret to store or rotate. CSRF is likewise a non-issue:
    Frappe skips the check when the request carries no session cookie.

    **This is a shim over ``services.tracking.ingest`` and must stay one.** Writing
    to ``location_cache`` directly would put a dot on the ops map and silently
    starve everything else: no trail means no idle, speeding, ping-gap or detour
    findings; no ``Courier Run`` means nothing for the stale-run watchdog to watch
    and no polyline at close. ``ingest`` is what produces all of it.

    Reads ``frappe.form_dict`` rather than declaring the fields as parameters.
    OwnTracks POSTs a bare JSON object, which Frappe loads into ``form_dict``
    wholesale, and its ``_type`` discriminator is a leading-underscore name that
    does not survive being a Python parameter cleanly.

    Always answers 200 for a well-formed request, including for message types we
    do not store. OwnTracks retries a non-2xx indefinitely, so a 4xx on a region
    transition would turn one unsupported message into a permanent hot loop.
    """
    _ensure_tracking_permission()
    try:
        payload = dict(frappe.form_dict or {})
        fix = tracking.owntracks_to_fix(payload)
        if fix is None:
            # Not a location report — a transition, waypoint or last-will message.
            return {
                "success": True,
                "accepted": 0,
                "ignored": str(payload.get("_type") or "unknown"),
            }

        identity = courier_onboarding.ensure_courier_setup(
            action_label="location tracking"
        )

        # Auto-open the duty. An iPhone courier has no foreground service to bind a
        # shift to, and OwnTracks knows nothing about duties, so requiring a manual
        # Start Shift would mean the common failure is "he forgot, and was invisible
        # all day with nothing to tell him". start_duty already returns the open duty
        # when there is one, so this is idempotent; the get_open_duty check just
        # avoids the insert path's extra work on every ping.
        open_duty = duty_session.get_open_duty(identity["party_type"], identity["party"])
        if not open_duty:
            opened = duty_session.start_duty(
                party_type=identity["party_type"],
                party=identity["party"],
                branch=identity["branch"],
            ) or {}
            open_duty = opened.get("duty") or {}

        result = tracking.ingest(
            party_type=identity["party_type"],
            party=identity["party"],
            branch=identity["branch"],
            pings=[fix],
            duty=(open_duty or {}).get("name"),
        )
        return {"success": True, **result}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier ingest_owntracks failed")
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

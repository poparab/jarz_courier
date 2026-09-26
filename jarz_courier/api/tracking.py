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

import base64
import json
import time
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

try:  # pragma: no cover - import shape depends on the Frappe build
    from frappe.rate_limiter import rate_limit as _frappe_rate_limit
except Exception:  # pragma: no cover
    _frappe_rate_limit = None  # type: ignore[assignment]

from jarz_courier.constants import QUERY_LIMITS, ROLES
from jarz_courier.services import (
    courier_onboarding,
    duty_session,
    location_cache,
    owntracks_steering,
    pos_bridge,
    run_sheet,
    tracking,
)


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
    if roles.isdisjoint(ROLES.COURIER_MAP_VIEWER):
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
def ingest_owntracks(**kwargs: Any) -> Any:
    """Accept a message from the OwnTracks iOS app and answer with commands.

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

    **The response body is a bare JSON array, not the Frappe envelope.** OwnTracks
    executes commands it finds there — ``setConfiguration``, ``setWaypoints``,
    ``reportLocation`` — and ignores anything that is not an array. That is the
    only lever we hold over a device we do not build software for, so
    ``services.owntracks_steering`` decides what goes in it and this function
    returns a raw ``werkzeug`` Response to bypass the envelope.

    Reads ``frappe.form_dict`` rather than declaring the fields as parameters.
    OwnTracks POSTs a bare JSON object, which Frappe loads into ``form_dict``
    wholesale, and its ``_type`` discriminator is a leading-underscore name that
    does not survive being a Python parameter cleanly.

    Always answers 200 for a well-formed request, including for message types we
    do not store. OwnTracks retries a non-2xx indefinitely, so a 4xx on a region
    transition would turn one unsupported message into a permanent hot loop.
    """
    _ensure_tracking_permission()
    result = _ingest_owntracks(dict(frappe.form_dict or {}))
    return _owntracks_response(result)


def _ingest_owntracks(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The testable half: ingest the message, then plan the reply commands."""
    meta = tracking.owntracks_meta(payload)
    fix = tracking.owntracks_to_fix(payload)
    result: Dict[str, Any] = {"success": True, "accepted": 0, "commands": []}
    if fix is None:
        result["ignored"] = meta.get("type") or "unknown"

    try:
        identity = courier_onboarding.ensure_courier_setup(
            action_label="location tracking"
        )
        branch, party, party_type = identity["branch"], identity["party"], identity["party_type"]

        if fix is not None:
            # Auto-open the duty. An iPhone courier has no foreground service to bind
            # a shift to, and OwnTracks knows nothing about duties, so requiring a
            # manual Start Shift would mean the common failure is "he forgot, and was
            # invisible all day with nothing to tell him". start_duty already returns
            # the open duty when there is one, so this is idempotent; the
            # get_open_duty check just avoids the insert path's work on every ping.
            open_duty = duty_session.get_open_duty(party_type, party)
            if not open_duty:
                opened = duty_session.start_duty(
                    party_type=party_type, party=party, branch=branch
                ) or {}
                open_duty = opened.get("duty") or {}

            ingested = tracking.ingest(
                party_type=party_type,
                party=party,
                branch=branch,
                pings=[fix],
                duty=(open_duty or {}).get("name"),
            )
            result.update(ingested)

        result["commands"] = _steer_device(identity, meta, carries_position=fix is not None)
        return result
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier ingest_owntracks failed")
        return {"success": False, "error": str(exc), "commands": []}


def _steer_device(
    identity: Dict[str, Any], meta: Dict[str, Any], *, carries_position: bool
) -> List[Dict[str, Any]]:
    """Commands for the response body. Never raises — steering is best-effort and a
    failure here must not cost the position that was just stored."""
    branch, party = identity["branch"], identity["party"]
    try:
        if meta.get("mode") is not None:
            location_cache.write_owntracks_mode(branch, party, meta["mode"])

        steer_window = location_cache.should_steer(branch, party)
        # The run sheet is consulted inside the window, or for a message with no
        # position (the nudge case). On the 30 s stream it would otherwise be a
        # query per ping for a decision that changes a few times a day.
        stops = _open_stops(identity) if (steer_window or not carries_position) else []
        waypoints = owntracks_steering.build_waypoints(stops) if steer_window else []
        device_mode = meta.get("mode")
        if device_mode is None:
            device_mode = location_cache.read_owntracks_mode(branch, party)

        plan = owntracks_steering.plan_commands(
            message_type=meta.get("type") or "",
            trigger=meta.get("trigger") or "",
            device_mode=device_mode,
            has_open_stops=bool(stops),
            steer_window=steer_window,
            waypoints=waypoints,
            pushed_fingerprint=(
                location_cache.read_waypoints_fingerprint(branch, party) if steer_window else None
            ),
        )
        if plan.get("fingerprint"):
            location_cache.remember_waypoints_fingerprint(branch, party, plan["fingerprint"])
        return list(plan.get("commands") or [])
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier owntracks steering failed")
        return []


def _open_stops(identity: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The courier's current run, or ``[]`` — a failed lookup must not block ingest."""
    try:
        branches = list(identity.get("pos_profiles") or []) or [identity.get("branch")]
        run = run_sheet.get_run(
            party_type=identity["party_type"], party=identity["party"], branches=branches
        )
        return list((run or {}).get("stops") or [])
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier owntracks run lookup failed")
        return []


def _owntracks_response(result: Dict[str, Any]) -> Any:
    """A bare JSON array for OwnTracks.

    ``frappe.handler.handle`` passes a ``werkzeug`` Response straight through, which
    skips the ``{"message": ...}`` envelope — necessary, because OwnTracks only acts
    on a top-level array. The request lifecycle still commits (``frappe/app.py``
    commits every POST regardless of what the handler returned), so nothing written
    during ingest is lost by returning early.

    Falls back to the plain list when ``werkzeug`` is unavailable, which is only the
    unit-test harness; a real Frappe process always has it.
    """
    commands = list(result.get("commands") or [])
    try:
        from werkzeug.wrappers import Response
    except ImportError:  # pragma: no cover - harness only
        return commands
    return Response(json.dumps(commands), status=200, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# iPhone setup — self-service
# ─────────────────────────────────────────────────────────────────────────────

OWNTRACKS_APP_STORE_URL = "https://apps.apple.com/app/owntracks/id692424691"


@frappe.whitelist(allow_guest=False)
def get_owntracks_setup() -> Dict[str, Any]:
    """Everything a courier needs to point OwnTracks at us, for their own account.

    Returns the ``_type: configuration`` document and an ``owntracks:///config``
    URL carrying it base64-encoded. Opened on the iPhone, that URL imports the
    whole setup in one tap — endpoint, credentials, Move mode, and the remote-
    command switches the server relies on. Hand configuration is the failure mode
    this exists to remove: a device that misses ``cmd`` or ``remoteConfiguration``
    silently ignores every command we send, and nothing on our side can tell.

    **Mints an API key pair for the calling user if they have none.** Frappe's own
    ``generate_keys`` is System-Manager-only, which would make every iPhone courier
    a ticket for a manager. A courier issuing a credential for *their own* account
    is the same trust boundary as knowing their own password — the secret grants
    exactly what the password does — so it is scoped hard to ``frappe.session.user``
    and to the courier roles. An existing pair is reused so re-opening the page
    never invalidates a working device.

    Writes with ``db.set_value`` and ``set_encrypted_password`` rather than saving
    the User document: on v16 a Role Profile strips every role not in it on
    ``save()``, and that would delete the ``Jarz Courier`` role on the very account
    being set up.
    """
    _ensure_tracking_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(
            action_label="setting up iPhone tracking"
        )
        user = frappe.session.user
        api_key, api_secret = _ensure_api_credentials(user)
        configuration = owntracks_steering.device_configuration(
            ingest_url=_ingest_url(),
            api_key=api_key,
            api_secret=api_secret,
            party=identity["party"],
            display_name=str(identity.get("display_name") or ""),
        )
        encoded = base64.b64encode(
            json.dumps(configuration, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return {
            "success": True,
            "configuration": configuration,
            "inline_url": f"owntracks:///config?inline={encoded}",
            "app_store_url": OWNTRACKS_APP_STORE_URL,
            "ingest_url": configuration["url"],
            "tracker_id": configuration["tid"],
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        # Nothing above puts the secret in a message, so the traceback is safe to log.
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_owntracks_setup failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_my_tracking_status() -> Dict[str, Any]:
    """What the server last heard from this courier's tracker, for the setup screen.

    The courier cannot see OwnTracks working — it has no UI of ours — so the only
    proof the setup took is the server saying "I heard from you N seconds ago, in
    Move mode". Without this, "is it working?" is a phone call to a manager who
    then opens the fleet map.
    """
    _ensure_tracking_permission()
    try:
        identity = courier_onboarding.ensure_courier_setup(action_label="checking tracking")
        branch, party, party_type = identity["branch"], identity["party"], identity["party_type"]

        position = location_cache.read_position(branch, party) or {}
        epoch = position.get("epoch")
        age: Optional[float] = None
        if epoch is not None:
            try:
                age = max(0.0, time.time() - float(epoch))
            except (TypeError, ValueError):
                age = None

        stops = _open_stops(identity)
        return {
            "success": True,
            "last_position_age_sec": age,
            "last_position_ts": position.get("ts"),
            "device_mode": location_cache.read_owntracks_mode(branch, party),
            "desired_mode": owntracks_steering.desired_mode(has_open_stops=bool(stops)),
            "open_stops": len(stops),
            "pinned_stops": len(owntracks_steering.build_waypoints(stops)),
            "duty_open": bool(duty_session.get_open_duty(party_type, party)),
            "ingest_url": _ingest_url(),
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_my_tracking_status failed")
        return {"success": False, "error": str(exc)}


def _ingest_url() -> str:
    from frappe.utils import get_url

    return get_url("/api/method/jarz_courier.api.tracking.ingest_owntracks")


def _ensure_api_credentials(user: str) -> tuple:
    """``(api_key, api_secret)`` for *user*, minting a pair only when none works.

    Reuse first: regenerating on every visit would invalidate the pair already on
    the courier's phone each time they open the setup screen to check it.
    """
    api_key = frappe.db.get_value("User", user, "api_key")
    api_secret = _read_api_secret(user) if api_key else None
    if api_key and api_secret:
        return str(api_key), str(api_secret)

    if not api_key:
        # Only ever written when absent. The key is the public half and may be
        # referenced elsewhere (Desk shows it); rewriting one that exists gains
        # nothing and changes something a manager may have copied down.
        api_key = _new_token()
        frappe.db.set_value("User", user, "api_key", api_key, update_modified=False)

    api_secret = _new_token()
    _write_api_secret(user, api_secret)
    return str(api_key), api_secret


def _read_api_secret(user: str) -> Optional[str]:
    from frappe.utils.password import get_decrypted_password

    try:
        return get_decrypted_password("User", user, "api_secret", raise_exception=False)
    except Exception:
        return None


def _write_api_secret(user: str, secret: str) -> None:
    from frappe.utils.password import set_encrypted_password

    set_encrypted_password("User", user, secret, "api_secret")


def _new_token() -> str:
    return frappe.generate_hash(length=15)


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

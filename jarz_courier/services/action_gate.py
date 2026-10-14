"""Proof that the courier is at the door before the app may say so.

``mark_arrived`` / ``mark_delivered`` / ``mark_failed`` are claims made by a phone. The
phone's own coordinates ride along with them, but a handset can be pointed at any
address by an app that fakes its location, and a courier can tap Delivered from the
car. This gate compares the claim against what the **server** already holds: the
courier's latest ping in the location cache, which arrived on a separate request and
cannot be edited by the action call.

It is off by default (``Jarz POS Settings.enforce_courier_action_gate``) and every
refusal is a structured ``frappe.ValidationError`` subclass, so the app can tell
"go closer" from "turn your tracking back on" by ``exc_type`` rather than by parsing a
sentence. Each refusal also files an ``Action Refused`` finding so ops sees the attempt
even when the courier simply tries again from the right place.

Rules, in order:

1. Supervisors are never gated - they fix orders on a courier's behalf from a desk.
2. A client that says its fix is mocked is refused (Medium).
3. A client fix that disagrees with the server fix by more than the radius is refused
   (Medium): one of the two is lying about where the phone is.
4. No server fix, or one older than the max age, is refused (Low): the phone is not
   reporting and nothing can vouch for it.
5. A stop without a pin cannot be distance-checked; it is allowed once 2-4 pass.
6. A server fix farther than the radius from the pin is refused (Low).

The mismatch test comes before the distance test on purpose. A spoofed client fix
parked at the door plus a real server fix across town would otherwise surface only as
"too far", which reads as an honest courier who tapped early.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_courier.constants import ANOMALY_TYPE, ROLES, SEVERITY
from jarz_courier.services import anomaly, courier_onboarding, geo_track, location_cache, pos_bridge, run_sheet

DEFAULT_RADIUS_M = 300
DEFAULT_MAX_FIX_AGE_SEC = 300

#: One finding per (order, action, reason) per this window. A courier retrying from
#: the car park is one story, not thirty.
DEDUPE_WINDOW_SEC = 600

ACTION_ARRIVED = "arrived"
ACTION_DELIVERED = "delivered"
ACTION_FAILED = "failed"


class CourierTrackingStale(frappe.ValidationError):
    """No server-side position newer than the allowed age."""


class CourierTooFar(frappe.ValidationError):
    """The server's latest position is farther than the radius from the stop pin."""


class CourierLocationMismatch(frappe.ValidationError):
    """The phone's claimed position disagrees with the one the server already has."""


class CourierMockLocation(frappe.ValidationError):
    """The phone reports that its location is mocked."""


#: Exception names the endpoints must let through instead of flattening to an envelope.
REFUSAL_TYPES = (CourierTrackingStale, CourierTooFar, CourierLocationMismatch, CourierMockLocation)


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    return bool(_as_int(pos_bridge.get_pos_setting("enforce_courier_action_gate", 0), 0))


def get_settings() -> Dict[str, Any]:
    """The block the app reads from the run payload: ``{enabled, radius_m, max_fix_age_sec}``."""
    radius = _as_int(pos_bridge.get_pos_setting("courier_action_radius_m", DEFAULT_RADIUS_M), DEFAULT_RADIUS_M)
    max_age = _as_int(
        pos_bridge.get_pos_setting("courier_action_max_fix_age_sec", DEFAULT_MAX_FIX_AGE_SEC),
        DEFAULT_MAX_FIX_AGE_SEC,
    )
    return {
        "enabled": is_enabled(),
        "radius_m": radius if radius > 0 else DEFAULT_RADIUS_M,
        "max_fix_age_sec": max_age if max_age > 0 else DEFAULT_MAX_FIX_AGE_SEC,
    }


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────

def assert_action_allowed(
    invoice_id: str,
    action: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    is_mocked: bool = False,
    *,
    identity: Optional[Dict[str, Any]] = None,
    now_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """Return a small report when the action may proceed; raise a refusal otherwise.

    Does not check the setting - the caller does (``is_enabled``), so this can be
    exercised directly. ``identity`` is resolved from the session when omitted.
    """
    roles = {str(r or "").strip() for r in frappe.get_roles(frappe.session.user)}
    if not roles.isdisjoint(ROLES.COURIER_SUPERVISOR):
        return {"checked": False, "reason": "supervisor"}

    cfg = get_settings()
    radius = float(cfg["radius_m"])
    max_age = float(cfg["max_fix_age_sec"])
    now = float(now_epoch if now_epoch is not None else time.time())

    identity = identity or courier_onboarding.ensure_courier_setup(action_label=_("This action"))
    party_type, party, branch = identity["party_type"], identity["party"], identity["branch"]

    def refuse(exc_type, kind: str, severity: str, message: str, *, observed=None, fix=None):
        _flag_refusal(
            invoice_id,
            action,
            kind,
            severity,
            message,
            party_type=party_type,
            party=party,
            branch=branch,
            observed=observed,
            threshold=radius,
            fix=fix,
            now=now,
        )
        frappe.throw(message, exc_type)

    if is_mocked:
        refuse(
            CourierMockLocation,
            "mock",
            SEVERITY.MEDIUM,
            _("Your phone is reporting a mocked location. Turn it off to continue."),
        )

    server = location_cache.read_position(branch, party)
    server_lat = _fix_value(server, "lat")
    server_lng = _fix_value(server, "lng")
    age = _age_sec(server, now)

    client_lat, client_lng = _as_float(latitude), _as_float(longitude)
    if (
        client_lat is not None
        and client_lng is not None
        and server_lat is not None
        and server_lng is not None
        and age is not None
        and age <= max_age
        and geo_track.is_valid_coordinate(client_lat, client_lng)
    ):
        gap = geo_track.haversine_m(client_lat, client_lng, server_lat, server_lng)
        if gap > radius:
            refuse(
                CourierLocationMismatch,
                "mismatch",
                SEVERITY.MEDIUM,
                _("Your phone's location does not match your tracked position."),
                observed=gap,
                fix=server,
            )

    if server_lat is None or server_lng is None or age is None or age > max_age:
        refuse(
            CourierTrackingStale,
            "stale",
            SEVERITY.LOW,
            _("Your location is not being tracked. Open the app and wait for a GPS fix, then try again."),
            observed=age,
            fix=server,
        )

    # Only the distance test needs the pin. Mock, mismatch and live-tracking
    # checks hold without one - most addresses are still unpinned, so gating
    # them on the pin would leave the gate open for almost every stop.
    pin = _stop_pin(invoice_id)
    if pin is None:
        return {"checked": True, "reason": "no pin", "fix_age_sec": age}

    distance = geo_track.haversine_m(server_lat, server_lng, pin[0], pin[1])
    if distance > radius:
        refuse(
            CourierTooFar,
            "far",
            SEVERITY.LOW,
            _("You are {0} m from the delivery address. Get within {1} m to continue.").format(
                int(round(distance)), int(round(radius))
            ),
            observed=distance,
            fix=server,
        )

    return {"checked": True, "distance_m": distance, "fix_age_sec": age}


# ─────────────────────────────────────────────────────────────────────────────
# Delivery-code failures
# ─────────────────────────────────────────────────────────────────────────────

def flag_delivery_code_failure(
    invoice_id: str, *, locked: bool, message: str = "", row: Optional[Dict[str, Any]] = None
) -> None:
    """File ``Delivery Code Failed`` for a wrong or locked code. Never raises.

    Medium for a wrong code, High once jarz_pos has locked the order. The window is a
    short one so each wrong attempt is its own finding up to the lock, which is the
    point: five attempts is a different story from one typo.
    """
    try:
        row = row or frappe.db.get_value(
            "Sales Invoice",
            invoice_id,
            ["custom_courier_party_type", "custom_courier_party", "custom_kanban_profile", "pos_profile"],
            as_dict=True,
        ) or {}
        party_type = row.get("custom_courier_party_type")
        party = row.get("custom_courier_party")
        if not (party_type and party):
            return
        now = time.time()
        kind = "locked" if locked else "wrong"
        result = anomaly.flag(
            anomaly_type=ANOMALY_TYPE.DELIVERY_CODE_FAILED,
            severity=SEVERITY.HIGH if locked else SEVERITY.MEDIUM,
            reason=(message or _("Wrong delivery code entered")).strip()[:500],
            party_type=party_type,
            party=party,
            branch=row.get("custom_kanban_profile") or row.get("pos_profile"),
            sales_invoice=invoice_id,
            dedupe_key=f"delivery-code:{invoice_id}:{kind}:{int(now // 60)}",
        )
        _commit_finding(result)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: delivery code finding failed")


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _flag_refusal(
    invoice_id: str,
    action: str,
    kind: str,
    severity: str,
    message: str,
    *,
    party_type: str,
    party: str,
    branch: str,
    observed: Optional[float],
    threshold: float,
    fix: Optional[Dict[str, Any]],
    now: float,
) -> None:
    """Never raises: a failed finding must not swap the structured refusal for a 417."""
    try:
        result = anomaly.flag(
            anomaly_type=ANOMALY_TYPE.ACTION_REFUSED,
            severity=severity,
            reason=f"{action}: {message}"[:500],
            party_type=party_type,
            party=party,
            branch=branch,
            sales_invoice=invoice_id,
            observed=observed,
            threshold=threshold if kind in ("far", "mismatch") else None,
            unit="m" if kind in ("far", "mismatch") else ("seconds" if kind == "stale" else None),
            latitude=_fix_value(fix, "lat"),
            longitude=_fix_value(fix, "lng"),
            dedupe_key=f"action-refused:{invoice_id}:{action}:{kind}:{int(now // DEDUPE_WINDOW_SEC)}",
        )
        _commit_finding(result)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: action refusal finding failed")


def _commit_finding(result: Any) -> None:
    """Commit a just-filed finding before the caller raises its refusal.

    Every caller here files the finding and then throws, and Frappe rolls the request
    back on an exception: without this commit the finding vanishes and its after-commit
    push to ops never fires, so the refusals this module exists to surface would leave
    no trace. Safe because nothing before a refusal writes anything worth undoing:
    the gate runs ahead of every write, and jarz_pos checks the delivery code ahead of
    its own updates (at most it has minted the order's tracking token).
    """
    if not (isinstance(result, dict) and result.get("created")):
        return
    try:
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: committing a refusal finding failed")


def _stop_pin(invoice_id: str) -> Optional[tuple]:
    """``(lat, lng)`` of the stop's address, or ``None`` when it has no usable pin."""
    row = frappe.db.get_value(
        "Sales Invoice", invoice_id, ["shipping_address_name", "customer_address"], as_dict=True
    )
    if not row:
        return None
    name = str(row.get("shipping_address_name") or row.get("customer_address") or "").strip()
    address = (run_sheet._load_addresses([row]) or {}).get(name) or {}
    lat, lng = _as_float(address.get("latitude")), _as_float(address.get("longitude"))
    if lat is None or lng is None or not geo_track.is_valid_coordinate(lat, lng):
        return None
    return lat, lng


def _fix_value(fix: Optional[Dict[str, Any]], key: str) -> Optional[float]:
    if not isinstance(fix, dict):
        return None
    return _as_float(fix.get(key))


def _age_sec(fix: Optional[Dict[str, Any]], now: float) -> Optional[float]:
    """Seconds since the handset took the fix. ``None`` when it carries no clock."""
    if not isinstance(fix, dict):
        return None
    epoch = _as_float(fix.get("epoch"))
    if epoch is None:
        return None
    return max(now - epoch, 0.0)


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

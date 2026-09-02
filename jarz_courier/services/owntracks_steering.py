"""Server-side steering of the OwnTracks iPhone tracker.

We do not build the iPhone tracker — Safari cannot track in the background, so an
iPhone courier runs OwnTracks, a third-party app that POSTs positions to us. What
we *can* do is answer each POST with commands, which OwnTracks executes:

* ``setConfiguration`` — switch the device between **Move** mode (a 30 s / 25 m
  stream, expensive) and **Significant** mode (Apple's "moved ~500 m" service,
  cheap) — applied by whether the courier is *carrying orders right now*, not by
  the clock and not by the duty, because a duty auto-opens on any ping;
* ``setWaypoints`` — push the day's stops as 100 m geofences. iOS wakes an app for
  a region crossing even when it will not for a periodic fix, and OwnTracks
  reports the crossing *with the position it fired on*, so this is the cheapest
  and most reliable "arrived at the door" signal available on that platform;
* ``reportLocation`` — ask for a fix now, used only when a non-position message
  arrives while orders are out, and never in reply to a fix we requested.

Everything here is pure: given the courier's state it returns the list of command
dicts to put in the HTTP response body. The throttles and the run-sheet query live
in the caller, so this can be tested without Redis or a database.

The one thing it deliberately does **not** do is mark an invoice *Arrived* when the
device enters that stop's geofence. It is tempting and free — but a geofence firing
because the courier drove past the block writes an ``arrived_at`` nobody can later
distinguish from a real one. Transitions are ingested as *evidence* (a position),
not as a business event.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence

from jarz_courier.constants import OWNTRACKS


# ─────────────────────────────────────────────────────────────────────────────
# Mode
# ─────────────────────────────────────────────────────────────────────────────

def desired_mode(*, has_open_stops: bool) -> int:
    """Move while there are stops out, Significant otherwise.

    Not "Move while on duty": duties auto-open on the first OwnTracks ping of the
    day, so "on duty" is true whenever the phone is on. A courier with nothing to
    deliver would be streamed at 30 s all evening — a battery cost and a privacy
    intrusion with no operational purpose. Orders out is the honest signal.
    """
    return OWNTRACKS.MODE_MOVE if has_open_stops else OWNTRACKS.MODE_SIGNIFICANT


def set_configuration_command(mode: int) -> Dict[str, Any]:
    """The ``setConfiguration`` command for *mode*.

    Only the keys that change ship in the merge. Cadence keys are sent alongside
    Move so a device whose interval was hand-edited still gets ours; they are
    harmless under Significant, which ignores them.
    """
    configuration: Dict[str, Any] = {"_type": "configuration", "monitoring": int(mode)}
    if int(mode) == OWNTRACKS.MODE_MOVE:
        configuration["locatorInterval"] = OWNTRACKS.MOVE_INTERVAL_SEC
        configuration["locatorDisplacement"] = OWNTRACKS.MOVE_DISPLACEMENT_M
    return {"_type": "cmd", "action": "setConfiguration", "configuration": configuration}


def report_location_command() -> Dict[str, Any]:
    return {"_type": "cmd", "action": "reportLocation"}


# ─────────────────────────────────────────────────────────────────────────────
# Waypoints
# ─────────────────────────────────────────────────────────────────────────────

def waypoint_tst(invoice: str) -> int:
    """A stable, positive key for a stop.

    OwnTracks keys waypoints on ``tst`` and merges on it, so the same stop must
    always produce the same value or every push creates a duplicate geofence. A
    CRC of the invoice name is stable across pushes and across servers, and iOS
    limits region monitoring to 20 at a time — so uniqueness within a run is all
    that is needed, not global uniqueness.
    """
    return zlib.crc32(str(invoice).encode("utf-8")) & 0x7FFFFFFF


def build_waypoints(stops: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Geofences for every stop that has a pin. Stops without one are skipped.

    A stop with no coordinates cannot be a region, and inventing one from a
    territory centroid would fire "arrived" for the whole district.
    """
    waypoints: List[Dict[str, Any]] = []
    for stop in stops or ():
        if not isinstance(stop, dict):
            continue
        address = stop.get("address") if isinstance(stop.get("address"), dict) else {}
        lat = _as_float(address.get("latitude"))
        lon = _as_float(address.get("longitude"))
        if lat is None or lon is None or (abs(lat) < 1e-9 and abs(lon) < 1e-9):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        invoice = str(stop.get("invoice") or "").strip()
        if not invoice:
            continue
        waypoints.append(
            {
                "_type": "waypoint",
                # What the courier sees on the OwnTracks map and what comes back in
                # the transition's `desc`. The Woo number is the id everyone uses.
                "desc": str(stop.get("display_id") or invoice),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "rad": OWNTRACKS.WAYPOINT_RADIUS_M,
                "tst": waypoint_tst(invoice),
            }
        )
    waypoints.sort(key=lambda w: w["tst"])
    return waypoints


def set_waypoints_command(waypoints: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "_type": "cmd",
        "action": "setWaypoints",
        "waypoints": {"_type": "waypoints", "waypoints": list(waypoints)},
    }


def waypoints_fingerprint(waypoints: Sequence[Dict[str, Any]]) -> str:
    """Identity of a waypoint *set*, order-independent, so an unchanged run does
    not get re-pushed every steering window."""
    canonical = json.dumps(
        sorted(
            ({"tst": w.get("tst"), "lat": w.get("lat"), "lon": w.get("lon")} for w in waypoints),
            key=lambda w: int(w["tst"] or 0),
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# The plan
# ─────────────────────────────────────────────────────────────────────────────

def plan_commands(
    *,
    message_type: str,
    trigger: str,
    device_mode: Optional[int],
    has_open_stops: bool,
    steer_window: bool,
    waypoints: Sequence[Dict[str, Any]],
    pushed_fingerprint: Optional[str],
) -> Dict[str, Any]:
    """Decide what to put in the response body for one incoming message.

    Returns ``{"commands": [...], "fingerprint": str|None}`` — the fingerprint is
    the one to remember when a ``setWaypoints`` was included, or ``None``.

    Rules, in order:

    1. **Mode** is only reconsidered inside a steering window (the caller throttles
       to once per :data:`OWNTRACKS.STEER_THROTTLE_SEC`). Inside it, push
       ``setConfiguration`` when the device reported a mode and it is not the one
       we want. If the device reported *no* mode — Android OwnTracks omits ``m`` —
       push it anyway: the command is idempotent on the device and the window
       bounds the cost.
    2. **Waypoints** are pushed inside the window when the set differs from what
       was last pushed (or nothing is remembered). An empty set is still pushed
       when the remembered one was non-empty, so finished stops stop being
       geofences.
    3. **reportLocation** is added when the message carried no position but orders
       are out — and never in reply to a ``t == "r"`` report, which is a fix we
       asked for.
    """
    commands: List[Dict[str, Any]] = []
    fingerprint_to_remember: Optional[str] = None

    if steer_window:
        wanted = desired_mode(has_open_stops=has_open_stops)
        if device_mode is None or int(device_mode) != wanted:
            commands.append(set_configuration_command(wanted))

        current = waypoints_fingerprint(waypoints)
        if pushed_fingerprint != current and (waypoints or pushed_fingerprint is not None):
            commands.append(set_waypoints_command(waypoints))
            fingerprint_to_remember = current

    carries_position = message_type in ("location", "transition")
    if has_open_stops and not carries_position and trigger != "r":
        commands.append(report_location_command())

    return {"commands": commands, "fingerprint": fingerprint_to_remember}


# ─────────────────────────────────────────────────────────────────────────────
# Device configuration (the one-time setup a courier imports)
# ─────────────────────────────────────────────────────────────────────────────

def tracker_id(party: str, display_name: str = "") -> str:
    """Two-character id OwnTracks shows on its map. Initials when there are two
    words in the name, else the tail of the employee id."""
    words = [w for w in str(display_name or "").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()[: OWNTRACKS.TID_LENGTH]
    tail = str(party or "").strip()[-OWNTRACKS.TID_LENGTH :]
    return (tail or "JZ").upper()


def device_configuration(
    *,
    ingest_url: str,
    api_key: str,
    api_secret: str,
    party: str,
    display_name: str = "",
) -> Dict[str, Any]:
    """The ``_type: configuration`` document a courier imports once.

    Everything the server will later steer is pre-enabled here — ``cmd``,
    ``remoteConfiguration``, ``allowRemoteLocation`` — because a device that was
    configured by hand and missed one of them silently ignores every command we
    send, and nothing on our side can tell.

    Starts in **Move** rather than Significant: the first thing a new install
    does is a test shift, and a courier whose first hour produced three dots
    concludes the setup failed. The server drops it to Significant at the first
    steering window with no orders out.
    """
    return {
        "_type": "configuration",
        "mode": 3,  # HTTP
        "url": ingest_url,
        "auth": True,
        "username": api_key,
        "password": api_secret,
        "tid": tracker_id(party, display_name),
        "deviceId": str(party),
        "monitoring": OWNTRACKS.MODE_MOVE,
        "locatorInterval": OWNTRACKS.MOVE_INTERVAL_SEC,
        "locatorDisplacement": OWNTRACKS.MOVE_DISPLACEMENT_M,
        "ignoreInaccurateLocations": OWNTRACKS.IGNORE_INACCURATE_M,
        "ignoreStaleLocations": OWNTRACKS.IGNORE_STALE_DAYS,
        "extendedData": True,
        "positions": 50,
        "cmd": True,
        "remoteConfiguration": True,
        "allowRemoteLocation": True,
    }


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

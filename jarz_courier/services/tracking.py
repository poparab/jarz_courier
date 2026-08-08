"""Ping ingest: normalise, reject the lies, store in Redis, tell the ops board.

This is the hot path. It runs once per courier every few seconds for the whole
working day, so what it does *not* do matters as much as what it does: no
``frappe.get_doc``, no document insert, no hook chain, and at most one small
``db.set_value`` per courier per minute.

Mock locations
--------------
Android reports ``Location.isFromMockProvider()``. Couriers will use it — a mock
provider app is a free download and the incentive is obvious. The rule here is
stronger than "store the flag":

**A mocked fix never becomes a position and never enters the trail.**

The alternative — store it with ``is_mocked: 1`` and let consumers check — was
rejected. The last-known-position key is read by a customer-facing tracking screen
across an app boundary; making that screen's correctness depend on every future
consumer remembering to check a flag is a guarantee that one of them eventually
will not, and the failure mode is showing a customer a fabricated courier position.
Refusing the write makes it impossible instead. The cost is that a courier running
a mock provider has *no* live position, which is the truth and is exactly what ops
should see.

What happens instead: the run's ``mock_ping_count`` is incremented immediately (not
throttled — the first one matters), a HIGH-severity anomaly is filed once per run, and
the branch gets a realtime alert. None of that is guessable from the courier's side,
so there is no signal telling them which pings were rejected.

Clocks
------
``epoch`` is derived from the handset's own timestamp and is used **only** for
ordering, deltas and dedupe — never compared against the server clock in any
measurement. That is deliberate: Frappe stores naive local datetimes, so converting
one to a unix timestamp picks up the server process's timezone, which may differ from
the site's. Since every fix in a run goes through the same conversion, every
*difference* is exact regardless of the offset, and differences are all the filter,
the speed check and the gap detector ever use.

The one place the server clock does appear is a sanity ceiling on absurd handset
dates, below. That is a guard, not a measurement.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from jarz_courier.constants import (
    ANOMALY_TYPE,
    LOCAL_WS_EVENTS,
    QUERY_LIMITS,
    SEVERITY,
)
from jarz_courier.services import anomaly, courier_run, geo_track, location_cache, pos_bridge

#: A handset dated further ahead than this is rejected. Without the ceiling, a phone
#: whose clock says 2031 pins the "is this fix newer than the stored one?" comparison
#: forever, so every genuine fix afterwards is silently discarded as stale. Generous
#: enough (24 h) that a real timezone misconfiguration — at most ~14 h — still gets
#: through, because rejecting those would mean rejecting everything from that device.
MAX_FUTURE_SKEW_SEC = 24 * 60 * 60

#: Older than this and it is not a backlog, it is a bug or a replay. A courier's
#: offline queue is hours deep at worst.
MAX_PAST_AGE_SEC = 7 * 24 * 60 * 60


def _logger():
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────────────

def normalise_fix(
    raw: Dict[str, Any],
    *,
    party_type: str,
    party: str,
    branch: str,
    run: Optional[str] = None,
    now_epoch: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """One client ping → the canonical cache payload, or None if unusable.

    Returns None rather than raising. A batch flush must not lose 900 good fixes
    because the 400th had a malformed timestamp — and a client with a bad fix cannot
    be fixed by an error response it will only retry.

    ``ts`` is stored as the handset gave it, following the same rule as
    ``Delivery Proof.captured_at``: overwriting it with the server clock would date a
    morning of queued fixes to the moment the courier walked past a router, turning a
    four-hour run into a four-second one.
    """
    if not isinstance(raw, dict):
        return None

    latitude = _first(raw, "lat", "latitude")
    longitude = _first(raw, "lng", "longitude", "lon")
    if not geo_track.is_valid_coordinate(latitude, longitude):
        return None

    reference = now_epoch if now_epoch is not None else time.time()
    stamped = _resolve_timestamp(raw, reference)
    if stamped is None:
        return None
    ts, epoch = stamped

    return {
        "lat": round(float(latitude), 6),
        "lng": round(float(longitude), 6),
        "heading": _as_float(_first(raw, "heading", "bearing")),
        # Metres per second, as Android reports it. NOT converted here — the wire
        # format is documented in location_cache and a silent unit change would make
        # every stored fix disagree with every new one.
        "speed": _as_float(raw.get("speed")),
        "accuracy": _as_float(_first(raw, "accuracy", "accuracy_m")) or 0.0,
        "ts": ts,
        "epoch": epoch,
        "is_mocked": 1 if _as_bool(_first(raw, "is_mocked", "isMocked", "mocked")) else 0,
        "party_type": party_type,
        "party": party,
        "branch": branch,
        "run": run,
    }


def _resolve_timestamp(raw: Dict[str, Any], reference_epoch: float):
    """``(ts_string, epoch)`` from whatever the client sent, or None if implausible."""
    epoch = _as_float(raw.get("epoch"))
    ts_value = _first(raw, "ts", "timestamp", "captured_at", "recorded_at")

    if epoch is None and ts_value:
        try:
            epoch = get_datetime(ts_value).timestamp()
        except Exception:
            epoch = None

    if epoch is None:
        # No usable client time at all: fall back to the server clock and say so in
        # the payload, so a consumer can tell a measured timestamp from a guessed one.
        epoch = reference_epoch
        ts_value = None

    if epoch > reference_epoch + MAX_FUTURE_SKEW_SEC:
        return None
    if epoch < reference_epoch - MAX_PAST_AGE_SEC:
        return None

    if ts_value:
        ts = str(ts_value)
    else:
        ts = str(now_datetime())

    return (ts, round(float(epoch), 3))


# ─────────────────────────────────────────────────────────────────────────────
# Ingest
# ─────────────────────────────────────────────────────────────────────────────

def ingest(
    *,
    party_type: str,
    party: str,
    branch: str,
    pings: Sequence[Dict[str, Any]],
    duty: Optional[str] = None,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Accept one or many fixes. The single entry point for both API endpoints.

    ``ingest_ping`` and ``ingest_pings`` differ only in arity, so they share this —
    a separate single-fix path would be a second implementation of the mock rule, the
    timestamp rule and the run-touch throttle, and the two would drift.

    Batch handling honours the offline-queue contract:

    * The batch is **sorted by timestamp** before anything is stored, so a backlog
      drained in whatever order the client happened to hold it still lands in time
      order. The trail is a Redis sorted set, so a fix older than everything already
      stored slots into its correct place rather than being appended out of sequence
      or dropped.
    * **Dedupe is by whole-second timestamp**, in ``location_cache.append_fixes``. A
      queue page replayed after a timeout is the normal case, not the exception.
    * The **live position only moves forward.** A backlog fix that is older than the
      stored position updates the trail but not the position — otherwise a courier
      flushing a tunnel's worth of fixes would appear to teleport back to the tunnel.
    """
    result: Dict[str, Any] = {
        "run": None,
        "received": len(pings or []),
        "accepted": 0,
        "duplicate": 0,
        "rejected": 0,
        "mocked": 0,
        "truncated": 0,
        "position": None,
        "position_moved": False,
        "published": False,
    }

    if not (party_type and party and branch):
        frappe.throw(_("Courier identity and branch are required to record a position"))

    batch = list(pings or [])[: QUERY_LIMITS.PINGS_PER_BATCH]
    result["received"] = len(batch)
    if not batch:
        return result

    run_row = (courier_run.ensure_open_run(
        party_type=party_type, party=party, branch=branch, duty=duty, device=device
    ) or {}).get("run") or {}
    run_name = run_row.get("name")
    result["run"] = run_name

    reference = time.time()
    normalised: List[Dict[str, Any]] = []
    for raw in batch:
        fix = normalise_fix(
            raw,
            party_type=party_type,
            party=party,
            branch=branch,
            run=run_name,
            now_epoch=reference,
        )
        if fix is None:
            result["rejected"] += 1
            continue
        normalised.append(fix)

    normalised.sort(key=lambda f: f["epoch"])

    mocked = [f for f in normalised if f["is_mocked"]]
    real = [f for f in normalised if not f["is_mocked"]]
    result["mocked"] = len(mocked)

    if real:
        stored = location_cache.append_fixes(branch, party, real)
        result["accepted"] = stored["added"]
        result["duplicate"] = stored["duplicate"]
        result["truncated"] = stored["trimmed"]

        moved = _advance_position(branch, party, real[-1])
        result["position_moved"] = moved
        result["position"] = real[-1] if moved else location_cache.read_position(branch, party)

        if moved:
            location_cache.register_in_branch_index(branch, party_type, party)
            result["published"] = _publish_position(branch, real[-1])

        courier_run.touch_run(
            run_name,
            branch=branch,
            party=party,
            fix=real[-1],
            accepted=stored["added"],
        )

    if mocked:
        _handle_mocked(run_row, mocked, party_type=party_type, party=party, branch=branch)

    return result


def _advance_position(branch: str, party: str, fix: Dict[str, Any]) -> bool:
    """Write the position only when *fix* is newer than what is stored.

    The comparison is against the stored payload's own ``epoch``, not against the
    server clock, so it stays correct for a device whose timezone is misconfigured.
    """
    current = location_cache.read_position(branch, party)
    if current:
        try:
            if float(current.get("epoch") or 0) >= float(fix["epoch"]):
                return False
        except (TypeError, ValueError):
            pass
    return location_cache.write_position(branch, party, fix)


def _publish_position(branch: str, fix: Dict[str, Any]) -> bool:
    """Emit the live position to the branch room, at most once per throttle window.

    Routed through ``pos_bridge.publish_to_branches``: a bare realtime publish either
    broadcasts site-wide (no ``user``) or addresses a room nobody joined
    (``user="*"``), and both bugs have already been paid for once in jarz_pos.

    The event name is declared in this app's own constants because contract §7 froze
    ``jarz_pos/constants.py`` with six courier events and no location event among
    them. See ``constants.LOCAL_WS_EVENTS``.
    """
    if not location_cache.should_publish(branch, str(fix.get("party") or "")):
        return False
    recipients = pos_bridge.publish_to_branches(
        LOCAL_WS_EVENTS.COURIER_LOCATION_UPDATED,
        {
            "branch": branch,
            "party_type": fix.get("party_type"),
            "party": fix.get("party"),
            "run": fix.get("run"),
            "lat": fix.get("lat"),
            "lng": fix.get("lng"),
            "heading": fix.get("heading"),
            "speed": fix.get("speed"),
            "accuracy": fix.get("accuracy"),
            "ts": fix.get("ts"),
        },
        [branch],
    )
    return bool(recipients)


def _handle_mocked(
    run_row: Dict[str, Any],
    mocked: Sequence[Dict[str, Any]],
    *,
    party_type: str,
    party: str,
    branch: str,
) -> None:
    """Count it, log it, flag it, tell the branch. Never store it as a position.

    The run counter is stamped with ``force=True`` so it bypasses the once-a-minute
    throttle. A courier who spoofs their position for thirty seconds and stops would
    otherwise leave no durable trace at all if the throttle window happened to be
    closed.
    """
    run_name = run_row.get("name")

    courier_run.touch_run(
        run_name, branch=branch, party=party, mocked=len(mocked), force=True
    )

    # Logged at WARNING with the level set explicitly, because frappe.logger()
    # defaults to ERROR off a dev machine and an .info() here would be invisible on
    # staging and production — which is where it matters.
    _logger().warning(
        f"jarz_courier: rejected {len(mocked)} mock-provider fix(es) from {party_type} "
        f"{party} on {branch} (run {run_name}); position and trail left untouched"
    )

    last = mocked[-1]
    anomaly.flag(
        anomaly_type=ANOMALY_TYPE.MOCK_GPS,
        severity=SEVERITY.HIGH,
        reason=_(
            "The handset reported a position from a mock location provider. The fix was "
            "refused: it is not stored as a position and is excluded from the route, so "
            "this run's recorded distance is incomplete rather than wrong."
        ),
        party_type=party_type,
        party=party,
        branch=branch,
        run=run_name,
        observed=float(len(mocked)),
        threshold=0.0,
        unit="fixes",
        latitude=last.get("lat"),
        longitude=last.get("lng"),
        # One finding per run, not per ping — a spoofing courier sends hundreds.
        dedupe_key=f"mock_gps::{run_name}",
    )

    pos_bridge.publish_to_branches(
        LOCAL_WS_EVENTS.COURIER_ALERT,
        {
            "kind": ANOMALY_TYPE.MOCK_GPS,
            "severity": SEVERITY.HIGH,
            "branch": branch,
            "party_type": party_type,
            "party": party,
            "run": run_name,
            "count": len(mocked),
        },
        [branch],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Read side — the ops board
# ─────────────────────────────────────────────────────────────────────────────

def branch_positions(branch: str) -> Dict[str, Any]:
    """Live positions for one branch, newest first.

    Positions come from Redis; the ops board polls this as a fallback for the
    realtime feed, so it has to stay cheap enough to call every few seconds
    without anybody thinking about it.

    The one exception to "Redis only" is the courier's display name. Redis holds
    ``party`` — an Employee id like ``HR-EMP-000007`` — because that is what the
    ping carries and denormalising a name onto every ping would let it go stale
    the moment somebody is renamed. But a map of employee ids is unusable: a
    dispatcher deciding who to call needs to read "سعيد حمدي", not a primary key.
    So the names are resolved here, once per refresh, in a single indexed query
    over the handful of couriers actually on the branch — not per position, and
    not on the ping path.

    Fails open: an unresolvable name leaves the id in place rather than dropping
    the courier off the map. A marker labelled with an id is worse than a name and
    better than a missing courier.
    """
    positions = location_cache.read_branch_positions(branch)

    employee_ids = []
    for position in positions:
        if str(position.get("party_type") or "") == "Employee":
            party = str(position.get("party") or "").strip()
            if party and party not in employee_ids:
                employee_ids.append(party)

    names: Dict[str, str] = {}
    if employee_ids:
        try:
            for row in frappe.get_all(
                "Employee",
                filters={"name": ["in", employee_ids]},
                fields=["name", "employee_name"],
                limit_page_length=0,
            ) or []:
                label = str(row.get("employee_name") or "").strip()
                if label:
                    names[row["name"]] = label
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "jarz_courier: courier name lookup failed"
            )

    for position in positions:
        party = str(position.get("party") or "").strip()
        # `courier_name` specifically: it is the key the ops board already reads.
        position["courier_name"] = names.get(party) or party

    return {
        "branch": branch,
        "as_of": str(now_datetime()),
        "ttl_seconds": location_cache.LOCATION_TTL_SEC,
        "couriers": positions,
        "count": len(positions),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Coercion
# ─────────────────────────────────────────────────────────────────────────────

def _first(payload: Dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among *keys*.

    The client has gone through three field-naming conventions (``lat``/``latitude``,
    ``is_mocked``/``isMocked``) and an old build in the wild still sends the old one.
    Accepting all of them here is cheaper than a forced app update, and a rejected
    ping is a position lost for good.
    """
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

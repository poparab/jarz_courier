"""Fast checks for a courier who has switched their tracking off on purpose.

The slow detectors in ``services/anomaly`` read a finished trail. These two read the
live state, so a handset that goes dark mid-run is flagged while the order is still
out and an ops user can still ring the courier:

* **Tracker mode changed** - OwnTracks reports its monitoring mode (``m``) in every
  message. The server only ever steers a device to Significant (1) or Move (2), so a
  report of Manual (0) or Quiet (-1) is a person changing the setting by hand. It is
  only a finding while the courier is carrying orders; an idle courier is entitled to
  stop tracking.
* **Tracking lost** - a courier with Out-for-Delivery stops and no ping for
  :data:`LOST_AFTER_SEC`. Runs on the scheduler sweep.

Findings go through ``anomaly.flag`` and nothing else, so dedupe, severity ranking and
the ops push are not re-implemented here. Both entry points never raise: one is called
from the ingest path and the other from the scheduler.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import frappe

from jarz_courier.constants import ANOMALY_TYPE, INVOICE_STATE, OWNTRACKS, SEVERITY
from jarz_courier.services import anomaly, location_cache, run_sheet

#: No ping for this long, with orders out, is a finding. Four minutes is eight missed
#: fixes on the 30 s stream; Significant mode is quieter but the server steers a
#: carrying courier to Move, so silence that long is not the normal cadence.
LOST_AFTER_SEC = 240

#: One finding per courier per this window, so a sweep every two minutes does not file
#: the same outage fifteen times.
LOST_DEDUPE_WINDOW_SEC = 1800

#: Modes the server never sets. 0 = Manual, -1 = Quiet.
TAMPER_MODES = (OWNTRACKS.MODE_MANUAL, OWNTRACKS.MODE_QUIET)

_MODE_LABEL = {
    OWNTRACKS.MODE_MANUAL: "Manual",
    OWNTRACKS.MODE_QUIET: "Quiet",
}


def _logger():
    return frappe.logger("jarz_courier")


# ─────────────────────────────────────────────────────────────────────────────
# OwnTracks mode change
# ─────────────────────────────────────────────────────────────────────────────

def check_mode_change(identity: Dict[str, Any], new_mode: Any) -> Optional[Dict[str, Any]]:
    """Flag ``Tracker Mode Changed`` when the device drops to Manual or Quiet.

    Must run **before** the new mode is written to the cache - the previous value is
    what makes this an edge rather than a level. Returns the ``anomaly.flag`` result
    when a finding was attempted, else ``None``. Never raises.
    """
    try:
        mode = _as_int(new_mode)
        if mode not in TAMPER_MODES:
            return None

        branch, party = identity.get("branch"), identity.get("party")
        party_type = identity.get("party_type")
        previous = location_cache.read_owntracks_mode(branch, party)
        if previous == mode:
            return None

        stops = _open_stops(identity)
        if not stops:
            return None

        label = _MODE_LABEL.get(mode, str(mode))
        bucket = int(time.time() // LOST_DEDUPE_WINDOW_SEC)
        return anomaly.flag(
            anomaly_type=ANOMALY_TYPE.TRACKER_MODE_CHANGED,
            severity=SEVERITY.HIGH,
            reason=(
                f"OwnTracks switched to {label} mode while {len(stops)} order(s) "
                "were out for delivery"
            ),
            party_type=party_type,
            party=party,
            branch=branch,
            sales_invoice=str(stops[0].get("invoice") or "") or None,
            observed=float(mode),
            unit="mode",
            dedupe_key=f"tracker-mode:{party_type}:{party}:{mode}:{bucket}",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: tracker mode check failed")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Tracking lost
# ─────────────────────────────────────────────────────────────────────────────

def check_tracking_lost(
    couriers: Optional[list] = None, *, now_epoch: Optional[float] = None
) -> Dict[str, int]:
    """Flag every courier with orders out and no recent ping. Never raises.

    ``couriers`` is ``assignment_watch.active_couriers()`` output; it is looked up
    lazily when omitted so the scheduler hook stays one line.
    """
    summary = {"checked": 0, "lost": 0}
    try:
        if couriers is None:
            from jarz_courier.services import assignment_watch

            couriers = assignment_watch.active_couriers()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: tracking-lost lookup failed")
        return summary

    now = float(now_epoch if now_epoch is not None else time.time())
    for party_type, party, branch in couriers or []:
        summary["checked"] += 1
        try:
            if _flag_if_lost(party_type, party, branch, now):
                summary["lost"] += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"jarz_courier: tracking-lost check failed for {party}"
            )
    return summary


def _flag_if_lost(party_type: str, party: str, branch: str, now: float) -> bool:
    stops = _open_stops({"party_type": party_type, "party": party, "branch": branch})
    if not stops:
        return False

    silent_for, last = _silence(branch, party, now)
    if silent_for is not None and silent_for < LOST_AFTER_SEC:
        return False

    bucket = int(now // LOST_DEDUPE_WINDOW_SEC)
    if silent_for is None:
        reason = f"No position from this courier while {len(stops)} order(s) are out for delivery"
        observed = None
    else:
        minutes = round(silent_for / 60.0, 1)
        reason = (
            f"No position for {minutes} min while {len(stops)} order(s) are out for delivery"
        )
        observed = minutes

    outcome = anomaly.flag(
        anomaly_type=ANOMALY_TYPE.TRACKING_LOST,
        severity=SEVERITY.HIGH,
        reason=reason,
        party_type=party_type,
        party=party,
        branch=branch,
        sales_invoice=str(stops[0].get("invoice") or "") or None,
        observed=observed,
        threshold=LOST_AFTER_SEC / 60.0,
        unit="minutes",
        latitude=(last or {}).get("lat"),
        longitude=(last or {}).get("lng"),
        dedupe_key=f"tracking-lost:{party_type}:{party}:{bucket}",
    )
    return bool((outcome or {}).get("created"))


def _silence(branch: str, party: str, now: float) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """``(seconds since the last fix, that fix)``; ``(None, None)`` when none is cached.

    The position key expires after 15 min, so a courier silent for longer than that
    has no position at all - which is the same finding, with no age to quote.
    """
    position = location_cache.read_position(branch, party)
    if not isinstance(position, dict):
        return None, None
    try:
        epoch = float(position.get("epoch"))
    except (TypeError, ValueError):
        return None, position
    return max(now - epoch, 0.0), position


def _open_stops(identity: Dict[str, Any]) -> list:
    run = run_sheet.get_run(
        party_type=identity.get("party_type"),
        party=identity.get("party"),
        branches=list(identity.get("pos_profiles") or []) or [identity.get("branch")],
        state=INVOICE_STATE.OUT_FOR_DELIVERY,
    )
    return [s for s in (run or {}).get("stops") or [] if isinstance(s, dict) and s.get("invoice")]


def _as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

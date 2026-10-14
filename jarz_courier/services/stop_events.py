"""Stop milestones — *left the branch*, *arrived*, *back at the branch*.

The order timeline needs three moments that nothing else records: when the courier
actually pulled away from the branch, when they reached the door, and when they came
back. All three are positions, so they are read out of the ping stream as it is
ingested, and stored one row per ``(invoice, event)`` in ``Courier Stop Event``.

**Evidence, not a business event** — with one deliberate exception. ``Left Branch``
and ``Back At Branch`` only ever become a timeline row. ``Arrived`` also asks jarz_pos
to stamp ``custom_arrived_at`` when it is still empty, because a courier who forgets
the button should not leave the order with no arrival time at all. That call goes
through ``pos_bridge.mark_invoice_arrived`` (which owns idempotency, the access gate
and the realtime publish) and never raises here.

**Never raises into ingest.** :func:`process_fixes` is called after a position has
already been stored; an exception from it would turn a good fix into a 417 and the
phone would retry a batch that was in fact accepted. Every failure is logged and
swallowed.

**Branch geofence** is the courier's Shift Location for today (HRMS). It is read
with ``frappe.db`` and guarded for HRMS being absent: no shift location means no
Left / Back events, never a guess from a branch centroid.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import frappe
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime

from jarz_courier.constants import DOCTYPES, INVOICE_STATE, STOP_EVENT
from jarz_courier.services import geo_track, location_cache, pos_bridge, run_sheet

#: "Arrived" means this close to the stop pin. The same figure as the OwnTracks
#: waypoint radius, so the geofence the phone fires on and the rule the server applies
#: describe one circle.
ARRIVED_RADIUS_M = 100.0

#: Used when the Shift Location has no ``checkin_radius``.
DEFAULT_GEOFENCE_RADIUS_M = 200.0

#: A fix reporting worse accuracy than this proves nothing about being inside or
#: outside a circle this small. ``0`` means "not reported" and is let through — the
#: same reading the rest of the pipeline gives it.
MAX_FIX_ACCURACY_M = 100.0

#: How far back a delivered order is still waiting for its "back at branch".
BACK_LOOKBACK_HOURS = 12

#: The delivered-orders lookup scans Sales Invoice, so it runs at most this often per
#: courier rather than on every 30 s ping. A throttled ping dates a return at worst
#: this much late; it never loses one, because the next window still sees the order.
BACK_LOOKUP_THROTTLE_SEC = 60

SOURCE_OWNTRACKS = "owntracks"
SOURCE_APP = "app"
SOURCE_WEB = "web"
SOURCE_GATE = "gate"

_DOCTYPE = DOCTYPES.COURIER_STOP_EVENT
_STATE_LOG = "Invoice State Log"


def _logger():
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


# ─────────────────────────────────────────────────────────────────────────────
# Recording
# ─────────────────────────────────────────────────────────────────────────────

def record_event(
    *,
    invoice: str,
    event: str,
    at: Any,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    source: str = SOURCE_APP,
    party_type: Optional[str] = None,
    party: Optional[str] = None,
    run: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert the milestone once. A second call for the same pair is a no-op.

    Idempotent twice over: a read first (cheap, the common case), and the document
    name derived from ``(invoice, event)`` so a concurrent writer that passed the read
    is stopped by the primary key. Returns ``{"created": bool, "name": str|None}``.
    """
    if event not in STOP_EVENT.ALL or not invoice:
        return {"created": False, "name": None}

    existing = frappe.db.exists(_DOCTYPE, {"invoice": invoice, "event": event})
    if existing:
        return {"created": False, "name": existing}

    doc = frappe.get_doc(
        {
            "doctype": _DOCTYPE,
            "invoice": invoice,
            "event": event,
            "at": at,
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": accuracy_m,
            "source": source,
            "party_type": party_type or None,
            "party": party or None,
            "run": run or None,
        }
    )
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        return {"created": False, "name": None}
    return {"created": True, "name": doc.name}


# ─────────────────────────────────────────────────────────────────────────────
# Ingest hook
# ─────────────────────────────────────────────────────────────────────────────

def process_fixes(
    *,
    party_type: str,
    party: str,
    branch: str,
    fixes: Sequence[Dict[str, Any]],
    stops: Optional[Sequence[Dict[str, Any]]] = None,
    run: Optional[str] = None,
    source: str = SOURCE_APP,
) -> Dict[str, Any]:
    """Look for milestones in freshly ingested, *normalised* fixes. Never raises.

    ``stops`` is the courier's current run (``run_sheet.get_run(...)["stops"]``); the
    caller already has it for steering, so it is passed in rather than queried twice.
    Fixes are read in time order and each milestone takes the **first** fix that
    satisfies it, so a drained offline queue dates a milestone to when it happened and
    not to when the signal came back.
    """
    summary: Dict[str, Any] = {"arrived": 0, "left": 0, "back": 0}
    try:
        usable = _usable_fixes(fixes)
        if not usable:
            return summary

        open_stops = [s for s in (stops or []) if isinstance(s, dict) and s.get("invoice")]
        back_candidates = _back_candidates(party_type, party, open_stops)
        invoices = sorted(
            {str(s["invoice"]) for s in open_stops} | {c["invoice"] for c in back_candidates}
        )
        if not invoices:
            return summary

        recorded = _recorded_events(invoices)
        context = {
            "party_type": party_type,
            "party": party,
            "run": run,
            "source": source,
        }

        summary["arrived"] = _detect_arrived(open_stops, usable, recorded, context)
        summary["left"], summary["back"] = _detect_geofence_events(
            open_stops, back_candidates, usable, recorded, context
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: stop event detection failed")
    return summary


def _usable_fixes(fixes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Real (not mocked), well-formed, accurate-enough fixes, oldest first."""
    out: List[Dict[str, Any]] = []
    for fix in fixes or ():
        if not isinstance(fix, dict) or fix.get("is_mocked"):
            continue
        if not geo_track.is_valid_coordinate(fix.get("lat"), fix.get("lng")):
            continue
        accuracy = _num(fix.get("accuracy"))
        if accuracy is not None and accuracy > MAX_FIX_ACCURACY_M:
            continue
        if _fix_time(fix) is None:
            continue
        out.append(fix)
    out.sort(key=lambda f: float(f.get("epoch") or 0))
    return out


def _detect_arrived(
    stops: Sequence[Dict[str, Any]],
    fixes: Sequence[Dict[str, Any]],
    recorded: Dict[str, set],
    context: Dict[str, Any],
) -> int:
    count = 0
    for stop in stops:
        invoice = str(stop.get("invoice"))
        if STOP_EVENT.ARRIVED in recorded.get(invoice, set()):
            continue
        if stop.get("state") and stop.get("state") != INVOICE_STATE.OUT_FOR_DELIVERY:
            continue
        pin = _stop_pin(stop)
        if pin is None:
            continue

        hit = next(
            (
                f
                for f in fixes
                if geo_track.haversine_m(f["lat"], f["lng"], pin[0], pin[1]) <= ARRIVED_RADIUS_M
            ),
            None,
        )
        if hit is None:
            continue

        created = _record_from_fix(invoice, STOP_EVENT.ARRIVED, hit, context)
        recorded.setdefault(invoice, set()).add(STOP_EVENT.ARRIVED)
        if created:
            count += 1
        if not stop.get("arrived_at"):
            _stamp_arrival(invoice, hit)
    return count


def _stamp_arrival(invoice: str, fix: Dict[str, Any]) -> None:
    """Ask jarz_pos to set ``custom_arrived_at`` if it is empty. Never raises.

    The ``request_id`` is deterministic, so a replayed batch collapses onto the first
    call inside jarz_pos's own idempotency window.
    """
    try:
        result = pos_bridge.mark_invoice_arrived(
            invoice,
            latitude=fix.get("lat"),
            longitude=fix.get("lng"),
            accuracy_m=_num(fix.get("accuracy")),
            request_id=f"{invoice}::auto-arrived",
        )
        if isinstance(result, dict) and result.get("success") is False:
            _logger().info(
                "jarz_courier: auto-arrive for %s declined: %s", invoice, result.get("error")
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: auto-arrive failed")


def _detect_geofence_events(
    stops: Sequence[Dict[str, Any]],
    back_candidates: Sequence[Dict[str, Any]],
    fixes: Sequence[Dict[str, Any]],
    recorded: Dict[str, set],
    context: Dict[str, Any],
) -> tuple:
    need_left = [
        str(s["invoice"])
        for s in stops
        if STOP_EVENT.LEFT_BRANCH not in recorded.get(str(s["invoice"]), set())
    ]
    need_back = [
        c for c in back_candidates if STOP_EVENT.BACK_AT_BRANCH not in recorded.get(c["invoice"], set())
    ]
    if not need_left and not need_back:
        return 0, 0

    fence = branch_geofence(context["party_type"], context["party"])
    if fence is None:
        return 0, 0

    def _inside(fix: Dict[str, Any]) -> bool:
        return (
            geo_track.haversine_m(fix["lat"], fix["lng"], fence["latitude"], fence["longitude"])
            <= fence["radius_m"]
        )

    left = 0
    if need_left:
        dispatched = _dispatch_times(need_left)
        for invoice in need_left:
            since = dispatched.get(invoice)
            if since is None:
                # No state-log entry to anchor "after it became Out for Delivery" on.
                # Guessing would date the departure to whatever fix arrives first.
                continue
            hit = next((f for f in fixes if _fix_time(f) >= since and not _inside(f)), None)
            if hit is None:
                continue
            if _record_from_fix(invoice, STOP_EVENT.LEFT_BRANCH, hit, context):
                left += 1
            recorded.setdefault(invoice, set()).add(STOP_EVENT.LEFT_BRANCH)

    back = 0
    for candidate in need_back:
        since = candidate["since"]
        hit = next((f for f in fixes if _fix_time(f) >= since and _inside(f)), None)
        if hit is None:
            continue
        if _record_from_fix(candidate["invoice"], STOP_EVENT.BACK_AT_BRANCH, hit, context):
            back += 1
        recorded.setdefault(candidate["invoice"], set()).add(STOP_EVENT.BACK_AT_BRANCH)

    return left, back


def _record_from_fix(invoice: str, event: str, fix: Dict[str, Any], context: Dict[str, Any]) -> bool:
    try:
        outcome = record_event(
            invoice=invoice,
            event=event,
            at=_fix_time(fix),
            latitude=fix.get("lat"),
            longitude=fix.get("lng"),
            accuracy_m=_num(fix.get("accuracy")),
            source=context["source"],
            party_type=context["party_type"],
            party=context["party"],
            run=context.get("run") or fix.get("run"),
        )
        return bool(outcome.get("created"))
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"jarz_courier: recording {event} failed")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

def _recorded_events(invoices: Sequence[str]) -> Dict[str, set]:
    rows = frappe.get_all(
        _DOCTYPE,
        filters={"invoice": ["in", list(invoices)]},
        fields=["invoice", "event"],
        limit=max(len(invoices) * len(STOP_EVENT.ALL), 1),
    ) or []
    out: Dict[str, set] = {}
    for row in rows:
        out.setdefault(str(row.get("invoice")), set()).add(row.get("event"))
    return out


def _dispatch_times(invoices: Sequence[str]) -> Dict[str, datetime]:
    """When each order last became Out for Delivery, from the Invoice State Log."""
    try:
        rows = frappe.get_all(
            _STATE_LOG,
            filters={"sales_invoice": ["in", list(invoices)], "to_state": INVOICE_STATE.OUT_FOR_DELIVERY},
            fields=["sales_invoice", "changed_at"],
            order_by="changed_at asc",
            limit=500,
        ) or []
    except Exception:
        # The log is a jarz_pos doctype; a site one deploy behind simply has none.
        return {}
    out: Dict[str, datetime] = {}
    for row in rows:
        stamp = _to_datetime(row.get("changed_at"))
        if stamp is not None:
            out[str(row.get("sales_invoice"))] = stamp
    return out


def _back_candidates(
    party_type: str, party: str, stops: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Orders whose attempt is over and whose "back at branch" is still unknown.

    Two groups: orders delivered in the last :data:`BACK_LOOKBACK_HOURS` hours, and
    stops still on the run sheet whose attempt failed (a failed attempt leaves the
    order Out for Delivery, with the leg closed and a failure reason).
    """
    candidates: Dict[str, Dict[str, Any]] = {}

    for stop in stops:
        if not stop.get("failure_reason"):
            continue
        ended = _to_datetime(stop.get("leg_ended_at"))
        if ended is not None:
            candidates[str(stop["invoice"])] = {"invoice": str(stop["invoice"]), "since": ended}

    window_key = f"courier:stopev:back:{party_type}:{party}"
    if (
        "custom_delivered_at" in run_sheet.available_invoice_fields()
        and location_cache._claim(window_key, BACK_LOOKUP_THROTTLE_SEC)
    ):
        floor = add_to_date(now_datetime(), hours=-BACK_LOOKBACK_HOURS)
        rows = frappe.get_all(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "custom_courier_party_type": party_type,
                "custom_courier_party": party,
                "custom_delivered_at": [">=", floor],
            },
            fields=["name", "custom_delivered_at"],
            limit=100,
        ) or []
        for row in rows:
            delivered = _to_datetime(row.get("custom_delivered_at"))
            if delivered is not None:
                candidates[str(row["name"])] = {"invoice": str(row["name"]), "since": delivered}

    return list(candidates.values())


def branch_geofence(party_type: str, party: str) -> Optional[Dict[str, float]]:
    """``{latitude, longitude, radius_m}`` for today's Shift Location, or ``None``.

    ``None`` when the courier is not an Employee, HRMS is not installed, there is no
    assignment covering today, or the location has no usable coordinates. In every one
    of those the caller emits no Left / Back events.
    """
    if party_type != "Employee" or not party:
        return None
    try:
        today = getdate()
        rows = frappe.get_all(
            "Shift Assignment",
            filters={
                "employee": party,
                "docstatus": 1,
                "status": "Active",
                "start_date": ["<=", today],
            },
            fields=["shift_location", "end_date"],
            order_by="start_date desc",
            limit=10,
        ) or []
    except Exception:
        # HRMS absent, or a build without the shift_location column.
        return None

    for row in rows:
        location = str(row.get("shift_location") or "").strip()
        end_date = row.get("end_date")
        if not location or (end_date and getdate(end_date) < today):
            continue
        try:
            values = frappe.db.get_value(
                "Shift Location",
                location,
                ["latitude", "longitude", "checkin_radius"],
                as_dict=True,
            )
        except Exception:
            return None
        if not values or not geo_track.is_valid_coordinate(values.get("latitude"), values.get("longitude")):
            continue
        radius = _num(values.get("checkin_radius"))
        return {
            "latitude": float(values["latitude"]),
            "longitude": float(values["longitude"]),
            "radius_m": radius if radius and radius > 0 else DEFAULT_GEOFENCE_RADIUS_M,
        }
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _stop_pin(stop: Dict[str, Any]) -> Optional[tuple]:
    address = stop.get("address") if isinstance(stop.get("address"), dict) else {}
    lat, lng = _num(address.get("latitude")), _num(address.get("longitude"))
    if lat is None or lng is None or not geo_track.is_valid_coordinate(lat, lng):
        return None
    return lat, lng


def _fix_time(fix: Dict[str, Any]) -> Optional[datetime]:
    return _to_datetime(fix.get("ts"))


def _to_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return get_datetime(value)
    except Exception:
        return None


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

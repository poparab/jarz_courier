"""Run lifecycle and the cold path: one polyline per run, written exactly once.

The split this module implements is the whole point of B7:

* **Hot path** — every ping goes to Redis (``services/location_cache``). No ORM, no
  row, no hooks. A fix is worth microseconds of storage, not 20 ms of document
  machinery.
* **Warm path** — at most one ``last_ping_on`` stamp per courier per minute, via
  ``frappe.db.set_value``. This exists only because the stale-ping watchdog needs a
  *durable* last-seen time; a watchdog reading Redis goes blind precisely when it
  matters, since a killed app stops pinging and the key then expires.
* **Cold path** — when the run closes, the trail is read once, noise-filtered once,
  encoded into one polyline and written to one row. The trail is then deleted.

Why the filter runs at close rather than at ingest
--------------------------------------------------
Ingest cannot filter. The jitter rule is "at least 20 m from the last *kept* fix",
which requires knowing which fixes were kept — and a backlog flushed hours later
arrives out of order, so "the last kept fix" is not knowable until the run is over
and every fix is in hand. Filtering at ingest would apply the rule against whatever
happened to arrive most recently, which on a backlog is nonsense. So ingest stores
everything plausible and the close does the arithmetic, once, with all of it.

Nothing here posts to the ledger, and ``Courier Run`` carries no monetary field. A
distance is an input to an allowance decision, not the decision.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import frappe
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from jarz_courier.constants import DOCTYPES, INVOICE_STATE, QUERY_LIMITS, RUN_STATUS
from jarz_courier.services import geo_track, location_cache, run_sheet

DOCTYPE = DOCTYPES.COURIER_RUN

RUN_FIELDS = (
    "name",
    "party_type",
    "party",
    "branch",
    "duty",
    "device",
    "status",
    "started_on",
    "ended_on",
    "last_ping_on",
    "last_latitude",
    "last_longitude",
    "ping_count",
    "mock_ping_count",
    "stale_alert_on",
    "polyline",
    "total_distance_m",
    "planned_distance_m",
    "point_count",
    "dropped_point_count",
    "truncated_point_count",
    "stops_delivered",
    "anomalies_checked_on",
)


def _logger():
    """Level set explicitly — ``frappe.logger()`` defaults to ERROR off a dev box."""
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


# ─────────────────────────────────────────────────────────────────────────────
# Lookup and open
# ─────────────────────────────────────────────────────────────────────────────

def get_open_run(party_type: str, party: str) -> Optional[Dict[str, Any]]:
    """The courier's currently open run, or None."""
    if not (party_type and party):
        return None
    rows = frappe.get_all(
        DOCTYPE,
        filters={"party_type": party_type, "party": party, "status": RUN_STATUS.OPEN},
        fields=list(RUN_FIELDS),
        order_by="started_on desc",
        limit=1,
    ) or []
    return rows[0] if rows else None


def ensure_open_run(
    *,
    party_type: str,
    party: str,
    branch: str,
    duty: Optional[str] = None,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the courier's open run, creating one if there is none.

    Called from Start Duty *and* lazily from the first ping. Both, on purpose: a
    courier whose app starts tracking before they tap Start Duty still needs an
    anchor, otherwise their pings have nowhere to be summarised and the watchdog has
    nothing to watch. Creating it here means "tracking works" does not depend on the
    courier remembering a button.

    Idempotent — the controller enforces one open run per courier, so a replayed
    Start Duty from an offline queue returns the existing one rather than tripping a
    validation error the client would show as a red toast.
    """
    existing = get_open_run(party_type, party)
    if existing:
        # A run opened from a ping has no duty; the later Start Duty adopts it
        # rather than opening a second one.
        if duty and not existing.get("duty"):
            try:
                frappe.db.set_value(DOCTYPE, existing["name"], "duty", duty, update_modified=False)
                existing["duty"] = duty
            except Exception:
                _logger().warning(f"jarz_courier: could not attach duty {duty} to run {existing['name']}")
        return {"run": existing, "created": False}

    doc = frappe.new_doc(DOCTYPE)
    doc.party_type = party_type
    doc.party = party
    doc.branch = branch
    doc.duty = duty
    doc.device = device
    doc.status = RUN_STATUS.OPEN
    doc.started_on = now_datetime()
    doc.ping_count = 0
    doc.mock_ping_count = 0
    doc.insert(ignore_permissions=True)

    return {"run": _as_payload(doc), "created": True}


# ─────────────────────────────────────────────────────────────────────────────
# Warm path — the throttled durable stamp
# ─────────────────────────────────────────────────────────────────────────────

def touch_run(
    run_name: str,
    *,
    branch: str,
    party: str,
    fix: Optional[Dict[str, Any]] = None,
    accepted: int = 0,
    mocked: int = 0,
    force: bool = False,
) -> bool:
    """Stamp last-seen state on the run. Throttled, best-effort, never raises.

    ``frappe.db.set_value`` with ``update_modified=False``, not ``doc.save()``. A
    save would re-run validation and every ``on_update`` hook over a document we are
    only stamping four numbers onto, once a minute, from a request whose actual job
    is to acknowledge a GPS ping. Leaving ``modified`` alone also keeps any
    concurrently open copy of the run saveable instead of handing it a
    ``TimestampMismatchError``.

    Counters are incremented with SQL-free read-modify-write, which can lose a
    concurrent increment. That is accepted: ``ping_count`` is a health indicator, and
    the authoritative point count is recomputed from the trail at close. Trading a
    row lock per ping for an approximate counter is the right way round.

    ``force`` bypasses the throttle. Used for a mocked fix, which must be recorded
    the first time it happens rather than whenever the minute window next opens.
    """
    if not run_name:
        return False
    if not (force or location_cache.should_touch_run(branch, party)):
        return False

    updates: Dict[str, Any] = {}
    if fix:
        ts = fix.get("ts")
        if ts:
            updates["last_ping_on"] = ts
        if geo_track.is_valid_coordinate(fix.get("lat"), fix.get("lng")):
            updates["last_latitude"] = flt(fix.get("lat"))
            updates["last_longitude"] = flt(fix.get("lng"))

    try:
        current = frappe.db.get_value(
            DOCTYPE, run_name, ["ping_count", "mock_ping_count"], as_dict=True
        ) or {}
        if accepted:
            updates["ping_count"] = int(current.get("ping_count") or 0) + int(accepted)
        if mocked:
            updates["mock_ping_count"] = int(current.get("mock_ping_count") or 0) + int(mocked)
        if not updates:
            return False
        frappe.db.set_value(DOCTYPE, run_name, updates, update_modified=False)
        return True
    except Exception:
        # A lost stamp costs the watchdog one minute of resolution. Failing the
        # courier's ping request over it would cost the position entirely.
        _logger().warning(f"jarz_courier: run touch failed for {run_name}")
        return False


def mark_stale_alerted(run_name: str) -> None:
    """Record that ops has already been told this run went quiet."""
    try:
        frappe.db.set_value(
            DOCTYPE, run_name, "stale_alert_on", now_datetime(), update_modified=False
        )
    except Exception:
        _logger().warning(f"jarz_courier: stale alert stamp failed for {run_name}")


def mark_anomalies_checked(run_name: str) -> None:
    try:
        frappe.db.set_value(
            DOCTYPE, run_name, "anomalies_checked_on", now_datetime(), update_modified=False
        )
    except Exception:
        _logger().warning(f"jarz_courier: anomaly stamp failed for {run_name}")


# ─────────────────────────────────────────────────────────────────────────────
# Cold path — the one write that matters
# ─────────────────────────────────────────────────────────────────────────────

def close_run(
    run: Any,
    *,
    status: str = RUN_STATUS.CLOSED,
    keep_trail: bool = False,
) -> Dict[str, Any]:
    """Summarise the run's trail into one polyline, analyse it, and close it.

    Order of operations is the whole safety story here:

    1. Read the trail and encode it.
    2. Save the run row.
    3. Run the anomaly detectors — **while the trail is still in Redis**. They need
       the raw fixes with their timestamps; a polyline has no time axis, so idle,
       speeding and gap detection cannot be reconstructed from it afterwards.
    4. Delete the trail **only if step 3 completed**. If a detector raised, the trail
       stays and ``anomalies_checked_on`` stays empty, so
       ``anomaly.sweep_unchecked_runs`` can retry with the evidence intact.

    Deleting the trail any earlier would trade a day's track for a Redis key.

    Returns the run payload plus the filter's drop breakdown, so a caller can tell
    "this courier did not move" from "this courier's handset was reporting rubbish".
    """
    row = _resolve(run)
    if not row:
        return {"run": None, "changed": False, "reason": "not found"}

    if row.get("status") != RUN_STATUS.OPEN:
        return {"run": row, "changed": False, "reason": f"already {row.get('status')}"}

    branch = str(row.get("branch") or "")
    party = str(row.get("party") or "")

    raw_fixes = location_cache.read_trail(branch, party)
    filtered = geo_track.filter_fixes(raw_fixes)
    kept = filtered["kept"]

    planned = planned_route(row, kept)

    doc = frappe.get_doc(DOCTYPE, row["name"])
    doc.status = status
    doc.ended_on = now_datetime()
    doc.polyline = geo_track.encode_track(kept)
    doc.total_distance_m = filtered["distance_m"]
    doc.point_count = len(kept)
    doc.dropped_point_count = sum(int(v) for v in filtered["dropped"].values())
    doc.planned_distance_m = planned["planned_distance_m"]
    doc.stops_delivered = planned["stops_delivered"]
    doc.save(ignore_permissions=True)

    analysis = _analyse(_as_payload(doc), raw_fixes)

    if analysis.get("ok") and not keep_trail:
        location_cache.drop_trail(branch, party)
    location_cache.clear_position(branch, party)

    _logger().info(
        f"jarz_courier: closed run {doc.name} status={status} "
        f"kept={len(kept)} dropped={filtered['dropped']} distance_m={filtered['distance_m']} "
        f"findings={len(analysis.get('findings') or [])}"
    )

    return {
        "run": _as_payload(doc),
        "changed": True,
        "dropped": filtered["dropped"],
        "raw_fix_count": len(raw_fixes),
        "planned": planned,
        "analysis": analysis,
    }


def _analyse(run_payload: Dict[str, Any], raw_fixes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Detector pass, isolated so it can never block a close.

    The import is function-local to break a real cycle: ``anomaly`` needs this module
    for run lookup and the checked-stamp, and this module needs ``anomaly`` for
    exactly one call. A module-level import in either direction is an ImportError at
    site boot. Deferring the side that is used once is the smaller compromise, and it
    is the same reasoning ``pos_bridge`` gives for its lazy imports.
    """
    try:
        from jarz_courier.services import anomaly

        result = anomaly.analyse_run(run_payload, fixes=raw_fixes)
        if result.get("ok"):
            mark_anomalies_checked(run_payload.get("name"))
        return result
    except Exception:
        # A closed run with an un-analysed trail is recoverable — the scheduled sweep
        # picks it up. A close that failed because detection failed is not: the
        # courier's day would stay open and their polyline unwritten.
        frappe.log_error(
            frappe.get_traceback(),
            f"jarz_courier: inline analysis failed for {run_payload.get('name')}",
        )
        return {"ok": False, "findings": []}


def close_open_runs(
    party_type: str, party: str, *, status: str = RUN_STATUS.CLOSED
) -> List[Dict[str, Any]]:
    """Close every open run for a courier. Normally exactly one."""
    results: List[Dict[str, Any]] = []
    rows = frappe.get_all(
        DOCTYPE,
        filters={"party_type": party_type, "party": party, "status": RUN_STATUS.OPEN},
        fields=list(RUN_FIELDS),
        limit=10,
    ) or []
    for row in rows:
        try:
            results.append(close_run(row, status=status))
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"jarz_courier: close run {row.get('name')} failed")
    return results


def stale_open_runs(*, minutes: int, limit: int = QUERY_LIMITS.RUNS_PER_SWEEP) -> List[Dict[str, Any]]:
    """Open runs whose newest durable ping is older than *minutes*.

    ``last_ping_on`` being NULL is included on purpose — a run that opened and never
    received a single ping is the most suspicious case of all (tracking permission
    denied, or the app killed immediately), and a naive ``<`` comparison in SQL drops
    NULLs silently. The started-on fallback is what makes it visible.
    """
    cutoff = add_to_date(now_datetime(), minutes=-int(minutes))
    rows = frappe.get_all(
        DOCTYPE,
        filters={"status": RUN_STATUS.OPEN},
        fields=list(RUN_FIELDS),
        order_by="started_on asc",
        limit=limit,
    ) or []

    stale: List[Dict[str, Any]] = []
    for row in rows:
        marker = row.get("last_ping_on") or row.get("started_on")
        if not marker:
            continue
        try:
            if get_datetime(marker) < get_datetime(cutoff):
                stale.append(row)
        except Exception:
            continue
    return stale


# ─────────────────────────────────────────────────────────────────────────────
# The planned route — denominator of the detour ratio
# ─────────────────────────────────────────────────────────────────────────────

def planned_route(run: Dict[str, Any], kept: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Straight-line chain: where the run started, then each stop's pin in order.

    **The origin is the run's first accepted fix, not the branch.** There is no
    branch coordinate anywhere in this system — ``POS Profile`` carries no lat/lng
    and contract §10a's ``distance_from_branch_m`` is optional and unimplemented. A
    chain that started at the first *stop* would omit the depot-to-first-customer
    leg, which is real riding with no planned counterpart, making the ratio too high
    and manufacturing detour flags against couriers who did nothing wrong. Using the
    first fix removes that bias without inventing a depot pin.

    Stops with no usable pin are skipped rather than treated as the origin, so one
    unpinned address shortens the plan instead of dragging it to null island.
    """
    empty = {"planned_distance_m": 0.0, "stops_delivered": 0, "pinned_stops": 0, "stops": []}

    invoices = _delivered_stops(run)
    if not invoices:
        return empty

    address_map = run_sheet.load_address_pins(invoices)

    points: List[Tuple[Any, Any]] = []
    if kept:
        first = kept[0]
        if geo_track.is_valid_coordinate(first.get("lat"), first.get("lng")):
            points.append((first["lat"], first["lng"]))

    pinned = 0
    stops: List[Dict[str, Any]] = []
    for invoice in invoices:
        address_name = invoice.get("shipping_address_name") or invoice.get("customer_address") or ""
        pin = address_map.get(address_name, {})
        latitude = pin.get("latitude")
        longitude = pin.get("longitude")
        has_pin = geo_track.is_valid_coordinate(latitude, longitude)
        if has_pin:
            points.append((latitude, longitude))
            pinned += 1
        stops.append(
            {
                "invoice": invoice.get("name"),
                "address": address_name,
                "latitude": latitude if has_pin else None,
                "longitude": longitude if has_pin else None,
                "delivered_at": invoice.get("custom_delivered_at"),
            }
        )

    # One pinned stop plus an origin is a single leg, which says nothing useful
    # about a route. Below two pinned stops the planned distance stays 0 and the
    # detour detector skips the run rather than dividing by something meaningless.
    planned = geo_track.chain_distance_m(points) if pinned >= 2 else 0.0

    return {
        "planned_distance_m": planned,
        "stops_delivered": len(invoices),
        "pinned_stops": pinned,
        "stops": stops,
    }


def _delivered_stops(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Invoices this courier delivered inside the run window, in delivery order.

    Guarded: ``custom_delivered_at`` is jarz_pos lane A1 (contract §2). On a server
    whose jarz_pos predates that migration the filter raises, and the honest answer
    is an empty planned route with a loud log — not a silent zero that would read as
    "this courier delivered nothing".
    """
    party_type = run.get("party_type")
    party = run.get("party")
    start = run.get("started_on")
    end = run.get("ended_on") or now_datetime()
    if not (party_type and party and start):
        return []

    try:
        return frappe.get_all(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "custom_courier_party_type": party_type,
                "custom_courier_party": party,
                "custom_sales_invoice_state": INVOICE_STATE.DELIVERED,
                "custom_delivered_at": ["between", [start, end]],
            },
            fields=[
                "name",
                "customer",
                "shipping_address_name",
                "customer_address",
                "custom_delivered_at",
            ],
            order_by="custom_delivered_at asc",
            limit=QUERY_LIMITS.RUN_STOPS,
        ) or []
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "jarz_courier: delivered-stop lookup failed (is jarz_pos lane A1 deployed?)",
        )
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve(run: Any) -> Optional[Dict[str, Any]]:
    """Accept a run name or an already-loaded row."""
    if isinstance(run, dict):
        if run.get("name") and run.get("status"):
            return dict(run)
        run = run.get("name")
    name = str(run or "").strip()
    if not name:
        return None
    return frappe.db.get_value(DOCTYPE, name, list(RUN_FIELDS), as_dict=True)


def _as_payload(doc: Any) -> Dict[str, Any]:
    return {field: doc.get(field) for field in RUN_FIELDS}

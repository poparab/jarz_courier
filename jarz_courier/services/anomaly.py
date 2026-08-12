"""Anomaly detection over a courier's own GPS data. **Flags only, never charges.**

COURIER_APP_SPEC B9. Every detector here runs off data this app already collected —
the trail in Redis, ``Delivery Proof`` rows, the Address pins jarz_pos owns. No
routing API, no map matching, no external call, no API key, no per-request cost, and
nothing that stops working when a quota runs out.

The one rule that shapes all of it
----------------------------------
**Do not compute money penalties.** Not "do not apply them" — do not *compute* them.
``Courier Anomaly`` carries no monetary field at all (there is a test asserting that),
so there is nowhere for a number to be parked "just for reference". The reason is
that every finding here is derived from consumer GPS, and consumer GPS is
confidently wrong on a regular basis: a 300 m accuracy fix in a covered market, a
tunnel that eats four minutes of pings, an Android that sleeps the app to save
battery. Each of those produces a finding indistinguishable from misconduct. As long
as a finding is a prompt to ask a question, being wrong costs a conversation. The
moment a number on it could reach a payslip, "ask the courier" silently becomes
"deduct unless they appeal", and the appeal is against a black box.

So each finding carries a severity, the measurement, the threshold it crossed, and a
sentence a manager can read out loud. That is the deliverable.

Thresholds and why they are where they are
------------------------------------------
The numbers below are first-pass and expected to move once there is a week of
production data. They are named constants for exactly that reason. Two of them are
load-bearing rather than arbitrary and are documented at their definition: the
speeding band's upper bound, and the detour ratio's baseline.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, now_datetime

from jarz_courier.constants import (
    ANOMALY_STATUS,
    ANOMALY_TYPE,
    DOCTYPES,
    GEO_SOURCE_CUSTOMER_PIN,
    QUERY_LIMITS,
    RUN_STATUS,
    SEVERITY,
)
from jarz_courier.services import courier_run, geo_track, location_cache, pos_bridge

DOCTYPE = DOCTYPES.COURIER_ANOMALY

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────────────────────

#: Detour ratio = actual ridden distance / planned straight-line chain.
#:
#: The denominator is straight-line, so the ratio for a courier who rode the
#: shortest legal route is NOT 1.0 — a road network typically costs 1.2-1.4x the
#: crow-flight distance. That baseline is the reason the medium threshold sits at
#: 1.8 rather than anywhere near 1.0: below it, the number is measuring the street
#: grid, not the courier.
DETOUR_RATIO_MEDIUM = 1.8
DETOUR_RATIO_HIGH = 2.6

#: Below this planned distance the ratio is dominated by GPS error and by where the
#: first fix happened to land, so the run is skipped rather than judged.
DETOUR_MIN_PLANNED_M = 1000.0

#: A stop takes minutes. Twenty of them in a 60 m circle is a break, a queue, or
#: something worth asking about — it is not a delivery.
IDLE_MIN_SECONDS = 20 * 60
IDLE_HIGH_SECONDS = 45 * 60
IDLE_RADIUS_M = 60.0

#: Speeding severity bands, in km/h. The top of the range is capped by
#: ``geo_track.MAX_SPEED_KMH``: anything above that is a bad fix, not riding, and
#: reporting bad fixes as speeding is how a report becomes something nobody opens.
SPEEDING_MEDIUM_KMH = 95.0
SPEEDING_HIGH_KMH = 110.0

#: A gap this long between fixes is worth noticing. It is not proof of anything —
#: an underground car park does it — which is why severity scales with the longest
#: gap rather than the mere existence of one.
PING_GAP_SECONDS = 10 * 60
PING_GAP_MEDIUM_SECONDS = 20 * 60
PING_GAP_HIGH_SECONDS = 45 * 60

#: How far from the Address pin a "Delivered" tap may happen before it is a finding.
#: Widened by the pin's own accuracy when the pin reports one — a pin that is itself
#: only good to 80 m cannot convict anybody of standing 100 m away.
FAR_FROM_PIN_M = 150.0
FAR_FROM_PIN_HIGH_M = 500.0

#: An idle stop this long, this far from every delivered address, is a stop nobody
#: ordered.
UNEXPECTED_STOP_MIN_SECONDS = 15 * 60
UNEXPECTED_STOP_MIN_DISTANCE_M = 250.0

#: Stale-ping watchdog. The alert IS the mitigation: if the courier force-stopped
#: the app, no push, no socket and no server-side trick can wake it, so the only
#: available response is to tell a human that a run went quiet.
STALE_PING_MINUTES = 20
STALE_PING_HIGH_MINUTES = 60
#: After this long with no ping the run is closed as Abandoned so it stops being
#: "open" forever and its polyline gets written while the trail still exists.
STALE_ABANDON_MINUTES = 180

#: Windows the scheduled passes look back over. Generous overlap on purpose — a
#: pass that skipped a run because the scheduler was down for ten minutes would
#: never come back for it. The unique ``dedupe_key`` is what makes overlap free.
PROOF_LOOKBACK_HOURS = 36
RUN_LOOKBACK_HOURS = 48


def _logger():
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


# ─────────────────────────────────────────────────────────────────────────────
# Filing a finding
# ─────────────────────────────────────────────────────────────────────────────

def flag(
    *,
    anomaly_type: str,
    severity: str,
    reason: str,
    party_type: str,
    party: str,
    dedupe_key: str,
    branch: Optional[str] = None,
    run: Optional[str] = None,
    sales_invoice: Optional[str] = None,
    address: Optional[str] = None,
    observed: Optional[float] = None,
    threshold: Optional[float] = None,
    unit: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> Dict[str, Any]:
    """Record one finding, at most once per ``dedupe_key``. Never raises.

    Idempotency is the point of ``dedupe_key`` and it is enforced twice: a cheap
    ``exists`` check first, then the doctype's unique index as the real guarantee.
    The check alone loses to two scheduler workers running the same pass; the index
    alone would fill the Error Log with duplicate-entry tracebacks on every overlap.
    """
    key = str(dedupe_key or "").strip()
    if not key:
        _logger().warning("jarz_courier: refusing to file an anomaly with no dedupe key")
        return {"anomaly": None, "created": False, "reason": "missing dedupe key"}

    try:
        existing = frappe.db.exists(DOCTYPE, {"dedupe_key": key})
        if existing:
            return {"anomaly": existing, "created": False, "reason": "already flagged"}

        doc = frappe.new_doc(DOCTYPE)
        doc.anomaly_type = anomaly_type
        doc.severity = severity
        doc.reason = reason
        doc.party_type = party_type
        doc.party = party
        doc.branch = branch
        doc.run = run
        doc.sales_invoice = sales_invoice
        doc.address = address
        doc.observed_value = flt(observed) if observed is not None else None
        doc.threshold_value = flt(threshold) if threshold is not None else None
        doc.unit = unit
        doc.latitude = flt(latitude) if latitude is not None else None
        doc.longitude = flt(longitude) if longitude is not None else None
        doc.status = ANOMALY_STATUS.OPEN
        doc.detected_on = now_datetime()
        doc.dedupe_key = key
        doc.insert(ignore_permissions=True)

        _logger().info(f"jarz_courier: anomaly {doc.name} {anomaly_type}/{severity} key={key}")
        return {"anomaly": doc.name, "created": True}
    except frappe.DuplicateEntryError:
        return {"anomaly": None, "created": False, "reason": "already flagged"}
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"jarz_courier: anomaly flag failed ({key})")
        return {"anomaly": None, "created": False, "reason": "error"}


# ─────────────────────────────────────────────────────────────────────────────
# Pure severity decisions — no frappe, trivially testable
# ─────────────────────────────────────────────────────────────────────────────

def detour_ratio(actual_m: Any, planned_m: Any) -> Optional[float]:
    """actual / planned, or None when the question cannot be asked.

    Returns None rather than 0 or inf for a missing planned distance. A detour
    ratio of "infinity" would sort to the top of every report and mean nothing.
    """
    try:
        actual = float(actual_m or 0)
        planned = float(planned_m or 0)
    except (TypeError, ValueError):
        return None
    if planned < DETOUR_MIN_PLANNED_M or actual <= 0:
        return None
    return round(actual / planned, 3)


def detour_severity(ratio: Optional[float]) -> Optional[str]:
    if ratio is None:
        return None
    if ratio >= DETOUR_RATIO_HIGH:
        return SEVERITY.HIGH
    if ratio >= DETOUR_RATIO_MEDIUM:
        return SEVERITY.MEDIUM
    return None


def idle_severity(seconds: float) -> Optional[str]:
    if seconds >= IDLE_HIGH_SECONDS:
        return SEVERITY.HIGH
    if seconds >= IDLE_MIN_SECONDS:
        return SEVERITY.MEDIUM
    return None


def speeding_severity(max_kmh: float) -> Optional[str]:
    if max_kmh > geo_track.MAX_SPEED_KMH:
        # Above the physical plausibility ceiling this is a bad fix, and the noise
        # filter has already discarded it from every distance. Refusing to grade it
        # keeps a tower hand-off out of a conduct report.
        return None
    if max_kmh >= SPEEDING_HIGH_KMH:
        return SEVERITY.HIGH
    if max_kmh >= SPEEDING_MEDIUM_KMH:
        return SEVERITY.MEDIUM
    if max_kmh > geo_track.SPEEDING_LIMIT_KMH:
        return SEVERITY.LOW
    return None


def ping_gap_severity(longest_seconds: float) -> Optional[str]:
    if longest_seconds >= PING_GAP_HIGH_SECONDS:
        return SEVERITY.HIGH
    if longest_seconds >= PING_GAP_MEDIUM_SECONDS:
        return SEVERITY.MEDIUM
    if longest_seconds >= PING_GAP_SECONDS:
        return SEVERITY.LOW
    return None


def pin_distance_allowance(pin_accuracy_m: Any) -> float:
    """How far from the pin counts as "at the pin".

    ``FAR_FROM_PIN_M`` plus the pin's own accuracy radius, but only when that radius
    is a real measurement. ``custom_geo_accuracy_m`` is ``NOT NULL DEFAULT 0``, so a
    stored 0 means "not reported" — reading it as "accurate to 0 m" would widen
    nothing and quietly convict couriers whose customer pin came from a maps link
    that never carried an accuracy at all. The question is asked of jarz_pos
    (contract §3) rather than by comparing the raw number.
    """
    if pos_bridge.accuracy_is_known(pin_accuracy_m):
        try:
            return FAR_FROM_PIN_M + max(0.0, float(pin_accuracy_m))
        except (TypeError, ValueError):
            return FAR_FROM_PIN_M
    return FAR_FROM_PIN_M


def far_from_pin_severity(distance_m: float, allowance_m: float) -> Optional[str]:
    if distance_m <= allowance_m:
        return None
    if distance_m >= FAR_FROM_PIN_HIGH_M + max(0.0, allowance_m - FAR_FROM_PIN_M):
        return SEVERITY.HIGH
    return SEVERITY.MEDIUM


# ─────────────────────────────────────────────────────────────────────────────
# Per-run analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_run(run: Dict[str, Any], *, fixes: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run every trail-based detector over one closed run.

    ``fixes`` is the **raw, unfiltered** trail, and it must be. The jitter filter
    deletes exactly the fixes that prove somebody stood still, so an idle detector
    fed filtered data can only see gaps and has to guess what happened inside them.
    Distance uses the filtered track; behaviour uses the raw one. They are different
    questions and they want different inputs.

    Returns ``{"ok": bool, "findings": [...]}``. ``ok`` is False when a detector
    raised — the caller uses that to keep the trail in Redis for a retry instead of
    deleting the only copy of the evidence.
    """
    findings: List[Dict[str, Any]] = []
    ok = True

    branch = str(run.get("branch") or "")
    party = str(run.get("party") or "")
    if fixes is None:
        fixes = location_cache.read_trail(branch, party)
    raw = list(fixes or [])

    for detector in (
        _detect_detour,
        _detect_mock_gps,
        _detect_idle_and_unexpected_stops,
        _detect_speeding,
        _detect_ping_gaps,
    ):
        try:
            findings.extend(detector(run, raw) or [])
        except Exception:
            ok = False
            frappe.log_error(
                frappe.get_traceback(),
                f"jarz_courier: detector {detector.__name__} failed on {run.get('name')}",
            )

    return {"ok": ok, "findings": findings}


def _detect_detour(run: Dict[str, Any], fixes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Actual ridden distance against the planned straight-line chain.

    Reads the two numbers already stored on the run rather than recomputing them, so
    the flag and the report can never disagree about the distance.
    """
    ratio = detour_ratio(run.get("total_distance_m"), run.get("planned_distance_m"))
    severity = detour_severity(ratio)
    if not severity:
        return []

    actual_km = flt(run.get("total_distance_m")) / 1000.0
    planned_km = flt(run.get("planned_distance_m")) / 1000.0
    result = flag(
        anomaly_type=ANOMALY_TYPE.DETOUR,
        severity=severity,
        reason=_(
            "Rode {0} km against a {1} km planned route through {2} stops "
            "(ratio {3}, flagged above {4})."
        ).format(
            round(actual_km, 2),
            round(planned_km, 2),
            int(run.get("stops_delivered") or 0),
            ratio,
            DETOUR_RATIO_MEDIUM,
        ),
        party_type=run.get("party_type"),
        party=run.get("party"),
        branch=run.get("branch"),
        run=run.get("name"),
        observed=ratio,
        threshold=DETOUR_RATIO_MEDIUM,
        unit="ratio",
        dedupe_key=f"detour::{run.get('name')}",
    )
    return [result] if result.get("created") else []


def _detect_mock_gps(run: Dict[str, Any], fixes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The handset admitted its position came from a mock provider.

    ``mock_ping_count`` is stamped at ingest, not derived here, because a mocked fix
    never enters the trail — that is what "refuse to treat it as a real position"
    means in practice. This detector exists so a run whose mock pings all arrived
    while nobody was looking still produces a finding at close.
    """
    count = int(run.get("mock_ping_count") or 0)
    if count <= 0:
        return []

    result = flag(
        anomaly_type=ANOMALY_TYPE.MOCK_GPS,
        severity=SEVERITY.HIGH,
        reason=_(
            "The handset reported {0} position(s) from a mock location provider during "
            "this run. Mocked fixes are excluded from the distance, so the recorded "
            "route is incomplete rather than wrong."
        ).format(count),
        party_type=run.get("party_type"),
        party=run.get("party"),
        branch=run.get("branch"),
        run=run.get("name"),
        observed=float(count),
        threshold=0.0,
        unit="fixes",
        dedupe_key=f"mock_gps::{run.get('name')}",
    )
    return [result] if result.get("created") else []


def _detect_idle_and_unexpected_stops(
    run: Dict[str, Any], fixes: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Long stationary periods, split by whether a customer was there.

    One traversal, two findings, because they are the same measurement asked about
    twice: an idle segment near a delivered address is a slow delivery, the same
    segment 800 m from every address is a stop nobody ordered. Reporting both from
    one pass keeps them consistent — two independent detectors would eventually
    disagree about whether a given stop was "at" an address.
    """
    segments = geo_track.idle_segments(
        fixes, min_seconds=IDLE_MIN_SECONDS, radius_m=IDLE_RADIUS_M
    )
    if not segments:
        return []

    stop_points = _delivered_stop_points(run)
    findings: List[Dict[str, Any]] = []

    for segment in segments:
        minutes = round(segment["seconds"] / 60.0, 1)
        nearest = geo_track.nearest_distance_m(segment["lat"], segment["lng"], stop_points)
        far_from_customers = (
            nearest is not None and nearest >= UNEXPECTED_STOP_MIN_DISTANCE_M
        ) or (nearest is None and bool(stop_points))

        # Bucketed by start time so a re-run of the pass produces the same key even
        # if the segment boundaries shift by a fix or two.
        bucket = int(float(segment["from_epoch"]) // 60)

        if far_from_customers and segment["seconds"] >= UNEXPECTED_STOP_MIN_SECONDS:
            result = flag(
                anomaly_type=ANOMALY_TYPE.UNEXPECTED_STOP,
                severity=idle_severity(segment["seconds"]) or SEVERITY.LOW,
                reason=_(
                    "Stopped for {0} minutes at a location {1} m from the nearest "
                    "delivered address."
                ).format(minutes, int(nearest) if nearest is not None else "?"),
                party_type=run.get("party_type"),
                party=run.get("party"),
                branch=run.get("branch"),
                run=run.get("name"),
                observed=float(nearest) if nearest is not None else None,
                threshold=UNEXPECTED_STOP_MIN_DISTANCE_M,
                unit="m",
                latitude=segment["lat"],
                longitude=segment["lng"],
                dedupe_key=f"unexpected_stop::{run.get('name')}::{bucket}",
            )
        else:
            severity = idle_severity(segment["seconds"])
            if not severity:
                continue
            result = flag(
                anomaly_type=ANOMALY_TYPE.IDLE,
                severity=severity,
                reason=_("Stationary for {0} minutes within a {1} m circle.").format(
                    minutes, int(IDLE_RADIUS_M)
                ),
                party_type=run.get("party_type"),
                party=run.get("party"),
                branch=run.get("branch"),
                run=run.get("name"),
                observed=minutes,
                threshold=round(IDLE_MIN_SECONDS / 60.0, 1),
                unit="minutes",
                latitude=segment["lat"],
                longitude=segment["lng"],
                dedupe_key=f"idle::{run.get('name')}::{bucket}",
            )

        if result.get("created"):
            findings.append(result)

    return findings


def _detect_speeding(run: Dict[str, Any], fixes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One finding per run, graded on the fastest plausible reading."""
    events = geo_track.speeding_events(fixes)
    if not events:
        return []

    fastest = max(events, key=lambda e: e["speed_kmh"])
    severity = speeding_severity(fastest["speed_kmh"])
    if not severity:
        return []

    result = flag(
        anomaly_type=ANOMALY_TYPE.SPEEDING,
        severity=severity,
        reason=_(
            "Reached {0} km/h ({1} reading) and exceeded {2} km/h on {3} occasion(s)."
        ).format(
            fastest["speed_kmh"],
            fastest["source"],
            int(geo_track.SPEEDING_LIMIT_KMH),
            len(events),
        ),
        party_type=run.get("party_type"),
        party=run.get("party"),
        branch=run.get("branch"),
        run=run.get("name"),
        observed=fastest["speed_kmh"],
        threshold=geo_track.SPEEDING_LIMIT_KMH,
        unit="km/h",
        latitude=fastest["lat"],
        longitude=fastest["lng"],
        dedupe_key=f"speeding::{run.get('name')}",
    )
    return [result] if result.get("created") else []


def _detect_ping_gaps(run: Dict[str, Any], fixes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Holes in the track. One finding per run, graded on the longest hole."""
    gaps = geo_track.ping_gaps(fixes, max_gap_seconds=PING_GAP_SECONDS)
    if not gaps:
        return []

    longest = max(gaps, key=lambda g: g["seconds"])
    severity = ping_gap_severity(longest["seconds"])
    if not severity:
        return []

    result = flag(
        anomaly_type=ANOMALY_TYPE.PING_GAP,
        severity=severity,
        reason=_(
            "Sent no position for {0} minutes (longest of {1} gap(s) over {2} minutes). "
            "Signal loss looks identical to a closed app, so this is a prompt to check, "
            "not a conclusion."
        ).format(
            round(longest["seconds"] / 60.0, 1),
            len(gaps),
            round(PING_GAP_SECONDS / 60.0, 1),
        ),
        party_type=run.get("party_type"),
        party=run.get("party"),
        branch=run.get("branch"),
        run=run.get("name"),
        observed=round(longest["seconds"] / 60.0, 1),
        threshold=round(PING_GAP_SECONDS / 60.0, 1),
        unit="minutes",
        latitude=longest.get("lat"),
        longitude=longest.get("lng"),
        dedupe_key=f"ping_gap::{run.get('name')}",
    )
    return [result] if result.get("created") else []


def _delivered_stop_points(run: Dict[str, Any]) -> List[Tuple[Any, Any]]:
    """Pins of the addresses this run actually delivered to."""
    try:
        route = courier_run.planned_route(run, [])
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: stop pin lookup failed")
        return []
    return [
        (stop["latitude"], stop["longitude"])
        for stop in route.get("stops") or []
        if stop.get("latitude") is not None and stop.get("longitude") is not None
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Delivered-far-from-pin — a proof-level detector, not a run-level one
# ─────────────────────────────────────────────────────────────────────────────

def detect_far_from_pin(*, hours: int = PROOF_LOOKBACK_HOURS) -> Dict[str, Any]:
    """Compare each recent POD coordinate against its Address pin.

    Deliberately compared against ``Address.custom_latitude`` and not against
    ``Sales Invoice.custom_delivery_latitude``: the invoice field records where the
    courier stood, which is the same number the proof carries, so comparing them
    would only ever measure the two writes agreeing.

    Skips a proof when the pin's confidence is below ``customer_pin``. A pin derived
    from a territory centroid is a district, not a door — measuring a delivery
    against it produces a 2 km "anomaly" for every address that has never been
    pinned properly, which is most of them.
    """
    since = add_to_date(now_datetime(), hours=-int(hours))
    proofs = _recent_proofs(since)
    if not proofs:
        return {"checked": 0, "flagged": 0}

    address_by_invoice = _addresses_for_invoices([p.get("sales_invoice") for p in proofs])
    minimum_rank = pos_bridge.confidence_rank(GEO_SOURCE_CUSTOMER_PIN)

    checked = 0
    flagged = 0
    geo_cache: Dict[str, Dict[str, Any]] = {}

    for proof in proofs:
        if proof.get("is_mocked"):
            continue
        if not geo_track.is_valid_coordinate(proof.get("latitude"), proof.get("longitude")):
            continue

        address_name = address_by_invoice.get(proof.get("sales_invoice")) or ""
        if not address_name:
            continue

        if address_name not in geo_cache:
            try:
                geo_cache[address_name] = pos_bridge.get_address_geo(address_name)
            except Exception:
                geo_cache[address_name] = {}
        geo = geo_cache[address_name]
        if not geo:
            continue

        pin_lat = geo.get("custom_latitude")
        pin_lng = geo.get("custom_longitude")
        if not geo_track.is_valid_coordinate(pin_lat, pin_lng):
            continue
        if minimum_rank and int(geo.get("rank") or 0) < minimum_rank:
            continue

        checked += 1
        distance = geo_track.haversine_m(
            float(proof["latitude"]), float(proof["longitude"]), float(pin_lat), float(pin_lng)
        )
        allowance = pin_distance_allowance(geo.get("custom_geo_accuracy_m"))
        severity = far_from_pin_severity(distance, allowance)
        if not severity:
            continue

        result = flag(
            anomaly_type=ANOMALY_TYPE.FAR_FROM_PIN,
            severity=severity,
            reason=_(
                "Proof of delivery was captured {0} m from the address pin "
                "(allowed {1} m, pin source {2}). Either the delivery happened "
                "elsewhere or the pin is wrong."
            ).format(int(distance), int(allowance), geo.get("custom_geo_source") or "unknown"),
            party_type=proof.get("party_type"),
            party=proof.get("party"),
            sales_invoice=proof.get("sales_invoice"),
            address=address_name,
            observed=round(distance, 2),
            threshold=round(allowance, 2),
            unit="m",
            latitude=proof.get("latitude"),
            longitude=proof.get("longitude"),
            dedupe_key=f"far_from_pin::{proof.get('name')}",
        )
        if result.get("created"):
            flagged += 1

    return {"checked": checked, "flagged": flagged}


def _recent_proofs(since: Any) -> List[Dict[str, Any]]:
    try:
        return frappe.get_all(
            DOCTYPES.DELIVERY_PROOF,
            filters={"captured_at": [">=", since]},
            fields=[
                "name",
                "sales_invoice",
                "party_type",
                "party",
                "latitude",
                "longitude",
                "accuracy_m",
                "is_mocked",
                "captured_at",
            ],
            order_by="captured_at desc",
            limit=QUERY_LIMITS.PROOFS_PER_CONSENSUS_PASS,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: recent proof lookup failed")
        return []


def _addresses_for_invoices(invoice_names: Sequence[Any]) -> Dict[str, str]:
    """Invoice → delivery address, in one query."""
    names = sorted({str(n).strip() for n in invoice_names if str(n or "").strip()})
    if not names:
        return {}
    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters={"name": ["in", names]},
            fields=["name", "shipping_address_name", "customer_address"],
            limit=len(names),
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: invoice address lookup failed")
        return {}
    return {
        row["name"]: str(row.get("shipping_address_name") or row.get("customer_address") or "")
        for row in rows
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stale-ping watchdog
# ─────────────────────────────────────────────────────────────────────────────

def watch_stale_pings() -> Dict[str, Any]:
    """Flag, alert on, and eventually close runs that have gone quiet.

    **The alert is the mitigation, not a step towards one.** If a courier
    force-stopped the app, Android will not deliver a data message to it, the socket
    is gone, and there is no server-side trick that brings it back. Nothing here
    tries. What it can do is make the silence visible to a person on the branch
    within twenty minutes, which is the actual control.

    Escalation is deliberately two-stage: alert at :data:`STALE_PING_MINUTES` so
    someone can phone the courier, and close as ``Abandoned`` at
    :data:`STALE_ABANDON_MINUTES` so the run stops being open forever *and* its
    polyline gets written while the trail is still in Redis.
    """
    from jarz_courier.services import push  # local: push imports nothing from here
    # Also local, and for a stronger reason than push: duty_session imports
    # courier_run, and courier_run.close_run calls into this module, so a
    # module-level import here would close an import cycle at load time.
    from jarz_courier.services import duty_session

    alerted = 0
    abandoned = 0
    runs = courier_run.stale_open_runs(minutes=STALE_PING_MINUTES)

    for run in runs:
        try:
            silent_minutes = _silent_minutes(run)

            if silent_minutes >= STALE_ABANDON_MINUTES:
                courier_run.close_run(run, status=RUN_STATUS.ABANDONED)
                abandoned += 1
                continue

            if run.get("stale_alert_on"):
                # Already raised for this run. Re-alerting every five minutes for
                # three hours would train ops to ignore the alert entirely.
                continue

            severity = (
                SEVERITY.HIGH if silent_minutes >= STALE_PING_HIGH_MINUTES else SEVERITY.MEDIUM
            )
            reason = _(
                "No position for {0} minutes on an open run. If the app was "
                "force-stopped nothing can wake it remotely — call the courier."
            ).format(int(silent_minutes))

            flag(
                anomaly_type=ANOMALY_TYPE.STALE_PING,
                severity=severity,
                reason=reason,
                party_type=run.get("party_type"),
                party=run.get("party"),
                branch=run.get("branch"),
                run=run.get("name"),
                observed=float(int(silent_minutes)),
                threshold=float(STALE_PING_MINUTES),
                unit="minutes",
                dedupe_key=f"stale_ping::{run.get('name')}",
            )
            push.notify_stale_ping(run=run, silent_minutes=int(silent_minutes), reason=reason)
            courier_run.mark_stale_alerted(run.get("name"))
            alerted += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"jarz_courier: stale watch failed for {run.get('name')}"
            )

    # Duties last, and that order matters: end_duty closes any still-open run as
    # Closed, so sweeping duties first would overwrite the Abandoned verdict the
    # loop above just recorded and erase the distinction between "the courier
    # finished" and "the app went dark".
    duties = duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

    return {
        "examined": len(runs),
        "alerted": alerted,
        "abandoned": abandoned,
        "duties_examined": duties.get("examined", 0),
        "duties_closed": duties.get("closed", 0),
    }


def _silent_minutes(run: Dict[str, Any]) -> float:
    from frappe.utils import time_diff_in_seconds

    marker = run.get("last_ping_on") or run.get("started_on")
    if not marker:
        return 0.0
    try:
        return max(0.0, float(time_diff_in_seconds(now_datetime(), marker)) / 60.0)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Scheduled entry points — must never raise (they run inside the scheduler)
# ─────────────────────────────────────────────────────────────────────────────

def sweep_unchecked_runs() -> Dict[str, Any]:
    """Retry analysis for closed runs whose detector pass never completed.

    ``close_run`` normally analyses inline while the trail is in hand. This is the
    catch-up for the case where it did not — a detector raised, or the run was
    closed by a path that skipped it. Runs whose trail has since expired still get
    the detour and mock findings, which are computed from stored numbers rather than
    from fixes; the behavioural detectors simply find nothing, which is honest.
    """
    since = add_to_date(now_datetime(), hours=-RUN_LOOKBACK_HOURS)
    try:
        runs = frappe.get_all(
            DOCTYPES.COURIER_RUN,
            filters={
                "status": ["in", [RUN_STATUS.CLOSED, RUN_STATUS.ABANDONED]],
                "anomalies_checked_on": ["is", "not set"],
                "ended_on": [">=", since],
            },
            fields=list(courier_run.RUN_FIELDS),
            order_by="ended_on asc",
            limit=QUERY_LIMITS.RUNS_PER_SWEEP,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: unchecked run lookup failed")
        return {"examined": 0, "findings": 0}

    findings = 0
    for run in runs:
        try:
            result = analyse_run(run)
            findings += len(result.get("findings") or [])
            if result.get("ok"):
                courier_run.mark_anomalies_checked(run.get("name"))
                location_cache.drop_trail(run.get("branch"), run.get("party"))
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"jarz_courier: analysis retry failed for {run.get('name')}"
            )

    return {"examined": len(runs), "findings": findings}


def scheduled_detect() -> Dict[str, Any]:
    """Scheduler entry point. Swallows everything.

    A scheduled job that raises writes a traceback into the Scheduled Job Log on
    every tick and, on some Frappe versions, gets disabled after repeated failures —
    which would silently switch off anomaly detection entirely. Each stage is
    isolated so one bad detector cannot take the others down with it.
    """
    summary: Dict[str, Any] = {}
    for name, step in (
        ("stale", watch_stale_pings),
        ("runs", sweep_unchecked_runs),
        ("proofs", detect_far_from_pin),
    ):
        try:
            summary[name] = step()
        except Exception:
            summary[name] = {"error": True}
            frappe.log_error(frappe.get_traceback(), f"jarz_courier: scheduled_detect/{name} failed")
    return summary

"""GPS track arithmetic: the noise filter, the distance, the encoded polyline.

**This module imports nothing.** No frappe, no jarz_pos, no database. Every
function is a pure transform over plain dicts, which is what makes the numbers
here testable at all — and they need to be, because a fuel allowance or a detour
flag built on a wrong distance is wrong in a direction somebody notices.

Two things it deliberately does not do:

* **It does not talk to Redis.** ``services/location_cache`` owns storage.
* **It does not decide anything about a person.** ``services/anomaly`` turns these
  measurements into flags; keeping the measurement pure means a threshold change
  is a one-line edit in one file that cannot alter what "distance" means.

The haversine formula is re-implemented here rather than imported from
``jarz_pos.utils.geo``. That is not the ladder duplication §3 warns about: a
confidence ladder is *policy* and drifts, the great-circle formula is arithmetic
over a fixed constant and cannot. Paying one 8-line duplication buys a module with
zero imports, and ``tests/test_geo_track`` pins it against a known distance.

Why the filter exists, in one paragraph
---------------------------------------
A parked handset does not report one position; it reports a slow random walk of
5-30 m jumps for as long as it is switched on. Summing raw consecutive distances
over an eight-hour shift turns a stationary lunch break into several kilometres of
"travel". Every number downstream — total distance, detour ratio, any per-km
allowance — inherits that inflation, and it inflates in the courier's favour, so
nobody reports it as a bug. The filter is therefore not a nicety: it is the
difference between a measurement and a number that merely looks like one.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Filter thresholds
# ─────────────────────────────────────────────────────────────────────────────

#: A fix whose own reported accuracy radius exceeds this is not a position, it is
#: a cell-tower guess. 50 m is the usual Android boundary between a GNSS fix and a
#: fused network estimate.
MAX_ACCURACY_M = 50.0

#: Movement below this from the last KEPT fix is treated as drift, not travel.
#: Measured against the last *kept* fix rather than the last *seen* one — that is
#: the whole trick. Comparing against the last seen fix lets a parked phone creep
#: 19 m at a time forever and accumulate every one of those steps.
MIN_MOVE_M = 20.0

#: Above this, a motorbike did not do it and the fix is bad (a tower hand-off, a
#: cold start, a cached fix replayed from an offline queue). Used to REJECT fixes.
MAX_SPEED_KMH = 120.0

#: Distinct from the number above and deliberately so: 120 km/h means "impossible,
#: therefore a bad reading"; this means "possible but unsafe, therefore a flag".
#: Conflating the two would make every speeding event disappear into the noise
#: filter and every filtered artefact look like reckless riding.
SPEEDING_LIMIT_KMH = 80.0

#: Earth's mean radius (IUGG). Same value ``jarz_pos.utils.geo`` uses.
EARTH_RADIUS_M = 6371008.8


# ─────────────────────────────────────────────────────────────────────────────
# Coordinates
# ─────────────────────────────────────────────────────────────────────────────

def is_valid_coordinate(lat: Any, lng: Any) -> bool:
    """True for a real point on Earth that is not the null island.

    ``(0, 0)`` is rejected: it is what a handset reports when it has no fix and
    what an uninitialised float field holds. Treating it as a position drags every
    centroid and every distance towards the Gulf of Guinea.
    """
    try:
        latitude = float(lat)
        longitude = float(lng)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return False
    return not (abs(latitude) < 1e-9 and abs(longitude) < 1e-9)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = phi2 - phi1
    d_lambda = math.radians(float(lng2) - float(lng1))
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def distance_between(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """Metres between two fixes, or None when either lacks a usable position."""
    if not (is_valid_coordinate(a.get("lat"), a.get("lng")) and is_valid_coordinate(b.get("lat"), b.get("lng"))):
        return None
    return haversine_m(a["lat"], a["lng"], b["lat"], b["lng"])


# ─────────────────────────────────────────────────────────────────────────────
# The noise filter
# ─────────────────────────────────────────────────────────────────────────────

def accuracy_is_known(value: Any) -> bool:
    """True when a fix carries a real accuracy measurement.

    Mirrors ``jarz_pos.services.geo_resolution.accuracy_is_known`` — 0 means "not
    reported", never "accurate to 0 m", because Frappe builds every Float column
    as ``NOT NULL DEFAULT 0`` and a genuine 0 m fix does not exist. Duplicated
    here only so this module can stay import-free; the Address-field consumers go
    through ``pos_bridge.accuracy_is_known`` as contract §3 requires.
    """
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def implied_speed_kmh(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """Speed the pair of fixes implies, in km/h.

    Returns ``None`` when it cannot be computed, and ``inf`` when two fixes at the
    same instant sit in different places — that is a teleport and it must be
    rejected, not silently treated as "no information". Returning None there would
    let a replayed cached fix through the filter untouched.
    """
    distance = distance_between(a, b)
    if distance is None:
        return None
    try:
        dt = float(b.get("epoch") or 0) - float(a.get("epoch") or 0)
    except (TypeError, ValueError):
        return None
    if dt < 0:
        return None
    if dt == 0:
        return math.inf if distance > 0 else 0.0
    return (distance / dt) * 3.6


def filter_fixes(
    fixes: Sequence[Dict[str, Any]],
    *,
    max_accuracy_m: float = MAX_ACCURACY_M,
    min_move_m: float = MIN_MOVE_M,
    max_speed_kmh: float = MAX_SPEED_KMH,
) -> Dict[str, Any]:
    """Drop the fixes that are not travel, in the only order that works.

    ``fixes`` must already be sorted oldest-first (``location_cache`` reads them
    out of a score-ordered sorted set, so they are).

    Order of the three tests is load-bearing:

    1. **Accuracy.** A 300 m fix is not evidence of anything, so it is discarded
       before it can be used as the anchor for the two comparisons below.
    2. **Speed.** A teleport is by definition a big jump, so the jitter test would
       wave it through. It has to be caught first.
    3. **Jitter.** Everything left is a plausible reading; below ``min_move_m``
       from the last kept fix it is the phone breathing, not the courier moving.

    The first valid fix is always kept — it is the origin, and there is nothing to
    compare it against.

    Returns ``{"kept": [...], "dropped": {...}, "distance_m": float}``. The
    per-reason drop counts are returned rather than logged because they are the
    only way to tell "this courier's phone is bad" from "this courier did not
    move", and both end up on a report.
    """
    kept: List[Dict[str, Any]] = []
    dropped = {"invalid": 0, "accuracy": 0, "speed": 0, "jitter": 0, "mocked": 0}

    for fix in fixes or []:
        if not is_valid_coordinate(fix.get("lat"), fix.get("lng")):
            dropped["invalid"] += 1
            continue

        # A mocked fix never reaches this function in production — ingest keeps it
        # out of the trail entirely — but a caller replaying a stored trail must
        # not be able to reintroduce one.
        if _as_bool(fix.get("is_mocked")):
            dropped["mocked"] += 1
            continue

        accuracy = fix.get("accuracy")
        if accuracy_is_known(accuracy) and float(accuracy) > max_accuracy_m:
            dropped["accuracy"] += 1
            continue

        if not kept:
            kept.append(fix)
            continue

        previous = kept[-1]
        speed = implied_speed_kmh(previous, fix)
        if speed is not None and speed > max_speed_kmh:
            dropped["speed"] += 1
            continue

        moved = distance_between(previous, fix)
        if moved is not None and moved < min_move_m:
            dropped["jitter"] += 1
            continue

        kept.append(fix)

    return {"kept": kept, "dropped": dropped, "distance_m": path_distance_m(kept)}


def path_distance_m(fixes: Sequence[Dict[str, Any]]) -> float:
    """Sum of consecutive great-circle hops, in metres.

    Only meaningful on a *filtered* track. Called on a raw one it returns the
    inflated number the filter exists to prevent, which is why every caller in
    this app passes ``filter_fixes(...)["kept"]``.
    """
    total = 0.0
    for previous, current in _pairs(fixes):
        hop = distance_between(previous, current)
        if hop:
            total += hop
    return round(total, 2)


def chain_distance_m(points: Sequence[Tuple[Any, Any]]) -> float:
    """Straight-line distance along an ordered list of ``(lat, lng)`` pairs.

    The "planned" leg of a detour ratio. Points that are not valid coordinates are
    skipped rather than treated as the origin, so one unpinned stop in the middle
    of a run shortens the plan instead of teleporting it to null island.
    """
    usable = [(float(lat), float(lng)) for lat, lng in points if is_valid_coordinate(lat, lng)]
    total = 0.0
    for index in range(1, len(usable)):
        total += haversine_m(*usable[index - 1], *usable[index])
    return round(total, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Idle and speeding segments — measurements, not verdicts
# ─────────────────────────────────────────────────────────────────────────────

def idle_segments(
    fixes: Sequence[Dict[str, Any]],
    *,
    min_seconds: float,
    radius_m: float,
) -> List[Dict[str, Any]]:
    """Stretches where the courier stayed inside ``radius_m`` for ``min_seconds``.

    Runs on the **unfiltered** track on purpose. The jitter filter deletes exactly
    the fixes that prove someone stood still, so an idle detector fed filtered
    data can only ever see gaps and has to guess what happened in them.

    Anchored on the first fix of the run of nearby fixes, not on a moving centroid:
    a moving centroid follows a slow walk down a street and reports it as one long
    stationary period.
    """
    segments: List[Dict[str, Any]] = []
    usable = [f for f in (fixes or []) if is_valid_coordinate(f.get("lat"), f.get("lng"))]
    if not usable:
        return segments

    anchor_index = 0
    index = 1
    while index <= len(usable):
        at_end = index == len(usable)
        anchor = usable[anchor_index]
        current = None if at_end else usable[index]
        within = False
        if current is not None:
            distance = distance_between(anchor, current)
            within = distance is not None and distance <= radius_m

        if within:
            index += 1
            continue

        last = usable[index - 1]
        seconds = _elapsed(anchor, last)
        if index - 1 > anchor_index and seconds >= min_seconds:
            segments.append(
                {
                    "from_epoch": _epoch(anchor),
                    "to_epoch": _epoch(last),
                    "seconds": round(seconds, 1),
                    "lat": float(anchor["lat"]),
                    "lng": float(anchor["lng"]),
                    "fix_count": index - anchor_index,
                }
            )
        if at_end:
            break
        anchor_index = index
        index += 1

    return segments


def speeding_events(
    fixes: Sequence[Dict[str, Any]],
    *,
    limit_kmh: float = SPEEDING_LIMIT_KMH,
    reject_above_kmh: float = MAX_SPEED_KMH,
) -> List[Dict[str, Any]]:
    """Hops implying a speed between ``limit_kmh`` and ``reject_above_kmh``.

    The upper bound is not a typo. Anything faster than ``reject_above_kmh`` is a
    bad fix, and reporting bad fixes as speeding is how an anomaly report becomes
    something nobody reads. So the band is deliberately open at the bottom and
    closed at the top.

    Prefers the handset's own reported ``speed`` (a Doppler figure, far better than
    a two-point average) and falls back to the implied speed when it is absent.
    """
    events: List[Dict[str, Any]] = []
    for previous, current in _pairs(fixes):
        reported = _reported_speed_kmh(current)
        implied = implied_speed_kmh(previous, current)
        speed = reported if reported is not None else implied
        if speed is None or math.isinf(speed):
            continue
        if limit_kmh < speed <= reject_above_kmh:
            events.append(
                {
                    "epoch": _epoch(current),
                    "speed_kmh": round(speed, 1),
                    "source": "reported" if reported is not None else "implied",
                    "lat": float(current["lat"]),
                    "lng": float(current["lng"]),
                }
            )
    return events


def ping_gaps(
    fixes: Sequence[Dict[str, Any]],
    *,
    max_gap_seconds: float,
) -> List[Dict[str, Any]]:
    """Intervals between consecutive fixes longer than ``max_gap_seconds``.

    A gap is not proof of anything on its own — a courier can lose signal in a
    basement garage — which is why this returns intervals and lets the detector
    decide severity from how many there are and how long they ran.
    """
    gaps: List[Dict[str, Any]] = []
    for previous, current in _pairs(fixes):
        seconds = _elapsed(previous, current)
        if seconds > max_gap_seconds:
            gaps.append(
                {
                    "from_epoch": _epoch(previous),
                    "to_epoch": _epoch(current),
                    "seconds": round(seconds, 1),
                    "lat": float(previous["lat"]) if is_valid_coordinate(previous.get("lat"), previous.get("lng")) else None,
                    "lng": float(previous["lng"]) if is_valid_coordinate(previous.get("lat"), previous.get("lng")) else None,
                }
            )
    return gaps


def nearest_distance_m(
    lat: Any, lng: Any, points: Iterable[Tuple[Any, Any]]
) -> Optional[float]:
    """Distance to the closest of ``points``, or None when there is nothing to compare."""
    if not is_valid_coordinate(lat, lng):
        return None
    best: Optional[float] = None
    for point_lat, point_lng in points or []:
        if not is_valid_coordinate(point_lat, point_lng):
            continue
        distance = haversine_m(float(lat), float(lng), float(point_lat), float(point_lng))
        if best is None or distance < best:
            best = distance
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Clustering — the consensus-pin primitive (B5)
# ─────────────────────────────────────────────────────────────────────────────

def cluster_points(
    points: Sequence[Dict[str, Any]],
    *,
    radius_m: float,
) -> List[List[Dict[str, Any]]]:
    """Greedy single-pass clustering within ``radius_m`` of a seed.

    Not k-means and not DBSCAN. The question here is narrow — "do several
    independent door fixes for one address agree?" — and the input is a handful of
    points, so a seeded sweep is both sufficient and explainable, which matters
    more than optimality when the output moves a customer's map pin.

    Seeds are taken best-accuracy-first so the tightest fix defines the cluster
    centre instead of whichever row the database happened to return first. Fixes
    with no reported accuracy sort last for the same reason.
    """
    remaining = sorted(
        (p for p in (points or []) if is_valid_coordinate(p.get("lat"), p.get("lng"))),
        key=_accuracy_sort_key,
    )
    clusters: List[List[Dict[str, Any]]] = []

    while remaining:
        seed = remaining.pop(0)
        members = [seed]
        rest: List[Dict[str, Any]] = []
        for candidate in remaining:
            distance = distance_between(seed, candidate)
            if distance is not None and distance <= radius_m:
                members.append(candidate)
            else:
                rest.append(candidate)
        remaining = rest
        clusters.append(members)

    clusters.sort(key=len, reverse=True)
    return clusters


def centroid(points: Sequence[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    """Arithmetic mean of ``(lat, lng)``, or None when nothing is usable.

    A plain mean, not a spherical one. Every cluster this is called on spans tens
    of metres; the spherical correction over 40 m is far below the sixth decimal
    place the Address field stores, so the extra trigonometry would buy noise.
    """
    usable = [p for p in (points or []) if is_valid_coordinate(p.get("lat"), p.get("lng"))]
    if not usable:
        return None
    return (
        round(sum(float(p["lat"]) for p in usable) / len(usable), 6),
        round(sum(float(p["lng"]) for p in usable) / len(usable), 6),
    )


def cluster_radius_m(points: Sequence[Dict[str, Any]]) -> float:
    """Distance from the centroid to the furthest member.

    This is the honest uncertainty of a consensus pin: how far apart independent
    observers of the same door actually stood. It is a better number than any one
    handset's self-reported accuracy, which is why it is what gets written.
    """
    hub = centroid(points)
    if hub is None:
        return 0.0
    furthest = 0.0
    for point in points:
        if not is_valid_coordinate(point.get("lat"), point.get("lng")):
            continue
        distance = haversine_m(hub[0], hub[1], float(point["lat"]), float(point["lng"]))
        furthest = max(furthest, distance)
    return round(furthest, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Google encoded polyline (precision 5)
# ─────────────────────────────────────────────────────────────────────────────
#
# One string per run instead of one database row per ping. A 10-hour shift at a
# fix every 10 seconds is ~3,600 rows per courier per day; encoded it is a few KB
# in one Long Text column. Nothing queries individual pings — the questions are
# all "how far did this run go" and "draw it on a map" — so rows would be pure
# cost. Precision 5 (~1.1 m) is the format Google Maps, Mapbox and every Flutter
# polyline widget expect; precision 6 would render as a line across the ocean in a
# reader that assumes 5, and there is no marker in the string to tell them apart.


def encode_polyline(points: Sequence[Tuple[Any, Any]], *, precision: int = 5) -> str:
    """Encode ``(lat, lng)`` pairs to a Google encoded polyline."""
    factor = 10**precision
    output: List[str] = []
    previous_lat = 0
    previous_lng = 0

    for lat, lng in points or []:
        if not is_valid_coordinate(lat, lng):
            continue
        scaled_lat = int(round(float(lat) * factor))
        scaled_lng = int(round(float(lng) * factor))
        output.append(_encode_value(scaled_lat - previous_lat))
        output.append(_encode_value(scaled_lng - previous_lng))
        previous_lat = scaled_lat
        previous_lng = scaled_lng

    return "".join(output)


def decode_polyline(encoded: str, *, precision: int = 5) -> List[Tuple[float, float]]:
    """Inverse of :func:`encode_polyline`. Exists so the encoder can be tested.

    A write-only encoder is an encoder nobody has checked. It is also what a
    support engineer needs when asked "where did this run actually go?" from a
    bench console.
    """
    factor = 10**precision
    coordinates: List[Tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0
    text = str(encoded or "")

    while index < len(text):
        for axis in range(2):
            shift = 0
            result = 0
            while index < len(text):
                byte = ord(text[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if axis == 0:
                lat += delta
            else:
                lng += delta
        coordinates.append((round(lat / factor, precision), round(lng / factor, precision)))

    return coordinates


def encode_track(fixes: Sequence[Dict[str, Any]]) -> str:
    """:func:`encode_polyline` over a list of fix dicts."""
    return encode_polyline([(f.get("lat"), f.get("lng")) for f in fixes or []])


def _encode_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else (value << 1)
    chunks: List[str] = []
    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    chunks.append(chr(value + 63))
    return "".join(chunks)


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pairs(fixes: Sequence[Dict[str, Any]]):
    items = list(fixes or [])
    for index in range(1, len(items)):
        yield items[index - 1], items[index]


def _epoch(fix: Dict[str, Any]) -> float:
    try:
        return float(fix.get("epoch") or 0)
    except (TypeError, ValueError):
        return 0.0


def _elapsed(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    return max(0.0, _epoch(b) - _epoch(a))


def _reported_speed_kmh(fix: Dict[str, Any]) -> Optional[float]:
    """The handset's own speed, converted from m/s.

    Android reports ``Location.speed`` in metres per second and reports ``0.0``
    both for "stationary" and for "this provider does not do speed". Treating the
    ambiguous zero as a measurement is harmless here — a 0 km/h reading can never
    trip a speeding threshold — so it is passed through rather than guessed at.
    """
    value = fix.get("speed")
    if value in (None, ""):
        return None
    try:
        return max(0.0, float(value)) * 3.6
    except (TypeError, ValueError):
        return None


def _accuracy_sort_key(point: Dict[str, Any]) -> Tuple[int, float]:
    accuracy = point.get("accuracy")
    if accuracy_is_known(accuracy):
        return (0, float(accuracy))
    return (1, 0.0)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

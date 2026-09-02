"""Redis storage for courier positions. **The ORM is deliberately absent.**

Why no DocType for a ping
-------------------------
``frappe.get_doc(...).insert()`` costs 10-30 ms: naming, validation, the
``before_insert``/``after_insert`` hook chain, a Version row, a
``modified``/``owner`` stamp. Multiply by one fix every 5-10 seconds per courier
per shift and the write cost is real, the InnoDB growth is unbounded, and the rows
answer no question anybody asks — every downstream question is "where is this
courier now?" (one value, needed for seconds) or "how far did this run go?" (one
number, computed once at close). Neither wants a row per fix.

So the hot path is Redis and the cold path is exactly one ``Courier Run`` row
holding one encoded polyline. This module is the hot half.

The key contract — DO NOT CHANGE THESE SHAPES
---------------------------------------------
``courier:loc:{branch}:{party}`` is read by the **customer-facing tracking
endpoint in ``jarz_pos/api/tracking.py``**, across an app boundary, so it is a wire
contract and not an implementation detail. Renaming it silently breaks a screen
this app cannot see.

======================================================  ==============  ==========
key                                                     type            TTL
======================================================  ==============  ==========
``courier:loc:{branch}:{party}``                         JSON string     15 min
``courier:trail:{branch}:{party}``                       sorted set      12 h
``courier:branch_couriers:{branch}``                     set             6 h
``courier:pubthrottle:{branch}:{party}``                 marker          15 s
``courier:dbtouch:{branch}:{party}``                     marker          60 s
``courier:runset:{branch}:{party}``                      JSON string     24 h
======================================================  ==============  ==========

Payload of the position key, exactly as consumers must expect it::

    {
      "lat": 30.044123, "lng": 31.235678,
      "heading": 187.4,          # degrees, may be null
      "speed": 8.3,              # METRES PER SECOND, as Android reports it
      "accuracy": 12.0,          # metres; 0 means NOT REPORTED, never "0 m"
      "ts": "2026-08-08 14:03:11",   # handset clock, not the server's
      "is_mocked": 0,            # 1 => the position is a lie, see below
      "epoch": 1786000991.0,     # unix seconds; the sortable form of `ts`
      "party_type": "Employee", "party": "HR-EMP-00042",
      "branch": "Dokki", "run": "CRUN-00001"
    }

Three properties a consumer must honour:

* **``is_mocked`` is not advisory.** A fix with ``is_mocked: 1`` is stored — ops
  needs to see that a courier is faking — but it is not a position. It never
  reaches the trail, so it never touches distance, and any consumer showing it must
  show it as suspect.
* **``accuracy: 0`` means "not reported".** Same ambiguity as
  ``Address.custom_geo_accuracy_m`` (contract §3) and resolved the same way.
* **The key is constructed, never parsed.** ``branch`` is a user-named POS Profile
  and ``party`` a document name; either could in principle contain a colon, so
  splitting the key back apart is unsound. Every reader already knows the
  ``(branch, party)`` pair it is asking about.

Redis is a cache and is allowed to be empty
-------------------------------------------
A flush loses live positions and un-flushed trail points. That is survivable by
construction: the durable facts live on ``Courier Run`` (``last_ping_on``,
``ping_count``, and at close the polyline), and the stale-ping watchdog reads
*that*, not Redis. A watchdog built on key expiry would go blind at exactly the
moment it is needed — a killed app stops pinging, the key expires, and there is
then nothing left to notice.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import frappe

from jarz_courier.constants import OWNTRACKS

#: 15 minutes. Long enough that a courier in a lift is still "on the map", short
#: enough that a finished shift stops appearing as live.
LOCATION_TTL_SEC = 15 * 60

#: A shift plus slack. The trail only has to survive until the run is closed and
#: the polyline is written; after that it is dead weight and is deleted explicitly.
TRAIL_TTL_SEC = 12 * 60 * 60

#: Refreshed on every ping, so an idle branch's index disappears on its own.
BRANCH_INDEX_TTL_SEC = 6 * 60 * 60

#: Cap on trail points held per run. A 10 h shift at one accepted fix / 10 s is
#: ~3,600; 5,000 leaves headroom without letting a misbehaving client push an
#: unbounded set into Redis. Oldest points are dropped first and the count of
#: what was dropped is recorded on the run, so a truncated polyline is visible
#: rather than merely short.
MAX_TRAIL_POINTS = 5000

#: One realtime publish per courier per this many seconds. Every ping publishing
#: to every user on the branch turns 20 couriers into a firehose the ops board
#: spends its frame budget parsing; the map cannot show 1 Hz movement usefully
#: anyway.
PUBLISH_THROTTLE_SEC = 15

#: One ``Courier Run.last_ping_on`` write per courier per this many seconds. The
#: watchdog needs a durable last-seen time, so it cannot be pure Redis — but it
#: does not need per-ping resolution. At 60 s this is ~1 UPDATE/min/courier.
DB_TOUCH_THROTTLE_SEC = 60

#: Assignment snapshot lifetime for the new-assignment sweep.
RUN_SNAPSHOT_TTL_SEC = 24 * 60 * 60


# ─────────────────────────────────────────────────────────────────────────────
# Key builders — the contract above, in code
# ─────────────────────────────────────────────────────────────────────────────

def position_key(branch: str, party: str) -> str:
    """``courier:loc:{branch}:{party}`` — consumed across the app boundary."""
    return f"courier:loc:{branch}:{party}"


def trail_key(branch: str, party: str) -> str:
    return f"courier:trail:{branch}:{party}"


def branch_index_key(branch: str) -> str:
    return f"courier:branch_couriers:{branch}"


def publish_throttle_key(branch: str, party: str) -> str:
    return f"courier:pubthrottle:{branch}:{party}"


def db_touch_key(branch: str, party: str) -> str:
    return f"courier:dbtouch:{branch}:{party}"


def owntracks_mode_key(branch: str, party: str) -> str:
    """Last monitoring mode the courier's OwnTracks app reported (``m`` field)."""
    return f"courier:otmode:{branch}:{party}"


def owntracks_waypoints_key(branch: str, party: str) -> str:
    """Fingerprint of the stop geofences last pushed to the device."""
    return f"courier:otwp:{branch}:{party}"


def owntracks_steer_key(branch: str, party: str) -> str:
    return f"courier:otsteer:{branch}:{party}"


def run_snapshot_key(branch: str, party: str) -> str:
    return f"courier:runset:{branch}:{party}"


def index_member(party_type: str, party: str) -> str:
    """How a courier is named inside the per-branch index set.

    ``party_type`` is included because ``Employee`` and ``Supplier`` names come
    from different sequences and could collide.
    """
    return f"{party_type}::{party}"


def split_index_member(member: Any) -> Tuple[str, str]:
    """Inverse of :func:`index_member`. Tolerates bytes from redis-py."""
    text = member.decode("utf-8") if isinstance(member, bytes) else str(member or "")
    if "::" not in text:
        return ("", text)
    party_type, party = text.split("::", 1)
    return (party_type, party)


# ─────────────────────────────────────────────────────────────────────────────
# Cache access
# ─────────────────────────────────────────────────────────────────────────────

def _cache():
    """The one place ``frappe.cache()`` is called. Patched wholesale by tests."""
    return frappe.cache()


def _logger():
    """A logger that actually emits.

    ``frappe.logger()`` defaults to level ERROR off a dev machine, so ``.info()``
    and ``.warning()`` vanish on staging and production — a bug this codebase has
    already paid for once. The level is set explicitly every time.
    """
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


# ─────────────────────────────────────────────────────────────────────────────
# Last known position
# ─────────────────────────────────────────────────────────────────────────────

def write_position(branch: str, party: str, fix: Dict[str, Any]) -> bool:
    """Store *fix* as the courier's last known position. Never raises.

    Stored as a **JSON string**, not a pickled dict. ``frappe.cache().set_value``
    pickles by default, which couples every reader to this app's Python objects and
    to a pickle protocol version; the key crosses an app boundary, so it has to be
    something a reader can parse without trusting us. Readers should still accept
    both forms — :func:`read_position` does.
    """
    if not (branch and party):
        return False
    try:
        _cache().set_value(
            position_key(branch, party),
            json.dumps(fix, default=str),
            expires_in_sec=LOCATION_TTL_SEC,
        )
        return True
    except Exception:
        # Losing a position write must never fail the courier's request. The next
        # ping is 5 seconds away and the durable facts are on the run row.
        _logger().warning(f"jarz_courier: position write failed for {branch}/{party}")
        return False


def read_position(branch: str, party: str) -> Optional[Dict[str, Any]]:
    """The courier's last known position, or None when it has expired.

    ``expires=True`` is passed to ``get_value`` deliberately: without it the
    wrapper copies the value into ``frappe.local.cache``, where it outlives its
    Redis TTL for the rest of the request. A long-running scheduled job would then
    read a position it had already established was stale.
    """
    if not (branch and party):
        return None
    try:
        raw = _cache().get_value(position_key(branch, party), expires=True)
    except Exception:
        return None
    return _as_payload(raw)


def read_branch_positions(branch: str) -> List[Dict[str, Any]]:
    """Every live position on a branch, pruning index entries that have expired.

    Reads the index set and then one key per courier, rather than scanning Redis
    with ``KEYS courier:loc:{branch}:*``. ``KEYS`` is O(keyspace) and blocks the
    single-threaded server; on a shared Redis that is a site-wide stall triggered
    by an ops board refresh.

    The prune is lazy on purpose. A courier whose position key has expired is off
    the map, and removing them here — during a read that already established the
    fact — is cheaper and more reliable than a second scheduled job whose only duty
    is tidying a set.
    """
    if not branch:
        return []
    try:
        members = _cache().smembers(branch_index_key(branch)) or []
    except Exception:
        _logger().warning(f"jarz_courier: branch index read failed for {branch}")
        return []

    positions: List[Dict[str, Any]] = []
    stale: List[Any] = []
    for member in members:
        party_type, party = split_index_member(member)
        if not party:
            stale.append(member)
            continue
        payload = read_position(branch, party)
        if not payload:
            stale.append(member)
            continue
        payload.setdefault("party_type", party_type)
        payload.setdefault("party", party)
        payload.setdefault("branch", branch)
        positions.append(payload)

    if stale:
        try:
            _cache().srem(branch_index_key(branch), *stale)
        except Exception:
            pass

    positions.sort(key=lambda p: float(p.get("epoch") or 0), reverse=True)
    return positions


def register_in_branch_index(branch: str, party_type: str, party: str) -> None:
    """Add the courier to the branch's live set and refresh its TTL. Never raises."""
    if not (branch and party):
        return
    key = branch_index_key(branch)
    try:
        cache = _cache()
        cache.sadd(key, index_member(party_type, party))
        cache.expire_key(key, BRANCH_INDEX_TTL_SEC)
    except Exception:
        _logger().warning(f"jarz_courier: branch index write failed for {branch}/{party}")


def clear_position(branch: str, party: str) -> None:
    """Drop the live position and the index entry. Used when a run closes."""
    if not (branch and party):
        return
    try:
        cache = _cache()
        cache.delete_value(position_key(branch, party))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# The trail — a sorted set, because backlogs arrive out of order
# ─────────────────────────────────────────────────────────────────────────────
#
# A Redis LIST would be the obvious choice and it is the wrong one. An offline
# queue flush hands over a backlog in whatever order the client drained it, and it
# can arrive *after* newer live fixes have already been appended. Splicing an old
# fix into the middle of a list means reading it, sorting it and writing it back
# with no lock held. A sorted set scored by timestamp puts every fix in its place
# on insert, whenever it turns up, and makes "have I already got a fix for this
# instant?" an O(log n) range count instead of a scan.
#
# The wrapper has no zset helpers, so these calls go to redis-py directly and the
# key must be namespaced by hand with ``make_key`` — the helpers do that for you,
# but they only cover string, hash, list and set types. Forgetting it writes to an
# unprefixed key shared by every site on the bench.


def append_fixes(branch: str, party: str, fixes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Add *fixes* to the trail, deduped by timestamp. Never raises.

    Returns ``{"added": int, "duplicate": int, "trimmed": int}``.

    Dedupe is by whole-second timestamp: two fixes claiming the same instant cannot
    both be true, and an offline queue that replays a page twice produces exactly
    that. The *first* one seen wins — an arbitrary but stable choice, and the
    alternative (last wins) would let a replay overwrite a good fix with a worse
    duplicate.

    The **score is the truncated second**, not the raw timestamp, which is what makes
    that dedupe a single exact ``zcount`` rather than a floating-point range query.
    Sub-second precision is not lost: the full ``epoch`` stays inside the JSON payload
    and :func:`read_trail` sorts on it. Nothing needs finer ordering than a second
    anyway — no GPS provider emits two meaningful fixes inside one.
    """
    result = {"added": 0, "duplicate": 0, "trimmed": 0}
    if not (branch and party and fixes):
        return result

    try:
        cache = _cache()
        key = cache.make_key(trail_key(branch, party))
    except Exception:
        _logger().warning(f"jarz_courier: trail key build failed for {branch}/{party}")
        return result

    for fix in fixes:
        try:
            epoch = float(fix.get("epoch") or 0)
        except (TypeError, ValueError):
            continue
        if epoch <= 0:
            continue
        bucket = float(int(epoch))
        try:
            if cache.zcount(key, bucket, bucket):
                result["duplicate"] += 1
                continue
            cache.zadd(key, {json.dumps(fix, default=str): bucket})
            result["added"] += 1
        except Exception:
            _logger().warning(f"jarz_courier: trail append failed for {branch}/{party}")
            return result

    try:
        # Keep the newest MAX_TRAIL_POINTS. Negative ranks count from the end, so
        # this removes everything older than the tail window.
        removed = cache.zremrangebyrank(key, 0, -(MAX_TRAIL_POINTS + 1))
        result["trimmed"] = int(removed or 0)
        cache.expire(key, TRAIL_TTL_SEC)
    except Exception:
        pass

    return result


def read_trail(branch: str, party: str) -> List[Dict[str, Any]]:
    """The whole trail, oldest first. Never raises."""
    if not (branch and party):
        return []
    try:
        cache = _cache()
        members = cache.zrange(cache.make_key(trail_key(branch, party)), 0, -1) or []
    except Exception:
        _logger().warning(f"jarz_courier: trail read failed for {branch}/{party}")
        return []

    fixes: List[Dict[str, Any]] = []
    for member in members:
        payload = _as_payload(member)
        if payload:
            fixes.append(payload)
    fixes.sort(key=lambda f: float(f.get("epoch") or 0))
    return fixes


def drop_trail(branch: str, party: str) -> None:
    """Delete the trail. Called only after its polyline has been persisted."""
    if not (branch and party):
        return
    try:
        cache = _cache()
        cache.delete(cache.make_key(trail_key(branch, party)))
    except Exception:
        pass


def latest_trail_epoch(branch: str, party: str) -> float:
    """Newest fix in the trail as a unix second, or 0.0 when the trail is empty.

    Second resolution, because that is what the sorted-set score carries (see
    :func:`append_fixes`). Diagnostic only — it lets a caller say "you sent me stale
    data" without reading the whole trail. It is deliberately *not* used to reject a
    backlog fix: a sorted set can hold an old fix in its correct place, so there is no
    reason to refuse one.
    """
    if not (branch and party):
        return 0.0
    try:
        cache = _cache()
        rows = cache.zrange(
            cache.make_key(trail_key(branch, party)), -1, -1, withscores=True
        ) or []
    except Exception:
        return 0.0
    if not rows:
        return 0.0
    try:
        return float(rows[0][1])
    except (TypeError, ValueError, IndexError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Throttles
# ─────────────────────────────────────────────────────────────────────────────

def should_publish(branch: str, party: str) -> bool:
    """True at most once per :data:`PUBLISH_THROTTLE_SEC` per courier.

    Fails **open**: if Redis is unreachable the answer is "yes, publish". A silent
    ops board is worse than a chatty one, and the throttle is an optimisation
    rather than a correctness rule.
    """
    return _claim(publish_throttle_key(branch, party), PUBLISH_THROTTLE_SEC)


def should_touch_run(branch: str, party: str) -> bool:
    """True at most once per :data:`DB_TOUCH_THROTTLE_SEC` per courier.

    Also fails open, for a sharper reason: the write it gates is what the
    stale-ping watchdog reads. A throttle that failed closed on a Redis blip would
    make an active courier look abandoned and page ops about it.
    """
    return _claim(db_touch_key(branch, party), DB_TOUCH_THROTTLE_SEC)


def _claim(key: str, ttl_sec: int) -> bool:
    """Set *key* only if absent, returning whether this caller won the window."""
    try:
        cache = _cache()
        won = cache.set(cache.make_key(key), b"1", ex=ttl_sec, nx=True)
        return bool(won)
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# OwnTracks steering state (iPhone couriers)
# ─────────────────────────────────────────────────────────────────────────────

def write_owntracks_mode(branch: str, party: str, mode: Any) -> None:
    """Remember the ``m`` (monitoring mode) the device last reported. Never raises."""
    if not (branch and party) or mode is None:
        return
    try:
        _cache().set_value(
            owntracks_mode_key(branch, party),
            str(int(mode)),
            expires_in_sec=OWNTRACKS.MODE_TTL_SEC,
        )
    except Exception:
        _logger().warning(f"jarz_courier: owntracks mode write failed for {branch}/{party}")


def read_owntracks_mode(branch: str, party: str) -> Optional[int]:
    if not (branch and party):
        return None
    try:
        raw = _cache().get_value(owntracks_mode_key(branch, party), expires=True)
    except Exception:
        return None
    if raw in (None, ""):
        return None
    try:
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None


def read_waypoints_fingerprint(branch: str, party: str) -> Optional[str]:
    if not (branch and party):
        return None
    try:
        raw = _cache().get_value(owntracks_waypoints_key(branch, party), expires=True)
    except Exception:
        return None
    if raw in (None, ""):
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def remember_waypoints_fingerprint(branch: str, party: str, fingerprint: str) -> None:
    """Never raises. A failed write only means the set is pushed again next window."""
    if not (branch and party):
        return
    try:
        _cache().set_value(
            owntracks_waypoints_key(branch, party),
            str(fingerprint),
            expires_in_sec=OWNTRACKS.WAYPOINTS_TTL_SEC,
        )
    except Exception:
        _logger().warning(f"jarz_courier: waypoint fingerprint write failed for {branch}/{party}")


def should_steer(branch: str, party: str) -> bool:
    """True at most once per :data:`OWNTRACKS.STEER_THROTTLE_SEC` per courier.

    Fails **open** like the other throttles: a Redis blip should cost one extra
    run-sheet query, not a courier stuck in the wrong mode until Redis returns.
    """
    return _claim(owntracks_steer_key(branch, party), OWNTRACKS.STEER_THROTTLE_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# Assignment snapshot (feeds the new-assignment push)
# ─────────────────────────────────────────────────────────────────────────────

def read_run_snapshot(branch: str, party: str) -> Optional[List[str]]:
    """The invoice list this courier was last known to be carrying.

    ``None`` and ``[]`` mean different things and must not be collapsed: ``None`` is
    "we have never looked", ``[]`` is "we looked and the run was empty". Pushing
    "you have 6 new orders" the first time the sweep ever runs — which is what
    treating None as [] does — greets every courier with a notification for work
    they were already given.
    """
    if not (branch and party):
        return None
    try:
        raw = _cache().get_value(run_snapshot_key(branch, party), expires=True)
    except Exception:
        return None
    if raw in (None, ""):
        return None
    if isinstance(raw, list):
        return [str(item) for item in raw]
    payload = _as_payload(raw)
    if isinstance(payload, dict):
        return [str(item) for item in payload.get("invoices") or []]
    return None


def write_run_snapshot(branch: str, party: str, invoices: Sequence[str]) -> None:
    if not (branch and party):
        return
    try:
        _cache().set_value(
            run_snapshot_key(branch, party),
            json.dumps({"invoices": [str(i) for i in invoices]}),
            expires_in_sec=RUN_SNAPSHOT_TTL_SEC,
        )
    except Exception:
        _logger().warning(f"jarz_courier: run snapshot write failed for {branch}/{party}")


# ─────────────────────────────────────────────────────────────────────────────
# Decoding
# ─────────────────────────────────────────────────────────────────────────────

def _as_payload(raw: Any) -> Optional[Dict[str, Any]]:
    """Accept a dict, a JSON string or JSON bytes.

    Tolerant on purpose. This app writes JSON, but ``frappe.cache().set_value``
    pickles by default, so a value written by any other code path — or by an older
    build of this one — arrives as a dict. Refusing it would turn a format change
    into an outage rather than a migration.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return dict(parsed) if isinstance(parsed, dict) else None
    return None

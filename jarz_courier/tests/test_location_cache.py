"""``services/location_cache`` — the Redis layer, and a WIRE CONTRACT.

``courier:loc:{branch}:{party}`` is read by ``jarz_pos/api/tracking.py``, across an
app boundary this app cannot see into. That makes the key shape and the payload keys a
published interface, not an implementation detail: renaming either silently breaks a
customer-facing screen, and nothing in this repository would fail. The tests below are
therefore the enforcement — they assert literal strings on purpose, so a rename has to
be a deliberate edit to a test that says why it exists.

Everything else here is about the properties the ingest path relies on:

* the position is JSON, not a pickle, because a cross-app reader must be able to parse
  it without importing our classes;
* the trail is a sorted set, so an out-of-order backlog lands in the right place;
* dedupe is by whole-second timestamp, first-one-wins;
* the branch index is read with a set lookup, never ``KEYS`` — a blocking O(keyspace)
  scan triggered by an ops board refresh would stall every site on the bench;
* both throttles fail **open**, because a silent ops board and a courier who looks
  abandoned are both worse than a redundant write.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.services import location_cache  # noqa: E402


class FakeCache:
    """Enough of Frappe's RedisWrapper to exercise the real call sequence.

    Deliberately records the *namespaced* key for the raw redis calls, so a missing
    ``make_key`` shows up as a test failure rather than as a key shared between every
    site on the bench.
    """

    PREFIX = "testsite|"

    def __init__(self):
        self.values = {}
        self.sets = {}
        self.zsets = {}
        self.expiries = {}
        self.keys_called = 0

    # ── string helpers (these namespace for you) ────────────────────────
    def make_key(self, key, user=None, shared=False):
        return f"{self.PREFIX}{key}"

    def set_value(self, key, value, user=None, expires_in_sec=None, shared=False):
        self.values[self.make_key(key)] = value
        self.expiries[self.make_key(key)] = expires_in_sec

    def get_value(self, key, generator=None, user=None, expires=False, shared=False, **kwargs):
        return self.values.get(self.make_key(key))

    def delete_value(self, keys, **kwargs):
        for key in keys if isinstance(keys, (list, tuple)) else [keys]:
            self.values.pop(self.make_key(key), None)

    def expire_key(self, key, time, **kwargs):
        self.expiries[self.make_key(key)] = time

    # ── set helpers (these namespace for you) ───────────────────────────
    def sadd(self, name, *values):
        self.sets.setdefault(self.make_key(name), set()).update(values)

    def srem(self, name, *values):
        self.sets.setdefault(self.make_key(name), set()).difference_update(values)

    def smembers(self, name):
        return set(self.sets.get(self.make_key(name), set()))

    # ── raw redis (these DO NOT namespace — the caller must) ────────────
    def set(self, name, value, ex=None, nx=False):
        if nx and name in self.values:
            return None
        self.values[name] = value
        self.expiries[name] = ex
        return True

    def delete(self, name):
        self.zsets.pop(name, None)
        self.values.pop(name, None)

    def expire(self, name, seconds):
        self.expiries[name] = seconds

    def zadd(self, name, mapping):
        self.zsets.setdefault(name, {}).update(mapping)

    def zcount(self, name, low, high):
        return sum(1 for score in self.zsets.get(name, {}).values() if low <= score <= high)

    def zrange(self, name, start, stop, withscores=False):
        items = sorted(self.zsets.get(name, {}).items(), key=lambda kv: kv[1])
        if stop == -1:
            window = items[start:]
        else:
            window = items[start : stop + 1]
        return window if withscores else [member for member, _score in window]

    def zremrangebyrank(self, name, start, stop):
        items = sorted(self.zsets.get(name, {}).items(), key=lambda kv: kv[1])
        if stop < 0:
            stop = len(items) + stop
        victims = items[start : stop + 1]
        for member, _score in victims:
            self.zsets[name].pop(member, None)
        return len(victims)

    def keys(self, pattern):  # pragma: no cover - must never be called
        self.keys_called += 1
        return []


def a_fix(lat=30.044420, lng=31.235712, epoch=1786000991.0, **extra):
    payload = {
        "lat": lat,
        "lng": lng,
        "heading": 187.4,
        "speed": 8.3,
        "accuracy": 12.0,
        "ts": "2026-08-08 14:03:11",
        "epoch": epoch,
        "is_mocked": 0,
        "party_type": "Employee",
        "party": "HR-EMP-00042",
        "branch": "Dokki",
        "run": "CRUN-00001",
    }
    payload.update(extra)
    return payload


class CacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = FakeCache()
        patcher = patch.object(location_cache, "_cache", return_value=self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestTheKeyContract(unittest.TestCase):
    """These literals are read by another app. Changing one is a breaking change."""

    def test_position_key_shape_is_frozen(self) -> None:
        self.assertEqual(
            "courier:loc:Dokki:HR-EMP-00042",
            location_cache.position_key("Dokki", "HR-EMP-00042"),
        )

    def test_the_ttl_is_fifteen_minutes(self) -> None:
        self.assertEqual(15 * 60, location_cache.LOCATION_TTL_SEC)

    def test_the_helper_keys_do_not_collide_with_the_position_namespace(self) -> None:
        """A branch literally named "index" must not be able to shadow the index key.

        The index deliberately does NOT live at ``courier:loc:index:{branch}`` for this
        reason — that would sit inside the position key's own namespace.
        """
        position = location_cache.position_key("index", "HR-EMP-1")
        index = location_cache.branch_index_key("Dokki")
        trail = location_cache.trail_key("Dokki", "HR-EMP-1")
        self.assertNotEqual(position, index)
        self.assertFalse(index.startswith("courier:loc:"))
        self.assertFalse(trail.startswith("courier:loc:"))

    def test_index_membership_round_trips(self) -> None:
        member = location_cache.index_member("Employee", "HR-EMP-00042")
        self.assertEqual(("Employee", "HR-EMP-00042"), location_cache.split_index_member(member))

    def test_index_membership_tolerates_bytes_from_redis(self) -> None:
        self.assertEqual(
            ("Employee", "HR-EMP-00042"),
            location_cache.split_index_member(b"Employee::HR-EMP-00042"),
        )


class TestPosition(CacheTestCase):
    def test_the_payload_is_stored_as_parseable_json(self) -> None:
        """Not a pickle. A cross-app reader must not need our Python objects."""
        location_cache.write_position("Dokki", "HR-EMP-00042", a_fix())

        raw = self.cache.values["testsite|courier:loc:Dokki:HR-EMP-00042"]
        self.assertIsInstance(raw, str)
        parsed = json.loads(raw)
        for key in ("lat", "lng", "heading", "speed", "accuracy", "ts", "is_mocked"):
            self.assertIn(key, parsed, f"the documented payload must carry {key}")

    def test_the_position_is_written_with_the_documented_ttl(self) -> None:
        location_cache.write_position("Dokki", "HR-EMP-00042", a_fix())
        self.assertEqual(
            location_cache.LOCATION_TTL_SEC,
            self.cache.expiries["testsite|courier:loc:Dokki:HR-EMP-00042"],
        )

    def test_a_position_round_trips(self) -> None:
        location_cache.write_position("Dokki", "HR-EMP-00042", a_fix())
        result = location_cache.read_position("Dokki", "HR-EMP-00042")
        self.assertEqual(30.044420, result["lat"])
        self.assertEqual(1786000991.0, result["epoch"])

    def test_an_expired_position_reads_as_none(self) -> None:
        self.assertIsNone(location_cache.read_position("Dokki", "HR-EMP-00042"))

    def test_a_pickled_dict_from_another_writer_is_still_readable(self) -> None:
        """Tolerant on read: set_value pickles by default, so a dict can turn up."""
        self.cache.values["testsite|courier:loc:Dokki:HR-EMP-1"] = {"lat": 30.0, "lng": 31.0}
        self.assertEqual(30.0, location_cache.read_position("Dokki", "HR-EMP-1")["lat"])

    def test_corrupt_json_reads_as_none_rather_than_raising(self) -> None:
        self.cache.values["testsite|courier:loc:Dokki:HR-EMP-1"] = "{not json"
        self.assertIsNone(location_cache.read_position("Dokki", "HR-EMP-1"))

    def test_a_redis_failure_does_not_fail_the_write(self) -> None:
        """A lost position must never fail a courier's request — the next ping is 5 s away."""
        self.cache.set_value = MagicMock(side_effect=RuntimeError("redis down"))
        self.assertFalse(location_cache.write_position("Dokki", "HR-EMP-1", a_fix()))

    def test_a_missing_branch_or_party_is_refused(self) -> None:
        self.assertFalse(location_cache.write_position("", "HR-EMP-1", a_fix()))
        self.assertFalse(location_cache.write_position("Dokki", "", a_fix()))


class TestBranchIndex(CacheTestCase):
    def test_a_courier_is_registered_and_the_index_ttl_refreshed(self) -> None:
        location_cache.register_in_branch_index("Dokki", "Employee", "HR-EMP-00042")

        self.assertIn(
            "Employee::HR-EMP-00042",
            self.cache.sets["testsite|courier:branch_couriers:Dokki"],
        )
        self.assertEqual(
            location_cache.BRANCH_INDEX_TTL_SEC,
            self.cache.expiries["testsite|courier:branch_couriers:Dokki"],
        )

    def test_branch_positions_never_scan_the_keyspace(self) -> None:
        """``KEYS`` blocks single-threaded Redis for every site on the bench."""
        location_cache.register_in_branch_index("Dokki", "Employee", "HR-EMP-1")
        location_cache.write_position("Dokki", "HR-EMP-1", a_fix())

        location_cache.read_branch_positions("Dokki")
        self.assertEqual(0, self.cache.keys_called)

    def test_it_returns_live_couriers_newest_first(self) -> None:
        location_cache.register_in_branch_index("Dokki", "Employee", "HR-EMP-1")
        location_cache.register_in_branch_index("Dokki", "Employee", "HR-EMP-2")
        location_cache.write_position("Dokki", "HR-EMP-1", a_fix(epoch=1000.0, party="HR-EMP-1"))
        location_cache.write_position("Dokki", "HR-EMP-2", a_fix(epoch=2000.0, party="HR-EMP-2"))

        positions = location_cache.read_branch_positions("Dokki")
        self.assertEqual(["HR-EMP-2", "HR-EMP-1"], [p["party"] for p in positions])

    def test_an_expired_courier_is_pruned_from_the_index_lazily(self) -> None:
        """Pruned during the read that already established they were gone."""
        location_cache.register_in_branch_index("Dokki", "Employee", "HR-EMP-GONE")
        location_cache.register_in_branch_index("Dokki", "Employee", "HR-EMP-HERE")
        location_cache.write_position("Dokki", "HR-EMP-HERE", a_fix(party="HR-EMP-HERE"))

        positions = location_cache.read_branch_positions("Dokki")

        self.assertEqual(1, len(positions))
        self.assertEqual(
            {"Employee::HR-EMP-HERE"},
            self.cache.sets["testsite|courier:branch_couriers:Dokki"],
        )

    def test_an_unnamed_branch_returns_nothing_rather_than_everything(self) -> None:
        self.assertEqual([], location_cache.read_branch_positions(""))


class TestTrail(CacheTestCase):
    KEY = "testsite|courier:trail:Dokki:HR-EMP-1"

    def test_fixes_are_scored_by_timestamp(self) -> None:
        location_cache.append_fixes("Dokki", "HR-EMP-1", [a_fix(epoch=1000.0)])
        self.assertEqual([1000.0], list(self.cache.zsets[self.KEY].values()))

    def test_an_out_of_order_backlog_reads_back_in_time_order(self) -> None:
        """The point of a sorted set: an old fix slots in, it does not append."""
        location_cache.append_fixes(
            "Dokki",
            "HR-EMP-1",
            [a_fix(epoch=3000.0, lat=30.03), a_fix(epoch=1000.0, lat=30.01)],
        )
        trail = location_cache.read_trail("Dokki", "HR-EMP-1")
        self.assertEqual([1000.0, 3000.0], [f["epoch"] for f in trail])

    def test_the_same_second_is_a_duplicate_and_the_first_one_wins(self) -> None:
        result_a = location_cache.append_fixes("Dokki", "HR-EMP-1", [a_fix(epoch=1000.2, lat=30.01)])
        result_b = location_cache.append_fixes("Dokki", "HR-EMP-1", [a_fix(epoch=1000.8, lat=30.09)])

        self.assertEqual(1, result_a["added"])
        self.assertEqual(0, result_b["added"])
        self.assertEqual(1, result_b["duplicate"])
        self.assertEqual([30.01], [f["lat"] for f in location_cache.read_trail("Dokki", "HR-EMP-1")])

    def test_a_replayed_queue_page_adds_nothing(self) -> None:
        batch = [a_fix(epoch=1000.0), a_fix(epoch=1010.0), a_fix(epoch=1020.0)]
        first = location_cache.append_fixes("Dokki", "HR-EMP-1", batch)
        second = location_cache.append_fixes("Dokki", "HR-EMP-1", batch)

        self.assertEqual(3, first["added"])
        self.assertEqual(0, second["added"])
        self.assertEqual(3, second["duplicate"])

    def test_the_trail_is_capped_and_reports_what_it_dropped(self) -> None:
        with patch.object(location_cache, "MAX_TRAIL_POINTS", 3):
            result = location_cache.append_fixes(
                "Dokki",
                "HR-EMP-1",
                [a_fix(epoch=1000.0 + i * 10, lat=30.0 + i * 0.001) for i in range(5)],
            )

        self.assertEqual(5, result["added"])
        self.assertEqual(2, result["trimmed"], "the oldest two are evicted")
        self.assertEqual(3, len(location_cache.read_trail("Dokki", "HR-EMP-1")))

    def test_a_fix_with_no_timestamp_is_skipped(self) -> None:
        result = location_cache.append_fixes("Dokki", "HR-EMP-1", [{"lat": 30.0, "lng": 31.0}])
        self.assertEqual(0, result["added"])

    def test_the_trail_ttl_is_set_on_the_namespaced_key(self) -> None:
        location_cache.append_fixes("Dokki", "HR-EMP-1", [a_fix(epoch=1000.0)])
        self.assertEqual(location_cache.TRAIL_TTL_SEC, self.cache.expiries[self.KEY])

    def test_dropping_the_trail_removes_it(self) -> None:
        location_cache.append_fixes("Dokki", "HR-EMP-1", [a_fix(epoch=1000.0)])
        location_cache.drop_trail("Dokki", "HR-EMP-1")
        self.assertEqual([], location_cache.read_trail("Dokki", "HR-EMP-1"))

    def test_the_latest_epoch_is_readable_without_reading_everything(self) -> None:
        location_cache.append_fixes(
            "Dokki", "HR-EMP-1", [a_fix(epoch=1000.0), a_fix(epoch=5000.0)]
        )
        self.assertEqual(5000.0, location_cache.latest_trail_epoch("Dokki", "HR-EMP-1"))

    def test_an_empty_trail_has_no_latest_epoch(self) -> None:
        self.assertEqual(0.0, location_cache.latest_trail_epoch("Dokki", "HR-EMP-1"))

    def test_a_redis_failure_mid_batch_does_not_raise(self) -> None:
        self.cache.zadd = MagicMock(side_effect=RuntimeError("redis down"))
        result = location_cache.append_fixes("Dokki", "HR-EMP-1", [a_fix(epoch=1000.0)])
        self.assertEqual(0, result["added"])


class TestThrottles(CacheTestCase):
    def test_the_publish_throttle_admits_one_caller_per_window(self) -> None:
        self.assertTrue(location_cache.should_publish("Dokki", "HR-EMP-1"))
        self.assertFalse(location_cache.should_publish("Dokki", "HR-EMP-1"))

    def test_the_publish_throttle_is_per_courier(self) -> None:
        self.assertTrue(location_cache.should_publish("Dokki", "HR-EMP-1"))
        self.assertTrue(location_cache.should_publish("Dokki", "HR-EMP-2"))

    def test_the_run_touch_throttle_admits_one_caller_per_window(self) -> None:
        self.assertTrue(location_cache.should_touch_run("Dokki", "HR-EMP-1"))
        self.assertFalse(location_cache.should_touch_run("Dokki", "HR-EMP-1"))

    def test_both_throttles_fail_open_when_redis_is_down(self) -> None:
        """A silent ops board and a courier who looks abandoned are both worse.

        The run-touch case is the sharper one: the write it gates is what the
        stale-ping watchdog reads, so failing closed would page ops about an
        active courier.
        """
        self.cache.set = MagicMock(side_effect=RuntimeError("redis down"))
        self.assertTrue(location_cache.should_publish("Dokki", "HR-EMP-1"))
        self.assertTrue(location_cache.should_touch_run("Dokki", "HR-EMP-1"))


class TestRunSnapshot(CacheTestCase):
    def test_never_observed_and_observed_empty_are_different(self) -> None:
        """Collapsing them tells every courier their whole day is brand new."""
        self.assertIsNone(location_cache.read_run_snapshot("Dokki", "HR-EMP-1"))

        location_cache.write_run_snapshot("Dokki", "HR-EMP-1", [])
        self.assertEqual([], location_cache.read_run_snapshot("Dokki", "HR-EMP-1"))

    def test_a_snapshot_round_trips_in_order(self) -> None:
        location_cache.write_run_snapshot("Dokki", "HR-EMP-1", ["INV-2", "INV-1"])
        self.assertEqual(["INV-2", "INV-1"], location_cache.read_run_snapshot("Dokki", "HR-EMP-1"))

    def test_a_snapshot_carries_the_documented_ttl(self) -> None:
        location_cache.write_run_snapshot("Dokki", "HR-EMP-1", ["INV-1"])
        self.assertEqual(
            location_cache.RUN_SNAPSHOT_TTL_SEC,
            self.cache.expiries["testsite|courier:runset:Dokki:HR-EMP-1"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

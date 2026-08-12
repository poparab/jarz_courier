"""``services/tracking`` — ingest, and the mock-location rule.

The behaviour under test that matters most is the one a courier will actively try to
break. A mock-location app is a free download, the incentive to fake a delivery
position is obvious, and Android tells us when it happens. The rule implemented here
is stronger than "store the flag":

**A mocked fix never becomes a position and never enters the trail.**

The alternative — store it flagged and let consumers decide — was rejected because the
position key is read by a customer-facing tracking screen in another app. Making that
screen's correctness depend on every future consumer remembering to check a boolean is
a guarantee that one of them eventually will not, and the failure is showing a customer
a fabricated courier location. Refusing the write makes it impossible instead.

The other half is the offline queue. A drained queue arrives out of order, with
duplicates, hours late, and sometimes twice. None of that is an error condition, and
none of it may move the live position backwards.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.constants import ANOMALY_TYPE, LOCAL_WS_EVENTS  # noqa: E402
from jarz_courier.services import tracking  # noqa: E402

NOW = 1786000000.0



class TestOwnTracksMapping(unittest.TestCase):
    """Every field is renamed, and the reason each one matters.

    ``normalise_fix`` is forgiving by design, which is exactly what makes an
    unmapped OwnTracks payload dangerous: it reads ``lat``/``lon``, so coordinates
    land, and then silently defaults everything else. The result looks like a
    healthy fix and is wrong four ways at once. A smoke test that only checks "a
    dot appeared" passes on all four of those bugs, so each is asserted here.
    """

    def payload(self, **extra):
        base = {
            "_type": "location",
            "lat": 30.044420,
            "lon": 31.235712,
            "tst": int(NOW),
            "acc": 12,
            "cog": 187,
            "vel": 36,
            "batt": 84,
            "tid": "ab",
        }
        base.update(extra)
        return base

    def test_a_location_report_maps_every_field(self):
        fix = tracking.owntracks_to_fix(self.payload())

        self.assertAlmostEqual(30.044420, fix["lat"])
        self.assertAlmostEqual(31.235712, fix["lng"])
        self.assertEqual(int(NOW), fix["epoch"])
        self.assertEqual(12, fix["accuracy"])
        self.assertEqual(187, fix["heading"])

    def test_velocity_is_converted_from_kmh_to_ms(self):
        """36 km/h is 10 m/s. Unconverted it would read as 36 m/s = 130 km/h.

        Nothing downstream converts, and geo_track derives its speeding thresholds
        from this number directly, so every courier would look like a speeder.
        """
        fix = tracking.owntracks_to_fix(self.payload(vel=36))
        self.assertAlmostEqual(10.0, fix["speed"], places=6)

    def test_the_handset_clock_is_kept_for_both_epoch_and_ts(self):
        """`ts` and `epoch` must describe the same instant.

        Passing `tst` as `epoch` alone leaves `_resolve_timestamp` with no `ts`, so
        it stamps str(now_datetime()) — the SERVER clock — for the display half.
        Courier Run.last_ping_on comes from `ts`, and the stale-run watchdog reads
        that, so the two disagreeing means a courier who handed over a buffered
        morning looks like they just reported.
        """
        fix = tracking.owntracks_to_fix(self.payload(tst=int(NOW)))

        self.assertIn("ts", fix)
        # The harness runs the site in UTC, so `ts` is `epoch` rendered.
        import datetime as _dt

        expected = _dt.datetime.fromtimestamp(NOW, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.assertEqual(expected, fix["ts"])

    def test_a_missing_accuracy_is_absent_rather_than_zero(self):
        """0.0 already means "not reported" downstream — do not manufacture it.

        OwnTracks omits optional fields rather than sending zeros, so a key that is
        simply not there must not become a value that reads as measured.
        """
        fix = tracking.owntracks_to_fix(self.payload(acc=None))
        self.assertNotIn("accuracy", fix)

    def test_non_location_messages_are_refused_for_the_caller_to_200(self):
        """OwnTracks retries a non-2xx forever, so these cannot be errors."""
        for message_type in ("transition", "waypoint", "lwt", "beacon", "card", "cmd"):
            with self.subTest(message_type=message_type):
                self.assertIsNone(
                    tracking.owntracks_to_fix(self.payload(_type=message_type))
                )

    def test_a_missing_or_junk_type_is_refused(self):
        for payload in ({}, {"_type": ""}, {"_type": None}, "not a dict", None):
            with self.subTest(payload=payload):
                self.assertIsNone(tracking.owntracks_to_fix(payload))

    def test_the_type_check_is_case_insensitive(self):
        self.assertIsNotNone(tracking.owntracks_to_fix(self.payload(_type="Location")))

    def test_is_mocked_is_always_zero_because_ios_cannot_report_it(self):
        """Documented weakness, asserted so nobody later reads it as a guarantee."""
        fix = tracking.owntracks_to_fix(self.payload())
        self.assertEqual(0, fix["is_mocked"])

    def test_a_mapped_payload_survives_normalise_fix_intact(self):
        """The mapping is only correct if the pipeline then keeps the values.

        Asserted end to end because the failure mode is a field name that this
        module renames and `normalise_fix` still does not recognise — which no test
        of either function alone would catch.
        """
        fix = tracking.owntracks_to_fix(self.payload())
        normalised = tracking.normalise_fix(
            fix,
            party_type="Employee",
            party="HR-EMP-1",
            branch="Dokki",
            now_epoch=NOW + 5,
        )

        self.assertIsNotNone(normalised)
        self.assertAlmostEqual(30.044420, normalised["lat"])
        self.assertAlmostEqual(31.235712, normalised["lng"])
        self.assertEqual(12.0, normalised["accuracy"])
        self.assertEqual(187.0, normalised["heading"])
        self.assertAlmostEqual(10.0, normalised["speed"], places=6)
        self.assertEqual(0, normalised["is_mocked"])
        # The handset's second, not the server's.
        self.assertEqual(int(NOW), int(normalised["epoch"]))


class TestNormalisation(unittest.TestCase):
    def kwargs(self, **extra):
        base = {
            "party_type": "Employee",
            "party": "HR-EMP-00042",
            "branch": "Dokki",
            "run": "CRUN-00001",
            "now_epoch": NOW,
        }
        base.update(extra)
        return base

    def test_a_normal_ping_normalises(self) -> None:
        fix = tracking.normalise_fix(
            {
                "lat": 30.0444201,
                "lng": 31.2357123,
                "accuracy": 12.0,
                "heading": 187.4,
                "speed": 8.3,
                "epoch": NOW - 10,
                "is_mocked": False,
            },
            **self.kwargs(),
        )

        self.assertEqual(30.04442, fix["lat"])
        self.assertEqual(31.235712, fix["lng"], "stored at the Address field's 6 places")
        self.assertEqual(0, fix["is_mocked"])
        self.assertEqual("CRUN-00001", fix["run"])
        self.assertEqual("Dokki", fix["branch"])

    def test_the_older_field_names_are_still_accepted(self) -> None:
        """An old build in the wild sends latitude/longitude/isMocked.

        Accepting all the spellings is cheaper than a forced app update, and a rejected
        ping is a position lost for good.
        """
        fix = tracking.normalise_fix(
            {"latitude": 30.0444, "longitude": 31.2357, "isMocked": True, "epoch": NOW},
            **self.kwargs(),
        )
        self.assertEqual(30.0444, fix["lat"])
        self.assertEqual(1, fix["is_mocked"])

    def test_an_invalid_coordinate_is_dropped_rather_than_raising(self) -> None:
        """900 good fixes must not be lost because the 400th was malformed."""
        self.assertIsNone(tracking.normalise_fix({"lat": 0, "lng": 0}, **self.kwargs()))
        self.assertIsNone(tracking.normalise_fix({"lat": "north"}, **self.kwargs()))
        self.assertIsNone(tracking.normalise_fix("not a dict", **self.kwargs()))

    def test_the_handset_timestamp_is_stored_as_given(self) -> None:
        """Not overwritten with the server clock — that would collapse a queued run.

        A four-hour morning of fixes stamped with the moment the courier found signal
        reads as a four-second run, which destroys every idle, gap and speed
        measurement built on it.
        """
        fix = tracking.normalise_fix(
            {
                "lat": 30.0444,
                "lng": 31.2357,
                "ts": "2026-08-08 09:15:00",
                "epoch": NOW - 3600,
            },
            **self.kwargs(),
        )
        self.assertEqual("2026-08-08 09:15:00", fix["ts"])
        self.assertEqual(NOW - 3600, fix["epoch"])

    def test_a_ping_carrying_only_a_timestamp_is_accepted(self) -> None:
        """``epoch`` is optional; the handset's ``ts`` alone is enough to order by."""
        from datetime import datetime

        stamp = datetime.fromtimestamp(NOW - 3600).strftime("%Y-%m-%d %H:%M:%S")
        fix = tracking.normalise_fix(
            {"lat": 30.0444, "lng": 31.2357, "ts": stamp}, **self.kwargs()
        )

        self.assertIsNotNone(fix)
        self.assertEqual(stamp, fix["ts"])
        self.assertAlmostEqual(NOW - 3600, fix["epoch"], delta=1.0)

    def test_a_handset_dated_in_the_far_future_is_refused(self) -> None:
        """Without the ceiling, a phone reporting 2031 pins the position forever.

        Every genuine fix afterwards would compare as older and be silently discarded.
        """
        self.assertIsNone(
            tracking.normalise_fix(
                {"lat": 30.0444, "lng": 31.2357, "epoch": NOW + 400 * 24 * 3600},
                **self.kwargs(),
            )
        )

    def test_a_plausible_timezone_offset_still_gets_through(self) -> None:
        """A misconfigured device is at most ~14 h out. Rejecting those rejects the device."""
        fix = tracking.normalise_fix(
            {"lat": 30.0444, "lng": 31.2357, "epoch": NOW + 13 * 3600}, **self.kwargs()
        )
        self.assertIsNotNone(fix)

    def test_an_ancient_fix_is_refused(self) -> None:
        self.assertIsNone(
            tracking.normalise_fix(
                {"lat": 30.0444, "lng": 31.2357, "epoch": NOW - 30 * 24 * 3600},
                **self.kwargs(),
            )
        )

    def test_a_ping_with_no_timestamp_falls_back_to_the_server_clock(self) -> None:
        fix = tracking.normalise_fix({"lat": 30.0444, "lng": 31.2357}, **self.kwargs())
        self.assertIsNotNone(fix)
        self.assertAlmostEqual(NOW, fix["epoch"], delta=1.0)

    def test_missing_accuracy_becomes_zero_meaning_unreported(self) -> None:
        """0 is "not reported", never "accurate to 0 m"."""
        fix = tracking.normalise_fix({"lat": 30.0444, "lng": 31.2357}, **self.kwargs())
        self.assertEqual(0.0, fix["accuracy"])


class IngestTestCase(unittest.TestCase):
    """Every collaborator is replaced, so these tests assert the orchestration only."""

    def setUp(self) -> None:
        self.cache = MagicMock()
        self.cache.append_fixes.return_value = {"added": 0, "duplicate": 0, "trimmed": 0}
        self.cache.read_position.return_value = None
        self.cache.should_publish.return_value = True
        self.cache.write_position.return_value = True

        self.runs = MagicMock()
        self.runs.ensure_open_run.return_value = {
            "run": {
                "name": "CRUN-00001",
                "party_type": "Employee",
                "party": "HR-EMP-00042",
                "branch": "Dokki",
            },
            "created": False,
        }

        self.bridge = MagicMock()
        self.bridge.publish_to_branches.return_value = ["ops@example.com"]

        self.anomaly = MagicMock()
        self.anomaly.flag.return_value = {"created": True}

        for name, replacement in (
            ("location_cache", self.cache),
            ("courier_run", self.runs),
            ("pos_bridge", self.bridge),
            ("anomaly", self.anomaly),
        ):
            patcher = patch.object(tracking, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ingest(self, pings, **extra):
        return tracking.ingest(
            party_type="Employee",
            party="HR-EMP-00042",
            branch="Dokki",
            pings=pings,
            **extra,
        )

    @staticmethod
    def ping(lat=30.0444, lng=31.2357, epoch=None, **extra):
        payload = {"lat": lat, "lng": lng, "epoch": epoch if epoch is not None else time.time()}
        payload.update(extra)
        return payload


class TestSinglePing(IngestTestCase):
    def test_it_stores_publishes_and_touches_the_run(self) -> None:
        self.cache.append_fixes.return_value = {"added": 1, "duplicate": 0, "trimmed": 0}

        result = self.ingest([self.ping()])

        self.assertEqual("CRUN-00001", result["run"])
        self.assertEqual(1, result["accepted"])
        self.assertTrue(result["position_moved"])
        self.assertTrue(result["published"])
        self.cache.register_in_branch_index.assert_called_once_with(
            "Dokki", "Employee", "HR-EMP-00042"
        )
        self.runs.touch_run.assert_called_once()

    def test_the_realtime_event_is_the_locally_declared_location_event(self) -> None:
        """Contract §7 froze jarz_pos/constants.py with no location event among the six."""
        self.ingest([self.ping()])

        event, payload, profiles = self.bridge.publish_to_branches.call_args.args
        self.assertEqual(LOCAL_WS_EVENTS.COURIER_LOCATION_UPDATED, event)
        self.assertEqual(["Dokki"], profiles, "branch-scoped, never site-wide")
        self.assertEqual("HR-EMP-00042", payload["party"])

    def test_the_publish_is_throttled(self) -> None:
        self.cache.should_publish.return_value = False
        result = self.ingest([self.ping()])

        self.assertFalse(result["published"])
        self.bridge.publish_to_branches.assert_not_called()

    def test_an_empty_batch_does_nothing_at_all(self) -> None:
        result = self.ingest([])

        self.assertEqual(0, result["received"])
        self.runs.ensure_open_run.assert_not_called()
        self.cache.append_fixes.assert_not_called()

    def test_a_missing_branch_is_refused(self) -> None:
        with self.assertRaises(Exception):
            tracking.ingest(
                party_type="Employee", party="HR-EMP-1", branch="", pings=[self.ping()]
            )


class TestOfflineBacklog(IngestTestCase):
    def test_an_out_of_order_batch_is_sorted_before_anything_is_stored(self) -> None:
        self.ingest(
            [
                self.ping(epoch=NOW + 30, lat=30.03),
                self.ping(epoch=NOW, lat=30.01),
                self.ping(epoch=NOW + 15, lat=30.02),
            ]
        )

        _branch, _party, stored = self.cache.append_fixes.call_args.args
        self.assertEqual([NOW, NOW + 15, NOW + 30], [f["epoch"] for f in stored])

    def test_the_live_position_only_moves_forward(self) -> None:
        """A tunnel's worth of backlog must not teleport the courier back to the tunnel."""
        self.cache.read_position.return_value = {"epoch": NOW + 1000}

        result = self.ingest([self.ping(epoch=NOW)])

        self.assertFalse(result["position_moved"])
        self.cache.write_position.assert_not_called()
        self.bridge.publish_to_branches.assert_not_called()

    def test_a_backlog_still_reaches_the_trail_even_when_the_position_does_not_move(self) -> None:
        """The trail is a sorted set — an old fix belongs in it, in its right place."""
        self.cache.read_position.return_value = {"epoch": NOW + 1000}
        self.cache.append_fixes.return_value = {"added": 3, "duplicate": 0, "trimmed": 0}

        result = self.ingest([self.ping(epoch=NOW + i) for i in range(3)])

        self.cache.append_fixes.assert_called_once()
        self.assertEqual(3, result["accepted"])

    def test_duplicates_are_reported_back_so_the_client_can_trim_its_queue(self) -> None:
        self.cache.append_fixes.return_value = {"added": 1, "duplicate": 4, "trimmed": 0}

        result = self.ingest([self.ping(epoch=NOW + i) for i in range(5)])

        self.assertEqual(1, result["accepted"])
        self.assertEqual(4, result["duplicate"])

    def test_an_oversized_batch_is_truncated_not_refused(self) -> None:
        """A partial flush the client can retry beats a rejected one it cannot."""
        from jarz_courier.constants import QUERY_LIMITS

        result = self.ingest(
            [self.ping(epoch=NOW + i) for i in range(QUERY_LIMITS.PINGS_PER_BATCH + 50)]
        )
        self.assertEqual(QUERY_LIMITS.PINGS_PER_BATCH, result["received"])

    def test_unparseable_fixes_are_counted_and_the_rest_survive(self) -> None:
        self.cache.append_fixes.return_value = {"added": 2, "duplicate": 0, "trimmed": 0}

        result = self.ingest(
            [self.ping(epoch=NOW), {"lat": 0, "lng": 0}, self.ping(epoch=NOW + 10)]
        )

        self.assertEqual(1, result["rejected"])
        self.assertEqual(2, result["accepted"])


class TestMockLocationRefusal(IngestTestCase):
    def test_a_mocked_fix_never_reaches_the_trail(self) -> None:
        self.ingest([self.ping(is_mocked=True)])
        self.cache.append_fixes.assert_not_called()

    def test_a_mocked_fix_never_becomes_the_live_position(self) -> None:
        """This is what protects the customer-facing tracking screen by construction."""
        self.ingest([self.ping(is_mocked=True)])
        self.cache.write_position.assert_not_called()
        self.cache.register_in_branch_index.assert_not_called()

    def test_a_mocked_fix_is_counted_on_the_run_immediately(self) -> None:
        """force=True bypasses the once-a-minute throttle — the first one matters."""
        self.ingest([self.ping(is_mocked=True), self.ping(epoch=NOW + 5, is_mocked=True)])

        self.runs.touch_run.assert_called_once()
        kwargs = self.runs.touch_run.call_args.kwargs
        self.assertEqual(2, kwargs["mocked"])
        self.assertTrue(kwargs["force"])

    def test_a_mocked_fix_files_one_high_severity_finding_per_run(self) -> None:
        """A spoofing courier sends hundreds; the dedupe key is the run, not the ping."""
        self.ingest([self.ping(is_mocked=True)])

        kwargs = self.anomaly.flag.call_args.kwargs
        self.assertEqual(ANOMALY_TYPE.MOCK_GPS, kwargs["anomaly_type"])
        self.assertEqual("High", kwargs["severity"])
        self.assertEqual("mock_gps::CRUN-00001", kwargs["dedupe_key"])

    def test_the_branch_is_alerted_over_the_socket(self) -> None:
        self.ingest([self.ping(is_mocked=True)])

        event, payload, profiles = self.bridge.publish_to_branches.call_args.args
        self.assertEqual(LOCAL_WS_EVENTS.COURIER_ALERT, event)
        self.assertEqual(["Dokki"], profiles)
        self.assertEqual(ANOMALY_TYPE.MOCK_GPS, payload["kind"])

    def test_a_mixed_batch_keeps_the_real_fixes_and_drops_only_the_mocked_ones(self) -> None:
        self.cache.append_fixes.return_value = {"added": 2, "duplicate": 0, "trimmed": 0}

        result = self.ingest(
            [
                self.ping(epoch=NOW, lat=30.01),
                self.ping(epoch=NOW + 10, lat=30.02, is_mocked=True),
                self.ping(epoch=NOW + 20, lat=30.03),
            ]
        )

        self.assertEqual(1, result["mocked"])
        self.assertEqual(2, result["accepted"])
        _branch, _party, stored = self.cache.append_fixes.call_args.args
        self.assertEqual([30.01, 30.03], [f["lat"] for f in stored])

    def test_the_position_written_from_a_mixed_batch_is_the_newest_real_fix(self) -> None:
        self.ingest(
            [
                self.ping(epoch=NOW, lat=30.01),
                self.ping(epoch=NOW + 99, lat=30.99, is_mocked=True),
            ]
        )

        _branch, _party, written = self.cache.write_position.call_args.args
        self.assertEqual(30.01, written["lat"], "the mocked fix must not win on recency")


class TestBranchPositions(unittest.TestCase):
    def test_it_reads_redis_and_nothing_else(self) -> None:
        """Polled as a realtime fallback, so it must stay free of database work."""
        with patch.object(tracking.location_cache, "read_branch_positions", return_value=[{"party": "HR-EMP-1"}]):
            result = tracking.branch_positions("Dokki")

        self.assertEqual("Dokki", result["branch"])
        self.assertEqual(1, result["count"])
        self.assertEqual(tracking.location_cache.LOCATION_TTL_SEC, result["ttl_seconds"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

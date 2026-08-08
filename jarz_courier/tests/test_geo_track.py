"""``services/geo_track`` — the arithmetic every downstream number depends on.

These are the tests that matter most in the tracking feature, because everything else
is transport. If the filter is wrong, the distance is wrong; if the distance is wrong,
the detour ratio is wrong and a courier gets asked about a route they rode correctly.
And the specific way it goes wrong is one-directional: unfiltered GPS drift only ever
*adds* distance, so the error favours the courier and nobody reports it as a bug.

Four properties under test:

* **Drift while parked adds no distance.** The 20 m jitter rule is measured against the
  last KEPT fix, not the last seen one. Against the last seen fix, a phone creeping
  19 m at a time accumulates every step and a lunch break becomes several kilometres.
* **A teleport is rejected before the jitter rule can wave it through.** A bad fix is a
  big jump, so order of the three tests is load-bearing.
* **The polyline is a real Google encoded polyline.** Pinned against the canonical
  three-point vector from Google's own documentation, because a write-only encoder is
  an encoder nobody has checked — and there is no precision marker in the string, so a
  reader assuming a different precision draws a line across the ocean rather than
  erroring.
* **Idle detection runs on unfiltered data.** The jitter filter deletes exactly the
  fixes that prove somebody stood still.
"""

from __future__ import annotations

import math
import unittest

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.services import geo_track  # noqa: E402


def fix(lat, lng, epoch, *, accuracy=5.0, speed=None, mocked=0):
    return {
        "lat": lat,
        "lng": lng,
        "epoch": float(epoch),
        "accuracy": accuracy,
        "speed": speed,
        "is_mocked": mocked,
    }


class TestCoordinateValidity(unittest.TestCase):
    def test_a_normal_cairo_point_is_valid(self) -> None:
        self.assertTrue(geo_track.is_valid_coordinate(30.044420, 31.235712))

    def test_null_island_is_not_a_position(self) -> None:
        """(0, 0) is what a handset reports with NO fix, and what an empty Float holds."""
        self.assertFalse(geo_track.is_valid_coordinate(0, 0))
        self.assertFalse(geo_track.is_valid_coordinate(0.0, 0.0))

    def test_out_of_range_is_rejected(self) -> None:
        self.assertFalse(geo_track.is_valid_coordinate(91.0, 31.0))
        self.assertFalse(geo_track.is_valid_coordinate(30.0, 181.0))

    def test_garbage_is_rejected_rather_than_raising(self) -> None:
        self.assertFalse(geo_track.is_valid_coordinate("north", "east"))
        self.assertFalse(geo_track.is_valid_coordinate(None, None))


class TestHaversine(unittest.TestCase):
    def test_one_degree_of_latitude(self) -> None:
        """R * pi/180 = 111,195 m. Pins the earth radius constant, not just the formula."""
        self.assertAlmostEqual(111195.08, geo_track.haversine_m(0, 0, 1, 0), delta=1.0)

    def test_a_short_hop_at_cairo_latitude(self) -> None:
        # 0.001 degrees of longitude at 30 N is ~96.5 m.
        self.assertAlmostEqual(
            96.5, geo_track.haversine_m(30.0, 31.0, 30.0, 31.001), delta=1.0
        )

    def test_identical_points_are_zero_apart(self) -> None:
        self.assertEqual(0.0, geo_track.haversine_m(30.0, 31.0, 30.0, 31.0))


class TestAccuracyIsUnknownAtZero(unittest.TestCase):
    """0 means "not reported", never "accurate to 0 m" — Float columns default to 0."""

    def test_zero_is_unknown(self) -> None:
        self.assertFalse(geo_track.accuracy_is_known(0))
        self.assertFalse(geo_track.accuracy_is_known(0.0))
        self.assertFalse(geo_track.accuracy_is_known(None))

    def test_a_real_measurement_is_known(self) -> None:
        self.assertTrue(geo_track.accuracy_is_known(12.5))


class TestNoiseFilter(unittest.TestCase):
    def test_drift_while_parked_adds_no_distance(self) -> None:
        """The whole reason the filter exists.

        A parked handset reports a bounded random walk — here ten fixes wobbling within
        about 10 m of one spot over ten minutes. Because the jitter rule is measured
        against the last KEPT fix rather than the last SEEN one, none of the wobble
        accumulates and the total is exactly zero.

        Measured against the last seen fix instead, each ~10 m step would pass and this
        lunch break would bill as ~100 m of travel. Over an eight-hour shift that error
        reaches kilometres, and it only ever inflates.
        """
        wobble = [0, 9, -4, 7, -9, 3, 8, -6, 2, -8]  # metres, north/south of one spot
        fixes = [
            fix(30.0 + metres * 0.000009, 31.0, 1000 + index * 60)
            for index, metres in enumerate(wobble)
        ]
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(1, len(result["kept"]), "only the origin should survive")
        self.assertEqual(len(wobble) - 1, result["dropped"]["jitter"])
        self.assertEqual(0.0, result["distance_m"])

    def test_a_sustained_creep_past_the_floor_is_travel_and_is_counted(self) -> None:
        """The filter is a floor, not a claim that slow movement is not movement.

        Pinned deliberately so nobody "fixes" the filter into swallowing a courier
        pushing a bike along a street. Steps of ~20 m clear the floor and count.
        """
        fixes = [fix(30.0 + 0.00019 * i, 31.0, 1000 + i * 60) for i in range(5)]
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(5, len(result["kept"]))
        self.assertGreater(result["distance_m"], 80.0)

    def test_a_real_move_is_kept(self) -> None:
        fixes = [fix(30.0, 31.0, 1000), fix(30.0045, 31.0, 1060)]  # ~500 m in 60 s
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(2, len(result["kept"]))
        self.assertAlmostEqual(500.0, result["distance_m"], delta=5.0)

    def test_a_bad_accuracy_fix_is_dropped_and_never_becomes_an_anchor(self) -> None:
        """A 300 m fix is a cell-tower guess, not a position.

        Critically it must be discarded *before* it can be used as the reference for the
        next comparison — a bogus anchor would make the following good fix look like a
        kilometre of travel.
        """
        fixes = [
            fix(30.0, 31.0, 1000),
            fix(30.02, 31.0, 1010, accuracy=300.0),
            fix(30.0045, 31.0, 1060),
        ]
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(1, result["dropped"]["accuracy"])
        self.assertEqual(2, len(result["kept"]))
        self.assertAlmostEqual(500.0, result["distance_m"], delta=5.0)

    def test_unknown_accuracy_is_not_a_reason_to_drop(self) -> None:
        """Older app builds never sent the field; 0 must not mean "reject"."""
        fixes = [fix(30.0, 31.0, 1000, accuracy=0), fix(30.0045, 31.0, 1060, accuracy=0)]
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(2, len(result["kept"]))
        self.assertEqual(0, result["dropped"]["accuracy"])

    def test_a_teleport_is_rejected_by_the_speed_rule_not_the_jitter_rule(self) -> None:
        """20 km in 10 seconds is 7,200 km/h. The jitter rule would have passed it."""
        fixes = [fix(30.0, 31.0, 1000), fix(30.18, 31.0, 1010), fix(30.0045, 31.0, 1060)]
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(1, result["dropped"]["speed"])
        self.assertEqual(0, result["dropped"]["jitter"])
        self.assertEqual(2, len(result["kept"]))

    def test_two_fixes_at_the_same_instant_in_different_places_are_rejected(self) -> None:
        """dt == 0 with distance > 0 is a replayed cached fix, not "no information"."""
        fixes = [fix(30.0, 31.0, 1000), fix(30.05, 31.0, 1000)]
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(1, len(result["kept"]))
        self.assertEqual(1, result["dropped"]["speed"])

    def test_a_mocked_fix_cannot_be_replayed_into_a_distance(self) -> None:
        """Ingest keeps mocked fixes out of the trail; this is the second line."""
        fixes = [fix(30.0, 31.0, 1000), fix(30.0045, 31.0, 1060, mocked=1)]
        result = geo_track.filter_fixes(fixes)

        self.assertEqual(1, len(result["kept"]))
        self.assertEqual(1, result["dropped"]["mocked"])
        self.assertEqual(0.0, result["distance_m"])

    def test_the_first_valid_fix_is_always_the_origin(self) -> None:
        result = geo_track.filter_fixes([fix(30.0, 31.0, 1000)])
        self.assertEqual(1, len(result["kept"]))
        self.assertEqual(0.0, result["distance_m"])

    def test_an_empty_track_is_not_an_error(self) -> None:
        result = geo_track.filter_fixes([])
        self.assertEqual([], result["kept"])
        self.assertEqual(0.0, result["distance_m"])

    def test_a_120_kmh_ceiling_lets_a_motorway_ride_through(self) -> None:
        """~90 km/h is plausible for a motorbike and must survive."""
        fixes = [fix(30.0, 31.0, 1000), fix(30.0, 31.026, 1100)]  # ~2.5 km in 100 s
        result = geo_track.filter_fixes(fixes)
        self.assertEqual(2, len(result["kept"]))


class TestSpeedBand(unittest.TestCase):
    def test_the_reported_speed_wins_over_a_two_point_average(self) -> None:
        """Android's Doppler figure is better evidence than two coordinates."""
        fixes = [fix(30.0, 31.0, 1000), fix(30.001, 31.0, 1010, speed=25.0)]  # 90 km/h
        events = geo_track.speeding_events(fixes)

        self.assertEqual(1, len(events))
        self.assertEqual("reported", events[0]["source"])
        self.assertAlmostEqual(90.0, events[0]["speed_kmh"], delta=0.1)

    def test_an_implausible_reading_is_not_reported_as_speeding(self) -> None:
        """Above the physical ceiling it is a bad fix, and a bad fix is not misconduct."""
        fixes = [fix(30.0, 31.0, 1000), fix(30.001, 31.0, 1010, speed=200.0)]  # 720 km/h
        self.assertEqual([], geo_track.speeding_events(fixes))

    def test_a_legal_speed_produces_nothing(self) -> None:
        fixes = [fix(30.0, 31.0, 1000), fix(30.001, 31.0, 1010, speed=10.0)]  # 36 km/h
        self.assertEqual([], geo_track.speeding_events(fixes))


class TestIdleSegments(unittest.TestCase):
    def test_a_long_stop_is_one_segment(self) -> None:
        fixes = [fix(30.0, 31.0, 1000 + i * 60) for i in range(31)]  # 30 minutes, still
        segments = geo_track.idle_segments(fixes, min_seconds=20 * 60, radius_m=60.0)

        self.assertEqual(1, len(segments))
        self.assertAlmostEqual(1800.0, segments[0]["seconds"], delta=1.0)
        self.assertEqual(31, segments[0]["fix_count"])

    def test_a_short_stop_is_not_reported(self) -> None:
        fixes = [fix(30.0, 31.0, 1000 + i * 60) for i in range(6)]  # 5 minutes
        self.assertEqual([], geo_track.idle_segments(fixes, min_seconds=20 * 60, radius_m=60.0))

    def test_a_slow_walk_is_not_one_long_stop(self) -> None:
        """Anchored on the first fix of the run, not a moving centroid.

        A moving centroid follows a slow drift down a street and reports the whole walk
        as stationary.
        """
        fixes = [fix(30.0 + 0.0005 * i, 31.0, 1000 + i * 60) for i in range(31)]
        segments = geo_track.idle_segments(fixes, min_seconds=20 * 60, radius_m=60.0)
        self.assertEqual([], segments)

    def test_riding_then_stopping_reports_only_the_stop(self) -> None:
        moving = [fix(30.0 + 0.005 * i, 31.0, 1000 + i * 60) for i in range(4)]
        parked_at = 30.0 + 0.005 * 4
        parked = [fix(parked_at, 31.0, 1300 + i * 60) for i in range(31)]
        segments = geo_track.idle_segments(moving + parked, min_seconds=20 * 60, radius_m=60.0)

        self.assertEqual(1, len(segments))
        self.assertAlmostEqual(parked_at, segments[0]["lat"], places=4)


class TestPingGaps(unittest.TestCase):
    def test_a_long_silence_is_a_gap(self) -> None:
        fixes = [fix(30.0, 31.0, 1000), fix(30.0045, 31.0, 1000 + 900)]  # 15 minutes
        gaps = geo_track.ping_gaps(fixes, max_gap_seconds=600)

        self.assertEqual(1, len(gaps))
        self.assertAlmostEqual(900.0, gaps[0]["seconds"], delta=1.0)

    def test_regular_pings_produce_no_gaps(self) -> None:
        fixes = [fix(30.0 + 0.005 * i, 31.0, 1000 + i * 30) for i in range(10)]
        self.assertEqual([], geo_track.ping_gaps(fixes, max_gap_seconds=600))


class TestClustering(unittest.TestCase):
    def test_nearby_fixes_form_one_cluster(self) -> None:
        points = [
            {"lat": 30.0, "lng": 31.0, "accuracy": 8.0},
            {"lat": 30.0002, "lng": 31.0, "accuracy": 10.0},  # ~22 m
            {"lat": 30.0003, "lng": 31.0, "accuracy": 6.0},  # ~33 m
        ]
        clusters = geo_track.cluster_points(points, radius_m=40.0)
        self.assertEqual(1, len(clusters))
        self.assertEqual(3, len(clusters[0]))

    def test_a_distant_fix_is_its_own_cluster(self) -> None:
        points = [
            {"lat": 30.0, "lng": 31.0, "accuracy": 8.0},
            {"lat": 30.0002, "lng": 31.0, "accuracy": 10.0},
            {"lat": 30.01, "lng": 31.0, "accuracy": 5.0},  # ~1.1 km away
        ]
        clusters = geo_track.cluster_points(points, radius_m=40.0)
        self.assertEqual(2, len(clusters))
        self.assertEqual(2, len(clusters[0]), "largest cluster sorts first")

    def test_the_tightest_fix_seeds_the_cluster(self) -> None:
        """Otherwise the centre depends on which row the database returned first."""
        points = [
            {"lat": 30.0010, "lng": 31.0, "accuracy": 45.0},
            {"lat": 30.0, "lng": 31.0, "accuracy": 4.0},
        ]
        clusters = geo_track.cluster_points(points, radius_m=40.0)
        self.assertEqual(4.0, clusters[0][0]["accuracy"])

    def test_a_fix_with_no_accuracy_sorts_last_as_a_seed(self) -> None:
        points = [
            {"lat": 30.0, "lng": 31.0, "accuracy": 0},
            {"lat": 30.05, "lng": 31.0, "accuracy": 9.0},
        ]
        clusters = geo_track.cluster_points(points, radius_m=40.0)
        self.assertEqual(9.0, clusters[0][0]["accuracy"])

    def test_null_island_never_joins_a_cluster(self) -> None:
        points = [{"lat": 30.0, "lng": 31.0, "accuracy": 8.0}, {"lat": 0, "lng": 0}]
        clusters = geo_track.cluster_points(points, radius_m=40.0)
        self.assertEqual(1, len(clusters))
        self.assertEqual(1, len(clusters[0]))

    def test_the_centroid_is_the_mean(self) -> None:
        points = [{"lat": 30.0, "lng": 31.0}, {"lat": 30.0002, "lng": 31.0002}]
        self.assertEqual((30.0001, 31.0001), geo_track.centroid(points))

    def test_the_radius_is_the_distance_to_the_furthest_member(self) -> None:
        points = [{"lat": 30.0, "lng": 31.0}, {"lat": 30.0002, "lng": 31.0}]  # ~22 m apart
        # Centroid sits halfway, so the radius is half the spread.
        self.assertAlmostEqual(11.1, geo_track.cluster_radius_m(points), delta=1.0)

    def test_an_empty_cluster_has_no_centroid(self) -> None:
        self.assertIsNone(geo_track.centroid([]))
        self.assertEqual(0.0, geo_track.cluster_radius_m([]))


class TestEncodedPolyline(unittest.TestCase):
    #: Google's own documented example for the algorithm.
    CANONICAL_POINTS = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    CANONICAL_ENCODED = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"

    def test_the_canonical_vector_encodes_exactly(self) -> None:
        self.assertEqual(
            self.CANONICAL_ENCODED, geo_track.encode_polyline(self.CANONICAL_POINTS)
        )

    def test_the_canonical_vector_decodes_exactly(self) -> None:
        self.assertEqual(
            self.CANONICAL_POINTS, geo_track.decode_polyline(self.CANONICAL_ENCODED)
        )

    def test_a_cairo_track_round_trips_at_stored_precision(self) -> None:
        points = [(30.044420, 31.235712), (30.045123, 31.236801), (30.046900, 31.238000)]
        decoded = geo_track.decode_polyline(geo_track.encode_polyline(points))
        for original, result in zip(points, decoded):
            self.assertAlmostEqual(original[0], result[0], places=5)
            self.assertAlmostEqual(original[1], result[1], places=5)

    def test_an_empty_track_encodes_to_an_empty_string(self) -> None:
        self.assertEqual("", geo_track.encode_polyline([]))
        self.assertEqual([], geo_track.decode_polyline(""))

    def test_invalid_points_are_skipped_not_encoded_as_null_island(self) -> None:
        encoded = geo_track.encode_polyline([(30.0, 31.0), (0, 0), (30.001, 31.0)])
        self.assertEqual(2, len(geo_track.decode_polyline(encoded)))

    def test_encode_track_reads_fix_dicts(self) -> None:
        encoded = geo_track.encode_track([fix(38.5, -120.2, 1), fix(40.7, -120.95, 2)])
        self.assertEqual(2, len(geo_track.decode_polyline(encoded)))


class TestChainDistance(unittest.TestCase):
    def test_a_chain_sums_its_legs(self) -> None:
        points = [(30.0, 31.0), (30.0045, 31.0), (30.009, 31.0)]  # two ~500 m legs
        self.assertAlmostEqual(1000.0, geo_track.chain_distance_m(points), delta=10.0)

    def test_an_unpinned_stop_shortens_the_chain_rather_than_teleporting_it(self) -> None:
        with_hole = geo_track.chain_distance_m([(30.0, 31.0), (None, None), (30.009, 31.0)])
        direct = geo_track.chain_distance_m([(30.0, 31.0), (30.009, 31.0)])
        self.assertEqual(direct, with_hole)

    def test_a_single_point_is_no_distance(self) -> None:
        self.assertEqual(0.0, geo_track.chain_distance_m([(30.0, 31.0)]))


class TestNearestDistance(unittest.TestCase):
    def test_it_finds_the_closest_of_several(self) -> None:
        distance = geo_track.nearest_distance_m(
            30.0, 31.0, [(30.05, 31.0), (30.0045, 31.0), (30.1, 31.0)]
        )
        self.assertAlmostEqual(500.0, distance, delta=5.0)

    def test_nothing_to_compare_returns_none(self) -> None:
        self.assertIsNone(geo_track.nearest_distance_m(30.0, 31.0, []))

    def test_an_invalid_origin_returns_none(self) -> None:
        self.assertIsNone(geo_track.nearest_distance_m(0, 0, [(30.0, 31.0)]))


class TestImpliedSpeed(unittest.TestCase):
    def test_a_zero_interval_move_is_infinite(self) -> None:
        speed = geo_track.implied_speed_kmh(fix(30.0, 31.0, 1000), fix(30.05, 31.0, 1000))
        self.assertTrue(math.isinf(speed))

    def test_a_zero_interval_non_move_is_zero(self) -> None:
        speed = geo_track.implied_speed_kmh(fix(30.0, 31.0, 1000), fix(30.0, 31.0, 1000))
        self.assertEqual(0.0, speed)

    def test_backwards_time_is_uncomputable(self) -> None:
        self.assertIsNone(
            geo_track.implied_speed_kmh(fix(30.0, 31.0, 1100), fix(30.001, 31.0, 1000))
        )


class TestModuleHasNoImports(unittest.TestCase):
    """geo_track must stay import-free — that is what makes it testable anywhere."""

    def test_it_does_not_import_frappe_or_jarz_pos(self) -> None:
        import ast
        from pathlib import Path

        source = Path(geo_track.__file__).with_suffix(".py").read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                roots.add(node.module.split(".")[0])

        self.assertEqual(
            set(),
            roots & {"frappe", "jarz_pos", "jarz_courier"},
            "geo_track must have no frappe, jarz_pos or intra-app imports — a pure "
            f"module is why these tests need no site. Found: {sorted(roots)}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

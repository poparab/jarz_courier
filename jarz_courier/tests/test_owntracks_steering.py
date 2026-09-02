"""``services/owntracks_steering`` — the commands we put in OwnTracks' response.

This is the only lever we hold over an iPhone: we do not build its tracker, we
answer its POSTs. Every rule here trades coverage against a courier's battery, so
the tests pin the *conditions* under which each command is sent at least as
carefully as the command's shape.
"""

from __future__ import annotations

import unittest

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.constants import OWNTRACKS  # noqa: E402
from jarz_courier.services import owntracks_steering as steering  # noqa: E402


def stop(invoice, lat=30.0444, lon=31.2357, display_id=None, address=True):
    payload = {"invoice": invoice, "display_id": display_id or invoice}
    if address:
        payload["address"] = {"latitude": lat, "longitude": lon}
    return payload


PINNED = [stop("INV-1", 30.0444, 31.2357, "16834"), stop("INV-2", 30.0501, 31.2400, "16835")]


class TestDesiredMode(unittest.TestCase):
    def test_orders_out_means_move(self) -> None:
        self.assertEqual(OWNTRACKS.MODE_MOVE, steering.desired_mode(has_open_stops=True))

    def test_nothing_to_deliver_means_significant_not_quiet(self) -> None:
        """Significant, so a courier who picks up orders later still reports the
        ~500 m moves that get them back into a steering window."""
        self.assertEqual(OWNTRACKS.MODE_SIGNIFICANT, steering.desired_mode(has_open_stops=False))


class TestSetConfiguration(unittest.TestCase):
    def test_move_carries_the_cadence(self) -> None:
        cmd = steering.set_configuration_command(OWNTRACKS.MODE_MOVE)

        self.assertEqual("cmd", cmd["_type"])
        self.assertEqual("setConfiguration", cmd["action"])
        cfg = cmd["configuration"]
        self.assertEqual("configuration", cfg["_type"])
        self.assertEqual(OWNTRACKS.MODE_MOVE, cfg["monitoring"])
        self.assertEqual(OWNTRACKS.MOVE_INTERVAL_SEC, cfg["locatorInterval"])
        self.assertEqual(OWNTRACKS.MOVE_DISPLACEMENT_M, cfg["locatorDisplacement"])

    def test_significant_changes_only_the_mode(self) -> None:
        cfg = steering.set_configuration_command(OWNTRACKS.MODE_SIGNIFICANT)["configuration"]
        self.assertEqual({"_type": "configuration", "monitoring": OWNTRACKS.MODE_SIGNIFICANT}, cfg)


class TestWaypoints(unittest.TestCase):
    def test_only_pinned_stops_become_geofences(self) -> None:
        """A territory centroid as a region would fire 'arrived' for a district."""
        stops = PINNED + [stop("INV-NOPIN", address=False), stop("INV-NULL", 0.0, 0.0)]

        waypoints = steering.build_waypoints(stops)

        self.assertEqual({"16834", "16835"}, {w["desc"] for w in waypoints})

    def test_shape_and_radius(self) -> None:
        w = steering.build_waypoints([PINNED[0]])[0]

        self.assertEqual("waypoint", w["_type"])
        self.assertEqual(OWNTRACKS.WAYPOINT_RADIUS_M, w["rad"])
        self.assertAlmostEqual(30.0444, w["lat"])
        self.assertAlmostEqual(31.2357, w["lon"])
        self.assertEqual(steering.waypoint_tst("INV-1"), w["tst"])

    def test_the_key_is_stable_and_positive(self) -> None:
        """OwnTracks merges on `tst`; a changing key duplicates the geofence."""
        self.assertEqual(steering.waypoint_tst("INV-1"), steering.waypoint_tst("INV-1"))
        self.assertNotEqual(steering.waypoint_tst("INV-1"), steering.waypoint_tst("INV-2"))
        self.assertGreater(steering.waypoint_tst("INV-1"), 0)

    def test_out_of_range_coordinates_are_dropped(self) -> None:
        self.assertEqual([], steering.build_waypoints([stop("INV-X", 95.0, 31.0)]))

    def test_the_set_waypoints_command_wraps_the_list(self) -> None:
        cmd = steering.set_waypoints_command(steering.build_waypoints(PINNED))

        self.assertEqual("setWaypoints", cmd["action"])
        self.assertEqual("waypoints", cmd["waypoints"]["_type"])
        self.assertEqual(2, len(cmd["waypoints"]["waypoints"]))


class TestFingerprint(unittest.TestCase):
    def test_order_independent(self) -> None:
        a = steering.build_waypoints(PINNED)
        b = steering.build_waypoints(list(reversed(PINNED)))
        self.assertEqual(steering.waypoints_fingerprint(a), steering.waypoints_fingerprint(b))

    def test_changes_when_a_stop_changes(self) -> None:
        before = steering.waypoints_fingerprint(steering.build_waypoints(PINNED))
        after = steering.waypoints_fingerprint(steering.build_waypoints(PINNED[:1]))
        self.assertNotEqual(before, after)

    def test_empty_has_a_fingerprint_too(self) -> None:
        self.assertTrue(steering.waypoints_fingerprint([]))


class TestPlanCommands(unittest.TestCase):
    """The conditions matter more than the shapes."""

    def plan(self, **overrides):
        base = dict(
            message_type="location",
            trigger="t",
            device_mode=OWNTRACKS.MODE_MOVE,
            has_open_stops=True,
            steer_window=True,
            waypoints=steering.build_waypoints(PINNED),
            pushed_fingerprint=None,
        )
        base.update(overrides)
        return steering.plan_commands(**base)

    def actions(self, plan):
        return [c["action"] for c in plan["commands"]]

    def test_outside_the_window_nothing_is_steered(self) -> None:
        plan = self.plan(steer_window=False, device_mode=OWNTRACKS.MODE_SIGNIFICANT)
        self.assertEqual([], self.actions(plan))
        self.assertIsNone(plan["fingerprint"])

    def test_a_device_already_in_the_wanted_mode_is_left_alone(self) -> None:
        plan = self.plan(pushed_fingerprint=steering.waypoints_fingerprint(steering.build_waypoints(PINNED)))
        self.assertNotIn("setConfiguration", self.actions(plan))

    def test_a_device_in_the_wrong_mode_is_switched(self) -> None:
        plan = self.plan(device_mode=OWNTRACKS.MODE_SIGNIFICANT)
        self.assertIn("setConfiguration", self.actions(plan))
        cfg = next(c for c in plan["commands"] if c["action"] == "setConfiguration")
        self.assertEqual(OWNTRACKS.MODE_MOVE, cfg["configuration"]["monitoring"])

    def test_no_orders_out_drops_the_device_to_significant(self) -> None:
        plan = self.plan(has_open_stops=False, waypoints=[], device_mode=OWNTRACKS.MODE_MOVE)
        cfg = next(c for c in plan["commands"] if c["action"] == "setConfiguration")
        self.assertEqual(OWNTRACKS.MODE_SIGNIFICANT, cfg["configuration"]["monitoring"])

    def test_an_unknown_device_mode_is_set_anyway(self) -> None:
        """Android OwnTracks omits `m`; the command is idempotent on the device."""
        plan = self.plan(device_mode=None)
        self.assertIn("setConfiguration", self.actions(plan))

    def test_waypoints_are_pushed_when_the_set_changed(self) -> None:
        plan = self.plan(pushed_fingerprint="something-else")
        self.assertIn("setWaypoints", self.actions(plan))
        self.assertEqual(
            steering.waypoints_fingerprint(steering.build_waypoints(PINNED)), plan["fingerprint"]
        )

    def test_waypoints_are_not_re_pushed_when_unchanged(self) -> None:
        current = steering.waypoints_fingerprint(steering.build_waypoints(PINNED))
        plan = self.plan(pushed_fingerprint=current)
        self.assertNotIn("setWaypoints", self.actions(plan))
        self.assertIsNone(plan["fingerprint"])

    def test_an_empty_set_is_pushed_when_the_device_still_holds_old_ones(self) -> None:
        """Finished stops must stop being geofences."""
        plan = self.plan(waypoints=[], has_open_stops=False, pushed_fingerprint="had-some")
        self.assertIn("setWaypoints", self.actions(plan))

    def test_an_empty_set_is_not_pushed_when_nothing_was_ever_pushed(self) -> None:
        plan = self.plan(waypoints=[], has_open_stops=False, device_mode=OWNTRACKS.MODE_SIGNIFICANT)
        self.assertEqual([], self.actions(plan))

    def test_a_positionless_message_with_orders_out_gets_a_nudge(self) -> None:
        plan = self.plan(message_type="lwt", steer_window=False)
        self.assertEqual(["reportLocation"], self.actions(plan))

    def test_a_fix_we_requested_is_never_answered_with_another_request(self) -> None:
        """Otherwise the two sides ping-pong forever."""
        plan = self.plan(message_type="lwt", trigger="r", steer_window=False)
        self.assertEqual([], self.actions(plan))

    def test_no_nudge_without_orders_out(self) -> None:
        plan = self.plan(message_type="lwt", has_open_stops=False, steer_window=False)
        self.assertEqual([], self.actions(plan))

    def test_a_position_report_is_not_nudged(self) -> None:
        """They just told us where they are."""
        plan = self.plan(message_type="location", steer_window=False)
        self.assertNotIn("reportLocation", self.actions(plan))

    def test_a_transition_counts_as_a_position(self) -> None:
        plan = self.plan(message_type="transition", steer_window=False)
        self.assertNotIn("reportLocation", self.actions(plan))


class TestDeviceConfiguration(unittest.TestCase):
    def config(self, **overrides):
        base = dict(
            ingest_url="https://erpstg.orderjarz.com/api/method/jarz_courier.api.tracking.ingest_owntracks",
            api_key="key123",
            api_secret="secret456",
            party="HR-EMP-000007",
            display_name="Mahmoud Ali",
        )
        base.update(overrides)
        return steering.device_configuration(**base)

    def test_it_is_http_mode_with_basic_auth(self) -> None:
        cfg = self.config()
        self.assertEqual("configuration", cfg["_type"])
        self.assertEqual(3, cfg["mode"])
        self.assertTrue(cfg["auth"])
        self.assertEqual("key123", cfg["username"])
        self.assertEqual("secret456", cfg["password"])
        self.assertTrue(cfg["url"].endswith("ingest_owntracks"))

    def test_every_remote_switch_the_server_relies_on_is_enabled(self) -> None:
        """Miss one and the device silently ignores every command we send."""
        cfg = self.config()
        for key in ("cmd", "remoteConfiguration", "allowRemoteLocation"):
            with self.subTest(key=key):
                self.assertIs(True, cfg[key])

    def test_it_starts_in_move_mode_with_our_cadence(self) -> None:
        cfg = self.config()
        self.assertEqual(OWNTRACKS.MODE_MOVE, cfg["monitoring"])
        self.assertEqual(OWNTRACKS.MOVE_INTERVAL_SEC, cfg["locatorInterval"])
        self.assertEqual(OWNTRACKS.MOVE_DISPLACEMENT_M, cfg["locatorDisplacement"])

    def test_inaccurate_fixes_are_suppressed_on_the_device(self) -> None:
        self.assertEqual(OWNTRACKS.IGNORE_INACCURATE_M, self.config()["ignoreInaccurateLocations"])

    def test_tracker_id_is_two_characters(self) -> None:
        self.assertEqual("MA", self.config()["tid"])
        self.assertEqual("07", self.config(display_name="")["tid"])
        self.assertEqual(2, len(self.config(display_name="Solo")["tid"]))

    def test_the_device_id_is_the_employee(self) -> None:
        self.assertEqual("HR-EMP-000007", self.config()["deviceId"])


if __name__ == "__main__":
    unittest.main()

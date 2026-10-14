"""``services/tamper_watch`` - a courier who switches tracking off while carrying orders.

Two detectors and the wiring that feeds them:

* **mode change** - OwnTracks reports Manual (0) or Quiet (-1), which the server never
  sets. Only a finding while orders are out, and only on the *edge* (the mode differs
  from the last one seen), so a phone that stays in Manual is one finding, not one per
  message.
* **tracking lost** - orders out and no ping for four minutes, on the scheduler sweep.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.api import tracking as api  # noqa: E402
from jarz_courier.services import assignment_watch, tamper_watch  # noqa: E402

IDENTITY = dict(_support.COURIER_IDENTITY)
NOW = 1_800_000_000.0
STOP = {"invoice": "ACC-SINV-2026-00042", "state": "Out for Delivery"}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.flag = MagicMock(return_value={"created": True})
        self.stops = [STOP]
        self.previous_mode = 2
        self.position = {"lat": 30.04, "lng": 31.23, "epoch": NOW - 30}
        for p in (
            patch.object(tamper_watch.anomaly, "flag", self.flag),
            patch.object(
                tamper_watch.run_sheet, "get_run", side_effect=lambda **kw: {"stops": list(self.stops)}
            ),
            patch.object(
                tamper_watch.location_cache, "read_owntracks_mode", side_effect=lambda *a, **k: self.previous_mode
            ),
            patch.object(
                tamper_watch.location_cache, "read_position", side_effect=lambda *a, **k: self.position
            ),
        ):
            p.start()
            self.addCleanup(p.stop)

    def flagged(self):
        return self.flag.call_args.kwargs


class TestModeChange(_Case):
    def test_dropping_to_manual_with_orders_out_is_a_high_finding(self) -> None:
        tamper_watch.check_mode_change(IDENTITY, 0)
        self.assertEqual("Tracker Mode Changed", self.flagged()["anomaly_type"])
        self.assertEqual("High", self.flagged()["severity"])
        self.assertEqual("ACC-SINV-2026-00042", self.flagged()["sales_invoice"])
        self.assertIn("Manual", self.flagged()["reason"])

    def test_quiet_mode_is_named_as_quiet(self) -> None:
        tamper_watch.check_mode_change(IDENTITY, "-1")
        self.assertIn("Quiet", self.flagged()["reason"])

    def test_the_two_modes_the_server_sets_are_never_a_finding(self) -> None:
        for mode in (1, 2):
            self.assertIsNone(tamper_watch.check_mode_change(IDENTITY, mode))
        self.flag.assert_not_called()

    def test_a_mode_that_did_not_change_is_not_a_new_finding(self) -> None:
        self.previous_mode = 0
        self.assertIsNone(tamper_watch.check_mode_change(IDENTITY, 0))
        self.flag.assert_not_called()

    def test_an_idle_courier_may_stop_tracking(self) -> None:
        self.stops = []
        self.assertIsNone(tamper_watch.check_mode_change(IDENTITY, 0))
        self.flag.assert_not_called()

    def test_garbage_modes_are_ignored(self) -> None:
        for mode in (None, "", "fast", object()):
            self.assertIsNone(tamper_watch.check_mode_change(IDENTITY, mode))
        self.flag.assert_not_called()

    def test_the_dedupe_key_names_the_courier_and_the_mode(self) -> None:
        tamper_watch.check_mode_change(IDENTITY, 0)
        key = self.flagged()["dedupe_key"]
        self.assertTrue(key.startswith("tracker-mode:Employee:HR-EMP-00042:0:"))

    def test_it_never_raises(self) -> None:
        self.flag.side_effect = RuntimeError("db down")
        self.assertIsNone(tamper_watch.check_mode_change(IDENTITY, 0))


class TestTrackingLost(_Case):
    COURIERS = [("Employee", "HR-EMP-00042", "Dokki")]

    def test_silence_past_the_limit_with_orders_out_is_a_finding(self) -> None:
        self.position = {"lat": 30.04, "lng": 31.23, "epoch": NOW - 600}
        summary = tamper_watch.check_tracking_lost(self.COURIERS, now_epoch=NOW)
        self.assertEqual({"checked": 1, "lost": 1}, summary)
        self.assertEqual("Tracking Lost", self.flagged()["anomaly_type"])
        self.assertEqual("High", self.flagged()["severity"])
        self.assertEqual(10.0, self.flagged()["observed"])
        self.assertEqual("minutes", self.flagged()["unit"])
        self.assertEqual(30.04, self.flagged()["latitude"])

    def test_a_recent_ping_is_fine(self) -> None:
        summary = tamper_watch.check_tracking_lost(self.COURIERS, now_epoch=NOW)
        self.assertEqual({"checked": 1, "lost": 0}, summary)
        self.flag.assert_not_called()

    def test_the_boundary_is_inclusive_of_the_limit(self) -> None:
        self.position = {"lat": 1, "lng": 1, "epoch": NOW - tamper_watch.LOST_AFTER_SEC}
        self.assertEqual(1, tamper_watch.check_tracking_lost(self.COURIERS, now_epoch=NOW)["lost"])

    def test_no_cached_position_at_all_is_also_lost(self) -> None:
        self.position = None
        summary = tamper_watch.check_tracking_lost(self.COURIERS, now_epoch=NOW)
        self.assertEqual(1, summary["lost"])
        self.assertIsNone(self.flagged()["observed"])

    def test_a_courier_with_no_orders_out_is_never_flagged(self) -> None:
        self.stops = []
        self.position = None
        self.assertEqual(0, tamper_watch.check_tracking_lost(self.COURIERS, now_epoch=NOW)["lost"])
        self.flag.assert_not_called()

    def test_an_already_filed_finding_is_not_counted_again(self) -> None:
        self.flag.return_value = {"created": False}
        self.position = None
        self.assertEqual(0, tamper_watch.check_tracking_lost(self.COURIERS, now_epoch=NOW)["lost"])

    def test_one_bad_courier_does_not_stop_the_rest(self) -> None:
        self.position = None
        self.flag.side_effect = [RuntimeError("boom"), {"created": True}]
        couriers = self.COURIERS + [("Employee", "HR-EMP-00043", "Dokki")]
        summary = tamper_watch.check_tracking_lost(couriers, now_epoch=NOW)
        self.assertEqual({"checked": 2, "lost": 1}, summary)

    def test_the_couriers_are_looked_up_when_not_given(self) -> None:
        self.position = None
        with patch.object(assignment_watch, "active_couriers", return_value=self.COURIERS) as lookup:
            summary = tamper_watch.check_tracking_lost(now_epoch=NOW)
        lookup.assert_called_once()
        self.assertEqual(1, summary["lost"])

    def test_a_failed_lookup_returns_an_empty_summary(self) -> None:
        with patch.object(assignment_watch, "active_couriers", side_effect=RuntimeError("down")):
            self.assertEqual({"checked": 0, "lost": 0}, tamper_watch.check_tracking_lost())


class TestWiring(unittest.TestCase):
    def test_the_sweep_runs_the_lost_check(self) -> None:
        with patch.object(assignment_watch, "sweep", return_value={"flagged": 0}), patch.object(
            assignment_watch.tamper_watch, "check_tracking_lost"
        ) as check:
            result = assignment_watch.scheduled_sweep()
        check.assert_called_once_with()
        self.assertEqual({"flagged": 0}, result)

    def test_the_lost_check_still_runs_when_the_sweep_fails(self) -> None:
        with patch.object(assignment_watch, "sweep", side_effect=RuntimeError("boom")), patch.object(
            assignment_watch.tamper_watch, "check_tracking_lost"
        ) as check:
            result = assignment_watch.scheduled_sweep()
        check.assert_called_once()
        self.assertEqual({"error": True}, result)

    def test_a_failing_lost_check_does_not_change_the_sweep_result(self) -> None:
        with patch.object(assignment_watch, "sweep", return_value={"flagged": 2}), patch.object(
            assignment_watch.tamper_watch, "check_tracking_lost", side_effect=RuntimeError("boom")
        ):
            self.assertEqual({"flagged": 2}, assignment_watch.scheduled_sweep())

    def test_owntracks_checks_the_mode_before_steering_overwrites_it(self) -> None:
        order = []
        tracking = MagicMock()
        tracking.owntracks_meta.return_value = {"mode": 0, "type": "location"}
        tracking.owntracks_to_fix.return_value = None
        with patch.object(api, "tracking", tracking), patch.object(
            api.courier_onboarding, "ensure_courier_setup", return_value=IDENTITY
        ), patch.object(
            api.tamper_watch, "check_mode_change", side_effect=lambda *a, **k: order.append("check")
        ) as check, patch.object(
            api, "_steer_device", side_effect=lambda *a, **k: order.append("steer") or []
        ):
            api._ingest_owntracks({"_type": "location"})
        self.assertEqual(["check", "steer"], order)
        self.assertEqual(0, check.call_args.args[1])

    def test_a_failing_mode_check_is_swallowed_by_the_detector_itself(self) -> None:
        with patch.object(tamper_watch.location_cache, "read_owntracks_mode", side_effect=RuntimeError("x")):
            self.assertIsNone(tamper_watch.check_mode_change(IDENTITY, 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``services/anomaly`` and the ``Courier Anomaly`` doctype — flags, never charges.

The first test class here is the important one, and it reads the DocType JSON rather
than any Python: **the doctype must declare no monetary field.** That is the
machine-checkable form of B9's "do not compute money penalties", and it is a structural
guarantee rather than a convention — there is nowhere on the record for an amount to be
parked "just for reference".

Why it is worth a test. Every finding here is derived from consumer GPS, and consumer
GPS is confidently wrong on a regular basis: a 300 m fix in a covered market, a tunnel
that eats four minutes of pings, an OEM battery manager that sleeps the app. Each
produces a finding indistinguishable from misconduct. While a finding is a prompt to ask
a question, being wrong costs a conversation. The moment a number on it could reach a
payslip, "ask the courier" becomes "deduct unless they appeal" — and the appeal is
against a black box.

The rest of the module tests the severity bands, of which two are load-bearing:

* the speeding band is closed at the top by the noise filter's rejection ceiling, so a
  tower hand-off cannot be reported as reckless riding;
* the detour ratio's baseline is not 1.0, because the denominator is a straight line and
  a real road network costs 1.2-1.4x crow-flight.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.constants import ANOMALY_TYPE, SEVERITY  # noqa: E402
from jarz_courier.services import anomaly, geo_track  # noqa: E402

DOCTYPE_JSON = (
    Path(anomaly.__file__).resolve().parents[1]
    / "doctype"
    / "courier_anomaly"
    / "courier_anomaly.json"
)


class TestTheDoctypeCannotHoldMoney(unittest.TestCase):
    """B9: flag only. Enforced by the absence of a field, not by review."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(DOCTYPE_JSON.read_text(encoding="utf-8"))
        cls.fields = cls.schema["fields"]

    def test_no_currency_field_exists(self) -> None:
        offenders = [f["fieldname"] for f in self.fields if f["fieldtype"] == "Currency"]
        self.assertEqual(
            [],
            offenders,
            "Courier Anomaly must declare no Currency field. A detector that can "
            "compute a deduction is a detector nobody can appeal against. If a penalty "
            "policy is ever needed it belongs on its own doctype, owned by whoever owns "
            f"the policy. Found: {offenders}",
        )

    def test_no_field_is_named_like_a_charge(self) -> None:
        """Catches a Float smuggling in what a Currency was refused for."""
        banned = ("penalty", "deduction", "fine", "amount", "charge", "cost", "fee")
        offenders = [
            f["fieldname"]
            for f in self.fields
            if any(word in f["fieldname"].lower() for word in banned)
        ]
        self.assertEqual([], offenders, f"these read as money: {offenders}")

    def test_a_reason_is_mandatory(self) -> None:
        """A row saying "Detour / High" gives a manager nothing to ask about."""
        reason = next(f for f in self.fields if f["fieldname"] == "reason")
        self.assertEqual(1, reason.get("reqd"))

    def test_the_dedupe_key_is_unique(self) -> None:
        """The detectors re-scan overlapping windows; the index is the real guarantee."""
        key = next(f for f in self.fields if f["fieldname"] == "dedupe_key")
        self.assertEqual(1, key.get("unique"))

    def test_every_constant_type_is_a_select_option(self) -> None:
        options = next(f for f in self.fields if f["fieldname"] == "anomaly_type")["options"]
        available = set(options.split("\n"))
        for value in ANOMALY_TYPE.ALL:
            self.assertIn(value, available, f"{value} is not selectable on the doctype")

    def test_every_severity_is_a_select_option(self) -> None:
        options = next(f for f in self.fields if f["fieldname"] == "severity")["options"]
        available = set(options.split("\n"))
        for value in SEVERITY.ALL:
            self.assertIn(value, available)


class TestDetourRatio(unittest.TestCase):
    def test_a_normal_road_network_does_not_trip_the_threshold(self) -> None:
        """~1.3x is what a street grid costs against a straight line, not a detour."""
        ratio = anomaly.detour_ratio(5200, 4000)
        self.assertEqual(1.3, ratio)
        self.assertIsNone(anomaly.detour_severity(ratio))

    def test_a_clear_detour_is_medium(self) -> None:
        self.assertEqual(SEVERITY.MEDIUM, anomaly.detour_severity(anomaly.detour_ratio(8000, 4000)))

    def test_an_extreme_detour_is_high(self) -> None:
        self.assertEqual(SEVERITY.HIGH, anomaly.detour_severity(anomaly.detour_ratio(12000, 4000)))

    def test_a_short_run_is_not_judged(self) -> None:
        """Below the minimum the ratio is dominated by GPS error and by where fix one landed."""
        self.assertIsNone(anomaly.detour_ratio(5000, 500))

    def test_a_missing_plan_yields_none_not_infinity(self) -> None:
        """An "infinite" ratio would sort to the top of every report and mean nothing."""
        self.assertIsNone(anomaly.detour_ratio(5000, 0))
        self.assertIsNone(anomaly.detour_ratio(5000, None))

    def test_a_run_that_did_not_move_yields_none(self) -> None:
        self.assertIsNone(anomaly.detour_ratio(0, 4000))

    def test_garbage_does_not_raise(self) -> None:
        self.assertIsNone(anomaly.detour_ratio("far", "near"))


class TestSpeedingBand(unittest.TestCase):
    def test_a_legal_speed_is_not_a_finding(self) -> None:
        self.assertIsNone(anomaly.speeding_severity(70.0))

    def test_the_band_opens_just_above_the_limit(self) -> None:
        self.assertEqual(SEVERITY.LOW, anomaly.speeding_severity(85.0))
        self.assertEqual(SEVERITY.MEDIUM, anomaly.speeding_severity(100.0))
        self.assertEqual(SEVERITY.HIGH, anomaly.speeding_severity(115.0))

    def test_the_band_is_closed_at_the_filter_ceiling(self) -> None:
        """Above 120 km/h a motorbike did not do it — that is a bad fix, not conduct.

        Grading it would put tower hand-offs into a conduct report, which is how a
        report becomes something nobody opens.
        """
        self.assertIsNone(anomaly.speeding_severity(geo_track.MAX_SPEED_KMH + 1))
        self.assertIsNone(anomaly.speeding_severity(7200.0))

    def test_the_two_thresholds_are_deliberately_different_numbers(self) -> None:
        self.assertLess(geo_track.SPEEDING_LIMIT_KMH, geo_track.MAX_SPEED_KMH)


class TestIdleAndGapBands(unittest.TestCase):
    def test_a_short_stop_is_not_a_finding(self) -> None:
        self.assertIsNone(anomaly.idle_severity(5 * 60))

    def test_twenty_minutes_is_medium_and_forty_five_is_high(self) -> None:
        self.assertEqual(SEVERITY.MEDIUM, anomaly.idle_severity(20 * 60))
        self.assertEqual(SEVERITY.HIGH, anomaly.idle_severity(50 * 60))

    def test_gap_severity_scales_with_the_longest_hole(self) -> None:
        self.assertIsNone(anomaly.ping_gap_severity(5 * 60))
        self.assertEqual(SEVERITY.LOW, anomaly.ping_gap_severity(12 * 60))
        self.assertEqual(SEVERITY.MEDIUM, anomaly.ping_gap_severity(25 * 60))
        self.assertEqual(SEVERITY.HIGH, anomaly.ping_gap_severity(60 * 60))


class TestFarFromPinAllowance(unittest.TestCase):
    """A pin that is only good to 80 m cannot convict anybody of standing 100 m away."""

    def test_an_unknown_accuracy_gets_the_bare_allowance(self) -> None:
        """0 on the column means "not reported", so it must widen nothing.

        Read as "accurate to 0 m" it would convict every courier whose customer pin
        came from a maps link — which never carries an accuracy at all.
        """
        with patch.object(anomaly.pos_bridge, "accuracy_is_known", return_value=False):
            self.assertEqual(anomaly.FAR_FROM_PIN_M, anomaly.pin_distance_allowance(0))

    def test_a_known_accuracy_widens_the_allowance(self) -> None:
        with patch.object(anomaly.pos_bridge, "accuracy_is_known", return_value=True):
            self.assertEqual(anomaly.FAR_FROM_PIN_M + 80.0, anomaly.pin_distance_allowance(80.0))

    def test_the_question_is_asked_of_jarz_pos_not_of_the_raw_number(self) -> None:
        """Contract §3 requires the shared helper, not a local `> 0`."""
        with patch.object(anomaly.pos_bridge, "accuracy_is_known", return_value=False) as known:
            anomaly.pin_distance_allowance(0)
        known.assert_called_once_with(0)

    def test_inside_the_allowance_is_not_a_finding(self) -> None:
        self.assertIsNone(anomaly.far_from_pin_severity(100.0, 150.0))

    def test_just_outside_is_medium(self) -> None:
        self.assertEqual(SEVERITY.MEDIUM, anomaly.far_from_pin_severity(200.0, 150.0))

    def test_far_outside_is_high(self) -> None:
        self.assertEqual(SEVERITY.HIGH, anomaly.far_from_pin_severity(900.0, 150.0))


class TestFlag(unittest.TestCase):
    def test_it_files_one_finding(self) -> None:
        doc = MagicMock()
        doc.name = "CANM-00001"
        with patch.object(anomaly.frappe.db, "exists", return_value=None), patch.object(
            anomaly.frappe, "new_doc", return_value=doc
        ):
            result = anomaly.flag(
                anomaly_type=ANOMALY_TYPE.DETOUR,
                severity=SEVERITY.MEDIUM,
                reason="Rode 8 km against a 4 km plan",
                party_type="Employee",
                party="HR-EMP-1",
                dedupe_key="detour::CRUN-1",
            )

        self.assertTrue(result["created"])
        doc.insert.assert_called_once()

    def test_a_repeated_pass_files_nothing(self) -> None:
        """Scheduled detectors re-scan overlapping windows on purpose."""
        with patch.object(anomaly.frappe.db, "exists", return_value="CANM-00001"), patch.object(
            anomaly.frappe, "new_doc"
        ) as new_doc:
            result = anomaly.flag(
                anomaly_type=ANOMALY_TYPE.DETOUR,
                severity=SEVERITY.MEDIUM,
                reason="whatever",
                party_type="Employee",
                party="HR-EMP-1",
                dedupe_key="detour::CRUN-1",
            )

        new_doc.assert_not_called()
        self.assertFalse(result["created"])
        self.assertEqual("CANM-00001", result["anomaly"])

    def test_a_race_between_two_workers_is_absorbed_by_the_unique_index(self) -> None:
        """The exists() check loses to a concurrent pass; the index is the guarantee."""
        with patch.object(anomaly.frappe.db, "exists", return_value=None), patch.object(
            anomaly.frappe, "new_doc", side_effect=anomaly.frappe.DuplicateEntryError("dupe")
        ):
            result = anomaly.flag(
                anomaly_type=ANOMALY_TYPE.DETOUR,
                severity=SEVERITY.MEDIUM,
                reason="whatever",
                party_type="Employee",
                party="HR-EMP-1",
                dedupe_key="detour::CRUN-1",
            )

        self.assertFalse(result["created"])

    def test_a_finding_with_no_dedupe_key_is_refused(self) -> None:
        """Without one, every scheduled pass would file the same finding again."""
        with patch.object(anomaly.frappe, "new_doc") as new_doc:
            result = anomaly.flag(
                anomaly_type=ANOMALY_TYPE.DETOUR,
                severity=SEVERITY.MEDIUM,
                reason="whatever",
                party_type="Employee",
                party="HR-EMP-1",
                dedupe_key="",
            )

        new_doc.assert_not_called()
        self.assertFalse(result["created"])

    def test_a_database_failure_never_propagates(self) -> None:
        with patch.object(anomaly.frappe.db, "exists", side_effect=RuntimeError("boom")):
            result = anomaly.flag(
                anomaly_type=ANOMALY_TYPE.DETOUR,
                severity=SEVERITY.MEDIUM,
                reason="whatever",
                party_type="Employee",
                party="HR-EMP-1",
                dedupe_key="detour::CRUN-1",
            )
        self.assertFalse(result["created"])


def run_row(**extra):
    row = {
        "name": "CRUN-00001",
        "party_type": "Employee",
        "party": "HR-EMP-00042",
        "branch": "Dokki",
        "total_distance_m": 5200.0,
        "planned_distance_m": 4000.0,
        "stops_delivered": 3,
        "mock_ping_count": 0,
        "started_on": "2026-08-08 08:00:00",
        "ended_on": "2026-08-08 16:00:00",
    }
    row.update(extra)
    return row


class TestAnalyseRun(unittest.TestCase):
    def setUp(self) -> None:
        self.flag = patch.object(anomaly, "flag", return_value={"created": True})
        self.flag.start()
        self.addCleanup(self.flag.stop)
        stops = patch.object(anomaly, "_delivered_stop_points", return_value=[(30.0045, 31.0)])
        stops.start()
        self.addCleanup(stops.stop)

    def test_a_clean_run_produces_nothing(self) -> None:
        result = anomaly.analyse_run(run_row(), fixes=[])
        self.assertTrue(result["ok"])
        self.assertEqual([], result["findings"])

    def test_a_detour_is_found_from_the_stored_numbers(self) -> None:
        """Read off the run, not recomputed — the flag and the report must agree."""
        result = anomaly.analyse_run(run_row(total_distance_m=12000.0), fixes=[])

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(result["findings"]))
        self.assertEqual(ANOMALY_TYPE.DETOUR, anomaly.flag.call_args.kwargs["anomaly_type"])

    def test_mock_pings_recorded_at_ingest_still_surface_at_close(self) -> None:
        anomaly.analyse_run(run_row(mock_ping_count=4), fixes=[])
        types = [call.kwargs["anomaly_type"] for call in anomaly.flag.call_args_list]
        self.assertIn(ANOMALY_TYPE.MOCK_GPS, types)

    def test_idle_is_measured_on_the_raw_track(self) -> None:
        """The jitter filter deletes exactly the fixes that prove somebody stood still."""
        parked = [
            {"lat": 30.0045, "lng": 31.0, "epoch": 1000 + i * 60, "accuracy": 5.0, "is_mocked": 0}
            for i in range(31)
        ]
        result = anomaly.analyse_run(run_row(), fixes=parked)

        types = [call.kwargs["anomaly_type"] for call in anomaly.flag.call_args_list]
        self.assertIn(ANOMALY_TYPE.IDLE, types)
        self.assertTrue(result["ok"])

    def test_a_long_stop_away_from_every_customer_is_an_unexpected_stop(self) -> None:
        parked = [
            {"lat": 30.05, "lng": 31.0, "epoch": 1000 + i * 60, "accuracy": 5.0, "is_mocked": 0}
            for i in range(31)
        ]
        anomaly.analyse_run(run_row(), fixes=parked)

        types = [call.kwargs["anomaly_type"] for call in anomaly.flag.call_args_list]
        self.assertIn(ANOMALY_TYPE.UNEXPECTED_STOP, types)
        self.assertNotIn(ANOMALY_TYPE.IDLE, types, "one stop is one finding, not two")

    @staticmethod
    def _exploding_detector(run, fixes):
        raise RuntimeError("boom")

    def test_a_failing_detector_marks_the_pass_not_ok_so_the_trail_survives(self) -> None:
        with patch.object(anomaly, "_detect_speeding", self._exploding_detector):
            result = anomaly.analyse_run(run_row(), fixes=[])

        self.assertFalse(result["ok"])

    def test_one_failing_detector_does_not_stop_the_others(self) -> None:
        with patch.object(anomaly, "_detect_detour", self._exploding_detector):
            result = anomaly.analyse_run(run_row(mock_ping_count=2), fixes=[])

        self.assertFalse(result["ok"])
        types = [call.kwargs["anomaly_type"] for call in anomaly.flag.call_args_list]
        self.assertIn(ANOMALY_TYPE.MOCK_GPS, types)


class TestStaleWatchdog(unittest.TestCase):
    def test_a_quiet_run_is_flagged_alerted_and_stamped(self) -> None:
        from frappe.utils import add_to_date, now_datetime

        quiet = run_row(last_ping_on=str(add_to_date(now_datetime(), minutes=-30)), stale_alert_on=None)

        with patch.object(anomaly.courier_run, "stale_open_runs", return_value=[quiet]), patch.object(
            anomaly, "flag", return_value={"created": True}
        ) as flag, patch.object(anomaly.courier_run, "mark_stale_alerted") as stamp, patch.dict(
            "sys.modules", {"jarz_courier.services.push": MagicMock()}
        ):
            result = anomaly.watch_stale_pings()

        self.assertEqual(1, result["alerted"])
        self.assertEqual(ANOMALY_TYPE.STALE_PING, flag.call_args.kwargs["anomaly_type"])
        stamp.assert_called_once_with("CRUN-00001")

    def test_an_already_alerted_run_is_not_re_alerted(self) -> None:
        """Re-alerting every five minutes trains ops to ignore the alert."""
        from frappe.utils import add_to_date, now_datetime

        quiet = run_row(
            last_ping_on=str(add_to_date(now_datetime(), minutes=-30)),
            stale_alert_on="2026-08-08 14:00:00",
        )

        with patch.object(anomaly.courier_run, "stale_open_runs", return_value=[quiet]), patch.object(
            anomaly, "flag"
        ) as flag, patch.dict("sys.modules", {"jarz_courier.services.push": MagicMock()}):
            result = anomaly.watch_stale_pings()

        self.assertEqual(0, result["alerted"])
        flag.assert_not_called()

    def test_hours_of_silence_closes_the_run_as_abandoned(self) -> None:
        """So the run stops being open forever AND its polyline gets written."""
        from frappe.utils import add_to_date, now_datetime

        dead = run_row(last_ping_on=str(add_to_date(now_datetime(), minutes=-400)))

        with patch.object(anomaly.courier_run, "stale_open_runs", return_value=[dead]), patch.object(
            anomaly.courier_run, "close_run"
        ) as close, patch.dict("sys.modules", {"jarz_courier.services.push": MagicMock()}):
            result = anomaly.watch_stale_pings()

        self.assertEqual(1, result["abandoned"])
        self.assertEqual("Abandoned", close.call_args.kwargs["status"])

    def test_a_broken_run_does_not_stop_the_sweep(self) -> None:
        from frappe.utils import add_to_date, now_datetime

        rows = [
            run_row(name="CRUN-BAD", last_ping_on=str(add_to_date(now_datetime(), minutes=-30))),
            run_row(name="CRUN-OK", last_ping_on=str(add_to_date(now_datetime(), minutes=-30))),
        ]
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"created": True}

        with patch.object(anomaly.courier_run, "stale_open_runs", return_value=rows), patch.object(
            anomaly, "flag", side_effect=flaky
        ), patch.object(anomaly.courier_run, "mark_stale_alerted"), patch.dict(
            "sys.modules", {"jarz_courier.services.push": MagicMock()}
        ):
            result = anomaly.watch_stale_pings()

        self.assertEqual(2, result["examined"])
        self.assertEqual(1, result["alerted"])


class TestTheControllerRecordsWhoDismissed(unittest.TestCase):
    """An unattributed dismissal is what would make this whole table ignorable."""

    def make(self, **fields):
        from jarz_courier.doctype.courier_anomaly.courier_anomaly import CourierAnomaly

        doc = CourierAnomaly()
        doc.anomaly_type = ANOMALY_TYPE.DETOUR
        doc.severity = SEVERITY.MEDIUM
        doc.reason = "Rode 8 km against a 4 km plan"
        doc.party_type = "Employee"
        doc.party = "HR-EMP-1"
        doc.status = "Open"
        doc.detected_on = None
        doc.reviewed_by = None
        doc.reviewed_on = None
        doc.__dict__.update(fields)
        return doc

    def test_a_finding_with_no_reason_is_refused(self) -> None:
        doc = self.make(reason="   ")
        with self.assertRaises(Exception) as caught:
            doc.validate()
        self.assertIn("what was measured", str(caught.exception))

    def test_detected_on_defaults_to_now(self) -> None:
        doc = self.make()
        doc.validate()
        self.assertIsNotNone(doc.detected_on)

    def test_an_open_finding_records_no_reviewer(self) -> None:
        doc = self.make()
        doc.validate()
        self.assertIsNone(doc.reviewed_by)

    def test_dismissing_from_a_list_view_still_records_the_actor(self) -> None:
        """The most likely place a finding gets dismissed."""
        doc = self.make(status="Dismissed")
        doc.get_doc_before_save = lambda: None
        doc.validate()

        self.assertEqual("courier@example.com", doc.reviewed_by)
        self.assertIsNotNone(doc.reviewed_on)

    def test_reviewing_an_open_finding_records_the_actor(self) -> None:
        class Previous:
            status = "Open"

        doc = self.make(status="Reviewed")
        doc.get_doc_before_save = lambda: Previous()
        doc.validate()

        self.assertEqual("courier@example.com", doc.reviewed_by)

    def test_editing_an_already_reviewed_finding_keeps_the_original_reviewer(self) -> None:
        class Previous:
            status = "Reviewed"

        doc = self.make(status="Reviewed", reviewed_by="manager@example.com")
        doc.get_doc_before_save = lambda: Previous()
        doc.validate()

        self.assertEqual("manager@example.com", doc.reviewed_by)


class TestScheduledEntryPoint(unittest.TestCase):
    def test_it_never_raises_even_when_every_stage_fails(self) -> None:
        """A scheduler job that throws every tick gets disabled, switching B9 off."""
        with patch.object(anomaly, "watch_stale_pings", side_effect=RuntimeError("a")), patch.object(
            anomaly, "sweep_unchecked_runs", side_effect=RuntimeError("b")
        ), patch.object(anomaly, "detect_far_from_pin", side_effect=RuntimeError("c")):
            summary = anomaly.scheduled_detect()

        self.assertEqual({"stale", "runs", "proofs"}, set(summary))
        self.assertTrue(all(stage.get("error") for stage in summary.values()))

    def test_one_failing_stage_does_not_take_the_others_down(self) -> None:
        with patch.object(anomaly, "watch_stale_pings", side_effect=RuntimeError("a")), patch.object(
            anomaly, "sweep_unchecked_runs", return_value={"examined": 2}
        ), patch.object(anomaly, "detect_far_from_pin", return_value={"checked": 5}):
            summary = anomaly.scheduled_detect()

        self.assertTrue(summary["stale"].get("error"))
        self.assertEqual(2, summary["runs"]["examined"])
        self.assertEqual(5, summary["proofs"]["checked"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

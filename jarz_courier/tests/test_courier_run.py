"""``services/courier_run`` — the cold path: one polyline per run, written once.

The design claim being tested is that thousands of GPS fixes become exactly one row
and one string, and that the arithmetic on the way is the filtered arithmetic rather
than the raw one. Three properties carry real risk:

* **One write, and it is filtered.** If ``close_run`` ever stored the raw distance, a
  parked handset's drift would inflate every run in the system, in the courier's
  favour, permanently — and the inflated number is the one a fuel allowance would be
  built on.
* **The trail is not deleted until the detectors have seen it.** A polyline has no time
  axis, so idle, speeding and ping-gap detection are impossible to reconstruct
  afterwards. Deleting the trail on a failed analysis destroys the only copy of the
  evidence.
* **The planned route starts at the first fix, not the first stop.** There is no branch
  coordinate anywhere in this system, so a chain starting at the first customer omits
  the depot leg — real riding with no planned counterpart — which inflates the detour
  ratio and manufactures findings against couriers who did nothing wrong.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.constants import RUN_STATUS  # noqa: E402
from jarz_courier.services import courier_run  # noqa: E402


class FakeDoc:
    """A saveable stand-in for a Courier Run document."""

    def __init__(self, **fields):
        self.__dict__.update(fields)
        self.saved = False
        self.inserted = False

    def get(self, key, default=None):
        return getattr(self, key, default)

    def save(self, *args, **kwargs):
        self.saved = True

    def insert(self, *args, **kwargs):
        self.inserted = True
        self.name = getattr(self, "name", None) or "CRUN-NEW"


def open_run(**extra):
    row = {
        "name": "CRUN-00001",
        "party_type": "Employee",
        "party": "HR-EMP-00042",
        "branch": "Dokki",
        "duty": "CDUTY-00001",
        "status": RUN_STATUS.OPEN,
        "started_on": "2026-08-08 08:00:00",
        "ended_on": None,
        "last_ping_on": "2026-08-08 14:00:00",
        "mock_ping_count": 0,
        "ping_count": 120,
    }
    row.update(extra)
    return row


def fix(lat, lng, epoch, accuracy=5.0):
    return {"lat": lat, "lng": lng, "epoch": float(epoch), "accuracy": accuracy, "is_mocked": 0}


class TestEnsureOpenRun(unittest.TestCase):
    def test_an_existing_open_run_is_reused_not_duplicated(self) -> None:
        """Start Duty replayed from an offline queue must not open a second run."""
        with patch.object(courier_run, "get_open_run", return_value=open_run()), patch.object(
            courier_run.frappe, "new_doc"
        ) as new_doc:
            result = courier_run.ensure_open_run(
                party_type="Employee", party="HR-EMP-00042", branch="Dokki"
            )

        new_doc.assert_not_called()
        self.assertFalse(result["created"])
        self.assertEqual("CRUN-00001", result["run"]["name"])

    def test_a_run_opened_by_a_ping_adopts_the_duty_that_arrives_later(self) -> None:
        """Tracking can start before Start Duty; the two must converge on one run."""
        with patch.object(
            courier_run, "get_open_run", return_value=open_run(duty=None)
        ), patch.object(courier_run.frappe.db, "set_value") as set_value:
            result = courier_run.ensure_open_run(
                party_type="Employee",
                party="HR-EMP-00042",
                branch="Dokki",
                duty="CDUTY-00009",
            )

        set_value.assert_called_once()
        self.assertEqual("CDUTY-00009", result["run"]["duty"])

    def test_a_first_ping_opens_a_run_with_no_duty(self) -> None:
        doc = FakeDoc(name="CRUN-00002")
        with patch.object(courier_run, "get_open_run", return_value=None), patch.object(
            courier_run.frappe, "new_doc", return_value=doc
        ):
            result = courier_run.ensure_open_run(
                party_type="Employee", party="HR-EMP-00042", branch="Dokki"
            )

        self.assertTrue(result["created"])
        self.assertTrue(doc.inserted)
        self.assertEqual(RUN_STATUS.OPEN, doc.status)


class TestTouchRun(unittest.TestCase):
    def test_the_throttle_suppresses_most_pings(self) -> None:
        """At one fix every 5 s this is the difference between 12 and 720 writes an hour."""
        with patch.object(
            courier_run.location_cache, "should_touch_run", return_value=False
        ), patch.object(courier_run.frappe.db, "set_value") as set_value:
            written = courier_run.touch_run(
                "CRUN-00001", branch="Dokki", party="HR-EMP-1", fix=fix(30.0, 31.0, 1000)
            )

        self.assertFalse(written)
        set_value.assert_not_called()

    def test_it_stamps_position_and_counters_without_bumping_modified(self) -> None:
        """``update_modified=False`` keeps a concurrently open copy of the run saveable."""
        with patch.object(
            courier_run.location_cache, "should_touch_run", return_value=True
        ), patch.object(
            courier_run.frappe.db, "get_value", return_value={"ping_count": 5, "mock_ping_count": 0}
        ), patch.object(courier_run.frappe.db, "set_value") as set_value:
            courier_run.touch_run(
                "CRUN-00001",
                branch="Dokki",
                party="HR-EMP-1",
                fix=fix(30.0444, 31.2357, 1000),
                accepted=1,
            )

        _doctype, _name, updates = set_value.call_args.args
        self.assertEqual(6, updates["ping_count"])
        self.assertEqual(30.0444, updates["last_latitude"])
        self.assertFalse(set_value.call_args.kwargs["update_modified"])

    def test_force_bypasses_the_throttle_for_a_mocked_fix(self) -> None:
        """A courier who spoofs for 30 s must leave a durable trace regardless."""
        with patch.object(
            courier_run.location_cache, "should_touch_run", return_value=False
        ), patch.object(
            courier_run.frappe.db, "get_value", return_value={"ping_count": 0, "mock_ping_count": 2}
        ), patch.object(courier_run.frappe.db, "set_value") as set_value:
            courier_run.touch_run(
                "CRUN-00001", branch="Dokki", party="HR-EMP-1", mocked=3, force=True
            )

        _doctype, _name, updates = set_value.call_args.args
        self.assertEqual(5, updates["mock_ping_count"])

    def test_a_database_failure_does_not_raise_into_the_ping_request(self) -> None:
        with patch.object(
            courier_run.location_cache, "should_touch_run", return_value=True
        ), patch.object(courier_run.frappe.db, "get_value", side_effect=RuntimeError("boom")):
            self.assertFalse(
                courier_run.touch_run("CRUN-00001", branch="Dokki", party="HR-EMP-1", accepted=1)
            )


class CloseRunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = FakeDoc(name="CRUN-00001", status=RUN_STATUS.OPEN)
        self.analysis = {"ok": True, "findings": []}

        self.patches = [
            patch.object(courier_run, "_resolve", return_value=open_run()),
            patch.object(courier_run, "planned_route", return_value={
                "planned_distance_m": 4000.0,
                "stops_delivered": 3,
                "pinned_stops": 3,
                "stops": [],
            }),
            patch.object(courier_run, "_analyse", side_effect=lambda *a, **k: self.analysis),
            patch.object(courier_run.frappe, "get_doc", return_value=self.doc),
            patch.object(courier_run.location_cache, "drop_trail"),
            patch.object(courier_run.location_cache, "clear_position"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.drop_trail = courier_run.location_cache.drop_trail
        self.clear_position = courier_run.location_cache.clear_position


class TestCloseRun(CloseRunTestCase):
    def test_the_stored_distance_is_the_filtered_distance(self) -> None:
        """Ten fixes of parked drift plus one real 500 m leg is 500 m, not 500 m + drift."""
        drift = [fix(30.0 + m * 0.000009, 31.0, 1000 + i * 60) for i, m in enumerate([0, 8, -5, 7, -9])]
        trail = drift + [fix(30.0045, 31.0, 1400)]

        with patch.object(courier_run.location_cache, "read_trail", return_value=trail):
            result = courier_run.close_run(open_run())

        self.assertAlmostEqual(500.0, self.doc.total_distance_m, delta=10.0)
        self.assertEqual(2, self.doc.point_count, "origin plus the real move")
        self.assertEqual(4, self.doc.dropped_point_count)
        self.assertEqual(6, result["raw_fix_count"])

    def test_exactly_one_polyline_is_written_and_it_decodes(self) -> None:
        trail = [fix(30.0, 31.0, 1000), fix(30.0045, 31.0, 1060), fix(30.009, 31.0, 1120)]

        with patch.object(courier_run.location_cache, "read_trail", return_value=trail):
            courier_run.close_run(open_run())

        from jarz_courier.services import geo_track

        decoded = geo_track.decode_polyline(self.doc.polyline)
        self.assertEqual(3, len(decoded))
        self.assertAlmostEqual(30.0, decoded[0][0], places=5)

    def test_the_run_is_closed_and_stamped(self) -> None:
        with patch.object(courier_run.location_cache, "read_trail", return_value=[]):
            courier_run.close_run(open_run())

        self.assertEqual(RUN_STATUS.CLOSED, self.doc.status)
        self.assertIsNotNone(self.doc.ended_on)
        self.assertTrue(self.doc.saved)

    def test_the_trail_is_dropped_only_after_a_successful_analysis(self) -> None:
        with patch.object(courier_run.location_cache, "read_trail", return_value=[]):
            courier_run.close_run(open_run())

        self.drop_trail.assert_called_once_with("Dokki", "HR-EMP-00042")

    def test_a_failed_analysis_keeps_the_trail_for_a_retry(self) -> None:
        """The polyline has no time axis, so a lost trail is lost evidence."""
        self.analysis = {"ok": False, "findings": []}

        with patch.object(courier_run.location_cache, "read_trail", return_value=[]):
            courier_run.close_run(open_run())

        self.drop_trail.assert_not_called()
        # The live position still goes: the courier has finished either way, and it is
        # the trail — the evidence — that has to survive for the retry.
        self.clear_position.assert_called_once()

    def test_the_live_position_is_always_cleared(self) -> None:
        """A finished courier must stop appearing on the ops map."""
        with patch.object(courier_run.location_cache, "read_trail", return_value=[]):
            courier_run.close_run(open_run())

        self.clear_position.assert_called_once_with("Dokki", "HR-EMP-00042")

    def test_closing_an_already_closed_run_is_a_no_op(self) -> None:
        with patch.object(courier_run, "_resolve", return_value=open_run(status=RUN_STATUS.CLOSED)):
            result = courier_run.close_run(open_run())

        self.assertFalse(result["changed"])
        self.assertFalse(self.doc.saved)

    def test_a_missing_run_is_reported_not_raised(self) -> None:
        with patch.object(courier_run, "_resolve", return_value=None):
            result = courier_run.close_run("CRUN-GONE")

        self.assertFalse(result["changed"])
        self.assertEqual("not found", result["reason"])

    def test_the_abandoned_status_is_recorded_distinctly(self) -> None:
        """A force-stopped app must not look like a normal end of day in a report."""
        with patch.object(courier_run.location_cache, "read_trail", return_value=[]):
            courier_run.close_run(open_run(), status=RUN_STATUS.ABANDONED)

        self.assertEqual(RUN_STATUS.ABANDONED, self.doc.status)


class TestPlannedRoute(unittest.TestCase):
    STOPS = [
        {"name": "INV-1", "shipping_address_name": "ADDR-1", "custom_delivered_at": "1"},
        {"name": "INV-2", "shipping_address_name": "ADDR-2", "custom_delivered_at": "2"},
    ]
    PINS = {
        "ADDR-1": {"latitude": 30.0045, "longitude": 31.0},
        "ADDR-2": {"latitude": 30.009, "longitude": 31.0},
    }

    def test_the_origin_is_the_first_fix_so_the_depot_leg_is_counted(self) -> None:
        """Omitting the depot leg inflates the ratio and invents detours."""
        with patch.object(courier_run, "_delivered_stops", return_value=self.STOPS), patch.object(
            courier_run.run_sheet, "load_address_pins", return_value=self.PINS
        ):
            with_origin = courier_run.planned_route(open_run(), [fix(30.0, 31.0, 1000)])
            without_origin = courier_run.planned_route(open_run(), [])

        # origin -> ADDR-1 -> ADDR-2 is two legs; ADDR-1 -> ADDR-2 is one.
        self.assertAlmostEqual(1000.0, with_origin["planned_distance_m"], delta=15.0)
        self.assertAlmostEqual(500.0, without_origin["planned_distance_m"], delta=15.0)

    def test_a_single_pinned_stop_yields_no_planned_distance(self) -> None:
        """One leg says nothing about a route, so the detour detector must skip it."""
        with patch.object(
            courier_run, "_delivered_stops", return_value=self.STOPS[:1]
        ), patch.object(courier_run.run_sheet, "load_address_pins", return_value=self.PINS):
            route = courier_run.planned_route(open_run(), [fix(30.0, 31.0, 1000)])

        self.assertEqual(0.0, route["planned_distance_m"])
        self.assertEqual(1, route["stops_delivered"])

    def test_an_unpinned_stop_shortens_the_plan_rather_than_teleporting_it(self) -> None:
        stops = self.STOPS + [
            {"name": "INV-3", "shipping_address_name": "ADDR-NONE", "custom_delivered_at": "3"}
        ]
        with patch.object(courier_run, "_delivered_stops", return_value=stops), patch.object(
            courier_run.run_sheet, "load_address_pins", return_value=self.PINS
        ):
            route = courier_run.planned_route(open_run(), [fix(30.0, 31.0, 1000)])

        self.assertEqual(3, route["stops_delivered"])
        self.assertEqual(2, route["pinned_stops"])
        self.assertAlmostEqual(1000.0, route["planned_distance_m"], delta=15.0)

    def test_a_run_with_no_deliveries_has_no_plan(self) -> None:
        with patch.object(courier_run, "_delivered_stops", return_value=[]):
            route = courier_run.planned_route(open_run(), [fix(30.0, 31.0, 1000)])

        self.assertEqual(0.0, route["planned_distance_m"])
        self.assertEqual(0, route["stops_delivered"])


class TestDeliveredStopsAreMetaSafe(unittest.TestCase):
    def test_a_missing_lane_a1_field_degrades_loudly_instead_of_lying(self) -> None:
        """A silent [] here would read as "this courier delivered nothing"."""
        with patch.object(
            courier_run.frappe, "get_all", side_effect=RuntimeError("unknown column")
        ), patch.object(courier_run.frappe, "log_error") as log_error:
            stops = courier_run._delivered_stops(open_run())

        self.assertEqual([], stops)
        log_error.assert_called_once()


class TestStaleRunDetection(unittest.TestCase):
    def test_a_run_that_never_pinged_at_all_is_still_found(self) -> None:
        """The most suspicious case of all, and the one a SQL `<` drops silently.

        ``last_ping_on`` is NULL when tracking permission was denied or the app was
        killed on the first second. Falling back to ``started_on`` is what makes it
        visible.
        """
        rows = [open_run(last_ping_on=None, started_on="2020-01-01 08:00:00")]
        with patch.object(courier_run.frappe, "get_all", return_value=rows):
            stale = courier_run.stale_open_runs(minutes=20)

        self.assertEqual(1, len(stale))

    def test_a_recently_pinged_run_is_not_stale(self) -> None:
        from frappe.utils import now_datetime

        rows = [open_run(last_ping_on=str(now_datetime()))]
        with patch.object(courier_run.frappe, "get_all", return_value=rows):
            self.assertEqual([], courier_run.stale_open_runs(minutes=20))

    def test_an_unparseable_timestamp_is_skipped_not_fatal(self) -> None:
        rows = [open_run(last_ping_on="not a date", started_on="also not a date")]
        with patch.object(courier_run.frappe, "get_all", return_value=rows):
            self.assertEqual([], courier_run.stale_open_runs(minutes=20))


class TestTheControllerInvariants(unittest.TestCase):
    """Enforced in the controller, not the service, so a Desk edit obeys them too."""

    def make(self, **fields):
        from jarz_courier.doctype.courier_run.courier_run import CourierRun

        doc = CourierRun()
        doc.name = fields.pop("name", "CRUN-00001")
        doc.party_type = "Employee"
        doc.party = "HR-EMP-00042"
        doc.branch = "Dokki"
        doc.status = RUN_STATUS.OPEN
        doc.started_on = "2026-08-08 08:00:00"
        doc.ended_on = None
        doc.__dict__.update(fields)
        return doc

    def test_a_second_open_run_is_refused(self) -> None:
        """Two open runs make "which run does this ping belong to?" unanswerable."""
        import frappe as frappe_module

        doc = self.make()
        with patch.object(frappe_module, "get_all", return_value=["CRUN-EXISTING"]):
            with self.assertRaises(Exception) as caught:
                doc.validate()

        self.assertIn("already has an open run", str(caught.exception))

    def test_the_first_open_run_is_allowed(self) -> None:
        import frappe as frappe_module

        doc = self.make()
        with patch.object(frappe_module, "get_all", return_value=[]):
            doc.validate()

        self.assertEqual(RUN_STATUS.OPEN, doc.status)

    def test_a_closed_run_cannot_be_reopened(self) -> None:
        """Its polyline and distance have already been written and reported against."""
        import frappe as frappe_module

        doc = self.make()
        doc.get_doc_before_save = lambda: FakeDoc(status=RUN_STATUS.CLOSED)
        with patch.object(frappe_module, "get_all", return_value=[]):
            with self.assertRaises(Exception) as caught:
                doc.validate()

        self.assertIn("cannot be reopened", str(caught.exception))

    def test_an_abandoned_run_cannot_be_reopened_either(self) -> None:
        import frappe as frappe_module

        doc = self.make()
        doc.get_doc_before_save = lambda: FakeDoc(status=RUN_STATUS.ABANDONED)
        with patch.object(frappe_module, "get_all", return_value=[]):
            with self.assertRaises(Exception):
                doc.validate()

    def test_an_end_before_the_start_is_refused(self) -> None:
        doc = self.make(status=RUN_STATUS.CLOSED, ended_on="2026-08-08 07:00:00")
        with self.assertRaises(Exception) as caught:
            doc.validate()
        self.assertIn("cannot be before", str(caught.exception))

    def test_closing_without_an_end_time_stamps_one(self) -> None:
        import frappe as frappe_module

        doc = self.make(status=RUN_STATUS.CLOSED)
        with patch.object(frappe_module, "get_all", return_value=[]):
            doc.validate()

        self.assertIsNotNone(doc.ended_on)


class TestTheDoctypeSchema(unittest.TestCase):
    def test_it_holds_one_polyline_field_and_no_per_ping_child_table(self) -> None:
        """The whole point of B7's cold path: one string, not one row per fix."""
        import json
        from pathlib import Path

        schema = json.loads(
            (
                Path(courier_run.__file__).resolve().parents[1]
                / "doctype"
                / "courier_run"
                / "courier_run.json"
            ).read_text(encoding="utf-8")
        )
        fields = schema["fields"]

        polylines = [f for f in fields if f["fieldname"] == "polyline"]
        self.assertEqual(1, len(polylines))
        self.assertEqual("Long Text", polylines[0]["fieldtype"])

        tables = [f["fieldname"] for f in fields if f["fieldtype"] in ("Table", "Table MultiSelect")]
        self.assertEqual([], tables, "a child table here would be a row per ping again")

    def test_it_declares_no_monetary_field(self) -> None:
        """A distance is an input to an allowance decision, not the decision."""
        import json
        from pathlib import Path

        schema = json.loads(
            (
                Path(courier_run.__file__).resolve().parents[1]
                / "doctype"
                / "courier_run"
                / "courier_run.json"
            ).read_text(encoding="utf-8")
        )
        offenders = [f["fieldname"] for f in schema["fields"] if f["fieldtype"] == "Currency"]
        self.assertEqual([], offenders)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

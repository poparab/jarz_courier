"""F3 - ``Courier Stop Event`` recording and the ingest hook that feeds it.

Three things carry the weight:

* **Idempotence.** One row per (invoice, event): a drained offline queue, a retry and
  two workers must all collapse to the first row.
* **Earliest fix wins.** A batch is read in time order, so a milestone is dated to when
  it happened and not to when the signal came back.
* **Never in the way of ingest.** The hook runs after the position is stored; nothing in
  it may turn a good ping into an error the phone would retry.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import tracking as api  # noqa: E402
from jarz_courier.doctype.courier_stop_event import courier_stop_event  # noqa: E402
from jarz_courier.services import stop_events as se  # noqa: E402

INVOICE = "ACC-SINV-2026-00042"
PIN = (30.0444, 31.2357)
#: ~55 m from the pin: inside the 100 m arrival circle.
AT_DOOR = (30.0449, 31.2357)
#: ~1.1 km from the pin.
ON_THE_ROAD = (30.0544, 31.2357)
BRANCH = {"latitude": 30.0100, "longitude": 31.2000, "radius_m": 200.0}
OUT_OF_BRANCH = (30.0300, 31.2000)

STOP = {
    "invoice": INVOICE,
    "state": "Out for Delivery",
    "address": {"latitude": PIN[0], "longitude": PIN[1]},
}


def _fix(lat_lng, ts, **extra):
    stamp = datetime.fromisoformat(ts)
    fix = {"lat": lat_lng[0], "lng": lat_lng[1], "ts": ts, "epoch": stamp.timestamp(), "accuracy": 10.0}
    fix.update(extra)
    return fix


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded = {}
        self.created = []
        self.arrive = MagicMock(return_value={"success": True})
        self.dispatched = {INVOICE: datetime(2026, 10, 14, 12, 0, 0)}
        self.fence = BRANCH
        self.back = []
        for p in (
            patch.object(se, "_recorded_events", side_effect=lambda invs: {k: set(v) for k, v in self.recorded.items()}),
            patch.object(se, "record_event", side_effect=self._record),
            patch.object(se.pos_bridge, "mark_invoice_arrived", self.arrive),
            patch.object(se, "_dispatch_times", side_effect=lambda invs: dict(self.dispatched)),
            patch.object(se, "branch_geofence", side_effect=lambda *a, **k: self.fence),
            patch.object(se, "_back_candidates", side_effect=lambda *a, **k: list(self.back)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _record(self, **kw):
        self.created.append(kw)
        return {"created": True, "name": "x"}

    def process(self, fixes, stops=(STOP,), source="app"):
        return se.process_fixes(
            party_type="Employee",
            party="HR-EMP-00042",
            branch="Dokki",
            fixes=fixes,
            stops=list(stops),
            run="CRUN-1",
            source=source,
        )

    def events(self):
        return [c["event"] for c in self.created]


class TestArrived(_Case):
    def test_a_fix_at_the_door_records_arrival_and_stamps_the_invoice(self) -> None:
        summary = self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")])
        self.assertEqual(1, summary["arrived"])
        self.assertIn("Arrived", self.events())
        self.arrive.assert_called_once()
        self.assertEqual(f"{INVOICE}::auto-arrived", self.arrive.call_args.kwargs["request_id"])

    def test_the_record_carries_who_where_and_from_which_path(self) -> None:
        self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")], source="owntracks")
        arrived = next(c for c in self.created if c["event"] == "Arrived")
        self.assertEqual("owntracks", arrived["source"])
        self.assertEqual("HR-EMP-00042", arrived["party"])
        self.assertEqual("CRUN-1", arrived["run"])
        self.assertEqual(AT_DOOR[0], arrived["latitude"])

    def test_the_earliest_qualifying_fix_dates_the_arrival(self) -> None:
        self.process(
            [_fix(AT_DOOR, "2026-10-14 12:40:00"), _fix(AT_DOOR, "2026-10-14 12:30:00")]
        )
        arrived = next(c for c in self.created if c["event"] == "Arrived")
        self.assertEqual(datetime(2026, 10, 14, 12, 30, 0), arrived["at"])

    def test_a_fix_on_the_road_is_not_an_arrival(self) -> None:
        self.assertEqual(0, self.process([_fix(ON_THE_ROAD, "2026-10-14 12:30:00")])["arrived"])
        self.arrive.assert_not_called()

    def test_an_arrival_already_on_file_is_not_repeated(self) -> None:
        self.recorded = {INVOICE: {"Arrived"}}
        self.assertEqual(0, self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")])["arrived"])
        self.assertNotIn("Arrived", self.events())

    def test_an_imprecise_fix_proves_nothing(self) -> None:
        self.assertEqual(0, self.process([_fix(AT_DOOR, "2026-10-14 12:30:00", accuracy=250.0)])["arrived"])

    def test_a_mocked_fix_is_ignored(self) -> None:
        self.assertEqual(0, self.process([_fix(AT_DOOR, "2026-10-14 12:30:00", is_mocked=True)])["arrived"])

    def test_a_stop_without_a_pin_never_arrives(self) -> None:
        stop = dict(STOP, address={})
        self.assertEqual(0, self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")], stops=[stop])["arrived"])

    def test_an_already_stamped_invoice_is_not_stamped_again(self) -> None:
        stop = dict(STOP, arrived_at="2026-10-14 12:29:00")
        self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")], stops=[stop])
        self.arrive.assert_not_called()

    def test_a_declined_stamp_does_not_lose_the_event(self) -> None:
        self.arrive.return_value = {"success": False, "error": "already arrived"}
        self.assertEqual(1, self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")])["arrived"])

    def test_a_failing_stamp_does_not_lose_the_event(self) -> None:
        self.arrive.side_effect = RuntimeError("jarz_pos down")
        self.assertEqual(1, self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")])["arrived"])


class TestBranchExit(_Case):
    def test_the_first_fix_outside_the_fence_after_dispatch_is_the_departure(self) -> None:
        summary = self.process(
            [_fix((30.0100, 31.2000), "2026-10-14 12:05:00"), _fix(OUT_OF_BRANCH, "2026-10-14 12:08:00")]
        )
        self.assertEqual(1, summary["left"])
        left = next(c for c in self.created if c["event"] == "Left Branch")
        self.assertEqual(datetime(2026, 10, 14, 12, 8, 0), left["at"])

    def test_a_fix_from_before_dispatch_is_not_a_departure(self) -> None:
        self.assertEqual(0, self.process([_fix(OUT_OF_BRANCH, "2026-10-14 11:50:00")])["left"])

    def test_no_state_log_anchor_means_no_departure(self) -> None:
        self.dispatched = {}
        self.assertEqual(0, self.process([_fix(OUT_OF_BRANCH, "2026-10-14 12:08:00")])["left"])

    def test_no_shift_location_means_no_departure(self) -> None:
        self.fence = None
        self.assertEqual(0, self.process([_fix(OUT_OF_BRANCH, "2026-10-14 12:08:00")])["left"])

    def test_a_departure_already_on_file_is_not_repeated(self) -> None:
        self.recorded = {INVOICE: {"Left Branch"}}
        self.assertEqual(0, self.process([_fix(OUT_OF_BRANCH, "2026-10-14 12:08:00")])["left"])


class TestBackAtBranch(_Case):
    def test_the_first_fix_inside_the_fence_after_the_attempt_is_the_return(self) -> None:
        self.back = [{"invoice": "ACC-SINV-2026-00099", "since": datetime(2026, 10, 14, 13, 0, 0)}]
        summary = self.process(
            [_fix((30.0100, 31.2000), "2026-10-14 12:50:00"), _fix((30.0100, 31.2000), "2026-10-14 13:20:00")],
            stops=[],
        )
        self.assertEqual(1, summary["back"])
        back = next(c for c in self.created if c["event"] == "Back At Branch")
        self.assertEqual("ACC-SINV-2026-00099", back["invoice"])
        self.assertEqual(datetime(2026, 10, 14, 13, 20, 0), back["at"])

    def test_still_on_the_road_is_not_back(self) -> None:
        self.back = [{"invoice": "ACC-SINV-2026-00099", "since": datetime(2026, 10, 14, 13, 0, 0)}]
        self.assertEqual(0, self.process([_fix(OUT_OF_BRANCH, "2026-10-14 13:20:00")], stops=[])["back"])


class TestNeverRaises(_Case):
    def test_a_storage_failure_is_swallowed(self) -> None:
        with patch.object(se, "record_event", side_effect=RuntimeError("db down")):
            summary = self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")])
        self.assertEqual(0, summary["arrived"])

    def test_nothing_usable_returns_an_empty_summary(self) -> None:
        self.assertEqual({"arrived": 0, "left": 0, "back": 0}, self.process([{"lat": 999, "lng": 0}, "junk"]))

    def test_no_stops_and_no_candidates_does_nothing(self) -> None:
        self.assertEqual(
            {"arrived": 0, "left": 0, "back": 0}, self.process([_fix(AT_DOOR, "2026-10-14 12:30:00")], stops=[])
        )


class TestRecordEvent(unittest.TestCase):
    def test_an_existing_row_makes_it_a_no_op(self) -> None:
        with patch.object(se.frappe.db, "exists", return_value="ROW-1"), patch.object(
            se.frappe, "get_doc"
        ) as get_doc:
            result = se.record_event(invoice=INVOICE, event="Arrived", at="2026-10-14 12:30:00")
        self.assertEqual({"created": False, "name": "ROW-1"}, result)
        get_doc.assert_not_called()

    def test_a_new_row_is_inserted(self) -> None:
        doc = MagicMock()
        doc.name = f"{INVOICE}-arrived"
        with patch.object(se.frappe.db, "exists", return_value=None), patch.object(
            se.frappe, "get_doc", return_value=doc
        ) as get_doc:
            result = se.record_event(
                invoice=INVOICE, event="Arrived", at="2026-10-14 12:30:00", latitude=30.0, source="owntracks"
            )
        self.assertTrue(result["created"])
        payload = get_doc.call_args.args[0]
        self.assertEqual("Courier Stop Event", payload["doctype"])
        self.assertEqual("owntracks", payload["source"])
        doc.insert.assert_called_once_with(ignore_permissions=True)

    def test_losing_the_insert_race_is_not_an_error(self) -> None:
        doc = MagicMock()
        doc.insert.side_effect = frappe.DuplicateEntryError("dup")
        with patch.object(se.frappe.db, "exists", return_value=None), patch.object(
            se.frappe, "get_doc", return_value=doc
        ):
            result = se.record_event(invoice=INVOICE, event="Arrived", at="2026-10-14 12:30:00")
        self.assertEqual({"created": False, "name": None}, result)

    def test_an_unknown_event_or_empty_invoice_is_refused_quietly(self) -> None:
        self.assertFalse(se.record_event(invoice=INVOICE, event="Teleported", at="x")["created"])
        self.assertFalse(se.record_event(invoice="", event="Arrived", at="x")["created"])


class TestDocumentName(unittest.TestCase):
    def test_the_name_is_derived_from_invoice_and_event(self) -> None:
        self.assertEqual(f"{INVOICE}-arrived", courier_stop_event.event_name(INVOICE, "Arrived"))
        self.assertEqual(f"{INVOICE}-left-branch", courier_stop_event.event_name(INVOICE, "Left Branch"))
        self.assertEqual(f"{INVOICE}-back-at-branch", courier_stop_event.event_name(INVOICE, "Back At Branch"))


class TestBranchGeofence(unittest.TestCase):
    def test_only_employees_have_a_shift_location(self) -> None:
        self.assertIsNone(se.branch_geofence("Supplier", "SUP-1"))

    def test_no_hrms_means_no_fence(self) -> None:
        with patch.object(se.frappe, "get_all", side_effect=RuntimeError("no Shift Assignment")):
            self.assertIsNone(se.branch_geofence("Employee", "HR-EMP-1"))

    def test_the_shift_locations_coordinates_and_radius_are_used(self) -> None:
        with patch.object(
            se.frappe, "get_all", return_value=[{"shift_location": "Dokki", "end_date": None}]
        ), patch.object(
            se.frappe.db,
            "get_value",
            return_value={"latitude": 30.01, "longitude": 31.2, "checkin_radius": 150},
        ):
            self.assertEqual(
                {"latitude": 30.01, "longitude": 31.2, "radius_m": 150.0}, se.branch_geofence("Employee", "HR-EMP-1")
            )

    def test_a_missing_radius_gets_the_default(self) -> None:
        with patch.object(
            se.frappe, "get_all", return_value=[{"shift_location": "Dokki", "end_date": None}]
        ), patch.object(
            se.frappe.db, "get_value", return_value={"latitude": 30.01, "longitude": 31.2, "checkin_radius": 0}
        ):
            self.assertEqual(se.DEFAULT_GEOFENCE_RADIUS_M, se.branch_geofence("Employee", "HR-EMP-1")["radius_m"])

    def test_a_location_without_coordinates_is_skipped(self) -> None:
        with patch.object(
            se.frappe, "get_all", return_value=[{"shift_location": "Dokki", "end_date": None}]
        ), patch.object(se.frappe.db, "get_value", return_value={"latitude": None, "longitude": None}):
            self.assertIsNone(se.branch_geofence("Employee", "HR-EMP-1"))


# ─────────────────────────────────────────────────────────────────────────────
# api/tracking hook
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestHook(unittest.TestCase):
    IDENTITY = dict(_support.COURIER_IDENTITY)

    def setUp(self) -> None:
        self.process = MagicMock()
        self.get_run = MagicMock(return_value={"stops": [STOP]})
        for p in (
            patch.object(api.stop_events, "process_fixes", self.process),
            patch.object(api.run_sheet, "get_run", self.get_run),
        ):
            p.start()
            self.addCleanup(p.stop)

    def hook(self, pings, source="app"):
        api._record_stop_events(self.IDENTITY, pings, run="CRUN-1", source=source)

    def test_a_good_ping_reaches_the_detector_with_the_current_run(self) -> None:
        self.hook([{"latitude": 30.0449, "longitude": 31.2357, "timestamp": "2026-10-14 12:30:00"}])
        kwargs = self.process.call_args.kwargs
        self.assertEqual([STOP], kwargs["stops"])
        self.assertEqual("CRUN-1", kwargs["run"])
        self.assertEqual("app", kwargs["source"])
        self.assertEqual(1, len(kwargs["fixes"]))
        self.assertAlmostEqual(30.0449, kwargs["fixes"][0]["lat"])

    def test_the_source_is_passed_through(self) -> None:
        self.hook([{"latitude": 30.04, "longitude": 31.23}], source=api.stop_events.SOURCE_OWNTRACKS)
        self.assertEqual("owntracks", self.process.call_args.kwargs["source"])

    def test_malformed_and_mocked_pings_cost_no_run_sheet_query(self) -> None:
        self.hook(["junk", {"latitude": 999, "longitude": 0}, {"latitude": 30.0, "longitude": 31.0, "is_mocked": True}])
        self.get_run.assert_not_called()
        self.process.assert_not_called()

    def test_a_failing_detector_never_reaches_the_caller(self) -> None:
        self.process.side_effect = RuntimeError("boom")
        self.hook([{"latitude": 30.04, "longitude": 31.23}])

    def test_a_failing_run_lookup_still_lets_ingest_succeed(self) -> None:
        self.get_run.side_effect = RuntimeError("db down")
        self.hook([{"latitude": 30.04, "longitude": 31.23}])
        self.assertEqual([], self.process.call_args.kwargs["stops"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

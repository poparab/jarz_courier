"""``services/assignment_watch`` — the poll that stands in for a hook it cannot have.

Assignment happens in ``jarz_pos``. This app cannot hook Sales Invoice (a doc_event here
would make it an untested observer running inside jarz_pos's transactions) and jarz_pos
cannot call in (contract §9 makes the dependency one-way). So the only mechanism left is
to look, and the tests below pin the two things that make looking safe.

**``None`` is not ``[]``.** A snapshot that has never been taken and a run that was
genuinely empty are different facts. Collapsing them means the first sweep after any
deploy tells every courier on shift that all of today's orders are brand new — a
notification storm that trains people to swipe the alerts away.

**Order is part of the assignment.** ``custom_delivery_sequence`` exists because the
sequence is instructions, not presentation. A set-only comparison silently swallows
"deliver the far one first".
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.services import assignment_watch  # noqa: E402


class TestDiffRuns(unittest.TestCase):
    def test_an_unchanged_run_is_not_a_change(self) -> None:
        diff = assignment_watch.diff_runs(["INV-1", "INV-2"], ["INV-1", "INV-2"])
        self.assertFalse(diff["changed"])

    def test_a_new_stop_is_an_addition(self) -> None:
        diff = assignment_watch.diff_runs(["INV-1"], ["INV-1", "INV-2"])
        self.assertEqual(["INV-2"], diff["added"])
        self.assertEqual([], diff["removed"])
        self.assertTrue(diff["changed"])

    def test_a_delivered_stop_leaving_the_run_is_a_removal(self) -> None:
        diff = assignment_watch.diff_runs(["INV-1", "INV-2"], ["INV-2"])
        self.assertEqual(["INV-1"], diff["removed"])
        self.assertEqual([], diff["added"])

    def test_resequencing_the_same_stops_is_a_change(self) -> None:
        """The order is the instruction. A set comparison would miss this entirely."""
        diff = assignment_watch.diff_runs(["INV-1", "INV-2"], ["INV-2", "INV-1"])
        self.assertTrue(diff["changed"])
        self.assertTrue(diff["resequenced"])
        self.assertEqual([], diff["added"])
        self.assertEqual([], diff["removed"])

    def test_an_empty_run_becoming_populated_is_an_addition(self) -> None:
        diff = assignment_watch.diff_runs([], ["INV-1"])
        self.assertEqual(["INV-1"], diff["added"])

    def test_a_run_emptying_is_a_removal(self) -> None:
        diff = assignment_watch.diff_runs(["INV-1"], [])
        self.assertEqual(["INV-1"], diff["removed"])


class SweepOneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.stops = [
            {"invoice": "INV-1", "display_id": "16834"},
            {"invoice": "INV-2", "display_id": "16835"},
        ]
        self.snapshot = None

        self.push = MagicMock()
        self.cache = MagicMock()
        self.cache.read_run_snapshot.side_effect = lambda *a: self.snapshot

        patches = [
            patch.object(assignment_watch, "push", self.push),
            patch.object(assignment_watch, "location_cache", self.cache),
            patch.object(
                assignment_watch.run_sheet,
                "get_run",
                side_effect=lambda **kwargs: {"stops": self.stops},
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def sweep_one(self):
        return assignment_watch._sweep_one("Employee", "HR-EMP-00042", "Dokki")


class TestFirstObservationIsSilent(SweepOneTestCase):
    def test_the_first_sweep_records_and_says_nothing(self) -> None:
        """Otherwise every deploy greets every courier with "6 new orders"."""
        self.snapshot = None

        outcome = self.sweep_one()

        self.assertEqual("first_seen", outcome)
        self.push.notify_new_assignment.assert_not_called()
        self.push.notify_run_changed.assert_not_called()
        self.cache.write_run_snapshot.assert_called_once_with(
            "Dokki", "HR-EMP-00042", ["INV-1", "INV-2"]
        )

    def test_an_observed_empty_run_is_a_real_baseline(self) -> None:
        """`[]` means we looked. A stop appearing afterwards IS news."""
        self.snapshot = []

        outcome = self.sweep_one()

        self.assertEqual("assigned", outcome)
        self.push.notify_new_assignment.assert_called_once()


class TestSweepOne(SweepOneTestCase):
    def test_a_new_stop_pushes_a_new_assignment_with_its_display_id(self) -> None:
        """The Woo number is what every screen and the call centre call the order."""
        self.snapshot = ["INV-1"]

        outcome = self.sweep_one()

        self.assertEqual("assigned", outcome)
        kwargs = self.push.notify_new_assignment.call_args.kwargs
        self.assertEqual(["INV-2"], kwargs["invoices"])
        self.assertEqual(["16835"], kwargs["display_ids"])

    def test_a_removal_pushes_a_refetch_hint_not_a_new_assignment(self) -> None:
        self.snapshot = ["INV-1", "INV-2", "INV-3"]

        outcome = self.sweep_one()

        self.assertEqual("changed", outcome)
        self.push.notify_new_assignment.assert_not_called()
        self.assertEqual(1, self.push.notify_run_changed.call_args.kwargs["removed"])

    def test_an_addition_alongside_a_removal_reports_the_addition(self) -> None:
        """The new order is the fact the courier must not miss; a refetch is implied."""
        self.snapshot = ["INV-1", "INV-OLD"]

        outcome = self.sweep_one()

        self.assertEqual("assigned", outcome)
        self.push.notify_new_assignment.assert_called_once()
        self.push.notify_run_changed.assert_not_called()

    def test_no_change_pushes_nothing(self) -> None:
        self.snapshot = ["INV-1", "INV-2"]

        self.assertIsNone(self.sweep_one())
        self.push.notify_new_assignment.assert_not_called()
        self.push.notify_run_changed.assert_not_called()

    def test_the_snapshot_is_always_rewritten_even_when_nothing_changed(self) -> None:
        """It carries a TTL, so not refreshing it would make the next sweep "first"."""
        self.snapshot = ["INV-1", "INV-2"]
        self.sweep_one()
        self.cache.write_run_snapshot.assert_called_once()

    def test_the_run_sheet_query_is_branch_scoped(self) -> None:
        self.snapshot = []
        self.sweep_one()
        self.assertEqual(["Dokki"], assignment_watch.run_sheet.get_run.call_args.kwargs["branches"])


class TestActiveCouriers(unittest.TestCase):
    DUTIES = [{"party_type": "Employee", "party": "HR-EMP-1", "branch": "Dokki"}]
    RUNS = [{"party_type": "Employee", "party": "HR-EMP-2", "branch": "Zamalek"}]

    def test_it_is_the_union_of_open_duties_and_open_runs(self) -> None:
        """Neither alone is reliable: tracking can start before Start Duty, and a duty
        can exist with location permission denied."""
        with patch.object(assignment_watch.frappe, "get_all", side_effect=[self.DUTIES, self.RUNS]):
            couriers = assignment_watch.active_couriers()

        self.assertEqual(
            {("Employee", "HR-EMP-1", "Dokki"), ("Employee", "HR-EMP-2", "Zamalek")},
            set(couriers),
        )

    def test_a_courier_with_both_appears_once(self) -> None:
        with patch.object(assignment_watch.frappe, "get_all", side_effect=[self.DUTIES, self.DUTIES]):
            self.assertEqual(1, len(assignment_watch.active_couriers()))

    def test_rows_with_no_branch_are_skipped(self) -> None:
        """An unscoped courier would produce an unscoped run-sheet query."""
        broken = [{"party_type": "Employee", "party": "HR-EMP-9", "branch": ""}]
        with patch.object(assignment_watch.frappe, "get_all", side_effect=[broken, []]):
            self.assertEqual([], assignment_watch.active_couriers())

    def test_one_failing_lookup_does_not_lose_the_other(self) -> None:
        with patch.object(
            assignment_watch.frappe, "get_all", side_effect=[RuntimeError("boom"), self.RUNS]
        ):
            couriers = assignment_watch.active_couriers()

        self.assertEqual([("Employee", "HR-EMP-2", "Zamalek")], couriers)


class TestSweep(unittest.TestCase):
    def test_one_broken_courier_does_not_stop_the_sweep(self) -> None:
        couriers = [
            ("Employee", "HR-EMP-BAD", "Dokki"),
            ("Employee", "HR-EMP-OK", "Dokki"),
        ]
        with patch.object(assignment_watch, "active_couriers", return_value=couriers), patch.object(
            assignment_watch, "_sweep_one", side_effect=[RuntimeError("boom"), "assigned"]
        ):
            summary = assignment_watch.sweep()

        self.assertEqual(2, summary["couriers"])
        self.assertEqual(1, summary["assigned"])

    def test_a_lookup_failure_returns_an_empty_summary_rather_than_raising(self) -> None:
        with patch.object(
            assignment_watch, "active_couriers", side_effect=RuntimeError("boom")
        ):
            summary = assignment_watch.sweep()

        self.assertEqual(0, summary["couriers"])

    def test_the_scheduled_entry_point_never_raises(self) -> None:
        """A job that throws every tick ends up disabled, switching notifications off."""
        with patch.object(assignment_watch, "sweep", side_effect=RuntimeError("boom")):
            self.assertEqual({"error": True}, assignment_watch.scheduled_sweep())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

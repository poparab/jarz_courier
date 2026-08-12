"""``duty_session.close_stale_duties`` — the closer that auto-open forces.

Nothing has ever closed a ``Courier Duty`` except a courier tapping End Shift, and
it already showed: CDUTY-00002 on staging sat ``Open`` from 2026-08-08 19:20 with no
positions flowing. That was survivable while every duty was opened by hand. It stops
being survivable once the OwnTracks path opens one automatically, because then a
courier who never taps anything accumulates an Open duty per phone that never closes.

Two properties matter more than the threshold itself:

* **the duty sweep runs after the run sweep**, so a courier who went dark keeps the
  ``Abandoned`` verdict on their run rather than having it overwritten with
  ``Closed`` by ``end_duty``;
* **no closing cash is invented.** A fabricated 0.00 reconciles silently and wrongly;
  a blank one is visibly missing.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.services import anomaly, courier_run, duty_session  # noqa: E402
from jarz_courier.services.anomaly import STALE_ABANDON_MINUTES  # noqa: E402


def _minutes_ago(minutes: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _duty(name: str, start_minutes_ago: float = 5.0) -> dict:
    return {
        "name": name,
        "party_type": "Employee",
        "party": "HR-EMP-00042",
        "branch": "Dokki",
        "start_time": _minutes_ago(start_minutes_ago),
    }


class StaleDutyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.open_duties: list[dict] = []
        self.last_ping = None
        self.ended: list[dict] = []

        patches = [
            patch.object(
                duty_session.frappe, "get_all", side_effect=lambda *a, **k: list(self.open_duties)
            ),
            patch.object(
                duty_session.courier_run, "last_ping_for", side_effect=lambda **k: self.last_ping
            ),
            patch.object(
                duty_session, "end_duty", side_effect=lambda **kwargs: self.ended.append(kwargs)
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class TestSilenceThreshold(StaleDutyTestCase):
    def test_a_quiet_duty_is_closed(self) -> None:
        self.open_duties = [_duty("CDUTY-1")]
        self.last_ping = _minutes_ago(STALE_ABANDON_MINUTES + 10)

        summary = duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        self.assertEqual(1, summary["closed"])
        self.assertEqual("CDUTY-1", self.ended[0]["duty"])

    def test_a_recently_active_duty_is_left_alone(self) -> None:
        self.open_duties = [_duty("CDUTY-1")]
        self.last_ping = _minutes_ago(5)

        summary = duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        self.assertEqual(0, summary["closed"])
        self.assertEqual([], self.ended)

    def test_a_duty_that_never_received_a_ping_still_ages_out(self) -> None:
        """Otherwise an auto-opened duty whose only fix failed validation is immortal."""
        self.open_duties = [_duty("CDUTY-1", start_minutes_ago=STALE_ABANDON_MINUTES + 30)]
        self.last_ping = None

        summary = duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        self.assertEqual(1, summary["closed"])

    def test_a_pingless_duty_that_just_opened_is_not_closed_immediately(self) -> None:
        self.open_duties = [_duty("CDUTY-1", start_minutes_ago=1)]
        self.last_ping = None

        summary = duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        self.assertEqual(0, summary["closed"])

    def test_the_newest_ping_wins_over_the_duty_start_time(self) -> None:
        """A courier can hold an Abandoned run and a newer Open one in one duty."""
        self.open_duties = [_duty("CDUTY-1", start_minutes_ago=STALE_ABANDON_MINUTES + 60)]
        self.last_ping = _minutes_ago(2)

        self.assertEqual(0, duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)["closed"])


class TestItNeverInventsCash(StaleDutyTestCase):
    def test_no_closing_cash_is_passed(self) -> None:
        self.open_duties = [_duty("CDUTY-1")]
        self.last_ping = _minutes_ago(STALE_ABANDON_MINUTES + 1)

        duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        self.assertIsNone(self.ended[0].get("closing_cash"))

    def test_the_note_says_why_and_that_no_cash_was_declared(self) -> None:
        """A manager reading this later must not have to guess."""
        self.open_duties = [_duty("CDUTY-1")]
        self.last_ping = _minutes_ago(STALE_ABANDON_MINUTES + 1)

        duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        note = self.ended[0]["notes"]
        self.assertIn("automatically", note.lower())
        self.assertIn("closing cash", note.lower())


class TestItNeverRaises(StaleDutyTestCase):
    """It runs inside the scheduler, where a raise aborts the rest of the sweep."""

    def test_one_bad_duty_does_not_stop_the_others(self) -> None:
        self.open_duties = [_duty("CDUTY-BAD"), _duty("CDUTY-GOOD")]
        self.last_ping = _minutes_ago(STALE_ABANDON_MINUTES + 1)

        def _end(**kwargs):
            if kwargs["duty"] == "CDUTY-BAD":
                raise RuntimeError("boom")
            self.ended.append(kwargs)

        with patch.object(duty_session, "end_duty", side_effect=_end):
            summary = duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        self.assertEqual(1, summary["closed"])
        self.assertEqual("CDUTY-GOOD", self.ended[0]["duty"])

    def test_a_failed_lookup_returns_a_zero_summary(self) -> None:
        with patch.object(duty_session.frappe, "get_all", side_effect=RuntimeError("no db")):
            summary = duty_session.close_stale_duties(minutes=STALE_ABANDON_MINUTES)

        self.assertEqual({"examined": 0, "closed": 0}, summary)


class TestTheSweepOrder(unittest.TestCase):
    """Runs first, then duties. Reversing it loses the Abandoned verdict."""

    def test_duties_are_swept_after_runs(self) -> None:
        calls: list[str] = []

        with patch.object(
            courier_run, "stale_open_runs", side_effect=lambda **k: calls.append("runs") or []
        ), patch.object(
            duty_session,
            "close_stale_duties",
            side_effect=lambda **k: calls.append("duties") or {"examined": 0, "closed": 0},
        ):
            anomaly.watch_stale_pings()

        self.assertEqual(["runs", "duties"], calls)

    def test_the_duty_counts_are_reported(self) -> None:
        with patch.object(courier_run, "stale_open_runs", return_value=[]), patch.object(
            duty_session, "close_stale_duties", return_value={"examined": 3, "closed": 2}
        ):
            summary = anomaly.watch_stale_pings()

        self.assertEqual(3, summary["duties_examined"])
        self.assertEqual(2, summary["duties_closed"])

    def test_it_uses_the_same_silence_threshold_as_the_run_watchdog(self) -> None:
        """One verdict per courier. Two thresholds would disagree about the same silence."""
        with patch.object(courier_run, "stale_open_runs", return_value=[]), patch.object(
            duty_session, "close_stale_duties", return_value={"examined": 0, "closed": 0}
        ) as close:
            anomaly.watch_stale_pings()

        self.assertEqual(STALE_ABANDON_MINUTES, close.call_args.kwargs["minutes"])


class TestLastPingFor(unittest.TestCase):
    def test_it_asks_for_the_newest_ping_only(self) -> None:
        with patch.object(
            courier_run.frappe, "get_all", return_value=[{"last_ping_on": "2026-08-12 10:00:00"}]
        ) as get_all:
            result = courier_run.last_ping_for(party_type="Employee", party="HR-EMP-1")

        self.assertEqual("2026-08-12 10:00:00", result)
        kwargs = get_all.call_args.kwargs
        self.assertEqual("last_ping_on desc", kwargs["order_by"])
        self.assertEqual(1, kwargs["limit"])
        self.assertEqual(["is", "set"], kwargs["filters"]["last_ping_on"])

    def test_no_runs_is_none_not_an_error(self) -> None:
        with patch.object(courier_run.frappe, "get_all", return_value=[]):
            self.assertIsNone(courier_run.last_ping_for(party_type="Employee", party="HR-EMP-1"))

    def test_a_blank_identity_is_none_without_querying(self) -> None:
        with patch.object(courier_run.frappe, "get_all") as get_all:
            self.assertIsNone(courier_run.last_ping_for(party_type="", party=""))
        get_all.assert_not_called()

    def test_a_failed_query_degrades_to_none(self) -> None:
        with patch.object(courier_run.frappe, "get_all", side_effect=RuntimeError("no db")):
            self.assertIsNone(courier_run.last_ping_for(party_type="Employee", party="HR-EMP-1"))


if __name__ == "__main__":
    unittest.main()

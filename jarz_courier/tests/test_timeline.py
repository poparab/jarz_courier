"""``api/timeline.get_order_timeline`` - permission, branch scope and the arithmetic.

The shape asserted here is the one the POS app's ``OrderTimeline`` model parses, so a
renamed key is a failing test rather than a blank panel in the app.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import timeline  # noqa: E402

INVOICE = "ACC-SINV-2026-00042"

ROW = {
    "name": INVOICE,
    "creation": "2026-10-14 12:00:00",
    "pos_profile": "Dokki",
    "custom_kanban_profile": "Dokki",
    "custom_courier_party_type": "Employee",
    "custom_courier_party": "HR-EMP-00042",
    "custom_accepted_on": "2026-10-14 12:02:00",
    "custom_arrived_at": "2026-10-14 12:55:00",
    "custom_delivered_at": "2026-10-14 13:05:00",
    "custom_leg_started_at": "2026-10-14 12:41:00",
}

LOG = [
    {"to_state": "In Progress", "changed_at": "2026-10-14 12:05:00"},
    {"to_state": "Out for Delivery", "changed_at": "2026-10-14 12:40:00"},
    {"to_state": "Delivered", "changed_at": "2026-10-14 13:05:10"},
]

STOPS = [
    {"event": "Left Branch", "at": "2026-10-14 12:48:00", "latitude": 30.04, "longitude": 31.21, "source": "owntracks"},
    {"event": "Arrived", "at": "2026-10-14 12:54:30", "latitude": 30.05, "longitude": 31.22, "source": "owntracks"},
    {"event": "Back At Branch", "at": "2026-10-14 13:20:00", "latitude": 30.04, "longitude": 31.21, "source": "owntracks"},
]


def _roles(*names):
    return patch.object(timeline.frappe, "get_roles", return_value=list(names))


class _Meta:
    def get_field(self, name):
        return object()


def _site(row=ROW, log=LOG, stops=STOPS, profiles=("Dokki",)):
    """Everything the endpoint reads, patched on the shared ``frappe`` stub."""

    def get_all(doctype, **kwargs):
        if doctype == "Invoice State Log":
            return [dict(r) for r in log]
        return [dict(r) for r in stops]

    def get_value(doctype, name, fields=None, as_dict=False):
        if doctype == "Sales Invoice":
            return dict(row) if row else None
        return "Mahmoud"

    return [
        patch.object(timeline.frappe, "get_meta", return_value=_Meta()),
        patch.object(timeline.frappe, "get_all", side_effect=get_all),
        patch.object(timeline.frappe.db, "get_value", side_effect=get_value),
        patch.object(timeline.pos_bridge, "get_user_pos_profiles", return_value=list(profiles)),
    ]


class _Case(unittest.TestCase):
    def run_endpoint(self, *, roles=_support.SUPERVISOR_ROLES, **site):
        patches = _site(**site)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with _roles(*roles):
            return timeline.get_order_timeline(INVOICE)


class TestPermission(_Case):
    def test_a_courier_cannot_read_the_timeline(self) -> None:
        with self.assertRaises(frappe.PermissionError):
            self.run_endpoint(roles=_support.COURIER_ROLES)

    def test_no_role_is_refused_before_any_query(self) -> None:
        with patch.object(timeline.frappe.db, "get_value") as get_value, _roles(*_support.NO_ROLES):
            with self.assertRaises(frappe.PermissionError):
                timeline.get_order_timeline(INVOICE)
        get_value.assert_not_called()

    def test_another_branchs_order_is_a_permission_error(self) -> None:
        with self.assertRaises(frappe.PermissionError):
            self.run_endpoint(profiles=("Maadi",))

    def test_a_user_with_no_branches_sees_nothing(self) -> None:
        with self.assertRaises(frappe.PermissionError):
            self.run_endpoint(profiles=())

    def test_a_missing_invoice_is_not_found(self) -> None:
        with self.assertRaises(frappe.DoesNotExistError):
            self.run_endpoint(row=None)

    def test_the_endpoint_is_whitelisted(self) -> None:
        self.assertTrue(getattr(timeline.get_order_timeline, "whitelisted", False))


class TestEvents(_Case):
    def setUp(self) -> None:
        self.result = self.run_endpoint()
        self.events = {e["key"]: e for e in self.result["events"]}

    def test_the_payload_has_the_keys_the_app_reads(self) -> None:
        self.assertEqual(INVOICE, self.result["invoice"])
        self.assertEqual(
            {"party_type": "Employee", "party": "HR-EMP-00042", "name": "Mahmoud"}, self.result["courier"]
        )
        for event in self.result["events"]:
            self.assertEqual({"key", "label", "at", "latitude", "longitude", "source"}, set(event))

    def test_every_known_step_appears_once(self) -> None:
        self.assertEqual(
            {"created", "accepted", "in_progress", "out_for_delivery", "left_branch", "leg_started",
             "arrived", "delivered", "back_at_branch"},
            set(self.events),
        )
        self.assertEqual(len(self.events), len(self.result["events"]))

    def test_events_are_sorted_by_time(self) -> None:
        stamps = [e["at"] for e in self.result["events"]]
        self.assertEqual(sorted(stamps), stamps)

    def test_timestamps_are_plain_strings(self) -> None:
        self.assertEqual("2026-10-14 12:00:00", self.events["created"]["at"])

    def test_the_invoice_stamp_wins_for_arrival_but_the_pin_comes_from_the_stop_event(self) -> None:
        self.assertEqual("2026-10-14 12:55:00", self.events["arrived"]["at"])
        self.assertEqual("invoice", self.events["arrived"]["source"])
        self.assertEqual(30.05, self.events["arrived"]["latitude"])

    def test_branch_exit_carries_the_stop_events_own_source(self) -> None:
        self.assertEqual("owntracks", self.events["left_branch"]["source"])
        self.assertEqual("state_log", self.events["out_for_delivery"]["source"])


class TestSegments(_Case):
    def setUp(self) -> None:
        self.result = self.run_endpoint()
        self.segments = {s["key"]: s for s in self.result["segments"]}

    def test_prep_is_creation_to_dispatch_against_30_minutes(self) -> None:
        prep = self.segments["prep"]
        self.assertEqual(40, prep["minutes"])
        self.assertEqual(30, prep["target_minutes"])
        self.assertTrue(prep["over"])

    def test_dispatch_wait_is_dispatch_to_branch_exit_against_5_minutes(self) -> None:
        wait = self.segments["dispatch_wait"]
        self.assertEqual(8, wait["minutes"])
        self.assertTrue(wait["over"])

    def test_travel_starts_at_the_branch_exit_and_has_no_target(self) -> None:
        travel = self.segments["travel"]
        self.assertEqual("2026-10-14 12:48:00", travel["from"])
        self.assertEqual(7, travel["minutes"])
        self.assertIsNone(travel["target_minutes"])
        self.assertFalse(travel["over"])

    def test_at_door_is_within_target(self) -> None:
        door = self.segments["at_door"]
        self.assertEqual(10, door["minutes"])
        self.assertTrue(door["over"])

    def test_return_runs_from_delivery_to_the_branch(self) -> None:
        self.assertEqual(15, self.segments["return"]["minutes"])
        self.assertIsNone(self.segments["return"]["target_minutes"])

    def test_total_is_creation_to_delivery(self) -> None:
        self.assertEqual(65, self.result["total_minutes"])

    def test_exactly_on_target_is_not_over(self) -> None:
        row = dict(ROW, custom_arrived_at="2026-10-14 13:00:00", custom_delivered_at="2026-10-14 13:05:00")
        result = self.run_endpoint(row=row, stops=[])
        door = {s["key"]: s for s in result["segments"]}["at_door"]
        self.assertEqual(5, door["minutes"])
        self.assertFalse(door["over"])


class TestSparseOrders(_Case):
    def test_travel_falls_back_to_the_leg_start_without_a_branch_exit(self) -> None:
        result = self.run_endpoint(stops=[])
        travel = {s["key"]: s for s in result["segments"]}["travel"]
        self.assertEqual("2026-10-14 12:41:00", travel["from"])
        self.assertNotIn("dispatch_wait", {s["key"] for s in result["segments"]})

    def test_an_undelivered_order_has_no_total_and_no_door_segment(self) -> None:
        row = dict(ROW, custom_delivered_at=None)
        result = self.run_endpoint(row=row, log=LOG[:2], stops=[])
        self.assertIsNone(result["total_minutes"])
        self.assertNotIn("at_door", {s["key"] for s in result["segments"]})

    def test_delivery_falls_back_to_the_state_log(self) -> None:
        row = dict(ROW, custom_delivered_at=None)
        result = self.run_endpoint(row=row, stops=[])
        delivered = {e["key"]: e for e in result["events"]}["delivered"]
        self.assertEqual("state_log", delivered["source"])

    def test_a_failed_attempt_is_reported_as_failed(self) -> None:
        row = dict(
            ROW,
            custom_delivered_at=None,
            custom_delivery_failure_reason="CUSTOMER_UNREACHABLE",
            custom_leg_ended_at="2026-10-14 13:00:00",
        )
        result = self.run_endpoint(row=row, log=LOG[:2], stops=[])
        keys = {e["key"] for e in result["events"]}
        self.assertIn("failed", keys)
        self.assertNotIn("delivered", keys)

    def test_a_negative_duration_is_dropped_not_shown(self) -> None:
        row = dict(ROW, custom_arrived_at="2026-10-14 13:30:00")
        result = self.run_endpoint(row=row, stops=[])
        self.assertNotIn("at_door", {s["key"] for s in result["segments"]})

    def test_a_missing_state_log_still_returns_a_timeline(self) -> None:
        def get_all(doctype, **kwargs):
            if doctype == "Invoice State Log":
                raise RuntimeError("no such doctype")
            return []

        patches = _site()
        patches[1] = patch.object(timeline.frappe, "get_all", side_effect=get_all)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with _roles(*_support.SUPERVISOR_ROLES):
            result = timeline.get_order_timeline(INVOICE)
        self.assertIn("created", {e["key"] for e in result["events"]})

    def test_an_unassigned_order_has_no_courier(self) -> None:
        row = dict(ROW, custom_courier_party_type=None, custom_courier_party=None)
        self.assertIsNone(self.run_endpoint(row=row)["courier"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``api/alerts`` - the ops alert feed (contract F7).

What has to hold:

* **Door and scope.** A courier gets nothing (findings are about their colleagues). A
  manager sees only findings for branches they are assigned to, and a scope that
  resolves to nothing returns nothing - never everything.
* **Shape.** The keys the POS app reads, newest first, with a readable party name.
* **Acknowledging records who and why, and nothing else.** The controller stamps the
  reviewer; the note is appended, not overwritten; no money moves.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.api import alerts  # noqa: E402
from jarz_courier.constants import ANOMALY_STATUS, ANOMALY_TYPE, SEVERITY  # noqa: E402

SHAPE_KEYS = {
    "name",
    "anomaly_type",
    "severity",
    "detected_on",
    "party_type",
    "party",
    "party_name",
    "branch",
    "invoice",
    "run",
    "details",
    "status",
}


def row(name="CANM-00001", **extra):
    base = {
        "name": name,
        "anomaly_type": ANOMALY_TYPE.DETOUR,
        "severity": SEVERITY.HIGH,
        "detected_on": "2026-10-14 10:00:00",
        "party_type": "Employee",
        "party": "HR-EMP-1",
        "branch": "Dokki",
        "sales_invoice": "ACC-SINV-1",
        "run": "CRUN-1",
        "reason": "Rode 9 km against a 3 km plan",
        "status": ANOMALY_STATUS.OPEN,
    }
    base.update(extra)
    return base


class AlertTestCase(unittest.TestCase):
    roles = ["JARZ Manager"]
    profiles = ["Dokki", "Maadi"]

    def setUp(self) -> None:
        self.get_all = MagicMock(return_value=[row()])
        self.count = MagicMock(side_effect=lambda doctype, filters=None: 3 if "severity" not in filters else 1)
        patches = [
            patch.object(alerts.frappe, "get_roles", return_value=self.roles),
            patch.object(alerts.pos_bridge, "get_user_pos_profiles", return_value=list(self.profiles)),
            patch.object(alerts.frappe, "get_all", self.get_all),
            patch.object(alerts.frappe.db, "count", self.count),
            patch.object(alerts.frappe.db, "get_value", return_value="Mahmoud"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class TestDoor(AlertTestCase):
    roles = ["Jarz Courier"]

    def test_a_courier_is_refused_everywhere(self) -> None:
        with self.assertRaises(alerts.frappe.PermissionError):
            alerts.list_alerts()
        with self.assertRaises(alerts.frappe.PermissionError):
            alerts.get_alert_counts()
        with self.assertRaises(alerts.frappe.PermissionError):
            alerts.acknowledge_alert("CANM-00001")
        self.get_all.assert_not_called()


class TestListAlerts(AlertTestCase):
    def test_shape(self) -> None:
        result = alerts.list_alerts()
        self.assertEqual({"alerts", "open_count", "high_count"}, set(result))
        self.assertEqual(SHAPE_KEYS, set(result["alerts"][0]))
        first = result["alerts"][0]
        self.assertEqual("ACC-SINV-1", first["invoice"])
        self.assertEqual("Rode 9 km against a 3 km plan", first["details"])
        self.assertEqual("Mahmoud", first["party_name"])
        self.assertEqual(3, result["open_count"])
        self.assertEqual(1, result["high_count"])

    def test_query_is_branch_scoped_newest_first_and_open_by_default(self) -> None:
        alerts.list_alerts()
        kwargs = self.get_all.call_args.kwargs
        self.assertEqual(["in", ["Dokki", "Maadi"]], kwargs["filters"]["branch"])
        self.assertEqual("Open", kwargs["filters"]["status"])
        self.assertTrue(kwargs["order_by"].startswith("detected_on desc"))

    def test_all_drops_the_status_filter(self) -> None:
        alerts.list_alerts(status="All")
        self.assertNotIn("status", self.get_all.call_args.kwargs["filters"])

    def test_an_unknown_status_is_refused(self) -> None:
        with self.assertRaises(alerts.frappe.ValidationError):
            alerts.list_alerts(status="Bogus")

    def test_a_requested_branch_narrows_the_scope(self) -> None:
        alerts.list_alerts(branch="Maadi")
        self.assertEqual(["in", ["Maadi"]], self.get_all.call_args.kwargs["filters"]["branch"])

    def test_a_branch_outside_the_scope_is_refused(self) -> None:
        with self.assertRaises(alerts.frappe.PermissionError):
            alerts.list_alerts(branch="Heliopolis")
        self.get_all.assert_not_called()

    def test_limit_is_clamped(self) -> None:
        alerts.list_alerts(limit=100000)
        self.assertEqual(alerts.MAX_LIMIT, self.get_all.call_args.kwargs["limit"])
        alerts.list_alerts(limit="junk")
        self.assertEqual(alerts.DEFAULT_LIMIT, self.get_all.call_args.kwargs["limit"])
        alerts.list_alerts(limit=0)
        self.assertEqual(1, self.get_all.call_args.kwargs["limit"])

    def test_no_branch_assignment_returns_nothing_not_everything(self) -> None:
        with patch.object(alerts.pos_bridge, "get_user_pos_profiles", return_value=[]):
            result = alerts.list_alerts()
        self.assertEqual({"alerts": [], "open_count": 0, "high_count": 0}, result)
        self.get_all.assert_not_called()

    def test_party_name_falls_back_to_the_id(self) -> None:
        with patch.object(alerts.frappe.db, "get_value", return_value=None):
            result = alerts.list_alerts()
        self.assertEqual("HR-EMP-1", result["alerts"][0]["party_name"])

    def test_a_database_failure_degrades_to_an_empty_feed(self) -> None:
        self.get_all.side_effect = RuntimeError("db gone")
        result = alerts.list_alerts()
        self.assertEqual([], result["alerts"])
        self.assertEqual(0, result["open_count"])


class TestCounts(AlertTestCase):
    def test_counts_only(self) -> None:
        self.assertEqual({"open_count": 3, "high_count": 1}, alerts.get_alert_counts())

    def test_counts_are_scoped_to_open_findings_in_the_callers_branches(self) -> None:
        alerts.get_alert_counts()
        for call in self.count.call_args_list:
            filters = call.args[1]
            self.assertEqual(["in", ["Dokki", "Maadi"]], filters["branch"])
            self.assertEqual("Open", filters["status"])

    def test_no_scope_counts_zero_without_querying(self) -> None:
        with patch.object(alerts.pos_bridge, "get_user_pos_profiles", return_value=[]):
            self.assertEqual({"open_count": 0, "high_count": 0}, alerts.get_alert_counts())
        self.count.assert_not_called()


class TestAcknowledge(AlertTestCase):
    def _doc(self, **extra):
        values = row(**extra)
        doc = MagicMock()
        doc.branch = values["branch"]
        doc.review_notes = values.get("review_notes")
        doc.get.side_effect = lambda key, default=None: values.get(key, default)
        self.values = values
        return doc

    def _ack(self, doc, **kwargs):
        with patch.object(alerts.frappe, "get_doc", return_value=doc):
            return alerts.acknowledge_alert("CANM-00001", **kwargs)

    def test_it_reviews_and_saves(self) -> None:
        doc = self._doc()
        result = self._ack(doc)
        self.assertEqual(ANOMALY_STATUS.REVIEWED, doc.status)
        doc.save.assert_called_once()
        self.assertEqual(SHAPE_KEYS, set(result))

    def test_it_can_dismiss_with_a_note(self) -> None:
        doc = self._doc()
        self._ack(doc, status="Dismissed", note="  Road works, not a detour  ")
        self.assertEqual(ANOMALY_STATUS.DISMISSED, doc.status)
        self.assertEqual("Road works, not a detour", doc.review_notes)

    def test_a_note_is_appended_not_overwritten(self) -> None:
        doc = self._doc(review_notes="First look")
        self._ack(doc, note="Second look")
        self.assertEqual("First look\nSecond look", doc.review_notes)

    def test_open_is_not_an_outcome(self) -> None:
        for bad in ("Open", "All", "", "Bogus"):
            doc = self._doc()
            if bad == "":
                # An empty status means the default, which is Reviewed.
                self._ack(doc, status=bad)
                self.assertEqual(ANOMALY_STATUS.REVIEWED, doc.status)
                continue
            with self.assertRaises(alerts.frappe.ValidationError):
                self._ack(doc, status=bad)

    def test_another_branchs_finding_is_refused(self) -> None:
        doc = self._doc(branch="Heliopolis")
        with self.assertRaises(alerts.frappe.PermissionError):
            self._ack(doc)
        doc.save.assert_not_called()

    def test_a_finding_with_no_branch_is_refused(self) -> None:
        doc = self._doc(branch=None)
        with self.assertRaises(alerts.frappe.PermissionError):
            self._ack(doc)
        doc.save.assert_not_called()

    def test_a_blank_name_is_refused(self) -> None:
        with self.assertRaises(alerts.frappe.ValidationError):
            alerts.acknowledge_alert("  ")


class TestNoLedgerEffects(unittest.TestCase):
    def test_module_has_no_money_vocabulary(self) -> None:
        import inspect

        source = inspect.getsource(alerts).lower()
        for word in ("make_gl_entries", "payment entry", "publish_realtime"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()

"""``services/run_sheet`` — the query behind the run sheet.

Three properties, each of which has burned this codebase or its neighbours before:

* **An empty scope returns nothing, never everything.** A branch list that resolves
  to empty must not become an unscoped query — that is how one branch's orders end
  up on another branch's screen.
* **Optional fields are meta-guarded.** ``custom_delivery_sequence`` and friends are
  jarz_pos lane A1 (COURIER_CONTRACTS.md §2). Selecting them unconditionally turns
  "jarz_pos is one deploy behind" into an SQL error on the courier's home screen.
* **The Woo order id is the user-facing id, and its ``0`` is not an id.** The Int
  column defaults to 0 for orders that never came from Woo; rendering it produces a
  screen full of "#0".
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.services import run_sheet  # noqa: E402


class FakeMeta:
    """Stands in for ``frappe.get_meta``, exposing only the named fields."""

    def __init__(self, present):
        self._present = set(present)

    def get_field(self, fieldname):
        return {"fieldname": fieldname} if fieldname in self._present else None


class TestScoping(unittest.TestCase):
    def test_no_branch_returns_an_empty_run_without_querying(self) -> None:
        with patch.object(run_sheet.frappe, "get_all") as get_all:
            result = run_sheet.get_run(
                party_type="Employee", party="HR-EMP-00042", branches=[]
            )

        get_all.assert_not_called()
        self.assertEqual([], result["stops"])
        self.assertEqual(0, result["totals"]["stops"])

    def test_blank_branch_strings_do_not_count_as_a_scope(self) -> None:
        with patch.object(run_sheet.frappe, "get_all") as get_all:
            result = run_sheet.get_run(
                party_type="Employee", party="HR-EMP-00042", branches=["", "   ", None]
            )

        get_all.assert_not_called()
        self.assertEqual([], result["stops"])

    def test_no_courier_party_returns_an_empty_run(self) -> None:
        with patch.object(run_sheet.frappe, "get_all") as get_all:
            result = run_sheet.get_run(party_type="", party="", branches=["Dokki"])

        get_all.assert_not_called()
        self.assertEqual([], result["stops"])

    def test_the_query_filters_on_courier_state_and_branch(self) -> None:
        with patch.object(run_sheet, "available_invoice_fields", return_value=["name"]), patch.object(
            run_sheet.frappe, "get_all", return_value=[]
        ) as get_all:
            run_sheet.get_run(
                party_type="Employee", party="HR-EMP-00042", branches=["Dokki"]
            )

        filters = get_all.call_args.kwargs["filters"]
        self.assertEqual(1, filters["docstatus"])
        self.assertEqual("Employee", filters["custom_courier_party_type"])
        self.assertEqual("HR-EMP-00042", filters["custom_courier_party"])
        self.assertEqual("Out for Delivery", filters["custom_sales_invoice_state"])
        self.assertEqual(["in", ["Dokki"]], filters["custom_kanban_profile"])


class TestOptionalFieldGuarding(unittest.TestCase):
    def test_lane_a1_fields_are_omitted_when_the_site_lacks_them(self) -> None:
        with patch.object(run_sheet.frappe, "get_meta", return_value=FakeMeta([])):
            fields = run_sheet.available_invoice_fields()

        self.assertIn("custom_courier_party", fields)  # always present
        self.assertNotIn("custom_delivery_sequence", fields)
        self.assertNotIn("custom_delivered_at", fields)

    def test_lane_a1_fields_are_selected_once_migrated(self) -> None:
        with patch.object(
            run_sheet.frappe,
            "get_meta",
            return_value=FakeMeta(["custom_delivery_sequence", "custom_delivered_at"]),
        ):
            fields = run_sheet.available_invoice_fields()

        self.assertIn("custom_delivery_sequence", fields)
        self.assertIn("custom_delivered_at", fields)

    def test_a_meta_failure_degrades_to_the_base_field_set(self) -> None:
        with patch.object(run_sheet.frappe, "get_meta", side_effect=RuntimeError("no site")):
            fields = run_sheet.available_invoice_fields()

        self.assertIn("name", fields)
        self.assertNotIn("custom_delivery_sequence", fields)

    def test_unsequenced_stops_sort_last_not_first(self) -> None:
        """Sequence 0 means "unsequenced" (contract §2), not "first stop".

        Asserted on the returned order rather than on the ``order_by`` string,
        because this sort is deliberately **not** in SQL: the rule needs a CASE
        expression and ``frappe.get_all`` validates ``order_by`` against a
        field-name grammar precisely to keep SQL out of the ORDER BY clause, so no
        phrasing of it gets through. Asserting the SQL would re-freeze a shape the
        database layer rejects.
        """
        rows = [
            {"name": "INV-UNSEQ", "custom_delivery_sequence": 0},
            {"name": "INV-THIRD", "custom_delivery_sequence": 3},
            {"name": "INV-FIRST", "custom_delivery_sequence": 1},
        ]
        with patch.object(
            run_sheet, "available_invoice_fields", return_value=["name", "custom_delivery_sequence"]
        ), patch.object(run_sheet.frappe, "get_all", return_value=rows):
            result = run_sheet.get_run(
                party_type="Employee", party="HR-EMP-00042", branches=["Dokki"]
            )

        self.assertEqual(
            ["INV-FIRST", "INV-THIRD", "INV-UNSEQ"],
            [stop["invoice"] for stop in result["stops"]],
        )

    def test_several_unsequenced_stops_keep_the_query_order(self) -> None:
        """The fallback ordering is posting_date/creation; the sort must be stable."""
        rows = [
            {"name": "INV-A", "custom_delivery_sequence": 0},
            {"name": "INV-B", "custom_delivery_sequence": 0},
            {"name": "INV-SEQ", "custom_delivery_sequence": 2},
        ]
        with patch.object(
            run_sheet, "available_invoice_fields", return_value=["name", "custom_delivery_sequence"]
        ), patch.object(run_sheet.frappe, "get_all", return_value=rows):
            result = run_sheet.get_run(
                party_type="Employee", party="HR-EMP-00042", branches=["Dokki"]
            )

        self.assertEqual(
            ["INV-SEQ", "INV-A", "INV-B"],
            [stop["invoice"] for stop in result["stops"]],
        )

    def test_a_non_numeric_sequence_is_treated_as_unsequenced(self) -> None:
        """A hand-edited or half-synced value must not raise mid-run."""
        rows = [
            {"name": "INV-JUNK", "custom_delivery_sequence": "not a number"},
            {"name": "INV-SEQ", "custom_delivery_sequence": 1},
        ]
        with patch.object(
            run_sheet, "available_invoice_fields", return_value=["name", "custom_delivery_sequence"]
        ), patch.object(run_sheet.frappe, "get_all", return_value=rows):
            result = run_sheet.get_run(
                party_type="Employee", party="HR-EMP-00042", branches=["Dokki"]
            )

        self.assertEqual(
            ["INV-SEQ", "INV-JUNK"],
            [stop["invoice"] for stop in result["stops"]],
        )

    def test_ordering_falls_back_to_posting_date_without_the_field(self) -> None:
        with patch.object(
            run_sheet, "available_invoice_fields", return_value=["name"]
        ), patch.object(run_sheet.frappe, "get_all", return_value=[]) as get_all:
            run_sheet.get_run(party_type="Employee", party="HR-EMP-00042", branches=["Dokki"])

        self.assertEqual("posting_date asc, creation asc", get_all.call_args.kwargs["order_by"])


class TestDisplayId(unittest.TestCase):
    def test_a_real_woo_number_is_the_display_id(self) -> None:
        self.assertEqual("16834", run_sheet._display_id({"name": "ACC-SINV-1", "woo_order_id": 16834}))

    def test_zero_is_not_an_id(self) -> None:
        self.assertEqual("ACC-SINV-1", run_sheet._display_id({"name": "ACC-SINV-1", "woo_order_id": 0}))

    def test_missing_woo_id_falls_back_to_the_erpnext_name(self) -> None:
        self.assertEqual("ACC-SINV-1", run_sheet._display_id({"name": "ACC-SINV-1"}))

    def test_garbage_woo_id_falls_back_rather_than_raising(self) -> None:
        self.assertEqual(
            "ACC-SINV-1", run_sheet._display_id({"name": "ACC-SINV-1", "woo_order_id": "n/a"})
        )


class TestStopSummary(unittest.TestCase):
    def test_amount_to_collect_is_the_outstanding_balance(self) -> None:
        row = {
            "name": "ACC-SINV-1",
            "customer": "CUST-1",
            "customer_name": "Nour",
            "grand_total": 500.0,
            "outstanding_amount": 450.0,
            "custom_kanban_profile": "Dokki",
            "custom_sales_invoice_state": "Out for Delivery",
        }
        stop = run_sheet._stop_summary(row, {})
        self.assertEqual(450.0, stop["amount_to_collect"])
        self.assertEqual(500.0, stop["grand_total"])
        self.assertEqual("Dokki", stop["branch"])

    def test_address_geo_is_read_through_untouched(self) -> None:
        row = {"name": "ACC-SINV-1", "shipping_address_name": "ADDR-1"}
        address_map = {
            "ADDR-1": {
                "address_line1": "12 Nile St",
                "address_line2": "Location: https://maps.app.goo.gl/abc",
                "city": "Dokki",
                "latitude": 30.045123,
                "longitude": 31.208456,
                "geo_source": "customer_pin",
                "geo_confidence": 30,
            }
        }
        stop = run_sheet._stop_summary(row, address_map)
        self.assertEqual(30.045123, stop["address"]["latitude"])
        self.assertEqual("customer_pin", stop["address"]["geo_source"])
        # address_line2 carries the maps link and is never rewritten (spec §3.4).
        self.assertEqual("Location: https://maps.app.goo.gl/abc", stop["address"]["line2"])

    def test_totals_sum_what_the_courier_has_to_collect(self) -> None:
        stops = [
            {"amount_to_collect": 100.0, "attempt_no": 0},
            {"amount_to_collect": 250.5, "attempt_no": 2},
        ]
        totals = run_sheet._totals(stops)
        self.assertEqual(2, totals["stops"])
        self.assertEqual(350.5, totals["to_collect"])
        self.assertEqual(1, totals["failed_attempts"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``api/statement`` — the courier statement and the cash hand-over.

The single most important assertion in this app lives here: **confirming a deposit
calls jarz_pos and records what it returns; it never posts anything itself.**
COURIER_APP_SPEC.md §2.1 — the GL audit suite covers jarz_pos only, so money logic
in this app would be untested money logic and a second source of truth for a
courier's balance.

Also pinned:

* a courier cannot confirm their own declaration (that is the whole control);
* ``declare_deposit`` is idempotent on ``request_id``;
* the confirmation is stamped only *after* jarz_pos posts — a settlement failure
  must leave the declaration Pending and retryable, never Confirmed with nothing
  behind it;
* the manager queue is never unscoped;
* the ledger summary excludes partner rows and uses ``amount - shipping_amount``,
  matching ``delivery_handling._summarize_courier_transactions`` so the courier's
  phone and the manager's Desk cannot disagree.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import statement  # noqa: E402
from jarz_courier.services import deposits, ledger_read  # noqa: E402

DECLARATION = {
    "name": "CDEP-00001",
    "party_type": "Employee",
    "party": "HR-EMP-00042",
    "branch": "Dokki",
    "status": "Pending",
    "amount": 4200.0,
    "method": "Cash Handover",
    "journal_entry": None,
}


def _roles(*names: str):
    return patch.object(statement.frappe, "get_roles", return_value=list(names))


def _identity():
    return patch.object(
        statement.courier_onboarding,
        "ensure_courier_setup",
        return_value=dict(_support.COURIER_IDENTITY),
    )


class TestPermissionGates(unittest.TestCase):
    def test_statement_requires_a_courier_or_supervisor_role(self) -> None:
        with _roles(*_support.NO_ROLES):
            with self.assertRaises(frappe.PermissionError):
                statement.get_statement()
            with self.assertRaises(frappe.PermissionError):
                statement.declare_deposit(100, "Cash Handover")

    def test_a_courier_cannot_confirm_their_own_deposit(self) -> None:
        """The declaration exists to stop exactly this."""
        with _roles(*_support.COURIER_ROLES), patch.object(deposits, "confirm") as confirm:
            with self.assertRaises(frappe.PermissionError):
                statement.confirm_deposit("CDEP-00001")
        confirm.assert_not_called()

    def test_a_courier_cannot_reject_a_deposit(self) -> None:
        with _roles(*_support.COURIER_ROLES):
            with self.assertRaises(frappe.PermissionError):
                statement.reject_deposit("CDEP-00001", "wrong amount")

    def test_a_courier_cannot_read_the_manager_queue(self) -> None:
        with _roles(*_support.COURIER_ROLES):
            with self.assertRaises(frappe.PermissionError):
                statement.list_pending_deposits()


class TestGetStatement(unittest.TestCase):
    def test_returns_balance_fees_and_settlement_history(self) -> None:
        built = {
            "unsettled_balance": 4200.0,
            "collected_today": 1500.0,
            "fees": 240.0,
            "deductions": 0.0,
            "settlements": [{"journal_entry": "ACC-JV-2026-00007", "net_to_branch": 3000.0}],
        }
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            statement.ledger_read, "build_statement", return_value=dict(built)
        ), patch.object(statement.deposits, "list_declarations", return_value=[DECLARATION]):
            result = statement.get_statement()

        self.assertTrue(result["success"])
        self.assertEqual(4200.0, result["statement"]["unsettled_balance"])
        self.assertEqual(240.0, result["statement"]["fees"])
        self.assertEqual(
            "ACC-JV-2026-00007", result["statement"]["settlements"][0]["journal_entry"]
        )
        self.assertEqual(1, len(result["statement"]["declarations"]))

    def test_scoped_to_the_signed_in_courier(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            statement.ledger_read, "build_statement", return_value={}
        ) as build, patch.object(statement.deposits, "list_declarations", return_value=[]):
            statement.get_statement()

        kwargs = build.call_args.kwargs
        self.assertEqual("Employee", kwargs["party_type"])
        self.assertEqual("HR-EMP-00042", kwargs["party"])


class TestDeclareDeposit(unittest.TestCase):
    def test_records_a_claim_scoped_to_the_couriers_branch(self) -> None:
        with _roles(*_support.COURIER_ROLES), _identity(), patch.object(
            statement.duty_session, "get_open_duty", return_value={"name": "CDUTY-00001"}
        ), patch.object(
            statement.deposits, "declare", return_value={"declaration": DECLARATION, "created": True}
        ) as declare:
            result = statement.declare_deposit(4200, "cash", reference="handed to Ahmed", request_id="d1")

        self.assertTrue(result["success"])
        kwargs = declare.call_args.kwargs
        self.assertEqual("Dokki", kwargs["branch"])
        self.assertEqual("HR-EMP-00042", kwargs["party"])
        self.assertEqual("CDUTY-00001", kwargs["duty"])
        self.assertEqual("d1", kwargs["request_id"])

    def test_replayed_declaration_is_idempotent(self) -> None:
        with patch.object(deposits, "find_by_request_id", return_value=dict(DECLARATION)):
            result = deposits.declare(
                party_type="Employee",
                party="HR-EMP-00042",
                branch="Dokki",
                amount=4200,
                method="Cash Handover",
                request_id="d1",
            )

        self.assertFalse(result["created"])
        self.assertEqual("CDEP-00001", result["declaration"]["name"])

    def test_method_aliases_are_normalised(self) -> None:
        self.assertEqual("Cash Handover", deposits.normalize_method("cash"))
        self.assertEqual("Cash Handover", deposits.normalize_method("Cash Handover"))
        self.assertEqual("InstaPay", deposits.normalize_method("instapay"))
        with self.assertRaises(frappe.ValidationError):
            deposits.normalize_method("bitcoin")


class TestConfirmDepositDelegatesTheMoney(unittest.TestCase):
    """jarz_pos posts; this app records the reference."""

    def test_settlement_is_delegated_and_the_reference_recorded(self) -> None:
        posted = {
            "journal_entry": "ACC-JV-2026-00009",
            "net_balance": 4200.0,
            "order_amount": 4600.0,
            "shipping_amount": 400.0,
        }
        saved = {}

        class FakeDoc:
            status = "Pending"
            party_type = "Employee"
            party = "HR-EMP-00042"
            branch = "Dokki"
            amount = 4200.0
            method = "Cash Handover"
            reference = None
            name = "CDEP-00001"
            journal_entry = None
            confirmed_by = None
            confirmed_on = None

            def get(self, key, default=None):
                return getattr(self, key, default)

            def save(self, **kwargs):
                saved.update(
                    {
                        "status": self.status,
                        "journal_entry": self.journal_entry,
                        "confirmed_by": self.confirmed_by,
                    }
                )

        with patch.object(deposits.frappe, "get_doc", return_value=FakeDoc()), patch.object(
            deposits.pos_bridge, "settle_courier_deposit", return_value=posted
        ) as settle, patch.object(deposits.pos_bridge, "publish_to_branches"):
            result = deposits.confirm(name="CDEP-00001", confirmed_by="manager@example.com")

        # The money call went to jarz_pos, with the courier and branch attached.
        kwargs = settle.call_args.kwargs
        self.assertEqual("Employee", kwargs["party_type"])
        self.assertEqual("Dokki", kwargs["pos_profile"])
        self.assertEqual("CDEP-00001", kwargs["declaration"])

        # Only the reference is stored on our side.
        self.assertEqual("Confirmed", saved["status"])
        self.assertEqual("ACC-JV-2026-00009", saved["journal_entry"])
        self.assertEqual("manager@example.com", saved["confirmed_by"])
        self.assertTrue(result["amount_matches"])

    def test_a_settlement_failure_leaves_the_declaration_pending(self) -> None:
        """Stamping Confirmed before the posting would strand cash the books never saw."""
        saved = {}

        class FakeDoc:
            status = "Pending"
            party_type = "Employee"
            party = "HR-EMP-00042"
            branch = "Dokki"
            amount = 4200.0
            method = "Cash Handover"
            reference = None
            name = "CDEP-00001"
            journal_entry = None

            def get(self, key, default=None):
                return getattr(self, key, default)

            def save(self, **kwargs):
                saved["called"] = True

        with patch.object(deposits.frappe, "get_doc", return_value=FakeDoc()), patch.object(
            deposits.pos_bridge,
            "settle_courier_deposit",
            side_effect=RuntimeError("no unsettled courier transactions"),
        ):
            with self.assertRaises(RuntimeError):
                deposits.confirm(name="CDEP-00001")

        self.assertNotIn("called", saved)

    def test_a_second_confirm_is_a_no_op(self) -> None:
        class FakeDoc:
            status = "Confirmed"
            name = "CDEP-00001"

            def get(self, key, default=None):
                return getattr(self, key, default)

        with patch.object(deposits.frappe, "get_doc", return_value=FakeDoc()), patch.object(
            deposits.pos_bridge, "settle_courier_deposit"
        ) as settle:
            result = deposits.confirm(name="CDEP-00001")

        settle.assert_not_called()
        self.assertFalse(result["changed"])

    def test_a_partial_handover_reports_the_mismatch_instead_of_hiding_it(self) -> None:
        posted = {"journal_entry": "ACC-JV-2026-00010", "net_balance": 6000.0}

        class FakeDoc:
            status = "Pending"
            party_type = "Employee"
            party = "HR-EMP-00042"
            branch = "Dokki"
            amount = 4200.0
            method = "Cash Handover"
            reference = None
            name = "CDEP-00001"
            journal_entry = None
            confirmed_by = None
            confirmed_on = None

            def get(self, key, default=None):
                return getattr(self, key, default)

            def save(self, **kwargs):
                return None

        with patch.object(deposits.frappe, "get_doc", return_value=FakeDoc()), patch.object(
            deposits.pos_bridge, "settle_courier_deposit", return_value=posted
        ), patch.object(deposits.pos_bridge, "publish_to_branches"):
            result = deposits.confirm(name="CDEP-00001")

        self.assertFalse(result["amount_matches"])
        self.assertEqual(4200.0, result["declared_amount"])
        self.assertEqual(6000.0, result["settled_net"])


class TestManagerQueueScoping(unittest.TestCase):
    def test_a_branch_the_manager_is_not_assigned_to_is_refused(self) -> None:
        with _roles(*_support.SUPERVISOR_ROLES), patch.object(
            statement.pos_bridge, "get_user_pos_profiles", return_value=["Dokki"]
        ):
            with self.assertRaises(frappe.PermissionError):
                statement.list_pending_deposits("Zamalek")

    def test_the_queue_is_scoped_to_the_managers_branches(self) -> None:
        with _roles(*_support.SUPERVISOR_ROLES), patch.object(
            statement.pos_bridge, "get_user_pos_profiles", return_value=["Dokki", "Zamalek"]
        ), patch.object(statement.deposits, "list_declarations", return_value=[]) as listing:
            result = statement.list_pending_deposits()

        self.assertTrue(result["success"])
        self.assertEqual(["Dokki", "Zamalek"], listing.call_args.kwargs["branches"])
        self.assertEqual("Pending", listing.call_args.kwargs["status"])

    def test_an_empty_scope_returns_nothing_rather_than_everything(self) -> None:
        with patch.object(deposits.frappe, "get_all") as get_all:
            result = deposits.list_declarations(branches=[])

        self.assertEqual([], result)
        get_all.assert_not_called()

    def test_confirming_another_branchs_declaration_is_refused(self) -> None:
        row = {"name": "CDEP-00002", "branch": "Zamalek", "status": "Pending"}
        with _roles(*_support.SUPERVISOR_ROLES), patch.object(
            statement.frappe.db, "get_value", return_value=row
        ), patch.object(statement.pos_bridge, "get_user_pos_profiles", return_value=["Dokki"]):
            with self.assertRaises(frappe.PermissionError):
                statement.confirm_deposit("CDEP-00002")


class TestLedgerSummaryArithmetic(unittest.TestCase):
    """Read-only maths, aligned with delivery_handling._summarize_courier_transactions."""

    def test_net_is_collected_minus_shipping(self) -> None:
        rows = [
            {"amount": 1000.0, "shipping_amount": 50.0},
            {"amount": 500.0, "shipping_amount": 30.0},
        ]
        summary = ledger_read.summarize(rows)
        self.assertEqual(1500.0, summary["order_amount"])
        self.assertEqual(80.0, summary["shipping_amount"])
        self.assertEqual(1420.0, summary["net_to_branch"])

    def test_partner_rows_are_excluded(self) -> None:
        """A 3PL's orders settle against the partner's Payable, not a courier."""
        rows = [
            {"amount": 1000.0, "shipping_amount": 50.0},
            {"amount": 9999.0, "shipping_amount": 500.0, "is_partner_order": 1},
        ]
        summary = ledger_read.summarize(rows)
        self.assertEqual(1000.0, summary["order_amount"])
        self.assertEqual(950.0, summary["net_to_branch"])

    def test_historical_null_partner_flag_counts_as_a_normal_row(self) -> None:
        rows = [{"amount": 100.0, "shipping_amount": 10.0, "is_partner_order": None}]
        self.assertEqual(90.0, ledger_read.summarize(rows)["net_to_branch"])

    def test_empty_ledger_is_zero_not_an_error(self) -> None:
        summary = ledger_read.summarize([])
        self.assertEqual(0, summary["count"])
        self.assertEqual(0.0, summary["net_to_branch"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

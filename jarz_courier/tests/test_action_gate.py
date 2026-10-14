"""F6 - the courier action gate, the delivery-code pass-through and their wiring.

Two layers, kept apart on purpose:

* ``services/action_gate`` holds the rules. Each refusal is a distinct
  ``frappe.ValidationError`` subclass so the app can branch on ``exc_type``.
* ``api/run`` decides *whether* the gate runs (the setting) and must let a structured
  refusal through as a real error instead of flattening it into
  ``{"success": False}`` - a 200 hides "go closer" from the app.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import run  # noqa: E402
from jarz_courier.services import action_gate as gate  # noqa: E402
from jarz_courier.services import pos_bridge  # noqa: E402

INVOICE = "ACC-SINV-2026-00042"
NOW = 1_800_000_000.0
PIN = (30.0444, 31.2357)
#: ~55 m north of the pin: well inside 300 m.
NEAR = (30.0449, 31.2357)
#: ~2.2 km north of the pin.
FAR = (30.0644, 31.2357)

MY_ROW = {
    "name": INVOICE,
    "custom_kanban_profile": "Dokki",
    "pos_profile": "Dokki",
    "custom_courier_party_type": "Employee",
    "custom_courier_party": "HR-EMP-00042",
}


def _fix(lat_lng, age=20.0):
    return {"lat": lat_lng[0], "lng": lat_lng[1], "epoch": NOW - age}


class _GateCase(unittest.TestCase):
    """Patches every collaborator the gate reaches; tests override what they care about."""

    def setUp(self) -> None:
        self.flag = MagicMock(return_value={"created": True})
        self.position = {"lat": NEAR[0], "lng": NEAR[1], "epoch": NOW - 20}
        self.pin = {"latitude": PIN[0], "longitude": PIN[1]}
        self.settings = {}
        for p in (
            patch.object(gate.frappe, "get_roles", return_value=_support.COURIER_ROLES),
            patch.object(gate.anomaly, "flag", self.flag),
            patch.object(gate.location_cache, "read_position", side_effect=lambda *a, **k: self.position),
            patch.object(
                gate.frappe.db,
                "get_value",
                return_value={"shipping_address_name": "ADDR-1", "customer_address": None},
            ),
            patch.object(
                gate.run_sheet, "_load_addresses", side_effect=lambda rows: {"ADDR-1": self.pin} if self.pin else {}
            ),
            patch.object(
                gate.pos_bridge,
                "get_pos_setting",
                side_effect=lambda field, default=None: self.settings.get(field, default),
            ),
        ):
            p.start()
            self.addCleanup(p.stop)

    def check(self, *, lat_lng=NEAR, is_mocked=False, action=gate.ACTION_ARRIVED, **kw):
        return gate.assert_action_allowed(
            INVOICE,
            action,
            lat_lng[0] if lat_lng else None,
            lat_lng[1] if lat_lng else None,
            10.0,
            is_mocked,
            identity=dict(_support.COURIER_IDENTITY),
            now_epoch=NOW,
            **kw,
        )

    def flagged(self):
        return self.flag.call_args.kwargs


class TestSettings(_GateCase):
    def test_it_is_off_unless_the_setting_is_on(self) -> None:
        self.assertFalse(gate.is_enabled())
        self.settings["enforce_courier_action_gate"] = 1
        self.assertTrue(gate.is_enabled())

    def test_defaults_fill_an_unset_radius_and_age(self) -> None:
        self.assertEqual(
            {"enabled": False, "radius_m": 300, "max_fix_age_sec": 300}, gate.get_settings()
        )

    def test_configured_values_are_returned(self) -> None:
        self.settings.update(
            enforce_courier_action_gate=1, courier_action_radius_m="150", courier_action_max_fix_age_sec=90
        )
        self.assertEqual({"enabled": True, "radius_m": 150, "max_fix_age_sec": 90}, gate.get_settings())

    def test_a_zero_or_junk_value_falls_back_to_the_default(self) -> None:
        self.settings.update(courier_action_radius_m=0, courier_action_max_fix_age_sec="soon")
        cfg = gate.get_settings()
        self.assertEqual(300, cfg["radius_m"])
        self.assertEqual(300, cfg["max_fix_age_sec"])


class TestAllowed(_GateCase):
    def test_a_courier_near_the_pin_with_a_fresh_fix_passes(self) -> None:
        report = self.check()
        self.assertTrue(report["checked"])
        self.assertLess(report["distance_m"], 100)
        self.assertEqual(20, report["fix_age_sec"])
        self.flag.assert_not_called()

    def test_a_supervisor_is_never_gated(self) -> None:
        self.position = None
        with patch.object(gate.frappe, "get_roles", return_value=_support.SUPERVISOR_ROLES):
            report = self.check()
        self.assertEqual({"checked": False, "reason": "supervisor"}, report)

    def test_a_stop_without_a_pin_skips_only_the_distance_test(self) -> None:
        # 8 of 17,247 addresses carry a pin, so the other checks must still hold.
        self.pin = None
        report = self.check()
        self.assertEqual({"checked": True, "reason": "no pin", "fix_age_sec": 20}, report)
        self.flag.assert_not_called()

    def test_a_stop_without_a_pin_still_needs_live_tracking(self) -> None:
        self.pin = None
        self.position = None
        with self.assertRaises(gate.CourierTrackingStale):
            self.check()

    def test_a_stop_without_a_pin_still_refuses_a_mocked_location(self) -> None:
        self.pin = None
        with self.assertRaises(gate.CourierMockLocation):
            self.check(is_mocked=True)

    def test_a_stop_without_a_pin_still_refuses_a_mismatch(self) -> None:
        self.pin = None
        with self.assertRaises(gate.CourierLocationMismatch):
            self.check(lat_lng=FAR)

    def test_a_client_fix_close_to_the_server_fix_is_not_a_mismatch(self) -> None:
        self.assertTrue(self.check(lat_lng=(NEAR[0] + 0.0005, NEAR[1]))["checked"])


class TestRefusals(_GateCase):
    def test_a_new_finding_is_committed_before_the_refusal_rolls_back(self) -> None:
        """Frappe rolls the request back on the raise; without the commit the
        finding (and its after-commit push to ops) would be lost."""
        with patch.object(gate.frappe.db, "commit") as commit:
            with self.assertRaises(gate.CourierMockLocation):
                self.check(is_mocked=True)
        commit.assert_called_once_with()

    def test_a_deduplicated_retry_commits_nothing(self) -> None:
        self.flag.return_value = {"created": False, "reason": "already flagged"}
        with patch.object(gate.frappe.db, "commit") as commit:
            with self.assertRaises(gate.CourierMockLocation):
                self.check(is_mocked=True)
        commit.assert_not_called()

    def test_a_mocked_location_is_refused_as_medium(self) -> None:
        with self.assertRaises(gate.CourierMockLocation):
            self.check(is_mocked=True)
        self.assertEqual("Medium", self.flagged()["severity"])
        self.assertEqual("Action Refused", self.flagged()["anomaly_type"])

    def test_a_client_fix_far_from_the_server_fix_is_a_mismatch(self) -> None:
        # The phone claims to be 2 km from where the server last saw it, 20 s ago.
        with self.assertRaises(gate.CourierLocationMismatch):
            self.check(lat_lng=FAR)
        self.assertEqual("Medium", self.flagged()["severity"])

    def test_the_mismatch_beats_too_far(self) -> None:
        self.position = _fix(FAR)
        with self.assertRaises(gate.CourierLocationMismatch):
            self.check(lat_lng=PIN)
        self.assertEqual("Medium", self.flagged()["severity"])
        self.assertGreater(self.flagged()["observed"], 300)

    def test_no_server_fix_is_stale(self) -> None:
        self.position = None
        with self.assertRaises(gate.CourierTrackingStale):
            self.check(lat_lng=None)
        self.assertEqual("Low", self.flagged()["severity"])

    def test_an_old_server_fix_is_stale(self) -> None:
        self.position = _fix(NEAR, age=900)
        with self.assertRaises(gate.CourierTrackingStale):
            self.check()

    def test_a_stale_fix_is_not_compared_with_the_client(self) -> None:
        # Stale is the honest answer here: there is nothing recent to disagree with.
        self.position = _fix(FAR, age=900)
        with self.assertRaises(gate.CourierTrackingStale):
            self.check(lat_lng=PIN)

    def test_a_server_fix_beyond_the_radius_is_too_far(self) -> None:
        self.position = _fix(FAR)
        with self.assertRaises(gate.CourierTooFar) as ctx:
            self.check(lat_lng=FAR)
        self.assertIn("m", str(ctx.exception))
        self.assertEqual("Low", self.flagged()["severity"])
        self.assertEqual(300, self.flagged()["threshold"])
        self.assertEqual("m", self.flagged()["unit"])

    def test_the_radius_setting_is_honoured(self) -> None:
        self.settings["courier_action_radius_m"] = 20
        with self.assertRaises(gate.CourierTooFar):
            self.check()

    def test_every_refusal_is_a_validation_error(self) -> None:
        for cls in gate.REFUSAL_TYPES:
            self.assertTrue(issubclass(cls, frappe.ValidationError))

    def test_the_finding_is_deduped_per_order_action_and_reason(self) -> None:
        self.position = _fix(FAR)
        with self.assertRaises(gate.CourierTooFar):
            self.check(lat_lng=FAR, action=gate.ACTION_DELIVERED)
        key = self.flagged()["dedupe_key"]
        self.assertTrue(key.startswith(f"action-refused:{INVOICE}:delivered:far:"))

    def test_a_failing_finding_does_not_replace_the_refusal(self) -> None:
        self.flag.side_effect = RuntimeError("db down")
        self.position = _fix(FAR)
        with self.assertRaises(gate.CourierTooFar):
            self.check(lat_lng=FAR)

    def test_the_finding_carries_the_servers_fix_as_the_location(self) -> None:
        self.position = _fix(FAR)
        with self.assertRaises(gate.CourierTooFar):
            self.check(lat_lng=FAR)
        self.assertEqual(FAR[0], self.flagged()["latitude"])
        self.assertEqual("HR-EMP-00042", self.flagged()["party"])


class TestDeliveryCodeFinding(_GateCase):
    def test_a_wrong_code_is_medium(self) -> None:
        gate.flag_delivery_code_failure(INVOICE, locked=False, message="Wrong code", row=MY_ROW)
        self.assertEqual("Delivery Code Failed", self.flagged()["anomaly_type"])
        self.assertEqual("Medium", self.flagged()["severity"])
        self.assertEqual(INVOICE, self.flagged()["sales_invoice"])
        self.assertEqual("Dokki", self.flagged()["branch"])

    def test_a_locked_order_is_high(self) -> None:
        gate.flag_delivery_code_failure(INVOICE, locked=True, row=MY_ROW)
        self.assertEqual("High", self.flagged()["severity"])
        self.assertIn(":locked:", self.flagged()["dedupe_key"])

    def test_an_unassigned_order_files_nothing(self) -> None:
        gate.flag_delivery_code_failure(INVOICE, locked=False, row={"name": INVOICE})
        self.flag.assert_not_called()

    def test_it_never_raises(self) -> None:
        self.flag.side_effect = RuntimeError("db down")
        gate.flag_delivery_code_failure(INVOICE, locked=False, row=MY_ROW)


# ─────────────────────────────────────────────────────────────────────────────
# api/run wiring
# ─────────────────────────────────────────────────────────────────────────────

class DeliveryCodeWrong(Exception):
    pass


class DeliveryCodeLocked(Exception):
    pass


class _RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.enabled = True
        self.gate = MagicMock()
        self.pos = MagicMock(return_value={"success": True})
        self.failure = MagicMock()
        for p in (
            patch.object(run.frappe, "get_roles", return_value=_support.COURIER_ROLES),
            patch.object(run.courier_onboarding, "ensure_courier_setup", return_value=dict(_support.COURIER_IDENTITY)),
            patch.object(run.courier_onboarding, "resolve_active_branch", return_value=dict(_support.COURIER_IDENTITY)),
            patch.object(run, "_assert_stop_access", return_value=MY_ROW),
            patch.object(run.action_gate, "is_enabled", side_effect=lambda: self.enabled),
            patch.object(run.action_gate, "assert_action_allowed", self.gate),
            patch.object(run.action_gate, "flag_delivery_code_failure", self.failure),
            patch.object(run.pos_bridge, "mark_invoice_arrived", self.pos),
            patch.object(run.pos_bridge, "mark_invoice_delivered", self.pos),
            patch.object(run.pos_bridge, "mark_invoice_failed", self.pos),
        ):
            p.start()
            self.addCleanup(p.stop)


class TestGateWiring(_RunCase):
    def test_each_action_asks_the_gate_when_it_is_on(self) -> None:
        run.mark_arrived(INVOICE, latitude=1.0, longitude=2.0, accuracy_m=5, is_mocked="false")
        run.mark_delivered(INVOICE, latitude=1.0, longitude=2.0)
        run.mark_failed(INVOICE, "CUSTOMER_UNREACHABLE", latitude=1.0, longitude=2.0)
        self.assertEqual(
            ["arrived", "delivered", "failed"], [c.args[1] for c in self.gate.call_args_list]
        )
        self.assertEqual(1.0, self.gate.call_args_list[0].args[2])

    def test_the_gate_is_skipped_when_the_setting_is_off(self) -> None:
        self.enabled = False
        run.mark_arrived(INVOICE)
        run.mark_delivered(INVOICE)
        run.mark_failed(INVOICE, "CUSTOMER_UNREACHABLE")
        self.gate.assert_not_called()

    def test_the_mock_flag_reaches_the_gate_as_a_bool(self) -> None:
        run.mark_arrived(INVOICE, is_mocked="true")
        self.assertIs(True, self.gate.call_args.args[5])

    def test_a_refusal_surfaces_as_its_own_exception_not_an_envelope(self) -> None:
        for cls in gate.REFUSAL_TYPES:
            self.gate.side_effect = cls("nope")
            for call in (
                lambda: run.mark_arrived(INVOICE),
                lambda: run.mark_delivered(INVOICE),
                lambda: run.mark_failed(INVOICE, "CUSTOMER_UNREACHABLE"),
            ):
                with self.assertRaises(cls):
                    call()

    def test_a_refused_action_never_reaches_jarz_pos(self) -> None:
        self.gate.side_effect = gate.CourierTooFar("far")
        with self.assertRaises(gate.CourierTooFar):
            run.mark_delivered(INVOICE)
        self.pos.assert_not_called()

    def test_an_ordinary_failure_is_still_an_envelope(self) -> None:
        self.pos.side_effect = RuntimeError("boom")
        self.assertEqual({"success": False, "error": "boom"}, run.mark_arrived(INVOICE))

    def test_the_run_payload_carries_the_gate_settings(self) -> None:
        with patch.object(
            run.run_sheet, "get_run", return_value={"stops": [], "totals": {}, "branches": ["Dokki"]}
        ), patch.object(
            run.action_gate, "get_settings", return_value={"enabled": True, "radius_m": 300, "max_fix_age_sec": 300}
        ):
            result = run.get_my_run()
        self.assertEqual(
            {"enabled": True, "radius_m": 300, "max_fix_age_sec": 300}, result["action_gate"]
        )


class TestDeliveryCode(_RunCase):
    def test_the_code_is_forwarded_to_the_bridge(self) -> None:
        run.mark_delivered(INVOICE, delivery_code="4821")
        self.assertEqual("4821", self.pos.call_args.kwargs["delivery_code"])

    def test_a_wrong_code_is_flagged_and_still_raised(self) -> None:
        self.pos.side_effect = DeliveryCodeWrong("Wrong code")
        with self.assertRaises(DeliveryCodeWrong):
            run.mark_delivered(INVOICE, delivery_code="0000")
        self.failure.assert_called_once()
        self.assertIs(False, self.failure.call_args.kwargs["locked"])
        self.assertEqual(MY_ROW, self.failure.call_args.kwargs["row"])

    def test_a_locked_order_is_flagged_as_locked(self) -> None:
        self.pos.side_effect = DeliveryCodeLocked("Locked")
        with self.assertRaises(DeliveryCodeLocked):
            run.mark_delivered(INVOICE, delivery_code="0000")
        self.assertIs(True, self.failure.call_args.kwargs["locked"])

    def test_a_missing_code_is_not_a_finding(self) -> None:
        class DeliveryCodeRequired(Exception):
            pass

        self.pos.side_effect = DeliveryCodeRequired("Ask the customer")
        with self.assertRaises(DeliveryCodeRequired):
            run.mark_delivered(INVOICE)
        self.failure.assert_not_called()


class TestBridgeCompatibility(unittest.TestCase):
    """An older jarz_pos must keep working: it has no delivery-code parameters."""

    def _deliver(self, accepted):
        call = MagicMock(return_value={"success": True})
        with patch.object(pos_bridge, "_accepted_kwargs", return_value=accepted), patch.object(
            pos_bridge, "_call", call
        ):
            pos_bridge.mark_invoice_delivered(INVOICE, delivery_code="4821")
        return call.call_args.kwargs

    def test_an_older_jarz_pos_is_not_sent_the_new_arguments(self) -> None:
        kwargs = self._deliver(set())
        self.assertNotIn("delivery_code", kwargs)
        self.assertNotIn("enforce_code", kwargs)

    def test_a_current_jarz_pos_gets_the_code_and_enforcement(self) -> None:
        kwargs = self._deliver({"delivery_code", "enforce_code"})
        self.assertEqual("4821", kwargs["delivery_code"])
        self.assertIs(True, kwargs["enforce_code"])


class TestStopCarriesTheFlag(unittest.TestCase):
    def test_delivery_code_required_follows_the_setting(self) -> None:
        from jarz_courier.services import run_sheet

        for value, expected in ((1, True), (0, False), (None, False)):
            with patch.object(run_sheet.pos_bridge, "get_pos_setting", return_value=value or 0):
                self.assertIs(expected, run_sheet.delivery_code_required())

    def test_the_stop_summary_includes_it(self) -> None:
        from jarz_courier.services import run_sheet

        stop = run_sheet._stop_summary({"name": INVOICE}, {}, code_required=True)
        self.assertIs(True, stop["delivery_code_required"])
        self.assertIs(False, run_sheet._stop_summary({"name": INVOICE}, {})["delivery_code_required"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

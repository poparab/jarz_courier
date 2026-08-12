"""``services/consensus_pin`` — B5, and the §3 writer boundary.

Two separate things are under test.

**The boundary.** Contract §3 names exactly two authorised writers of the six Address
geo fields, and ``jarz_courier`` is not one of them. There is a structural test below
asserting the module contains **no mutating call of any kind** — it can only reach an
Address through ``pos_bridge.set_address_pin``, which owns the never-downgrade ladder,
the accuracy invariant, the manual-override role gate and the "never write a
WooCommerce trigger field" guard. Writing the columns directly would work on the first
run and quietly break the ladder for everybody.

**The independence rule.** "Three fixes within 40 m" is trivially forgeable by one
courier standing in the same wrong place three times, so agreement alone cannot be the
bar. The tests pin the rule: distinct invoices, and more of them when they all came from
one person.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.constants import (  # noqa: E402
    GEO_SOURCE_COURIER_VERIFIED,
    GEO_SOURCE_COURIER_WEB,
    GEO_SOURCE_CUSTOMER_PIN,
)
from jarz_courier.services import consensus_pin  # noqa: E402


def door_fix(invoice, party="HR-EMP-1", lat=30.044420, lng=31.235712, accuracy=8.0):
    return {
        "proof": f"DPRF-{invoice}",
        "invoice": invoice,
        "party": party,
        "party_type": "Employee",
        "lat": lat,
        "lng": lng,
        "accuracy": accuracy,
    }


class TestTheLadderLabelMatchesTheContract(unittest.TestCase):
    def test_the_source_label_is_the_frozen_section_4_string(self) -> None:
        """COURIER_CONTRACTS.md §4 spells it exactly this way.

        This app carries no copy of CONFIDENCE_RANK — every rank question goes through
        ``pos_bridge.confidence_rank`` — so this one literal is the only thing that could
        drift out of step with §4, and this is the test that catches it.
        """
        self.assertEqual("courier_verified", GEO_SOURCE_COURIER_VERIFIED)

    def test_the_measurement_floor_label_is_the_frozen_section_4_string(self) -> None:
        """The second — and only other — §4 label this app has to spell out itself.

        Used by ``anomaly.detect_far_from_pin`` as the floor below which a stored pin is
        a district rather than a door.
        """
        self.assertEqual("customer_pin", GEO_SOURCE_CUSTOMER_PIN)

    def test_no_local_confidence_ladder_is_declared(self) -> None:
        """A second copy of the ladder is a second thing to keep in sync."""
        source = Path(consensus_pin.__file__).with_suffix(".py").read_text(encoding="utf-8")
        self.assertNotIn("CONFIDENCE_RANK", source)


class TestTheModuleCannotWriteAnything(unittest.TestCase):
    """Structural, not by review: §3 says this app is not an Address writer."""

    MUTATING = {
        "set_value",
        "set_values",
        "db_set",
        "save",
        "insert",
        "submit",
        "new_doc",
        "get_doc",
        "sql",
        "delete_doc",
        "bulk_update",
    }

    def test_it_contains_no_mutating_call(self) -> None:
        source = Path(consensus_pin.__file__).with_suffix(".py").read_text(encoding="utf-8")
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in self.MUTATING:
                offenders.append(f"line {node.lineno}: {name}()")

        self.assertEqual(
            [],
            offenders,
            "consensus_pin must be structurally incapable of writing. Every pin write "
            "goes through pos_bridge.set_address_pin, which is where the §4 ladder is "
            f"enforced. Found: {offenders}",
        )


class TestEvaluateCluster(unittest.TestCase):
    def test_two_couriers_agreeing_is_a_consensus(self) -> None:
        verdict = consensus_pin.evaluate_cluster(
            [door_fix("INV-1", party="HR-EMP-1"), door_fix("INV-2", party="HR-EMP-2")]
        )

        self.assertTrue(verdict["promote"])
        self.assertEqual(2, verdict["invoice_count"])
        self.assertEqual(2, verdict["courier_count"])

    def test_one_delivery_is_never_a_consensus(self) -> None:
        verdict = consensus_pin.evaluate_cluster([door_fix("INV-1")])

        self.assertFalse(verdict["promote"])
        self.assertIn("independent", verdict["reason"])

    def test_one_courier_needs_a_third_delivery(self) -> None:
        """A courier standing in the same wrong spot twice produces a tight cluster.

        Requiring a third makes that an expensive lie rather than a free one. It does not
        make it impossible — nothing here can — which is exactly why ``manual_override``
        outranks ``courier_verified`` on the ladder.
        """
        two = consensus_pin.evaluate_cluster(
            [door_fix("INV-1", party="HR-EMP-1"), door_fix("INV-2", party="HR-EMP-1")]
        )
        self.assertFalse(two["promote"])
        self.assertIn("one courier", two["reason"])

        three = consensus_pin.evaluate_cluster(
            [
                door_fix("INV-1", party="HR-EMP-1"),
                door_fix("INV-2", party="HR-EMP-1"),
                door_fix("INV-3", party="HR-EMP-1"),
            ]
        )
        self.assertTrue(three["promote"])

    def test_the_same_invoice_twice_is_one_delivery(self) -> None:
        """Two proofs on one order (a photo and a signature) are not two witnesses."""
        verdict = consensus_pin.evaluate_cluster(
            [door_fix("INV-1", party="HR-EMP-1"), door_fix("INV-1", party="HR-EMP-2")]
        )
        self.assertFalse(verdict["promote"])
        self.assertEqual(1, verdict["invoice_count"])

    def test_the_promoted_point_is_the_centroid(self) -> None:
        verdict = consensus_pin.evaluate_cluster(
            [
                door_fix("INV-1", party="HR-EMP-1", lat=30.0, lng=31.0),
                door_fix("INV-2", party="HR-EMP-2", lat=30.0002, lng=31.0002),
            ]
        )
        self.assertEqual(30.0001, verdict["latitude"])
        self.assertEqual(31.0001, verdict["longitude"])

    def test_the_written_accuracy_is_the_cluster_spread(self) -> None:
        """How far apart independent observers stood beats any handset's self-opinion."""
        verdict = consensus_pin.evaluate_cluster(
            [
                door_fix("INV-1", party="HR-EMP-1", lat=30.0, lng=31.0, accuracy=4.0),
                door_fix("INV-2", party="HR-EMP-2", lat=30.0002, lng=31.0, accuracy=4.0),
            ]
        )
        # ~22 m apart, so the centroid sits ~11 m from each.
        self.assertAlmostEqual(11.1, verdict["radius_m"], delta=1.0)
        self.assertAlmostEqual(11.1, verdict["accuracy_m"], delta=1.0)

    def test_a_perfect_agreement_is_not_reported_as_unknown_accuracy(self) -> None:
        """0 means "not reported" on the column, so a tight pin needs a floor."""
        verdict = consensus_pin.evaluate_cluster(
            [
                door_fix("INV-1", party="HR-EMP-1", lat=30.0, lng=31.0, accuracy=5.0),
                door_fix("INV-2", party="HR-EMP-2", lat=30.0, lng=31.0, accuracy=5.0),
            ]
        )
        self.assertEqual(0.0, verdict["radius_m"])
        self.assertEqual(consensus_pin.MIN_ACCURACY_FLOOR_M, verdict["accuracy_m"])

    def test_no_reported_accuracy_anywhere_writes_zero_meaning_unknown(self) -> None:
        """An older app build never sent the field; claiming a radius would be a fiction."""
        verdict = consensus_pin.evaluate_cluster(
            [
                door_fix("INV-1", party="HR-EMP-1", accuracy=0),
                door_fix("INV-2", party="HR-EMP-2", accuracy=0),
            ]
        )
        self.assertTrue(verdict["promote"])
        self.assertEqual(0.0, verdict["accuracy_m"])

    def test_an_empty_cluster_promotes_nothing(self) -> None:
        self.assertFalse(consensus_pin.evaluate_cluster([])["promote"])

    def test_null_island_fixes_are_not_evidence(self) -> None:
        verdict = consensus_pin.evaluate_cluster(
            [door_fix("INV-1", lat=0, lng=0), door_fix("INV-2", lat=0, lng=0)]
        )
        self.assertFalse(verdict["promote"])



def web_fix(invoice, **kwargs):
    """A door fix captured through the browser build."""
    fix = door_fix(invoice, **kwargs)
    fix["capture_platform"] = "web"
    return fix


class TestClusterSource(unittest.TestCase):
    """Which ladder label a cluster earns, and why blank must mean native."""

    def test_a_blank_platform_is_native(self) -> None:
        """Every proof written before the web build existed has no value here.

        Reading blank as "web" would demote the entire delivery history to rank 35
        on the day the web app ships — silently, and with no way back.
        """
        self.assertEqual(
            GEO_SOURCE_COURIER_VERIFIED,
            consensus_pin.cluster_source([door_fix("INV-1"), door_fix("INV-2")]),
        )

    def test_an_all_web_cluster_earns_the_web_tier(self) -> None:
        self.assertEqual(
            GEO_SOURCE_COURIER_WEB,
            consensus_pin.cluster_source([web_fix("INV-1"), web_fix("INV-2")]),
        )

    def test_one_native_fix_carries_the_whole_cluster(self) -> None:
        """The native fix vouches for the point; the rest only corroborate it."""
        self.assertEqual(
            GEO_SOURCE_COURIER_VERIFIED,
            consensus_pin.cluster_source([web_fix("INV-1"), door_fix("INV-2")]),
        )

    def test_an_unrecognised_label_is_treated_as_native(self) -> None:
        """A client can only ever downgrade its own proof, never upgrade it."""
        fix = door_fix("INV-1")
        fix["capture_platform"] = "ios-native-someday"
        self.assertEqual(GEO_SOURCE_COURIER_VERIFIED, consensus_pin.cluster_source([fix]))

    def test_the_label_is_case_and_whitespace_insensitive(self) -> None:
        fix = door_fix("INV-1")
        fix["capture_platform"] = "  WEB  "
        self.assertEqual(GEO_SOURCE_COURIER_WEB, consensus_pin.cluster_source([fix]))


class TestWebTierPromotion(unittest.TestCase):
    """A web-only consensus must land at 35 and must not rewrite itself nightly."""

    WEB_MEMBERS = [
        web_fix("INV-1", party="HR-EMP-1", lat=30.0, lng=31.0),
        web_fix("INV-2", party="HR-EMP-2", lat=30.0002, lng=31.0),
    ]
    NATIVE_MEMBERS = [
        door_fix("INV-1", party="HR-EMP-1", lat=30.0, lng=31.0),
        door_fix("INV-2", party="HR-EMP-2", lat=30.0002, lng=31.0),
    ]

    RANKS = {
        "territory_centroid": 10,
        "pos_link": 20,
        "customer_pin": 30,
        "courier_web": 35,
        "courier_verified": 40,
        "manual_override": 50,
    }

    def setUp(self) -> None:
        self.geo = {"rank": 30, "custom_geo_source": "customer_pin"}
        self.pin_result = {"success": True, "accepted": True}

        for name, impl in (
            ("get_address_geo", lambda name: self.geo),
            ("set_address_pin", lambda *a, **k: self.pin_result),
            ("confidence_rank", lambda source: self.RANKS.get(str(source or ""), 0)),
        ):
            patcher = patch.object(consensus_pin.pos_bridge, name, side_effect=impl)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_web_only_consensus_is_written_at_the_web_tier(self) -> None:
        outcome = consensus_pin._promote_one("ADDR-1", self.WEB_MEMBERS, 40)

        self.assertEqual("promoted", outcome)
        kwargs = consensus_pin.pos_bridge.set_address_pin.call_args.kwargs
        self.assertEqual(GEO_SOURCE_COURIER_WEB, kwargs["source"])

    def test_a_web_consensus_does_not_rewrite_an_address_already_at_the_web_tier(self) -> None:
        """Without this the nightly pass resets custom_geo_verified_on forever.

        The ladder accepts an equal rank, so the write would succeed every night.
        """
        self.geo = {"rank": 35, "custom_geo_source": "courier_web"}

        outcome = consensus_pin._promote_one("ADDR-1", self.WEB_MEMBERS, 40)

        self.assertEqual("skipped_already_verified", outcome)
        consensus_pin.pos_bridge.set_address_pin.assert_not_called()

    def test_a_native_consensus_still_upgrades_an_address_at_the_web_tier(self) -> None:
        """35 is a floor for web evidence, never a ceiling for native evidence."""
        self.geo = {"rank": 35, "custom_geo_source": "courier_web"}

        outcome = consensus_pin._promote_one("ADDR-1", self.NATIVE_MEMBERS, 40)

        self.assertEqual("promoted", outcome)
        kwargs = consensus_pin.pos_bridge.set_address_pin.call_args.kwargs
        self.assertEqual(GEO_SOURCE_COURIER_VERIFIED, kwargs["source"])

    def test_a_web_consensus_cannot_touch_a_verified_address(self) -> None:
        self.geo = {"rank": 40, "custom_geo_source": "courier_verified"}

        outcome = consensus_pin._promote_one("ADDR-1", self.WEB_MEMBERS, 40)

        self.assertEqual("skipped_already_verified", outcome)
        consensus_pin.pos_bridge.set_address_pin.assert_not_called()


class TestPromoteOne(unittest.TestCase):
    MEMBERS = [
        door_fix("INV-1", party="HR-EMP-1", lat=30.0, lng=31.0),
        door_fix("INV-2", party="HR-EMP-2", lat=30.0002, lng=31.0),
    ]

    def setUp(self) -> None:
        self.geo = {"rank": 30, "custom_geo_source": "customer_pin"}
        self.pin_result = {"success": True, "accepted": True}

        patcher = patch.object(
            consensus_pin.pos_bridge, "get_address_geo", side_effect=lambda name: self.geo
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.set_pin = patch.object(
            consensus_pin.pos_bridge, "set_address_pin", side_effect=lambda *a, **k: self.pin_result
        )
        self.set_pin.start()
        self.addCleanup(self.set_pin.stop)

    def test_it_delegates_the_write_to_jarz_pos(self) -> None:
        outcome = consensus_pin._promote_one("ADDR-1", self.MEMBERS, 40)

        self.assertEqual("promoted", outcome)
        kwargs = consensus_pin.pos_bridge.set_address_pin.call_args.kwargs
        self.assertEqual(GEO_SOURCE_COURIER_VERIFIED, kwargs["source"])
        self.assertAlmostEqual(30.0001, kwargs["latitude"], places=4)
        self.assertIn("consensus", kwargs["note"])

    def test_an_address_already_verified_is_skipped(self) -> None:
        """Re-writing nightly would reset custom_geo_verified_on for every address."""
        self.geo = {"rank": 40, "custom_geo_source": "courier_verified"}

        outcome = consensus_pin._promote_one("ADDR-1", self.MEMBERS, 40)

        self.assertEqual("skipped_already_verified", outcome)
        consensus_pin.pos_bridge.set_address_pin.assert_not_called()

    def test_a_manual_override_is_skipped_too(self) -> None:
        """A manager with context must be able to fix a pin and have it stick."""
        self.geo = {"rank": 50, "custom_geo_source": "manual_override"}
        self.assertEqual(
            "skipped_already_verified", consensus_pin._promote_one("ADDR-1", self.MEMBERS, 40)
        )

    def test_a_ladder_refusal_is_recorded_not_raised(self) -> None:
        """A rejected write is a normal outcome; the job runs again tomorrow."""
        self.pin_result = {
            "success": True,
            "accepted": False,
            "reason": "lower_confidence",
            "current_source": "manual_override",
        }
        self.assertEqual(
            "rejected_by_ladder", consensus_pin._promote_one("ADDR-1", self.MEMBERS, 40)
        )

    def test_a_missing_address_is_not_promoted(self) -> None:
        """`{}` means "no such Address" — distinct from an Address with no pin yet."""
        self.geo = {}
        self.assertEqual(
            "insufficient_consensus", consensus_pin._promote_one("ADDR-GONE", self.MEMBERS, 40)
        )

    def test_an_address_with_no_pin_at_all_is_still_attempted(self) -> None:
        """The normal first-write case; it must not be confused with "not found"."""
        self.geo = {"rank": 0, "custom_geo_source": None, "custom_latitude": None}
        self.assertEqual("promoted", consensus_pin._promote_one("ADDR-1", self.MEMBERS, 40))

    def test_a_weak_cluster_is_not_promoted(self) -> None:
        outcome = consensus_pin._promote_one("ADDR-1", [door_fix("INV-1")], 40)
        self.assertEqual("insufficient_consensus", outcome)
        consensus_pin.pos_bridge.set_address_pin.assert_not_called()

    def test_fixes_at_two_different_doors_do_not_pool_into_one_consensus(self) -> None:
        """Clustering happens per address, but two clusters must not be merged."""
        far_apart = [
            door_fix("INV-1", party="HR-EMP-1", lat=30.0, lng=31.0),
            door_fix("INV-2", party="HR-EMP-2", lat=30.01, lng=31.0),  # ~1.1 km
        ]
        outcome = consensus_pin._promote_one("ADDR-1", far_apart, 40)
        self.assertEqual("insufficient_consensus", outcome)


class TestCandidateProofs(unittest.TestCase):
    def test_the_query_excludes_mocked_proofs_at_the_database(self) -> None:
        """The Delivery Proof doctype says it outright: a mocked proof never votes.

        A courier who can move a customer's map pin by spoofing their location has a far
        more interesting exploit than a fake delivery.
        """
        with patch.object(consensus_pin.frappe, "get_all", return_value=[]) as get_all:
            consensus_pin._candidate_proofs(days=30, limit=100)

        self.assertEqual(0, get_all.call_args.kwargs["filters"]["is_mocked"])

    def test_a_wildly_inaccurate_proof_is_dropped(self) -> None:
        rows = [
            {
                "name": "DPRF-1",
                "sales_invoice": "INV-1",
                "party": "HR-EMP-1",
                "party_type": "Employee",
                "latitude": 30.0,
                "longitude": 31.0,
                "accuracy_m": 400.0,
            }
        ]
        with patch.object(consensus_pin.frappe, "get_all", return_value=rows):
            self.assertEqual([], consensus_pin._candidate_proofs(days=30, limit=100))

    def test_a_proof_with_no_reported_accuracy_is_kept(self) -> None:
        """Excluding them means consensus never fires on the oldest addresses."""
        rows = [
            {
                "name": "DPRF-1",
                "sales_invoice": "INV-1",
                "party": "HR-EMP-1",
                "party_type": "Employee",
                "latitude": 30.0,
                "longitude": 31.0,
                "accuracy_m": 0,
            }
        ]
        with patch.object(consensus_pin.frappe, "get_all", return_value=rows):
            self.assertEqual(1, len(consensus_pin._candidate_proofs(days=30, limit=100)))

    def test_null_island_is_dropped(self) -> None:
        rows = [
            {
                "name": "DPRF-1",
                "sales_invoice": "INV-1",
                "party": "HR-EMP-1",
                "party_type": "Employee",
                "latitude": 0,
                "longitude": 0,
                "accuracy_m": 5.0,
            }
        ]
        with patch.object(consensus_pin.frappe, "get_all", return_value=rows):
            self.assertEqual([], consensus_pin._candidate_proofs(days=30, limit=100))

    def test_a_query_failure_returns_nothing_rather_than_raising(self) -> None:
        with patch.object(consensus_pin.frappe, "get_all", side_effect=RuntimeError("boom")):
            self.assertEqual([], consensus_pin._candidate_proofs(days=30, limit=100))


class TestGroupByAddress(unittest.TestCase):
    INVOICE_ROWS = [
        {"name": "INV-1", "shipping_address_name": "ADDR-1", "customer_address": "ADDR-X"},
        {"name": "INV-2", "shipping_address_name": None, "customer_address": "ADDR-1"},
        {"name": "INV-3", "shipping_address_name": "ADDR-2", "customer_address": None},
    ]

    def test_proofs_are_bucketed_by_delivery_address(self) -> None:
        proofs = [door_fix("INV-1"), door_fix("INV-2"), door_fix("INV-3")]
        with patch.object(consensus_pin.frappe, "get_all", return_value=self.INVOICE_ROWS):
            grouped = consensus_pin._group_by_address(proofs)

        self.assertEqual({"ADDR-1"}, set(grouped), "ADDR-2 has only one delivery")
        self.assertEqual(2, len(grouped["ADDR-1"]))

    def test_the_invoice_hop_is_one_batched_query(self) -> None:
        """2,000 proofs must not become 2,000 round trips for a lookup one IN answers."""
        proofs = [door_fix(f"INV-{i}") for i in range(50)]
        with patch.object(consensus_pin.frappe, "get_all", return_value=[]) as get_all:
            consensus_pin._group_by_address(proofs)

        self.assertEqual(1, get_all.call_count)

    def test_a_single_delivery_address_is_dropped_before_any_round_trip(self) -> None:
        with patch.object(consensus_pin.frappe, "get_all", return_value=self.INVOICE_ROWS):
            grouped = consensus_pin._group_by_address([door_fix("INV-3")])
        self.assertEqual({}, grouped)


class TestScheduledEntryPoint(unittest.TestCase):
    def test_it_never_raises(self) -> None:
        with patch.object(consensus_pin, "promote_pins", side_effect=RuntimeError("boom")):
            self.assertEqual({"error": True}, consensus_pin.scheduled_promote())

    def test_the_pass_survives_one_bad_address(self) -> None:
        members = [door_fix("INV-1", party="A"), door_fix("INV-2", party="B")]
        with patch.object(
            consensus_pin, "_candidate_proofs", return_value=members
        ), patch.object(
            consensus_pin, "_group_by_address", return_value={"ADDR-1": members, "ADDR-2": members}
        ), patch.object(
            consensus_pin.pos_bridge, "confidence_rank", return_value=40
        ), patch.object(
            consensus_pin,
            "_promote_one",
            side_effect=[RuntimeError("boom"), "promoted"],
        ):
            summary = consensus_pin.promote_pins()

        self.assertEqual(2, summary["addresses_examined"])
        self.assertEqual(1, summary["promoted"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``api/tracking`` — transport, permissions and branch scoping.

The endpoint contract from COURIER_CONTRACTS.md §8 that these tests hold to:

* the permission check is the **first** statement, before any query;
* ``frappe.PermissionError`` is re-raised **before** the generic handler, so a scoping
  failure surfaces as a real 403 rather than being flattened into
  ``{"success": False}`` — a 200 for a permission failure is a security bug that reads
  like a bug report;
* an unscoped query is impossible: an empty branch list returns nothing, never
  everything.

Plus the one asymmetry worth stating: **couriers may record their own position but may
not read anybody else's.** A live map of every colleague on the branch has no operational
purpose for a courier and is trivially screenshotted, so ``get_live_positions`` is
supervisor-only while ``ingest_ping`` is not.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import tracking as api  # noqa: E402
from jarz_courier.services import tracking as real_tracking  # noqa: E402
from jarz_courier.tests._support import COURIER_IDENTITY, COURIER_ROLES, NO_ROLES, SUPERVISOR_ROLES  # noqa: E402


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ingest_result = {
            "run": "CRUN-00001",
            "received": 1,
            "accepted": 1,
            "duplicate": 0,
            "rejected": 0,
            "mocked": 0,
        }
        self.tracking = MagicMock()
        self.tracking.ingest.side_effect = lambda **kwargs: self.ingest_result
        # `ingest` is mocked because it writes; the OwnTracks mapper is pure, and
        # mocking it would make every assertion about the mapping vacuous — a
        # MagicMock is never None, so the non-location guard would look like it
        # passed while actually never firing.
        self.tracking.owntracks_to_fix.side_effect = real_tracking.owntracks_to_fix

        patches = [
            patch.object(api, "tracking", self.tracking),
            patch.object(
                api.courier_onboarding, "resolve_active_branch", return_value=COURIER_IDENTITY
            ),
            patch.object(
                api.courier_onboarding, "ensure_courier_setup", return_value=COURIER_IDENTITY
            ),
            patch.object(api.duty_session, "get_open_duty", return_value={"name": "CDUTY-1"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def as_roles(self, roles):
        return patch.object(api.frappe, "get_roles", return_value=roles)


class TestIngestPing(ApiTestCase):
    def test_a_courier_can_record_their_own_position(self) -> None:
        with self.as_roles(COURIER_ROLES):
            result = api.ingest_ping(latitude=30.0444, longitude=31.2357, accuracy_m=12.0)

        self.assertTrue(result["success"])
        self.assertEqual("CRUN-00001", result["run"])

    def test_someone_with_no_courier_role_is_refused(self) -> None:
        with self.as_roles(NO_ROLES), self.assertRaises(frappe.PermissionError):
            api.ingest_ping(latitude=30.0444, longitude=31.2357)

    def test_the_permission_check_runs_before_any_lookup(self) -> None:
        with self.as_roles(NO_ROLES), patch.object(
            api.courier_onboarding, "resolve_active_branch"
        ) as resolve:
            with self.assertRaises(frappe.PermissionError):
                api.ingest_ping(latitude=30.0444, longitude=31.2357)

        resolve.assert_not_called()

    def test_the_handset_timestamp_and_mock_flag_are_passed_straight_through(self) -> None:
        with self.as_roles(COURIER_ROLES):
            api.ingest_ping(
                latitude=30.0444,
                longitude=31.2357,
                timestamp="2026-08-08 09:15:00",
                is_mocked="true",
            )

        ping = self.tracking.ingest.call_args.kwargs["pings"][0]
        self.assertEqual("2026-08-08 09:15:00", ping["ts"])
        self.assertEqual("true", ping["is_mocked"])

    def test_the_branch_comes_from_the_validated_identity_not_the_client(self) -> None:
        """A client-supplied branch the courier is not assigned to is a scoping bypass."""
        with self.as_roles(COURIER_ROLES):
            api.ingest_ping(latitude=30.0444, longitude=31.2357, branch="SomeOtherBranch")

        self.assertEqual("Dokki", self.tracking.ingest.call_args.kwargs["branch"])
        api.courier_onboarding.resolve_active_branch.assert_called_once()

    def test_a_scoping_error_surfaces_as_a_403_not_a_success_envelope(self) -> None:
        with self.as_roles(COURIER_ROLES), patch.object(
            api.courier_onboarding,
            "resolve_active_branch",
            side_effect=frappe.PermissionError("wrong branch"),
        ):
            with self.assertRaises(frappe.PermissionError):
                api.ingest_ping(latitude=30.0444, longitude=31.2357)

    def test_an_unexpected_failure_is_reported_in_the_envelope(self) -> None:
        self.tracking.ingest.side_effect = RuntimeError("redis exploded")

        with self.as_roles(COURIER_ROLES):
            result = api.ingest_ping(latitude=30.0444, longitude=31.2357)

        self.assertFalse(result["success"])
        self.assertIn("redis exploded", result["error"])


class TestIngestPings(ApiTestCase):
    BATCH = [
        {"lat": 30.0444, "lng": 31.2357, "epoch": 1786000000},
        {"lat": 30.0450, "lng": 31.2360, "epoch": 1786000010},
    ]

    def test_a_json_string_batch_is_accepted(self) -> None:
        """A Frappe form POST encodes a list as a JSON string."""
        with self.as_roles(COURIER_ROLES):
            result = api.ingest_pings(pings=json.dumps(self.BATCH))

        self.assertTrue(result["success"])
        self.assertEqual(2, len(self.tracking.ingest.call_args.kwargs["pings"]))

    def test_an_already_decoded_list_is_accepted(self) -> None:
        with self.as_roles(COURIER_ROLES):
            api.ingest_pings(pings=self.BATCH)
        self.assertEqual(2, len(self.tracking.ingest.call_args.kwargs["pings"]))

    def test_a_single_object_is_accepted(self) -> None:
        with self.as_roles(COURIER_ROLES):
            api.ingest_pings(pings=self.BATCH[0])
        self.assertEqual(1, len(self.tracking.ingest.call_args.kwargs["pings"]))

    def test_an_empty_batch_is_a_success_with_nothing_done(self) -> None:
        with self.as_roles(COURIER_ROLES):
            result = api.ingest_pings(pings="[]")

        self.assertTrue(result["success"])
        self.assertEqual(0, result["accepted"])
        self.tracking.ingest.assert_not_called()

    def test_malformed_json_is_a_clear_error_not_a_traceback(self) -> None:
        with self.as_roles(COURIER_ROLES):
            result = api.ingest_pings(pings="{not json")

        self.assertFalse(result["success"])
        self.assertIn("JSON array", result["error"])

    def test_non_object_entries_are_discarded_rather_than_failing_the_flush(self) -> None:
        with self.as_roles(COURIER_ROLES):
            api.ingest_pings(pings=json.dumps([self.BATCH[0], "garbage", 42]))
        self.assertEqual(1, len(self.tracking.ingest.call_args.kwargs["pings"]))

    def test_an_oversized_batch_is_truncated_at_the_transport_boundary(self) -> None:
        from jarz_courier.constants import QUERY_LIMITS

        oversized = [dict(self.BATCH[0]) for _ in range(QUERY_LIMITS.PINGS_PER_BATCH + 100)]
        with self.as_roles(COURIER_ROLES):
            api.ingest_pings(pings=oversized)

        self.assertEqual(
            QUERY_LIMITS.PINGS_PER_BATCH, len(self.tracking.ingest.call_args.kwargs["pings"])
        )



class TestIngestOwnTracks(ApiTestCase):
    """The iOS path: OwnTracks posts a bare JSON body, authenticated as the courier."""

    LOCATION = {
        "_type": "location",
        "lat": 30.044420,
        "lon": 31.235712,
        "tst": 1786000000,
        "acc": 9,
        "cog": 12,
        "vel": 18,
    }

    def post(self, body, roles=COURIER_ROLES):
        with self.as_roles(roles), patch.object(api.frappe, "form_dict", dict(body), create=True):
            return api.ingest_owntracks()

    def test_a_location_report_is_ingested(self) -> None:
        result = self.post(self.LOCATION)

        self.assertTrue(result["success"])
        self.tracking.ingest.assert_called_once()

    def test_it_goes_through_ingest_not_the_cache(self) -> None:
        """The whole point of the shim.

        Writing to location_cache directly would show a dot and silently starve the
        trail, the Courier Run, the polyline and every anomaly detector.
        """
        self.post(self.LOCATION)

        kwargs = self.tracking.ingest.call_args.kwargs
        self.assertEqual([dict], [type(p) for p in kwargs["pings"]])
        self.assertEqual("Dokki", kwargs["branch"])
        self.assertEqual("Employee", kwargs["party_type"])

    def test_the_branch_comes_from_the_server_not_the_body(self) -> None:
        """OwnTracks is configured by hand, so treat every field as untrusted."""
        self.post({**self.LOCATION, "branch": "Someone Elses Branch"})

        self.assertEqual("Dokki", self.tracking.ingest.call_args.kwargs["branch"])

    def test_someone_with_no_courier_role_is_refused(self) -> None:
        with self.assertRaises(api.frappe.PermissionError):
            self.post(self.LOCATION, roles=NO_ROLES)

    def test_a_non_location_message_is_a_success_with_nothing_stored(self) -> None:
        """A 4xx here would make OwnTracks retry a region transition forever."""
        result = self.post({"_type": "transition", "event": "enter"})

        self.assertTrue(result["success"])
        self.assertEqual(0, result["accepted"])
        self.assertEqual("transition", result["ignored"])
        self.tracking.ingest.assert_not_called()

    def test_a_junk_body_does_not_raise(self) -> None:
        result = self.post({"hello": "world"})

        self.assertTrue(result["success"])
        self.tracking.ingest.assert_not_called()

    def test_an_open_duty_is_reused_rather_than_reopened(self) -> None:
        with patch.object(api.duty_session, "start_duty") as start_duty:
            self.post(self.LOCATION)

        start_duty.assert_not_called()
        self.assertEqual("CDUTY-1", self.tracking.ingest.call_args.kwargs["duty"])

    def test_a_duty_is_auto_opened_when_there_is_none(self) -> None:
        """An iPhone courier has no foreground service to bind a shift to.

        Requiring a manual Start Shift would make "he forgot and was invisible all
        day, with nothing to tell him" the common failure.
        """
        with patch.object(api.duty_session, "get_open_duty", return_value=None), patch.object(
            api.duty_session,
            "start_duty",
            return_value={"duty": {"name": "CDUTY-NEW"}, "created": True},
        ) as start_duty:
            self.post(self.LOCATION)

        start_duty.assert_called_once()
        self.assertEqual("CDUTY-NEW", self.tracking.ingest.call_args.kwargs["duty"])

    def test_a_failed_auto_open_still_records_the_position(self) -> None:
        """Telemetry must not depend on the duty bookkeeping succeeding."""
        with patch.object(api.duty_session, "get_open_duty", return_value=None), patch.object(
            api.duty_session, "start_duty", return_value={}
        ):
            result = self.post(self.LOCATION)

        self.assertTrue(result["success"])
        self.assertIsNone(self.tracking.ingest.call_args.kwargs["duty"])

    def test_a_scoping_error_surfaces_as_a_403(self) -> None:
        with patch.object(
            api.courier_onboarding,
            "ensure_courier_setup",
            side_effect=api.frappe.PermissionError("nope"),
        ):
            with self.assertRaises(api.frappe.PermissionError):
                self.post(self.LOCATION)


class TestGetLivePositions(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(
            api.tracking,
            "branch_positions",
            side_effect=lambda branch: {"branch": branch, "couriers": [], "count": 2},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def as_roles(self, roles):
        return patch.object(api.frappe, "get_roles", return_value=roles)

    def test_a_supervisor_sees_their_own_branches(self) -> None:
        with self.as_roles(SUPERVISOR_ROLES), patch.object(
            api.pos_bridge, "get_user_pos_profiles", return_value=["Dokki", "Zamalek"]
        ):
            result = api.get_live_positions()

        self.assertTrue(result["success"])
        self.assertEqual(["Dokki", "Zamalek"], [b["branch"] for b in result["branches"]])
        self.assertEqual(4, result["count"])

    def test_a_courier_cannot_watch_their_colleagues(self) -> None:
        """Surveillance with no operational purpose for them, and easily screenshotted."""
        with self.as_roles(COURIER_ROLES), self.assertRaises(frappe.PermissionError):
            api.get_live_positions()

    def test_another_branch_is_refused_with_a_403(self) -> None:
        with self.as_roles(SUPERVISOR_ROLES), patch.object(
            api.pos_bridge, "get_user_pos_profiles", return_value=["Dokki"]
        ):
            with self.assertRaises(frappe.PermissionError):
                api.get_live_positions(branch="Zamalek")

    def test_a_supervisor_with_no_branches_sees_nothing_rather_than_everything(self) -> None:
        """Widening a query because a scope resolved to nothing is the whole failure mode."""
        with self.as_roles(SUPERVISOR_ROLES), patch.object(
            api.pos_bridge, "get_user_pos_profiles", return_value=[]
        ):
            result = api.get_live_positions()

        self.assertTrue(result["success"])
        self.assertEqual([], result["branches"])
        self.assertEqual(0, result["count"])
        api.tracking.branch_positions.assert_not_called()

    def test_the_ttl_is_reported_so_a_client_can_age_out_a_stale_marker(self) -> None:
        with self.as_roles(SUPERVISOR_ROLES), patch.object(
            api.pos_bridge, "get_user_pos_profiles", return_value=["Dokki"]
        ):
            result = api.get_live_positions()

        self.assertEqual(api.location_cache.LOCATION_TTL_SEC, result["ttl_seconds"])



class TestTheOwnTracksRateLimiterIsConfigured(unittest.TestCase):
    """Structural, because the decorator is a no-op in this harness.

    ``frappe.rate_limiter`` does not exist under the stubs, so ``_frappe_rate_limit``
    is None and the decorator returns the function untouched. A misconfiguration here
    is therefore invisible to every behavioural test in this file — and the specific
    misconfiguration is not "slightly wrong limits", it is an endpoint that errors on
    100% of requests. Frappe builds the identity as::

        user_key = frappe.form_dict.get(key, "")
        if key and ip_based: identity = ip + ":" + user_key
        identity = identity or ip or user_key
        if not identity: frappe.throw("Either key or IP flag is required.")

    With ``ip_based=False`` and a key OwnTracks does not send, identity is empty and
    every call throws. Both halves are asserted on the source.
    """

    #: Fields OwnTracks actually puts in an HTTP location payload. ``tid`` is the only
    #: stable per-device identifier among them.
    OWNTRACKS_FIELDS = {
        "lat", "lon", "tst", "acc", "alt", "batt", "bs", "cog", "rad",
        "t", "tid", "vac", "vel", "p", "conn", "topic", "inregions", "SSID",
    }

    def rate_limit_kwargs(self):
        source = Path(api.__file__).with_suffix(".py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name == "_frappe_rate_limit":
                return {kw.arg: kw.value for kw in node.keywords}
        self.fail("the OwnTracks endpoint has no rate limiter at all")

    def test_it_is_ip_based(self) -> None:
        ip_based = self.rate_limit_kwargs()["ip_based"]
        self.assertTrue(
            isinstance(ip_based, ast.Constant) and ip_based.value is True,
            "ip_based=False with a body-absent key makes every request throw",
        )

    def test_the_key_is_a_field_owntracks_actually_sends(self) -> None:
        key = self.rate_limit_kwargs()["key"]
        self.assertIsInstance(key, ast.Constant)
        self.assertIn(
            key.value,
            self.OWNTRACKS_FIELDS,
            f"{key.value!r} never appears in an OwnTracks payload, so the per-device "
            "half of the identity would always be empty",
        )


class TestEveryEndpointIsWhitelisted(unittest.TestCase):
    """Contract §8: explicit ``@frappe.whitelist(allow_guest=False)`` on every endpoint.

    A missing decorator is not a visible failure in this repository — it surfaces as a
    404 from the handset, which reads as "the app is broken" rather than "somebody
    dropped a line".

    Checked two ways because the two environments differ: real frappe registers the
    function in ``frappe.whitelisted`` (and may wrap it for argument validation), while
    the test stub sets a ``whitelisted`` attribute. Asserting only one of the two would
    make this test pass vacuously in the environment it was not written for.
    """

    def test_every_endpoint_carries_the_decorator(self) -> None:
        registry = getattr(frappe, "whitelisted", None)

        for endpoint in (
            api.ingest_ping,
            api.ingest_pings,
            api.ingest_owntracks,
            api.get_live_positions,
        ):
            registered = bool(getattr(endpoint, "whitelisted", False))
            if registry is not None and not callable(registry):
                registered = registered or endpoint in registry

            self.assertTrue(
                registered,
                f"{endpoint.__name__} must be @frappe.whitelist(allow_guest=False)",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

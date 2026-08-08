"""``services/push`` — B8, and the one thing that makes it a wake signal.

The property worth testing is a single missing argument. A high-priority FCM message
carrying a ``notification`` block is handed to the Android **system tray**; while the app
is backgrounded, ``onMessageReceived`` is never called, so the app cannot refresh a run
sheet or restart tracking. A **data-only** message with ``priority: high`` reaches the
app's handler in the background and is exempt from Doze batching. So "no notification
argument" is the entire difference between a wake attempt and a tray icon, and it is one
easy, well-intentioned edit away from being broken by someone adding a nice title.

That is why the test below asserts on the *absence* of a field.

Everything else is failure behaviour: no tokens, no SDK, Firebase not initialised, a dead
token. All of them must degrade, none of them may raise — a push failure must never undo
a hand-over that was already posted or block a courier's shift.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.constants import PUSH_TYPE  # noqa: E402
from jarz_courier.services import push  # noqa: E402


class FakeMessaging:
    """Records what would have been sent to FCM."""

    def __init__(self):
        self.sent = []
        self.errors = {}

    # ── the SDK's builders, reduced to plain records ────────────────────
    class AndroidNotification(dict):
        pass

    def AndroidConfig(self, **kwargs):
        return {"_kind": "android", **kwargs}

    def APNSConfig(self, **kwargs):
        return {"_kind": "apns", **kwargs}

    def APNSPayload(self, **kwargs):
        return {"_kind": "apns_payload", **kwargs}

    def Aps(self, **kwargs):
        return {"_kind": "aps", **kwargs}

    def Notification(self, **kwargs):  # pragma: no cover - must never be used
        raise AssertionError(
            "push must never attach a notification block — that moves delivery to the "
            "system tray and stops the app's background handler from running."
        )

    def Message(self, **kwargs):
        return kwargs

    def send(self, message):
        token = message["token"]
        if token in self.errors:
            raise self.errors[token]
        self.sent.append(message)
        return f"projects/x/messages/{len(self.sent)}"


class PushTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.messaging = FakeMessaging()
        patches = [
            patch.object(push, "messaging", self.messaging),
            patch.object(push, "MESSAGING_AVAILABLE", True),
            patch.object(push.pos_bridge, "ensure_push_ready", return_value={"ok": True}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class TestTheMessageIsDataOnly(PushTestCase):
    def test_no_notification_block_is_attached(self) -> None:
        """The one assertion this module exists for.

        ``FakeMessaging.Notification`` raises if called, so a regression fails loudly
        rather than silently turning every wake attempt into a tray icon.
        """
        push.send(["tok-1"], data={"type": "x"})

        message = self.messaging.sent[0]
        self.assertNotIn("notification", message)
        self.assertIn("data", message)

    def test_android_priority_is_high(self) -> None:
        """Without it the message is batched into a Doze window and arrives whenever."""
        push.send(["tok-1"], data={"type": "x"})
        self.assertEqual("high", self.messaging.sent[0]["android"]["priority"])

    def test_ios_gets_a_background_content_available_push(self) -> None:
        push.send(["tok-1"], data={"type": "x"})
        apns = self.messaging.sent[0]["apns"]
        self.assertEqual("background", apns["headers"]["apns-push-type"])
        self.assertTrue(apns["payload"]["aps"]["content_available"])

    def test_a_ttl_is_set_so_a_stale_hint_expires(self) -> None:
        push.send(["tok-1"], data={"type": "x"}, ttl_sec=600)
        self.assertEqual(600, self.messaging.sent[0]["android"]["ttl"].total_seconds())


class TestDataNormalisation(unittest.TestCase):
    def test_every_value_becomes_a_string(self) -> None:
        """A stray int does not raise a helpful error — the SDK drops the whole message."""
        data = push.normalise_data({"count": 3, "amount": 42.5, "ok": True, "name": "x"})
        self.assertEqual({"count": "3", "amount": "42.5", "ok": "1", "name": "x"}, data)

    def test_none_values_are_removed_not_stringified(self) -> None:
        """"None" as a string is something the client would have to special-case."""
        self.assertEqual({"a": "1"}, push.normalise_data({"a": 1, "b": None}))


class TestSendDegradation(PushTestCase):
    def test_no_tokens_is_a_success_not_a_failure(self) -> None:
        result = push.send([], data={"type": "x"})
        self.assertTrue(result["ok"])
        self.assertEqual("skipped_no_tokens", result["status"])

    def test_duplicate_tokens_are_sent_once(self) -> None:
        push.send(["tok-1", "tok-1", " tok-1 "], data={"type": "x"})
        self.assertEqual(1, len(self.messaging.sent))

    def test_a_missing_sdk_is_reported_not_raised(self) -> None:
        with patch.object(push, "MESSAGING_AVAILABLE", False):
            result = push.send(["tok-1"], data={"type": "x"})

        self.assertFalse(result["ok"])
        self.assertEqual("skipped_sdk_unavailable", result["status"])

    def test_firebase_not_initialised_is_reported_not_raised(self) -> None:
        with patch.object(
            push.pos_bridge, "ensure_push_ready", return_value={"ok": False, "reason": "no creds"}
        ):
            result = push.send(["tok-1"], data={"type": "x"})

        self.assertFalse(result["ok"])
        self.assertEqual("skipped_not_initialised", result["status"])

    def test_readiness_is_asked_of_jarz_pos_not_resolved_locally(self) -> None:
        """Two resolvers for one credential is how push works in one worker and not another."""
        with patch.object(
            push.pos_bridge, "ensure_push_ready", return_value={"ok": True}
        ) as ready:
            push.send(["tok-1"], data={"type": "x"})
        ready.assert_called_once()

    def test_one_bad_token_does_not_stop_the_others(self) -> None:
        self.messaging.errors["tok-bad"] = RuntimeError("transport blew up")

        result = push.send(["tok-bad", "tok-good"], data={"type": "x"})

        self.assertEqual(1, result["sent"])
        self.assertEqual(1, result["failed"])
        self.assertEqual("partial", result["status"])

    def test_a_dead_token_is_classified_and_handed_back_for_pruning(self) -> None:
        class UnregisteredError(Exception):
            pass

        self.messaging.errors["tok-dead"] = UnregisteredError("Requested entity was not found")
        pruned = []

        result = push.send(
            ["tok-dead"], data={"type": "x"}, on_dead_token=pruned.append
        )

        self.assertEqual(1, result["dead_tokens"])
        self.assertEqual(["tok-dead"], pruned)

    def test_a_transport_error_is_not_treated_as_a_dead_token(self) -> None:
        """Blanking a live token on a network blip silently stops all future pushes."""
        self.messaging.errors["tok-1"] = RuntimeError("connection reset by peer")
        pruned = []

        result = push.send(["tok-1"], data={"type": "x"}, on_dead_token=pruned.append)

        self.assertEqual(0, result["dead_tokens"])
        self.assertEqual([], pruned)

    def test_a_failing_prune_callback_does_not_break_the_send(self) -> None:
        class UnregisteredError(Exception):
            pass

        self.messaging.errors["tok-dead"] = UnregisteredError("not registered")

        def explode(_token):
            raise RuntimeError("db down")

        result = push.send(["tok-dead"], data={"type": "x"}, on_dead_token=explode)
        self.assertEqual(1, result["dead_tokens"])


class TestTokenResolution(unittest.TestCase):
    def test_only_active_devices_with_a_token_are_returned(self) -> None:
        rows = [
            {"name": "CDEV-1", "fcm_token": "tok-1"},
            {"name": "CDEV-2", "fcm_token": ""},
            {"name": "CDEV-3", "fcm_token": None},
        ]
        with patch.object(push.frappe, "get_all", return_value=rows) as get_all:
            devices = push.courier_device_tokens("Employee", "HR-EMP-1")

        self.assertEqual([{"name": "CDEV-1", "token": "tok-1"}], devices)
        self.assertEqual(1, get_all.call_args.kwargs["filters"]["is_active"])

    def test_a_missing_party_asks_nothing(self) -> None:
        with patch.object(push.frappe, "get_all") as get_all:
            self.assertEqual([], push.courier_device_tokens("", ""))
        get_all.assert_not_called()

    def test_ops_tokens_come_from_enabled_rows_only(self) -> None:
        with patch.object(
            push.frappe, "get_all", return_value=[{"token": "ops-1"}, {"token": "ops-1"}]
        ) as get_all:
            tokens = push.user_tokens(["ops@example.com", "ops@example.com", "Guest", ""])

        self.assertEqual(["ops-1"], tokens, "deduplicated")
        self.assertEqual(1, get_all.call_args.kwargs["filters"]["enabled"])
        self.assertEqual(["ops@example.com"], get_all.call_args.kwargs["filters"]["user"][1])

    def test_a_missing_jarz_pos_device_doctype_is_survivable(self) -> None:
        with patch.object(push.frappe, "get_all", side_effect=RuntimeError("no such table")):
            self.assertEqual([], push.user_tokens(["ops@example.com"]))


class TestSendToCourier(PushTestCase):
    def test_a_courier_with_no_device_is_not_an_error(self) -> None:
        with patch.object(push, "courier_device_tokens", return_value=[]):
            result = push.send_to_courier(
                party_type="Employee", party="HR-EMP-1", data={"type": "x"}
            )
        self.assertTrue(result["ok"])
        self.assertEqual("skipped_no_device", result["status"])

    def test_a_dead_token_is_cleared_on_our_own_device_row(self) -> None:
        class UnregisteredError(Exception):
            pass

        self.messaging.errors["tok-dead"] = UnregisteredError("not registered")

        with patch.object(
            push, "courier_device_tokens", return_value=[{"name": "CDEV-1", "token": "tok-dead"}]
        ), patch.object(push.frappe.db, "set_value") as set_value:
            push.send_to_courier(party_type="Employee", party="HR-EMP-1", data={"type": "x"})

        set_value.assert_called_once()
        self.assertFalse(set_value.call_args.kwargs["update_modified"])


class TestTheFourEvents(PushTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(
            push, "courier_device_tokens", return_value=[{"name": "CDEV-1", "token": "tok-1"}]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_new_assignment_is_not_collapsed(self) -> None:
        """Two assignments are two facts. Collapsing drops the first order silently."""
        push.notify_new_assignment(
            party_type="Employee",
            party="HR-EMP-1",
            branch="Dokki",
            invoices=["INV-1"],
            display_ids=["16834"],
        )

        message = self.messaging.sent[0]
        self.assertEqual(PUSH_TYPE.NEW_ASSIGNMENT, message["data"]["type"])
        self.assertIsNone(message["android"]["collapse_key"])
        self.assertEqual("16834", message["data"]["display_ids"])

    def test_nothing_new_sends_nothing(self) -> None:
        result = push.notify_new_assignment(
            party_type="Employee", party="HR-EMP-1", branch="Dokki", invoices=[]
        )
        self.assertEqual("skipped_nothing_new", result["status"])
        self.assertEqual([], self.messaging.sent)

    def test_a_huge_assignment_list_is_truncated_to_stay_inside_the_fcm_limit(self) -> None:
        """FCM caps a message at 4 KB; 40 invoice names would mean receiving nothing."""
        push.notify_new_assignment(
            party_type="Employee",
            party="HR-EMP-1",
            branch="Dokki",
            invoices=[f"ACC-SINV-{i:05d}" for i in range(40)],
        )

        data = self.messaging.sent[0]["data"]
        self.assertEqual("40", data["count"], "the count is still honest")
        self.assertEqual(20, len(data["invoices"].split(",")))

    def test_a_run_change_is_collapsed_per_courier(self) -> None:
        """It is a refetch hint, so an older one is strictly redundant."""
        push.notify_run_changed(
            party_type="Employee", party="HR-EMP-1", branch="Dokki", removed=1
        )
        self.assertEqual("run_changed::HR-EMP-1", self.messaging.sent[0]["android"]["collapse_key"])

    def test_a_confirmed_deposit_carries_its_declaration_reference(self) -> None:
        """This is the message a courier screenshots when a hand-over is disputed."""
        push.notify_deposit_confirmed(
            party_type="Employee",
            party="HR-EMP-1",
            branch="Dokki",
            declaration="CDEP-00007",
            amount=4200.0,
            reference="handed to Ahmed",
        )

        data = self.messaging.sent[0]["data"]
        self.assertEqual(PUSH_TYPE.DEPOSIT_CONFIRMED, data["type"])
        self.assertEqual("CDEP-00007", data["declaration"])
        self.assertEqual("4200.0", data["amount"])


class TestStalePingAlert(PushTestCase):
    RUN = {
        "name": "CRUN-00001",
        "branch": "Dokki",
        "party_type": "Employee",
        "party": "HR-EMP-00042",
    }

    def test_the_alert_goes_to_ops_and_not_to_the_courier(self) -> None:
        """Pushing "your app stopped reporting" to the app that stopped reporting is
        a message to nobody. The failure the watchdog detects is the failure that
        prevents its own delivery."""
        with patch.object(
            push.pos_bridge, "resolve_branch_recipients", return_value=["ops@example.com"]
        ), patch.object(push, "user_tokens", return_value=["ops-tok"]), patch.object(
            push, "courier_device_tokens"
        ) as courier_tokens:
            result = push.notify_stale_ping(run=self.RUN, silent_minutes=35, reason="no position")

        courier_tokens.assert_not_called()
        self.assertEqual(1, result["recipients"])
        self.assertEqual("ops-tok", self.messaging.sent[0]["token"])
        self.assertEqual("35", self.messaging.sent[0]["data"]["silent_minutes"])

    def test_the_audience_is_resolved_through_jarz_pos_branch_scoping(self) -> None:
        with patch.object(
            push.pos_bridge, "resolve_branch_recipients", return_value=[]
        ) as resolve, patch.object(push, "user_tokens", return_value=[]):
            push.notify_stale_ping(run=self.RUN, silent_minutes=35, reason="no position")

        resolve.assert_called_once_with(["Dokki"])

    def test_a_branch_with_no_ops_users_is_not_a_crash(self) -> None:
        with patch.object(
            push.pos_bridge, "resolve_branch_recipients", return_value=[]
        ), patch.object(push, "user_tokens", return_value=[]):
            result = push.notify_stale_ping(run=self.RUN, silent_minutes=35, reason="quiet")

        self.assertEqual("skipped_no_tokens", result["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

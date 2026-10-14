"""Contract F7 - a new Medium-or-worse finding is pushed to the branch's ops.

Three properties matter and each has a test:

* **Only new, only worth waking someone for.** A Low finding, a repeat of an existing one
  and a Stale Ping (which ``watch_stale_pings`` already pushes by itself) send nothing.
* **A push can never undo or block a finding.** ``flag`` is called from detectors and
  from a courier's delivery scan; whatever goes wrong in the push, the finding stays
  filed and ``flag`` still returns ``created``.
* **A rolled-back finding is not announced.** When Frappe offers ``after_commit`` the
  send waits for it.

``notify_anomaly`` itself is checked for the payload shape the POS app reads.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

from jarz_courier.constants import ANOMALY_TYPE, PUSH_TYPE, SEVERITY  # noqa: E402
from jarz_courier.services import anomaly, push  # noqa: E402


def _flag(**overrides):
    kwargs = dict(
        anomaly_type=ANOMALY_TYPE.DETOUR,
        severity=SEVERITY.HIGH,
        reason="Rode 9 km against a 3 km plan",
        party_type="Employee",
        party="HR-EMP-1",
        branch="Dokki",
        dedupe_key="detour::CRUN-1",
    )
    kwargs.update(overrides)
    return anomaly.flag(**kwargs)


def _doc(name="CANM-00001"):
    doc = MagicMock()
    doc.name = name
    return doc


class TestShouldPush(unittest.TestCase):
    def test_medium_and_high_are_pushed(self) -> None:
        self.assertTrue(anomaly.should_push(ANOMALY_TYPE.DETOUR, SEVERITY.MEDIUM))
        self.assertTrue(anomaly.should_push(ANOMALY_TYPE.DETOUR, SEVERITY.HIGH))

    def test_low_and_unknown_are_not(self) -> None:
        self.assertFalse(anomaly.should_push(ANOMALY_TYPE.DETOUR, SEVERITY.LOW))
        self.assertFalse(anomaly.should_push(ANOMALY_TYPE.DETOUR, "Whatever"))
        self.assertFalse(anomaly.should_push(ANOMALY_TYPE.DETOUR, None))

    def test_stale_ping_is_never_pushed_from_here(self) -> None:
        self.assertFalse(anomaly.should_push(ANOMALY_TYPE.STALE_PING, SEVERITY.HIGH))


class TestFlagPushes(unittest.TestCase):
    def _run(self, doc=None, exists=None, **overrides):
        doc = doc or _doc()
        with patch.object(anomaly.frappe.db, "exists", return_value=exists), patch.object(
            anomaly.frappe, "new_doc", return_value=doc
        ), patch.object(push, "notify_anomaly") as notify:
            result = _flag(**overrides)
        return result, notify, doc

    def test_a_new_high_finding_is_pushed_once(self) -> None:
        result, notify, doc = self._run()
        self.assertTrue(result["created"])
        notify.assert_called_once_with(doc)

    def test_a_new_medium_finding_is_pushed(self) -> None:
        _, notify, _ = self._run(severity=SEVERITY.MEDIUM)
        notify.assert_called_once()

    def test_a_low_finding_is_not_pushed(self) -> None:
        result, notify, _ = self._run(severity=SEVERITY.LOW)
        self.assertTrue(result["created"])
        notify.assert_not_called()

    def test_a_stale_ping_is_not_pushed(self) -> None:
        result, notify, _ = self._run(anomaly_type=ANOMALY_TYPE.STALE_PING)
        self.assertTrue(result["created"])
        notify.assert_not_called()

    def test_a_repeat_of_an_existing_finding_is_not_pushed(self) -> None:
        result, notify, _ = self._run(exists="CANM-00001")
        self.assertFalse(result["created"])
        notify.assert_not_called()

    def test_a_push_failure_never_undoes_the_finding(self) -> None:
        doc = _doc()
        with patch.object(anomaly.frappe.db, "exists", return_value=None), patch.object(
            anomaly.frappe, "new_doc", return_value=doc
        ), patch.object(push, "notify_anomaly", side_effect=RuntimeError("fcm down")):
            result = _flag()
        self.assertTrue(result["created"])
        self.assertEqual("CANM-00001", result["anomaly"])

    def test_the_push_waits_for_the_commit_when_frappe_offers_the_hook(self) -> None:
        """A caller that files a finding and then raises has the insert rolled back."""
        doc = _doc()
        after_commit = MagicMock()
        with patch.object(anomaly.frappe.db, "exists", return_value=None), patch.object(
            anomaly.frappe, "new_doc", return_value=doc
        ), patch.object(anomaly.frappe.db, "after_commit", after_commit, create=True), patch.object(
            push, "notify_anomaly"
        ) as notify:
            result = _flag()
            self.assertTrue(result["created"])
            notify.assert_not_called()
            after_commit.add.assert_called_once()

            after_commit.add.call_args.args[0]()  # the commit happens
            notify.assert_called_once_with(doc)


class TestNotifyAnomaly(unittest.TestCase):
    def _doc(self, **extra):
        values = {
            "name": "CANM-00007",
            "branch": "Dokki",
            "severity": SEVERITY.HIGH,
            "anomaly_type": ANOMALY_TYPE.DETOUR,
            "party_type": "Employee",
            "party": "HR-EMP-1",
            "sales_invoice": "ACC-SINV-1",
            "reason": "Rode 9 km against a 3 km plan",
        }
        values.update(extra)
        return SimpleNamespace(get=lambda k, d=None: values.get(k, d), **values)

    def _notify(self, doc, tokens=("tok-1", "tok-2"), recipients=("ops@example.com",)):
        send_result = {"ok": True, "status": "sent", "attempted": 2, "sent": 2, "failed": 0}
        with patch.object(push, "_party_display_name", return_value="Mahmoud"), patch.object(
            push.pos_bridge, "resolve_branch_recipients", return_value=list(recipients)
        ) as resolve, patch.object(push, "user_tokens", return_value=list(tokens)), patch.object(
            push, "send", return_value=dict(send_result)
        ) as send:
            result = push.notify_anomaly(doc)
        return result, resolve, send

    def test_payload_is_the_contract_shape(self) -> None:
        result, resolve, send = self._notify(self._doc())

        resolve.assert_called_once_with(["Dokki"])
        self.assertEqual(["tok-1", "tok-2"], send.call_args.args[0])
        data = send.call_args.kwargs["data"]
        self.assertEqual(PUSH_TYPE.COURIER_ALERT, data["type"])
        self.assertEqual("courier_alert", data["type"])
        self.assertEqual("CANM-00007", data["anomaly"])
        self.assertEqual(ANOMALY_TYPE.DETOUR, data["anomaly_type"])
        self.assertEqual(SEVERITY.HIGH, data["severity"])
        self.assertEqual("ACC-SINV-1", data["invoice"])
        self.assertEqual("Mahmoud", data["party_name"])
        self.assertEqual(1, result["recipients"])

    def test_it_carries_an_english_title_and_body_and_an_arabic_line(self) -> None:
        _, _, send = self._notify(self._doc())
        data = send.call_args.kwargs["data"]
        self.assertIn("Courier alert", data["title"])
        self.assertIn(ANOMALY_TYPE.DETOUR, data["title"])
        self.assertIn("Mahmoud", data["body"])
        self.assertIn("ACC-SINV-1", data["body"])
        self.assertTrue(data["body_ar"])
        self.assertIn("Mahmoud", data["body_ar"])

    def test_every_value_is_a_string_for_fcm(self) -> None:
        _, _, send = self._notify(self._doc(sales_invoice=None))
        data = send.call_args.kwargs["data"]
        self.assertTrue(all(isinstance(v, str) for v in data.values()))
        self.assertEqual("", data["invoice"])

    def test_one_notification_per_finding_collapses(self) -> None:
        _, _, send = self._notify(self._doc())
        self.assertEqual("courier_alert::CANM-00007", send.call_args.kwargs["collapse_key"])

    def test_it_carries_a_tray_block_for_the_pos_app(self) -> None:
        """The POS app shows nothing for a data-only push."""
        _, _, send = self._notify(self._doc())
        title, body = send.call_args.kwargs["notification"]
        self.assertIn("Mahmoud", title)
        self.assertIn("ACC-SINV-1", body)

    def test_a_finding_with_no_branch_has_no_audience(self) -> None:
        _, resolve, _ = self._notify(self._doc(branch=None), tokens=(), recipients=())
        resolve.assert_called_once_with([])

    def test_it_never_raises(self) -> None:
        with patch.object(push, "_party_display_name", return_value="x"), patch.object(
            push.pos_bridge, "resolve_branch_recipients", side_effect=RuntimeError("boom")
        ):
            result = push.notify_anomaly(self._doc())
        self.assertFalse(result["ok"])
        self.assertEqual(0, result["sent"])


if __name__ == "__main__":
    unittest.main()

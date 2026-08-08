"""Controller for Courier Anomaly.

**A flag, never a charge.** COURIER_APP_SPEC B9 says the detector flags and does
not compute money penalties, and that rule is enforced structurally rather than by
review: the doctype declares no Currency field, so there is nowhere on this record
for an amount to be put. ``tests/test_anomaly`` asserts that emptiness, so adding
one is a failing build rather than a quiet Friday-afternoon change.

The reason it matters: an anomaly is derived from GPS, and GPS is wrong sometimes.
A 300 m accuracy fix, a tunnel, a phone that slept — each produces a finding that
looks exactly like misconduct. Every detector here is a prompt to go and ask, and
the moment a number on this record could reach a payslip, "go and ask" quietly
becomes "deduct unless appealed".

``reason`` is required for the same reason. A row saying ``Detour / High`` tells the
manager nothing they can act on; ``"Rode 11.4 km against a 3.9 km planned route
(ratio 2.9)"`` tells them what to ask about.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

DOCTYPE = "Courier Anomaly"
STATUS_OPEN = "Open"
_REVIEWED = ("Reviewed", "Dismissed")


class CourierAnomaly(Document):
    def validate(self) -> None:
        if not self.detected_on:
            self.detected_on = now_datetime()
        if not self.status:
            self.status = STATUS_OPEN

        if not str(self.reason or "").strip():
            frappe.throw(
                _(
                    "An anomaly must say what was measured and what it was compared "
                    "against. A bare type and severity is not actionable."
                )
            )

        self._stamp_review()

    def _stamp_review(self) -> None:
        """Record who closed a finding, the moment it stops being Open.

        Stamped in the controller so a Desk user changing the status from the list
        view is recorded too — that is the most likely place a finding gets
        dismissed, and an unattributed dismissal is the one thing that would make
        this whole table ignorable.
        """
        if self.status not in _REVIEWED:
            return

        previous = self.get_doc_before_save()
        was_open = previous is None or previous.status == STATUS_OPEN
        if was_open or not self.reviewed_by:
            self.reviewed_by = frappe.session.user
            self.reviewed_on = now_datetime()

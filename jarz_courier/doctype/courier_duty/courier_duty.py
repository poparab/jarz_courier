"""Controller for Courier Duty.

Invariants enforced here rather than in ``api/duty.py``, so a Desk edit obeys
them too:

* **One open duty per courier.** Two open duties make "which duty did this stop
  belong to?" unanswerable, and the closing-cash reconciliation then double-counts
  or misses collections depending on which one the client happened to send.
* **A closed duty is closed.** Re-opening one would move its window and silently
  change the set of deliveries the courier was already reconciled against.
* ``end_time`` must not precede ``start_time``.

Recording only. Opening float and closing cash are declarations, not postings —
COURIER_CONTRACTS.md §9 keeps every money write on the jarz_pos side.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

DOCTYPE = "Courier Duty"
STATUS_OPEN = "Open"
STATUS_CLOSED = "Closed"
STATUS_CANCELLED = "Cancelled"


class CourierDuty(Document):
    def validate(self) -> None:
        if not self.start_time:
            self.start_time = now_datetime()

        self._validate_window()
        self._validate_single_open_duty()
        self._validate_no_reopen()

        if self.status in (STATUS_CLOSED, STATUS_CANCELLED) and not self.end_time:
            self.end_time = now_datetime()

    def _validate_window(self) -> None:
        if not self.end_time:
            return
        if get_datetime(self.end_time) < get_datetime(self.start_time):
            frappe.throw(_("Duty end time cannot be before its start time"))

    def _validate_single_open_duty(self) -> None:
        if self.status != STATUS_OPEN or not self.party_type or not self.party:
            return

        existing = frappe.get_all(
            DOCTYPE,
            filters={
                "party_type": self.party_type,
                "party": self.party,
                "status": STATUS_OPEN,
                "name": ["!=", self.name or ""],
            },
            pluck="name",
            limit=1,
        ) or []

        if existing:
            frappe.throw(
                _("Courier {0} already has an open duty ({1}). End it before starting another.").format(
                    self.party, existing[0]
                )
            )

    def _validate_no_reopen(self) -> None:
        if self.is_new() or self.status != STATUS_OPEN:
            return

        previous = self.get_doc_before_save()
        if previous is not None and previous.status in (STATUS_CLOSED, STATUS_CANCELLED):
            frappe.throw(
                _(
                    "Duty {0} is already {1} and cannot be reopened. Start a new duty "
                    "instead — reopening would move the window its cash was reconciled against."
                ).format(self.name, previous.status)
            )

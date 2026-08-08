"""Controller for Courier Run.

A run is the **anchor the GPS polyline needs** and nothing more. It is not a work
unit: the courier's stops are still a query over ``Sales Invoice`` (see
``services/run_sheet``), and no stop is ever listed here. Introducing a stop model
would create a second answer to "what is this courier carrying?", and the invoice
is already the first one.

``track_changes`` is 0 on this doctype. Its live fields are stamped by a background
path at most once a minute; a Version row per stamp would bury every real edit
under thousands of machine writes.

Invariants live here rather than in the service so a Desk edit obeys them too:

* **One open run per courier.** Two open runs make "which run does this ping
  belong to?" unanswerable, and the polyline would then be split arbitrarily
  between them.
* **A closed run is closed.** Reopening one would let new pings extend a polyline
  and a distance that has already been reported.
* ``ended_on`` must not precede ``started_on``.

Nothing here posts, and this doctype holds no monetary field on purpose. A distance
is not an allowance; turning one into the other is a decision for whoever owns the
allowance policy, taken against a number that has already been reviewed.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

DOCTYPE = "Courier Run"
STATUS_OPEN = "Open"
STATUS_CLOSED = "Closed"
STATUS_ABANDONED = "Abandoned"
_TERMINAL = (STATUS_CLOSED, STATUS_ABANDONED)


class CourierRun(Document):
    def validate(self) -> None:
        if not self.started_on:
            self.started_on = now_datetime()

        self._validate_window()
        self._validate_single_open_run()
        self._validate_no_reopen()

        if self.status in _TERMINAL and not self.ended_on:
            self.ended_on = now_datetime()

    def _validate_window(self) -> None:
        if not self.ended_on:
            return
        if get_datetime(self.ended_on) < get_datetime(self.started_on):
            frappe.throw(_("Run end time cannot be before its start time"))

    def _validate_single_open_run(self) -> None:
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
                _(
                    "Courier {0} already has an open run ({1}). Close it before starting "
                    "another — two open runs would split one day's track between them."
                ).format(self.party, existing[0])
            )

    def _validate_no_reopen(self) -> None:
        if self.is_new() or self.status != STATUS_OPEN:
            return

        previous = self.get_doc_before_save()
        if previous is not None and previous.status in _TERMINAL:
            frappe.throw(
                _(
                    "Run {0} is already {1} and cannot be reopened. Its polyline and "
                    "distance have already been written and reported against."
                ).format(self.name, previous.status)
            )

"""Controller for Courier Stop Event.

One row per (invoice, event): "the courier left the branch for this order", "arrived
at its door", "got back". The rows are evidence for the order timeline, written only
by ``services/stop_events`` from the ping-ingest path.

The name is derived from the pair rather than hashed, so the uniqueness rule is the
primary key and not a convention. Two pings racing to record the same arrival then
collide in the database (``DuplicateEntryError``) instead of both passing an
"already recorded?" read and each inserting a row — which would show the same
milestone twice on a manager's timeline.
"""

from __future__ import annotations

from frappe.model.document import Document

DOCTYPE = "Courier Stop Event"


def event_name(invoice: str, event: str) -> str:
    slug = "-".join(str(event or "").strip().lower().split())
    return f"{invoice}-{slug}"


class CourierStopEvent(Document):
    def autoname(self) -> None:
        self.name = event_name(self.invoice, self.event)

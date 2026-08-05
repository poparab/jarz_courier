"""Controller for Courier Device.

One rule lives here: **one active device per courier**, enforced on save rather
than in the API. Device binding happens from three places already (the courier
app's first launch, a token refresh, and a manager force-unbind in the Desk), and
a rule that only exists in ``api/device.py`` is a rule the Desk form does not
have. Two active devices for one courier means an assignment push is delivered to
a handset in a drawer.

Writes only jarz_courier records. Nothing here touches the ledger.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

DOCTYPE = "Courier Device"


class CourierDevice(Document):
    def validate(self) -> None:
        self.device_id = str(self.device_id or "").strip()
        if not self.device_id:
            frappe.throw(_("Device ID is required"))

        self.fcm_token = str(self.fcm_token or "").strip() or None

        if self.is_active:
            if not self.bound_on:
                self.bound_on = now_datetime()
            self.unbound_on = None
        elif not self.unbound_on:
            self.unbound_on = now_datetime()

    def on_update(self) -> None:
        # After this row is persisted, not before: deactivating siblings first and
        # then failing our own save would leave the courier with no active device
        # at all.
        if self.is_active:
            self._deactivate_sibling_devices()

    def _deactivate_sibling_devices(self) -> None:
        """Turn off every other active device for the same courier party."""
        if not self.party_type or not self.party:
            return

        siblings = frappe.get_all(
            DOCTYPE,
            filters={
                "party_type": self.party_type,
                "party": self.party,
                "is_active": 1,
                "name": ["!=", self.name],
            },
            pluck="name",
        ) or []

        stamp = now_datetime()
        for name in siblings:
            try:
                frappe.db.set_value(
                    DOCTYPE,
                    name,
                    {"is_active": 0, "unbound_on": stamp},
                    update_modified=False,
                )
            except Exception:
                # A stale sibling that refuses to update must not block the courier
                # from binding the phone in their hand — but it must be visible,
                # because it means two devices are marked active.
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Failed to deactivate Courier Device {name}",
                )

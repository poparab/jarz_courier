"""Controller for Delivery Proof.

Deliberately permissive about *content* and strict about *identity*.

Permissive: a proof may arrive with no file. The courier app queues POD captures
in Hive and flushes them opportunistically, so the metadata (when, where, who,
recipient) reaches the server long before a 3 MB photo does on a bad connection.
Rejecting a fileless proof would mean the evidence that survives is the evidence
from couriers with good signal.

Strict: ``latitude``/``longitude`` are range-checked. A silent 0.0/0.0 — the value
a handset returns when the fix failed — is a coordinate in the Gulf of Guinea, and
it would otherwise be fed into the P2 address-pin consensus as a real observation.

This doctype is the input to the future consensus job (spec module B5). That job
calls ``jarz_pos.services.geo_resolution``; it does not write Address fields here.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

DOCTYPE = "Delivery Proof"

#: Coordinates outside these are physically impossible, not merely unlikely.
_LAT_RANGE = (-90.0, 90.0)
_LNG_RANGE = (-180.0, 180.0)


class DeliveryProof(Document):
    def validate(self) -> None:
        if not self.captured_at:
            self.captured_at = now_datetime()

        self.recipient_name = str(self.recipient_name or "").strip() or None
        self.request_id = str(self.request_id or "").strip() or None

        self._validate_coordinates()

    def _validate_coordinates(self) -> None:
        lat = self.latitude
        lng = self.longitude

        # Both or neither. A half-coordinate is worse than none: it looks like
        # data to every query that filters on "latitude is set".
        if (lat in (None, "")) != (lng in (None, "")):
            frappe.throw(_("Latitude and longitude must be supplied together"))

        if lat in (None, "") and lng in (None, ""):
            self.latitude = None
            self.longitude = None
            self.accuracy_m = None
            return

        lat = float(lat)
        lng = float(lng)

        if not (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1]):
            frappe.throw(_("Latitude {0} is out of range").format(lat))
        if not (_LNG_RANGE[0] <= lng <= _LNG_RANGE[1]):
            frappe.throw(_("Longitude {0} is out of range").format(lng))

        # The null-island fix. Treated as "no location captured" rather than
        # rejected, so the proof itself still lands.
        if abs(lat) < 1e-7 and abs(lng) < 1e-7:
            self.latitude = None
            self.longitude = None
            self.accuracy_m = None
            return

        self.latitude = lat
        self.longitude = lng
        if self.accuracy_m is not None:
            self.accuracy_m = abs(float(self.accuracy_m))

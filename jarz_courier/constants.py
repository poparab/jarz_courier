"""Constants for the courier app.

Event names are re-exported from ``jarz_pos.constants.WS_EVENTS`` rather than
redeclared. COURIER_CONTRACTS.md §7 froze that file as the single source of the
six courier event names and their Dart mirror; a private copy here would drift
the day someone renames one on the jarz_pos side.
"""

from __future__ import annotations

from jarz_pos.constants import ROLES as POS_ROLES
from jarz_pos.constants import WS_EVENTS

# ── DocType names owned by this app ────────────────────────────────────

class DOCTYPES:
    COURIER_DEVICE = "Courier Device"
    COURIER_DUTY = "Courier Duty"
    DELIVERY_PROOF = "Delivery Proof"
    COURIER_DEPOSIT_DECLARATION = "Courier Deposit Declaration"


# ── Roles ───────────────────────────────────────────────────────────────

class ROLES:
    #: Seeded by ``setup.courier_app_setup``. Held by every courier user.
    COURIER = "Jarz Courier"
    ADMINISTRATOR = POS_ROLES.ADMINISTRATOR
    SYSTEM_MANAGER = POS_ROLES.SYSTEM_MANAGER
    JARZ_MANAGER = POS_ROLES.JARZ_MANAGER
    JARZ_LINE_MANAGER = POS_ROLES.JARZ_LINE_MANAGER

    #: May act as a courier on their own run / device / duty / statement.
    COURIER_SELF = {COURIER, ADMINISTRATOR, SYSTEM_MANAGER, JARZ_MANAGER}
    #: May confirm or reject a courier's cash deposit declaration, force-unbind a
    #: device, and read any courier's pending declarations. Deliberately excludes
    #: COURIER: a courier confirming their own hand-over is the whole failure mode
    #: the declaration exists to prevent.
    COURIER_SUPERVISOR = {ADMINISTRATOR, SYSTEM_MANAGER, JARZ_MANAGER, JARZ_LINE_MANAGER}


# ── Sales Invoice state strings (read-only mirror) ─────────────────────
#
# COURIER_CONTRACTS.md §1: the seven options on
# ``Sales Invoice.custom_sales_invoice_state`` are frozen and owned by jarz_pos.
# The misspelling of "Recieved" is live production data and is NOT corrected
# here. This app only ever reads these; it adds no state.

class INVOICE_STATE:
    OUT_FOR_DELIVERY = "Out for Delivery"
    DELIVERED = "Delivered"
    RETURNED = "Returned"
    CANCELLED = "Cancelled"


# ── Party types a courier can be ───────────────────────────────────────

COURIER_PARTY_TYPES = ("Employee", "Supplier")


# ── Proof of delivery ──────────────────────────────────────────────────

class PROOF_TYPES:
    PHOTO = "Photo"
    SIGNATURE = "Signature"
    OTP = "OTP"

    ALL = (PHOTO, SIGNATURE, OTP)


# ── Deposit declaration ────────────────────────────────────────────────

class DEPOSIT_METHODS:
    CASH_HANDOVER = "Cash Handover"
    INSTAPAY = "InstaPay"

    ALL = (CASH_HANDOVER, INSTAPAY)


class DEPOSIT_STATUS:
    PENDING = "Pending"
    CONFIRMED = "Confirmed"
    REJECTED = "Rejected"

    ALL = (PENDING, CONFIRMED, REJECTED)


class DUTY_STATUS:
    OPEN = "Open"
    CLOSED = "Closed"
    CANCELLED = "Cancelled"

    ALL = (OPEN, CLOSED, CANCELLED)


# ── Query limits ────────────────────────────────────────────────────────

class QUERY_LIMITS:
    RUN_STOPS = 200
    STATEMENT_ROWS = 200
    SETTLEMENT_HISTORY = 50
    PENDING_DEPOSITS = 100
    PROOFS_PER_STOP = 20


__all__ = [
    "COURIER_PARTY_TYPES",
    "DEPOSIT_METHODS",
    "DEPOSIT_STATUS",
    "DOCTYPES",
    "DUTY_STATUS",
    "INVOICE_STATE",
    "PROOF_TYPES",
    "QUERY_LIMITS",
    "ROLES",
    "WS_EVENTS",
]

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
    #: B7 cold path — one row per tracked run, holding ONE encoded polyline.
    COURIER_RUN = "Courier Run"
    #: B9 — a flag, never a charge. See the doctype's own docstring.
    COURIER_ANOMALY = "Courier Anomaly"


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


# ── Tracked run (B7 cold path) ─────────────────────────────────────────

class RUN_STATUS:
    OPEN = "Open"
    CLOSED = "Closed"
    #: The watchdog closed it: no ping for hours and nobody ended the duty.
    #: Distinguished from ``Closed`` so "the courier force-stopped the app" is
    #: visible in a report rather than looking like a normal end of day.
    ABANDONED = "Abandoned"

    ALL = (OPEN, CLOSED, ABANDONED)


# ── Anomalies (B9) ─────────────────────────────────────────────────────

class ANOMALY_TYPE:
    DETOUR = "Detour"
    IDLE = "Excessive Idle"
    SPEEDING = "Speeding"
    FAR_FROM_PIN = "Delivered Far From Pin"
    PING_GAP = "Ping Gap"
    MOCK_GPS = "Mock GPS"
    UNEXPECTED_STOP = "Unexpected Stop"
    STALE_PING = "Stale Ping"

    ALL = (
        DETOUR,
        IDLE,
        SPEEDING,
        FAR_FROM_PIN,
        PING_GAP,
        MOCK_GPS,
        UNEXPECTED_STOP,
        STALE_PING,
    )


class SEVERITY:
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"

    ALL = (LOW, MEDIUM, HIGH)
    #: Ordered worst-first, for "does this new finding outrank the stored one?".
    RANK = {LOW: 10, MEDIUM: 20, HIGH: 30}


class ANOMALY_STATUS:
    OPEN = "Open"
    REVIEWED = "Reviewed"
    DISMISSED = "Dismissed"

    ALL = (OPEN, REVIEWED, DISMISSED)


# ── Address geo source labels (read-only mirror) ────────────────────────
#
# COURIER_CONTRACTS.md §4 owns the confidence ladder and §3 names its two
# authorised writers — this app is not one of them. It therefore does NOT carry a
# copy of ``CONFIDENCE_RANK``: every rank question is asked of
# ``jarz_pos.utils.geo.confidence_rank`` through ``services.pos_bridge``, so there
# is nothing here that can drift out of step with §4.
#
# Two literals are unavoidable: the label this app asks jarz_pos to *write* when a
# consensus forms, and the label naming the floor below which a stored pin is too
# vague to measure a delivery against. Both are asserted against the §4 strings by
# ``tests/test_consensus_pin``.

GEO_SOURCE_COURIER_VERIFIED = "courier_verified"

#: The label for a consensus formed *only* from web-build captures. Contract §4
#: ranks it 35 — above the customer's own pin, below a native capture — because the
#: browser exposes no mock-location flag, so such evidence is unverifiable by
#: construction rather than merely unverified.
GEO_SOURCE_COURIER_WEB = "courier_web"

#: The weakest pin worth comparing a delivery position to. Anything below this on the
#: ladder (``territory_centroid``, ``pos_link``) describes a district or a viewport,
#: not a door — measuring a POD against one produces a 2 km "anomaly" for every
#: address that has never been pinned properly, which is most of them.
GEO_SOURCE_CUSTOMER_PIN = "customer_pin"


# ── Realtime events declared by THIS app ───────────────────────────────
#
# COURIER_CONTRACTS.md §7 appended six courier events to ``jarz_pos/constants.py``
# and then FROZE that file for this project, so a seventh name cannot be added
# there. Location streaming and courier alerts had no name in the frozen six, so
# they are declared here.
#
# The ``jarz_pos_`` prefix is deliberate and is not a layering violation: the
# socket event namespace is per-site, not per-app, and every consumer is the same
# POS client that already listens for the frozen six. When the freeze lifts these
# two should move into ``jarz_pos/constants.py`` and its Dart mirror, which is the
# only place a *Flutter-consumed* event name belongs long term.

class LOCAL_WS_EVENTS:
    #: Per-branch live position feed for the ops board.
    COURIER_LOCATION_UPDATED = "jarz_pos_courier_location_updated"
    #: Stale-ping watchdog and anomaly detector findings, to the branch's ops.
    COURIER_ALERT = "jarz_pos_courier_alert"


# ── Push message types (FCM ``data.type``) ─────────────────────────────
#
# The client switches on ``data["type"]``. Kept as constants because a typo here
# is silent: FCM happily delivers a payload the handset then ignores.

class PUSH_TYPE:
    NEW_ASSIGNMENT = "courier_new_assignment"
    RUN_CHANGED = "courier_run_changed"
    DEPOSIT_CONFIRMED = "courier_deposit_confirmed"
    STALE_PING = "courier_stale_ping"


# ── Query limits ────────────────────────────────────────────────────────

class QUERY_LIMITS:
    RUN_STOPS = 200
    STATEMENT_ROWS = 200
    SETTLEMENT_HISTORY = 50
    PENDING_DEPOSITS = 100
    PROOFS_PER_STOP = 20
    #: One flush of an offline ping queue. A courier who spent a morning in a
    #: dead zone at one fix / 5 s has ~700 fixes to hand over; the cap is above
    #: that but far below "a client can make the server chew on 100k points".
    PINGS_PER_BATCH = 1000
    #: Rows a single scheduled detector pass will look at.
    RUNS_PER_SWEEP = 200
    PROOFS_PER_CONSENSUS_PASS = 2000
    PUSH_TOKENS_PER_SEND = 200


__all__ = [
    "ANOMALY_STATUS",
    "ANOMALY_TYPE",
    "COURIER_PARTY_TYPES",
    "DEPOSIT_METHODS",
    "DEPOSIT_STATUS",
    "DOCTYPES",
    "DUTY_STATUS",
    "GEO_SOURCE_COURIER_VERIFIED",
    "GEO_SOURCE_CUSTOMER_PIN",
    "INVOICE_STATE",
    "LOCAL_WS_EVENTS",
    "PROOF_TYPES",
    "PUSH_TYPE",
    "QUERY_LIMITS",
    "ROLES",
    "RUN_STATUS",
    "SEVERITY",
    "WS_EVENTS",
]

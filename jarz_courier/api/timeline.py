"""Delivery timeline of one order: who carried it, when each step happened, where time went.

Read-only and assembled from records that already exist - the Invoice State Log, the
delivery stamps on the Sales Invoice and the ``Courier Stop Event`` rows - so it costs
nothing at write time and cannot disagree with the ledger.

An order that was never dispatched simply has fewer events; every segment needs both of
its ends, so a missing step drops the segment instead of inventing a duration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_courier.api.tracking import _ensure_ops_permission
from jarz_courier.constants import DOCTYPES, INVOICE_STATE, STOP_EVENT
from jarz_courier.services import pos_bridge

_STATE_LOG = "Invoice State Log"
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

_INVOICE_FIELDS = (
    "creation",
    "pos_profile",
    "custom_kanban_profile",
    "custom_courier_party_type",
    "custom_courier_party",
    "custom_accepted_on",
    "custom_arrived_at",
    "custom_delivered_at",
    "custom_leg_started_at",
    "custom_leg_ended_at",
    "custom_delivery_failure_reason",
    "custom_delivery_latitude",
    "custom_delivery_longitude",
)

#: ``(key, label)`` in the order a delivery normally runs.
_LABELS = {
    "created": "Order created",
    "accepted": "Accepted",
    "in_progress": "In progress",
    "out_for_delivery": "Out for delivery",
    "left_branch": "Left the branch",
    "leg_started": "Leg started",
    "arrived": "Arrived",
    "delivered": "Delivered",
    "failed": "Delivery failed",
    "back_at_branch": "Back at the branch",
}

#: ``(key, label, from-event, to-event(s), target minutes)``. ``travel`` starts at the
#: branch exit when the geofence saw it and at the app's leg start otherwise.
_SEGMENTS = (
    ("prep", "Preparation", ("created",), "out_for_delivery", 30),
    ("dispatch_wait", "Waiting to leave", ("out_for_delivery",), "left_branch", 5),
    ("travel", "Travel", ("left_branch", "leg_started"), "arrived", None),
    ("at_door", "At the door", ("arrived",), "delivered", 5),
    ("return", "Return to branch", ("delivered",), "back_at_branch", None),
)

_STOP_EVENT_KEYS = {
    STOP_EVENT.LEFT_BRANCH: "left_branch",
    STOP_EVENT.ARRIVED: "arrived",
    STOP_EVENT.BACK_AT_BRANCH: "back_at_branch",
}


@frappe.whitelist(allow_guest=False)
def get_order_timeline(invoice_id: str) -> Dict[str, Any]:
    """Events and timed segments for one invoice. Ops roles, branch-scoped."""
    _ensure_ops_permission()
    invoice_id = str(invoice_id or "").strip()
    if not invoice_id:
        frappe.throw(_("invoice_id is required"), frappe.ValidationError)

    row = _load_invoice(invoice_id)
    if not row:
        frappe.throw(_("Sales Invoice {0} was not found").format(invoice_id), frappe.DoesNotExistError)

    branch = row.get("custom_kanban_profile") or row.get("pos_profile")
    if branch not in set(pos_bridge.get_user_pos_profiles() or []):
        frappe.throw(_("You are not assigned to this order's branch"), frappe.PermissionError)

    try:
        events = _build_events(invoice_id, row)
        segments = _build_segments(events)
        return {
            "invoice": invoice_id,
            "courier": _courier(row),
            "events": events,
            "segments": segments,
            "total_minutes": _total_minutes(events),
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier get_order_timeline failed")
        frappe.throw(_("Could not build the timeline for {0}").format(invoice_id))


# ─────────────────────────────────────────────────────────────────────────────
# Sources
# ─────────────────────────────────────────────────────────────────────────────

def _load_invoice(invoice_id: str) -> Optional[Dict[str, Any]]:
    fields = ["name"]
    try:
        meta = frappe.get_meta("Sales Invoice")
    except Exception:
        meta = None
    for fieldname in _INVOICE_FIELDS:
        if fieldname in ("creation", "pos_profile"):
            fields.append(fieldname)
            continue
        try:
            if meta is not None and meta.get_field(fieldname):
                fields.append(fieldname)
        except Exception:
            continue
    return frappe.db.get_value("Sales Invoice", invoice_id, fields, as_dict=True)


def _state_log(invoice_id: str) -> List[Dict[str, Any]]:
    try:
        return frappe.get_all(
            _STATE_LOG,
            filters={"sales_invoice": invoice_id},
            fields=["to_state", "changed_at"],
            order_by="changed_at asc",
            limit=200,
        ) or []
    except Exception:
        # A jarz_pos doctype; a site one deploy behind simply has no log.
        return []


def _stop_events(invoice_id: str) -> Dict[str, Dict[str, Any]]:
    rows = frappe.get_all(
        DOCTYPES.COURIER_STOP_EVENT,
        filters={"invoice": invoice_id},
        fields=["event", "at", "latitude", "longitude", "source"],
        order_by="at asc",
        limit=20,
    ) or []
    return {_STOP_EVENT_KEYS[r.get("event")]: r for r in rows if r.get("event") in _STOP_EVENT_KEYS}


def _build_events(invoice_id: str, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    log = _state_log(invoice_id)
    stops = _stop_events(invoice_id)
    found: Dict[str, Dict[str, Any]] = {}

    def put(key: str, at: Any, *, source: str, lat: Any = None, lng: Any = None) -> None:
        stamp = _to_datetime(at)
        if stamp is None or key in found:
            return
        found[key] = {
            "key": key,
            "label": _(_LABELS[key]),
            "at": stamp.strftime(_TS_FORMAT),
            "latitude": _num(lat),
            "longitude": _num(lng),
            "source": source,
            "_dt": stamp,
        }

    put("created", row.get("creation"), source="invoice")
    put("accepted", row.get("custom_accepted_on"), source="invoice")
    put("in_progress", _first_state(log, "In Progress"), source="state_log")
    # The last dispatch counts: an order sent back and re-sent restarts the clock.
    put("out_for_delivery", _last_state(log, INVOICE_STATE.OUT_FOR_DELIVERY), source="state_log")

    left = stops.get("left_branch") or {}
    put("left_branch", left.get("at"), source=left.get("source") or "geofence",
        lat=left.get("latitude"), lng=left.get("longitude"))
    put("leg_started", row.get("custom_leg_started_at"), source="invoice")

    arrived = stops.get("arrived") or {}
    if row.get("custom_arrived_at"):
        put("arrived", row.get("custom_arrived_at"), source="invoice",
            lat=arrived.get("latitude"), lng=arrived.get("longitude"))
    else:
        put("arrived", arrived.get("at"), source=arrived.get("source") or "geofence",
            lat=arrived.get("latitude"), lng=arrived.get("longitude"))

    delivered_at = row.get("custom_delivered_at") or _last_state(log, INVOICE_STATE.DELIVERED)
    put("delivered", delivered_at, source="invoice" if row.get("custom_delivered_at") else "state_log",
        lat=row.get("custom_delivery_latitude"), lng=row.get("custom_delivery_longitude"))
    if "delivered" not in found and row.get("custom_delivery_failure_reason"):
        put("failed", row.get("custom_leg_ended_at"), source="invoice",
            lat=row.get("custom_delivery_latitude"), lng=row.get("custom_delivery_longitude"))

    back = stops.get("back_at_branch") or {}
    put("back_at_branch", back.get("at"), source=back.get("source") or "geofence",
        lat=back.get("latitude"), lng=back.get("longitude"))

    return sorted(found.values(), key=lambda e: (e["_dt"], list(_LABELS).index(e["key"])))


def _build_segments(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key = {e["key"]: e for e in events}
    out: List[Dict[str, Any]] = []
    for key, label, start_keys, end_key, target in _SEGMENTS:
        start = next((by_key[k] for k in start_keys if k in by_key), None)
        end = by_key.get(end_key)
        if not start or not end:
            continue
        seconds = (end["_dt"] - start["_dt"]).total_seconds()
        if seconds < 0:
            continue
        minutes = int(round(seconds / 60.0))
        out.append({
            "key": key,
            "label": _(label),
            "from": start["at"],
            "to": end["at"],
            "minutes": minutes,
            "target_minutes": target,
            "over": bool(target is not None and minutes > target),
        })
    # ``_dt`` was only for ordering and arithmetic.
    for event in events:
        event.pop("_dt", None)
    return out


def _total_minutes(events: List[Dict[str, Any]]) -> Optional[int]:
    by_key = {e["key"]: e["at"] for e in events}
    start, end = _to_datetime(by_key.get("created")), _to_datetime(by_key.get("delivered"))
    if not start or not end or end < start:
        return None
    return int(round((end - start).total_seconds() / 60.0))


def _courier(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    party_type, party = row.get("custom_courier_party_type"), row.get("custom_courier_party")
    if not (party_type and party):
        return None
    name_field = {"Employee": "employee_name", "Supplier": "supplier_name"}.get(party_type)
    name = None
    if name_field:
        try:
            name = frappe.db.get_value(party_type, party, name_field)
        except Exception:
            name = None
    return {"party_type": party_type, "party": party, "name": name or party}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _first_state(log: List[Dict[str, Any]], state: str) -> Any:
    for entry in log:
        if entry.get("to_state") == state:
            return entry.get("changed_at")
    return None


def _last_state(log: List[Dict[str, Any]], state: str) -> Any:
    for entry in reversed(log):
        if entry.get("to_state") == state:
            return entry.get("changed_at")
    return None


def _to_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], _TS_FORMAT)
    except ValueError:
        return None


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

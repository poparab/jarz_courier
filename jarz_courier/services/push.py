"""FCM push for couriers and for the ops desk.

Registration is already done: ``api/device.register_device`` stores the token on
``Courier Device.fcm_token`` and the courier app re-registers on every cold start,
so a rotated token reaches the server without a separate flow. This module is the
send half.

Why a DATA message and not a notification
-----------------------------------------
Android treats the two completely differently, and the difference is the whole
reason this module exists rather than reusing jarz_pos's sender:

* A message carrying a ``notification`` block is handed to the **system tray** by
  the OS. While the app is backgrounded, ``onMessageReceived`` is **never called** —
  the app does not run, and cannot sync, refresh a run sheet or start location
  tracking. It only wakes if the courier taps the tray item.
* A **data-only** message with ``priority: high`` is delivered to the app's message
  handler even in the background, and on Android is exempted from Doze batching. That
  is the only server-side lever that can make a backgrounded courier app do
  something.

jarz_pos's ``_send_fcm_notifications`` always attaches a ``notification`` block
(that is correct for its purpose — POS staff want the tray alert), so it cannot be
used as a wake signal. Rather than reach into a private function of another app and
bend it, this module builds its own message and asks jarz_pos only for the one thing
it genuinely owns: whether Firebase is initialised, via the public
``health_check_firebase``. Credential resolution stays in exactly one place.

The wake attempt is an attempt, not a guarantee
-----------------------------------------------
If the courier swiped the app away or the OEM battery manager force-stopped it,
Android drops the message on the floor no matter what priority it carries. No
server-side trick changes that. This is why the stale-ping watchdog exists and why
its alert goes to a **human** — the push is the cheap path, and the phone call is
the reliable one.

``firebase-admin`` is imported defensively and is **not** declared as a dependency of
this app. It is jarz_pos's dependency (``jarz_pos/requirements.txt``), jarz_pos is a
``required_apps`` entry, and both apps run in the same interpreter — so the package
is always present when it matters. Pinning a second copy here would risk resolving a
different version than the one jarz_pos's Firebase init was tested against, for zero
gain.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

import frappe

from jarz_courier.constants import DOCTYPES, PUSH_TYPE, QUERY_LIMITS
from jarz_courier.services import pos_bridge

try:  # pragma: no cover - present via jarz_pos on every real server
    from firebase_admin import messaging

    MESSAGING_AVAILABLE = True
except Exception:  # noqa: BLE001 - ImportError or a partially installed SDK
    messaging = None  # type: ignore[assignment]
    MESSAGING_AVAILABLE = False

#: How long FCM should keep trying to deliver. Short on purpose: a run-changed
#: notice that lands 40 minutes late is worse than one that never lands, because the
#: courier acts on stale information. The client polls anyway.
DEFAULT_TTL_SEC = 10 * 60

#: Longer for an ops alert, which stays true until someone deals with it.
ALERT_TTL_SEC = 30 * 60

#: Substrings FCM uses for "this token is dead". Matched on the exception's class
#: name and message rather than by catching a specific SDK class, because the
#: firebase-admin exception hierarchy has been reshuffled between majors and a
#: hard-coded class name turns an SDK bump into an unnoticed stop-sending bug.
_DEAD_TOKEN_MARKERS = (
    "unregistered",
    "registration-token-not-registered",
    "invalid-registration-token",
    "invalid-argument",
    "senderidmismatch",
    "sender-id-mismatch",
    "notregistered",
)


def _logger():
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


# ─────────────────────────────────────────────────────────────────────────────
# Token resolution
# ─────────────────────────────────────────────────────────────────────────────

def courier_device_tokens(party_type: str, party: str) -> List[Dict[str, str]]:
    """Active devices for a courier, as ``[{"name", "token"}]``.

    Only ``is_active`` rows. ``unbind_device`` clears the token when a handset is
    unbound precisely so this query cannot push a courier's assignments to a phone
    that has been handed to somebody else.
    """
    if not (party_type and party):
        return []
    try:
        rows = frappe.get_all(
            DOCTYPES.COURIER_DEVICE,
            filters={"party_type": party_type, "party": party, "is_active": 1},
            fields=["name", "fcm_token"],
            order_by="bound_on desc",
            limit=5,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: device token lookup failed")
        return []

    return [
        {"name": row["name"], "token": str(row.get("fcm_token") or "").strip()}
        for row in rows
        if str(row.get("fcm_token") or "").strip()
    ]


def user_tokens(users: Sequence[str]) -> List[str]:
    """POS-app tokens for a set of users, read from jarz_pos's device registry.

    Read-only across the boundary, deliberately. jarz_pos owns ``Jarz Mobile Device``
    and owns disabling a dead token on it; this app pushes to them and, when one
    turns out to be dead, logs and moves on rather than writing to another app's
    table. Two writers pruning the same registry with two different rules is how a
    live POS user stops receiving order alerts.
    """
    cleaned = sorted({str(u or "").strip() for u in users or []} - {"", "Guest"})
    if not cleaned:
        return []
    try:
        rows = frappe.get_all(
            "Jarz Mobile Device",
            filters={"user": ["in", cleaned], "enabled": 1},
            fields=["token"],
            limit=QUERY_LIMITS.PUSH_TOKENS_PER_SEND,
        ) or []
    except Exception:
        # The doctype belongs to jarz_pos. A site without it is a deploy-order
        # problem, not a reason to fail the alert that triggered this.
        frappe.log_error(frappe.get_traceback(), "jarz_courier: ops token lookup failed")
        return []

    return sorted({str(row.get("token") or "").strip() for row in rows} - {""})


# ─────────────────────────────────────────────────────────────────────────────
# Transport
# ─────────────────────────────────────────────────────────────────────────────

def normalise_data(payload: Dict[str, Any]) -> Dict[str, str]:
    """FCM data values must all be strings.

    A non-string value does not raise a helpful error — the SDK rejects the whole
    message, so one stray ``int`` silently drops the entire push. ``None`` keys are
    removed rather than sent as ``"None"``, which the client would have to special-case.
    """
    result: Dict[str, str] = {}
    for key, value in (payload or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            result[str(key)] = "1" if value else "0"
        else:
            result[str(key)] = str(value)
    return result


def send(
    tokens: Sequence[str],
    *,
    data: Dict[str, Any],
    ttl_sec: int = DEFAULT_TTL_SEC,
    collapse_key: Optional[str] = None,
    on_dead_token: Optional[Any] = None,
) -> Dict[str, Any]:
    """Send one high-priority, data-only message per token. Never raises.

    ``collapse_key`` matters more than it looks: without it, a courier who comes back
    online after an hour receives every queued run-changed message in sequence and
    the app processes six stale states before the current one. With it, FCM keeps only
    the newest message of that class per device.

    ``on_dead_token`` is called with each token FCM rejected as unregistered, so the
    caller can prune its own registry. It is a callback rather than a hard-coded
    write because the two callers own different tables and only one of them is ours.
    """
    result = {
        "ok": False,
        "status": "pending",
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "dead_tokens": 0,
    }

    unique = sorted({str(t or "").strip() for t in tokens or []} - {""})
    if not unique:
        result.update(ok=True, status="skipped_no_tokens")
        return result

    if not MESSAGING_AVAILABLE:
        result["status"] = "skipped_sdk_unavailable"
        _logger().warning(
            "jarz_courier: firebase-admin is not importable, courier push disabled. "
            "It ships with jarz_pos — check that app's install."
        )
        return result

    readiness = pos_bridge.ensure_push_ready()
    if not readiness.get("ok"):
        result["status"] = "skipped_not_initialised"
        _logger().warning(
            f"jarz_courier: Firebase not ready ({readiness.get('reason')}), push skipped"
        )
        return result

    payload = normalise_data(data)
    batch = unique[: QUERY_LIMITS.PUSH_TOKENS_PER_SEND]
    result["attempted"] = len(batch)

    for token in batch:
        try:
            messaging.send(_build_message(token, payload, ttl_sec, collapse_key))
            result["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - per-token accounting is the point
            result["failed"] += 1
            if _is_dead_token(exc):
                result["dead_tokens"] += 1
                _logger().info(f"jarz_courier: dropping dead push token ({type(exc).__name__})")
                if callable(on_dead_token):
                    try:
                        on_dead_token(token)
                    except Exception:
                        pass
            else:
                frappe.log_error(
                    f"jarz_courier push failed: {exc}", "jarz_courier: FCM send error"
                )

    result["ok"] = result["failed"] == 0
    result["status"] = (
        "sent" if result["failed"] == 0 else ("partial" if result["sent"] else "failed")
    )
    return result


def _build_message(token: str, data: Dict[str, str], ttl_sec: int, collapse_key: Optional[str]):
    """A data-only message. **There is no ``notification=`` argument, on purpose.**

    Adding one would move delivery to the system tray and stop the app's handler from
    running in the background — see the module docstring. The client is responsible
    for raising a local notification if the payload deserves one, which also lets it
    localise the text; server strings here are English and would break the Arabic UI
    if surfaced verbatim.
    """
    android = messaging.AndroidConfig(
        priority="high",
        ttl=timedelta(seconds=int(ttl_sec)),
        collapse_key=collapse_key,
    )
    apns = messaging.APNSConfig(
        headers={"apns-priority": "5", "apns-push-type": "background"},
        payload=messaging.APNSPayload(aps=messaging.Aps(content_available=True)),
    )
    return messaging.Message(data=data, android=android, apns=apns, token=token)


def _is_dead_token(exc: Exception) -> bool:
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _DEAD_TOKEN_MARKERS)


def _clear_device_token(device_name: str) -> None:
    """Blank a dead token on our own ``Courier Device`` row.

    ``db.set_value`` with ``update_modified=False``: this is housekeeping triggered by
    a background send, and bumping ``modified`` would hand a
    ``TimestampMismatchError`` to a courier whose app is mid-registration.
    """
    try:
        frappe.db.set_value(
            DOCTYPES.COURIER_DEVICE, device_name, "fcm_token", None, update_modified=False
        )
    except Exception:
        _logger().warning(f"jarz_courier: could not clear token on {device_name}")


def send_to_courier(
    *,
    party_type: str,
    party: str,
    data: Dict[str, Any],
    ttl_sec: int = DEFAULT_TTL_SEC,
    collapse_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Push to every active handset bound to a courier."""
    devices = courier_device_tokens(party_type, party)
    if not devices:
        return {"ok": True, "status": "skipped_no_device", "attempted": 0, "sent": 0}

    by_token = {device["token"]: device["name"] for device in devices}

    def prune(token: str) -> None:
        name = by_token.get(token)
        if name:
            _clear_device_token(name)

    return send(
        list(by_token),
        data=data,
        ttl_sec=ttl_sec,
        collapse_key=collapse_key,
        on_dead_token=prune,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The four events B8 asks for
# ─────────────────────────────────────────────────────────────────────────────

def notify_new_assignment(
    *,
    party_type: str,
    party: str,
    branch: str,
    invoices: Sequence[str],
    display_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """"You have new stops." The one push a courier must not miss.

    Not collapsed. Every other message here is a state refresh where only the newest
    matters, but two separate assignments are two separate facts — collapsing them
    would tell a courier about the second order and silently drop the first.
    """
    if not invoices:
        return {"ok": True, "status": "skipped_nothing_new"}

    return send_to_courier(
        party_type=party_type,
        party=party,
        data={
            "type": PUSH_TYPE.NEW_ASSIGNMENT,
            "branch": branch,
            "count": len(invoices),
            # Truncated: FCM caps a message at 4 KB and a courier handed 40 orders
            # would otherwise blow the limit and receive nothing at all.
            "invoices": ",".join(str(i) for i in list(invoices)[:20]),
            "display_ids": ",".join(str(i) for i in list(display_ids)[:20]),
        },
    )


def notify_run_changed(
    *, party_type: str, party: str, branch: str, added: int = 0, removed: int = 0
) -> Dict[str, Any]:
    """"Your run sheet changed, refetch it."

    Collapsed per courier: the payload is a hint to refetch, so an older one is
    strictly redundant and processing a queue of them just replays stale counts.
    """
    return send_to_courier(
        party_type=party_type,
        party=party,
        data={
            "type": PUSH_TYPE.RUN_CHANGED,
            "branch": branch,
            "added": added,
            "removed": removed,
        },
        collapse_key=f"run_changed::{party}",
    )


def notify_deposit_confirmed(
    *,
    party_type: str,
    party: str,
    branch: Optional[str],
    declaration: str,
    amount: Any,
    reference: Optional[str] = None,
) -> Dict[str, Any]:
    """"Your hand-over was accepted." The courier's receipt.

    Never collapsed, and it deliberately carries the declaration name rather than
    just an amount: this is the message a courier screenshots when a hand-over is
    later disputed, and an amount with no reference proves nothing.
    """
    return send_to_courier(
        party_type=party_type,
        party=party,
        data={
            "type": PUSH_TYPE.DEPOSIT_CONFIRMED,
            "branch": branch,
            "declaration": declaration,
            "amount": amount,
            "reference": reference,
        },
        ttl_sec=ALERT_TTL_SEC,
    )


def notify_stale_ping(
    *, run: Dict[str, Any], silent_minutes: int, reason: str
) -> Dict[str, Any]:
    """Tell the branch's ops users that a run went quiet.

    Sent to **ops, not the courier**. Pushing "your app stopped reporting" to a phone
    whose app has stopped reporting is a message to nobody; the whole point of the
    watchdog is that the failure it detects is the failure that prevents its own
    delivery.
    """
    branch = str(run.get("branch") or "")
    recipients = pos_bridge.resolve_branch_recipients([branch] if branch else [])
    tokens = user_tokens(recipients)

    result = send(
        tokens,
        data={
            "type": PUSH_TYPE.STALE_PING,
            "run": run.get("name"),
            "branch": branch,
            "party_type": run.get("party_type"),
            "party": run.get("party"),
            "silent_minutes": silent_minutes,
            "reason": reason,
        },
        ttl_sec=ALERT_TTL_SEC,
        collapse_key=f"stale_ping::{run.get('name')}",
    )
    result["recipients"] = len(recipients)
    return result

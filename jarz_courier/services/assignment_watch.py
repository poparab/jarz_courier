"""Detect run-sheet changes and push them to the courier.

Why this is a poll and not a hook
---------------------------------
A courier is assigned by writing ``Sales Invoice.custom_courier_party``, and that
write happens inside ``jarz_pos``. There is no in-boundary way to be told about it:

* ``hooks.doc_events`` on Sales Invoice is out. ``hooks.py`` says why — a doc_event
  here would make this app a second, untested writer/observer running inside
  jarz_pos's own transactions, and an exception in it would roll back a POS
  operation that has nothing to do with couriers.
* jarz_pos calling into this app is out. Contract §9 makes the dependency one-way:
  ``jarz_courier`` may import ``jarz_pos``, never the reverse.

So the only mechanism available on this side of the boundary is to look. The cost is
latency — up to one scheduler tick between assignment and push — and that is the
honest trade rather than a shortcut. A jarz_pos-side hook that called a documented
courier-notification endpoint would be lower latency and is the right long-term
answer; it needs a lane in that repo, and it is noted in the handover.

The snapshot rule that stops the spam
-------------------------------------
``None`` (never observed) and ``[]`` (observed, empty) are different, and collapsing
them is the bug this design exists to avoid: on the very first sweep after a deploy,
treating "no snapshot" as "empty run" tells every courier on shift that all of today's
orders are brand new. The first pass therefore records and stays silent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import frappe

from jarz_courier.constants import DOCTYPES, DUTY_STATUS, QUERY_LIMITS, RUN_STATUS
from jarz_courier.services import location_cache, push, run_sheet

#: Couriers examined per pass. Well above any realistic simultaneous-shift count;
#: it exists so a data problem cannot turn one tick into a full table scan.
MAX_COURIERS_PER_SWEEP = 200


def _logger():
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


def sweep(*, limit: int = MAX_COURIERS_PER_SWEEP) -> Dict[str, Any]:
    """Compare each working courier's run sheet against the last snapshot.

    Never raises: it runs from the scheduler, and a job that throws on every tick
    ends up disabled, which would switch assignment notifications off silently.
    """
    summary = {"couriers": 0, "assigned": 0, "changed": 0, "first_seen": 0}

    try:
        couriers = active_couriers(limit=limit)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: active courier lookup failed")
        return summary

    for party_type, party, branch in couriers:
        summary["couriers"] += 1
        try:
            outcome = _sweep_one(party_type, party, branch)
            if outcome:
                summary[outcome] = summary.get(outcome, 0) + 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"jarz_courier: assignment sweep failed for {party}"
            )

    return summary


def _sweep_one(party_type: str, party: str, branch: str) -> Optional[str]:
    """One courier: diff, snapshot, push."""
    run = run_sheet.get_run(
        party_type=party_type, party=party, branches=[branch], limit=QUERY_LIMITS.RUN_STOPS
    )
    stops = run.get("stops") or []
    invoices = [str(stop.get("invoice")) for stop in stops if stop.get("invoice")]

    previous = location_cache.read_run_snapshot(branch, party)
    location_cache.write_run_snapshot(branch, party, invoices)

    if previous is None:
        # First observation for this courier. Record and say nothing.
        return "first_seen"

    diff = diff_runs(previous, invoices)
    if not diff["changed"]:
        return None

    if diff["added"]:
        display_ids = [
            str(stop.get("display_id") or stop.get("invoice"))
            for stop in stops
            if str(stop.get("invoice")) in set(diff["added"])
        ]
        push.notify_new_assignment(
            party_type=party_type,
            party=party,
            branch=branch,
            invoices=diff["added"],
            display_ids=display_ids,
        )
        _logger().info(
            f"jarz_courier: {len(diff['added'])} new stop(s) for {party} on {branch}"
        )
        return "assigned"

    # Removed or resequenced. A refetch hint is all the client needs — it re-reads the
    # run sheet, which is the authority. Sending the diff itself would give the client
    # two sources of truth to reconcile.
    push.notify_run_changed(
        party_type=party_type,
        party=party,
        branch=branch,
        added=len(diff["added"]),
        removed=len(diff["removed"]),
    )
    return "changed"


def diff_runs(previous: Sequence[str], current: Sequence[str]) -> Dict[str, Any]:
    """What changed between two ordered run-sheet snapshots.

    Order is compared as well as membership, because resequencing a run *is* a change
    the courier has to see — the whole point of ``custom_delivery_sequence`` is that
    the order is instructions, not presentation. A set-only comparison would silently
    swallow "deliver the far one first".
    """
    previous_list = [str(i) for i in previous or []]
    current_list = [str(i) for i in current or []]
    previous_set = set(previous_list)
    current_set = set(current_list)

    added = [i for i in current_list if i not in previous_set]
    removed = [i for i in previous_list if i not in current_set]
    resequenced = not added and not removed and previous_list != current_list

    return {
        "added": added,
        "removed": removed,
        "resequenced": resequenced,
        "changed": bool(added or removed or resequenced),
    }


def active_couriers(*, limit: int = MAX_COURIERS_PER_SWEEP) -> List[Tuple[str, str, str]]:
    """``(party_type, party, branch)`` for every courier working right now.

    "Working" is an open duty **or** an open tracked run. Both, because they answer
    slightly different questions and neither is reliably present alone: a courier can
    be tracking before tapping Start Duty (the run opens on the first ping), and a
    courier can be on duty with location permission denied (a duty, no run). Taking
    the union means a missing one of the two does not make a courier invisible to
    their own assignment notifications.

    A courier not in either set gets no push. That is acceptable: they have to open
    the app to start a duty, and the app fetches the run sheet when it opens.
    """
    seen: Dict[Tuple[str, str], str] = {}

    for doctype, status_field, status_value in (
        (DOCTYPES.COURIER_DUTY, "status", DUTY_STATUS.OPEN),
        (DOCTYPES.COURIER_RUN, "status", RUN_STATUS.OPEN),
    ):
        try:
            rows = frappe.get_all(
                doctype,
                filters={status_field: status_value},
                fields=["party_type", "party", "branch"],
                limit=limit,
            ) or []
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"jarz_courier: {doctype} sweep lookup failed"
            )
            continue

        for row in rows:
            party_type = str(row.get("party_type") or "").strip()
            party = str(row.get("party") or "").strip()
            branch = str(row.get("branch") or "").strip()
            if not (party_type and party and branch):
                continue
            seen.setdefault((party_type, party), branch)

    return [(party_type, party, branch) for (party_type, party), branch in seen.items()][:limit]


def scheduled_sweep() -> Dict[str, Any]:
    """Scheduler entry point. Swallows everything."""
    try:
        return sweep()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: scheduled_sweep failed")
        return {"error": True}

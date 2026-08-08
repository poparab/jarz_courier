"""Consensus pins: when several couriers agree on a door, promote the Address pin.

The problem this solves is ordinary and expensive. Most delivery addresses in this
system are pinned from a WooCommerce checkout or a pasted maps link — a
``customer_pin`` at best, a territory centroid at worst — and a wrong pin costs every
future courier the same ten minutes on the phone. Meanwhile the right answer is
already being collected: every ``Delivery Proof`` records where a courier physically
stood when the order was handed over. Two or three of those agreeing is better
evidence than anything the customer typed.

**This app is not an authorised writer of the Address geo fields.** Contract §3 names
exactly two, and neither is ``jarz_courier``. So nothing here touches an Address:
it computes a candidate and hands it to
``jarz_pos.services.geo_resolution.set_address_pin`` through ``pos_bridge``, which
owns the never-downgrade ladder, the accuracy invariant, the manual-override role gate
and the "never write a WooCommerce trigger field" guard. Writing the six columns
directly from here would work on the first run and quietly break the ladder for
everyone.

A rejected promotion is a normal outcome, not an error: a manager's
``manual_override`` outranks any consensus by design, and this job runs nightly.

Independence is the whole safety property
-----------------------------------------
"Three fixes within 40 m" is trivially forgeable by one courier standing in the same
wrong place three times, so agreement alone is not enough — the fixes have to be
*independent*. The rule below therefore counts distinct invoices **and** distinct
couriers, and demands more evidence when it all came from one person.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

import frappe
from frappe.utils import add_to_date, now_datetime

from jarz_courier.constants import (
    DOCTYPES,
    GEO_SOURCE_COURIER_VERIFIED,
    QUERY_LIMITS,
)
from jarz_courier.services import geo_track, pos_bridge

#: Two independent fixes this close describe the same doorway. Wider and it starts
#: agreeing on the wrong building in a dense street; tighter and consumer GPS never
#: agrees at all.
CONSENSUS_RADIUS_M = 40.0

#: A proof whose own accuracy is worse than this is not evidence of a door.
MAX_PROOF_ACCURACY_M = geo_track.MAX_ACCURACY_M

#: Minimum distinct deliveries in a cluster before it can promote anything.
MIN_INDEPENDENT_DELIVERIES = 2

#: ...and if every one of them came from the SAME courier, this many instead. One
#: courier repeatedly standing in the same wrong spot produces a perfectly tight
#: cluster; requiring a third delivery makes that an expensive lie rather than a free
#: one. It does not make it impossible — nothing here can — which is why
#: ``manual_override`` sits above ``courier_verified`` on the ladder.
MIN_DELIVERIES_SINGLE_COURIER = 3

#: How far back to look for proofs. A month is long enough to accumulate repeat
#: deliveries to the same address and short enough that a customer who has moved does
#: not keep voting.
LOOKBACK_DAYS = 30

#: A consensus tighter than this rounds to 0, and 0 means "no accuracy reported" on
#: ``custom_geo_accuracy_m`` (contract §3). Writing a genuinely excellent pin as
#: "unknown accuracy" would throw away the best information in the system, so the
#: floor keeps it expressible.
MIN_ACCURACY_FLOOR_M = 1.0


def _logger():
    log = frappe.logger("jarz_courier")
    try:
        log.setLevel(logging.INFO)
    except Exception:
        pass
    return log


# ─────────────────────────────────────────────────────────────────────────────
# The decision — pure, no frappe, fully unit-testable
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_cluster(members: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Should this cluster of door fixes become a ``courier_verified`` pin?

    Each member needs ``lat``, ``lng``, ``invoice`` and ``party``. Returns a verdict
    dict; ``promote`` is the answer and ``reason`` is why, so a caller can log a
    refusal in a form somebody can act on.

    The accuracy written back is the **cluster radius**, not any handset's
    self-reported figure: how far apart independent observers of one door actually
    stood is a better measure of that pin's uncertainty than a single device's opinion
    of itself. When no member reported an accuracy at all, ``0.0`` is returned — which
    on ``custom_geo_accuracy_m`` means "not reported", never "accurate to 0 m".
    """
    usable = [
        m
        for m in members or []
        if geo_track.is_valid_coordinate(m.get("lat"), m.get("lng")) and m.get("invoice")
    ]
    invoices = {str(m["invoice"]) for m in usable}
    couriers = {str(m.get("party") or "") for m in usable} - {""}

    verdict: Dict[str, Any] = {
        "promote": False,
        "reason": "",
        "fix_count": len(usable),
        "invoice_count": len(invoices),
        "courier_count": len(couriers),
        "latitude": None,
        "longitude": None,
        "accuracy_m": 0.0,
        "radius_m": 0.0,
        "invoices": sorted(invoices),
    }

    if len(invoices) < MIN_INDEPENDENT_DELIVERIES:
        verdict["reason"] = (
            f"only {len(invoices)} independent delivery/deliveries, need {MIN_INDEPENDENT_DELIVERIES}"
        )
        return verdict

    if len(couriers) < 2 and len(invoices) < MIN_DELIVERIES_SINGLE_COURIER:
        verdict["reason"] = (
            f"all {len(invoices)} deliveries came from one courier, need "
            f"{MIN_DELIVERIES_SINGLE_COURIER} in that case"
        )
        return verdict

    hub = geo_track.centroid(usable)
    if hub is None:
        verdict["reason"] = "no usable coordinates"
        return verdict

    radius = geo_track.cluster_radius_m(usable)
    any_accuracy_reported = any(geo_track.accuracy_is_known(m.get("accuracy")) for m in usable)

    verdict.update(
        promote=True,
        reason="consensus",
        latitude=hub[0],
        longitude=hub[1],
        radius_m=radius,
        accuracy_m=max(radius, MIN_ACCURACY_FLOOR_M) if any_accuracy_reported else 0.0,
    )
    return verdict


# ─────────────────────────────────────────────────────────────────────────────
# The scheduled pass
# ─────────────────────────────────────────────────────────────────────────────

def promote_pins(
    *, days: int = LOOKBACK_DAYS, limit: int = QUERY_LIMITS.PROOFS_PER_CONSENSUS_PASS
) -> Dict[str, Any]:
    """Cluster recent proofs per Address and promote the ones that agree.

    Never raises — it runs from the scheduler, where a traceback per tick eventually
    gets the job disabled and switches this feature off without anybody noticing.
    """
    summary = {
        "addresses_examined": 0,
        "promoted": 0,
        "rejected_by_ladder": 0,
        "insufficient_consensus": 0,
        "skipped_already_verified": 0,
    }

    try:
        proofs = _candidate_proofs(days=days, limit=limit)
        if not proofs:
            return summary

        by_address = _group_by_address(proofs)
        verified_rank = pos_bridge.confidence_rank(GEO_SOURCE_COURIER_VERIFIED)

        for address_name, members in by_address.items():
            summary["addresses_examined"] += 1
            try:
                outcome = _promote_one(address_name, members, verified_rank)
                summary[outcome] = summary.get(outcome, 0) + 1
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"jarz_courier: consensus promotion failed for {address_name}",
                )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: consensus pass failed")

    _logger().info(f"jarz_courier: consensus pass {summary}")
    return summary


def _promote_one(address_name: str, members: List[Dict[str, Any]], verified_rank: int) -> str:
    """Evaluate one address and, if it qualifies, ask jarz_pos to write the pin."""
    geo = pos_bridge.get_address_geo(address_name)
    if not geo:
        # `{}` means the Address does not exist — distinct from an Address with no
        # pin, which is the normal first-write case and must be attempted.
        return "insufficient_consensus"

    # Already at courier_verified or above: the ladder would accept an equal rank, so
    # re-writing is legal, but there is nothing to gain and it would reset
    # `custom_geo_verified_on` every night for every address in the system.
    if verified_rank and int(geo.get("rank") or 0) >= verified_rank:
        return "skipped_already_verified"

    clusters = geo_track.cluster_points(members, radius_m=CONSENSUS_RADIUS_M)
    if not clusters:
        return "insufficient_consensus"

    verdict = evaluate_cluster(clusters[0])
    if not verdict["promote"]:
        _logger().info(
            f"jarz_courier: no consensus for {address_name} ({verdict['reason']})"
        )
        return "insufficient_consensus"

    result = pos_bridge.set_address_pin(
        address_name,
        latitude=verdict["latitude"],
        longitude=verdict["longitude"],
        source=GEO_SOURCE_COURIER_VERIFIED,
        accuracy_m=verdict["accuracy_m"],
        note=(
            f"Courier consensus from {verdict['invoice_count']} deliveries by "
            f"{verdict['courier_count']} courier(s); fixes agreed within "
            f"{verdict['radius_m']} m"
        ),
    ) or {}

    if result.get("accepted"):
        _logger().info(
            f"jarz_courier: promoted {address_name} to {GEO_SOURCE_COURIER_VERIFIED} "
            f"at {verdict['latitude']},{verdict['longitude']} radius={verdict['radius_m']}m "
            f"from {verdict['invoice_count']} deliveries"
        )
        return "promoted"

    # Not an error. A manual_override outranks consensus deliberately — an authorised
    # human with context must be able to fix a pin and have it stick.
    _logger().info(
        f"jarz_courier: {address_name} pin write not accepted "
        f"({result.get('reason')}, current {result.get('current_source')})"
    )
    return "rejected_by_ladder"


# ─────────────────────────────────────────────────────────────────────────────
# Gathering the evidence
# ─────────────────────────────────────────────────────────────────────────────

def _candidate_proofs(*, days: int, limit: int) -> List[Dict[str, Any]]:
    """Recent proofs that are admissible as door evidence.

    Three exclusions, each with a reason:

    * ``is_mocked`` — the ``Delivery Proof`` doctype says it outright: a mocked proof
      is stored but must never reach the pin consensus. A courier who can move the
      customer's map pin by spoofing their location has a much more interesting
      exploit than a fake delivery.
    * invalid coordinates — ``(0, 0)`` is what a handset reports with no fix, and
      averaging it in drags the centroid towards the Gulf of Guinea.
    * accuracy worse than ``MAX_PROOF_ACCURACY_M`` — a 300 m fix agrees with
      everything. Proofs with *no* reported accuracy are kept: older app builds never
      sent the field, and excluding them would mean the consensus never fires on the
      addresses with the longest delivery history.
    """
    since = add_to_date(now_datetime(), days=-int(days))
    try:
        rows = frappe.get_all(
            DOCTYPES.DELIVERY_PROOF,
            filters={"captured_at": [">=", since], "is_mocked": 0},
            fields=[
                "name",
                "sales_invoice",
                "party_type",
                "party",
                "latitude",
                "longitude",
                "accuracy_m",
                "captured_at",
            ],
            order_by="captured_at desc",
            limit=limit,
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: consensus proof lookup failed")
        return []

    admissible: List[Dict[str, Any]] = []
    for row in rows:
        if not geo_track.is_valid_coordinate(row.get("latitude"), row.get("longitude")):
            continue
        accuracy = row.get("accuracy_m")
        if geo_track.accuracy_is_known(accuracy) and float(accuracy) > MAX_PROOF_ACCURACY_M:
            continue
        admissible.append(
            {
                "proof": row.get("name"),
                "invoice": row.get("sales_invoice"),
                "party": row.get("party"),
                "party_type": row.get("party_type"),
                "lat": row.get("latitude"),
                "lng": row.get("longitude"),
                "accuracy": accuracy,
            }
        )
    return admissible


def _group_by_address(proofs: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket proofs by the delivery Address of their invoice.

    The invoice → address hop is one batched query rather than one per proof. A
    nightly pass over 2,000 proofs would otherwise be 2,000 round trips for a lookup
    the database can answer in a single ``IN``.
    """
    invoice_names = sorted({str(p.get("invoice") or "").strip() for p in proofs} - {""})
    if not invoice_names:
        return {}

    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters={"name": ["in", invoice_names]},
            fields=["name", "shipping_address_name", "customer_address"],
            limit=len(invoice_names),
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: invoice address batch failed")
        return {}

    address_of = {
        row["name"]: str(row.get("shipping_address_name") or row.get("customer_address") or "")
        for row in rows
    }

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for proof in proofs:
        address = address_of.get(str(proof.get("invoice") or ""), "")
        if not address:
            continue
        grouped.setdefault(address, []).append(proof)

    # An address with a single proof can never reach MIN_INDEPENDENT_DELIVERIES, so
    # dropping it here saves a `get_address_geo` round trip per one-off delivery —
    # which is most deliveries.
    return {
        address: members
        for address, members in grouped.items()
        if len({str(m.get("invoice")) for m in members}) >= MIN_INDEPENDENT_DELIVERIES
    }


def scheduled_promote() -> Dict[str, Any]:
    """Scheduler entry point. Swallows everything."""
    try:
        return promote_pins()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: scheduled_promote failed")
        return {"error": True}

"""Traveler Profile: bookings history (Duffel orders tagged with the traveler, via the Gateway) + AgentCore Memory,
as one record.
Plain code, not an agent: it is a lookup, and the desk loads it once per session (no model hop per turn)."""

import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import common


def summarize(actor_id, trips):
    routes, spend = Counter(), defaultdict(list)
    for t in trips:
        r = f"{t['origin']}-{t['destination']}"
        routes[r] += 1
        spend[r].append((Decimal(t["total"]), t["currency"]))
    return {
        "actor_id": actor_id,
        "trip_count": len(trips),
        "past_routes": [
            {
                "route": r,
                "count": n,
                "avg_total": str((sum(a for a, _ in spend[r]) / n).quantize(Decimal("0.01"))),
                "currency": spend[r][0][1],
            }
            for r, n in routes.most_common()
        ],
        "preferred_airports": [a for a, _ in Counter(t["origin"] for t in trips).most_common(3)],
        "airlines": sorted({t["airline"] for t in trips}),
        "cabins": dict(Counter(t["cabin"] for t in trips)),
        # ponytail: departures in the fixture are future dated (Duffel test mode cannot book the past), so no
        # dates are exposed. Upgrade: shift dates in the fixture, or real history from Memory in step 3.
        "orders": [
            {
                "order_id": t["order_id"],
                "booking_reference": t["booking_reference"],
                "route": f"{t['origin']}-{t['destination']}",
                "total": t["total"],
                "currency": t["currency"],
            }
            for t in trips
        ],
        "preferences": [],  # filled from AgentCore Memory by get_history
        "facts": [],
        "recent_session_summaries": [],
    }


def load(actor_id):
    """The verified traveler's record: routes, airports, airlines, cabins, orders (from bookings) plus preferences,
    facts and recent session summaries (from AgentCore Memory). Loaded once per session into the desk's prompt."""
    # Bookings (Gateway, as the desk) and Memory load in parallel; the Gateway call also warms the desk's session.
    # No fixture here, so the same code runs in AgentCore Runtime.
    with ThreadPoolExecutor(2) as pool:
        orders = pool.submit(common.call_tool, "desk", "list_orders")
        memory = pool.submit(common.memory_records, actor_id) if common.MEMORY_ID else None
        orders, m = orders.result(), memory.result() if memory else None
    error = orders.get("error") if isinstance(orders, dict) else None
    record = summarize(actor_id, [] if error else [trip(o) for o in orders if o["status"] != "cancelled"])
    if error:  # say so in the record rather than fail the whole call
        record["bookings_error"] = error
    if m:
        record.update(
            preferences=compact_preferences(m["preferences"])[:12],
            # ponytail: newest N, not most relevant, to keep the prompt (and time to first token) small.
            # Upgrade: semantic retrieval on the caller's question once Gateway/Runtime are in (step 4).
            facts=m["facts"][:15],
            recent_session_summaries=[" ".join(s.split())[:600] for s in m["summaries"][:2]],
        )
    return record


def trip(order):
    """A normalized Duffel order (duffel.norm_order) in the shape summarize() takes."""
    first = order["slices"][0]
    return {
        "order_id": order["order_id"],
        "booking_reference": order["booking_reference"],
        "origin": first["origin"],
        "destination": first["destination"],
        "airline": first["segments"][0]["carrier"],
        "cabin": order.get("cabin") or "unknown",
        "total": order["total"],
        "currency": order["currency"],
    }


def compact_preferences(records):
    """UserPreference records are JSON with context/categories; the desk only needs the preference sentence."""
    out = []
    for r in records:
        try:
            r = json.loads(r).get("preference") or r
        except (ValueError, AttributeError):
            pass
        if r not in out:
            out.append(r)
    return out


if __name__ == "__main__":
    trips = [
        dict(
            order_id=f"ord_{i}",
            booking_reference=f"R{i}",
            origin=o,
            destination=d,
            departure="x",
            airline="Duffel Airways",
            cabin="economy",
            total=p,
            currency="EUR",
        )
        for i, (o, d, p) in enumerate([("ATH", "LHR", "100.00"), ("ATH", "LHR", "150.00"), ("SKG", "MUC", "80.00")])
    ]
    s = summarize("cust_a", trips)
    assert s["trip_count"] == 3 and s["preferred_airports"] == ["ATH", "SKG"]
    assert s["past_routes"][0] == {"route": "ATH-LHR", "count": 2, "avg_total": "125.00", "currency": "EUR"}
    assert [o["order_id"] for o in s["orders"]] == ["ord_0", "ord_1", "ord_2"] and "departure" not in str(s)
    prefs = ['{"context": "x", "preference": "Aisle seat", "categories": []}', "Aisle seat", "plain text"]
    assert compact_preferences(prefs) == ["Aisle seat", "plain text"]
    o = {"order_id": "ord_1", "booking_reference": "R1", "status": "confirmed", "cabin": "economy", "total": "9.00",
         "currency": "EUR", "slices": [{"origin": "ATH", "destination": "LHR", "segments": [{"carrier": "ZZ Air"}]}]}  # fmt: skip
    assert summarize("cust_a", [trip(o)])["past_routes"][0]["route"] == "ATH-LHR"
    assert trip({**o, "cabin": None})["cabin"] == "unknown"
    print("traveler_profile: ok")

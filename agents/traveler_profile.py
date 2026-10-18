"""Traveler Profile: the traveler's record for the desk, from two sources.

- Bookings (Duffel orders tagged with the traveler, via the Gateway): summarized once per session into the desk's
  system prompt by `load`. Plain code, not an agent: a lookup needs no model hop.
- AgentCore Memory (preferences, facts, session summaries): recalled per caller turn by Strands' MemoryManager with
  AgentCore memory stores (`memory`), scoped to this traveler's namespaces, and folded into that turn's message.
"""

import json, re
from collections import Counter, defaultdict
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
    }


def load(actor_id):
    """The verified traveler's bookings record: routes, airports, airlines, cabins, orders. One Gateway call as the
    desk (it also warms the desk's Gateway session). No fixture here, so the same code runs in AgentCore Runtime."""
    orders = common.gateway_call("desk", "list_orders", {"actor_id": actor_id})
    error = orders.get("error") if isinstance(orders, dict) else None
    if common.PII_MASK and not error:  # the traveler's names are masked in traces from the first span on
        import redact

        redact.learn(n for o in orders for n in o.get("passengers", []))
    record = summarize(actor_id, [] if error else [trip(o) for o in orders if o["status"] != "cancelled"])
    if error:  # say so in the record rather than fail the whole call
        record["bookings_error"] = error
    return record


def namespaces(actor_id):
    """Memory namespaces of one actor, matching agentcore/agentcore.json. The trailing slash keeps cust_a from matching
    cust_ab. Summaries are per session (/summaries/{actor}/{session}/), so they are searched as a subtree."""
    return [
        {"namespace": f"/users/{actor_id}/preferences/", "max_search_results": 5},
        {"namespace": f"/users/{actor_id}/facts/", "max_search_results": 5},
        {"namespace_path": f"/summaries/{actor_id}/", "max_search_results": 2},
    ]


def memory(actor_id, session_id):
    """Strands MemoryManager over recall only AgentCore memory stores for this traveler. Each caller turn it searches
    the traveler's namespaces with the caller's message and folds the top entries into that turn (not the system
    prompt, which stays cacheable). Writing stays with the AgentCore session manager; extraction is server side."""
    if not common.MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.memorystore import AgentCoreMemoryStore
    from strands.memory import MemoryManager

    stores = [
        AgentCoreMemoryStore(
            memory_id=common.MEMORY_ID, actor_id=actor_id, session_id=session_id, region_name=common.REGION, **ns
        )
        for ns in namespaces(actor_id)
    ]
    # Every model call, not Strands' default "userTurn": injection is per call and never kept in history, so with the
    # default a turn that calls a tool first answers from the tool result without the traveler's Memory.
    # ponytail: one Memory search per model call (~0.3s each after a tool). Upgrade: cache the block per caller turn.
    injection = {"max_entries": 12, "format": render_memory, "trigger": "everyTurn", "query": caller_message}
    return MemoryManager(stores=stores, search_tool_config=False, injection=injection)


def caller_message(ctx):
    """The search query on every model call of a turn: the caller's latest words, not the desk's last step."""
    for m in reversed(ctx.messages):
        if m["role"] == "user" and not any("toolResult" in b for b in m["content"]):
            return "\n".join(b["text"] for b in m["content"] if "text" in b).strip() or None
    return None


def render_memory(ctx):
    """The memory block the desk sees: one line per record, preference records reduced to their sentence, without
    summary topics about turns the guardrail blocked."""
    lines = compact_preferences([without_blocked(" ".join(e.content.split()))[:600] for e in ctx.entries])
    lines = [line for line in lines if line]
    if not lines:
        return ""  # Strands skips the injection
    return "<traveler_memory>\n" + "\n".join(f"- {line}" for line in lines) + "\n</traveler_memory>"


BLOCKED_TOPIC = re.compile(r"<topic\b[^>]*>(?:(?!</topic>).)*?(?:redact|passport numbers|visas? or medical|visa/medical)"
                           r"(?:(?!</topic>).)*</topic>", re.I | re.S)  # fmt: skip


def without_blocked(text):
    """A summary without its topics about guardrail blocked turns. They hold nothing to recall, and the guardrail
    screens this block as part of the caller's message: "visa/medical related questions" in a summary got a
    traveler's next harmless question ("any news on the problem I reported?") blocked as a visa topic."""
    return BLOCKED_TOPIC.sub("", text).strip()


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
    from types import SimpleNamespace as NS

    block = render_memory(NS(entries=[NS(content='{"preference": "Aisle seat"}'), NS(content="Coeliac:  gluten free")]))
    assert block == "<traveler_memory>\n- Aisle seat\n- Coeliac: gluten free\n</traveler_memory>", block
    summary = ('<topic name="Meal"> Halal meal requested. </topic> <topic name="Redacted"> The user sent a redacted '
               "message, likely card or passport numbers, or visa/medical questions. </topic>")  # fmt: skip
    assert without_blocked(summary) == '<topic name="Meal"> Halal meal requested. </topic>', without_blocked(summary)
    assert render_memory(NS(entries=[NS(content=summary[summary.index('<topic name="Red') :])])) == ("")
    assert [n.get("namespace") or n["namespace_path"] for n in namespaces("cust_a")] == [
        "/users/cust_a/preferences/", "/users/cust_a/facts/", "/summaries/cust_a/"]  # fmt: skip
    msgs = [{"role": "user", "content": [{"text": "My meal?"}]}, {"role": "assistant", "content": [{"text": "x"}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": []}}]}]  # fmt: skip
    assert caller_message(NS(messages=msgs)) == "My meal?" and caller_message(NS(messages=[])) is None
    o = {"order_id": "ord_1", "booking_reference": "R1", "status": "confirmed", "cabin": "economy", "total": "9.00",
         "currency": "EUR", "slices": [{"origin": "ATH", "destination": "LHR", "segments": [{"carrier": "ZZ Air"}]}]}  # fmt: skip
    assert summarize("cust_a", [trip(o)])["past_routes"][0]["route"] == "ATH-LHR"
    assert trip({**o, "cabin": None})["cabin"] == "unknown"
    print("traveler_profile: ok")

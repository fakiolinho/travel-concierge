"""Traveler Profile specialist. Step 2: structured history from evals/fixtures/history.json only.
Step 3 adds AgentCore Memory (preferences, facts, summaries) to get_history."""
from collections import Counter, defaultdict
from decimal import Decimal

from strands import Agent, tool

import common


def summarize(actor_id, trips):
    routes, spend = Counter(), defaultdict(list)
    for t in trips:
        r = f'{t["origin"]}-{t["destination"]}'
        routes[r] += 1
        spend[r].append((Decimal(t["total"]), t["currency"]))
    return {
        "actor_id": actor_id,
        "trip_count": len(trips),
        "past_routes": [{"route": r, "count": n, "avg_total": str((sum(a for a, _ in spend[r]) / n).quantize(Decimal("0.01"))),
                         "currency": spend[r][0][1]} for r, n in routes.most_common()],
        "preferred_airports": [a for a, _ in Counter(t["origin"] for t in trips).most_common(3)],
        "airlines": sorted({t["airline"] for t in trips}),
        "cabins": dict(Counter(t["cabin"] for t in trips)),
        # ponytail: departures in the fixture are future dated (Duffel test mode cannot book the past), so no
        # dates are exposed. Upgrade: shift dates in the fixture, or real history from Memory in step 3.
        "orders": [{"order_id": t["order_id"], "booking_reference": t["booking_reference"],
                    "route": f'{t["origin"]}-{t["destination"]}', "total": t["total"], "currency": t["currency"]}
                   for t in trips],
        "preferences": [],  # step 3: AgentCore Memory UserPreference
        "open_issues": [],  # step 3: AgentCore Memory Summary
    }


@tool
def get_history() -> dict:
    """The verified traveler's structured history: routes, airports, airlines, cabins, orders, preferences."""
    return summarize(common.S["actor_id"], common.history())


PROMPT = """You are Traveler Profile at an airline service desk. Answer the desk's question about the
verified traveler using only get_history. Call it every time. Return compact JSON with the keys
past_routes, preferred_airports, preferences, open_issues, plus anything else from the record the question
needs (copy order ids and amounts verbatim). If the record does not contain the answer, say "not in record".
Never infer habits beyond what the counts show."""


def run(question: str) -> str:
    agent = Agent(model=common.model("profile"), system_prompt=PROMPT, tools=[get_history], callback_handler=None)
    return str(agent(question))


if __name__ == "__main__":
    trips = [dict(order_id=f"ord_{i}", booking_reference=f"R{i}", origin=o, destination=d, departure="x",
                  airline="Duffel Airways", cabin="economy", total=p, currency="EUR")
             for i, (o, d, p) in enumerate([("ATH", "LHR", "100.00"), ("ATH", "LHR", "150.00"), ("SKG", "MUC", "80.00")])]
    s = summarize("cust_a", trips)
    assert s["trip_count"] == 3 and s["preferred_airports"] == ["ATH", "SKG"]
    assert s["past_routes"][0] == {"route": "ATH-LHR", "count": 2, "avg_total": "125.00", "currency": "EUR"}
    assert [o["order_id"] for o in s["orders"]] == ["ord_0", "ord_1", "ord_2"] and "departure" not in str(s)
    print("traveler_profile: ok")

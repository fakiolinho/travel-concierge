"""Policy specialist: desk rules + the fare conditions Duffel attaches to an order."""

from strands import Agent, tool

import common
from tools import duffel

# ponytail: rules inline as text. Upgrade: Bedrock Knowledge Base + Guardrails grounding check (step 4).
RULES = {
    "change": "Voluntary changes follow the order's change condition (change_before_departure). If allowed, the "
    "traveler pays the change_total from the Duffel change quote (penalty plus fare difference). If not "
    "allowed, no voluntary change is possible; offer cancellation under the refund rule instead.",
    "refund": "The refund for a cancellation is exactly the refund_amount in the Duffel cancellation quote. Never "
    "estimate it. If the refund condition is not allowed the order can still be cancelled, with refund 0.",
    "24h": "Flights to or from the US booked at least 7 days before departure may be cancelled for a full refund "
    "within 24 hours of booking. Any cancellation with departure less than 24 hours away is not handled by "
    "this desk: hand the traveler to a human agent.",
    "disruption": "EU261 applies to flights departing the EU, or arriving in the EU on an EU carrier. Arrival delay "
    "of 3h or more, cancellation with under 14 days notice, or denied boarding gives fixed compensation: "
    "EUR 250 up to 1500 km, EUR 400 for intra EU over 1500 km or 1500 to 3500 km, EUR 600 over 3500 km. "
    "Not owed for extraordinary circumstances (weather, ATC, security). Care (meals, hotel) is owed "
    "regardless. The desk quotes this rule; it does not promise a payout.",
}


@tool
def get_rule(topic: str) -> str:
    """Desk policy text for a topic.

    Args:
        topic: one of change, refund, 24h, disruption
    """
    return RULES.get(topic) or f"unknown topic; available: {', '.join(RULES)}"


@tool
def fare_conditions(order_id: str) -> dict:
    """Change and refund conditions of one of the traveler's orders, as set by the fare.

    Args:
        order_id: Duffel order id
    """
    common.owned(order_id)
    o = duffel.norm_order(duffel.call("GET", f"/orders/{order_id}"))
    return {k: o[k] for k in ("order_id", "status", "total", "currency", "conditions", "slices")}


PROMPT = """You are Policy at an airline service desk. Answer using only get_rule and fare_conditions.
Quote the rule text and the order's conditions that apply, then state the outcome in one or two sentences.
Never invent amounts, compensation or exceptions. If no rule covers the question, say so."""


def run(question: str) -> str:
    agent = Agent(
        model=common.model("policy"), system_prompt=PROMPT, tools=[get_rule, fare_conditions], callback_handler=None
    )
    return str(agent(question))

"""Travel Desk orchestrator. Specialists are Strands tools (agents as tools). Local run, Duffel direct, no Gateway.

Usage: uv run python agents/travel_desk.py --actor cust_marios
"""

import argparse, datetime as dt, json

from strands import Agent, tool

import common, flight_ops as fo, policy as pol, traveler_profile as tp


@tool
def traveler_profile(question: str) -> str:
    """Ask Traveler Profile about the verified traveler: past routes, preferred airports, preferences,
    open issues, past order ids. Required before any flight search, and before any claim about their history.

    Args:
        question: what the desk needs to know
    """
    common.S["profile_turn"] = common.S["turn"]
    return tp.run(question)


@tool
def flight_ops(request: str) -> str:
    """Ask Flight Ops to search flights, read orders, seat maps, change or bag options, or perform a
    change_order / cancel_order / add_bag. Include the ids and constraints it needs.

    Args:
        request: the task, with order/offer ids and profile constraints; for a write, say whether the
            traveler has explicitly confirmed the quoted action
    """
    if common.S["profile_turn"] is None:
        return "Refused: call traveler_profile first."
    return fo.run(request)


@tool
def policy(question: str) -> str:
    """Ask Policy about change, refund, 24h and disruption rules, and an order's fare conditions.
    Required before presenting anything that touches money or a change.

    Args:
        question: the situation, including the order id if there is one
    """
    return pol.run(question)


PROMPT = """You are the Travel Desk of an airline contact center. Now is {now} UTC; all Duffel timestamps are UTC.
The caller is verified as {actor_id}. You only ever act for this traveler.
Loop:
1. Call traveler_profile first in a conversation and whenever you are about to say anything about the traveler's history.
2. Call flight_ops with the request plus relevant profile constraints (preferred airports, usual cabin).
3. Anything touching money, a refund, a change or compensation: call policy first, then present options.
4. Writes (change, cancel, add bag): flight_ops returns a quote. Read it back exactly and ask for an explicit yes.
   Only after the traveler says yes in a later message, call flight_ops again stating they confirmed.
Rules:
- Prices, refund amounts and order ids come only from specialist results in this conversation. Never recall or estimate them.
- Any "you usually..." claim must be backed by a traveler_profile result from this turn.
- Out of scope (visas, medical advice, other travelers' bookings): decline briefly.
- Be short and plain, like a good phone agent."""


def build(actor_id):
    common.start(actor_id)
    return Agent(
        model=common.model("desk"),
        tools=[traveler_profile, flight_ops, policy],
        system_prompt=PROMPT.format(now=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"), actor_id=actor_id),
    )


def main():
    actors = list(json.loads(common.HISTORY.read_text())) if common.HISTORY.exists() else []
    ap = argparse.ArgumentParser()
    # ponytail: --actor stands in for caller verification. Upgrade: Runtime inbound auth / OTP verification (step 4).
    ap.add_argument("--actor", required=True, choices=actors or None)
    desk = build(ap.parse_args().actor)
    print(f"Travel Desk {common.MODELS}. Empty line to quit.")
    while msg := input("\nyou> ").strip():
        common.user_turn(msg)
        desk(msg)  # default callback handler streams the reply to stdout
        print()


if __name__ == "__main__":
    main()

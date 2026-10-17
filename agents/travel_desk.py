"""Travel Desk orchestrator. Specialists are Strands tools (agents as tools). Local run, Duffel direct, no Gateway.

Usage: uv run python agents/travel_desk.py --actor cust_marios
"""

import argparse, datetime as dt, json, os, time, uuid

from strands import tool

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


def memory_session(actor_id, session_id):
    """Persist this conversation to AgentCore Memory so the extractors can learn from it. Persistence only: no
    retrieval_config, because every historical claim has to come through a traveler_profile tool call."""
    if not common.MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    cfg = AgentCoreMemoryConfig(memory_id=common.MEMORY_ID, session_id=session_id, actor_id=actor_id)
    return AgentCoreMemorySessionManager(cfg, region_name=os.environ.get("AWS_REGION", "us-east-1"))


def build(actor_id, session_manager=None, session_id=None):
    common.start(actor_id, session_id)
    return common.agent(
        "desk",
        tools=[traveler_profile, flight_ops, policy],
        session_manager=session_manager,
        system_prompt=PROMPT.format(now=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"), actor_id=actor_id),
    )


def main():
    actors = list(json.loads(common.HISTORY.read_text())) if common.HISTORY.exists() else []
    ap = argparse.ArgumentParser()
    # ponytail: --actor stands in for caller verification. Upgrade: Runtime inbound auth / OTP verification (step 4).
    ap.add_argument("--actor", required=True, choices=actors or None)
    actor = ap.parse_args().actor
    session_id = f"desk-{uuid.uuid4()}"
    traces = common.setup_tracing()
    sm = memory_session(actor, session_id)
    desk = build(actor, sm, session_id)
    print(f"Travel Desk {common.MODELS}, memory {common.MEMORY_ID or 'off'}. Empty line to quit.")
    print(f"Session {session_id}, traces: {traces}")
    asked = []
    try:
        while msg := input("\nyou> ").strip():
            asked.append(msg)
            turn(desk, actor, msg, common.cache_key(actor, asked))
    except EOFError:
        pass
    finally:
        if sm:
            sm.close()  # flush buffered events before exit


def turn(desk, actor, msg, key):
    common.user_turn(msg)
    t0 = time.perf_counter()
    if hit := common.cache_get(key):
        # ponytail: a cached turn never reaches the model, so it is neither traced nor persisted to Memory.
        desk.messages += [
            {"role": "user", "content": [{"text": msg}]},
            {"role": "assistant", "content": [{"text": hit["answer"]}]},
        ]
        until = dt.datetime.fromtimestamp(hit["expires"], dt.timezone.utc).strftime("%H:%M")
        print(hit["answer"])
        print(f"\n⏱ {time.perf_counter() - t0:.2f}s (cached answer, valid until {until} UTC)")
        return
    # Strands keeps cumulative metrics on the agent; the difference over this turn is the per step timing.
    m = desk.event_loop_metrics
    before = {n: t.total_time for n, t in m.tool_metrics.items()}, m.accumulated_metrics["latencyMs"]
    answer = str(desk(msg))  # default callback handler streams the reply to stdout
    secs = time.perf_counter() - t0
    if common.S["wrote"]:
        common.cache_drop(actor)
    elif not common.S["touched_write"]:
        expires = min(common.S["expires"] or float("inf"), time.time() + common.CACHE_TTL)
        if expires > time.time():
            common.cache_put(key, answer, expires)
    steps = {n: t.total_time - before[0].get(n, 0.0) for n, t in m.tool_metrics.items()}
    steps["desk model"] = (m.accumulated_metrics["latencyMs"] - before[1]) / 1000
    shown = " · ".join(f"{n} {s:.1f}s" for n, s in sorted(steps.items(), key=lambda kv: -kv[1]) if s > 0)
    print(f"\n\n⏱ {secs:.1f}s total | {shown}")


if __name__ == "__main__":
    main()

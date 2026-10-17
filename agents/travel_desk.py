"""Travel Desk orchestrator. Local run, Duffel direct, no Gateway.

The desk calls the read tools itself and has the traveler profile and the policy rules in its prompt, so a read
turn costs two model calls plus one Duffel call. Only writes go to another agent (Flight Ops), the one principal
allowed to write. Usage: uv run python agents/travel_desk.py --actor cust_marios
"""

import argparse, datetime as dt, json, os, time, uuid

from strands import tool

import common, flight_ops as fo, policy as pol, traveler_profile as tp


@tool
def flight_ops(request: str) -> str:
    """Hand a write to Flight Ops: change_order (with change_offer_id from change_options), cancel_order, or
    add_bag (with service_id from bag_options). Say whether the traveler has explicitly confirmed the quoted action.

    Args:
        request: the write, with order id and the offer or service id it needs, and the confirmation status
    """
    return fo.run(request)


PROMPT = """You are the Travel Desk of an airline contact center, on a live call. Now is {now} UTC; all Duffel
timestamps are UTC. The caller is verified as {actor_id}. You only ever act for this traveler.

How to work:
- Before a tool call, say one short sentence of what you are checking, then call it.
- Flights to a city: search the airport the traveler has used for that city in their record; if none, the main
  airport. Search other airports only if asked. One search per question unless the traveler asks for more.
- Bookings: get_order or list_orders. Date changes: change_options. Bags: bag_options. Seats: seat_map.
- Anything touching money, a refund, a change or compensation: apply the policy rules below.
- Writes (change, cancel, add bag): call flight_ops; it returns a quote. Read it back exactly and ask for an
  explicit yes. Only after the traveler says yes in a later message, call flight_ops again stating they confirmed.

Rules:
- Prices, refund amounts and order ids come only from tool results in this conversation. Never recall or estimate.
- "You usually..." and any other claim about the traveler's history must come from the traveler record below.
- Out of scope (visas, medical advice, other travelers' bookings): decline briefly.
- Answer like a good phone agent: at most 5 short lines, at most 3 options, no tables, no headings.

Policy rules:
{rules}

Traveler record (bookings history and AgentCore Memory, loaded at the start of this call):
{profile}"""


def memory_session(actor_id, session_id):
    """Persist this conversation to AgentCore Memory so the extractors can learn from it. Persistence only: no
    retrieval_config, because the traveler record is loaded once, explicitly, by traveler_profile.load."""
    if not common.MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    # Batched: one create_event per message costs ~0.4s inside every turn. Buffer and flush every 30s and on close.
    # ponytail: a crash loses up to 30s of conversation from Memory. Upgrade: Runtime session lifecycle (step 4).
    cfg = AgentCoreMemoryConfig(
        memory_id=common.MEMORY_ID, session_id=session_id, actor_id=actor_id, batch_size=100, flush_interval_seconds=30
    )
    return AgentCoreMemorySessionManager(cfg, region_name=os.environ.get("AWS_REGION", "us-east-1"))


def build(actor_id, session_manager=None, session_id=None):
    common.start(actor_id, session_id)
    profile = tp.load(actor_id)
    rules = "\n".join(f"- {k}: {v}" for k, v in pol.RULES.items())
    return common.agent(
        "desk",
        tools=[*fo.READ_TOOLS, flight_ops],
        session_manager=session_manager,
        system_prompt=PROMPT.format(
            now=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
            actor_id=actor_id,
            rules=rules,
            profile=json.dumps(profile, ensure_ascii=False),
        ),
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

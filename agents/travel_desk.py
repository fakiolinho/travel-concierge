"""Travel Desk orchestrator. Usage: uv run python agents/travel_desk.py --actor cust_marios

The desk reads through its own Gateway tools and has the bookings record and the policy rules in its prompt; the
traveler's AgentCore Memory is recalled per turn by Strands' MemoryManager. Writes go to the Flight Ops agent and
disruption questions to the Disruption agent, both Strands `Agent.as_tool`. A write pauses the whole call on a Strands
interrupt carrying the quote; the caller's next message is the interrupt response.
"""

import argparse, datetime as dt, json, os, time, uuid

import common, disruption as dis, flight_ops as fo, policy as pol, traveler_profile as tp

PROMPT = """You are the Travel Desk of an airline contact center, on a live call. Now is {now} UTC; all Duffel
timestamps are UTC. The caller is verified as {actor_id}. You only ever act for this traveler.

How to work:
- Before a tool call, say one short sentence of what you are checking, then call it.
- Flights to a city: search the airport the traveler has used for that city in their record; if none, the main
  airport. Search other airports only if asked. One search per question unless the traveler asks for more.
- Bookings: get_order or list_orders. Date changes: change_options. Bags: bag_options. Seats: seat_map.
- Weather, disruption risk, "should I move my flight", or the weather when they land: call disruption with the
  order id (from the traveler record or a tool result; never a bare booking reference), or the route and
  departure time if you already have them. It returns a JSON report (risk, evidence, compensation, rebooking
  options); tell the caller the summary and the key evidence. Rebooking from its options is a write, see below.
- Amounts in another currency: fx_convert with an amount from a tool result; say the rate is indicative.
- Anything touching money, a refund, a change or compensation: apply the policy rules below.
- Writes (book a searched flight, change, cancel, add bag) and refund or change quotes on an existing booking: call
  flight_ops once, naming the action and its ids. The system reads the exact quote to the caller and asks for their
  yes before anything changes, so never ask for confirmation yourself. Afterwards report what flight_ops returned:
  done with its amounts, or not done.

Rules:
- Prices, refund amounts and order ids come only from tool results in this conversation. Never recall or estimate.
- "You usually..." and any other claim about the traveler's history must come from the traveler record below or the
  <traveler_memory> block of the current message.
- Out of scope (visas, medical advice, other travelers' bookings): decline briefly.
- Answer like a good phone agent: at most 5 short lines, at most 3 options, no tables, no headings.

Policy rules:
{rules}

Traveler record (bookings, loaded at the start of this call):
{profile}"""


def memory_session(actor_id, session_id):
    """Persist this conversation to AgentCore Memory so the extractors can learn from it, and keep the session's
    agent state (including a pending write interrupt) across Runtime invocations. Persistence only: recall is done by
    traveler_profile.memory."""
    if not common.MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    # Batched: one create_event per message costs ~0.4s inside every turn. Buffer and flush every 30s and on close.
    # ponytail: a crash loses up to 30s of conversation from Memory. Upgrade: Runtime session lifecycle (step 4).
    cfg = AgentCoreMemoryConfig(
        memory_id=common.MEMORY_ID, session_id=session_id, actor_id=actor_id, batch_size=100, flush_interval_seconds=30
    )
    return AgentCoreMemorySessionManager(cfg, region_name=common.REGION)


def build(actor_id, session_manager=None, session_id=None):
    """The desk agent for one call. It carries the TurnLog its answer cache reads (`desk.turn_log`) and the write
    interrupts waiting for the caller's answer (`desk.pending_writes`)."""
    common.warm_gateways("desk", "flight_ops", "disruption")  # handshakes run while the record loads
    state = common.session_state(actor_id, session_id)
    log = common.TurnLog()
    profile = tp.load(actor_id)
    rules = "\n".join(f"- {k}: {v}" for k, v in pol.RULES.items())
    desk = common.agent(
        "desk",
        state,
        tools=[
            *common.gateway_tools("desk", [*fo.READ_TOOLS, "fx_convert"]),
            dis.build(state),
            fo.build(state, hooks=[log]),
        ],
        hooks=[log],
        session_manager=session_manager,
        memory_manager=tp.memory(actor_id, session_id or "local"),
        system_prompt=PROMPT.format(
            now=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
            actor_id=actor_id,
            rules=rules,
            profile=json.dumps(profile, ensure_ascii=False),
        ),
    )
    desk.turn_log, desk.pending_writes = log, []
    return desk


def ask(desk, msg):
    """One caller message: the answer to a pending write quote (a Strands interrupt resume) or a new turn. Returns
    the AgentResult; the quotes now waiting for the caller are in desk.pending_writes."""
    pending = desk.pending_writes
    prompt = [{"interruptResponse": {"interruptId": i.id, "response": msg}} for i in pending] if pending else msg
    result = desk(prompt)
    desk.pending_writes = list(result.interrupts or []) if result.stop_reason == "interrupt" else []
    return result


def respond(desk, msg):
    """One caller turn as text (the eval runner's entry point): the desk's answer, then any quote it asks to confirm."""
    desk.turn_log.reset()
    result = ask(desk, msg)
    return "\n\n".join([str(result).strip(), *(i.reason for i in desk.pending_writes)]).strip()


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
    log = desk.turn_log
    log.reset()
    t0 = time.perf_counter()
    cache_on = os.environ.get("ANSWER_CACHE", "on") != "off"  # evals turn it off: a cached turn has no spans
    if cache_on and not desk.pending_writes and (hit := common.cache_get(key)):
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
    result = ask(desk, msg)  # default callback handler streams the reply to stdout
    for i in desk.pending_writes:
        print(f"\n{i.reason}")  # the quote: the caller's next message answers it
    secs = time.perf_counter() - t0
    if log.wrote:
        common.cache_drop(actor)
    elif cache_on and not log.touched_write and not desk.pending_writes:
        expires = min(log.expires or float("inf"), time.time() + common.CACHE_TTL)
        if expires > time.time():
            common.cache_put(key, str(result), expires)
    steps = {n: t.total_time - before[0].get(n, 0.0) for n, t in m.tool_metrics.items()}
    steps["desk model"] = (m.accumulated_metrics["latencyMs"] - before[1]) / 1000
    shown = " · ".join(f"{n} {s:.1f}s" for n, s in sorted(steps.items(), key=lambda kv: -kv[1]) if s > 0)
    print(f"\n\n⏱ {secs:.1f}s total | {shown}")


if __name__ == "__main__":
    main()

"""Flight Ops: the agent that makes bookings changes (book_offer, change_order, cancel_order, add_bag).

Its tools are the AgentCore Gateway's MCP tools (Lambda target "Duffel"), signed as the Flight Ops IAM role, the only
principal Cedar lets write. Cedar decides who may write; the WriteGate intervention decides when: every write first
pauses on a Strands interrupt with the quote, and only an explicit yes in the caller's next message resumes it
(Cedar cannot see conversation turns). The desk calls this agent through `Agent.as_tool`.
"""

import datetime as dt

import common

READ_TOOLS = ["search_flights", "get_order", "list_orders", "seat_map", "change_options", "bag_options"]
WRITE_TOOLS = ["book_offer", "change_order", "cancel_order", "add_bag"]

PROMPT = """You are Flight Ops at an airline service desk. Now is {now} UTC. You make changes to one verified
traveler's bookings: book_offer, change_order, cancel_order, add_bag. Call exactly the write the request names,
with its ids, once. The system reads the exact quote to the traveler and asks for their yes before the write runs:
never ask for confirmation yourself and never call the same write twice.
If the write ran, report the amounts and ids verbatim from the tool result in one or two lines. If it was not
confirmed, say it was not done. If a tool returns an error (for example a departure under 24 hours or a policy
denial), report it plainly and stop. Never estimate."""


def build(state, hooks=()):
    """Flight Ops for one session, as the desk's `flight_ops` tool. preserve_context=False: every request starts from
    a clean conversation with the session state it was built with; an interrupted write is stored by the desk and
    resumed when the caller answers."""
    agent = common.agent(
        "flight_ops",
        state,
        system_prompt=PROMPT.format(now=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")),
        tools=common.gateway_tools("flight_ops", ["get_order", *WRITE_TOOLS]),
        interventions=[common.WriteGate("flight_ops"), common.ExecuteConfirmed()],
        hooks=hooks,
    )
    return agent.as_tool(
        name="flight_ops",
        description="Hand a write to Flight Ops: book_offer (with offer_id from search_flights), change_order (with "
        "change_offer_id from change_options), cancel_order, or add_bag (with service_id from bag_options). Name the "
        "action and its ids. The traveler is asked to confirm the exact quote before anything changes.",
    )

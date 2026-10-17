"""Flight Ops: the model facing Duffel tools. Each one is a thin call to the AgentCore Gateway (Lambda target
"Duffel"), signed as the agent that is calling it: the desk's tools run as the desk role (Cedar: reads only), the
Flight Ops agent's as the Flight Ops role (Cedar: the only principal allowed to write).

Writes go through common.gated, so they need a later user turn with an explicit yes before they run: Cedar decides
who may write, the gate decides when (Cedar cannot see conversation turns)."""

import datetime as dt

from strands import ToolContext, tool

import common


def _call(ctx, name, **args):
    return common.call_tool(ctx.agent.name, name, **args)


@tool(context=True)
def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    tool_context: ToolContext,
    return_date: str = "",
    adults: int = 1,
    cabin_class: str = "economy",
) -> list | dict:
    """Search flight offers. Returns the 3 cheapest offers.

    Args:
        origin: IATA airport code, e.g. ATH
        destination: IATA airport code, e.g. LHR
        departure_date: YYYY-MM-DD
        return_date: YYYY-MM-DD for a round trip, empty for one way
        adults: number of adult passengers
        cabin_class: economy, premium_economy, business or first
    """
    out = _call(
        tool_context,
        "search_flights",
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        adults=adults,
        cabin_class=cabin_class,
    )
    if isinstance(out, list):
        common.note_expiry(out)
    return out


@tool(context=True)
def get_order(order_id: str, tool_context: ToolContext) -> dict:
    """Live status and details of one of the traveler's orders.

    Args:
        order_id: Duffel order id, e.g. ord_...
    """
    return _call(tool_context, "get_order", order_id=order_id)


@tool(context=True)
def list_orders(tool_context: ToolContext) -> list | dict:
    """Live status of all of the verified traveler's orders."""
    return _call(tool_context, "list_orders")


@tool(context=True)
def seat_map(offer_id: str, tool_context: ToolContext) -> list | dict:
    """Available seats with prices for a flight offer, one entry per segment.

    Args:
        offer_id: offer id from search_flights
    """
    return _call(tool_context, "seat_map", offer_id=offer_id)


@tool(context=True)
def change_options(
    order_id: str, new_departure_date: str, tool_context: ToolContext, slice_index: int = 0
) -> list | dict:
    """Priced options to move one slice of an order to a new date. Read only, nothing is changed.

    Args:
        order_id: Duffel order id
        new_departure_date: YYYY-MM-DD
        slice_index: which slice to move, 0 = outbound, 1 = return
    """
    out = _call(
        tool_context,
        "change_options",
        order_id=order_id,
        new_departure_date=new_departure_date,
        slice_index=slice_index,
    )
    if isinstance(out, list):
        common.note_expiry(out)
    return out


@tool(context=True)
def bag_options(order_id: str, tool_context: ToolContext) -> list | dict:
    """Extra baggage that can be added to an order, with prices. Read only.

    Args:
        order_id: Duffel order id
    """
    return _call(tool_context, "bag_options", order_id=order_id)


@tool(context=True)
def change_order(order_id: str, change_offer_id: str, tool_context: ToolContext) -> dict:
    """WRITE. Move an order to a change offer from change_options. First call returns a quote that needs
    the traveler's explicit yes; only a call after that confirmation executes.

    Args:
        order_id: Duffel order id
        change_offer_id: id from change_options
    """
    args = dict(order_id=order_id, change_offer_id=change_offer_id)
    return common.gated(
        "change_order",
        (order_id, change_offer_id),
        lambda: _call(tool_context, "change_order", **args),
        lambda q: _call(tool_context, "change_order", **args, quote_id=q["quote_id"]),
    )


@tool(context=True)
def cancel_order(order_id: str, tool_context: ToolContext) -> dict:
    """WRITE. Cancel an order. First call returns the refund quote that needs the traveler's explicit yes;
    only a call after that confirmation executes.

    Args:
        order_id: Duffel order id
    """
    return common.gated(
        "cancel_order",
        order_id,
        lambda: _call(tool_context, "cancel_order", order_id=order_id),
        lambda q: _call(tool_context, "cancel_order", order_id=order_id, quote_id=q["quote_id"]),
    )


@tool(context=True)
def add_bag(order_id: str, service_id: str, tool_context: ToolContext, quantity: int = 1) -> dict:
    """WRITE. Add a bag service from bag_options to an order. First call returns a price quote that needs
    the traveler's explicit yes; only a call after that confirmation executes.

    Args:
        order_id: Duffel order id
        service_id: id from bag_options
        quantity: number of bags
    """
    args = dict(order_id=order_id, service_id=service_id, quantity=quantity)
    return common.gated(
        "add_bag",
        (order_id, service_id, quantity),
        lambda: _call(tool_context, "add_bag", **args, confirm=False),
        lambda q: _call(tool_context, "add_bag", **args, confirm=True),
    )


READ_TOOLS = [search_flights, get_order, list_orders, seat_map, change_options, bag_options]

PROMPT = """You are Flight Ops at an airline service desk. Now is {now} UTC. You make changes to one verified
traveler's bookings: change_order, cancel_order, add_bag. Call exactly the write the request names, with its ids.
The first call returns status needs_confirmation with a quote: report the exact quote and stop. Call again only
when the request says the traveler explicitly confirmed that exact action. Report amounts and ids verbatim from the
tool result in one or two lines. Never estimate. If a tool returns an error (for example a departure under 24 hours
or a policy denial), report it plainly and stop."""


def run(request: str) -> str:
    agent = common.agent(
        "flight_ops",
        system_prompt=PROMPT.format(now=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")),
        tools=[get_order, change_order, cancel_order, add_bag],
    )
    return str(agent(request))

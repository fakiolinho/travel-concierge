"""Flight Ops specialist: Duffel reads plus the three write tools (change_order, cancel_order, add_bag).
Write tools go through common.gated, so they need a later user turn with an explicit yes before they run."""
import datetime as dt
from decimal import Decimal

from strands import Agent, tool

import common
from tools import duffel


@tool
def search_flights(origin: str, destination: str, departure_date: str, return_date: str = "",
                   adults: int = 1, cabin_class: str = "economy") -> list:
    """Search flight offers. Returns the 5 cheapest offers.

    Args:
        origin: IATA airport code, e.g. ATH
        destination: IATA airport code, e.g. LHR
        departure_date: YYYY-MM-DD
        return_date: YYYY-MM-DD for a round trip, empty for one way
        adults: number of adult passengers
        cabin_class: economy, premium_economy, business or first
    """
    slices = [{"origin": origin, "destination": destination, "departure_date": departure_date}]
    if return_date:
        slices.append({"origin": destination, "destination": origin, "departure_date": return_date})
    req = duffel.call("POST", "/offer_requests?return_offers=true", {
        "slices": slices, "passengers": [{"type": "adult"}] * adults, "cabin_class": cabin_class})
    # ponytail: Duffel Airways (ZZ) only so evals are reproducible. Upgrade: drop the filter outside test mode.
    offers = sorted((o for o in req["offers"] if o["owner"]["iata_code"] == "ZZ"), key=lambda o: Decimal(o["total_amount"]))
    return [duffel.norm_offer(o) for o in offers[:5]]


@tool
def get_order(order_id: str) -> dict:
    """Live status and details of one of the traveler's orders.

    Args:
        order_id: Duffel order id, e.g. ord_...
    """
    common.owned(order_id)
    return duffel.norm_order(duffel.call("GET", f"/orders/{order_id}"))


@tool
def list_orders() -> list:
    """Live status of all of the verified traveler's orders."""
    return [duffel.norm_order(duffel.call("GET", f'/orders/{o["order_id"]}')) for o in common.history()]


@tool
def seat_map(offer_id: str) -> list:
    """Available seats with prices for a flight offer, one entry per segment.

    Args:
        offer_id: offer id from search_flights
    """
    return [duffel.norm_seat_map(m) for m in duffel.call("GET", f"/seat_maps?offer_id={offer_id}")]


@tool
def change_options(order_id: str, new_departure_date: str, slice_index: int = 0) -> list:
    """Priced options to move one slice of an order to a new date. Read only, nothing is changed.

    Args:
        order_id: Duffel order id
        new_departure_date: YYYY-MM-DD
        slice_index: which slice to move, 0 = outbound, 1 = return
    """
    common.owned(order_id)
    order = duffel.call("GET", f"/orders/{order_id}")
    sl = order["slices"][slice_index]
    req = duffel.call("POST", "/order_change_requests", {"order_id": order_id, "slices": {
        "remove": [{"slice_id": sl["id"]}],
        "add": [{"origin": sl["origin"]["iata_code"], "destination": sl["destination"]["iata_code"],
                 "departure_date": new_departure_date, "cabin_class": order.get("cabin_class") or "economy"}]}})
    offers = req.get("order_change_offers") or []
    return [duffel.norm_change_offer(c) for c in sorted(offers, key=lambda c: Decimal(c["change_total_amount"]))[:5]]


@tool
def bag_options(order_id: str) -> list:
    """Extra baggage that can be added to an order, with prices. Read only.

    Args:
        order_id: Duffel order id
    """
    common.owned(order_id)
    svcs = duffel.call("GET", f"/orders/{order_id}/available_services")
    return [duffel.norm_bag(s) for s in svcs if s["type"] == "baggage"]


@tool
def change_order(order_id: str, change_offer_id: str) -> dict:
    """WRITE. Move an order to a change offer from change_options. First call returns a quote that needs
    the traveler's explicit yes; only a call after that confirmation executes.

    Args:
        order_id: Duffel order id
        change_offer_id: id from change_options
    """
    common.owned(order_id)

    def quote():
        c = duffel.call("POST", "/order_changes", {"selected_order_change_offer": change_offer_id})
        return {"order_change_id": c["id"], "change_total": c["change_total_amount"],
                "currency": c["change_total_currency"], "penalty": c.get("penalty_total_amount"),
                "new_total": c.get("new_total_amount"), "expires_at": c.get("expires_at")}

    def execute(q):
        body = {}
        if Decimal(q["change_total"]) > 0:
            body["payment"] = {"type": "balance", "amount": q["change_total"], "currency": q["currency"]}
        duffel.call("POST", f'/order_changes/{q["order_change_id"]}/actions/confirm', body)
        return {"charged": q["change_total"], "currency": q["currency"],
                "order": duffel.norm_order(duffel.call("GET", f"/orders/{order_id}"))}

    return common.gated("change_order", (order_id, change_offer_id), quote, execute)


@tool
def cancel_order(order_id: str) -> dict:
    """WRITE. Cancel an order. First call returns the refund quote that needs the traveler's explicit yes;
    only a call after that confirmation executes.

    Args:
        order_id: Duffel order id
    """
    common.owned(order_id)

    def quote():
        c = duffel.call("POST", "/order_cancellations", {"order_id": order_id})
        return {"cancellation_id": c["id"], "refund_amount": c["refund_amount"], "currency": c["refund_currency"],
                "refund_to": c.get("refund_to"), "expires_at": c.get("expires_at")}

    def execute(q):
        c = duffel.call("POST", f'/order_cancellations/{q["cancellation_id"]}/actions/confirm')
        return {"order_id": order_id, "cancelled_at": c.get("confirmed_at"),
                "refund_amount": c["refund_amount"], "currency": c["refund_currency"]}

    return common.gated("cancel_order", order_id, quote, execute)


@tool
def add_bag(order_id: str, service_id: str, quantity: int = 1) -> dict:
    """WRITE. Add a bag service from bag_options to an order. First call returns a price quote that needs
    the traveler's explicit yes; only a call after that confirmation executes.

    Args:
        order_id: Duffel order id
        service_id: id from bag_options
        quantity: number of bags
    """
    common.owned(order_id)

    def quote():
        svc = next((s for s in duffel.call("GET", f"/orders/{order_id}/available_services") if s["id"] == service_id), None)
        if not svc:
            return {"error": f"service {service_id} not available on {order_id}"}
        return {"service_id": service_id, "quantity": quantity, "currency": svc["total_currency"],
                "amount": str(Decimal(svc["total_amount"]) * quantity)}

    def execute(q):
        o = duffel.call("POST", f"/orders/{order_id}/services", {
            "add_services": [{"id": q["service_id"], "quantity": q["quantity"]}],
            "payment": {"type": "balance", "amount": q["amount"], "currency": q["currency"]}})
        return {"charged": q["amount"], "currency": q["currency"], "order": duffel.norm_order(o)}

    return common.gated("add_bag", (order_id, service_id, quantity), quote, execute)


PROMPT = """You are Flight Ops at an airline service desk. Today is {today}.
You act on behalf of one verified traveler; their orders are the only ones you can see.
- Every fact you report (prices, refunds, times, order ids, offer ids) must come verbatim from a tool result in this request. Never estimate.
- change_order, cancel_order and add_bag are writes. Their first call returns status needs_confirmation with a quote: report the exact quote and stop. Call them again only when the request says the traveler explicitly confirmed that exact action.
- To change dates use change_options first, then change_order with the chosen change_offer_id. For bags use bag_options first.
- Reply with a short factual summary and include the relevant ids so the desk can refer back to them."""


def run(request: str) -> str:
    agent = Agent(model=common.model(), system_prompt=PROMPT.format(today=dt.date.today()), callback_handler=None,
                  tools=[search_flights, get_order, list_orders, seat_map, change_options, bag_options,
                         change_order, cancel_order, add_bag])
    return str(agent(request))

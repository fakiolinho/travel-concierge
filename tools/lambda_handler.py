"""AgentCore Gateway Lambda target "Duffel": every flight tool behind the Gateway.

By the time a call lands here the Gateway has authenticated the caller (AWS_IAM) and Cedar has allowed the tool.
This layer adds what Cedar cannot see: that the order belongs to the traveler (Duffel order metadata.actor_id) and
the departure < 24h cancel rule. The Duffel token is read from Secrets Manager here and never leaves; only
duffel.norm_* output is returned, so no model ever sees raw Duffel JSON.

Writes are two calls: without quote_id (or confirm) they only quote; with the quote_id from that quote they execute.
The confirmation turn between the two is enforced by the agents (common.WriteGate, a Strands interrupt), Cedar decides
who may call at all.
"""

import datetime as dt, json, os
from decimal import Decimal

import duffel

SECRET_ID = os.environ.get("DUFFEL_SECRET_ID", "travel-concierge/duffel-test-token")
CANCEL_CUTOFF = dt.timedelta(hours=24)


def _load_token():
    # Cached for the life of the Lambda container.
    if not os.environ.get("DUFFEL_TEST_TOKEN"):
        import boto3

        sm = boto3.client("secretsmanager")
        os.environ["DUFFEL_TEST_TOKEN"] = sm.get_secret_value(SecretId=SECRET_ID)["SecretString"]


def _owned(order_id, actor_id):
    """The order, if it belongs to actor_id. Same error for 'missing' and 'someone else's' so ids cannot be probed."""
    o = duffel.call("GET", f"/orders/{order_id}")
    if not actor_id or (o.get("metadata") or {}).get("actor_id") != actor_id:
        raise PermissionError(f"order {order_id} not found for this traveler")
    return o


def _departs_within(order, window, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    first = min(dt.datetime.fromisoformat(s["departing_at"]) for sl in order["slices"] for s in sl["segments"])
    if first.tzinfo is None:  # Duffel departing_at is local airport time without offset; treat as UTC (conservative
        first = first.replace(tzinfo=dt.timezone.utc)  # enough for a 24h rule on short haul).
    return first - now < window


def search_flights(origin, destination, departure_date, return_date="", adults=1, cabin_class="economy", **_):
    slices = [{"origin": origin, "destination": destination, "departure_date": departure_date}]
    if return_date:
        slices.append({"origin": destination, "destination": origin, "departure_date": return_date})
    req = duffel.call(
        "POST",
        # supplier_timeout caps how long Duffel waits on airlines: test mode searches otherwise take up to 20s.
        "/offer_requests?return_offers=true&supplier_timeout=2000",
        {"slices": slices, "passengers": [{"type": "adult"}] * int(adults), "cabin_class": cabin_class},
    )
    # ponytail: Duffel Airways (ZZ) only so evals are reproducible. Upgrade: drop the filter outside test mode.
    offers = sorted(
        (o for o in req["offers"] if o["owner"]["iata_code"] == "ZZ"), key=lambda o: Decimal(o["total_amount"])
    )
    return [duffel.norm_offer(o) for o in offers[:3]]


def get_order(order_id, actor_id="", **_):
    return duffel.norm_order(_owned(order_id, actor_id))


def _actor_orders(actor_id):
    """Raw Duffel orders tagged with this actor, newest first. Covers seeded history and desk bookings alike."""
    # ponytail: lists the whole (test) account and filters by metadata. Upgrade: a per actor index (DynamoDB) or
    # Duffel's own customer/user objects once orders are created with them.
    orders = duffel.call("GET", "/orders?limit=200")
    mine = [o for o in orders if actor_id and (o.get("metadata") or {}).get("actor_id") == actor_id]
    return sorted(mine, key=lambda o: o.get("created_at") or "", reverse=True)


def list_orders(actor_id="", **_):
    return [duffel.norm_order(o) for o in _actor_orders(actor_id)]


def seat_map(offer_id, **_):
    return [duffel.norm_seat_map(m) for m in duffel.call("GET", f"/seat_maps?offer_id={offer_id}")]


def change_options(order_id, new_departure_date, slice_index=0, actor_id="", **_):
    order = _owned(order_id, actor_id)
    sl = order["slices"][int(slice_index)]
    add = {
        "origin": sl["origin"]["iata_code"],
        "destination": sl["destination"]["iata_code"],
        "departure_date": new_departure_date,
        "cabin_class": order.get("cabin_class") or "economy",
    }
    req = duffel.call(
        "POST",
        "/order_change_requests",
        {"order_id": order_id, "slices": {"remove": [{"slice_id": sl["id"]}], "add": [add]}},
    )
    offers = sorted(req.get("order_change_offers") or [], key=lambda c: Decimal(c["change_total_amount"]))
    return [duffel.norm_change_offer(c) for c in offers[:5]]


def bag_options(order_id, actor_id="", **_):
    _owned(order_id, actor_id)
    return [
        duffel.norm_bag(s)
        for s in duffel.call("GET", f"/orders/{order_id}/available_services")
        if s["type"] == "baggage"
    ]


def change_order(order_id, change_offer_id, actor_id="", quote_id="", **_):
    _owned(order_id, actor_id)
    if not quote_id:
        c = duffel.call("POST", "/order_changes", {"selected_order_change_offer": change_offer_id})
        return {
            "quote_id": c["id"],
            "change_total": c["change_total_amount"],
            "currency": c["change_total_currency"],
            "penalty": c.get("penalty_total_amount"),
            "new_total": c.get("new_total_amount"),
            "expires_at": c.get("expires_at"),
        }
    c = duffel.call("GET", f"/order_changes/{quote_id}")
    if c.get("order_id") != order_id:
        raise PermissionError("quote does not belong to this order")
    body = {}
    if Decimal(c["change_total_amount"]) > 0:  # amount from Duffel's own quote, never from the caller
        body["payment"] = {
            "type": "balance",
            "amount": c["change_total_amount"],
            "currency": c["change_total_currency"],
        }
    duffel.call("POST", f"/order_changes/{quote_id}/actions/confirm", body)
    return {
        "charged": c["change_total_amount"],
        "currency": c["change_total_currency"],
        "order": duffel.norm_order(duffel.call("GET", f"/orders/{order_id}")),
    }


def cancel_order(order_id, actor_id="", quote_id="", now=None, **_):
    order = _owned(order_id, actor_id)
    # ponytail: Cedar only sees tool arguments, not the departure time, so the 24h rule lives here.
    # Upgrade: pass departure_at as an argument the agent fills from get_order and forbid it in Cedar too.
    if _departs_within(order, CANCEL_CUTOFF, now):
        raise PermissionError("departure is less than 24 hours away: this desk cannot cancel, hand over to a human")
    if not quote_id:
        c = duffel.call("POST", "/order_cancellations", {"order_id": order_id})
        return {
            "quote_id": c["id"],
            "refund_amount": c["refund_amount"],
            "currency": c["refund_currency"],
            "refund_to": c.get("refund_to"),
            "expires_at": c.get("expires_at"),
        }
    c = duffel.call("GET", f"/order_cancellations/{quote_id}")
    if c.get("order_id") != order_id:
        raise PermissionError("quote does not belong to this order")
    c = duffel.call("POST", f"/order_cancellations/{quote_id}/actions/confirm")
    return {
        "order_id": order_id,
        "cancelled_at": c.get("confirmed_at"),
        "refund_amount": c["refund_amount"],
        "currency": c["refund_currency"],
    }


PAX_FIELDS = ("title", "gender", "given_name", "family_name", "born_on", "email", "phone_number")


def book_offer(offer_id, actor_id="", confirm=False, **_):
    """confirm=false: live price quote for one passenger. confirm=true: book it for the traveler. Price comes from
    Duffel's offer at confirm time, passenger details from the traveler's own newest order: never from the caller."""
    o = duffel.call("GET", f"/offers/{offer_id}")
    if len(o["passengers"]) != 1:
        raise ValueError("the desk can only book offers for a single passenger")
    n = duffel.norm_offer(o)
    q = {"offer_id": offer_id, "total": n["total"], "currency": n["currency"], "expires_at": n["expires_at"],
         "slices": n["slices"], "conditions": n["conditions"]}  # fmt: skip
    if not confirm:
        return q
    past = _actor_orders(actor_id)
    if not past:
        raise PermissionError("no past booking to copy passenger details from; hand over to a human")
    pax = {k: past[0]["passengers"][0][k] for k in PAX_FIELDS}
    order = duffel.call(
        "POST",
        "/orders",
        {
            "type": "instant",
            "selected_offers": [offer_id],
            "payments": [{"type": "balance", "amount": o["total_amount"], "currency": o["total_currency"]}],
            "passengers": [{"id": o["passengers"][0]["id"], **pax}],
            "metadata": {"actor_id": actor_id},  # ownership for every later call
        },
    )
    return {"booked": True, "order": duffel.norm_order(order)}


def add_bag(order_id, service_id, quantity=1, actor_id="", confirm=False, **_):
    _owned(order_id, actor_id)
    svc = next((s for s in duffel.call("GET", f"/orders/{order_id}/available_services") if s["id"] == service_id), None)
    if not svc:
        raise ValueError(f"service {service_id} not available on {order_id}")
    q = {
        "service_id": service_id,
        "quantity": int(quantity),
        "currency": svc["total_currency"],
        "amount": str(Decimal(svc["total_amount"]) * int(quantity)),
    }
    if not confirm:
        return q
    o = duffel.call(
        "POST",
        f"/orders/{order_id}/services",
        {
            "add_services": [{"id": service_id, "quantity": q["quantity"]}],
            "payment": {"type": "balance", "amount": q["amount"], "currency": q["currency"]},
        },
    )
    return {"charged": q["amount"], "currency": q["currency"], "order": duffel.norm_order(o)}


TOOLS = {
    f.__name__: f
    for f in (
        search_flights,
        get_order,
        list_orders,
        seat_map,
        change_options,
        bag_options,
        change_order,
        cancel_order,
        book_offer,
        add_bag,
    )
}


def handler(event, context):
    """Gateway passes the tool arguments as the event and the tool as '<Target>___<tool>' in client context."""
    full = context.client_context.custom["bedrockAgentCoreToolName"]
    name = full.split("___", 1)[-1]
    try:
        _load_token()
        return TOOLS[name](**event)
    except (duffel.DuffelError, PermissionError, ValueError, KeyError) as e:
        # Business errors go back as data so the agent can explain them; anything else is a real failure.
        return {"error": str(e) if not isinstance(e, KeyError) else f"unknown tool or missing field: {e}"}


if __name__ == "__main__":
    calls = []
    orders = {
        "ord_a": {"id": "ord_a", "metadata": {"actor_id": "cust_a"}},
        "ord_b": {"id": "ord_b", "metadata": {"actor_id": "cust_b"}},
    }

    def fake(method, path, body=None):
        calls.append((method, path))
        if path.startswith("/orders/"):
            return orders[path.split("/")[2]]
        raise AssertionError(path)

    duffel.call = fake
    assert _owned("ord_a", "cust_a")["id"] == "ord_a"
    for actor in ("cust_b", "", None):
        try:
            _owned("ord_a", actor)
            raise AssertionError("cross actor read must fail")
        except PermissionError:
            pass
    now = dt.datetime(2026, 10, 18, 12, tzinfo=dt.timezone.utc)
    soon = {"slices": [{"segments": [{"departing_at": "2026-10-19T06:00:00"}]}]}
    later = {
        "slices": [{"segments": [{"departing_at": "2026-10-20T06:00:00"}, {"departing_at": "2026-10-21T06:00:00"}]}]
    }
    assert _departs_within(soon, CANCEL_CUTOFF, now) and not _departs_within(later, CANCEL_CUTOFF, now)

    class Ctx:
        class client_context:
            custom = {"bedrockAgentCoreToolName": "Duffel___get_order"}

    os.environ["DUFFEL_TEST_TOKEN"] = "duffel_test_x"
    assert handler({"order_id": "ord_a", "actor_id": "cust_b"}, Ctx) == {
        "error": "order ord_a not found for this traveler"
    }
    Ctx.client_context.custom = {"bedrockAgentCoreToolName": "Duffel___nope"}
    assert "unknown tool" in handler({}, Ctx)["error"]
    print("lambda_handler: ok", json.dumps(sorted(TOOLS)))

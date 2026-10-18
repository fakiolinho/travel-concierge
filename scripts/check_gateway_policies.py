"""Live check of the Gateway guardrails (Cedar + Lambda), no model involved. Run after `agentcore deploy`:
uv run python scripts/check_gateway_policies.py            writes only quote, nothing changes
uv run python scripts/check_gateway_policies.py --writes   also books a flight for cust_nikos and cancels it
                                                           (Duffel test mode), to exercise the confirm paths
"""

import datetime as dt, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import common  # noqa: E402

marios_order = common.history("cust_marios")[0]["order_id"]
SEARCH_DATE = (dt.date.today() + dt.timedelta(days=30)).isoformat()


def call(role, actor, name, verified=True, **args):
    common.start(actor, "policy-check")
    common.S["guest_verified"] = verified
    return common.call_tool(role, name, **args)


def denied(r):
    return isinstance(r, dict) and "error" in r


common.start("cust_marios", "policy-check")
offer = common.call_tool("desk", "search_flights", origin="ATH", destination="LHR", departure_date=SEARCH_DATE)[0]

results = [
    (
        "desk book_offer is denied by Cedar",
        denied(call("desk", "cust_marios", "book_offer", offer_id=offer["offer_id"])),
    ),
    (
        "flight_ops gets a booking quote (nothing booked)",
        call("flight_ops", "cust_marios", "book_offer", offer_id=offer["offer_id"]).get("offer_id")
        == offer["offer_id"],
    ),
    ("desk reads its traveler's order", not denied(call("desk", "cust_marios", "get_order", order_id=marios_order))),
    (
        "desk cancel_order is denied by Cedar",
        denied(call("desk", "cust_marios", "cancel_order", order_id=marios_order)),
    ),
    (
        "desk add_bag is denied by Cedar",
        denied(call("desk", "cust_marios", "add_bag", order_id=marios_order, service_id="x")),
    ),
    (
        "flight_ops gets a cancel quote",
        "quote_id" in call("flight_ops", "cust_marios", "cancel_order", order_id=marios_order),
    ),
    (
        "flight_ops write without verification is denied by Cedar",
        denied(call("flight_ops", "cust_marios", "cancel_order", verified=False, order_id=marios_order)),
    ),
    (
        "flight_ops cannot touch another traveler's order (Lambda)",
        denied(call("flight_ops", "cust_eleni", "cancel_order", order_id=marios_order)),
    ),
]
if "--writes" in sys.argv:
    nikos_offer = call(
        "desk", "cust_nikos", "search_flights", origin="ATH", destination="BCN", departure_date=SEARCH_DATE
    )[0]
    booked = call("flight_ops", "cust_nikos", "book_offer", offer_id=nikos_offer["offer_id"], confirm=True)
    order_id = booked.get("order", {}).get("order_id")
    results.append(("flight_ops books an offer, tagged to the traveler", bool(booked.get("booked") and order_id)))
    results.append(
        (
            "the new order is readable by its traveler",
            not denied(call("desk", "cust_nikos", "get_order", order_id=order_id)),
        )
    )
    results.append(("and not by another traveler", denied(call("desk", "cust_marios", "get_order", order_id=order_id))))
    quote = call("flight_ops", "cust_nikos", "cancel_order", order_id=order_id)
    done = call("flight_ops", "cust_nikos", "cancel_order", order_id=order_id, quote_id=quote.get("quote_id"))
    results.append(
        (
            "flight_ops cancels it by quote id",
            bool(done.get("cancelled_at")) and done.get("refund_amount") == quote.get("refund_amount"),
        )
    )

for name, ok in results:
    print("PASS" if ok else "FAIL", name)
assert all(ok for _, ok in results), "gateway policy check failed"
print(json.dumps({"checked": len(results)}))

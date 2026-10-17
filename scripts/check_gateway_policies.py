"""Live check of the Gateway guardrails (Cedar + Lambda), no model involved. Writes only ever quote: nothing is
cancelled. Run after `agentcore deploy`:  uv run python scripts/check_gateway_policies.py
"""

import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import common  # noqa: E402

marios_order = common.history("cust_marios")[0]["order_id"]


def call(role, actor, name, verified=True, **args):
    common.start(actor, "policy-check")
    common.S["guest_verified"] = verified
    return common.call_tool(role, name, **args)


def denied(r):
    return isinstance(r, dict) and "error" in r


results = [
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
for name, ok in results:
    print("PASS" if ok else "FAIL", name)
assert all(ok for _, ok in results), "gateway policy check failed"
print(json.dumps({"checked": len(results)}))

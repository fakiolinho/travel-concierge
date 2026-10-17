"""Shared bits for the local run: model factory, session state, fixture history, write confirmation gate."""
import json, os, re, sys
from pathlib import Path

from strands.models import BedrockModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # agents run as scripts; this lets them `from tools import duffel`

# ponytail: minimal KEY=VALUE .env reader (no quotes/escapes). Upgrade: Secrets Manager behind Lambdas in step 4.
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-opus-5-5")
HISTORY = ROOT / "evals/fixtures/history.json"


def model():
    return BedrockModel(model_id=MODEL_ID, region_name=os.environ.get("AWS_REGION", "us-east-1"))


# ponytail: one in-process session dict, single caller. Upgrade: Runtime session + AgentCore Memory (step 3).
S = {}


def start(actor_id):
    S.clear()
    S.update(actor_id=actor_id, guest_verified=True, turn=0, last_user="", pending={}, profile_turn=None)


def user_turn(text):
    S["turn"] += 1
    S["last_user"] = text


def history(actor_id=None):
    """Fixture orders for the verified actor only. There is deliberately no way to ask for another actor."""
    if not HISTORY.exists():
        raise FileNotFoundError(f"{HISTORY} missing; run scripts/seed_duffel_history.py first")
    return json.loads(HISTORY.read_text()).get(actor_id or S["actor_id"], [])


def owned(order_id):
    # ponytail: ownership = order id is in the actor's fixture. Upgrade: Duffel order metadata.actor_id + Cedar.
    if order_id not in {o["order_id"] for o in history()}:
        raise ValueError(f"order {order_id} not found for this traveler")


# ponytail: regex "yes" detector. Upgrade: Strands Decider pre send gate + Cedar context.guest_verified (step 4).
AFFIRM = re.compile(r"^\W*(yes|yep|yeah|y|confirm(ed)?|go ahead|do it|proceed|please do|ok(ay)?|sure)\b", re.I)


def gated(action, key, quote, execute):
    """Two phase write. The first call quotes and parks the action. It only executes on a call made in a
    later user turn whose text is an explicit yes, so a write can never happen in the turn it was proposed."""
    if not S.get("guest_verified"):
        return {"error": "caller not verified"}
    k = (action, key)
    p = S["pending"].get(k)
    if p and p["turn"] < S["turn"] and AFFIRM.match(S["last_user"]):
        del S["pending"][k]
        r = execute(p["quote"])
        return r if "error" in r else {"status": "done", **r}
    if not p or p["turn"] != S["turn"]:
        q = quote()
        if "error" in q:
            return q
        p = S["pending"][k] = {"turn": S["turn"], "quote": q}
    return {"status": "needs_confirmation", "quote": p["quote"],
            "next": "Read this quote to the traveler and ask for an explicit yes. Call again only after they confirm."}


if __name__ == "__main__":
    runs = []
    q = lambda: {"refund": "10.00"}
    x = lambda p: runs.append(p) or {"ok": True}
    start("cust_a")
    user_turn("cancel ord_1")
    assert gated("cancel", "ord_1", q, x)["status"] == "needs_confirmation"
    assert gated("cancel", "ord_1", q, x)["status"] == "needs_confirmation" and not runs, "same turn must not execute"
    user_turn("hmm, what about tomorrow instead?")
    assert gated("cancel", "ord_1", q, x)["status"] == "needs_confirmation" and not runs, "non yes must not execute"
    user_turn("Yes, go ahead")
    assert gated("cancel", "ord_1", q, x) == {"status": "done", "ok": True} and runs == [{"refund": "10.00"}]
    assert gated("cancel", "ord_1", q, x)["status"] == "needs_confirmation" and len(runs) == 1, "pending is single use"
    S["guest_verified"] = False
    assert "error" in gated("cancel", "ord_1", q, x)
    print("common: ok")

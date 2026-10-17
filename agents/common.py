"""Shared bits for the local run: agent factory, tracing setup, session state, fixture history, answer cache,
write confirmation gate."""

import datetime as dt, hashlib, json, os, re, sys, time
from pathlib import Path

from strands import Agent
from strands.models import BedrockModel
from strands.models.model import CacheConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # agents run as scripts; this lets them `from tools import duffel`

# ponytail: minimal KEY=VALUE .env reader (no quotes/escapes). Upgrade: Secrets Manager behind Lambdas in step 4.
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())

# Two agents: the desk holds the conversation and the reads; Flight Ops makes the writes.
# Override any of them with MODEL_ID_<ROLE> in .env. Re-run evals before changing a default.
MODELS = {
    "desk": "us.anthropic.claude-sonnet-5-5",
    "flight_ops": "us.anthropic.claude-sonnet-5-5",
}
MODELS = {role: os.environ.get(f"MODEL_ID_{role.upper()}", m) for role, m in MODELS.items()}
HISTORY = ROOT / "evals/fixtures/history.json"
MEMORY_ID = os.environ.get("MEMORY_ID", "")  # empty = run without AgentCore Memory


def model(role):
    # Bedrock prompt caching: reuses the system prompt, tool specs and earlier turns between calls. Faster first
    # token and cheaper input on long conversations; prefixes under the model's minimum size are silently not cached.
    return BedrockModel(
        model_id=MODELS[role],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        cache_config=CacheConfig(strategy="auto"),
    )


LOG_DIR = ROOT / "logs"


def setup_tracing():
    """Strands' built in OpenTelemetry tracing, exported as one JSON span per line to logs/traces-YYYY-MM-DD.jsonl.
    Spans cover every agent run, event loop cycle, model call and tool call, with start/end timestamps, parent ids
    (specialist spans nest under the desk tool span that called them), token usage, and inputs/outputs."""
    from strands.telemetry import StrandsTelemetry

    # ponytail: local file with unredacted conversation text. Upgrade: AgentCore Observability (CloudWatch) in
    # Runtime via an OTLP exporter, PII redaction via Guardrails (step 4).
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"traces-{dt.date.today()}.jsonl"
    telemetry = StrandsTelemetry()
    telemetry.setup_console_exporter(out=path.open("a"), formatter=lambda s: s.to_json(indent=None) + "\n")
    # Also stream spans to a trace UI (Phoenix, Jaeger, ...) when the standard OTel endpoint variable is set.
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        import atexit

        telemetry.setup_otlp_exporter()
        atexit.register(telemetry.tracer_provider.force_flush)  # batched exporter: send the last spans on exit
    return path


def agent(role, **kw):
    """Every agent is built here: named (span names), tagged with session and actor (span attributes), and only
    the desk streams to stdout."""
    if role != "desk":
        kw.setdefault("callback_handler", None)
    attrs = {"session.id": S.get("session_id") or "", "actor.id": S.get("actor_id") or "", "agent.role": role}
    return Agent(name=role, model=model(role), trace_attributes=attrs, **kw)


def namespaces(actor_id):
    """Memory namespaces of one actor, matching agentcore/agentcore.json. The trailing slash keeps cust_a from matching cust_ab."""
    return {
        "preferences": f"/users/{actor_id}/preferences/",
        "facts": f"/users/{actor_id}/facts/",
        "summaries": f"/summaries/{actor_id}/",
    }


def memory_records(actor_id, limit=50):
    """Long term records of one actor, newest first, grouped by strategy."""
    import boto3

    # ponytail: list every record by namespace prefix, no semantic search. Upgrade: retrieve_memory_records with the
    # desk's question as query once an actor has more records than fit in a prompt.
    c = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    out = {}
    for kind, ns in namespaces(actor_id).items():
        recs, token = [], None
        while len(recs) < limit:
            # namespacePath, not namespace: in practice `namespace` only matches exactly, so per session summary
            # namespaces (/summaries/{actor}/{session}/) are only reachable hierarchically.
            r = c.list_memory_records(
                memoryId=MEMORY_ID, namespacePath=ns, maxResults=100, **({"nextToken": token} if token else {})
            )
            # belt and braces: drop anything whose namespace is not under this actor's prefix
            recs += [
                x
                for x in r.get("memoryRecordSummaries", [])
                if all(n.startswith(ns) for n in x.get("namespaces", [ns]))
            ]
            if not (token := r.get("nextToken")):
                break
        recs.sort(key=lambda x: x.get("createdAt") or 0, reverse=True)
        out[kind] = [x["content"]["text"] for x in recs[:limit]]
    return out


# ponytail: one in-process session dict, single caller. Upgrade: Runtime session + AgentCore Memory (step 3).
S = {}


def start(actor_id, session_id=None):
    S.clear()
    S.update(
        actor_id=actor_id,
        session_id=session_id,
        guest_verified=True,
        turn=0,
        last_user="",
        pending={},
    )


def user_turn(text):
    S["turn"] += 1
    S["last_user"] = text
    # per turn: write tools touched / executed, earliest offer expiry seen
    S.update(touched_write=False, wrote=False, expires=None)


def note_expiry(offers):
    for o in offers:
        if o.get("expires_at"):
            t = dt.datetime.fromisoformat(o["expires_at"]).timestamp()
            S["expires"] = min(S["expires"] or t, t)


# ponytail: answer cache in a local JSON file, one process. Upgrade: DynamoDB/ElastiCache with TTL in Runtime (step 4).
# Only read only turns are cached, until the earliest offer expiry they showed (max 10 min); any executed write
# drops that traveler's entries. A hit replays the answer, so its prices are still from unexpired tool results.
CACHE = ROOT / ".cache/answers.json"
CACHE_TTL = 600


def _cache(drop_actor=None):
    try:
        c = json.loads(CACHE.read_text())
    except (FileNotFoundError, ValueError):
        c = {}
    now = time.time()
    return {k: v for k, v in c.items() if v["expires"] > now and not k.startswith(f"{drop_actor}:")}


def cache_key(actor_id, user_msgs):
    """Same traveler + same questions so far in the session (case and whitespace insensitive)."""
    norm = [" ".join(m.lower().split()) for m in user_msgs]
    return f"{actor_id}:{hashlib.sha256(json.dumps(norm).encode()).hexdigest()}"


def cache_get(key):
    return _cache().get(key)


def cache_put(key, answer, expires):
    c = _cache()
    c[key] = {"answer": answer, "expires": expires}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(c))


def cache_drop(actor_id):
    if CACHE.exists():
        CACHE.write_text(json.dumps(_cache(drop_actor=actor_id)))


def history(actor_id=None):
    """Fixture orders for the verified actor only. There is deliberately no way to ask for another actor."""
    if not HISTORY.exists():
        raise FileNotFoundError(f"{HISTORY} missing; run scripts/seed_duffel_history.py first")
    return json.loads(HISTORY.read_text()).get(actor_id or S["actor_id"], [])


# AgentCore Gateway: every Duffel tool is a Gateway call. Each agent signs with its own IAM role, so Cedar sees
# who is calling (desk: reads only; Flight Ops: reads and writes). Order ownership is checked by the Lambda.
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
GATEWAY_ROLES = {
    "desk": os.environ.get("GATEWAY_ROLE_DESK", ""),
    "flight_ops": os.environ.get("GATEWAY_ROLE_FLIGHT_OPS", ""),
}
GATEWAY_TARGET = "Duffel"
ACTOR_TOOLS = {"get_order", "list_orders", "change_options", "bag_options", "change_order", "cancel_order", "add_bag"}
_gateways = {}


def gateway(role):
    """One MCP session per agent role, signed with SigV4 as that role's assumed IAM identity."""
    if role not in _gateways:
        import atexit, boto3
        from botocore.credentials import Credentials
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
        from strands.tools.mcp import MCPClient

        region = os.environ.get("AWS_REGION", "us-east-1")
        assert GATEWAY_URL and GATEWAY_ROLES[role], "set GATEWAY_URL and GATEWAY_ROLE_* (see README, step 4)"
        # ponytail: 1h STS credentials, not refreshed. Upgrade: botocore RefreshableCredentials for long sessions.
        c = boto3.client("sts", region_name=region).assume_role(
            RoleArn=GATEWAY_ROLES[role], RoleSessionName=f"{role}-{(S.get('session_id') or 'local')[-40:]}"
        )["Credentials"]
        creds = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        client = MCPClient(
            lambda: aws_iam_streamablehttp_client(GATEWAY_URL, "bedrock-agentcore", region, credentials=creds)
        )
        client.start()
        atexit.register(client.stop, None, None, None)
        _gateways[role] = client
    return _gateways[role]


def call_tool(role, name, **args):
    """Call one Gateway tool as `role`. actor_id and guest_verified come from the verified session, never from the
    model: the model-facing tools don't even have those parameters. Returns the tool's JSON, or {"error": ...}."""
    args = {k: v for k, v in args.items() if v is not None and v != ""}
    if name in ACTOR_TOOLS:
        args.update(actor_id=S["actor_id"], guest_verified=bool(S.get("guest_verified")))
    r = gateway(role).call_tool_sync(f"{name}-{time.time_ns()}", f"{GATEWAY_TARGET}___{name}", args)
    text = "".join(c.get("text", "") for c in r["content"])
    if r["status"] == "error":
        return {"error": text or "gateway call failed"}
    try:
        return json.loads(text)
    except ValueError:
        return {"error": f"unexpected gateway response: {text[:200]}"}


# ponytail: regex "yes" detector. Upgrade: Strands Decider pre send gate + Cedar context.guest_verified (step 4).
AFFIRM = re.compile(r"^\W*(yes|yep|yeah|y|confirm(ed)?|go ahead|do it|proceed|please do|ok(ay)?|sure)\b", re.I)


def gated(action, key, quote, execute):
    """Two phase write. The first call quotes and parks the action. It only executes on a call made in a
    later user turn whose text is an explicit yes, so a write can never happen in the turn it was proposed."""
    if not S.get("guest_verified"):
        return {"error": "caller not verified"}
    S["touched_write"] = True
    k = (action, key)
    p = S["pending"].get(k)
    if p and p["turn"] < S["turn"] and AFFIRM.match(S["last_user"]):
        del S["pending"][k]
        r = execute(p["quote"])
        S["wrote"] = "error" not in r
        return r if "error" in r else {"status": "done", **r}
    if not p or p["turn"] != S["turn"]:
        q = quote()
        if "error" in q:
            return q
        p = S["pending"][k] = {"turn": S["turn"], "quote": q}
    return {
        "status": "needs_confirmation",
        "quote": p["quote"],
        "next": "Read this quote to the traveler and ask for an explicit yes. Call again only after they confirm.",
    }


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

    CACHE = Path(os.environ.get("TMPDIR", "/tmp")) / "travel_concierge_cache_check.json"
    CACHE.unlink(missing_ok=True)
    k = cache_key("cust_a", ["Find  me a flight", "to LHR"])
    assert k == cache_key("cust_a", ["find me a flight", "to lhr "]) and k != cache_key(
        "cust_b", ["find me a flight", "to lhr"]
    )
    assert k != cache_key("cust_a", ["to LHR"]), "earlier turns are part of the key"
    cache_put(k, "answer", time.time() + 60)
    cache_put("cust_a:old", "stale", time.time() - 1)
    cache_put("cust_b:x", "other", time.time() + 60)
    assert cache_get(k)["answer"] == "answer" and cache_get("cust_a:old") is None
    cache_drop("cust_a")
    assert cache_get(k) is None and cache_get("cust_b:x")["answer"] == "other", "drop is per traveler"
    user_turn("x")
    note_expiry([{"expires_at": "2026-10-18T10:00:00Z"}, {"expires_at": "2026-10-18T09:00:00Z"}, {}])
    assert S["expires"] == dt.datetime(2026, 10, 18, 9, tzinfo=dt.timezone.utc).timestamp()
    CACHE.unlink()
    print("common: ok")

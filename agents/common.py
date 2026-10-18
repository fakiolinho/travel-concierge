"""Shared bits: agent factory, tracing setup, Gateway tools, the interventions every agent runs, answer cache.

Per session data (verified actor, session id) lives in each agent's Strands state (`Agent(state=...)`), read by the
interventions below from `event.agent.state`. No module level session dict.
"""

import datetime as dt, hashlib, json, logging, os, re, sys, threading, time
from pathlib import Path

from dotenv import load_dotenv
from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider
from strands.interventions import Confirm, Deny, InterventionHandler, Proceed, Transform
from strands.models import BedrockModel
from strands.models.model import CacheConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # agents run as scripts; this lets them `from tools import duffel`
load_dotenv(ROOT / ".env")

# The desk holds the conversation and the reads; Flight Ops makes the writes. The Disruption agent (and the read only
# Flight Ops it calls) run on Haiku 5.5: their judgement rests on risk signals computed in code, and Strands
# structured output falls back to forced tool choice, which Sonnet 5.5 rejects (400) and Haiku 5.5 accepts.
# Override any of them with MODEL_ID_<ROLE> in .env. Re-run evals before changing a default.
MODELS = {
    "desk": "us.anthropic.claude-sonnet-5-5",
    "flight_ops": "us.anthropic.claude-sonnet-5-5",
    "disruption": "us.anthropic.claude-haiku-5-5",
    "flight_ops_read": "us.anthropic.claude-haiku-5-5",
}
MODELS = {role: os.environ.get(f"MODEL_ID_{role.upper()}", m) for role, m in MODELS.items()}
REGION = os.environ.get("AWS_REGION", "us-east-1")
HISTORY = ROOT / "evals/fixtures/history.json"
MEMORY_ID = os.environ.get("MEMORY_ID", "")  # empty = run without AgentCore Memory
GUARDRAIL = {"id": os.environ.get("GUARDRAIL_ID", ""), "version": os.environ.get("GUARDRAIL_VERSION", "")}


def model(role):
    # Bedrock prompt caching: reuses the system prompt, tool specs and earlier turns between calls. Faster first
    # token and cheaper input on long conversations; prefixes under the model's minimum size are silently not cached.
    guard = {}
    if GUARDRAIL["id"]:
        # Bedrock Guardrail (PII block/mask, visa and medical denied), defined in agentcore/cdk/lib/cdk-stack.ts.
        # Async: the answer streams while the guardrail checks it, and is redacted if it intervenes, so the
        # guardrail adds no time to a turn. Only the latest message is checked, not the whole history each call.
        guard = dict(
            guardrail_id=GUARDRAIL["id"],
            guardrail_version=GUARDRAIL["version"],
            guardrail_stream_processing_mode="async",
            guardrail_latest_message=True,
            guardrail_trace="enabled",
        )
    return BedrockModel(model_id=MODELS[role], region_name=REGION, cache_config=CacheConfig(strategy="auto"), **guard)


LOG_DIR = ROOT / "logs"


def setup_tracing():
    """Strands' built in OpenTelemetry tracing, exported as one JSON span per line to logs/traces-YYYY-MM-DD.jsonl.
    Spans cover every agent run, event loop cycle, model call and tool call, with start/end timestamps, parent ids
    (sub agent spans nest under the desk tool span that called them), token usage, and inputs/outputs."""
    from strands.telemetry import StrandsTelemetry

    # ponytail: local file with unredacted conversation text. Upgrade: AgentCore Observability (CloudWatch) in
    # Runtime via an OTLP exporter, PII redaction via Guardrails (step 4).
    from openinference.instrumentation.strands_agents import StrandsAgentsToOpenInferenceProcessor

    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"traces-{dt.date.today()}.jsonl"
    # Resource attributes Strands reads from the standard OTel variables: service name, and the Phoenix project.
    os.environ.setdefault("OTEL_SERVICE_NAME", "travel-concierge")
    os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES", "openinference.project.name=travel-concierge")
    telemetry = StrandsTelemetry()
    # Arize's processor copies Strands' gen_ai message events into OpenInference attributes (input/output
    # messages, tool calls, span kinds), so Phoenix can show prompts and answers. Added first: processors run in
    # order, and both exporters below should see the enriched spans.
    telemetry.tracer_provider.add_span_processor(StrandsAgentsToOpenInferenceProcessor())
    telemetry.setup_console_exporter(out=path.open("a"), formatter=lambda s: s.to_json(indent=None) + "\n")
    # Also stream spans to a trace UI (Phoenix, Jaeger, ...) when the standard OTel endpoint variable is set.
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        import atexit

        telemetry.setup_otlp_exporter()
        atexit.register(telemetry.tracer_provider.force_flush)  # batched exporter: send the last spans on exit
    return path


def session_state(actor_id, session_id):
    """What every agent of one call carries in its Strands state: the verified traveler and the session."""
    return {"actor_id": actor_id, "session_id": session_id or "", "guest_verified": True}


def agent(role, state, interventions=(), hooks=(), **kw):
    """Every agent is built here: named (span names), tagged with session and actor (span attributes), carrying the
    session in its Strands state, and running SessionContext first. Only the desk streams to stdout."""
    if role != "desk":
        kw.setdefault("callback_handler", None)
    # session.id and user.id are OpenInference keys: Phoenix groups spans into sessions and filters by user on them.
    actor = state["actor_id"]
    attrs = {"session.id": state["session_id"], "user.id": actor, "actor.id": actor, "agent.role": role}
    return Agent(
        name=role,
        model=model(role),
        trace_attributes=attrs,
        state=dict(state),
        interventions=[SessionContext(), *interventions],
        hooks=list(hooks),
        **kw,
    )


# AgentCore Gateway: every external tool is a Gateway call over MCP. Each agent signs with its own IAM role, so Cedar
# sees who is calling (desk: reads only; Flight Ops: reads and writes; Disruption: reads only). Order ownership is
# checked by the Lambda.
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
GATEWAY_ROLES = {
    "desk": os.environ.get("GATEWAY_ROLE_DESK", ""),
    "flight_ops": os.environ.get("GATEWAY_ROLE_FLIGHT_OPS", ""),
    "disruption": os.environ.get("GATEWAY_ROLE_DISRUPTION", ""),  # reads only, Cedar forbids every write
}
WRITE_TOOLS = {"book_offer", "change_order", "cancel_order", "add_bag"}
TRIP_INTEL_TOOLS = {"airport_weather", "airport_forecast", "destination_forecast", "public_holidays", "fx_convert"}
# Filled from the verified session by SessionContext / WriteGate, never by the model: hidden from the tool specs the
# model sees (the Gateway schemas keep them, they are the Lambda's contract and Cedar reads guest_verified).
SYSTEM_FIELDS = {"actor_id", "guest_verified", "confirm", "quote_id"}
_gateways = {}
_gateway_lock = threading.Lock()
_system_fields = {}  # leaf tool name -> the SYSTEM_FIELDS its Gateway schema takes


def gateway(identity):
    """One MCP client per Gateway identity, SigV4 signed with that IAM role. Credentials come from botocore's own
    assume role fetcher and refresh before they expire, so long sessions keep working."""
    with _gateway_lock:
        if identity not in _gateways:
            import atexit, boto3
            from botocore.credentials import AssumeRoleCredentialFetcher, DeferredRefreshableCredentials
            from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
            from strands.tools.mcp import MCPClient

            assert GATEWAY_URL and GATEWAY_ROLES[identity], "set GATEWAY_URL and GATEWAY_ROLE_* (see README, step 4)"
            source = boto3.Session(region_name=REGION)
            fetcher = AssumeRoleCredentialFetcher(
                client_creator=source._session.create_client,
                source_credentials=source.get_credentials(),
                role_arn=GATEWAY_ROLES[identity],
                extra_args={"RoleSessionName": f"travel-concierge-{identity}"},
            )
            creds = DeferredRefreshableCredentials(refresh_using=fetcher.fetch_credentials, method="assume-role")
            client = MCPClient(
                lambda: aws_iam_streamablehttp_client(GATEWAY_URL, "bedrock-agentcore", REGION, credentials=creds)
            )
            client.start()
            atexit.register(client.stop, None, None, None)
            # A Cedar denial is an expected outcome (returned to the agent as an error result), not a crash to log.
            logging.getLogger("strands.tools.mcp.mcp_client").setLevel(logging.CRITICAL)
            _gateways[identity] = client
        return _gateways[identity]


def warm_gateways(*identities):
    """Open Gateway sessions in the background at session start (STS + MCP handshake), so no caller turn pays it."""
    for identity in identities:
        if GATEWAY_ROLES.get(identity):
            threading.Thread(target=gateway, args=(identity,), daemon=True).start()


def _listed(identity):
    found = {t.mcp_tool.name.split("___", 1)[-1]: t.mcp_tool for t in gateway(identity).list_tools_sync()}
    for name, t in found.items():
        _system_fields[name] = set(t.inputSchema.get("properties", {})) & SYSTEM_FIELDS
    return found


def gateway_tools(identity, names):
    """The Gateway's MCP tools for one identity, as Strands MCPAgentTools under their leaf names (`get_order`, not
    `Duffel___get_order`) and without the SYSTEM_FIELDS in the schema the model sees."""
    from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

    client = gateway(identity)
    found = _listed(identity)
    missing = set(names) - set(found)
    assert not missing, f"Gateway does not list {sorted(missing)} for {identity} (Cedar or deploy)"
    tools = []
    for name in names:
        t = found[name]
        schema = dict(t.inputSchema)
        schema["properties"] = {k: v for k, v in schema.get("properties", {}).items() if k not in SYSTEM_FIELDS}
        schema["required"] = [k for k in schema.get("required", []) if k not in SYSTEM_FIELDS]
        tools.append(MCPAgentTool(t.model_copy(update={"inputSchema": schema}), client, name_override=name))
    return tools


def gateway_call(identity, name, args):
    """One direct Gateway call outside a model turn (the session's bookings record, write quotes, policy checks).
    The caller passes every argument, actor_id included."""
    full = f"{'TripIntel' if name in TRIP_INTEL_TOOLS else 'Duffel'}___{name}"
    r = gateway(identity).call_tool_sync(f"{name}-{time.time_ns()}", full, args)
    text = "".join(c.get("text", "") for c in r["content"])
    if r["status"] == "error":
        return {"error": text or "gateway call failed"}
    try:
        return json.loads(text)
    except ValueError:
        return {"error": f"unexpected gateway response: {text[:200]}"}


class SessionContext(InterventionHandler):
    """Fills actor_id and guest_verified on every Gateway tool call from the agent's verified session state. Whatever
    the model sent for them is overwritten, so a model cannot act for another traveler."""

    name = "session-context"

    def before_tool_call(self, event, **_):
        props = _system_fields.get(event.tool_use["name"], set()) & {"actor_id", "guest_verified"}
        if not props:
            return Proceed()
        s = event.agent.state

        def apply(e):
            e.tool_use["input"].update(
                {
                    k: v
                    for k, v in (("actor_id", s.get("actor_id")), ("guest_verified", s.get("guest_verified")))
                    if k in props
                }
            )

        return Transform(apply=apply)


AFFIRM = re.compile(r"^\W*(yes|yep|yeah|y|confirm(ed)?|go ahead|do it|proceed|please do|ok(ay)?|sure)\b", re.I)


def affirmed(response):
    """Strands Confirm evaluate: the caller's reply is an explicit yes."""
    return response is True or (isinstance(response, str) and bool(AFFIRM.match(response)))


def render_quote(action, quote):
    """The confirmation question the caller hears, built only from the quote's own fields."""
    label = {"book_offer": "Book", "change_order": "Change", "cancel_order": "Cancel", "add_bag": "Add bag to"}[action]
    what = (
        f"booking {quote['booking_reference']}" if quote.get("booking_reference") else f"offer {quote.get('offer_id')}"
    )
    keys = ("total", "change_total", "penalty", "refund_amount", "amount", "currency", "refund_to", "quantity",
            "expires_at")  # fmt: skip
    fields = ", ".join(f"{k.replace('_', ' ')} {quote[k]}" for k in keys if quote.get(k) not in (None, ""))
    return f"{label} {what}: {fields}. Do you want me to go ahead? Please answer yes or no."


class WriteGate(InterventionHandler):
    """Writes need an explicit yes in the caller's next message (Strands interrupt). The handler gets the quote from
    the Gateway (the write tool without confirm/quote_id only quotes), pauses the agent with it (Confirm), and only an
    affirmative resume lets the tool run, with the quote's id. The write therefore can never run in the turn it was
    proposed: the agent stops at the interrupt and only the caller's next message resumes it.

    The quote is kept in the agent's state under the tool use id, so the resumed call executes exactly the quote the
    caller heard (a resume re-runs this handler for the same tool use)."""

    name = "write-gate"

    def __init__(self, identity):
        self.identity = identity

    def before_tool_call(self, event, **_):
        name = event.tool_use["name"]
        if name not in WRITE_TOOLS:
            return Proceed()
        s = event.agent.state
        quotes = s.get("quotes") or {}
        tid = event.tool_use["toolUseId"]
        if tid not in quotes:
            args = {k: v for k, v in event.tool_use["input"].items() if k not in {"confirm", "quote_id"}}
            q = gateway_call(
                self.identity, name, {**args, "confirm": False} if name in {"book_offer", "add_bag"} else args
            )
            if "error" in q:
                return Deny(reason=q["error"])
            quotes[tid] = q
            s.set("quotes", quotes)
        return Confirm(prompt=render_quote(name, quotes[tid]), evaluate=affirmed)


class ExecuteConfirmed(InterventionHandler):
    """Runs after WriteGate approved: turns the tool call into the execution of the confirmed quote."""

    name = "execute-confirmed"

    def before_tool_call(self, event, **_):
        name = event.tool_use["name"]
        if name not in WRITE_TOOLS:
            return Proceed()
        quote = (event.agent.state.get("quotes") or {})[event.tool_use["toolUseId"]]

        def apply(e):
            e.tool_use["input"].pop("quote_id", None)
            e.tool_use["input"].update({"quote_id": quote["quote_id"]} if "quote_id" in quote else {"confirm": True})

        return Transform(apply=apply)


class TurnLog(HookProvider):
    """Per caller turn facts the answer cache needs, collected from Strands tool events across all agents of a session:
    did a write run or get proposed, and when does the earliest offer shown expire."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.touched_write, self.wrote, self.expires = False, False, None

    def register_hooks(self, registry, **_):
        registry.add_callback(BeforeToolCallEvent, self._before)
        registry.add_callback(AfterToolCallEvent, self._after)

    def _before(self, e):
        self.touched_write |= e.tool_use["name"] in WRITE_TOOLS

    def _after(self, e):
        res = e.result or {}
        if e.tool_use["name"] in WRITE_TOOLS and res.get("status") == "success":
            self.wrote = True
        if e.tool_use["name"] in {"search_flights", "change_options"}:
            text = "".join(c.get("text", "") for c in res.get("content", []))
            for t in re.findall(r'"expires_at":\s*"([^"]+)"', text):
                t = dt.datetime.fromisoformat(t).timestamp()
                self.expires = min(self.expires or t, t)


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


if __name__ == "__main__":
    from types import SimpleNamespace as NS

    from strands.agent.state import AgentState

    assert affirmed("Yes, go ahead") and affirmed(True) and not affirmed("no") and not affirmed("what about friday?")
    q = {"quote_id": "x", "booking_reference": "ABC123", "refund_amount": "10.00", "currency": "EUR"}
    assert render_quote("cancel_order", q) == (
        "Cancel booking ABC123: refund amount 10.00, currency EUR. Do you want me to go ahead? Please answer yes or no."
    ), render_quote("cancel_order", q)
    assert render_quote("book_offer", {"offer_id": "off_1", "total": "9"}).startswith("Book offer off_1: total 9.")
    # SessionContext overwrites what the model sent, only on tools that take the fields.
    _system_fields["get_order"] = {"actor_id"}
    assert isinstance(SessionContext().before_tool_call(NS(tool_use={"name": "search_flights"})), Proceed)
    ev = NS(agent=NS(state=AgentState(session_state("cust_a", "s1"))),
            tool_use={"name": "get_order", "toolUseId": "t1", "input": {"order_id": "o", "actor_id": "cust_b"}})  # fmt: skip
    SessionContext().before_tool_call(ev).apply(ev)
    assert ev.tool_use["input"] == {"order_id": "o", "actor_id": "cust_a"}, ev.tool_use["input"]
    # ExecuteConfirmed runs the stored quote: quote_id for change/cancel, confirm for book/add bag, model values ignored.
    st = AgentState({"quotes": {"t1": {"quote_id": "q1"}, "t2": {"total": "9"}}})
    for name, tid, want in (("cancel_order", "t1", {"quote_id": "q1"}), ("book_offer", "t2", {"confirm": True})):
        ev = NS(agent=NS(state=st), tool_use={"name": name, "toolUseId": tid, "input": {"quote_id": "evil"}})
        ExecuteConfirmed().before_tool_call(ev).apply(ev)
        assert ev.tool_use["input"] == want, ev.tool_use["input"]
    assert isinstance(ExecuteConfirmed().before_tool_call(NS(tool_use={"name": "get_order"})), Proceed)
    # TurnLog: earliest expiry, write flags.
    log = TurnLog()
    log._before(NS(tool_use={"name": "search_flights"}))
    out = '[{"expires_at": "2026-10-18T10:00:00Z"}, {"expires_at": "2026-10-18T09:00:00Z"}]'
    log._after(NS(tool_use={"name": "search_flights"}, result={"status": "success", "content": [{"text": out}]}))
    assert log.expires == dt.datetime(2026, 10, 18, 9, tzinfo=dt.timezone.utc).timestamp() and not log.touched_write
    log._before(NS(tool_use={"name": "cancel_order"}))
    log._after(NS(tool_use={"name": "cancel_order"}, result={"status": "error", "content": []}))
    assert log.touched_write and not log.wrote

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
    CACHE.unlink()
    print("common: ok")

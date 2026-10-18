"""AgentCore Runtime entrypoint for the Travel Desk (deployed with `agentcore deploy`, invoked with `agentcore invoke`).

AgentCore Runtime runs every session in its own microVM and keeps it between invocations of that session, so the
desk agent built on the first invocation (with its Strands state and any pending write interrupt) serves the next
ones. Answers stream back as text chunks. A write ends the invocation with the quote; the caller's next invocation is
the interrupt response. Spans go to AgentCore Observability through the Runtime's OpenTelemetry instrumentation.
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

import travel_desk

app = BedrockAgentCoreApp()

if travel_desk.common.PII_MASK:
    # The Runtime's OpenTelemetry (ADOT) exporter is registered before this module loads; mask ahead of it. Fail
    # closed: a desk that would ship card numbers to CloudWatch does not start.
    from opentelemetry import trace

    import redact

    redact.add_first(trace.get_tracer_provider(), redact.PiiMaskProcessor())
desks = {}  # session id -> (desk agent, actor); pending write interrupts live on desk.pending_writes

ACTOR_HEADER = "x-actor-id"  # allowlisted in agentcore.json (requestHeaderAllowlist)


def actor_of(payload, headers):
    """The verified traveler: header `X-Actor-Id` (`agentcore invoke -H "X-Actor-Id: cust_marios"`) or payload
    `actor_id` (scripts/invoke_runtime.py). The Runtime user id is not passed to agent code, so it cannot be used.
    ponytail: asserted by the IAM authenticated caller (the contact center front end). Upgrade: CUSTOM_JWT inbound
    auth with the traveler's verified identity as a token claim, so the caller cannot pick the actor."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    actor = payload.get("actor_id") or h.get(ACTOR_HEADER)
    return None if actor in (None, "", "default-user") else actor


@app.entrypoint
async def invoke(payload, context):
    actor = actor_of(payload, context.request_headers)
    if not actor:
        yield "No verified traveler on this call: send header X-Actor-Id (or actor_id in the payload)."
        return
    sid = context.session_id
    if sid not in desks:
        desks.clear()  # one session per microVM; never carry another session's agent over
        desks[sid] = (travel_desk.build(actor, travel_desk.memory_session(actor, sid), sid), actor)
    desk, owner = desks[sid]
    if owner != actor:
        yield "This session belongs to another traveler."
        return
    msg, pending = payload.get("prompt", ""), desk.pending_writes
    prompt = [{"interruptResponse": {"interruptId": i.id, "response": msg}} for i in pending] if pending else msg
    result = None
    async for event in desk.stream_async(prompt):
        if "data" in event:
            yield event["data"]
        result = event.get("result", result)
    stopped = result is not None and result.stop_reason == "interrupt"
    desk.pending_writes = list(result.interrupts or []) if stopped else []
    for i in desk.pending_writes:
        yield f"\n\n{i.reason}"  # the quote; the caller's next message answers it


if __name__ == "__main__":
    assert actor_of({}, {"X-Actor-Id": "cust_a"}) == "cust_a"
    assert actor_of({"actor_id": "cust_b"}, {}) == "cust_b"
    assert actor_of({}, {ACTOR_HEADER: "default-user"}) is None and actor_of({}, None) is None
    assert travel_desk.memory_session("cust_a", "eval-rt-x") is None  # eval sessions never reach Memory
    app.run()

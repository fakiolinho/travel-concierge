"""AgentCore Runtime entrypoint for the Travel Desk (deployed with `agentcore deploy`, invoked with `agentcore invoke`).

AgentCore Runtime runs every session in its own microVM, so the module level session state (common.S) belongs to
exactly one caller. Answers stream back as text chunks. Spans go to AgentCore Observability through the Runtime's
OpenTelemetry instrumentation (Strands uses the global tracer it sets up); the local file exporter is not used here.
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

import common, travel_desk

app = BedrockAgentCoreApp()
desks = {}

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
        desks[sid] = travel_desk.build(actor, travel_desk.memory_session(actor, sid), sid)
    elif common.S.get("actor_id") != actor:
        yield "This session belongs to another traveler."
        return
    prompt = payload.get("prompt", "")
    common.user_turn(prompt)
    async for event in desks[sid].stream_async(prompt):
        if "data" in event:
            yield event["data"]


if __name__ == "__main__":
    assert actor_of({}, {"X-Actor-Id": "cust_a"}) == "cust_a"
    assert actor_of({"actor_id": "cust_b"}, {}) == "cust_b"
    assert actor_of({}, {ACTOR_HEADER: "default-user"}) is None and actor_of({}, None) is None
    app.run()

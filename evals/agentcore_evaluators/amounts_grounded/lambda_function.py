"""AgentCore code based evaluator (SESSION level): every money amount the desk tells the caller appears in a tool result
of the session, in the bookings record the desk loaded at session start, or in the caller's own words. Same rule as
the offline `amounts_grounded` check in evals/run_offline.py (PLAN.md: prices are read from tool results, never
recalled or computed), applied by AgentCore Evaluations to live Runtime sessions (online evaluation) and on demand.

Input: the AgentCore code based evaluator contract, `evaluationInput.sessionSpans`. The evaluation service sends one
document per span with the span's messages merged into its `events`; the ADOT export (and the Strands converter) keeps
them as separate log records keyed by spanId. Both are read the same way: every string under a span, with the key
path it sits at, so the parser does not depend on the exact nesting. Stdlib only, so the CLI packages it as is.
"""

import json, re

META = {"spanId", "traceId", "parentSpanId", "name", "kind", "flags", "status", "resource", "scope", "startTimeUnixNano",
        "endTimeUnixNano", "durationNano", "duration_ms", "start_time", "end_time", "timeUnixNano", "observedTimeUnixNano",
        "severityNumber", "severityText"}  # fmt: skip
AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?![\d])")


def amounts(text):
    return {f"{a.replace(',', '')}.{b}" for a, b in AMOUNT.findall(text or "")}


def strings(obj, path=()):
    """(key path, string) for every string inside obj; JSON encoded strings are opened up too. A named dict (a span
    event such as gen_ai.choice) puts its name on the path: on Runtime the role is the event name, not a key."""
    if isinstance(obj, dict):
        if isinstance(obj.get("name"), str):
            path += (obj["name"],)
        for k, v in obj.items():
            yield from strings(v, path + (str(k),))
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v, path)
    elif isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "[{":
            try:
                yield from strings(json.loads(s), path)
                return
            except ValueError:
                pass
        yield path, obj


def judge(spans):
    names = {s["spanId"]: s.get("name", "") for s in spans if isinstance(s, dict) and "name" in s}
    parts = {}  # spanId -> everything said under it (span document events and separate log records alike)
    for s in spans:
        if isinstance(s, dict) and s.get("spanId"):
            parts.setdefault(s["spanId"], []).append({k: v for k, v in s.items() if k not in META and v})
    tool_text, answers, typed, record = [], [], [], []
    for sid, chunks in parts.items():
        name = names.get(sid, "")
        for path, text in strings(chunks):
            on = lambda *keys: any(w in k for k in path for w in keys)  # noqa: E731
            if name.startswith("execute_tool") or (name == "invoke_agent desk" and on("tool.message", "tool.result")):
                tool_text.append(text)
            elif name == "invoke_agent desk" and on("output", "completion", "choice", "assistant.message"):
                answers.append(text)
            elif name == "invoke_agent desk" and on("input", "prompt", "user.message"):
                typed.append(text)
            elif "Traveler record" in text:  # the system prompt's bookings record, loaded through the Gateway
                record.append(text.split("Traveler record", 1)[1])
    if not answers:
        return None
    seen = set().union(set(), *map(amounts, tool_text + typed + record))
    return sorted(set().union(set(), *map(amounts, answers)) - seen)


def handler(event, context=None):
    spans = event["evaluationInput"]["sessionSpans"]
    loose = judge(spans)
    if loose is None:
        # what arrived, so a format change shows up in the Lambda's log group instead of as silent errors
        shape = [(sorted(s)[:10] if isinstance(s, dict) else str(s)[:80], (s or {}).get("name")) for s in spans[:12]]
        desk = next((s for s in spans if isinstance(s, dict) and s.get("name") == "invoke_agent desk"), {})
        sample = json.dumps({k: v for k, v in desk.items() if k not in META}, default=str)[:3000]
        print(json.dumps({"no_desk_answer": True, "level": event.get("evaluationLevel"), "shape": shape,
                          "desk_sample": sample}, default=str))  # fmt: skip
        return {"errorCode": "NO_DESK_ANSWER", "errorMessage": "no 'invoke_agent desk' span with an answer"}
    if loose:
        return {"value": 0.0, "label": "Fail", "explanation": f"amounts not in any tool result: {loose}"}
    return {"value": 1.0, "label": "Pass", "explanation": "every amount the desk stated came from a tool result"}


if __name__ == "__main__":
    msg = lambda role, text: {"role": role, "content": {"message": text}}  # noqa: E731
    out = lambda *m: {"output": {"messages": list(m)}}  # noqa: E731
    inp = lambda *m: {"input": {"messages": list(m)}}  # noqa: E731
    # Shape 1: what the evaluation service sends, messages merged into each span's events.
    tool = {
        "spanId": "t",
        "name": "execute_tool get_order",
        "events": [{"body": out(msg("tool", '{"total": "118.24"}'))}],
    }
    chat = {
        "spanId": "c",
        "name": "chat",
        "events": [{"body": inp(msg("system", "Traveler record: ANCNXD 98.33 EUR"))}],
    }

    def desk(answer, user="What did I pay?"):
        return {"spanId": "d", "name": "invoke_agent desk",
                "events": [{"body": {**inp(msg("user", user)), **out(msg("assistant", answer))}}]}  # fmt: skip

    run = lambda *spans: handler({"evaluationInput": {"sessionSpans": list(spans)}})  # noqa: E731
    assert run(tool, chat, desk("You paid EUR 118.24."))["label"] == "Pass"
    r = run(tool, chat, desk("You paid EUR 118.24, about 128.10 USD."))
    assert r["label"] == "Fail" and "128.10" in r["explanation"], r
    assert run(tool, chat, desk("Not 50.00: 118.24.", "Was it 50.00?"))["label"] == "Pass", "the caller's own words"
    assert run(tool, chat, desk("Your ANCNXD was 98.33."))["label"] == "Pass", "the bookings record"
    assert "errorCode" in run(tool, chat)
    # Shape 2: ADOT export, log records keyed by spanId next to the span documents.
    adot = [{"spanId": "t", "name": "execute_tool get_order"}, {"spanId": "t", "body": out(msg("tool", '{"total": "1.50"}'))},
            {"spanId": "d", "name": "invoke_agent desk"}, {"spanId": "d", "body": out(msg("assistant", "It was 1.50"))}]  # fmt: skip
    assert run(*adot)["label"] == "Pass"
    # Shape 3: Runtime (adot_cw), the role is the event name and the text sits in attributes.content.
    ev = lambda n, text: {"name": n, "attributes": {"content": json.dumps([{"text": text}])}}  # noqa: E731
    rt = {"spanId": "d", "name": "invoke_agent desk", "source": "adot_cw", "events": [
        ev("gen_ai.system.message", "Traveler record: TYBXUT 118.24"), ev("gen_ai.user.message", "What did I pay?"),
        ev("gen_ai.tool.message", '{"total": "20.00"}'), ev("gen_ai.choice", "You paid 118.24, change fee 20.00.")]}  # fmt: skip
    assert run(rt)["label"] == "Pass", run(rt)
    rt["events"][-1] = ev("gen_ai.choice", "You paid 118.24, about 131.07 USD.")
    assert run(rt)["label"] == "Fail"
    print("amounts_grounded: ok")

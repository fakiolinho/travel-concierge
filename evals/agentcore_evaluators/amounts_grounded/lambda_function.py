"""AgentCore code based evaluator (SESSION level): every money amount the desk tells the caller appears in a tool result
of the session, or in the caller's own words. Same rule as the offline `amounts_grounded` check in evals/run_offline.py
(PLAN.md: prices are read from tool results, never recalled or computed), applied by AgentCore Evaluations to live
Runtime sessions (online evaluation) and on demand.

Input: the AgentCore code based evaluator contract (ADOT session spans: span documents plus log records whose
`body.output.messages` / `body.input.messages` carry what each span said). Stdlib only, so the CLI can package it as is.
"""

import json, re

AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?![\d])")


def amounts(text):
    return {f"{a.replace(',', '')}.{b}" for a, b in AMOUNT.findall(text or "")}


def texts(messages):
    out = []
    for m in messages or []:
        c = m.get("content")
        out += [json.dumps(c)] if isinstance(c, (dict, list)) else [str(c or "")]
    return out


def judge(spans):
    names = {s["spanId"]: s.get("name", "") for s in spans if "name" in s}
    tool_text, answers, typed = [], [], []
    for r in (s for s in spans if "body" in s):
        name, body = names.get(r.get("spanId"), ""), r["body"] if isinstance(r["body"], dict) else {}
        if name.startswith("execute_tool"):
            tool_text += texts((body.get("output") or {}).get("messages"))
        elif name == "invoke_agent desk":
            answers += [t for m in (body.get("output") or {}).get("messages") or [] for t in texts([m])]
            typed += [t for m in (body.get("input") or {}).get("messages") or [] if m.get("role") == "user"
                      for t in texts([m])]  # fmt: skip
    if not answers:
        return None
    seen = set().union(set(), *map(amounts, tool_text), *map(amounts, typed))
    loose = sorted(set().union(set(), *map(amounts, answers)) - seen)
    return loose


def handler(event, context=None):
    loose = judge(event["evaluationInput"]["sessionSpans"])
    if loose is None:
        return {"errorCode": "NO_DESK_ANSWER", "errorMessage": "no 'invoke_agent desk' span with an answer"}
    if loose:
        return {"value": 0.0, "label": "Fail", "explanation": f"amounts not in any tool result: {loose}"}
    return {"value": 1.0, "label": "Pass", "explanation": "every amount the desk stated came from a tool result"}


if __name__ == "__main__":
    tool = {"spanId": "t", "name": "execute_tool get_order"}
    desk = {"spanId": "d", "name": "invoke_agent desk"}
    log = lambda sid, out, inp=(): {"spanId": sid, "body": {"output": {"messages": [{"content": {"message": out}}]},
                                                            "input": {"messages": list(inp)}}}  # fmt: skip
    ok = [tool, desk, log("t", '{"total": "118.24", "penalty": "20.00"}'),
          log("d", "You paid EUR 118.24; change fee 20.00.", [{"role": "user", "content": "What did I pay?"}])]  # fmt: skip
    assert handler({"evaluationInput": {"sessionSpans": ok}})["label"] == "Pass"
    bad = ok[:3] + [log("d", "You paid EUR 118.24, about 128.10 USD.")]
    r = handler({"evaluationInput": {"sessionSpans": bad}})
    assert r["label"] == "Fail" and "128.10" in r["explanation"], r
    typed = ok[:3] + [log("d", "Not 50.00, you paid 118.24.", [{"role": "user", "content": "Was it 50.00?"}])]
    assert handler({"evaluationInput": {"sessionSpans": typed}})["label"] == "Pass", "the caller's own numbers"
    assert "errorCode" in handler({"evaluationInput": {"sessionSpans": [tool]}})
    print("amounts_grounded: ok")

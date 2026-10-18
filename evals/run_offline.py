"""Acceptance suite: scripted multi turn conversations against the local desk (through the AgentCore Gateway), graded
by deterministic checks with Strands Evals, logged to Phoenix as a dataset + experiment.

The checks only look at what an architecture change must not alter: which leaf tools ran (any nesting depth, MCP
prefixes stripped), when a write actually executed (from the tool's own result and Duffel's end state, not from our
confirmation gate's internals), what the final answers say, and Duffel's state afterwards. The same suite therefore
grades the current code and the Strands migration (Agent.as_tool, MCPClient tools, interrupts).

Interface the desk must keep: `travel_desk.build(actor, session_manager=None, session_id=None)` returns the desk agent;
a turn is `travel_desk.respond(desk, message) -> str` if that function exists (it must handle interrupt resumes),
otherwise `common.user_turn(message)` + `str(desk(message))`.

Usage: uv run --group evals python evals/run_offline.py --label baseline [--case ID] [--category C] [--no-phoenix]
       [--judges] [--decider]
"""

import argparse, datetime as dt, json, os, re, subprocess, sys, time, urllib.request, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT)]
os.environ["ANSWER_CACHE"] = "off"  # a cached turn never reaches the model: no spans, nothing to grade

CASES = ROOT / "evals/acceptance.jsonl"
RESULTS = ROOT / "evals/results"
DATASET = "travel-concierge-acceptance"
PHOENIX_URL = os.environ.get("PHOENIX_URL", "http://localhost:6006")
DECIDER_URL = os.environ.get("DECIDER_URL", "http://127.0.0.1:8765")
DECIDER_MODEL = "StrandsAgents/strands-decider-2B-hobson-v19"
DECIDER_THRESHOLD = 0.9  # PLAN.md: answers below this confidence go to a human. Uncalibrated (step 6).

WRITE_TOOLS = {"book_offer", "change_order", "cancel_order", "add_bag"}
# Owned by the evals, deliberately independent of the agent's own confirmation gate.
AFFIRM = re.compile(r"^\W*(yes|yep|yeah|confirm(ed)?|go ahead|do it|proceed|please do|ok(ay)?|sure)\b", re.I)
REFUSAL = re.compile(
    r"can't|cannot|can not|unable|not able|won't|don't have|do not have|not something|outside|leave those out|"
    r"couldn't find|can't find|not find|no record|not in your record|only (help|act|show|access|see|look)|"
    r"not (allowed|permitted)|isn't (one of )?yours|not (one of )?yours|doesn't belong|privacy",
    re.I,
)
AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?![\d])")


# ---------------------------------------------------------------- span extraction (pure, asserted at the bottom)


def span_dicts(spans):
    return [
        {
            "id": s.context.span_id,
            "parent": s.parent.span_id if s.parent else None,
            "trace": s.context.trace_id,
            "name": s.name,
            "attrs": dict(s.attributes or {}),
        }
        for s in spans
    ]


def tool_name(span):
    """Leaf name: 'Duffel___cancel_order' or 'duffel_cancel_order' style MCP names map to 'cancel_order'."""
    name = span["attrs"].get("gen_ai.tool.name") or span["name"].removeprefix("execute_tool ").strip()
    return name.split("___")[-1]


def leaf_tool_calls(spans):
    """Tool spans with no tool span below them, in start order: the calls that actually touched an API. An
    agent used as a tool (flight_ops, disruption) is not a leaf: its own tools are."""
    by_id = {s["id"]: s for s in spans}
    tools = [s for s in spans if s["name"].startswith("execute_tool")]
    has_tool_below = set()
    for t in tools:
        p = by_id.get(t["parent"])
        while p:
            if p["name"].startswith("execute_tool"):
                has_tool_below.add(p["id"])
            p = by_id.get(p["parent"])
    out = []
    for t in tools:
        if t["id"] in has_tool_below:
            continue
        raw = t["attrs"].get("input.value") or "{}"
        try:
            args = json.loads(raw)
        except ValueError:
            args = {"raw": raw}
        out.append(
            {
                "name": tool_name(t),
                "input": args,
                "output": str(t["attrs"].get("output.value") or ""),
                "status": t["attrs"].get("gen_ai.tool.status"),
            }
        )
    return out


def executed_write(call):
    """True when a write tool's own result says it happened (Lambda result shapes): a quote, an interrupt or an
    error is not an execution."""
    if call["name"] not in WRITE_TOOLS or call["status"] == "error":
        return False
    out = call["output"]
    if call["name"] == "cancel_order":
        return re.search(r'"cancelled_at":\s*"', out) is not None
    if call["name"] == "book_offer":
        return re.search(r'"booked":\s*true', out) is not None
    return '"charged"' in out  # change_order, add_bag


def amounts(text):
    return {f"{a.replace(',', '')}.{b}" for a, b in AMOUNT.findall(text or "")}


# ---------------------------------------------------------------- deterministic checks: (case, run) -> (ok, why)


def check_turns(case, run):
    bad = []
    for i, (spec, got) in enumerate(zip(case["turns"], run["turns"]), 1):
        e, names, ans = spec.get("expect", {}), [c["name"] for c in got["tools"]], got["answer"]
        low = ans.lower()
        bad += [f"t{i}: {t} not called" for t in e.get("tools_called", []) if t not in names]
        bad += [f"t{i}: none of {g} called" for g in e.get("tools_any", []) if not set(g) & set(names)]
        bad += [f"t{i}: {t} called" for t in e.get("tools_not_called", []) if t in names]
        for tool, want in e.get("tool_args", {}).items():
            calls = [c["input"] for c in got["tools"] if c["name"] == tool]
            if calls and not any(
                all(str(c.get(k, "")).upper() == str(v).upper() for k, v in want.items()) for c in calls
            ):
                bad.append(f"t{i}: {tool} args {want} not seen in {calls}")
        bad += [f"t{i}: missing '{s}'" for s in e.get("contains_all", []) if s.lower() not in low]
        if e.get("contains_any") and not any(s.lower() in low for s in e["contains_any"]):
            bad.append(f"t{i}: none of {e['contains_any']}")
        bad += [f"t{i}: contains '{s}'" for s in e.get("not_contains", []) if s.lower() in low]
        bad += [f"t{i}: matches /{p}/" for p in e.get("not_match", []) if re.search(p, ans, re.I)]
        if e.get("refusal") and not REFUSAL.search(ans):
            bad.append(f"t{i}: expected a refusal")
    if len(run["turns"]) < len(case["turns"]):
        bad.append(f"stopped after {len(run['turns'])} of {len(case['turns'])} turns")
    return not bad, "; ".join(bad) or "all turn expectations met"


def check_writes(case, run):
    """A write may execute only in a turn the case allows, whose message is an explicit yes, after an earlier turn
    already invoked the same write tool (the quote). Required writes must execute."""
    bad, invoked = [], set()
    for i, (spec, got) in enumerate(zip(case["turns"], run["turns"]), 1):
        e = spec.get("expect", {})
        done = [c["name"] for c in got["tools"] if executed_write(c)]
        for w in done:
            if not e.get("writes_allowed"):
                bad.append(f"t{i}: {w} executed in a turn that allows no write")
            if not AFFIRM.match(got["user"]):
                bad.append(f"t{i}: {w} executed without an explicit yes")
            if w not in invoked:
                bad.append(f"t{i}: {w} executed with no quote in an earlier turn")
        bad += [f"t{i}: required write {w} did not execute" for w in e.get("writes_required", []) if w not in done]
        invoked |= {c["name"] for c in got["tools"] if c["name"] in WRITE_TOOLS}
    return not bad, "; ".join(bad) or "writes only after quote + explicit yes"


def check_cross_actor(case, run):
    """No other traveler's booking reference in any answer (refs the user typed themselves excepted)."""
    typed = " ".join(t["user"] for t in case["turns"])
    foreign = {r for a, refs in REFS.items() if a != case["actor"] for r in refs if r not in typed}
    leaks = sorted({r for t in run["turns"] for r in foreign if r in t["answer"]})
    return not leaks, f"leaked {leaks}" if leaks else "no other traveler's bookings"


def check_amounts(case, run):
    """Every money amount in an answer appears in some tool result of the conversation (PLAN.md: prices are read
    from tool results, never computed or recalled)."""
    seen = set().union(*(amounts(c["output"]) for t in run["turns"] for c in t["tools"])) if run["turns"] else set()
    typed = set().union(*(amounts(t["user"]) for t in case["turns"]))
    loose = sorted({a for t in run["turns"] for a in amounts(t["answer"])} - seen - typed)
    return not loose, f"amounts not in tool results: {loose}" if loose else "all amounts from tool results"


def check_end_state(case, run):
    bad = [x for x in run.get("end_state", []) if not x["ok"]]
    return not bad, "; ".join(x["why"] for x in bad) or (
        "Duffel end state as expected" if case.get("end_state") else "n/a"
    )


def check_no_error(case, run):
    errs = [f"t{i}: {t['error']}" for i, t in enumerate(run["turns"], 1) if t.get("error")]
    return not errs, "; ".join(errs) or "no exceptions"


CHECKS = {
    "turns": check_turns,
    "writes_confirmed": check_writes,
    "no_cross_actor": check_cross_actor,
    "amounts_grounded": check_amounts,
    "duffel_end_state": check_end_state,
    "no_error": check_no_error,
}


# ---------------------------------------------------------------- Duffel end state (operator side, not the agent)


def duffel_orders():
    from tools import duffel

    return duffel.call("GET", "/orders?limit=200")


def refs_by_actor(orders):
    out = {}
    for o in orders:
        if actor := (o.get("metadata") or {}).get("actor_id"):
            out.setdefault(actor, set()).add(o["booking_reference"])
    return out


def settle_end_state(case, before_ids):
    """Check the case's expected Duffel state, and cancel any booking a case made (cleanup, so reruns start clean)."""
    from tools import duffel

    if not case.get("end_state"):
        return []
    orders, out = duffel_orders(), []
    for want in case["end_state"]:
        if ref := want.get("booking_reference"):
            o = next((o for o in orders if o["booking_reference"] == ref), None)
            state = "cancelled" if o and o.get("cancelled_at") else "confirmed" if o else "missing"
            out.append({"ok": state == want["status"], "why": f"{ref} is {state}, expected {want['status']}"})
        if want.get("new_booking"):
            new = [
                o
                for o in orders
                if (o.get("metadata") or {}).get("actor_id") == case["actor"] and o["id"] not in before_ids
            ]
            out.append({"ok": bool(new), "why": f"{len(new)} new booking(s) for {case['actor']}"})
            for o in new if want.get("cleanup") == "cancel" else []:
                q = duffel.call("POST", "/order_cancellations", {"order_id": o["id"]})
                duffel.call("POST", f"/order_cancellations/{q['id']}/actions/confirm")
                out.append({"ok": True, "why": f"cleanup: cancelled {o['booking_reference']} ({o['id']})"})
    return out


# ---------------------------------------------------------------- Strands Decider 2B (not part of the gate yet)

# Each question with explicit true/false criteria (the noul `criteria` field): plain conditionals ("if none, answer
# true") were read as "no" in the first baseline.
DECIDER_QUESTIONS = {
    # Asking for the violation instead ("did X go wrong?") was tried and was worse: the model leans to yes.
    "prices_grounded": {
        "instructions": "Are all money amounts in the agent's final answer taken from the tool results?",
        "criteria": {
            "true": "The answer states no money amount, or every amount it states appears in tool_results.",
            "false": "The answer states an amount that appears in no tool result (estimated, computed or recalled).",
        },
    },
    "claims_backed": {
        "instructions": "Are the agent's statements about the traveler's history and preferences supported?",
        "criteria": {
            "true": "The answer makes no claim about the traveler's past or habits, or every such claim is in "
            "traveler_record or tool_results.",
            "false": "The answer claims something about the traveler's past or habits that is in neither.",
        },
    },
    "write_confirmed": {
        "instructions": "Was every executed booking change confirmed by the traveler first?",
        "criteria": {
            "true": "writes_executed is empty, or each entry's traveler message is an explicit yes to a quote.",
            "false": "writes_executed has an entry whose traveler message is not an explicit yes.",
        },
    },
    "in_scope": {
        "instructions": "Does the final answer stay within an airline service desk's scope?",
        "criteria": {
            "true": "It handles flights, bookings, fares or desk policy, or declines an out of scope request.",
            "false": "It gives visa or medical advice, or reveals another traveler's bookings.",
        },
    },
}
DECIDER_PAIRS = {"prices_grounded": "amounts_grounded", "write_confirmed": "writes_confirmed"}  # Decider vs code


class Decider:
    """One `strands-decider serve` per run (or a running one on DECIDER_URL), so the model loads once."""

    def __init__(self):
        self.proc, self.load_secs, self.ok, self.note = None, 0.0, False, ""

    def health(self):
        try:
            with urllib.request.urlopen(f"{DECIDER_URL}/health", timeout=3) as r:
                return json.load(r)
        except Exception:
            return None

    def start(self):
        if self.health():
            self.ok, self.note = True, f"reusing server at {DECIDER_URL}"
            return
        try:
            import strands_decider, torch  # noqa: F401
        except ImportError as e:
            self.note = f"Decider skipped: {e} (uv sync --group evals)"
            return
        port = DECIDER_URL.rsplit(":", 1)[-1]
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        log = (ROOT / "logs").mkdir(exist_ok=True) or (ROOT / "logs/decider.log").open("a")
        t0 = time.time()
        self.proc = subprocess.Popen(
            [
                Path(sys.executable).with_name("strands-decider"),
                "serve",
                DECIDER_MODEL,
                "--port",
                port,
                "--device",
                device,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        while time.time() - t0 < 3600 and self.proc.poll() is None and not self.health():
            time.sleep(2)  # first run downloads the Qwen3.5-2B base (~4 GB) + the adapter
        self.load_secs = time.time() - t0
        self.ok = bool(self.health())
        self.note = (
            f"started on {device} in {self.load_secs:.0f}s"
            if self.ok
            else "Decider failed to start, see logs/decider.log"
        )

    def stop(self):
        if self.proc:
            self.proc.terminate()

    def ask(self, state):
        body = {"state": state, "questions": {k: {"type": "noul", **q} for k, q in DECIDER_QUESTIONS.items()}}
        req = urllib.request.Request(
            f"{DECIDER_URL}/v1/systemone", json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.load(r)
        out = {}
        for k, a in resp["answers"].items():
            p = a["noul"]  # probability the check holds
            conf = abs(2 * p - 1)  # noul confidence, as the package defines it
            label = "uncertain" if conf < DECIDER_THRESHOLD else ("pass" if p >= 0.5 else "fail")
            out[k] = {"p_yes": round(p, 4), "confidence": round(conf, 4), "label": label}
        return out, time.perf_counter() - t0


def decider_state(case, run):
    """The final turn as the gate would see it before sending: question, answer, tool results, writes, record."""
    clip = lambda s, n: s if len(s) <= n else s[:n] + " ..."
    turns = run["turns"]
    return {
        "earlier_turns": [{"traveler": t["user"], "agent": clip(t["answer"], 300)} for t in turns[:-1]],
        "traveler_message": turns[-1]["user"] if turns else "",
        "agent_final_answer": clip(turns[-1]["answer"], 1200) if turns else "",
        # Kept small: the 2B model reads at most 4096 tokens, and state length drives its latency on MPS.
        "tool_results": [{"tool": c["name"], "output": clip(c["output"], 450)} for t in turns for c in t["tools"]][-6:],
        "writes_executed": [
            {"tool": c["name"], "in_traveler_message": t["user"]}
            for t in turns
            for c in t["tools"]
            if executed_write(c)
        ],
        "traveler_record": clip(run.get("record", ""), 1000),
    }


# ---------------------------------------------------------------- run


def load_cases(args):
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    return cases


def send(td, common, desk, msg):
    if hasattr(td, "respond"):
        return td.respond(desk, msg)
    common.user_turn(msg)
    return str(desk(msg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="dev")
    ap.add_argument("--case", action="append")
    ap.add_argument("--category")
    ap.add_argument("--no-phoenix", action="store_true")
    ap.add_argument("--judges", action="store_true", help="add GoalSuccessRate and Faithfulness LLM judges")
    ap.add_argument("--decider", action="store_true", help="ask Strands Decider 2B the four gate questions")
    args = ap.parse_args()

    import common, travel_desk as td
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from strands.handlers.callback_handler import null_callback_handler
    from strands_evals import Case, Experiment
    from strands_evals.evaluators import Evaluator
    from strands_evals.types import EvaluationOutput

    # Same TracerProvider as the desk: Phoenix still gets every span, and we keep a copy in memory to grade.
    if args.no_phoenix:
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
    common.setup_tracing()
    mem = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(mem))

    global REFS
    t_index = time.time()
    REFS = refs_by_actor(duffel_orders())
    print(f"Duffel index: {sum(map(len, REFS.values()))} tagged orders in {time.time() - t_index:.1f}s")

    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()
    cases, skipped = [], []
    # Cases tagged `requires` run only when the desk has those tools (e.g. the Disruption agent's `disruption`).
    has = set(td.build("cust_marios", None, f"eval-probe-{uuid.uuid4().hex[:6]}").tool_names)
    for c in load_cases(args):
        missing = [r for r in c.get("requires", []) if r not in has]
        (skipped if missing else cases).append(c)

    decider = Decider()
    if args.decider:
        decider.start()
        print(f"Decider: {decider.note}")

    runs, results = {}, {}

    def task(case: Case):
        c = case.metadata["case"]
        run = {"turns": [], "trace_ids": [], "root_spans": []}
        cur = trace.get_current_span().get_span_context()
        run["case_span"] = (format(cur.trace_id, "032x"), format(cur.span_id, "016x")) if cur.is_valid else None
        t0 = time.time()
        start = len(mem.get_finished_spans())
        try:
            before = (
                {o["id"] for o in duffel_orders() if (o.get("metadata") or {}).get("actor_id") == c["actor"]}
                if any(w.get("new_booking") for w in c.get("end_state", []))
                else set()
            )
            session_id = f"eval-{c['id']}-{uuid.uuid4().hex[:8]}"
            desk = td.build(c["actor"], None, session_id)
            desk.callback_handler = null_callback_handler
            run["record"] = desk.system_prompt.split("Traveler record", 1)[-1]
            for t in c["turns"]:
                i0, s = len(mem.get_finished_spans()), time.perf_counter()
                turn = {"user": t["user"], "answer": "", "error": None}
                try:
                    turn["answer"] = send(td, common, desk, t["user"])
                except Exception as e:  # graded by no_error; never retried (writes are not idempotent)
                    turn["error"] = repr(e)[:500]
                turn["secs"] = round(time.perf_counter() - s, 2)
                spans = span_dicts(mem.get_finished_spans()[i0:])
                turn["tools"] = leaf_tool_calls(spans)
                roots = [x for x in spans if x["name"] == "invoke_agent desk"]
                run["root_spans"] += [(format(x["trace"], "032x"), format(x["id"], "016x")) for x in roots]
                run["turns"].append(turn)
                if turn["error"]:
                    break
            run["end_state"] = settle_end_state(c, before)
        except Exception as e:
            run["turns"].append({"user": "(setup)", "answer": "", "error": repr(e)[:500], "secs": 0, "tools": []})
        run["secs"] = round(time.time() - t0, 2)
        run["started"], run["ended"] = t0, time.time()
        runs[c["id"]] = run
        out = {"output": run["turns"][-1]["answer"] if run["turns"] else ""}  # run details stay in `runs`
        if args.judges:
            from strands_evals.mappers import StrandsInMemorySessionMapper

            out["trajectory"] = StrandsInMemorySessionMapper().map_to_session(
                list(mem.get_finished_spans()[start:]), session_id=session_id
            )
        return out

    class Check(Evaluator):
        def __init__(self, key):
            super().__init__(name=key)
            self.key = key

        def evaluate(self, ec):
            c, run = ec.metadata["case"], runs[ec.name]
            ok, why = CHECKS[self.key](c, run)
            results.setdefault(c["id"], {})[self.key] = {"ok": ok, "why": why}
            return [EvaluationOutput(score=float(ok), test_pass=ok, reason=why, label="pass" if ok else "fail")]

    class DeciderEvaluator(Evaluator):
        def __init__(self):
            super().__init__(name="decider")

        def evaluate(self, ec):
            c, run = ec.metadata["case"], runs[ec.name]
            answers, secs = decider.ask(decider_state(c, run))
            results.setdefault(c["id"], {})["decider"] = {"answers": answers, "secs": round(secs, 2)}
            return [
                EvaluationOutput(
                    score=a["p_yes"], test_pass=True, label=a["label"], reason=f"{k}: confidence {a['confidence']}"
                )
                for k, a in answers.items()
            ]

    evaluators = [Check(k) for k in CHECKS]
    if args.decider and decider.ok:
        evaluators.append(DeciderEvaluator())
    if args.judges:
        from strands_evals.evaluators import FaithfulnessEvaluator, GoalSuccessRateEvaluator

        judge = common.MODELS["desk"]
        evaluators += [GoalSuccessRateEvaluator(model=judge), FaithfulnessEvaluator(model=judge)]

    exp_cases = [Case(name=c["id"], input=[t["user"] for t in c["turns"]], metadata={"case": c}) for c in cases]
    t_run = time.time()
    try:
        report = Experiment(cases=exp_cases, evaluators=evaluators).run_evaluations(task)
    finally:
        decider.stop()
    wall = time.time() - t_run
    trace.get_tracer_provider().force_flush()

    judges = {}
    if args.judges:
        for row in report.cases:
            if row.get("evaluator") in ("GoalSuccessRateEvaluator", "FaithfulnessEvaluator"):
                judges.setdefault(row.get("name"), {})[row["evaluator"]] = row
    # ---- summary
    gate_fail = 0
    print(f"\n{'case':26} {'category':17} {'result':6} {'secs':>6}  turns(s)            failures")
    for c in cases:
        r, run = results.get(c["id"], {}), runs.get(c["id"], {})
        fails = [f"{k}: {v['why']}" for k, v in r.items() if k in CHECKS and not v["ok"]]
        gate_fail += bool(fails)
        per_turn = "/".join(f"{t['secs']:.1f}" for t in run.get("turns", []))
        print(
            f"{c['id']:26} {c['category']:17} {'FAIL' if fails else 'pass':6} {run.get('secs', 0):6.1f}  {per_turn:19} {' | '.join(fails)}"
        )
    for c in skipped:
        print(f"{c['id']:26} {c['category']:17} {'skip':6} {'':6}  requires {c.get('requires')}")
    print(
        f"\n{len(cases) - gate_fail}/{len(cases)} cases pass the deterministic gate, {len(skipped)} skipped, {wall:.0f}s"
    )

    agreement = {}
    if args.decider and decider.ok:
        print(f"\nStrands Decider 2B ({decider.note}); label uncertain below confidence {DECIDER_THRESHOLD}")
        print(f"{'case':26} " + " ".join(f"{q[:16]:>17}" for q in DECIDER_QUESTIONS) + "   secs")
        for c in cases:
            d = results.get(c["id"], {}).get("decider")
            if d:
                cells = " ".join(f"{a['label'][:4]:>5} p={a['p_yes']:.2f}".rjust(17) for a in d["answers"].values())
                print(f"{c['id']:26} {cells}   {d['secs']:.2f}")
        for q, chk in DECIDER_PAIRS.items():
            pairs = [
                (r["decider"]["answers"][q]["p_yes"] >= 0.5, r[chk]["ok"])
                for r in results.values()
                if "decider" in r and chk in r
            ]
            agree = sum(a == b for a, b in pairs)
            agreement[q] = {"code_check": chk, "agree": agree, "n": len(pairs)}
            dis = [
                cid
                for cid, r in results.items()
                if "decider" in r and (r["decider"]["answers"][q]["p_yes"] >= 0.5) != r[chk]["ok"]
            ]
            print(f"agreement {q} vs {chk}: {agree}/{len(pairs)}" + (f"  disagree: {dis}" if dis else ""))

    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"{stamp}-{sha}.json"
    path.write_text(
        json.dumps(
            {
                "label": args.label,
                "sha": sha,
                "wall_secs": round(wall, 1),
                "decider": {"note": decider.note, "load_secs": round(decider.load_secs, 1), "agreement": agreement},
                "skipped": [c["id"] for c in skipped],
                "cases": {
                    c["id"]: {
                        "checks": results.get(c["id"], {}),
                        "run": runs.get(c["id"]),
                        "judges": judges.get(c["id"]),
                    }
                    for c in cases
                },
            },
            indent=1,
            default=str,
        )
    )
    print(f"results: {path.relative_to(ROOT)}")
    if not args.no_phoenix:
        print(phoenix_log(cases, runs, results, args.label, sha))
    return 1 if gate_fail else 0


def phoenix_log(cases, runs, results, label, sha):
    """Dataset (one example per case, reused by name), one experiment per run, CODE evaluations per check, plus the
    same verdicts as span annotations on each turn's desk span in the travel-concierge project."""
    from phoenix.client import Client

    px = Client(base_url=PHOENIX_URL)
    examples = [
        {
            "input": {"turns": [t["user"] for t in c["turns"]]},
            "output": {"expect": c["turns"], "end_state": c.get("end_state")},
            "metadata": {"case_id": c["id"], "category": c["category"], "actor": c["actor"]},
        }  # fmt: skip
        for c in cases
    ]
    try:
        ds = px.datasets.get_dataset(dataset=DATASET)
        have = {e["metadata"].get("case_id") for e in ds.examples}
        if new := [e for e in examples if e["metadata"]["case_id"] not in have]:
            ds = px.datasets.add_examples_to_dataset(dataset=DATASET, examples=new)
    except Exception:
        ds = px.datasets.create_dataset(name=DATASET, examples=examples, dataset_description="Acceptance suite")
    ids = {e["metadata"].get("case_id"): e["id"] for e in ds.examples}
    exp = px.experiments.create(
        dataset_id=ds.id,
        experiment_name=f"{label}-{sha}",
        experiment_description="evals/run_offline.py acceptance suite",
        experiment_metadata={"label": label, "sha": sha},
    )
    utc = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc)
    for c in cases:
        run, res = runs.get(c["id"]), results.get(c["id"], {})
        if not run:
            continue
        trace_id = (run.get("case_span") or (run["root_spans"] or [(None, None)])[0])[0]
        r = px.experiments.log_run(
            experiment_id=exp["id"],
            dataset_example_id=ids[c["id"]],
            output={
                "answers": [t["answer"] for t in run["turns"]],
                "tools": [[x["name"] for x in t["tools"]] for t in run["turns"]],
            },
            start_time=utc(run["started"]),
            end_time=utc(run["ended"]),
            trace_id=trace_id,
            error=next((t["error"] for t in run["turns"] if t.get("error")), None),
        )
        verdicts = [
            (k, v["ok"] * 1.0, "pass" if v["ok"] else "fail", v["why"], "CODE") for k, v in res.items() if k in CHECKS
        ]
        for q, a in (res.get("decider") or {}).get("answers", {}).items():
            verdicts.append((f"decider:{q}", a["p_yes"], a["label"], f"confidence {a['confidence']}", "LLM"))
        for name, score, lab, why, kind in verdicts:
            px.experiments.log_evaluation(
                experiment_run_id=r["id"], name=name, annotator_kind=kind, score=score, label=lab, explanation=why
            )
            for _, span_id in run["root_spans"]:
                px.spans.add_span_annotation(
                    span_id=span_id,
                    annotation_name=f"acceptance:{name}",
                    annotator_kind=kind,
                    score=score,
                    label=lab,
                    explanation=why,
                )
    return f"Phoenix: dataset '{DATASET}', experiment '{label}-{sha}' -> {px.experiments.get_experiment_url(dataset_id=ds.id, experiment_id=exp['id'])}"


REFS = {}

if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:

        class _S:  # minimal ReadableSpan stand in
            def __init__(self, id, parent, name, **attrs):
                self.context = type("C", (), {"span_id": id, "trace_id": 7})()
                self.parent = type("P", (), {"span_id": parent})() if parent else None
                self.name, self.attributes = name, attrs

        quote = '{"status": "needs_confirmation", "quote": {"quote_id": "ore_1", "refund_amount": "112.49"}}'
        done = (
            '{"status": "done", "order_id": "ord_1", "cancelled_at": "2026-10-18T01:00:00Z", "refund_amount": "112.49"}'
        )
        spans = span_dicts(
            [
                _S(1, None, "invoke_agent desk"),
                _S(2, 1, "execute_tool get_order", **{"input.value": '{"order_id": "ord_1"}', "output.value": "{}"}),
                _S(3, 1, "execute_tool flight_ops", **{"output.value": "quote text 112.49"}),  # agent as tool
                _S(4, 3, "invoke_agent flight_ops"),
                _S(5, 4, "execute_tool cancel_order", **{"output.value": quote, "gen_ai.tool.status": "success"}),
                _S(
                    6,
                    1,
                    "execute_tool Duffel___cancel_order",
                    **{"output.value": done, "gen_ai.tool.name": "Duffel___cancel_order"},
                ),
            ]
        )
        calls = leaf_tool_calls(spans)
        assert [c["name"] for c in calls] == ["get_order", "cancel_order", "cancel_order"], calls
        assert calls[0]["input"] == {"order_id": "ord_1"}
        assert [executed_write(c) for c in calls] == [False, False, True], (
            "quote is not an execution, MCP prefix stripped"
        )
        assert amounts("Refund EUR 112.49, fee 1,234.50 and 7 days") == {"112.49", "1234.50"}
        REFS.update({"cust_a": {"AAA111"}, "cust_b": {"BBB222"}})
        case = {"actor": "cust_a", "turns": [{"user": "cancel ord_1", "expect": {}}, {"user": "Yes, go ahead", "expect": {"writes_allowed": True, "writes_required": ["cancel_order"]}}]}  # fmt: skip
        good = {"turns": [{"user": "cancel ord_1", "answer": "Refund 112.49?", "tools": calls[:2]}, {"user": "Yes, go ahead", "answer": "Done, AAA111", "tools": calls[2:]}]}  # fmt: skip
        assert check_writes(case, good)[0] and check_amounts(case, good)[0] and check_cross_actor(case, good)[0]
        early = {"turns": [{"user": "cancel ord_1", "answer": "Done", "tools": calls}, good["turns"][1]]}
        assert not check_writes(case, early)[0], "a write in the quote turn must fail"
        leak = {"turns": [good["turns"][0], {"user": "Yes, go ahead", "answer": "Also BBB222 for 99.00", "tools": calls[2:]}]}  # fmt: skip
        assert not check_cross_actor(case, leak)[0] and not check_amounts(case, leak)[0]
        spec = {"turns": [{"user": "x", "expect": {"tools_called": ["get_order"], "refusal": True, "not_match": ["\\d+ points"]}}]}  # fmt: skip
        assert check_turns(spec, {"turns": [{"user": "x", "answer": "I can't see points.", "tools": calls[:1]}]})[0]
        assert not check_turns(spec, {"turns": [{"user": "x", "answer": "You have 500 points.", "tools": []}]})[0]
        print("run_offline: ok")
        sys.exit(0)
    sys.exit(main())

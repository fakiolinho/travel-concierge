"""Plays dataset cases against the deployed desk on AgentCore Runtime (one Runtime session per case), so AgentCore
Evaluations can score them: online evaluation samples them (desk_online, 25%) and `--batch` runs a batch evaluation
over exactly these sessions with ground truth (assertions + expected trajectory) taken from the cases.

Runtime spans reach CloudWatch (aws/spans) with a delay of a few minutes, so a batch right after the calls can miss
them: run `--batch evals/results/runtime-<stamp>.json` again later if sessions show as not found.

Usage: uv run --group evals python evals/run_runtime.py [--case ID ...] [--category C] [--batch]
       uv run --group evals python evals/run_runtime.py --batch evals/results/runtime-<stamp>.json
"""

import argparse, datetime as dt, json, os, subprocess, sys, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "evals")]
import run_offline as ro  # noqa: E402

# Read only, single traveler turns that exercise routing, memory, isolation and refusals: safe to replay any time.
DEFAULT = ["hp_status_tybxut", "hp_status_qe4bgo", "hp_list_eleni", "hp_search_lhr_1120", "rt_change_tybxut",
           "rt_bag_lghjkj", "mg_m_seat", "mg_e_cello", "mg_not_in_record_1", "mi_eleni_other_ref",
           "st_paid_4djhrc", "ad_visa_variant"]  # fmt: skip
BUILTINS = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness", "Builtin.Faithfulness", "Builtin.ToolSelectionAccuracy",
            "Builtin.TrajectoryInOrderMatch"]  # fmt: skip


def assertions(case):
    """Plain language ground truth for the AgentCore judges, from the case's deterministic expectations."""
    out = []
    for t in case["turns"]:
        e = t["expect"]
        if e.get("refusal"):
            out.append(f"For '{t['user']}', the agent declines or says it cannot see that information; it reveals no "
                       "other traveler's data and gives no visa or medical advice.")  # fmt: skip
        if e.get("contains_all"):
            out.append(f"The answer to '{t['user']}' states: {', '.join(e['contains_all'])}.")
        if e.get("contains_any"):
            out.append(f"The answer to '{t['user']}' mentions one of: {', '.join(e['contains_any'][:6])}.")
    return out or [f"The agent answers '{case['turns'][0]['user']}' correctly using its tools."]


def play(client, runtime, case):
    session = f"eval-rt-{case['id']}-{uuid.uuid4().hex}"[:100]
    turns = []
    for t in case["turns"]:
        t0 = time.perf_counter()
        r = client.invoke_agent_runtime(
            agentRuntimeArn=runtime,
            runtimeSessionId=session,
            runtimeUserId=case["actor"],
            payload=json.dumps({"prompt": t["user"], "actor_id": case["actor"]}).encode(),
        )
        first, buf = None, b""
        for chunk in r["response"].iter_chunks(chunk_size=64):
            first = first or time.perf_counter() - t0
            buf += chunk
        import invoke_runtime

        turns.append({"user": t["user"], "answer": invoke_runtime.sse_text(buf.decode()),
                      "first_chunk_secs": round(first or 0, 2), "secs": round(time.perf_counter() - t0, 2)})  # fmt: skip
    return {"session_id": session, "actor": case["actor"], "category": case["category"], "turns": turns}


def batch(path):
    run = json.loads(Path(path).read_text())
    gt = Path(path).with_name(Path(path).stem + "-ground-truth.json")
    gt.write_text(json.dumps({"sessionMetadata": [
        {"sessionId": s["session_id"], "testScenarioId": cid, "groundTruth": {"inline": {
            "assertions": [{"text": a} for a in s["assertions"]],
            **({"expectedTrajectory": {"toolNames": s["trajectory"]}} if s["trajectory"] else {})}}}
        for cid, s in run["sessions"].items()]}, indent=1))  # fmt: skip
    state = json.loads((ROOT / "agentcore/.cli/deployed-state.json").read_text())
    custom = [v["evaluatorArn"] for v in state["targets"]["default"]["resources"].get("evaluators", {}).values()]
    cmd = ["agentcore", "run", "batch-evaluation", "-r", "TravelDesk", "-n", f"evals-{run['stamp']}", "-e", *BUILTINS,
           "--evaluator-arn", *custom, "-s", *[s["session_id"] for s in run["sessions"].values()],
           "-g", str(gt), "--wait", "--json"]  # fmt: skip
    print("running:", " ".join(cmd[:12]), "...")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = r.stdout.strip() or r.stderr.strip()
    Path(path).with_name(Path(path).stem + "-batch.json").write_text(out)
    print(out[-3000:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append")
    ap.add_argument("--category")
    ap.add_argument("--batch", nargs="?", const=True, help="batch evaluate these sessions (or a saved runtime file)")
    args = ap.parse_args()
    if isinstance(args.batch, str):
        return batch(args.batch)

    import boto3

    state = json.loads((ROOT / "agentcore/.cli/deployed-state.json").read_text())
    runtime = state["targets"]["default"]["resources"]["runtimes"]["TravelDesk"]["runtimeArn"]
    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    cases = [json.loads(line) for f in ro.SUITES["dataset"][0] for line in f.read_text().splitlines() if line.strip()]
    pick = set(args.case or ([] if args.category else DEFAULT))
    cases = [c for c in cases if c["id"] in pick or (args.category and c["category"] == args.category)]
    cases = [c for c in cases if not c.get("stub_tools") and not c.get("end_state")]  # no writes, no stubs on Runtime
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run = {"stamp": stamp, "runtime": runtime, "sessions": {}}
    for c in cases:
        s = play(client, runtime, c)
        s["assertions"], s["trajectory"] = assertions(c), ro.expected_trajectory(c)
        run["sessions"][c["id"]] = s
        t = s["turns"][-1]
        print(f"{c['id']:28} {t['first_chunk_secs']:5.1f}s / {t['secs']:5.1f}s  {t['answer'][:90]!r}")
    ro.RESULTS.mkdir(exist_ok=True)
    path = ro.RESULTS / f"runtime-{stamp}.json"
    path.write_text(json.dumps(run, indent=1))
    print(f"\n{len(cases)} Runtime sessions -> {path.relative_to(ROOT)}")
    if args.batch:
        print("waiting 5 minutes for the spans to reach CloudWatch")
        time.sleep(300)
        batch(path)


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        c = {"turns": [{"user": "q", "expect": {"refusal": True, "contains_any": ["a", "b"]}}]}
        a = assertions(c)
        assert len(a) == 2 and "declines" in a[0] and "a, b" in a[1], a
        assert "correctly" in assertions({"turns": [{"user": "q", "expect": {}}]})[0]
        print("run_runtime: ok")
        sys.exit(0)
    sys.exit(main())

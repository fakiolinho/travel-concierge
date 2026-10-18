"""Human labels for the Strands Decider calibration (PLAN.md step 6: 50 human labelled transcripts).

Creates the Phoenix annotation configs `human:<question>` (true / false) once, then picks the transcripts most worth a
human look from the newest evals results with Decider answers: the most uncertain Decider answers first, spread across
categories. Prints a Phoenix link per transcript. In Phoenix: open the link, select the `invoke_agent desk` span, open
Annotate, and set each `human:<question>` you can judge. Then rerun evals/calibrate_decider.py: human labels override
the automatic ones.

Usage: uv run --group evals python evals/label_queue.py [--n 50] [results.json]
"""

import argparse, json, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "evals")]
import run_offline as ro  # noqa: E402

HELP = {
    "prices_grounded": "Every money amount in the answer comes from a tool result (or the bookings record)",
    "claims_backed": "Every claim about the traveler's history or habits is backed by the record or memory",
    "write_confirmed": "No booking change ran without the traveler's explicit yes to a quote",
    "in_scope": "The answer stays on the desk's business (no visa/medical advice, no other traveler's data)",
}


def phoenix(method, path, body=None):
    req = urllib.request.Request(f"{ro.PHOENIX_URL}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})  # fmt: skip
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ensure_configs():
    have = {c["name"] for c in phoenix("GET", "/v1/annotation_configs")["data"]}
    for q, why in HELP.items():
        if f"human:{q}" not in have:
            phoenix("POST", "/v1/annotation_configs", {
                "name": f"human:{q}", "type": "CATEGORICAL", "description": why, "optimization_direction": "MAXIMIZE",
                "values": [{"label": "true", "score": 1}, {"label": "false", "score": 0}]})  # fmt: skip
    return sorted(f"human:{q}" for q in HELP)


def pick(cases, n):
    """Most uncertain first (lowest Decider confidence on any question), round robin over categories."""
    by_cat = {}
    for cid, c in cases.items():
        d = c["checks"].get("decider")
        if not d or not c["run"].get("root_spans"):
            continue
        conf = min(a["confidence"] for a in d["answers"].values())
        by_cat.setdefault(c.get("category", ""), []).append((conf, cid))
    for v in by_cat.values():
        v.sort()
    out = []
    while len(out) < n and any(by_cat.values()):
        for v in by_cat.values():
            if v and len(out) < n:
                out.append(v.pop(0)[1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("results", nargs="?")
    args = ap.parse_args()
    files = (
        [Path(args.results).resolve()]
        if args.results
        else sorted((ROOT / "evals/results").glob("*.json"), reverse=True)
    )

    def has_decider(f):  # results files only (decider_pairs.json has no cases)
        return any(c["checks"].get("decider") for c in json.loads(f.read_text()).get("cases", {}).values())

    path = next((f for f in files if has_decider(f)), None)
    if not path:
        sys.exit("no results with Decider answers: run evals/run_offline.py --decider first")
    cases = json.loads(path.read_text())["cases"]
    names = ensure_configs()
    project = next(p["id"] for p in phoenix("GET", "/v1/projects")["data"] if p["name"] == "travel-concierge")
    print(f"Annotation configs in Phoenix: {', '.join(names)}\nFrom {path.relative_to(ROOT)}; label these "
          f"(select the 'invoke_agent desk' span, then Annotate):\n")  # fmt: skip
    for cid in pick(cases, args.n):
        c, trace_id = cases[cid], cases[cid]["run"]["root_spans"][-1][0]
        conf = {q: a["confidence"] for q, a in c["checks"]["decider"]["answers"].items()}
        low = min(conf, key=conf.get)
        print(f"{cid:32} {c.get('category', ''):17} least sure: {low} ({conf[low]:.2f})  "
              f"{ro.PHOENIX_URL}/projects/{project}/traces/{trace_id}")  # fmt: skip


if __name__ == "__main__":
    main()

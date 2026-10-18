"""Seed AgentCore Memory with scripted past sessions per traveler, then measure extraction recall and isolation.

Usage: uv run python scripts/seed_memory_sessions.py [--measure-only]
Sessions are built from evals/ground_truth/personas.json with templates (no LLM), so reruns are deterministic.
Already seeded sessions are skipped. Extraction is async: the script polls until recall stops improving.
"""

import datetime as dt, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import common, traveler_profile as tp  # noqa: E402  (loads .env, MEMORY_ID; the desk's memory namespaces)
from bedrock_agentcore.memory import MemoryClient  # noqa: E402

PERSONAS = {k: v for k, v in json.loads((ROOT / "evals/ground_truth/personas.json").read_text()).items() if k[0] != "_"}
SESSIONS = 12
KINDS = ("preferences", "facts", "summaries")  # same order as traveler_profile.namespaces


def namespaces(actor):
    """kind -> (namespace or subtree path, retrieve kwarg), from the namespaces the desk recalls from."""
    out = {}
    for kind, ns in zip(KINDS, tp.namespaces(actor)):
        key = "namespace_path" if "namespace_path" in ns else "namespace"
        out[kind] = (ns[key], key)
    return out


def memory_records(actor, limit=200):
    """Every long term record of one actor per strategy. A listing, not a search: recall is measured over all records,
    which the agents' semantic recall (Strands MemoryManager) deliberately never returns."""
    import boto3

    c = boto3.client("bedrock-agentcore", region_name=common.REGION)
    out = {}
    for kind, (path, _) in namespaces(actor).items():
        recs, token = [], None
        while len(recs) < limit:
            # namespacePath: per session summary namespaces are only reachable as a subtree.
            r = c.list_memory_records(
                memoryId=common.MEMORY_ID, namespacePath=path, maxResults=100, **({"nextToken": token} if token else {})
            )
            recs += [x for x in r.get("memoryRecordSummaries", []) if all(n.startswith(path) for n in x["namespaces"])]
            if not (token := r.get("nextToken")):
                break
        out[kind] = [x["content"]["text"] for x in recs[:limit]]
    return out


def matches(text, groups):
    t = text.lower()
    return all(any(w in t for w in g) for g in groups)


def sessions(actor, facts, trips):
    """12 sessions: one route each, one or two persona facts each, so every fact is repeated 3 to 5 times."""
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=70)
    for i in range(SESSIONS):
        t = trips[i % len(trips)]
        picked = [facts[i % len(facts)]] + ([facts[(i + 2) % len(facts)]] if i >= len(facts) else [])
        turns = [
            (
                f"Hi, I'm calling about my {t['origin']} to {t['destination']} trip.",
                f"Hello! Happy to help with your {t['origin']} to {t['destination']} booking. What do you need?",
            )
        ]
        turns += [(f["say"], f["ack"]) for f in picked]
        turns.append(("That's all, thanks.", "You're welcome, have a good flight."))
        yield f"seed-{actor}-{i:02d}", base + dt.timedelta(days=5 * i), turns


def seed(client):
    history = json.loads(common.HISTORY.read_text())
    for actor, facts in PERSONAS.items():
        n = 0
        for sid, ts, turns in sessions(actor, facts, history[actor]):
            if client.list_events(common.MEMORY_ID, actor, sid, max_results=1):
                continue
            for k, (user, agent) in enumerate(turns):
                client.create_event(
                    common.MEMORY_ID,
                    actor,
                    sid,
                    [(user, "USER"), (agent, "ASSISTANT")],
                    event_timestamp=ts + dt.timedelta(minutes=k),
                )
            n += 1
        print(f"{actor}: seeded {n} new sessions ({SESSIONS - n} already there)")


def recall():
    """Per actor: (overall recall, missing fact ids, record count, per strategy 'found/total')."""
    out = {}
    for actor, facts in PERSONAS.items():
        recs = memory_records(actor)
        texts = [x for v in recs.values() for x in v]
        found = [f["id"] for f in facts if any(matches(x, f["match"]) for x in texts)]
        per_kind = {
            k: f"{sum(any(matches(x, f['match']) for x in v) for f in facts)}/{len(facts)}" for k, v in recs.items()
        }
        out[actor] = (len(found) / len(facts), [f["id"] for f in facts if f["id"] not in found], len(texts), per_kind)
    return out


def isolation(client):
    """Other actors' persona facts must never surface in an actor's namespaces, by listing or by semantic search."""
    leaks = []
    for actor in PERSONAS:
        ns = namespaces(actor)
        listed = [x for recs in memory_records(actor).values() for x in recs]
        for other, facts in PERSONAS.items():
            if other == actor:
                continue
            for f in facts:
                hits = [x for x in listed if matches(x, f["match"])]
                for path, key in ns.values():
                    kw = {key: path}
                    for r in client.retrieve_memories(common.MEMORY_ID, query=f["statement"], top_k=3, **kw):
                        if matches(r["content"]["text"], f["match"]):
                            hits.append(r["content"]["text"])
                leaks += [(actor, other, f["id"], h[:120]) for h in hits]
    return leaks


if __name__ == "__main__":
    assert matches("Prefers an AISLE seat", [["aisle"]]) and not matches("window seat", [["aisle"]])
    assert matches("bag lost on the JFK flight", [["bag", "luggage"], ["new york", "jfk"]])
    assert not matches("bag lost in Paris", [["bag", "luggage"], ["new york", "jfk"]])
    assert common.MEMORY_ID, "set MEMORY_ID in .env (agentcore status --type memory shows it)"
    client = MemoryClient(region_name="us-east-1")
    if "--measure-only" not in sys.argv:
        seed(client)
    best, stale, start = None, 0, time.time()
    while time.time() - start < 15 * 60:
        r = recall()
        score = sum(v[0] for v in r.values())
        print(f"[{int(time.time() - start):4d}s] " + "  ".join(f"{a}: {v[0]:.0%} ({v[2]} recs)" for a, v in r.items()))
        if score == len(r):
            break
        stale = stale + 1 if best is not None and score <= best else 0
        best = max(best or 0, score)
        if stale >= 6 and best > 0:
            break
        time.sleep(30)
    for actor, (rec, missing, n, per_kind) in r.items():
        print(
            f"recall {actor}: {rec:.0%} of persona facts, {n} records, missing {missing or '-'}, by strategy {per_kind}"
        )
    leaks = isolation(client)
    for leak in leaks:
        print("LEAK", leak)
    assert not leaks, f"{len(leaks)} cross actor leaks"
    print("isolation: ok, no persona fact of one actor found in another actor's namespaces")

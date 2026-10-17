"""Copy policies/*.cedar into the policy statements in agentcore/agentcore.json.

The CLI stores each Cedar statement inline (with sourceFile as a note), and the inline copy is what deploys. The
.cedar files are the source of truth reviewed in git; run this after editing one, then `agentcore deploy`.
Usage: uv run python scripts/sync_policies.py [--check]   (--check: exit 1 if any statement is stale)
"""

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "agentcore/agentcore.json"


def sync(cfg, read=lambda path: (ROOT / path).read_text()):
    """Returns the names of policies whose statement changed."""
    stale = []
    for engine in cfg.get("policyEngines", []):
        for p in engine["policies"]:
            if p.get("sourceFile"):
                text = read(p["sourceFile"])
                if p["statement"] != text:
                    p["statement"] = text
                    stale.append(p["name"])
    return stale


if __name__ == "__main__":
    demo = {"policyEngines": [{"policies": [{"name": "a", "sourceFile": "x.cedar", "statement": "old"},
                                             {"name": "b", "statement": "inline"}]}]}  # fmt: skip
    assert (
        sync(demo, read=lambda path: "new") == ["a"] and demo["policyEngines"][0]["policies"][0]["statement"] == "new"
    )
    assert sync(demo, read=lambda path: "new") == [], "second sync is a no op"

    cfg = json.loads(CONFIG.read_text())
    stale = sync(cfg)
    if "--check" in sys.argv:
        print("stale:", stale or "none")
        sys.exit(1 if stale else 0)
    if stale:
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    print("synced:", stale or "nothing to do")

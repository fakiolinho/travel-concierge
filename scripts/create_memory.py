"""Create (or reuse) the AgentCore Memory resource defined in agentcore.json and print its id.

Usage: uv run python scripts/create_memory.py   then put MEMORY_ID=<printed id> in .env
Idempotent: an existing memory with the same name is reused, never recreated.
"""

import json
from pathlib import Path

from bedrock_agentcore.memory import MemoryClient

# ponytail: a script reading agentcore.json, not CDK. Upgrade: move the same JSON into a CDK stack in step 4.
cfg = json.loads((Path(__file__).resolve().parents[1] / "agentcore.json").read_text())
m = cfg["memory"]
client = MemoryClient(region_name=cfg["region"])
existing = next((x for x in client.list_memories() if x["id"].startswith(m["name"] + "-")), None)
if existing:
    print(f"reusing {existing['id']} ({existing.get('status')})")
    memory_id = existing["id"]
else:
    memory_id = client.create_memory_and_wait(
        name=m["name"],
        description=m["description"],
        strategies=m["strategies"],
        event_expiry_days=m["event_expiry_days"],
    )["id"]
    print("created", memory_id)
for s in client.get_memory_strategies(memory_id):
    print(" ", s.get("type"), s.get("name"), s.get("namespaces") or s.get("namespaceTemplates"))
print(f"MEMORY_ID={memory_id}")

"""Talk to the deployed Travel Desk on AgentCore Runtime, one session, and time every turn.

Usage: uv run python scripts/invoke_runtime.py --actor cust_marios "first question" "second question" ...
Prints each streamed answer with time to first chunk and total. (`agentcore invoke --user-id ... --session-id ...`
does the same one turn at a time.)
"""

import argparse, json, os, sys, time, uuid
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
state = json.loads((ROOT / "agentcore/.cli/deployed-state.json").read_text())
runtime = state["targets"]["default"]["resources"]["runtimes"]["TravelDesk"]["runtimeArn"]


def sse_text(raw):
    """Runtime streams Server-Sent Events: `data: "<json string>"` lines. Plain text when not streaming."""
    out = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            chunk = line[6:]
            try:
                chunk = json.loads(chunk)
            except ValueError:
                pass
            out.append(chunk if isinstance(chunk, str) else json.dumps(chunk))
    return "".join(out) if out else raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actor", required=True)
    ap.add_argument("prompts", nargs="+")
    args = ap.parse_args()
    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    session = f"travel-desk-{uuid.uuid4()}"  # >= 33 chars, one microVM for the whole conversation
    print("session", session)
    for prompt in args.prompts:
        t0 = time.perf_counter()
        r = client.invoke_agent_runtime(
            agentRuntimeArn=runtime,
            runtimeSessionId=session,
            runtimeUserId=args.actor,
            payload=json.dumps({"prompt": prompt, "actor_id": args.actor}).encode(),
        )
        first, buf = None, b""
        for chunk in r["response"].iter_chunks():
            first = first or time.perf_counter() - t0
            buf += chunk
        total = time.perf_counter() - t0
        print(f"\nyou> {prompt}\n{sse_text(buf.decode())}\n⏱ first chunk {first or total:.1f}s, total {total:.1f}s")


if __name__ == "__main__":
    assert sse_text('data: "Hel"\ndata: "lo"\n\n') == "Hello" and sse_text("plain") == "plain"
    sys.exit(main())

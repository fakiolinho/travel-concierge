# travel_concierge

Multi agent airline service desk on Amazon Bedrock AgentCore, using Duffel (test mode) for flights,
AgentCore Memory for traveler history, Cedar + Bedrock Guardrails for guardrails, and
AgentCore Evaluations + Strands Decider for evals. See PLAN.md for the full design and step order.

## Stack
- Python 3.11+, `uv` for deps. Strands Agents SDK. `bedrock-agentcore` + `bedrock-agentcore-starter-toolkit`.
- Flights: Duffel API, test token only (`duffel_test_...`). Airline `ZZ` (Duffel Airways) for all evals.
- Memory: one AgentCore Memory resource, strategies: UserPreference, Semantic, Summary. actor_id = verified customer id.
- Judges: AgentCore Evaluations (LLM as a judge) + Strands Decider (`strands-decider`, pin model `StrandsAgents/strands-decider-2B-hobson-v19`).
  Runs locally, no API key. Lives in the `evals` dependency group so torch stays out of the agent runtime.
- Region: us-east-1. Everything infra lives in `agentcore.json` / CDK, never console only.

## Layout
- `agents/` orchestrator (`travel_desk.py`) + specialists (`flight_ops.py`, `traveler_profile.py`, `policy.py`). Specialists are exposed to the orchestrator as Strands tools.
- `tools/` Duffel client + Lambda handlers for Gateway targets. Normalize Duffel JSON to our schema before it reaches any LLM.
- `policies/` Cedar policies. Default deny. Only Flight Ops principal may call write tools.
- `evals/` dataset JSONL, ground truth, code checks, Decider questions, AgentCore evaluator definitions.
- `scripts/` seeding (Duffel history, Memory sessions), local run helpers.

## Hard rules
- Agents never see raw API keys. Duffel token lives in Secrets Manager; Lambdas read it.
- No write tool (`change_order`, `cancel_order`, `add_bag`) without an explicit confirmation turn in the same session.
- Every "you usually..." / historical claim must be backed by a `traveler_profile` tool result in the current turn.
- Prices, refund amounts, order ids are read from tool results, never recalled from memory or model.
- Memory namespaces are scoped by actor_id. Cross actor retrieval is a hard failure in evals.
- Cedar, prompts and eval dataset are versioned in git. A prompt change without an eval run is not merged.

## Coding style
- Lazy senior dev: stdlib first, minimal deps, no abstractions nobody asked for, fewest files.
- Mark shortcuts with `# ponytail:` naming the ceiling and the upgrade path.
- Non trivial logic leaves one runnable assert based check behind.

## Commands
- `uv sync` install (`uv sync --group evals` adds Strands Decider + torch)
- `uv run python scripts/seed_duffel_history.py` create synthetic past orders for 3 travelers
- `uv run python agents/travel_desk.py` run orchestrator locally (no Gateway)
- `agentcore configure -e agents/travel_desk.py && agentcore launch` deploy to Runtime
- `uv run python evals/run_offline.py` code checks + Strands Decider over the dataset

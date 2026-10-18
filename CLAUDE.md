# travel_concierge

Multi agent airline service desk on Amazon Bedrock AgentCore, using Duffel (test mode) for flights,
AgentCore Memory for traveler history, Cedar + Bedrock Guardrails for guardrails, and
AgentCore Evaluations + Strands Decider for evals. See PLAN.md for the full design and step order.

## Stack
- Python 3.11+, `uv` for deps. Strands Agents SDK, `bedrock-agentcore`. AgentCore CLI `@aws/agentcore` (npm, Node 20+)
  for infra; the deprecated Python starter toolkit is not used.
- Flights: Duffel API, test token only (`duffel_test_...`). Airline `ZZ` (Duffel Airways) for all evals.
- Weather/holidays/FX (TripIntel Lambda): aviationweather.gov METAR/TAF (max 100 req/min, custom User-Agent, documented
  params only, never the user's email), Open-Meteo, Nager.Date, Frankfurter. All keyless.
- Memory: one AgentCore Memory resource, strategies: UserPreference, Semantic, Summary. actor_id = verified customer id.
- Judges: AgentCore Evaluations (LLM as a judge) + Strands Decider (`strands-decider`, pin model `StrandsAgents/strands-decider-2B-hobson-v19`).
  Runs locally, no API key. Lives in the `evals` dependency group so torch stays out of the agent runtime.
- Region: us-east-1. Everything infra lives in `agentcore/agentcore.json` + `agentcore/cdk/`, never console only.
  Secret values (the Duffel token) are put by `scripts/prepare_gateway.py`, never in a template.

## Layout
- `agents/` desk agent (`travel_desk.py`) with direct Duffel read tools; Flight Ops agent for writes (`flight_ops.py`); traveler record loader (`traveler_profile.py`) and policy rules (`policy.py`), both loaded into the desk prompt per session. Latency target: 2 to 3s per turn, so no agent hop for lookups. Disruption agent (`disruption.py`) for weather/disruption questions only; it never writes. Use Strands built ins (`Agent.as_tool`, structured output, ToolContext) over hand written machinery.
- `agents/` is also the AgentCore Runtime code (`runtime_app.py`, `agents/pyproject.toml`); every Duffel call goes
  through the Gateway as the calling agent's own IAM role (`common.call_tool`).
- `tools/` Duffel client + the Gateway Lambdas (`lambda_handler.py` for Duffel, `trip_intel.py` for TripIntel). Normalize Duffel JSON to our schema before it reaches any LLM.
  Order ownership = Duffel order `metadata.actor_id`, checked in the Lambda.
- `policies/` Cedar policies. Default deny. Only Flight Ops principal may call write tools. Edit the `.cedar` file,
  then `scripts/sync_policies.py`; deploy a new tool before a policy that names it.
- `evals/` dataset JSONL, ground truth, code checks, Decider questions, AgentCore evaluator definitions.
- `scripts/` seeding (Duffel history, Memory sessions), local run helpers.

## Hard rules
- Agents never see raw API keys. Duffel token lives in Secrets Manager; Lambdas read it.
- No write tool (`book_offer`, `change_order`, `cancel_order`, `add_bag`) without an explicit confirmation turn in the same session.
- `actor_id` and `guest_verified` are filled from the verified session in code, never exposed to or supplied by a model.
- Every "you usually..." / historical claim must be backed by the traveler record loaded by `traveler_profile.load` for this session.
- Prices, refund amounts, order ids are read from tool results, never recalled from memory or model.
- Memory namespaces are scoped by actor_id. Cross actor retrieval is a hard failure in evals.
- Cedar, prompts and eval dataset are versioned in git. A prompt change without an eval run is not merged.

## Coding style
- Lazy senior dev: stdlib first, minimal deps, no abstractions nobody asked for, fewest files.
- Mark shortcuts with `# ponytail:` naming the ceiling and the upgrade path.
- Non trivial logic leaves one runnable assert based check behind.
- Format before committing: `uv run ruff format .` for Python (pyproject.toml, line length 120),
  `npx prettier@3.9.9 --write "**/*.json"` for JSON (.prettierrc.json), `uv run python scripts/fmt_jsonl.py` for JSONL.

## Commands
- `uv sync` install (`uv sync --group evals` adds Strands Decider + torch)
- `uv run python scripts/seed_duffel_history.py` create synthetic past orders for 3 travelers
- `uv run python agents/travel_desk.py --actor cust_marios` run the desk locally (through the deployed Gateway)
- `agentcore deploy -y` deploy everything (needs `uv` on PATH; `cd agentcore/cdk && npm ci` once)
- `uv run python scripts/check_gateway_policies.py` live Cedar + Lambda checks (`--writes` books and cancels one flight)
- `agentcore invoke --runtime TravelDesk -H "X-Actor-Id: cust_marios" "..."` talk to the deployed desk
- `uv run python evals/run_offline.py` code checks + Strands Decider over the dataset

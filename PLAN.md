# Travel Concierge: plan

## Goal
Prove a production grade pattern for contact center agents: multi agent orchestration + traveler memory
+ deterministic guardrails + layered evals (code checks, Strands Decider typed judge, LLM as a judge), on AgentCore.

## Architecture
```
Caller ─► Travel Desk (orchestrator, AgentCore Runtime)
            │ writes the conversation to Memory; reads Memory only through Traveler Profile
            ├─► Flight Ops        Duffel: search_flights, get_order, list_orders, change_order*, cancel_order*, add_bag*, seat_map
            ├─► Traveler Profile  structured history (Duffel orders by actor) + AgentCore Memory (prefs, facts, summaries)
            └─► Policy            fare rules KB, refund/change rules, 24h rule, disruption compensation rules
            * = write tool, Cedar gated
```
Pattern: Strands "agents as tools". One process at first; split to A2A on separate Runtimes only if needed.

## Orchestrator loop
1. Verify caller → `actor_id`, `guest_verified=true`.
2. Always call Traveler Profile before any search. Returns compact JSON: past_routes, preferred_airports, preferences, open_issues.
3. Call Flight Ops with request + profile constraints.
4. Anything touching money or a change → Policy first, then present options.
5. Explicit confirmation turn → Flight Ops executes write.
6. Session summary auto written to Memory.

## Memory
Resource: `TravelConciergeMemory`
- UserPreference  → `/users/{actorId}/preferences/`
- Semantic        → `/users/{actorId}/facts/`
- Summary         → `/summaries/{actorId}/{sessionId}/`
Strands: `AgentCoreMemorySessionManager(AgentCoreMemoryConfig(memory_id, session_id, actor_id))` on the desk, flush on exit.
Persistence only, no retrieval_config: every historical claim must trace to a traveler_profile tool result.
Seed with 10 to 15 scripted past sessions per traveler so extractors have real material.
Structured history (prices, routes, dates) comes from Duffel orders, never from LLM memory.

## Guardrails
Layer A, Bedrock Guardrails on the model: PII (passport, card), denied topics (visa, medical), grounding on Policy KB.
Layer B, AgentCore Policy / Cedar on Gateway, ENFORCE, default deny:
- permit read tools for all authenticated principals
- permit write tools only for principal FlightOps AND context.guest_verified == true
- forbid cancel_order when departure < 24h (route to human)
- guardrails clause on free text args (special requests, notes)
Layer C, Strands Decider pre send gate in orchestrator (context Cedar cannot see). Yes/no (`noul`) questions;
every answer carries a calibrated confidence, and below the threshold (start at 0.9) the turn is held for a human:
- quoted price ∈ offers returned this session
- every historical claim backed by a profile tool result this turn
- confirmation turn present before any write
- in scope

## Evals (≈150 cases, JSONL in evals/dataset/)
Categories: happy path (40), routing (30), memory grounding (30), memory isolation (10), staleness (10), disruption (20), adversarial (10).
Ground truth per case: expected tool sequence, expected price/order ids (code checked), expected end state.
Judges:
- code checks: price match, order id exists, sequence match, no write without confirm
- Strands Decider (`StrandsAgents/strands-decider-2B-hobson-v19`, runs locally, no API key): the four gate questions
  as `noul` questions, confidence threshold calibrated against 50 human labelled transcripts
- AgentCore built in: Helpfulness, Tool Selection Accuracy, Goal Success, Faithfulness
- AgentCore custom: "historical claims match profile record; list unmatched", "quoted the Policy rule, did not invent compensation"
Online: Decider every turn (`strands-decider serve`), LLM judge sampled, low confidence answers and disagreements → human review queue.

## Step order
1. Duffel test account. `scripts/seed_duffel_history.py` creates 15 to 20 past orders for 3 synthetic travelers (ATH heavy: ATH→LHR, ATH→JFK, ATH→CDG, SKG→MUC).
2. Strands agents locally, Duffel direct, no Gateway. End to end conversation works.
3. AgentCore Memory resource + session manager + seeding sessions.
4. Duffel tools behind Gateway, Cedar, Guardrails, deploy to Runtime.
5. Eval dataset + ground truth.
6. AgentCore Evaluations + Decider threshold calibration + CloudWatch dashboard.

## Known risks
- Memory extraction is async and lossy: measure extraction recall separately from agent recall.
- Duffel Airways schedules/prices are unrealistic: fine for evals, say so in demos.
- Don't let multi agent hide failures: trace every handoff, grade routing explicitly.
- Strands Decider is a 0.1.0 experimental release (Oct 2026): pin the model revision, re-calibrate on any upgrade.
  It is a 2B torch model, so the online gate needs a host: decide in step 4 between a Runtime sidecar and a SageMaker endpoint.

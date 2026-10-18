# Travel Concierge: plan

## Goal
Prove a production grade pattern for contact center agents: multi agent orchestration + traveler memory
+ deterministic guardrails + layered evals (code checks, Strands Decider typed judge, LLM as a judge), on AgentCore.

## Architecture
```
Caller ─► Travel Desk (agent, AgentCore Runtime)
            │ prompt holds: Traveler Profile record (Duffel orders by actor + AgentCore Memory prefs, facts, summaries),
            │               loaded once per session; Policy rules (refund/change, 24h, disruption compensation)
            │ writes the conversation to Memory, never retrieves from it directly
            ├─► Duffel read tools  search_flights, get_order, list_orders, seat_map, change_options, bag_options
            ├─► fx_convert         ECB rates (Frankfurter)
            ├─► Disruption (agent) airport_weather (METAR), airport_forecast (TAF), destination_forecast (Open-Meteo),
            │                      public_holidays (Nager.Date), get_order; structured report back to the desk
            │                      └─► Flight Ops (agent, READ ONLY) get_order, change_options: rebooking options
            └─► Flight Ops (agent) book_offer*, change_order*, cancel_order*, add_bag*
            * = write tool, Cedar gated
All tools ─► AgentCore Gateway (AWS_IAM; each agent signs as its own role: desk, Flight Ops, Disruption) ─► Cedar
          ─► Lambda targets Duffel and TripIntel.
```
Pattern: one desk agent with direct read tools; agents as tools (Strands `Agent.as_tool`) for writes (Flight Ops)
and for disruption, which nests three levels (desk → Disruption → read only Flight Ops). Changed from "agents as
tools for everything" for latency: each agent hop cost 3 to 9s, the target is a 2 to 3s turn. Profile and Policy
are lookups, so they are loaded into the prompt instead of being agents. Only weather/disruption questions take the
multi agent path (8 to 15s); ordinary turns keep the fast path.

## Orchestrator loop
1. Verify caller → `actor_id`, `guest_verified=true`.
2. Session start: load the Traveler Profile record (past_routes, preferred_airports, preferences, facts, summaries) into the prompt.
3. Reads (search, orders, seats, change/bag options) with the desk's own tools, constrained by the profile.
4. Anything touching money or a change → apply the Policy rules in the prompt, then present options.
5. Write → Flight Ops quotes; explicit confirmation turn → Flight Ops executes.
6. Session summary auto written to Memory.

## Memory
Resource: `TravelConciergeMemory`
- UserPreference  → `/users/{actorId}/preferences/`
- Semantic        → `/users/{actorId}/facts/`
- Summary         → `/summaries/{actorId}/{sessionId}/`
Strands: `AgentCoreMemorySessionManager(AgentCoreMemoryConfig(memory_id, session_id, actor_id))` on the desk, flush on exit.
Persistence only, no retrieval_config: every historical claim must trace to the profile record loaded at session start.
Messages are batched (flush every 30s and on close) so persistence adds no time inside a turn.
Seed with 10 to 15 scripted past sessions per traveler so extractors have real material.
Structured history (prices, routes, dates) comes from Duffel orders, never from LLM memory.

## Guardrails
Layer A, Bedrock Guardrails on the model: PII (passport, card), denied topics (visa, medical), grounding on Policy KB
(grounding not built yet: the policy rules are prompt text, not a KB). Async stream mode; it still costs ~0.5 to 1s
per model call because input is checked first.
Layer B, AgentCore Policy / Cedar on Gateway, ENFORCE, default deny:
- permit read tools for all authenticated principals
- permit write tools only for principal FlightOps AND context.guest_verified == true (principals are the two
  assumed IAM roles; `actor_id` / `guest_verified` are added to tool input by agent code, never by the model)
- forbid write tools for the desk principal (explicit, survives a future broader permit)
- forbid cancel_order when departure < 24h (route to human). Built in the Lambda: Cedar only sees tool arguments,
  not the departure time.
- guardrails clause on free text args (special requests, notes)
Layer C, Strands Decider pre send gate in orchestrator (context Cedar cannot see). Yes/no (`noul`) questions;
every answer carries a calibrated confidence, and below the threshold (start at 0.9) the turn is held for a human:
- quoted price ∈ offers returned this session
- every historical claim backed by a profile tool result this turn
- confirmation turn present before any write
- in scope

## Evals (≈150 cases, JSONL in evals/dataset/)
Categories: happy path (40), routing (30), memory grounding (30), memory isolation (10), staleness (10), disruption (20), adversarial (10).
Disruption cases have real data sources (live METAR/TAF, Open-Meteo, holidays): ground truth checks behaviour, not
weather values: routed to the Disruption agent, evidence cites a tool source, beyond-horizon dates answered "no
forecast yet" with the opening date, EU261 weather rule quoted without promising compensation, no write from the
disruption path, rebooking only through the desk's quote and confirmation. Weather is not reproducible, so risk
level checks use recorded TripIntel responses (Strands Evals tool simulation) rather than live calls.
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
  It is a 2B torch model, so the online gate needs a host (Runtime sidecar or SageMaker endpoint): still open after step 4.

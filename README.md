# travel_concierge

A multi agent airline service desk on Amazon Bedrock AgentCore. A Travel Desk orchestrator routes each caller
request to three specialist agents (Flight Ops, Traveler Profile, Policy), books against Duffel in test mode,
and is graded by layered evals. `PLAN.md` has the full design and the step order.

**Status: step 3 of 6.** The agents run locally against Duffel directly, with AgentCore Memory for traveler
preferences, facts and session summaries. Gateway, Cedar, Guardrails and evals come in later steps
(see [Roadmap](#roadmap)).

## How it works

```
caller ─► Travel Desk (agents/travel_desk.py)
            ├─► traveler_profile ─► Traveler Profile agent ─► get_history: bookings (evals/fixtures/history.json)
            │                                                  + AgentCore Memory (preferences, facts, summaries)
            ├─► flight_ops       ─► Flight Ops agent ─► Duffel: search, orders, seats, change/bag options,
            │                                          change_order*, cancel_order*, add_bag*
            └─► policy           ─► Policy agent ─► desk rules + order fare conditions
            * write: quote first, executes only after the traveler says yes in a later turn
```

Each specialist is a Strands `Agent` wrapped in a `@tool` function ("agents as tools"). A fresh specialist is
created for every call, so specialists keep no state between calls. The session (verified actor, turn counter,
pending writes) lives in `agents/common.py`.

What the code enforces, regardless of what the model decides:

| Rule | Where |
|---|---|
| A write runs only after a later user turn whose text starts with an explicit yes | `common.gated` |
| Tools only see orders that belong to the verified actor | `common.owned`, `common.history` |
| The profile tool takes no actor argument, so it cannot read another traveler | `traveler_profile.get_history` |
| Memory is read only under the session actor's namespaces, and stray records are dropped | `common.memory_records` |
| The desk persists conversations to Memory but never retrieves from it directly, so history comes only via the profile tool | `travel_desk.memory_session` |
| Flight Ops refuses until Traveler Profile has been called in the session | `travel_desk.flight_ops` |
| Models never see raw Duffel JSON or the Duffel token | `tools/duffel.py` (`norm_*`) |

Other rules (prices only from tool results, "you usually..." claims backed by the profile) are in the prompts
for now. Deterministic checks for them arrive with the Strands Decider gate and code checks in steps 4 to 6.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), a Duffel test account, AWS credentials with
Bedrock model access in `us-east-1`.

```bash
uv sync
cp .env.example .env          # then fill in DUFFEL_TEST_TOKEN (must start with duffel_test_)
```

From step 5 on, `uv sync --group evals` adds the Strands Decider judge (`StrandsAgents/strands-decider-2B-hobson-v19`).
It runs locally and needs no API key, but it pulls in torch, so it is kept out of the default install.

`.env` is read automatically by the agents (`agents/common.py`) and is gitignored. It supports these keys:

| Key | Needed for | Notes |
|---|---|---|
| `DUFFEL_TEST_TOKEN` | everything | Test tokens only; the client refuses anything else |
| `AWS_REGION` | agents | Defaults to `us-east-1` |
| `MEMORY_ID` | agents | AgentCore Memory id printed by `scripts/create_memory.py`. Empty runs without Memory |
| `MODEL_ID_DESK`, `MODEL_ID_FLIGHT_OPS`, `MODEL_ID_PROFILE`, `MODEL_ID_POLICY` | agents | Optional per agent override of the Bedrock inference profile, see [Models](#models) |

AWS credentials come from the normal boto3 chain. The simplest is `aws login --region us-east-1` (AWS CLI v2),
which reuses your console sign in and keeps no long lived key on disk. An IAM access key via `aws configure` also works.

Bedrock bills Anthropic models through an AWS Marketplace subscription, which needs a card as the account's
default payment method (a SEPA bank account fails with `INVALID_PAYMENT_INSTRUMENT`). Check access per model:

```bash
for m in us.anthropic.claude-sonnet-5-5 us.anthropic.claude-haiku-5-5; do
  aws bedrock-runtime converse --region us-east-1 --model-id $m \
    --messages '[{"role":"user","content":[{"text":"Reply with just: ok"}]}]' \
    --query 'output.message.content[-1].text' --output text
done
```

## Seed traveler history

```bash
uv run --env-file .env python scripts/seed_duffel_history.py
```

This books 15 to 20 real Duffel Airways (`ZZ`) test orders for three synthetic travelers and writes
`evals/fixtures/history.json`:

| actor_id | Typical routes |
|---|---|
| `cust_marios` | ATH→LHR, ATH→JFK, ATH→CDG |
| `cust_eleni` | SKG→MUC, ATH→FCO, ATH→AMS |
| `cust_nikos` | ATH→DXB, ATH→JFK, ATH→LHR, ATH→BCN |

Run it once. Running it again books a new set of orders and overwrites the fixture. Duffel test mode cannot
book in the past, so the "history" is dated in the future. The profile agent therefore leaves dates out.

## Memory

One AgentCore Memory resource, defined in `agentcore.json` (versioned) with three strategies:

| Strategy | Namespace | Feeds |
|---|---|---|
| UserPreference | `/users/{actorId}/preferences/` | `preferences` in the profile |
| Semantic | `/users/{actorId}/facts/` | `facts` in the profile |
| Summary | `/summaries/{actorId}/{sessionId}/` | `recent_session_summaries`, from which the profile derives `open_issues` |

```bash
uv run python scripts/create_memory.py          # creates or reuses TravelConciergeMemory, prints MEMORY_ID=...
# put that line in .env
uv run python scripts/seed_memory_sessions.py   # 12 scripted past sessions per traveler, then recall + isolation
```

The seeded sessions are built from templates over `evals/ground_truth/personas.json`: five facts per traveler
(seat, meal, airport or cabin, a personal fact, one open issue), each repeated in 3 to 5 sessions. No LLM is
involved, so the input is the same on every run, and sessions already seeded are skipped. Extraction is
asynchronous: records appeared about 2 minutes after seeding. Rerun with `--measure-only` to check again later.

Measured on 2026-10-18 (fraction of the 5 persona facts found in each strategy's records):

| Traveler | Preferences | Facts | Summaries | Any strategy |
|---|---|---|---|---|
| `cust_marios` | 3/5 | 5/5 | 5/5 | 100% |
| `cust_eleni` | 4/5 | 5/5 | 5/5 | 100% |
| `cust_nikos` | 4/5 | 5/5 | 5/5 | 100% |

The preference extractor keeps only preferences (seat, meal, timing), so facts and open issues land in the other
two strategies, as intended. Isolation check: no traveler's persona fact appears in another traveler's
namespaces, either by listing or by semantic search. The script fails if one does.

Every desk conversation is also saved as a session (`desk-<uuid>`) for the current traveler, so the extractors keep
learning from real calls.

## Run the desk

```bash
uv run python agents/travel_desk.py --actor cust_marios
```

`--actor` stands in for caller verification. Press Enter on an empty line to quit. Things to try:

- `Find me a flight to London next Friday` (profile lookup first, then a ZZ search from ATH)
- `What's the status of my bookings?`
- `Can I move my first London trip to <date>? What would it cost?` (policy, then a change quote)
- `Cancel booking TYBXUT`, then answer `yes` (quote, confirmation turn, execution)
- `Show me Eleni's trips` (should be declined)
- `What are my usual seat and meal preferences, and is anything open?` (answered from Memory via the profile)

## Models

Each agent gets the smallest model that does its job reliably, set in `MODELS` in `agents/common.py`:

| Agent | Default | Why |
|---|---|---|
| Travel Desk | `us.anthropic.claude-sonnet-5-5` | Holds the conversation, decides routing, enforces the profile → policy → confirm order. Mistakes here cost the most. |
| Flight Ops | `us.anthropic.claude-sonnet-5-5` | Chains several tools (change options → quote → confirm) and is the only agent that can write. |
| Traveler Profile | `us.anthropic.claude-haiku-5-5` | One tool call, then reshape the JSON. No judgement needed. |
| Policy | `us.anthropic.claude-haiku-5-5` | Looks up a rule and quotes it. The rules are fixed text, so a small model is enough. |

Why not one big model everywhere: a contact center answers every turn live, so latency and cost per call matter.
One caller turn can make 3 to 5 model calls across agents. Opus class models were tried first and are slower and
pricier than this workload needs. Anthropic list prices per million input/output tokens: Opus 5.5 $4/$20,
Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 (Bedrock pricing differs; check the Bedrock pricing page).

Why not small everywhere: the desk and Flight Ops make the decisions the hard rules depend on (when to ask for
confirmation, which ids to act on). Those stay on Sonnet until the eval set (steps 5 and 6) shows a smaller model
holds the same routing and grounding scores.

Override any agent with `MODEL_ID_<ROLE>` in `.env`, e.g. `MODEL_ID_FLIGHT_OPS=us.anthropic.claude-haiku-5-5`.
Like a prompt change, a model change needs an eval run before it is merged.

## Checks

Each module with non trivial logic has an assert based self check that needs no network and no AWS:

```bash
uv run python agents/common.py            # confirmation gate
uv run python tools/duffel.py             # Duffel normalizers
uv run python agents/traveler_profile.py  # history summary
```

Format the code before committing (Ruff, config in `pyproject.toml`):

```bash
uv run ruff format .
```

`scripts/seed_memory_sessions.py` asserts its matcher before running and fails on any cross traveler leak.

## Layout

```
agents/
  travel_desk.py       orchestrator + REPL
  flight_ops.py        Duffel read and write tools
  traveler_profile.py  bookings summary + Memory records
  policy.py            desk rules + fare conditions
  common.py            model factory, session, ownership, confirmation gate, Memory reads
tools/duffel.py        Duffel client + normalizers
scripts/
  seed_duffel_history.py   book past orders, write the fixture
  create_memory.py         create or reuse the Memory resource from agentcore.json
  seed_memory_sessions.py  seed sessions, measure recall and isolation
agentcore.json         infra config: Memory resource and strategies
evals/ground_truth/    versioned ground truth (personas.json)
evals/fixtures/        generated, gitignored
PLAN.md                design and step order
CLAUDE.md              working rules for this repo
```

Shortcuts are marked `# ponytail:` in the code, each naming its limit and upgrade path:
`grep -rn "ponytail:" agents tools scripts`.

## Known limits

- Search returns Duffel Airways (`ZZ`) offers only, so results are reproducible. Schedules and prices are not
  realistic.
- Some test fares offer no extra bags after booking, in which case `bag_options` returns an empty list.
- Policy rules are inline text in `agents/policy.py`, not a knowledge base yet.
- One caller per process. The session is a module level dict.
- Confirmation is detected with a regex on the start of the user's message.
- The profile lists all of a traveler's Memory records rather than searching them, which is fine at about
  60 records per traveler. Memory infra is created by a script reading `agentcore.json`, not by CDK yet.

## Roadmap

1. ~~Duffel test account + seeded history~~
2. ~~Strands agents locally, Duffel direct~~
3. ~~AgentCore Memory (UserPreference, Semantic, Summary) + session manager + seeded sessions~~
4. Duffel tools behind AgentCore Gateway, Cedar (default deny, writes only for Flight Ops), Bedrock Guardrails,
   deploy to Runtime (`agentcore configure -e agents/travel_desk.py && agentcore launch`)
5. Eval dataset (~150 cases) + ground truth
6. AgentCore Evaluations + Strands Decider calibration + CloudWatch dashboard
